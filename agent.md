# agent.md — 子代理呼叫插件 · AI 开发指南

> 面向在本插件目录工作的 AI / 开发者：**先读这里再动手**。
> 重要改动后请在同一次改动里更新本文件（见最后一节）。

---

## 0. 一句话概览

角色扮演场景下，主对话模型**不启用工具**，改为输出简易标签（如 `[呼叫Iris:查今天天气]`）；
插件清洗标签 → 启动一个拥有完整工具与 Skill 的**隔离子代理**干活 → 把结果回传主模型 → 生成终稿。

| 项 | 值 |
| :--- | :--- |
| 框架 / 版本要求 | AstrBot `>=4.23.1`（`metadata.yaml: astrbot_version`，因为依赖 `on_agent_done` 钩子），Python 3.10+ |
| 插件身份 | `metadata.yaml`: `name: astrbot_plugin_subagent_call` / `author: iris1598`；市场 `plugin_id = author/name`，必须与包内 `metadata.yaml` 逐字一致 |
| 版本号 | 两处保持一致：`metadata.yaml: version`（`v0.1.0`）与 `main.py` 的 `@register(..., "0.1.0")` |
| 入口 | `main.py` → 插件类 `SubAgentCalloutPlugin`（类名必须以 `Plugin` 结尾） |
| 实现包 | `callout/`（`config` / `runner` / `tags` / `tools`） |
| 依赖 | 无第三方依赖（只用 AstrBot API + 标准库），`requirements.txt` 为空 |
| 前提 | 主对话人格**不启用工具**；AstrBot **关闭流式输出**（流式下标签会在清洗前推送出去） |

---

## 1. 铁律（改代码前必读）

1. **所有 `@filter.*` Handler 必须写在 `main.py`。**
   AstrBot 用 `handler.handler_module_path == 插件模块路径` **精确匹配**归属 Handler，放到别处会被静默丢弃、功能静默失效。业务逻辑放 `callout/`，Handler 壳子不许离开 `main.py`。
   当前只有 2 个 Handler：`inject_callout_rules`（`on_llm_request`）、`handle_callout`（`on_agent_done`）。
2. **不要改插件身份**：`metadata.yaml` 的 `name`、目录名、`_conf_schema.json` 的配置键名。改了会切断用户已有配置的读取。
3. **`_conf_schema.json` 里的三个模板默认值必须与代码常量逐字一致**（见第 5 节与验证清单）——它们是「给用户看的默认值」，真正的兜底在 `callout/tags.py` 与 `callout/config.py` 的常量里。两处不一致会导致「面板上看到一套、实际跑另一套」。
4. **日志器必须且只能从 `astrbot.api` 导入**：`from astrbot.api import logger`。
   **严禁** `import logging`、`getLogger`、`basicConfig`、`(File|Stream)Handler`，也**不要**动框架 logger 的状态（`setLevel` / `addHandler` / …）。这是上架审核的硬性规则，提交前跑第 7 节的自查命令。
5. **不要自己去适配别的插件的标签。** 终稿的 `on_llm_response` 由本插件手动分发（见 4.3），谁拥有那些标签谁自己清洗与记账。**不要**再加「通吃方括号」之类的兜底（历史上加过，已按需求移除）。
6. **不要执行 git 远端操作**（`pull` / `push`），`status` / `diff` 只读查看随意。
7. 行为敏感点：错误文案、日志文案、`event` extra 的键名、消息条数、配置默认值，非必要不改。

---

## 2. 目录结构

```text
astrbot_plugin_subagent_call/
├── main.py               # 插件类 + 全部 @filter.* Handler（必须留在这里）
├── metadata.yaml         # name / display_name / desc / version / author / repo / astrbot_version
├── _conf_schema.json     # WebUI 配置（三个模板项的默认值 = 代码里的内置文本）
├── requirements.txt      # 空（无第三方依赖）
├── README.md             # 使用者文档
├── agent.md              # ← 本文件
└── callout/
    ├── config.py         # 配置读取（CalloutConfig）+ 内置文案常量 + 占位符渲染
    ├── tags.py           # 标签正则、解析、清洗、示例标签
    ├── runner.py         # 子代理执行、上下文装配、终稿生成、钩子分发
    └── tools.py          # GetMainConversationHistoryTool（只给子代理用的历史查询工具）
```

