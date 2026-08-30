# In-band Dialect 設計文件

> Looplane 的 in-band tool-calling dialect 系統設計，參考 OMP (Oh My Pi) 的成熟實作。

---

## 1. OMP 參考摘要

### 1.1 端到端運作流程

OMP 的 dialect 系統解決一個核心問題：**不是所有模型都支援原生 function calling**。當模型不支援（或使用者強制指定）時，OMP 用「owned dialect」將工具呼叫編碼為 XML 文字嵌入 system prompt，模型以文字輸出工具呼叫，再由 scanner 即時解析回結構化的 `ToolCall` 物件。

完整資料流：

```
┌─────────────────────────────────────────────────────────────────┐
│ 1. resolveDialect(format, model)                                │
│    ├─ format="native" → None (用原生 tool calling)              │
│    ├─ format="auto" → model.supportsTools? None : preferredDialect(id) │
│    └─ format="glm"|"xml"|... → 該 dialect                      │
│                                                                 │
│ 2. 若 dialect ≠ None：                                          │
│    a. renderInbandToolPrompt(tools, dialect)                    │
│       → 產生工具目錄文字，附加到 system prompt                  │
│    b. encodeInbandToolHistory(messages, dialect, tools)          │
│       → 將歷史中的 ToolCall/ToolResult 重新編碼為 XML 文字      │
│    c. 送出請求時 tools=undefined (不傳原生工具定義)              │
│    d. tool_choice=undefined (無法指定)                           │
│                                                                 │
│ 3. 模型回覆純文字                                               │
│                                                                 │
│ 4. wrapInbandToolStream(stream, tools, dialect)                 │
│    ├─ InbandStreamProjector 吃文字 delta                        │
│    ├─ scanner.feed(delta) → InbandScanEvent[]                   │
│    └─ 投射回 toolcall_start/delta/end 事件                      │
│                                                                 │
│ 5. 下游 agent loop 收到的 AssistantMessage 與原生 tool calling   │
│    完全一致 — stopReason 也會被改寫為 "toolUse"                  │
└─────────────────────────────────────────────────────────────────┘
```

### 1.2 XML 格式範例

**System prompt 注入的工具目錄** (`prompt-template.md` + `catalog.ts`)：

```markdown
# Tools

You may call one or more functions to assist with the user query.
Tool calls are emitted as text using the exact syntax below, not as native provider tool messages.

Available functions are listed inside `<tools></tools>` as one JSON object per line:

<tools>
{"type":"function","function":{"name":"read_file","description":"Read a file","parameters":{"type":"object","properties":{"path":{"type":"string"}}}}}
{"type":"function","function":{"name":"write_file","description":"Write a file","parameters":{"type":"object","properties":{"path":{"type":"string"},"content":{"type":"string"}}}}}
</tools>

## Format guide

A call is one `<invoke>` element whose `<parameter>` children carry its arguments:

<invoke name="fn"><parameter name="arg">value</parameter></invoke>

Emit consecutive `<invoke>…</invoke>` blocks for multiple calls.
```

**模型輸出範例** (xml dialect)：

```xml
Let me read that file for you.

<invoke name="read_file"><parameter name="path">src/main.py</parameter></invoke>
```

**Anthropic dialect** — 多了 `<function_calls>` 外層：

```xml
<function_calls>
<invoke name="read_file"><parameter name="path">src/main.py</parameter></invoke>
</function_calls>
```

**MiniMax dialect** — `<minimax:tool_call>` 外層 + namespaced tag：

```xml
<minimax:tool_call>
<invoke name="read_file"><parameter name="path">src/main.py</parameter></invoke>
</minimax:tool_call>
```

**Tool result 回傳** (anthropic/minimax 格式)：

```xml
<function_results>
<result>
<tool_name>read_file</tool_name>
<stdout>print("hello world")</stdout>
</result>
</function_results>
```

**XML (簡化) 格式的 tool result**：

```xml
<tool_response>
print("hello world")
</tool_response>
```

### 1.3 Scanner 解析機制

核心是 `AnthropicInbandScanner` — 一個**增量式狀態機**，可處理 streaming token delta：

