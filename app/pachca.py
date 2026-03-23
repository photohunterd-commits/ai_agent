from __future__ import annotations

from typing import Any

import httpx

from app.config import Settings
from app.models import ChatMember, ChatMessage


class PachcaClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.base_url = "https://api.pachca.com/api/shared/v1"

    def list_messages(self, chat_id: int, *, cursor: str | None = None, limit: int = 50) -> tuple[list[ChatMessage], str | None]:
        params = {"chat_id": chat_id, "sort": "desc", "limit": limit}
        if cursor:
            params["cursor"] = cursor
        payload = self._request("GET", "/messages", params=params)
        messages = [ChatMessage.model_validate(item) for item in payload.get("data", [])]
        next_page = payload.get("meta", {}).get("paginate", {}).get("next_page")
        return messages, next_page

    def list_chat_members(self, chat_id: int, *, cursor: str | None = None, limit: int = 50) -> tuple[list[ChatMember], str | None]:
        params = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        payload = self._request("GET", f"/chats/{chat_id}/members", params=params)
        members = [ChatMember.model_validate(item) for item in payload.get("data", [])]
        next_page = payload.get("meta", {}).get("paginate", {}).get("next_page")
        return members, next_page

    def get_profile(self) -> dict[str, Any]:
        return self._request("GET", "/profile")

    def create_reminder(
        self,
        *,
        chat_id: int,
        content: str,
        due_at: str | None,
        all_day: bool,
        priority: int,
        performer_ids: list[int] | None = None,
    ) -> dict[str, Any]:
        task: dict[str, Any] = {
            "kind": "reminder",
            "content": content,
            "priority": priority,
            "chat_id": chat_id,
            "all_day": all_day,
        }
        if due_at:
            task["due_at"] = due_at
        if performer_ids:
            task["performer_ids"] = performer_ids

        body = {"task": task}
        if self.settings.dry_run:
            return {"data": {"id": 0, **task}}
        return self._request("POST", "/tasks", json=body)

    def send_message(self, *, entity_type: str, entity_id: int, content: str) -> dict[str, Any]:
        body = {
            "message": {
                "entity_type": entity_type,
                "entity_id": entity_id,
                "content": content,
            },
            "link_preview": False,
        }
        if self.settings.dry_run:
            return {"data": {"id": 0, **body["message"]}}
        return self._request("POST", "/messages", json=body)

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not self.settings.pachca_access_token:
            raise RuntimeError("PACHCA_ACCESS_TOKEN is not configured.")

        headers = {
            "Authorization": f"Bearer {self.settings.pachca_access_token}",
            "Content-Type": "application/json; charset=utf-8",
        }

        with httpx.Client(timeout=60.0) as client:
            response = client.request(
                method,
                f"{self.base_url}{path}",
                headers=headers,
                params=params,
                json=json,
            )
            response.raise_for_status()
            if response.content:
                return response.json()
            return {}