---

## 3. 核心数据流（一次呼叫的完整链路）

```
用户消息 → AstrBot 管线
 ├─ on_llm_request（本插件）          往 req.system_prompt 追加呼叫规则；记下 extra_user_content_parts
 │                                    ⚠️ 只改 system_prompt，绝不碰 image_urls / contexts / func_tool
 ├─ Agent 运行，assistant 原文（含 [呼叫…] 与其他插件的标签）写入 run_context.messages
 │                                    ↑ 这一步发生在钩子之前，所以历史里天然保留标签
 ├─ on_llm_response（其它插件）        各插件清洗自己的标签（如好感度的 [FAV:+5]）
 └─ on_agent_done（本插件）            ← 本插件的全部逻辑
      1. parse_calls(text) 有标签？
      2. guard 置位（防重入）
      3. ① event.send(清洗掉标签的第一段正文)
      4. ② 循环每个呼叫：发启动提示 → 跑子代理 → 收集结果块
      5. ③ 生成终稿：回传结果块给主模型 → **分发 on_llm_response** → 清自己的呼叫标签
      6. resp.completion_text = 终稿（→ 用户看到的是它）
      7. run_context.messages 追加 [子代理结果(user) → 终稿(assistant)]
```

**两条路径必须分清楚**（这是本插件能成立的根基）：

- `resp.completion_text` 改的是**发给用户的内容**；
- `run_context.messages` 才是**写进历史的内容**，且它在钩子触发**之前**就已用原文构建完毕。

所以「标签保留在历史、但从发给用户的消息里去掉」不需要任何额外操作。详见 4.1。

---

## 4. 关键接口契约

### 4.1 为什么挂在 `on_agent_done`

- 它**晚于** `on_llm_response`，因此读到的 `text` 已被其它插件清洗过（顺序对我们有利）。
- 它**早于**待发送 chain 的构建，所以此时改 `resp.completion_text` 能生效。
- 它拿得到 `run_context.messages`，这是当前版本唯一能影响「本轮历史」的注入点。
- 它是**主代理专属**：只有 `MainAgentHooks` 会 `call_event_hook`；子代理用 `tool_loop_agent` 时走空实现的 `BaseAgentRunHooks`，**不会递归触发本插件的钩子**。

签名固定为 `(self, event, run_context, resp)`。

### 4.2 `run_context.messages` 是内部结构

往里 append 消息是本插件**有意**依赖 AstrBot 内部实现的行为，升级 AstrBot 后需要回归测试历史格式。
已刻意回避更深的私有细节，只做 `role == "_checkpoint"` 过滤与 `_no_save` 内容块过滤。

追加的两条消息：
- `UserMessageSegment`（结果块，role=`user`）→ 让下一轮模型知道「叫过子代理、拿到了什么」；
- `AssistantMessageSegment`（终稿）→ 与实际发送内容一致。

### 4.3 终稿必须分发 `on_llm_response`

终稿走 `llm_generate`，**不经过 Agent runner**，所以其它插件的 `on_llm_response` 不会自动触发。
`runner._notify_llm_response()` 手动调一次 `call_event_hook(event, EventType.OnLLMResponseEvent, resp)`：

- 让拥有标签的插件自己去清洗与记账（**不要**改回「我们自己猜标签」）；
- 只分发 `on_llm_response`，**不触发 `on_agent_done`**（本插件逻辑挂在后者上，不触发即天然无递归）；
- **必须在分发「之后」再读 `resp.completion_text`**，插件正是通过改写它来清洗的；
- 分发失败只记 warning，不中断终稿。

### 4.4 标签格式与容错