```
States: outside → section → invoke → parameter → thinking
         │                    │         │
         └── <invoke>直接 ────┘         │
         └── <function_calls> → section ─┘ <invoke> → invoke → <parameter> → parameter
```

**關鍵設計**：

1. **增量 feed/flush**：每收到一個 text delta 就 `feed(text)`，返回 `InbandScanEvent[]`。結束時 `flush()` 清空殘留 buffer。

2. **Partial tag 暫存**：當 buffer 以 `<` 開頭但還不完整（`>` 尚未到達），且 buffer 是已知 tag prefix 的前綴，就返回 `"partial"` 暫不消費，等更多 token 到達。最大暫存 256 bytes。

3. **Tag 辨識**：regex `^<\s*(\/?)\s*(?:(?<prefix>[A-Za-z_][\w.-]*):)?(?<localName>[A-Za-z_][\w.-]*)(?<attrs>[^>]*)>$` 解析開/閉標籤、namespace prefix、屬性。

4. **Parameter 值讀取**：是 **delimiter matching**，不是真的 XML parsing。值的結束靠遇到 `</parameter>` 閉標籤，中間的 `<`/`>` 都被當成文字。這個設計讓模型輸出程式碼時不需要 HTML escape。

5. **型別推斷**：
   - 若 tool schema 宣告該參數為 `string` 型別 → 原樣保留文字
   - 否則嘗試 `parseJsonWithRepair(trimmed)` 將值解析為 JSON
   - 若 JSON 解析失敗 → fallback 為字串
   - `<parameter name="x" string="false">` 可強制 JSON 解析

6. **事件型別**：
   ```typescript
   type InbandScanEvent =
     | { type: "text"; text: string }           // 模型的普通文字輸出
     | { type: "thinkingStart" }                // <thinking> 開始
     | { type: "thinkingDelta"; delta: string } // thinking 內容增量
     | { type: "thinkingEnd"; thinking: string }// thinking 結束
     | { type: "toolStart"; id: string; name: string }       // <invoke> 開始
     | { type: "toolArgDelta"; id: string; name: string; key: string; delta: string } // 參數增量
     | { type: "toolEnd"; id: string; name: string; arguments: Record<string, unknown> } // </invoke> 結束
   ```

7. **Fabrication detection**：`InbandStreamProjector` 偵測模型是否開始自己偽造 `<tool_response>` / `<function_results>`。一旦發現 response open token，立即 `#stopped = true`，可選擇 abort 整個 stream。

### 1.4 Dialect 選擇機制

**型別定義** (`catalog/identity/dialect.ts`)：

```typescript
type Dialect = "glm" | "hermes" | "kimi" | "xml" | "anthropic"
             | "deepseek" | "minimax" | "harmony" | "qwen3"
             | "gemini" | "gemma";

const FALLBACK_DIALECT: Dialect = "xml";
```

**選擇流程** (`resolveDialect` in `sdk.ts`)：

```typescript
function resolveDialect(format, model):
  if format === "native"  → undefined (原生 tool calling)
  if format === "auto":
    if model.supportsTools !== false → undefined
    if !model.id → "glm"
    preferred = preferredDialect(model.id)
    return preferred === FALLBACK_DIALECT ? "glm" : preferred
  return format  // 使用者明確指定的 dialect
```

**Model → Dialect 映射** (`preferredDialect`)：

```typescript
switch (modelFamilyToken(modelId)):
  "anthropic" → "anthropic"
  "glm"       → "glm"
  "gemini"    → "gemini"
  "gemma"     → "gemma"
  "kimi"      → "kimi"
  "qwen"      → "qwen3"
  "deepseek"  → "deepseek"
  "minimax"   → "minimax"
  "openai"    → "harmony"
  "gpt-oss"   → "harmony"
  default     → "xml" (fallback)
```

`modelFamilyToken()` 用 regex 和子字串匹配判斷：`claude-*` → anthropic、包含 `qwen` → qwen、包含 `deepseek` → deepseek、`gemma-N` → gemma 等。

### 1.5 架構特點

OMP 的 dialect 架構有幾個值得注意的設計：

