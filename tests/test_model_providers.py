from unittest import mock

from app.model_providers import get_model_client


def test_flash_lite_uses_maximum_reasoning_effort():
    with (
        mock.patch("app.model_providers.require_gemini_api_key", return_value="test-key"),
        mock.patch("app.model_providers.OpenAIChatCompletionClient") as client,
    ):
        get_model_client("gemini::cloud::gemini-2.5-flash-lite")

    assert client.call_args.kwargs["model"] == "gemini-2.5-flash-lite"
    assert client.call_args.kwargs["reasoning_effort"] == "high"