格式固定 `[呼叫<任意标签>:原因理由]`（中英文冒号均可），**不可配置**。
冒号前的内容**不参与路由**（只有一个子代理），所以 `[呼叫:查天气]`、`[呼叫随便什么名字:查天气]` 一样触发——
这是**有意为之**：写死校验代理名会让模型拼错名字时静默失效。

- 单轮最多处理 `MAX_CALLOUTS_PER_TURN`（5）个呼叫；
- reason 截断到 `MAX_REASON_LEN`（500）；
- 空 reason 允许（任务模板会填「未提供具体说明」）。

### 4.5 终稿只清自己的标签

终稿发出前用 `strip_tags` 清 `[呼叫…]`，**其它方括号一律不动**（`[FAV:+5]`、`[叹气]` 都保留）。
理由见铁律 5。

**判断「是否还有新呼叫」必须用未清洗的原文**（`_generate_final_reply` 返回 `(发给用户的终稿, 原始输出)`）——
用清洗后的文本判断会让多轮永远失效（踩过：清洗完就检测不到标签，多轮退化成单轮）。

### 4.6 `strip_media`：转发上下文时剔除图片/语音

`_sanitize_message(..., strip_media=True)` 把 `image_url` / `audio_url` 块换成 `[图片]` / `[语音]` 占位符。
**只作用于「子代理上下文」与「终稿上下文」这两条本插件发起的请求**，主模型自己的请求完全不受影响。

原因：AstrBot 只在 provider **声明了 `modalities`** 时才会剔除多媒体；而主对话可能走的是图片描述模型
（图片被转成文字喂给主模型，但**原始 `image_url` 仍留在历史里**），这类链接很容易过期，
直接转发会得到 `400 invalid_image: Downloaded response does not contain a valid JPG, PNG, ...`。

终稿另有兜底：若关闭 `strip_media` 后请求失败，会自动剔除多媒体重试一次。

### 4.7 子代理的权限与工具

- **权限自动继承**：子代理用的是主对话那个 `event`（同角色、同 umo），所以 AstrBot 的
  `computer_use_local_permissions`（`filesystem_scope` / `allow_execution` / `allow_network`）、
  每工具的管理员校验、工作区解析全部照常生效。**不要**改传别的 event。
- **工具是全量**：`_build_all_tools()` 用 `FunctionToolExecutor._build_handoff_toolset(run_context, None)`
  （等于主代理语义下的「全部工具」：插件工具 + MCP 工具 + 按 `computer_use_runtime` 装配的
  本地/沙箱文件·shell·python 工具）。
  这是**私有 API**，已包 try/except，失败回退到 `get_full_tool_set()` + `iter_builtin_tools()`。
  ⚠️ 全量意味着**不遵守主人格的 `tools` 白名单**——这是产品决策，别顺手「修好」。
- **历史查询工具**（`callout/tools.py`）**不注册到全局**，只在构造子代理 ToolSet 时加实例，
  因此不会出现在主代理或其他插件的工具列表里。

### 4.8 上下文注入范围

- 子代理拿主对话最近 `context_window`（默认 6）条消息作背景（跳过 system / `_checkpoint` / `_no_save`）；
- 终稿复用**本轮**的 `extra_user_content_parts`（`EXTRA_PARTS_KEY`），避免丢掉其它插件注入的
  关系档位 / 时间 / 用户名等动态状态；缺失时传 `None` 而非空列表；
- 子代理系统提示词 = 人格 → Skill 清单 → 固定角色说明。**不要再往里塞「吐槽风格」之类的风格规训**，
  风格属于人格的事。

---

## 5. 配置项

`_conf_schema.json` 共 20 项。三个模板项的默认值**就是内置文本**，方便用户直接改：