1. **每個 dialect 是一個 DialectDefinition 物件**，包含：
   - `prompt`: markdown 格式說明（教模型怎麼寫 XML）
   - `createScanner()`: 建立 streaming parser
   - `renderToolCall()` / `renderAssistantToolCalls()`: 將 ToolCall 渲染為 XML 文字
   - `renderToolResults()`: 將工具結果渲染為 XML 文字
   - `renderThinking()` / `renderTranscript()`: 用於歷史重編碼

2. **Scanner 可組合**：`XmlInbandScanner` 和 `MinimaxInbandScanner` 都是 wrap `AnthropicInbandScanner` 的薄層，靠修改 config（wrapper tags, tag prefixes）來改變行為。

3. **歷史重編碼**：`encodeInbandToolHistory()` 將歷史中的結構化 ToolCall → XML 文字（assistant message），ToolResult → XML 文字（user message），確保模型看到的對話格式一致。

4. **InbandStreamProjector 是雙通道**：同時處理 in-band XML 和 provider-native tool call，以 `#toolChannel` 鎖定第一個真實 call 的來源，防止 double-dispatch。

---

## 2. Looplane 適配設計

### 2.1 設計原則

1. **最小可行**：Looplane 是非 streaming 的 Python 系統，不需要 streaming scanner 的複雜度。先做 **full-text parsing**，未來 streaming 時再加增量解析。
2. **單一 XML 格式**：不像 OMP 維護 11 種 dialect，Looplane 先只實作一種通用 XML 格式（基於 OMP 的 `xml` dialect，最簡單也最通用）。
3. **透明包裝**：dialect 在 `ModelProvider.complete()` 層透明運作，`loop.py` 不需修改。
4. **向後相容**：新增 `dialect.py` 模組，只在偵測到需要時啟用。

### 2.2 新檔案：`src/looplane/dialect.py`

