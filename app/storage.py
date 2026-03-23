from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from app.models import ConversationState, TaskRecord


class StateStore:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _init_db(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS conversation_state (
                    chat_id INTEGER NOT NULL,
                    date TEXT NOT NULL,
                    last_processed_message_id INTEGER,
                    last_processed_at TEXT,
                    rolling_summary TEXT NOT NULL DEFAULT '',
                    created_tasks_json TEXT NOT NULL DEFAULT '[]',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (chat_id, date)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS processed_webhooks (
                    webhook_key TEXT PRIMARY KEY,
                    processed_at TEXT NOT NULL
                )
                """
            )

    def get_state(self, chat_id: int, date_key: str) -> ConversationState | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT chat_id, date, last_processed_message_id, last_processed_at,
                       rolling_summary, created_tasks_json
                FROM conversation_state
                WHERE chat_id = ? AND date = ?
                """,
                (chat_id, date_key),
            ).fetchone()

        if row is None:
            return None

        created_tasks = [TaskRecord.model_validate(item) for item in json.loads(row["created_tasks_json"] or "[]")]
        return ConversationState(
            chat_id=row["chat_id"],
            date=row["date"],
            last_processed_message_id=row["last_processed_message_id"],
            last_processed_at=row["last_processed_at"],
            rolling_summary=row["rolling_summary"] or "",
            created_tasks=created_tasks,
        )

    def save_state(self, state: ConversationState) -> None:
        payload = json.dumps([task.model_dump(mode="json") for task in state.created_tasks], ensure_ascii=False)
        updated_at = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO conversation_state (
                    chat_id, date, last_processed_message_id, last_processed_at,
                    rolling_summary, created_tasks_json, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id, date) DO UPDATE SET
                    last_processed_message_id = excluded.last_processed_message_id,
                    last_processed_at = excluded.last_processed_at,
                    rolling_summary = excluded.rolling_summary,
                    created_tasks_json = excluded.created_tasks_json,
                    updated_at = excluded.updated_at
                """,
                (
                    state.chat_id,
                    state.date,
                    state.last_processed_message_id,
                    state.last_processed_at,
                    state.rolling_summary,
                    payload,
                    updated_at,
                ),
            )

    def is_processed(self, webhook_key: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM processed_webhooks WHERE webhook_key = ?",
                (webhook_key,),
            ).fetchone()
        return row is not None

    def mark_processed(self, webhook_key: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO processed_webhooks (webhook_key, processed_at)
                VALUES (?, ?)
                """,
                (webhook_key, datetime.now(timezone.utc).isoformat()),
            )

