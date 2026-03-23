from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from app.models import ConversationState, ReminderRecord


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
                    last_task_request_message_id INTEGER,
                    last_task_request_at TEXT,
                    rolling_summary TEXT NOT NULL DEFAULT '',
                    created_reminders_json TEXT NOT NULL DEFAULT '[]',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (chat_id, date)
                )
                """
            )
            self._migrate_conversation_state_table(connection)
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS processed_webhooks (
                    webhook_key TEXT PRIMARY KEY,
                    processed_at TEXT NOT NULL
                )
                """
            )

    def _migrate_conversation_state_table(self, connection: sqlite3.Connection) -> None:
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(conversation_state)").fetchall()
        }
        if "created_reminders_json" not in columns:
            connection.execute(
                "ALTER TABLE conversation_state ADD COLUMN created_reminders_json TEXT NOT NULL DEFAULT '[]'"
            )
        if "last_task_request_message_id" not in columns:
            connection.execute("ALTER TABLE conversation_state ADD COLUMN last_task_request_message_id INTEGER")
        if "last_task_request_at" not in columns:
            connection.execute("ALTER TABLE conversation_state ADD COLUMN last_task_request_at TEXT")
        if "created_tasks_json" in columns:
            connection.execute(
                """
                UPDATE conversation_state
                SET created_reminders_json = created_tasks_json
                WHERE (created_reminders_json IS NULL OR created_reminders_json = '[]')
                  AND created_tasks_json IS NOT NULL
                """
            )
        if "last_processed_message_id" in columns:
            connection.execute(
                """
                UPDATE conversation_state
                SET last_task_request_message_id = last_processed_message_id
                WHERE last_task_request_message_id IS NULL
                  AND last_processed_message_id IS NOT NULL
                """
            )
        if "last_processed_at" in columns:
            connection.execute(
                """
                UPDATE conversation_state
                SET last_task_request_at = last_processed_at
                WHERE last_task_request_at IS NULL
                  AND last_processed_at IS NOT NULL
                """
            )

    def get_state(self, chat_id: int, date_key: str) -> ConversationState | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT chat_id, date, last_task_request_message_id, last_task_request_at,
                       rolling_summary, created_reminders_json
                FROM conversation_state
                WHERE chat_id = ? AND date = ?
                """,
                (chat_id, date_key),
            ).fetchone()

        if row is None:
            return None

        created_reminders = [
            ReminderRecord.model_validate(item)
            for item in json.loads(row["created_reminders_json"] or "[]")
        ]
        return ConversationState(
            chat_id=row["chat_id"],
            date=row["date"],
            last_task_request_message_id=row["last_task_request_message_id"],
            last_task_request_at=row["last_task_request_at"],
            rolling_summary=row["rolling_summary"] or "",
            created_reminders=created_reminders,
        )

    def save_state(self, state: ConversationState) -> None:
        payload = json.dumps(
            [reminder.model_dump(mode="json") for reminder in state.created_reminders],
            ensure_ascii=False,
        )
        updated_at = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO conversation_state (
                    chat_id, date, last_task_request_message_id, last_task_request_at,
                    rolling_summary, created_reminders_json, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id, date) DO UPDATE SET
                    last_task_request_message_id = excluded.last_task_request_message_id,
                    last_task_request_at = excluded.last_task_request_at,
                    rolling_summary = excluded.rolling_summary,
                    created_reminders_json = excluded.created_reminders_json,
                    updated_at = excluded.updated_at
                """,
                (
                    state.chat_id,
                    state.date,
                    state.last_task_request_message_id,
                    state.last_task_request_at,
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

    def try_claim_webhook(self, webhook_key: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO processed_webhooks (webhook_key, processed_at)
                VALUES (?, ?)
                """,
                (webhook_key, datetime.now(timezone.utc).isoformat()),
            )
            return cursor.rowcount > 0

    def mark_processed(self, webhook_key: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO processed_webhooks (webhook_key, processed_at)
                VALUES (?, ?)
                """,
                (webhook_key, datetime.now(timezone.utc).isoformat()),
            )