```python
"""In-band XML tool-calling dialect for models without native function calling."""

from __future__ import annotations

import json
import re
import uuid
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence
    from looplane.contracts import ToolCall, ToolDefinition


# ── Protocol ────────────────────────────────────────────────────

class Dialect(ABC):
    """Base class for in-band tool-calling dialects."""

    @abstractmethod
    def tool_instructions(self, tools: Sequence[ToolDefinition]) -> str:
        """Generate XML format instructions + tool catalog for system prompt injection."""
        ...

    @abstractmethod
    def parse_tool_calls(self, text: str) -> list[ToolCall]:
        """Parse model text output and extract in-band tool calls."""
        ...

    @abstractmethod
    def render_tool_call(self, call: ToolCall) -> str:
        """Render a single tool call as XML text (for history re-encoding)."""
        ...

    @abstractmethod
    def render_tool_result(
        self, *, name: str, content: str, is_error: bool = False,
    ) -> str:
        """Render a tool result as XML text (for history re-encoding)."""
        ...

    def should_use_native_tools(self) -> bool:
        """In-band dialects always return False."""
        return False

    @property
    @abstractmethod
    def response_open_tokens(self) -> tuple[str, ...]:
        """Tokens that signal the model is fabricating its own tool result.

        Text at or after these tokens is discarded from the model output.
        """
        ...


# ── XML Dialect ─────────────────────────────────────────────────

_TOOL_CATALOG_TEMPLATE = """\
# Tools

You may call one or more functions to assist with the user query.
Tool calls are emitted as text using the exact syntax below, not as native provider tool messages.

Available functions are listed inside `<tools></tools>` as one JSON object per line:

<tools>
{tools}
</tools>

## Format guide

A call is one `<invoke>` element whose `<parameter>` children carry its arguments:

```text
<invoke name="fn"><parameter name="arg">value</parameter></invoke>
```

Emit consecutive `<invoke>…</invoke>` blocks for multiple calls; you MAY wrap them in `<tool_calls>…</tool_calls>`. Each call's result arrives as a response block:

```text
<tool_response>
verbatim tool result
</tool_response>
```

## Rules

- `name` MUST match a listed function.
- Parameter values are read literally by regex (delimiter matching), NOT a real XML parser: write them verbatim and never HTML-escape (emit `a & b`, never `a &amp; b`; `<`/`>` stay literal too). Only the body's own `</parameter>` closing tag is reserved. Non-string values are JSON.
- Read each `<tool_response>` in call order. NEVER emit `<tool_response>` yourself.
- Emit the stop sequence ONLY after the call is fully written — NEVER announce a tool then stop. Write the complete call, THEN the stop sequence, THEN halt."""


# Regex to match <invoke name="...">...</invoke> blocks.
# Uses re.DOTALL so `.` matches newlines within parameter values.
_INVOKE_RE = re.compile(
    r'<invoke\s+name\s*=\s*"([^"]+)"\s*>(.*?)</invoke>',
    re.DOTALL,
)
# Regex to extract <parameter name="...">...</parameter> within an invoke body.
_PARAM_RE = re.compile(
    r'<parameter\s+name\s*=\s*"([^"]+)"(?:\s+[^>]*)?>(.*?)</parameter>',
    re.DOTALL,
)
# Tokens that mark the start of a fabricated tool result.
_RESPONSE_OPEN_TOKENS = ("<tool_response>", "<tool_response ")
# Pattern to detect fabricated result blocks in model output.
_RESPONSE_BLOCK_RE = re.compile(
    r"<tool_response[\s>].*?</tool_response>",
    re.DOTALL,
)


def _coerce_param_value(
    raw: str,
    *,
    schema: dict[str, object] | None = None,
) -> object:
    """Coerce a raw parameter string to its appropriate Python type.

    String-typed parameters (per schema) are returned as-is.
    Everything else attempts JSON parse; falls back to raw string on failure.
    """
    if schema is not None:
        schema_type = schema.get("type")
        if schema_type == "string":
            return raw
    trimmed = raw.strip()
    if not trimmed:
        return raw
    try:
        return json.loads(trimmed)
    except (json.JSONDecodeError, ValueError):
        return raw


def _param_schema(
    tool: ToolDefinition, param_name: str,
) -> dict[str, object] | None:
    """Extract the JSON Schema for a single parameter from a tool definition."""
    params = tool.parameters
    if not isinstance(params, dict):
        return None
    props = params.get("properties")
    if not isinstance(props, dict):
        return None
    prop = props.get(param_name)
    return prop if isinstance(prop, dict) else None


class XmlDialect(Dialect):
    """Generic XML in-band tool-calling dialect.

    Based on OMP's ``xml`` dialect — the simplest and most universally
    compatible format.  Uses ``<invoke>``/``<parameter>`` tags without
    any wrapper element, making it the safest choice for unknown models.
    """

    def tool_instructions(self, tools: Sequence[ToolDefinition]) -> str:
        catalog_lines: list[str] = []
        for tool in tools:
            entry = {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description or "",
                    "parameters": tool.parameters,
                },
            }
            catalog_lines.append(json.dumps(entry, ensure_ascii=False))
        return _TOOL_CATALOG_TEMPLATE.format(tools="\n".join(catalog_lines))

    def parse_tool_calls(
        self,
        text: str,
        tools: Sequence[ToolDefinition] = (),
    ) -> list[ToolCall]:
        from looplane.contracts import ToolCall as TC

        # Strip any fabricated tool-response blocks the model may have hallucinated.
        clean = _strip_fabricated_responses(text)

        tool_map = {t.name: t for t in tools}
        calls: list[TC] = []
        for invoke_match in _INVOKE_RE.finditer(clean):
            name = invoke_match.group(1).strip()
            body = invoke_match.group(2)
            arguments: dict[str, object] = {}
            tool_def = tool_map.get(name)
            for param_match in _PARAM_RE.finditer(body):
                param_name = param_match.group(1).strip()
                raw_value = param_match.group(2)
                schema = _param_schema(tool_def, param_name) if tool_def else None
                arguments[param_name] = _coerce_param_value(raw_value, schema=schema)
            calls.append(
                TC(
                    tool_call_id=f"inband_{uuid.uuid4().hex[:12]}",
                    name=name,
                    arguments=arguments,
                )
            )
        return calls

    def render_tool_call(self, call: ToolCall) -> str:
        parts: list[str] = [f'<invoke name="{_escape_xml_attr(call.name)}">']
        for key, value in call.arguments.items():
            rendered = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
            parts.append(f'<parameter name="{_escape_xml_attr(key)}">{rendered}</parameter>')
        parts.append("</invoke>")
        return "".join(parts)

    def render_tool_result(
        self, *, name: str, content: str, is_error: bool = False,
    ) -> str:
        return f"<tool_response>\n{content}\n</tool_response>"

    @property
    def response_open_tokens(self) -> tuple[str, ...]:
        return _RESPONSE_OPEN_TOKENS


def _strip_fabricated_responses(text: str) -> str:
    """Remove any <tool_response>...</tool_response> blocks the model fabricated.

    Models occasionally hallucinate tool results after their tool calls.
    We strip these so they don't interfere with invoke parsing, and so the
    harness executes the tool itself.
    """
    # Find the first response open token; everything from there onwards is suspect.
    for token in _RESPONSE_OPEN_TOKENS:
        idx = text.find(token)
        if idx != -1:
            return text[:idx]
    return text


def _escape_xml_attr(value: str) -> str:
    return value.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")


# ── Dialect Resolution ──────────────────────────────────────────

# Model name patterns that are known not to support native tool calling
# and should default to in-band dialect.
_INBAND_MODEL_PATTERNS: tuple[tuple[str, ...], ...] = (
    # OpenRouter free tier models
    (":free",),
    # MiniMax models (unreliable native tool calling)
    ("minimax",),
    # Some local/small models
    ("gguf", "ggml"),
)

# Models that explicitly DO support native tools — takes priority over the
# pattern match above when ambiguous.
_NATIVE_TOOL_MODELS: tuple[str, ...] = (
    "gpt-4",
    "gpt-3.5",
    "claude-",
    "gemini-",
)


def resolve_dialect(
    model_name: str,
    *,
    supports_tool_calling: bool | None = None,
    force_dialect: str | None = None,
) -> Dialect | None:
    """Determine whether a model needs an in-band dialect.

    Returns:
        An ``XmlDialect`` instance if in-band tool calling should be used,
        or ``None`` if native tool calling is available.

    Resolution order:
        1. ``force_dialect="xml"`` → always use XmlDialect
        2. ``force_dialect="native"`` → always use native (return None)
        3. ``supports_tool_calling=False`` → use XmlDialect
        4. ``supports_tool_calling=True`` → use native
        5. Model name heuristic → check patterns
    """
    if force_dialect == "native":
        return None
    if force_dialect is not None:
        # Currently only "xml" is supported; future: "anthropic", "minimax", etc.
        return XmlDialect()
    if supports_tool_calling is False:
        return XmlDialect()
    if supports_tool_calling is True:
        return None
    # Heuristic: check model name patterns
    lower = model_name.lower()
    # Check native-capable models first
    for pattern in _NATIVE_TOOL_MODELS:
        if pattern in lower:
            return None
    # Check models known to need in-band
    for pattern_group in _INBAND_MODEL_PATTERNS:
        if any(p in lower for p in pattern_group):
            return XmlDialect()
    # Default: assume native tool calling is available
    return None


# ── History Re-encoding ─────────────────────────────────────────

def encode_inband_history(
    messages: Sequence[ConversationItem],
    dialect: Dialect,
    tools: Sequence[ToolDefinition] = (),
) -> list[ConversationItem]:
    """Re-encode structured ToolCall/ToolObservation items as plain text.

    When using an in-band dialect, the model must see tool calls and results
    as XML text in the conversation history, not as separate structured items.
    This function converts:
      - Assistant messages with tool_calls → assistant messages with XML text
      - ToolObservation items → user messages with XML result text
    """
    from looplane.contracts import Message, ToolObservation

    result: list[ConversationItem] = []
    for item in messages:
        if isinstance(item, Message) and item.role == "assistant" and item.tool_calls:
            # Re-encode assistant tool calls as XML text
            xml_parts: list[str] = []
            if item.content:
                xml_parts.append(item.content)
            for call in item.tool_calls:
                xml_parts.append(dialect.render_tool_call(call))
            result.append(
                Message(
                    role="assistant",
                    content="\n".join(xml_parts),
                    tool_calls=(),  # Clear structured calls
                )
            )
        elif isinstance(item, ToolObservation):
            # Re-encode tool results as user-role XML text
            result.append(
                Message(
                    role="user",
                    content=dialect.render_tool_result(
                        name=item.tool_name,
                        content=_observation_content_text(item),
                        is_error=not item.ok,
                    ),
                )
            )
        else:
            result.append(item)
    return result


def _observation_content_text(obs: ToolObservation) -> str:
    if obs.ok:
        return obs.content
    return f"Error: {obs.content}"
```

