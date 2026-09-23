"""子代理执行、上下文装配与终稿二次生成。"""

from __future__ import annotations

from typing import Any

from astrbot.api import logger
from astrbot.core.agent.message import (
    AssistantMessageSegment,
    Message,
    TextPart,
    UserMessageSegment,
)
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import ToolSet
from astrbot.core.astr_agent_context import AstrAgentContext
from astrbot.core.pipeline.context_utils import call_event_hook
from astrbot.core.skills import SkillManager, build_skills_prompt
from astrbot.core.star.star_handler import EventType

from .config import CalloutConfig
from .tags import parse_calls, strip_tags
from .tools import GetMainConversationHistoryTool

# 事件级 extra 键。由 main.py 的钩子写入，runner 读取。
GUARD_KEY = "_subagent_callout_active"
"""本轮已经在处理呼叫时的重入保护标记。"""

EXTRA_PARTS_KEY = "_subagent_callout_extra_parts"
"""本轮 on_llm_request 收集到的 extra_user_content_parts。

其它插件（如好感度）会通过这个字段注入动态状态（关系档位、时间、用户名等），
终稿生成时复用同一份，避免终稿丢掉这些上下文、语气跑偏。
"""

SUBAGENT_ROLE_TEMPLATE = (
    "你是「{name}」，一个被主对话临时呼叫出来的独立子代理（Sub-Agent）。\n"
    "你拥有完整的工具与技能，负责真正把交给你的任务执行完。\n"
    "- 任务由上面的对话上下文以及最后一轮用户消息给出，请自行判断用户到底想要什么。\n"
    "- 需要事实、实时信息或外部操作时调用工具，不要臆测，也不要编造。\n"
    "- 你只负责把任务做好并汇报结果，不要接着演下去。\n"
)

SKILLS_UNAVAILABLE_NOTE = (
    "\n注意：当前未启用 Computer Use（代码执行环境），你无法实际读取 SKILL.md 文件，"
    "请仅依据上面的技能描述判断，不要声称自己已经读过文件。\n"
)

# 转发上下文时，把多媒体内容块替换成占位文本，避免目标模型拒收。
# 原因：AstrBot 只在 provider 声明了 modalities 时才会剔除多媒体，
# 而主对话可能已经通过「图片描述模型」把图片转成了文字，遗留的原始
# image_url 往往已经失效，直接转发会触发 invalid_image 之类的 400 错误。
_MEDIA_PLACEHOLDERS = {
    "image_url": "[图片]",
    "image": "[图片]",
    "audio_url": "[语音]",
    "input_audio": "[语音]",
}


async def run_callouts(
    ctx: Any,
    event: Any,
    run_context: ContextWrapper[AstrAgentContext],
    reasons: list[str],
    cfg: CalloutConfig,
    fallback_text: str = "",
) -> str | None:
    """执行呼叫并在必要时做多轮，返回终稿文本。

    返回 None 表示没有真正执行过子代理，调用方应保持原回复不变。
    """
    final_text = ""
    executed_any = False

    for round_index in range(max(1, cfg.max_rounds)):
        if round_index > 0 and final_text:
            # 上一轮的终稿里还带着新标签，先把它（已去掉标签）发给用户，
            # 否则用户只会看到最后一轮，而中间轮次只存在于历史里。
            try:
                await event.send(event.plain_result(final_text))
            except Exception as exc:
                logger.warning(f"subagent_call: 发送中间轮回复失败: {exc}")

        blocks: list[str] = []
        for reason in reasons:
            executed_any = True
            if cfg.start_notice_enabled:
                try:
                    await event.send(event.plain_result(cfg.render_start_notice()))
                except Exception as exc:
                    logger.warning(f"subagent_call: 发送启动提示失败: {exc}")

            result = await _run_one_subagent(ctx, event, run_context, reason, cfg)
            blocks.append(cfg.render_result_block(reason, result))

        if not blocks:
            break

        final_text, raw_final = await _generate_final_reply(
            ctx, event, run_context, blocks, cfg, fallback_text or final_text
        )
        # 用未清洗的原文判断是否还有新标签，否则刚清掉就永远检测不到、多轮失效
        reasons = parse_calls(raw_final)
        if not reasons:
            break

    if not executed_any:
        return None
    return final_text


