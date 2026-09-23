"""角色扮演场景下的子代理呼叫插件。

主对话模型不使用工具，改为输出呼叫标签（如 ``[呼叫Iris:查今天的天气]``）。
本插件在 ``on_agent_done`` 钩子里检测标签，然后：

1. 把标签从「发给用户的消息」中清洗掉（历史记录里保留标签）；
2. 发送一条子代理启动提示；
3. 启动一个拥有完整工具与 Skill 的隔离子代理去执行任务；
4. 把子代理结果回传主模型，生成终稿并作为本轮最终回复。
"""

from __future__ import annotations

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.astr_agent_context import AstrAgentContext
from astrbot.core.provider.entities import LLMResponse

from .callout.config import CalloutConfig
from .callout.runner import EXTRA_PARTS_KEY, GUARD_KEY, run_callouts
from .callout.tags import parse_calls, strip_tags

PLUGIN_NAME = "astrbot_plugin_subagent_call"


@register(PLUGIN_NAME, "iris1598", "角色扮演子代理呼叫", "0.1.0")
class SubAgentCalloutPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.cfg = CalloutConfig(config)
        self._warn_if_streaming()

    @filter.on_llm_request()
    async def inject_callout_rules(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> None:
        """把呼叫规则注入系统提示词（内容稳定，不影响提示词缓存）。"""
        if event.get_extra(GUARD_KEY):
            return

        # 记下本轮其它插件注入的动态内容块（好感度档位、时间、用户名等），
        # 终稿生成时复用，避免终稿缺少这些上下文。
        event.set_extra(EXTRA_PARTS_KEY, list(req.extra_user_content_parts or []))

        if not self.cfg.enabled or not self.cfg.inject_rules:
            return

        rules = self.cfg.render_rules()
        if not rules:
            return
        req.system_prompt = f"{req.system_prompt or ''}\n\n{rules}\n"

    @filter.on_agent_done()
    async def handle_callout(
        self,
        event: AstrMessageEvent,
        run_context: ContextWrapper[AstrAgentContext],
        resp: LLMResponse,
    ) -> None:
        """检测呼叫标签并执行三段式流程。"""
        if not self.cfg.enabled or event.get_extra(GUARD_KEY):
            return

        text = resp.completion_text or ""
        if not text:
            return

        reasons = parse_calls(text)
        if not reasons:
            return

        if self.cfg.debug_log:
            logger.info(
                "subagent_call: 检测到 "
                + f"{len(reasons)} 个呼叫 -> "
                + " | ".join(reason or "(未说明)" for reason in reasons)
            )

        event.set_extra(GUARD_KEY, True)
        try:
            cleaned = strip_tags(text)

            # ① 清洗掉标签的正文
            if self.cfg.send_first_text and cleaned:
                await event.send(event.plain_result(cleaned))

            # ② 启动提示 + 子代理执行 + ③ 终稿生成
            final = await run_callouts(
                self.context,
                event,
                run_context,
                reasons,
                self.cfg,
                fallback_text=cleaned,
            )

            if final:
                resp.completion_text = final
            elif cleaned:
                # 没有子代理真正执行时，至少不要泄露标签
                resp.completion_text = cleaned
            # 否则保持原样：避免把 completion_text 置空导致本轮历史不被保存
        except Exception as exc:
            logger.error(f"subagent_call: 呼叫流程失败: {exc}", exc_info=True)
            notice = self.cfg.error_notice
            if notice:
                resp.completion_text = notice
        finally:
            event.set_extra(GUARD_KEY, False)

    async def terminate(self) -> None:
        """插件被卸载/停用时调用。"""

    def _warn_if_streaming(self) -> None:
        try:
            config = self.context.get_config()
        except Exception:
            return
        settings = (
            config.get("provider_settings", {}) if isinstance(config, dict) else {}
        )
        if settings.get("streaming_response", False):
            logger.warning(
                "subagent_call: 检测到全局开启了流式输出"
                "（provider_settings.streaming_response）。流式模式下标签会在本插件"
                "清洗之前就推送出去，呼叫流程无法正常工作，请关闭流式输出后重载插件。"
            )
