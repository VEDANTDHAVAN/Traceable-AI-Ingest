import pytest

from ai_ingest.llm.providers import (
    build_request,
    extract_response_text,
)


def test_groq_request_uses_json_mode():
    endpoint, payload = build_request(
        "groq",
        "fixture-model",
        "Synthetic source.",
    )

    assert endpoint == (
        "https://api.groq.com/openai/v1/chat/completions"
    )
    assert payload["response_format"] == {
        "type": "json_object"
    }


def test_deepseek_endpoint():
    endpoint, payload = build_request(
        "deepseek",
        "fixture-model",
        "Synthetic source.",
    )

    assert endpoint == (
        "https://api.deepseek.com/chat/completions"
    )
    assert payload["model"] == "fixture-model"


def test_gemini_request_uses_json_mime_type():
    endpoint, payload = build_request(
        "gemini",
        "fixture-model",
        "Synthetic source.",
    )

    assert endpoint.endswith(
        "/models/fixture-model:generateContent"
    )
    assert payload["generationConfig"]["responseMimeType"] == (
        "application/json"
    )


def test_gemini_text_extraction():
    payload = {
        "candidates": [
            {
                "finishReason": "STOP",
                "content": {
                    "parts": [{"text": '{"example": null}'}]
                },
            }
        ]
    }

    assert extract_response_text(
        "gemini",
        payload,
    ) == '{"example": null}'


def test_truncated_chat_output_rejected():
    payload = {
        "choices": [
            {
                "finish_reason": "length",
                "message": {"content": '{"partial":'},
            }
        ]
    }

    with pytest.raises(ValueError):
        extract_response_text("groq", payload)


def test_chat_text_extraction():
    payload = {
        "choices": [
            {
                "finish_reason": "stop",
                "message": {
                    "content": '{"example": null}'
                },
            }
        ]
    }

    assert extract_response_text(
        "deepseek",
        payload,
    ) == '{"example": null}'