async def _run_one_subagent(
    ctx: Any,
    event: Any,
    run_context: ContextWrapper[AstrAgentContext],
    reason: str,
    cfg: CalloutConfig,
) -> str:
    """跑一次子代理的工具循环，返回它的最终文本。"""
    try:
        system_prompt = await _build_subagent_system_prompt(ctx, event, cfg)
        tools = _build_all_tools(ctx, run_context, cfg)
        contexts = _recent_window(
            run_context.messages, cfg.context_window, cfg.strip_media
        )
        provider_id = (
            cfg.subagent_provider_id
            or await ctx.get_current_chat_provider_id(event.unified_msg_origin)
        )

        if cfg.debug_log:
            logger.info(
                f"subagent_call: 呼叫子代理 | provider={provider_id} | "
                f"tools={len(tools.tools)} | "
                f"contexts={len(contexts)} | max_steps={cfg.subagent_max_steps}"
            )

        resp = await ctx.tool_loop_agent(
            event=event,
            chat_provider_id=provider_id,
            prompt=cfg.render_task_prompt(reason),
            system_prompt=system_prompt,
            contexts=contexts,
            tools=tools,
            max_steps=cfg.subagent_max_steps,
            tool_call_timeout=cfg.tool_call_timeout,
        )
        if getattr(resp, "role", "") == "err":
            logger.error(f"subagent_call: 子代理请求失败: {resp.completion_text}")
            return f"（子代理请求失败：{resp.completion_text}）"
        return resp.completion_text or ""
    except Exception as exc:
        logger.error(f"subagent_call: 子代理执行失败: {exc}", exc_info=True)
        return f"（子代理执行失败：{exc}）"


async def _build_subagent_system_prompt(
    ctx: Any, event: Any, cfg: CalloutConfig
) -> str:
    parts: list[str] = []

    if cfg.subagent_persona_id:
        persona = ctx.persona_manager.get_persona_v3_by_id(cfg.subagent_persona_id)
        persona_prompt = persona.get("prompt") if isinstance(persona, dict) else None
        if persona_prompt:
            parts.append(persona_prompt)

    skills_section = _build_skills_section(ctx, event)
    if skills_section:
        parts.append(skills_section)

    parts.append(SUBAGENT_ROLE_TEMPLATE.format(name=cfg.subagent_name))

    return "\n\n".join(parts)


def _build_skills_section(ctx: Any, event: Any) -> str:
    runtime = _resolve_runtime(ctx, event)
    try:
        skills = SkillManager().list_skills(active_only=True, runtime=runtime)
    except Exception as exc:
        logger.warning(f"subagent_call: 读取 Skill 列表失败: {exc}")
        return ""
    if not skills:
        return ""

    section = build_skills_prompt(skills)
    if runtime == "none":
        section += SKILLS_UNAVAILABLE_NOTE
    return section


def _resolve_runtime(ctx: Any, event: Any) -> str:
    try:
        config = ctx.get_config(umo=event.unified_msg_origin)
    except Exception:
        return "none"
    provider_settings = (
        config.get("provider_settings", {}) if isinstance(config, dict) else {}
    )
    runtime = provider_settings.get("computer_use_runtime", "none")
    return str(runtime or "none")


def _build_all_tools(
    ctx: Any, run_context: ContextWrapper[AstrAgentContext], cfg: CalloutConfig
) -> ToolSet:
    """装配子代理可用的全部工具。"""
    toolset = _reuse_full_toolset(run_context)
    if toolset is None:
        toolset = _fallback_full_toolset(ctx)
    if toolset is None:
        toolset = ToolSet()

    if cfg.enable_history_tool:
        toolset.add_tool(
            GetMainConversationHistoryTool(max_messages=cfg.history_tool_max_messages)
        )
    return toolset


def _reuse_full_toolset(
    run_context: ContextWrapper[AstrAgentContext],
) -> ToolSet | None:
    """复用 AstrBot 内置的“全部工具”装配逻辑（主代理语义）。"""
    try:
        from astrbot.core.astr_agent_tool_exec import FunctionToolExecutor

        return FunctionToolExecutor._build_handoff_toolset(run_context, None)
    except Exception as exc:
        logger.warning(f"subagent_call: 复用内置全量工具装配失败，回退手动装配: {exc}")
        return None


def _fallback_full_toolset(ctx: Any) -> ToolSet | None:
    try:
        tool_manager = ctx.get_llm_tool_manager()
    except Exception as exc:
        logger.error(f"subagent_call: 获取工具管理器失败: {exc}")
        return None

    toolset = ToolSet()
    try:
        for tool in tool_manager.get_full_tool_set().tools:
            toolset.add_tool(tool)
    except Exception as exc:
        logger.warning(f"subagent_call: 装配已注册工具失败: {exc}")
    try:
        for tool in tool_manager.iter_builtin_tools():
            toolset.add_tool(tool)
    except Exception as exc:
        logger.warning(f"subagent_call: 装配内置工具失败: {exc}")

    return toolset if toolset.tools else None


def _recent_window(
    messages: list[Message], size: int, strip_media: bool = True
) -> list[Message]:
    """取主对话最近 size 条可用的消息作为子代理的背景上下文。"""
    if size <= 0:
        return []
    window: list[Message] = []
    for message in reversed(messages):
        sanitized = _sanitize_message(message, strip_media=strip_media)
        if sanitized is None:
            continue
        window.append(sanitized)
        if len(window) >= size:
            break
    window.reverse()
    return window