### 2.3 整合點

#### 2.3.1 `models.py:OpenAICompatibleModel.complete()` 的修改

```python
class OpenAICompatibleModel:
    def __init__(self, ..., dialect: Dialect | None = None):
        self._dialect = dialect
        ...

    async def complete(self, messages, tools=()):
        dialect = self._dialect

        if dialect and tools:
            # 1. 將工具定義注入 system prompt
            tool_instructions = dialect.tool_instructions(tools)

            # 2. 重新編碼歷史中的 ToolCall/ToolObservation 為 XML 文字
            messages = encode_inband_history(messages, dialect, tools)

            # 3. 在第一個 system message 後附加工具指令
            messages = _inject_tool_instructions(messages, tool_instructions)

            # 4. 不傳原生工具定義
            effective_tools = ()
        else:
            effective_tools = tools

        # ... 正常發送請求 ...

        if dialect and original_tools:
            # 5. 從回覆文字中解析 tool calls
            parsed_calls = dialect.parse_tool_calls(
                message.content or "", tools=original_tools,
            )
            # 6. 清除文字中的 XML tool call 殘留
            clean_content = _strip_tool_call_xml(message.content or "")
            return ModelTurn(
                content=clean_content or None,
                tool_calls=tuple(parsed_calls),
                usage=usage,
                finish_reason="tool_use" if parsed_calls else finish_reason,
            )
```

