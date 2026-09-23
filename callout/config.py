"""插件配置的读取、默认值与文案渲染。"""

from __future__ import annotations

from typing import Any

from .tags import (
    DEFAULT_RULES_TEMPLATE,
    DEFAULT_SUBAGENT_NAME,
    render_example_tag,
)

DEFAULT_START_NOTICE = "🔔 正在呼叫 {name}…"

DEFAULT_ERROR_NOTICE = "⚠️ 子代理执行失败，请稍后再试。"

DEFAULT_SUBAGENT_TASK_TEMPLATE = (
    "你需要执行下面这个由主对话交给你的任务。\n"
    "<task>\n{reason}\n</task>\n\n"
    "要求：\n"
    "1. 需要事实、实时信息或外部操作时，主动调用你可用的工具，不要臆测或编造。\n"
    "2. 回复先给出结论性的结果，再补充必要的关键细节（数据、来源、路径等）。\n"
    "3. 你的输出会被回传给主对话模型，请不要复述这段任务说明。"
)

DEFAULT_RESULT_BLOCK_TEMPLATE = (
    "<subagent_results>\n"
    "子代理已完成你呼叫的任务。\n"
    "任务：{reason}\n"
    "执行结果：\n{result}\n"
    "</subagent_results>\n\n"
    "请以上述结果为准，结合你的人设生成面向用户的最终回复：\n"
    "- 把结果自然地融入角色扮演，不要提及“标签”“系统”“子代理”“工具”等元信息。\n"
    "- 不要再输出任何呼叫标签。"
)


class _FormatMapping(dict):
    """缺失占位符时原样保留，避免用户模板里的多余花括号导致格式化失败。"""

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def safe_format(template: str, **values: Any) -> str:
    try:
        return template.format_map(_FormatMapping(values))
    except Exception:
        return template


class CalloutConfig:
    """包装 AstrBotConfig，提供带默认值与类型兜底的只读访问。"""

    def __init__(self, raw: Any = None) -> None:
        self._raw: dict = raw if isinstance(raw, dict) else {}

    # ---- 基础开关 ----
    @property
    def enabled(self) -> bool:
        return self._bool("enabled", True)

    @property
    def inject_rules(self) -> bool:
        return self._bool("inject_rules", True)

    @property
    def debug_log(self) -> bool:
        return self._bool("debug_log", False)

    @property
    def strip_media(self) -> bool:
        return self._bool("strip_media", True)

    @property
    def error_notice(self) -> str:
        return self._str("error_notice")

    # ---- 提示词模板 ----
    @property
    def tag_rules(self) -> str:
        return self._str("tag_rules")

    @property
    def subagent_task_template(self) -> str:
        return self._str("subagent_task_template") or DEFAULT_SUBAGENT_TASK_TEMPLATE

    @property
    def result_block_template(self) -> str:
        return self._str("result_block_template") or DEFAULT_RESULT_BLOCK_TEMPLATE

    # ---- 唯一的子代理 ----
    @property
    def subagent_name(self) -> str:
        return self._str("subagent_name") or DEFAULT_SUBAGENT_NAME

    @property
    def subagent_persona_id(self) -> str:
        return self._str("subagent_persona_id")

    @property
    def subagent_provider_id(self) -> str:
        return self._str("subagent_provider_id")

    @property
    def subagent_max_steps(self) -> int:
        return self._positive_int(self._raw.get("subagent_max_steps"), 15)

    # ---- 上下文窗口 ----
    @property
    def context_window(self) -> int:
        return self._non_negative_int(self._raw.get("context_window"), 6)

    @property
    def enable_history_tool(self) -> bool:
        return self._bool("enable_history_tool", True)

    @property
    def history_tool_max_messages(self) -> int:
        return self._positive_int(self._raw.get("history_tool_max_messages"), 40)

    # ---- 用户可见行为 ----
    @property
    def send_first_text(self) -> bool:
        return self._bool("send_first_text", True)

    @property
    def start_notice_enabled(self) -> bool:
        return self._bool("start_notice_enabled", True)

    @property
    def start_notice(self) -> str:
        return self._str("start_notice") or DEFAULT_START_NOTICE

    # ---- 执行参数 ----
    @property
    def tool_call_timeout(self) -> int:
        return self._positive_int(self._raw.get("tool_call_timeout"), 120)

    @property
    def max_rounds(self) -> int:
        return self._positive_int(self._raw.get("max_rounds"), 1)

    # ---- 渲染 ----
    def render_rules(self) -> str:
        return safe_format(
            self.tag_rules or DEFAULT_RULES_TEMPLATE,
            example=render_example_tag(self.subagent_name),
            name=self.subagent_name,
        )

    def render_start_notice(self) -> str:
        return safe_format(self.start_notice, name=self.subagent_name)

    def render_task_prompt(self, reason: str) -> str:
        return safe_format(
            self.subagent_task_template, reason=reason or "（未提供具体说明）"
        )

    def render_result_block(self, reason: str, result: str) -> str:
        return safe_format(
            self.result_block_template,
            reason=reason or "（未提供具体说明）",
            result=(result or "").strip() or "（子代理没有返回任何内容）",
        )

    # ---- 内部工具 ----
    def _str(self, key: str) -> str:
        value = self._raw.get(key)
        return value.strip() if isinstance(value, str) else ""

    def _bool(self, key: str, default: bool) -> bool:
        value = self._raw.get(key)
        return bool(value) if isinstance(value, bool) else default

    @staticmethod
    def _positive_int(value: Any, default: int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return default
        return parsed if parsed > 0 else default

    @staticmethod
    def _non_negative_int(value: Any, default: int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return default
        return parsed if parsed >= 0 else default
