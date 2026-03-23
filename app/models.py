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


class TaskRecord(BaseModel):
    title: str
    task_id: int | None = None
    due_at: str | None = None


class ConversationState(BaseModel):
    chat_id: int
    date: str
    last_processed_message_id: int | None = None
    last_processed_at: str | None = None
    rolling_summary: str = ""
    created_tasks: list[TaskRecord] = Field(default_factory=list)


class TaskDecision(BaseModel):
    action: Literal["create_task", "ask_deadline", "noop"]
    title: str | None = None
    details: str | None = None
    due_at: str | None = None
    all_day: bool = False
    priority: int = 1
    updated_summary: str = ""
    reply_message: str