async def _generate_final_reply(
    ctx: Any,
    event: Any,
    run_context: ContextWrapper[AstrAgentContext],
    blocks: list[str],
    cfg: CalloutConfig,
    fallback_text: str = "",
) -> tuple[str, str]:
    """把子代理结果回传主模型，生成面向用户的终稿，并把这段对话写回历史。

    返回 ``(发给用户的终稿, 模型原始输出)``：前者已清掉残留的呼叫标签，
    后者用于判断是否还有新的呼叫（多轮）。
    """
    messages = list(run_context.messages)
    system_text = ""
    start = 0
    if messages and messages[0].role == "system":
        system_text = _message_to_text(messages[0])
        start = 1

    bridge = UserMessageSegment(content=[TextPart(text="\n\n".join(blocks))])
    provider_id = await ctx.get_current_chat_provider_id(event.unified_msg_origin)
    extra_parts = event.get_extra(EXTRA_PARTS_KEY) or None

    try:
        resp = await _request_final_reply(
            ctx,
            provider_id,
            messages[start:],
            system_text,
            bridge,
            cfg.strip_media,
            extra_parts,
        )
    except Exception as exc:
        if cfg.strip_media:
            raise
        # 未剔除多媒体时失败，可能是上下文里的失效图片/语音被拒收，退一步重试一次
        logger.warning(
            f"subagent_call: 终稿生成失败，剔除多媒体后重试一次: {exc}",
        )
        resp = await _request_final_reply(
            ctx, provider_id, messages[start:], system_text, bridge, True, extra_parts
        )

    # 终稿是本插件自己发起的一次生成，不经过 Agent runner，
    # 所以其它插件的 on_llm_response 不会自动触发。这里手动分发一次，
    # 好感度之类的插件就能照常清洗、记账它自己要求的标签——
    # 我们不需要去逐个适配别人的标签格式。
    await _notify_llm_response(event, resp)

    # 上面的钩子可能已经改过 completion_text，所以要在这之后再读。
    raw_final = (resp.completion_text or "").strip()
    # 只清自己的呼叫标签；其它插件的标签由它们自己在上面那步处理。
    final_text = strip_tags(raw_final) or fallback_text

    run_context.messages.append(bridge)
    if final_text:
        run_context.messages.append(
            AssistantMessageSegment(content=[TextPart(text=final_text)])
        )
    return final_text, raw_final


async def _request_final_reply(
    ctx: Any,
    provider_id: str,
    history_messages: list[Message],
    system_text: str,
    bridge: Message,
    strip_media: bool,
    extra_parts: list[Any] | None = None,
) -> Any:
    contexts: list[dict] = []
    for message in history_messages:
        sanitized = _sanitize_message(message, strip_media=strip_media)
        if sanitized is None:
            continue
        contexts.append(sanitized.model_dump())
    contexts.append(bridge.model_dump())

    return await ctx.llm_generate(
        chat_provider_id=provider_id,
        system_prompt=system_text,
        contexts=contexts,
        extra_user_content_parts=extra_parts,
    )


async def _notify_llm_response(event: Any, resp: Any) -> None:
    """把终稿分发给其它插件的 ``on_llm_response`` 钩子。

    终稿走 ``llm_generate``，不经过 Agent runner，这些钩子不会自动触发；
    手动分发一次，让拥有这些标签的插件自己去清洗与记账，本插件无需适配。

    只分发 ``on_llm_response``，不触发 ``on_agent_done``——本插件的处理逻辑挂在
    后者上，不触发它也就天然没有递归问题。钩子里抛错不应该影响终稿，故吞掉异常。
    """
    try:
        await call_event_hook(event, EventType.OnLLMResponseEvent, resp)
    except Exception as exc:
        logger.warning(f"subagent_call: 终稿分发 on_llm_response 失败: {exc}")


def _sanitize_message(message: Message, *, strip_media: bool = False) -> Message | None:
    """剔除系统消息、检查点消息、仅本轮生效的临时内容，以及多媒体内容块。"""
    if message.role in ("system", "_checkpoint"):
        return None

    content = message.content
    if not isinstance(content, list):
        return message

    kept: list[Any] = []
    changed = False
    for part in content:
        if getattr(part, "_no_save", False):
            changed = True
            continue
        part_type = getattr(part, "type", None)
        if strip_media and part_type in _MEDIA_PLACEHOLDERS:
            kept.append(TextPart(text=_MEDIA_PLACEHOLDERS[part_type]))
            changed = True
            continue
        kept.append(part)

    if not kept:
        return None
    if changed:
        return Message(role=message.role, content=kept)
    return message


def _message_to_text(message: Message) -> str:
    content = message.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            getattr(part, "text", "")
            for part in content
            if getattr(part, "type", None) == "text"
        ]
        return "\n".join(part for part in parts if part)
    return ""
