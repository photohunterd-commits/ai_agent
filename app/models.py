from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class ThreadInfo(BaseModel):
    message_id: int | None = None
    message_chat_id: int | None = None


class WebhookMessage(BaseModel):
    type: str
    event: str
    id: int | None = None
    message_id: int | None = None
    chat_id: int
    content: str | None = ""
    user_id: int | None = None
    created_at: datetime | None = None
    entity_type: str | None = None
    entity_id: int | None = None
    parent_message_id: int | None = None
    root_chat_id: int | None = None
    thread: ThreadInfo | None = None
    url: str | None = None
    webhook_timestamp: int | None = None

    @property
    def effective_message_id(self) -> int | None:
        return self.id or self.message_id

    @property
    def is_thread(self) -> bool:
        return self.entity_type == "thread"


class ChatMessage(BaseModel):
    id: int
    content: str = ""
    user_id: int | None = None
    created_at: datetime
    chat_id: int | None = None
    entity_type: str | None = None
    entity_id: int | None = None


class ChatMember(BaseModel):
    id: int
    first_name: str
    last_name: str | None = None
    nickname: str | None = None
    email: str | None = None
    role: str | None = None
    bot: bool = False

    @property
    def full_name(self) -> str:
        return " ".join(part for part in (self.first_name, self.last_name or "") if part).strip()


class ReminderRecord(BaseModel):
    title: str
    reminder_id: int | None = None
    due_at: str | None = None


class ConversationState(BaseModel):
    chat_id: int
    date: str
    last_task_request_message_id: int | None = None
    last_task_request_at: str | None = None
    rolling_summary: str = ""
    created_reminders: list[ReminderRecord] = Field(default_factory=list)


class ReminderDraft(BaseModel):
    title: str
    details: str | None = None
    due_at: str | None = None
    all_day: bool = False
    priority: int = 1
    assignee_hint: str | None = None


class AgentDecision(BaseModel):
    action: Literal["create_reminders", "ask_followup", "reply", "noop"]
    reminders: list[ReminderDraft] = Field(default_factory=list)
    updated_summary: str = ""
    reply_message: str = ""