| 配置键 | 默认 | 说明 |
| :--- | :--- | :--- |
| `enabled` / `inject_rules` | 开 | 总开关 / 是否注入规则 |
| `tag_rules` | `tags.DEFAULT_RULES_TEMPLATE` | 呼叫规则（占位符 `{example}`、`{name}`） |
| `subagent_name` | `Iris` | 只影响示例标签与启动提示，**不影响识别** |
| `subagent_persona_id` / `subagent_provider_id` | 空 | `_special: select_persona` / `select_provider` |
| `subagent_max_steps` | 15 | 子代理最大工具步数 |
| `context_window` | 6 | 注入子代理的历史条数 |
| `enable_history_tool` / `history_tool_max_messages` | 开 / 40 | 历史查询工具 |
| `strip_media` | 开 | 见 4.6 |
| `send_first_text` / `start_notice_enabled` / `start_notice` | 开 / 开 / `🔔 正在呼叫 {name}…` | 三段式可见行为 |
| `subagent_task_template` | `config.DEFAULT_SUBAGENT_TASK_TEMPLATE` | 占位符 `{reason}` |
| `result_block_template` | `config.DEFAULT_RESULT_BLOCK_TEMPLATE` | 占位符 `{reason}`、`{result}` |
| `error_notice` | `⚠️ 子代理执行失败，请稍后再试。` | 清空则不发送 |
| `tool_call_timeout` / `max_rounds` | 120 / 1 | 单次工具调用超时 / 呼叫轮数上限 |
| `debug_log` | 关 | 调试日志 |

占位符用 `safe_format` 渲染：**未知占位符原样保留、不会抛错**，所以用户多写花括号是安全的。

---

## 6. 「我要做 X，改哪里」速查表

| 任务 | 改动位置 | 注意 |
| :--- | :--- | :--- |
| 改标签格式 | `callout/tags.py::DEFAULT_PATTERN` + 规则模板里的示例 | 示例标签由 `render_example_tag(subagent_name)` 生成 |
| 改呼叫规则文案 | `callout/tags.py::DEFAULT_RULES_TEMPLATE` **+** `_conf_schema.json: tag_rules.default` | 两处必须一致（铁律 3） |
| 改任务/结果块模板 | `callout/config.py` 两个 `DEFAULT_*_TEMPLATE` **+** schema 对应 `default` | 同上 |
| 改三段式行为（发不发正文、提示文案） | `callout/config.py` 的渲染方法 + `main.py` | `send_first_text` 只影响第一段 |
| 改子代理能用的工具 | `callout/runner.py::_build_all_tools` | 注意会同时影响权限面，见 4.7 |
| 改注入子代理的上下文范围 | `callout/runner.py::_recent_window` / `_sanitize_message` | 别把 `_no_save` 过滤掉 |
| 改终稿生成 | `callout/runner.py::_generate_final_reply` | **别去掉 `_notify_llm_response`，也别用清洗后的文本判断多轮**（4.3 / 4.5） |
| 改历史写入 | `callout/runner.py::_generate_final_reply` 末尾的两次 append | 内部结构，改动后必回归历史格式（4.2） |
| 新增 / 修改配置项 | `_conf_schema.json` + `callout/config.py` 的 property | 记得同步本文件第 5 节与 `README.md` |
| 加日志 | 只用 `from astrbot.api import logger` | 见铁律 4 |
| 改子代理 System Prompt 结构 | `callout/runner.py::_build_subagent_system_prompt` | 人格 → Skill → 角色说明，顺序有语义 |

---

## 7. 验证清单（改完必跑）

