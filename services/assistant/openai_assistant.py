import json

import httpx
from pydantic import ValidationError

from core.schemas import AssistantTurn, LeadSession
from services.assistant.prompts import SYSTEM_PROMPT


class AssistantResponseError(Exception):
    """Ошибка получения или проверки ответа LLM."""


class OpenAIAssistant:
    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: str = "https://api.openai.com/v1",
        client: httpx.Client | None = None,
    ):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.client = client or httpx.Client(timeout=30.0)
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def generate(
        self,
        session: LeadSession,
        user_message: str,
    ) -> AssistantTurn:
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "system",
                "content": (
                    "Текущие данные заявки: "
                    + session.lead.model_dump_json()
                    + ". Это состояние приложения, а не новая "
                    "реплика клиента."
                ),
            },
        ]

        messages.extend(session.history[-8:])
        messages.append(
            {"role": "user", "content": user_message}
        )

        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
        }

        try:
            response = self.client.post(
                f"{self.base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            response.raise_for_status()

            data = response.json()
            content = data["choices"][0]["message"]["content"]

            if not isinstance(content, str):
                raise AssistantResponseError(
                    "LLM вернула не текстовый ответ"
                )

            return AssistantTurn.model_validate_json(content)

        except (
            httpx.HTTPError,
            ValueError,
            KeyError,
            IndexError,
            TypeError,
            ValidationError,
        ) as exc:
            raise AssistantResponseError(
                "Не удалось получить корректный ответ LLM"
            ) from exc
