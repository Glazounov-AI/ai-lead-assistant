
import json

import httpx
import pytest

from core.schemas import LeadSession
from services.assistant.openai_assistant import (
    AssistantResponseError,
    OpenAIAssistant,
)


def test_valid_llm_response():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/chat/completions"

        payload = json.loads(request.content)
        assert payload["model"] == "test-model"
        assert payload["response_format"] == {
            "type": "json_object"
        }

        content = {
            "reply": "Как с вами связаться?",
            "extracted_lead": {
                "name": "Алексей",
                "business_task": "Нужен FAQ-бот",
            },
            "ready_to_submit": False,
        }

        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                content,
                                ensure_ascii=False,
                            )
                        }
                    }
                ]
            },
        )

    client = httpx.Client(
        transport=httpx.MockTransport(handler)
    )

    assistant = OpenAIAssistant(
        api_key="test-key",
        model="test-model",
        client=client,
    )

    session = LeadSession(user_id=1, chat_id=1)
    result = assistant.generate(
        session,
        "Меня зовут Алексей, нужен FAQ-бот.",
    )

    assert result.extracted_lead.name == "Алексей"
    assert result.ready_to_submit is False

    client.close()


def test_invalid_llm_response():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": "Это не JSON"
                        }
                    }
                ]
            },
        )

    client = httpx.Client(
        transport=httpx.MockTransport(handler)
    )

    assistant = OpenAIAssistant(
        api_key="test-key",
        model="test-model",
        client=client,
    )

    with pytest.raises(AssistantResponseError):
        assistant.generate(
            LeadSession(user_id=1, chat_id=1),
            "Здравствуйте",
        )

    client.close()
