from __future__ import annotations

import json
from datetime import datetime

import httpx

from app.config import Settings
from app.models import AgentDecision


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
        intent_hints: dict[str, bool],
    ) -> AgentDecision:
        if not self.settings.aitunnel_api_key:
            raise LLMError("AITUNNEL_API_KEY is not configured.")

        system_prompt = (
            "Ты работаешь как гибкий AI-агент внутри Пачки. "
            "Твоя задача: понять последний запрос пользователя в контексте обсуждения и вернуть только JSON.\n"
            "Приоритеты:\n"
            "1. Сначала смотри на последний запрос пользователя и самые свежие сообщения.\n"
            "2. existing_summary используй только как слабый фон. Не тащи старые темы в новый ответ, "
            "если пользователь явно переключился на другой вопрос.\n"
            "3. Если пользователь просит создать несколько напоминаний по списку задач выше, "
            "разбей список на несколько reminders.\n"
            "4. Если запрос про план или набор задач, выдели из контекста конкретные напоминания и создай reminders.\n"
            "5. Если данных для создания напоминаний не хватает, задай точный follow-up вопрос.\n"
            "6. Не выдумывай старые напоминания из summary, если их не просили снова.\n"
            "\n"
            "Доступные action:\n"
            "- create_reminders: создать одно или несколько напоминаний\n"
            "- ask_followup: задать уточняющий вопрос\n"
            "- noop: ничего не делать\n"
            "\n"
            "Правила для reminders:\n"
            "- reminders может содержать от 1 до 10 объектов.\n"
            "- title должен быть коротким и ясным.\n"
            "- details содержит полезный контекст, но без воды.\n"
            "- due_at возвращай в ISO-8601 с timezone offset, например 2026-03-23T14:00:00+03:00.\n"
            "- Если пользователь явно хочет без срока, можно вернуть due_at=null.\n"
            "- Если известна только дата без времени, верни all_day=true и due_at на конец дня.\n"
            "- priority может быть 1, 2 или 3.\n"
            "- assignee_hint можно заполнить именем человека из текста, если это полезно, даже если ты не знаешь user_id.\n"
            "\n"
            "Когда выбирать ask_followup:\n"
            "- пользователь просит создать несколько напоминаний, но неясно, нужен один общий срок или разные;\n"
            "- формулировка слишком расплывчатая и без неё будет мусорное напоминание.\n"
            "\n"
            "Верни строго объект этой формы:\n"
            "{"
            '"action":"create_reminders|ask_followup|noop",'
            '"reminders":['
            "{"
            '"title":"...",'
            '"details":"...",'
            '"due_at":"...",'
            '"all_day":false,'
            '"priority":1,'
            '"assignee_hint":"..."'
            "}"
            "],"
            '"updated_summary":"...",'
            '"reply_message":"..."'
            "}"
        )

        user_payload = {
            "now_local": now_local.isoformat(),
            "timezone": self.settings.agent_timezone,
            "request_text": request_text,
            "intent_hints": intent_hints,
            "existing_summary": existing_summary,
            "reminders_created_today": created_titles[-10:],
            "messages": rendered_messages,
        }

        response = self._post_chat_completion(system_prompt, json.dumps(user_payload, ensure_ascii=False))
        raw_content = response["choices"][0]["message"]["content"]
        decision = self._parse_json(raw_content)
        return AgentDecision.model_validate(decision)

    def _post_chat_completion(self, system_prompt: str, user_prompt: str) -> dict:
        headers = {
            "Authorization": f"Bearer {self.settings.aitunnel_api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.settings.aitunnel_model,
            "temperature": 0.2,
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