#### 2.3.2 `loop.py` — **不需修改**

因為 dialect 在 provider 層完全透明：
- `loop.py` 送出 `tools` 和 `messages`
- Provider 內部判斷是否需要 in-band
- 返回的 `ModelTurn` 格式完全一致（有 `tool_calls` → loop 執行工具）

#### 2.3.3 CLI 設定

在 `cli.py` 加入 `--dialect` 選項：

```python
@click.option(
    "--dialect",
    type=click.Choice(["auto", "native", "xml"]),
    default="auto",
    help="Tool calling mode: auto, native, or xml (in-band).",
)
```

`auto` 模式下，用 `resolve_dialect(model_name, supports_tool_calling=...)` 自動判斷。

---

## 3. XML 格式規格

### 3.1 工具呼叫格式

Looplane 使用的 XML 格式（與 OMP xml dialect 一致）：

```xml
<invoke name="tool_name"><parameter name="arg_name">arg_value</parameter></invoke>
```

**多參數**：

```xml
<invoke name="write_file"><parameter name="path">src/main.py</parameter><parameter name="content">print("hello")</parameter></invoke>
```

**多工具呼叫**（連續 invoke 或 `<tool_calls>` 包裹，兩者都能解析）：

```xml
<invoke name="read_file"><parameter name="path">a.py</parameter></invoke>
<invoke name="read_file"><parameter name="path">b.py</parameter></invoke>
```

### 3.2 工具結果格式

```xml
<tool_response>
verbatim tool result text
</tool_response>
```

錯誤結果：

```xml
<tool_response>
Error: file not found: /nonexistent
</tool_response>
```

### 3.3 參數值規則

| 情境 | 處理方式 |
|------|----------|
| Schema 為 `string` | 原樣保留（不 JSON decode） |
| 值為合法 JSON | `json.loads()` |
| 值非合法 JSON | 保留原始字串 |
| 空值 | 保留空字串 |

**重要**：參數值使用 **delimiter matching** 而非 XML parsing。值中間的 `<`、`>`、`&` 都是 literal text，不需要 HTML escape。唯一保留的終結符是 `</parameter>`。

### 3.4 Fabrication 偵測

如果模型在 tool call 之後自行偽造結果（hallucinate `<tool_response>` 區塊），parser 會：
1. 偵測到 `<tool_response>` open token
2. 截斷 — 只保留該 token 之前的文字
3. 正常解析 `<invoke>` 區塊

---

## 4. 實作計畫

