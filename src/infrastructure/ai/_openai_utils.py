"""Shared utilities for OpenAI-compatible providers (e.g. DeepSeek)."""
import base64
import json
import logging
from typing import Any

from src.infrastructure.ai_client import (
    AIResponse, ToolCall, UsageMetadata, VisualPart, VisualMode,
)

logger = logging.getLogger(__name__)

JSON_HINT = (
    "IMPORTANT: You MUST respond ONLY with a valid JSON object. "
    "Do NOT include markdown blocks, preamble, or explanations."
)


def build_messages(
    system_instruction: str | None, contents: list[Any],
    *, response_json: bool = False, supports_vision: bool = False,
) -> list[dict]:
    json_instruction = f"\n\n{JSON_HINT}" if response_json else ""
    system_content = (
        f"{system_instruction}{json_instruction}"
        if system_instruction else json_instruction or None
    )
    if system_content:
        messages: list[dict] = [{"role": "system", "content": system_content}]
    else:
        messages: list[dict] = []
    for item in contents:
        if isinstance(item, str):
            messages.append({"role": "user", "content": item})
        elif isinstance(item, VisualPart):
            if supports_vision:
                b64 = base64.b64encode(item.data).decode("ascii")
                data_uri = f"data:{item.mime_type};base64,{b64}"
                content_parts: list[dict] = []
                if item.label:
                    content_parts.append({"type": "text", "text": item.label})
                content_parts.append({
                    "type": "image_url",
                    "image_url": {"url": data_uri},
                })
                messages.append({"role": "user", "content": content_parts})
                logger.info("injected VisualPart into OpenAI message | label=%s | size_bytes=%d", item.label, len(item.data))
            else:
                # provider doesn't support vision — skip VisualPart entirely
                # (all chart data is already in the observation JSON)
                logger.debug("skipping VisualPart — vision not supported")
        elif isinstance(item, dict):
            role = item.get("role", "user")
            if "text" in item:
                messages.append({"role": role, "content": item["text"]})
            elif "tool_responses" in item:
                for tr in item["tool_responses"]:
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tr["id"],
                        "content": json.dumps(tr.get("result", {})),
                    })
            elif "tool_calls" in item:
                tcs = [{
                    "id": tc["id"], "type": "function",
                    "function": {"name": tc["name"], "arguments": json.dumps(tc["args"])},
                } for tc in item["tool_calls"]]
                assistant_msg: dict = {"role": "assistant", "content": None, "tool_calls": tcs}
                # DeepSeek thinking models require reasoning_content echoed back
                if item.get("reasoning_content"):
                    assistant_msg["reasoning_content"] = item["reasoning_content"]
                messages.append(assistant_msg)
            else:
                logger.warning("skipping unrecognized content type | type=%s", type(item).__name__)
    return messages


def _convert_dict_tool(tool: dict) -> dict | None:
    """Convert a plain-dict tool declaration (from MathTools) to OpenAI format."""
    name = tool.get("name")
    if not name:
        return None
    desc = tool.get("description", "")
    raw_params = tool.get("parameters", {})
    props, required = {}, []
    for pn, ps in raw_params.get("properties", {}).items():
        prop = {
            "type": ps.get("type", "string").lower(),
            "description": ps.get("description", ""),
        }
        for key in ("enum", "minimum", "maximum"):
            if key in ps:
                prop[key] = ps[key]
        if "items" in ps:
            prop["items"] = {"type": ps["items"].get("type", "string").lower()}
        props[pn] = prop
    required = list(raw_params.get("required", []) or [])
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": desc,
            "parameters": {
                "type": "object",
                "properties": props,
                "required": required,
            },
        },
    }


def _convert_gemini_tool(tool: Any) -> list[dict]:
    """Convert a Gemini-format Tool object (with function_declarations) to OpenAI format."""
    result = []
    for fd in tool.function_declarations:
        props, required = {}, []
        if hasattr(fd, "parameters") and fd.parameters:
            for pn, ps in getattr(fd.parameters, "properties", {}).items():
                prop = {
                    "type": getattr(ps, "type", "string").lower(),
                    "description": getattr(ps, "description", ""),
                }
                for key in ("enum", "minimum", "maximum"):
                    if hasattr(ps, key):
                        prop[key] = getattr(ps, key)
                if hasattr(ps, "items") and ps.items:
                    prop["items"] = {"type": getattr(ps.items, "type", "string").lower()}
                props[pn] = prop
            required = list(getattr(fd.parameters, "required", []) or [])
        result.append({
            "type": "function",
            "function": {
                "name": fd.name, "description": fd.description or "",
                "parameters": {"type": "object", "properties": props, "required": required},
            },
        })
    return result


def convert_tools(tools: list[Any]) -> list[dict]:
    """Convert tool declarations to OpenAI function-calling format.

    Handles both Gemini Tool objects (with ``function_declarations``) and
    plain dicts (from ``MathTools.get_tool_declarations()``).
    """
    result = []
    for tool in tools:
        if hasattr(tool, "function_declarations"):
            result.extend(_convert_gemini_tool(tool))
        elif isinstance(tool, dict):
            converted = _convert_dict_tool(tool)
            if converted:
                result.append(converted)
    return result


def clean_json_text(raw_text: str) -> str:
    text = raw_text.strip()
    if text.startswith("```json"):
        text = text.split("```json", 1)[1].rsplit("```", 1)[0].strip()
    elif text.startswith("```"):
        text = text.split("```", 1)[1].rsplit("```", 1)[0].strip()
    return text


# ── OpenAI response parser ──────────────────────────────────────────────────


def parse_openai_response(response, is_json: bool) -> AIResponse:
    """Parse an OpenAI chat completions response into a provider-agnostic AIResponse."""
    msg = response.choices[0].message
    text = clean_json_text(msg.content or "") if is_json else (msg.content or "")
    tool_calls = None
    if msg.tool_calls:
        tool_calls = []
        for tc in msg.tool_calls:
            try:
                args = json.loads(tc.function.arguments) if tc.function.arguments else {}
            except json.JSONDecodeError:
                args = {}
            tool_calls.append(ToolCall(name=tc.function.name, args=args))
    usage = None
    if response.usage:
        usage = UsageMetadata(
            total_token_count=response.usage.total_tokens or 0,
            prompt_token_count=response.usage.prompt_tokens or 0,
            candidates_token_count=response.usage.completion_tokens or 0,
        )
    reasoning = getattr(msg, "reasoning_content", None) or None
    return AIResponse(text=text, tool_calls=tool_calls, usage=usage,
                      reasoning_content=reasoning)
