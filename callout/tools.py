"""子代理专用的主对话历史查询工具。

该工具不会注册到全局工具表，只在构造子代理 ToolSet 时加入实例，
因此不会出现在主代理或其他插件的工具列表里。
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import Field
from pydantic.dataclasses import dataclass

from astrbot.api import logger
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext

DEFAULT_LIMIT = 20
MAX_LIMIT = 100

_ROLE_LABELS = {
    "user": "用户",
    "assistant": "角色",
    "tool": "工具",
    "system": "系统",
}


@dataclass
class GetMainConversationHistoryTool(FunctionTool[AstrAgentContext]):
    name: str = "get_main_conversation_history"
    description: str = (
        "查询主对话（用户正在进行中的角色扮演会话）的历史消息记录。"
        "当交接给你的任务描述信息不足、出现指代不明的说法，"
        "或需要更早的上下文才能判断用户到底想要什么时，调用本工具。"
        "默认返回最近 20 条，可用 limit / offset 向前翻页查看更早内容。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "number",
                    "description": "返回的消息条数，默认 20，最大 100。",
                },
                "offset": {
                    "type": "number",
                    "description": "从最新一条往前跳过的条数，默认 0，即从最新开始。",
                },
            },
            "required": [],
        }
    )
    max_messages: int = 40

    async def call(
        self, context: ContextWrapper[AstrAgentContext], **kwargs: Any
    ) -> ToolExecResult:
        plugin_ctx = getattr(context.context, "context", None)
        event = getattr(context.context, "event", None)
        if plugin_ctx is None or event is None:
            return "error: 无法访问 AstrBot 上下文，本工具只有在 AstrBot 会话中才可用。"

        limit = _clamp(kwargs.get("limit"), DEFAULT_LIMIT, 1, self.max_messages)
        offset = _clamp(kwargs.get("offset"), 0, 0, 100_000)

        try:
            history = await _load_history(plugin_ctx, event.unified_msg_origin)
        except Exception as exc:
            logger.error(f"subagent_call: 读取主对话历史失败: {exc}", exc_info=True)
            return f"error: 读取主对话历史失败: {exc}"

        if not history:
            return "（暂时没有可用的主对话历史记录）"

        end = len(history) - offset
        if end <= 0:
            return f"（主对话共 {len(history)} 条记录，已没有更早的内容了）"
        start = max(0, end - limit)

        lines: list[str] = []
        for index in range(start, end):
            rendered = _render_message(history[index])
            if rendered:
                lines.append(f"{index + 1}. {rendered}")
        if not lines:
            return f"（第 {start + 1}-{end} 条记录中没有可展示的文本内容）"

        header = (
            f"主对话历史共 {len(history)} 条，以下为第 {start + 1}-{end} 条"
            f"（越靠后越新，最近的是第 {len(history)} 条）："
        )
        return header + "\n" + "\n".join(lines)


async def _load_history(plugin_ctx: Any, umo: str) -> list[dict]:
    conv_mgr = plugin_ctx.conversation_manager
    conversation_id = await conv_mgr.get_curr_conversation_id(umo)
    if not conversation_id:
        return []
    conversation = await conv_mgr.get_conversation(umo, conversation_id)
    if conversation is None or not conversation.history:
        return []

    raw = conversation.history
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return []
    if not isinstance(raw, list):
        return []

    messages: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        if item.get("role") == "_checkpoint":
            continue
        messages.append(item)
    return messages


def _render_message(item: dict) -> str | None:
    role = str(item.get("role", "unknown"))
    label = _ROLE_LABELS.get(role, role)
    text = _content_to_text(item.get("content"))
    if not text:
        return None
    return f"[{label}] {text}"


def _content_to_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return str(content).strip()

    parts: list[str] = []
    for part in content:
        if isinstance(part, str):
            parts.append(part)
            continue
        if not isinstance(part, dict):
            continue
        if part.get("_no_save"):
            continue
        part_type = part.get("type")
        if part_type == "text":
            parts.append(str(part.get("text", "")))
        elif part_type == "image_url":
            parts.append("[图片]")
        elif part_type == "audio_url":
            parts.append("[语音]")
    return "\n".join(part for part in parts if part).strip()


def _clamp(value: Any, default: int, low: int, high: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, parsed))