### Phase 1：核心 dialect 模組 [低複雜度]

**新增** `src/looplane/dialect.py`：

- [ ] `Dialect` ABC
- [ ] `XmlDialect` 完整實作
  - `tool_instructions()` — 產生 system prompt 文字
  - `parse_tool_calls()` — regex 解析 invoke/parameter
  - `render_tool_call()` — ToolCall → XML 文字
  - `render_tool_result()` — 結果 → XML 文字
- [ ] `resolve_dialect()` — 模型名稱 → dialect 判斷
- [ ] `encode_inband_history()` — 歷史重編碼
- [ ] 單元測試：`tests/test_dialect.py`
  - 基本 parse/render round-trip
  - 多參數、多工具呼叫
  - 參數值型別推斷
  - fabrication 偵測與截斷
  - 空白/邊界情況

### Phase 2：Provider 整合 [中等複雜度]

**修改** `src/looplane/models.py`：

- [ ] `OpenAICompatibleModel.__init__` 接受 `dialect` 參數
- [ ] `OpenAICompatibleModel.complete()` 加入 dialect 分支
  - 注入工具指令到 system prompt
  - 重編碼歷史
  - 送出請求時 tools=() 
  - 解析回覆中的 tool calls
- [ ] 同步修改 `WorkersAIModel`（同為 OpenAI 相容格式）

### Phase 3：CLI 與自動偵測 [低複雜度]

**修改** `src/looplane/cli.py`：

- [ ] 新增 `--dialect` CLI 選項
- [ ] 在 model 建構時注入 dialect

**修改** `src/looplane/model_catalog.py`（如有需要）：

- [ ] 在 model catalog 中標記 `supports_tool_calling` 屬性

### Phase 4：進階功能 [未來]

- [ ] **Streaming scanner**：當 Looplane 支援 streaming 時，實作增量式狀態機（參考 `AnthropicInbandScanner`）
- [ ] **更多 dialect**：Anthropic（`<function_calls>` 包裹）、Hermes（`<tool_call>` JSON 格式）等
- [ ] **Thinking parsing**：解析 `<thinking>` 區塊
- [ ] **Schema-aware coercion**：更精確的 array/object 型別推斷

### 複雜度估算

| Phase | 新/改檔案 | 估計行數 | 風險 |
|-------|-----------|----------|------|
| 1 | dialect.py + test | ~350 + ~250 | 低：純邏輯，無副作用 |
| 2 | models.py | ~60 行修改 | 中：碰觸核心路徑 |
| 3 | cli.py | ~15 行修改 | 低：純配置傳遞 |
| 4 | dialect.py 擴展 | ~500+ | 高：streaming 狀態機 |

**建議**：Phase 1–3 可以一次 PR 完成，Phase 4 按需迭代。

---

## 附錄：OMP Dialect 定義一覽

| Dialect | Wrapper Tag | Scanner 基礎 | 特色 |
|---------|-------------|-------------|------|
| xml | 無（或 `<tool_calls>`） | AnthropicInbandScanner | 最簡單通用 |
| anthropic | `<function_calls>` | AnthropicInbandScanner | Claude 原生格式 |
| minimax | `<minimax:tool_call>` | AnthropicInbandScanner (自訂 config) | namespace prefix |
| glm | 自定義 | 獨立 scanner | GLM-4 系列專用 |
| hermes | `<tool_call>` | 獨立 scanner | JSON body（非 XML 參數） |
| kimi | 自定義 | 獨立 scanner | Moonshot/Kimi 格式 |
| deepseek | DSML tags | DeepSeekInbandScanner | pipe-wrapped 變體 |
| harmony | `<\|plugin\|>` | ChatML-based | OpenAI gpt-oss 格式 |
| qwen3 | `<tool_call>` | 獨立 scanner | Qwen 3.x 格式 |
| gemini | `<function_call>` | 獨立 scanner | Gemini 格式 |
| gemma | `<tool_call>` | 獨立 scanner | Gemma 開源模型 |

所有非 GLM/Hermes/Kimi/DeepSeek 的 dialect 都基於 `AnthropicInbandScanner`，靠修改 `wrapperTags` 和 `tagPrefixes` 來區分格式。
