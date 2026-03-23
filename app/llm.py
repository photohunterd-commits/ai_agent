from __future__ import annotations

import json
from datetime import datetime

import httpx

from app.config import Settings
from app.models import TaskDecision


class LLMError(RuntimeError):
    """Raised when the LLM response cannot be parsed."""


class AITunnelClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def analyze(
        self,
        *,
        request_text: str,
        rendered_messages: str,
        existing_summary: str,
        created_titles: list[str],
        now_local: datetime,
    ) -> TaskDecision:
        if not self.settings.aitunnel_api_key:
            raise LLMError("AITUNNEL_API_KEY is not configured.")

        system_prompt = (
            "Ты помогаешь агенту в Пачке. Твоя задача: по сообщению пользователя и контексту "
            "решить, нужно ли создавать задачу в Пачке, и вернуть только JSON без markdown.\n"
            "Правила:\n"
            "1. Допустимые action: create_task, ask_deadline, noop.\n"
            "2. Если пользователь явно просит создать задачу и срок понятен, верни create_task.\n"
            "3. Если задача нужна, но срок не указан или двусмысленен, верни ask_deadline.\n"
            "4. Если запрос не про создание задачи или это явный дубль уже созданной сегодня задачи, верни noop.\n"
            "5. Поле due_at всегда верни в ISO-8601 c timezone offset, например 2026-03-23T14:00:00+03:00.\n"
            "6. Если известна только дата без времени, выставь all_day=true и due_at на 23:59:59 локального дня.\n"
            "7. priority может быть 1, 2 или 3.\n"
            "8. updated_summary должен быть коротким фактическим summary для следующих вызовов, до 500 символов.\n"
            "9. reply_message напиши по-русски коротко и по делу.\n"
            "10. Верни объект строго этой формы:\n"
            '{'
            '"action":"create_task|ask_deadline|noop",'
            '"title":"...",'
            '"details":"...",'
            '"due_at":"...",'
            '"all_day":false,'
            '"priority":1,'
            '"updated_summary":"...",'
            '"reply_message":"..."'
            '}'
        )

        user_payload = {
            "now_local": now_local.isoformat(),
            "timezone": self.settings.agent_timezone,
            "request_text": request_text,
            "existing_summary": existing_summary,
            "tasks_created_today": created_titles,
            "messages": rendered_messages,
        }

        response = self._post_chat_completion(system_prompt, json.dumps(user_payload, ensure_ascii=False))
        raw_content = response["choices"][0]["message"]["content"]
        decision = self._parse_json(raw_content)
        return TaskDecision.model_validate(decision)

    def _post_chat_completion(self, system_prompt: str, user_prompt: str) -> dict:
        headers = {
            "Authorization": f"Bearer {self.settings.aitunnel_api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.settings.aitunnel_model,
            "temperature": 0.1,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }

        with httpx.Client(timeout=60.0) as client:
            response = client.post(
                f"{self.settings.aitunnel_base_url}/chat/completions",
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
            return response.json()

    @staticmethod
    def _parse_json(content: str) -> dict:
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            start = content.find("{")
            end = content.rfind("}")
            if start == -1 or end == -1:
                raise LLMError(f"LLM did not return JSON: {content}")
            try:
                return json.loads(content[start : end + 1])
            except json.JSONDecodeError as exc:
                raise LLMError(f"LLM JSON parse error: {content}") from exc

