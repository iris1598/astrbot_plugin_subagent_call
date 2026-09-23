"""呼叫标签的解析与清洗，以及内置提示词文本。

标签形如 ``[呼叫Iris:原因理由]`` 或 ``[呼叫:原因理由]``。冒号前的内容
只是给人看的标签，解析时不参与路由（只有一个子代理），所以写什么都不影响触发。
内置的规则文本、任务模板与结果块模板以常量形式导出，并作为
_conf_schema.json 中对应配置项的默认值，方便用户直接参考与修改。
"""

from __future__ import annotations

import re

# 兼容中英文冒号；冒号前的标签内容可省略，reason 不含右括号/换行
DEFAULT_PATTERN = r"\[呼叫\s*[^:\]：\n]*?\s*[:：]\s*(?P<reason>[^\]\n]*?)\s*\]"

# 全局唯一的标签正则
PATTERN = re.compile(DEFAULT_PATTERN)

# 默认的子代理名字，只用于渲染规则里的示例标签与启动提示
DEFAULT_SUBAGENT_NAME = "Iris"

# 单轮最多执行的呼叫数量，避免模型刷屏导致请求风暴
MAX_CALLOUTS_PER_TURN = 5

# 原因理由的长度上限
MAX_REASON_LEN = 500

# 注入系统提示词的呼叫规则。可用占位符：{example}
DEFAULT_RULES_TEMPLATE = (
    "# 外部能力与子代理呼叫\n"
    "你没有函数调用（function calling）能力，无法直接使用任何工具，"
    "也不能联网、读写文件或执行代码。\n"
    "当任务确实需要这些外部能力时，不要编造结果，也不要假装已经完成，"
    "而是输出一个「呼叫标签」，把任务交给具备完整工具的隔离子代理去执行。\n"
    "\n"
    "标签格式（必须原样保留方括号、冒号与原因）：{example}\n"
    "\n"
    "规则：\n"
    "1. 仅在确实需要外部能力时输出标签；日常闲聊与角色扮演不要输出。\n"
    "2. 标签不会展示给用户。请在标签之外正常继续角色扮演的正文，正文会照常发给用户。\n"
    "3. 一次回复可以输出多个标签，每个单独占一行。\n"
    "4. 输出标签后请结束本轮正文；系统会把子代理的执行结果回传给你，"
    "你再据此生成面向用户的最终回复。\n"
    "5. 最终回复要保持你的人设与语气，自然地融入子代理的发现，"
    "但不要提及“标签”“系统”“子代理”“工具”等元信息，也不要再输出标签。\n"
)


def render_example_tag(name: str) -> str:
    """渲染规则里的示例标签。"""
    return f"[呼叫{name.strip() or DEFAULT_SUBAGENT_NAME}:查一下今天的天气]"


def parse_calls(text: str, pattern: re.Pattern = PATTERN) -> list[str]:
    """从文本中解析出全部呼叫的原因理由（最多 MAX_CALLOUTS_PER_TURN 个）。"""
    if not text:
        return []
    reasons: list[str] = []
    for match in pattern.finditer(text):
        reasons.append((match.group("reason") or "").strip()[:MAX_REASON_LEN])
        if len(reasons) >= MAX_CALLOUTS_PER_TURN:
            break
    return reasons


def _tidy(text: str) -> str:
    """收拾删掉标签后产生的多余空白。"""
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def strip_tags(text: str, pattern: re.Pattern = PATTERN) -> str:
    """删除全部呼叫标签，并收拾由此产生的多余空白。"""
    if not text:
        return ""
    return _tidy(pattern.sub("", text))
