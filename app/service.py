from __future__ import annotations

import hashlib
import hmac
import logging
import re
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from app.config import Settings
from app.llm import AITunnelClient, LLMError
from app.models import ChatMessage, ConversationState, TaskDecision, TaskRecord, WebhookMessage
from app.pachca import PachcaClient
from app.storage import StateStore

logger = logging.getLogger(__name__)


class AgentService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.store = StateStore(settings.database_path)
        self.pachca = PachcaClient(settings)
        self.llm = AITunnelClient(settings)
        self.timezone = ZoneInfo(settings.agent_timezone)

    def handle_webhook(self, payload: WebhookMessage, raw_body: bytes, signature: str | None) -> dict:
        self._verify_signature(payload, raw_body, signature)

        webhook_key = self._build_webhook_key(payload)
        if self.store.is_processed(webhook_key):
            return {"status": "duplicate"}

        if payload.type != "message" or payload.event != "new" or not payload.effective_message_id:
            self.store.mark_processed(webhook_key)
            return {"status": "ignored", "reason": "unsupported_event"}

        trigger_match = self._match_trigger(payload.content or "")
        if trigger_match is None:
            self.store.mark_processed(webhook_key)
            return {"status": "ignored", "reason": "no_trigger"}

        now_utc = payload.created_at.astimezone(UTC) if payload.created_at else datetime.now(UTC)
        now_local = now_utc.astimezone(self.timezone)
        request_text = self._strip_trigger(payload.content or "", trigger_match)

        if payload.is_thread:
            messages = self._collect_thread_messages(payload)
            existing_summary = ""
            created_titles: list[str] = []
            state = None
        else:
            state, messages = self._collect_daily_chat_context(payload, now_local)
            existing_summary = state.rolling_summary if state else ""
            created_titles = [task.title for task in state.created_tasks] if state else []

        rendered_messages = self._render_messages(messages)

        try:
            decision = self.llm.analyze(
                request_text=request_text,
                rendered_messages=rendered_messages,
                existing_summary=existing_summary,
                created_titles=created_titles,
                now_local=now_local,
            )
        except LLMError as exc:
            logger.exception("LLM error while analyzing webhook")
            self._safe_reply(payload, f"Не смог разобрать задачу: {exc}")
            self.store.mark_processed(webhook_key)
            return {"status": "error", "reason": "llm_error"}
        except Exception as exc:  # pragma: no cover
            logger.exception("Unexpected LLM failure")
            self._safe_reply(payload, f"Не смог обратиться к модели: {exc}")
            self.store.mark_processed(webhook_key)
            return {"status": "error", "reason": "llm_request_failed"}

        duplicate_title = self._detect_duplicate(decision, created_titles)
        if duplicate_title:
            decision = TaskDecision(
                action="noop",
                title=decision.title,
                details=decision.details,
                due_at=decision.due_at,
                all_day=decision.all_day,
                priority=decision.priority,
                updated_summary=decision.updated_summary or existing_summary,
                reply_message=f'Похоже, задача "{duplicate_title}" уже создавалась сегодня, дубль пропустил.',
            )

        result: dict[str, object] = {"status": "ok", "action": decision.action}
        created_task_record: TaskRecord | None = None

        if decision.action == "create_task":
            if not decision.title or not decision.due_at:
                self._safe_reply(payload, "Не хватает названия или срока задачи, уточни формулировку.")
                result = {"status": "error", "reason": "invalid_llm_payload"}
            else:
                task_payload = self.pachca.create_task(
                    chat_id=payload.root_chat_id or payload.chat_id,
                    content=self._build_task_content(decision.title, decision.details),
                    due_at=decision.due_at,
                    all_day=decision.all_day,
                    priority=decision.priority,
                )
                task_id = task_payload.get("data", {}).get("id")
                created_task_record = TaskRecord(title=decision.title, task_id=task_id, due_at=decision.due_at)
                reply = decision.reply_message or self._build_created_reply(decision, task_id)
                self._safe_reply(payload, reply)
                result["task_id"] = task_id
        else:
            self._safe_reply(payload, decision.reply_message)

        if not payload.is_thread:
            new_state = self._update_state(
                payload=payload,
                previous=state,
                now_local=now_local,
                decision=decision,
                created_task=created_task_record,
            )
            self.store.save_state(new_state)

        self.store.mark_processed(webhook_key)
        return result

    def _verify_signature(self, payload: WebhookMessage, raw_body: bytes, signature: str | None) -> None:
        if self.settings.pachca_signing_secret:
            if not signature:
                raise ValueError("Missing Pachca-Signature header.")
            expected = hmac.new(
                self.settings.pachca_signing_secret.encode("utf-8"),
                raw_body,
                hashlib.sha256,
            ).hexdigest()
            if not hmac.compare_digest(expected, signature):
                raise ValueError("Invalid Pachca webhook signature.")

        if payload.webhook_timestamp is not None:
            sent_at = datetime.fromtimestamp(payload.webhook_timestamp, tz=UTC)
            if abs((datetime.now(UTC) - sent_at).total_seconds()) > 60:
                raise ValueError("Webhook timestamp is too old.")

    @staticmethod
    def _build_webhook_key(payload: WebhookMessage) -> str:
        identifier = payload.effective_message_id or 0
        timestamp = payload.webhook_timestamp or 0
        return f"{payload.type}:{payload.event}:{identifier}:{timestamp}"

    def _match_trigger(self, content: str) -> str | None:
        lowered = content.lower()
        for alias in self.settings.pachca_bot_aliases:
            if alias.lower() in lowered:
                return alias
        return None

    @staticmethod
    def _strip_trigger(content: str, alias: str) -> str:
        pattern = re.compile(re.escape(alias), re.IGNORECASE)
        return pattern.sub("", content, count=1).strip(" ,:\n\t")

    def _collect_thread_messages(self, payload: WebhookMessage) -> list[ChatMessage]:
        return self._scan_messages(
            chat_id=payload.chat_id,
            current_message_id=payload.effective_message_id,
            cutoff_message_id=None,
            start_utc=None,
        )

    def _collect_daily_chat_context(
        self,
        payload: WebhookMessage,
        now_local: datetime,
    ) -> tuple[ConversationState | None, list[ChatMessage]]:
        date_key = now_local.date().isoformat()
        state = self.store.get_state(payload.chat_id, date_key)
        day_start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        day_start_utc = day_start_local.astimezone(UTC)
        cutoff_message_id = state.last_processed_message_id if state else None

        messages = self._scan_messages(
            chat_id=payload.chat_id,
            current_message_id=payload.effective_message_id,
            cutoff_message_id=cutoff_message_id,
            start_utc=day_start_utc,
        )
        return state, messages

    def _scan_messages(
        self,
        *,
        chat_id: int,
        current_message_id: int | None,
        cutoff_message_id: int | None,
        start_utc: datetime | None,
    ) -> list[ChatMessage]:
        collected: list[ChatMessage] = []
        cursor: str | None = None
        done = False

        while not done and len(collected) < self.settings.max_messages_per_scan:
            batch, cursor = self.pachca.list_messages(chat_id, cursor=cursor)
            if not batch:
                break

            for message in batch:
                if current_message_id and message.id > current_message_id:
                    continue
                message_time = message.created_at.astimezone(UTC)
                if cutoff_message_id and message.id <= cutoff_message_id:
                    done = True
                    break
                if start_utc and message_time < start_utc:
                    done = True
                    break
                collected.append(message)

            if not cursor:
                break

        collected.reverse()
        return collected

    def _render_messages(self, messages: list[ChatMessage]) -> str:
        lines = [
            f"[{message.created_at.astimezone(self.timezone).strftime('%Y-%m-%d %H:%M')}] "
            f"user:{message.user_id or 'unknown'} msg:{message.id} {self._compact_text(message.content)}"
            for message in messages
        ]
        rendered = "\n".join(lines).strip()
        if len(rendered) <= self.settings.max_context_chars:
            return rendered

        budget = self.settings.max_context_chars
        head_budget = max(500, int(budget * 0.4))
        tail_budget = budget - head_budget - 64
        head = rendered[:head_budget].rstrip()
        tail = rendered[-tail_budget:].lstrip()
        return f"{head}\n\n... CONTEXT TRIMMED ...\n\n{tail}"

    @staticmethod
    def _compact_text(content: str) -> str:
        return " ".join((content or "").split())

    @staticmethod
    def _normalize_title(title: str | None) -> str:
        normalized = re.sub(r"\s+", " ", (title or "").strip().lower())
        return re.sub(r"[^\wа-яё ]+", "", normalized)

    def _detect_duplicate(self, decision: TaskDecision, created_titles: list[str]) -> str | None:
        normalized = self._normalize_title(decision.title)
        if not normalized or decision.action != "create_task":
            return None
        for title in created_titles:
            if normalized == self._normalize_title(title):
                return title
        return None

    @staticmethod
    def _build_task_content(title: str, details: str | None) -> str:
        title = title.strip()
        details = (details or "").strip()
        if not details:
            return title
        return f"{title}\n\n{details}"

    @staticmethod
    def _build_created_reply(decision: TaskDecision, task_id: int | None) -> str:
        suffix = f" #{task_id}" if task_id else ""
        due = f"\nСрок: {decision.due_at}" if decision.due_at else ""
        return f"Создал задачу{suffix}: {decision.title}.{due}"

    def _safe_reply(self, payload: WebhookMessage, content: str) -> None:
        if not content:
            return
        try:
            if not payload.entity_type or payload.entity_id is None:
                logger.warning("Skipping reply because entity routing is missing.")
                return
            self.pachca.send_message(
                entity_type=payload.entity_type,
                entity_id=payload.entity_id,
                content=content,
            )
        except Exception:  # pragma: no cover
            logger.exception("Failed to send reply back to Pachca")

    def _update_state(
        self,
        *,
        payload: WebhookMessage,
        previous: ConversationState | None,
        now_local: datetime,
        decision: TaskDecision,
        created_task: TaskRecord | None,
    ) -> ConversationState:
        date_key = now_local.date().isoformat()
        created_tasks = list(previous.created_tasks) if previous else []
        if created_task:
            created_tasks.append(created_task)

        rolling_summary = decision.updated_summary or (previous.rolling_summary if previous else "")

        return ConversationState(
            chat_id=payload.chat_id,
            date=date_key,
            last_processed_message_id=payload.effective_message_id,
            last_processed_at=now_local.isoformat(),
            rolling_summary=rolling_summary,
            created_tasks=created_tasks[-20:],
        )