```bash
# <PY> = 能 import astrbot 的解释器，即 AstrBot 运行环境所用的解释器
# 在插件目录的上级执行；路径按需替换

# 1) 静态检查 + 语法编译
<PY> -m ruff check data/plugins/astrbot_plugin_subagent_call
<PY> -m ruff format data/plugins/astrbot_plugin_subagent_call
<PY> -m compileall -q data/plugins/astrbot_plugin_subagent_call

# 2) 日志规范自查（审核要求；两条都应无输出）
grep -rnE "(import|from)\s+logging|logging\.[A-Za-z]|getLogger|basicConfig|(File|Stream)Handler" --include=*.py data/plugins/astrbot_plugin_subagent_call
grep -rnE "logger\.(setLevel|addHandler|removeHandler|handlers|propagate|filters)" --include=*.py data/plugins/astrbot_plugin_subagent_call

# 3) schema 默认值与代码常量是否一致（铁律 3）
<PY> -c "import sys,json,pathlib; \
p=pathlib.Path('data/plugins/astrbot_plugin_subagent_call'); sys.path.insert(0,str(p)); \
from callout.config import DEFAULT_SUBAGENT_TASK_TEMPLATE as T, DEFAULT_RESULT_BLOCK_TEMPLATE as R, DEFAULT_START_NOTICE as S, DEFAULT_ERROR_NOTICE as E; \
from callout.tags import DEFAULT_RULES_TEMPLATE as U, DEFAULT_SUBAGENT_NAME as N; \
d=json.loads((p/'_conf_schema.json').read_text(encoding='utf-8')); \
assert d['tag_rules']['default']==U and d['subagent_task_template']['default']==T and d['result_block_template']['default']==R; \
assert d['start_notice']['default']==S and d['error_notice']['default']==E and d['subagent_name']['default']==N; \
print('schema defaults OK')"

# 4) 真机：装到 AstrBot 插件目录后重载，按 README 的「验证」跑一遍三段式
```

**真机必查的几条**（自动化覆盖不到）：
1. 主对话有图时，**主模型仍能看图**（本插件不该影响它）；子代理侧不会因失效图片链接报 `invalid_image`。
2. 标签保留在历史里（WebUI 会话历史可见 `assistant(含标签) → user(子代理结果) → assistant(终稿)`）。
3. 同装好感度插件时：好感度标签被对方清掉、**且我方终稿没有吃掉 `[叹气]` 这类正文方括号**。
4. 全局开着流式时启动会打 WARNING（此时功能不可用）。

---

## 8. 已知坑

- **必须非流式**。流式下 delta 会在 `on_agent_done` 之前推给用户，标签会泄露、三段式失效。插件只警告，不擅自改配置。
- **`on_agent_done` 触发时机在会话锁内**，会延长本轮锁持有时间（用户侧表现为延迟，已用启动提示缓解）。子代理与终稿走的是不抢锁的直连调用，不会死锁。
- **终稿会额外消耗一次主模型调用**（携带本轮完整上下文），token 成本上升属预期。
- **终稿会被其它插件当成一次模型输出来记账**（因为分发了 `on_llm_response`）。若模型在终稿里又输出 `[FAV:+N]`，这一轮会计两次分。概率不高，别用「不分发」来回避——那会让标签泄露给用户，更糟。
- **多标签串行执行**，子代理慢会累加延迟（未做并行）。
- **`_build_handoff_toolset` 是私有 API**：升 AstrBot 后若它变了，会走回退装配（工具范围可能变大），看日志里的「复用内置全量工具装配失败」。
- **`run_context.messages` 是内部结构**：升 AstrBot 后必须回归历史格式（4.2）。
- **空 `completion_text` 会导致本轮历史不被保存**：所以失败兜底宁可写入 `error_notice`，也不要把它置空。
- **`_GUARD_KEY` / `EXTRA_PARTS_KEY` 必须集中在 `callout/runner.py` 定义**（`main.py` 导入使用），避免两边写字面量写岔。

---

## 9. 维护要求

**何时必须更新本文件**：目录/文件增删改名、Handler 增删或钩子变更、标签格式或清洗范围变化、
终稿生成方式（是否分发钩子）变化、配置项增删改名或默认值变化、日志方式变化、数据写入行为变化、
已知坑与验证基线变化。

**更新方式**：直接改对应小节，不要只在文末追加；顺手核对第 6 节速查表指向的文件仍然存在。
自检：文中每个路径都存在、命令可复制执行、第 5 节的配置清单与 `_conf_schema.json` 一致。
**只写与仓库内容有关的通用信息**：不要记录本机绝对路径、用户名、令牌等环境专属内容。
