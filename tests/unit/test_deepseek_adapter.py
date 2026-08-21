"""Unit tests for DeepSeekAdapter — visual mode, session lifecycle, and message formatting."""
from unittest.mock import MagicMock, patch
from src.infrastructure.ai.deepseek_adapter import DeepSeekAdapter
from src.infrastructure.ai_client import VisualMode, VisualPart


def test_deepseek_visual_mode_detection():
    """Verify visual_mode resolves to IMAGE for vision models and TEXT otherwise."""
    text_adapter = DeepSeekAdapter("test-key", default_model="deepseek-v4-flash")
    assert text_adapter.visual_mode == VisualMode.TEXT

    pro_adapter = DeepSeekAdapter("test-key", default_model="deepseek-v4-pro")
    assert pro_adapter.visual_mode == VisualMode.TEXT

    vision_adapter = DeepSeekAdapter("test-key", default_model="deepseek-v4-flash-vision-exp")
    assert vision_adapter.visual_mode == VisualMode.IMAGE


def test_deepseek_session_lifecycle_and_visual_injection():
    """Verify begin_session stores visual parts and generate_content injects them."""
    adapter = DeepSeekAdapter("test-key", default_model="deepseek-v4-flash-vision-exp")

    fake_image_bytes = b"\x89PNG\r\n\x1a\nfake_image_data"
    vp = VisualPart(mime_type="image/png", data=fake_image_bytes, label="[TEST_CHART]")

    # Before begin_session
    assert adapter._session_visual_parts is None

    # begin_session
    adapter.begin_session(
        system_instruction="System prompt",
        visual_parts=[vp],
        model="deepseek-v4-flash-vision-exp",
    )
    assert adapter._session_visual_parts == [vp]

    # Mock client.chat.completions.create
    mock_openai = MagicMock()
    mock_choice = MagicMock()
    mock_choice.message.content = "Analysis complete"
    mock_choice.message.tool_calls = None
    mock_response = MagicMock()
    mock_response.choices = [mock_choice]
    mock_response.usage = None
    mock_openai.chat.completions.create.return_value = mock_response

    with patch.object(adapter, "_get_client", return_value=mock_openai):
        res = adapter.generate_content(
            model="deepseek-v4-flash-vision-exp",
            contents=["Analyze market"],
            system_instruction="System prompt",
        )

        assert res.text == "Analysis complete"
        mock_openai.chat.completions.create.assert_called_once()
        call_kwargs = mock_openai.chat.completions.create.call_args[1]

        messages = call_kwargs["messages"]
        # messages should have system prompt, visual part message (with image_url), and user text
        assert messages[0]["role"] == "system"
        assert messages[0]["content"] == "System prompt"

        # Check visual part message
        assert messages[1]["role"] == "user"
        assert isinstance(messages[1]["content"], list)
        assert messages[1]["content"][0]["type"] == "text"
        assert messages[1]["content"][0]["text"] == "[TEST_CHART]"
        assert messages[1]["content"][1]["type"] == "image_url"
        assert messages[1]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")

        # Check user prompt
        assert messages[2]["role"] == "user"
        assert messages[2]["content"] == "Analyze market"

    # end_session
    adapter.end_session()
    assert adapter._session_visual_parts is None
