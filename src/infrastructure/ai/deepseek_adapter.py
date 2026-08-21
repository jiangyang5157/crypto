"""DeepSeekAdapter — talks to DeepSeek API via the OpenAI-compatible protocol."""
import logging
from typing import Any

from src.infrastructure.ai._openai_utils import (
    build_messages, convert_tools, parse_openai_response,
)
from src.infrastructure.ai_client import (
    AbstractAIClient, AIResponse, VisualMode, VisualPart,
)

logger = logging.getLogger(__name__)


class DeepSeekAdapter(AbstractAIClient):
    """Talks to DeepSeek API via the shared OpenAI-compatible protocol.

    Enables thinking mode when ``reasoning_effort`` is passed to
    ``generate_content()`` (disables temperature). Falls back to
    standard temperature-based generation when ``reasoning_effort`` is None.
    """

    def __init__(self, api_key: str, default_model: str = "deepseek-v4-flash",
                 base_url: str = "https://api.deepseek.com",
                 *, http_timeout: int = 240):
        self.api_key = api_key
        self.default_model = default_model
        self.base_url = base_url
        self.provider_label = "DeepSeekAdapter"
        self._http_timeout = http_timeout
        self._client = None
        self._session_visual_parts: list[VisualPart] | None = None

    def _get_client(self):
        if self._client is None:
            from openai import OpenAI
            self._client = OpenAI(api_key=self.api_key, base_url=self.base_url,
                                  timeout=self._http_timeout)
        return self._client

    @property
    def visual_mode(self) -> VisualMode:
        """Dynamic visual mode check based on default_model name."""
        if "vision" in (self.default_model or "").lower():
            return VisualMode.IMAGE
        return VisualMode.TEXT

    def begin_session(
        self,
        system_instruction: str | None = None,
        tools: list | None = None,
        visual_parts: list | None = None,
        model: str | None = None,
    ) -> None:
        """Store session-scoped visual assets for subsequent generation cycles."""
        self._session_visual_parts = visual_parts or []
        logger.info(
            "DeepSeekAdapter session begin | visual_parts=%d | model=%s",
            len(self._session_visual_parts), model,
        )

    def end_session(self) -> None:
        """Clear session-scoped visual assets."""
        self._session_visual_parts = None
        logger.info("DeepSeekAdapter session end | visual_parts cleared")

    def generate_content(
        self, model: str, contents: list[Any], *,
        system_instruction: str | None = None,
        tools: list[Any] | None = None,
        temperature: float = 0.5,
        reasoning_effort: str | None = None,
        response_json: bool = False,
        http_timeout: int | None = None,
    ) -> AIResponse:
        # model param is always the orchestrator's shared_model — use as-is
        target_model = model or self.default_model

        full_contents = list(contents)
        visual_count = 0
        if self.visual_mode == VisualMode.IMAGE and self._session_visual_parts:
            full_contents = list(self._session_visual_parts) + full_contents
            visual_count = len(self._session_visual_parts)

        messages = build_messages(system_instruction, full_contents,
                                  response_json=response_json,
                                  supports_vision=(self.visual_mode == VisualMode.IMAGE))
        openai_tools = convert_tools(tools) if tools else None

        api_params: dict[str, Any] = {
            "model": target_model,
            "messages": messages,
        }

        if reasoning_effort is not None:
            api_params["reasoning_effort"] = reasoning_effort
            api_params["extra_body"] = {"thinking": {"type": "enabled"}}
            # thinking mode disables temperature — omit it
        else:
            api_params["temperature"] = temperature

        if openai_tools:
            api_params["tools"] = openai_tools
            api_params["tool_choice"] = "auto"
        if response_json:
            api_params["response_format"] = {"type": "json_object"}
        if http_timeout:
            api_params["timeout"] = http_timeout

        if reasoning_effort is not None:
            logger.info("AI call | provider=%s | model=%s | images=%d | thinking=%s",
                        self.provider_label, target_model, visual_count, reasoning_effort)
        else:
            logger.info("AI call | provider=%s | model=%s | images=%d | temp=%.2f",
                        self.provider_label, target_model, visual_count, temperature)
        response = self._get_client().chat.completions.create(**api_params)
        return parse_openai_response(response, response_json)
