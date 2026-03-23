from __future__ import annotations

import hashlib
import hmac
import logging
import re
from time import perf_counter
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app.config import Settings
from app.llm import AITunnelClient, LLMError
from app.models import AgentDecision, ChatMember, ChatMessage, ConversationState, ReminderDraft, ReminderRecord, WebhookMessage
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
        self._bot_user_id: int | None = None

    def handle_webhook(self, payload: WebhookMessage, raw_body: bytes, signature: str | None) -> dict:
        started_at = perf_counter()
        logger.info(
            "Webhook received: type=%s event=%s message_id=%s chat_id=%s entity_type=%s entity_id=%s user_id=%s content=%r",
            payload.type,
            payload.event,
            payload.effective_message_id,
            payload.chat_id,
            payload.entity_type,
            payload.entity_id,
            payload.user_id,
            (payload.content or "")[:240],
        )
        self._verify_signature(payload, raw_body, signature)

        webhook_key = self._build_webhook_key(payload)
        if not self.store.try_claim_webhook(webhook_key):
            logger.info("Webhook duplicate ignored: key=%s", webhook_key)
            return {"status": "duplicate"}

        if payload.type != "message" or payload.event != "new" or not payload.effective_message_id:
            self.store.mark_processed(webhook_key)
            logger.info(
                "Webhook ignored: unsupported_event type=%s event=%s message_id=%s elapsed=%.2fs",
                payload.type,
                payload.event,
                payload.effective_message_id,
                perf_counter() - started_at,
            )
            return {"status": "ignored", "reason": "unsupported_event"}

        if self._is_self_message(payload.user_id):
            self.store.mark_processed(webhook_key)
            logger.info(
                "Webhook ignored: self_message message_id=%s user_id=%s elapsed=%.2fs",
                payload.effective_message_id,
                payload.user_id,
                perf_counter() - started_at,
            )
            return {"status": "ignored", "reason": "self_message"}

        trigger_match = self._match_trigger(payload.content or "")
        request_text = self._strip_trigger(payload.content or "", trigger_match) if trigger_match else ""
        if trigger_match is None:
            self.store.mark_processed(webhook_key)
            logger.info(
                "Webhook ignored: no_trigger message_id=%s content=%r elapsed=%.2fs",
                payload.effective_message_id,
                (payload.content or "")[:240],
                perf_counter() - started_at,
            )
            return {"status": "ignored", "reason": "no_trigger"}

        improvement_text = self._extract_improvement_request(request_text)
        if improvement_text:
            self._append_improvement_request(payload, improvement_text, now_text=payload.created_at.isoformat() if payload.created_at else None)
            self._safe_reply(payload, "Записал доработку в журнал. Потом прочитаю и дойду до неё.")
            self.store.mark_processed(webhook_key)
            logger.info("Improvement logged: message_id=%s elapsed=%.2fs", payload.effective_message_id, perf_counter() - started_at)
            return {"status": "ok", "action": "log_improvement"}

        if self._is_help_request(payload.content or "", request_text):
            self._safe_reply(payload, self._build_help_text())
            self.store.mark_processed(webhook_key)
            logger.info("Help handled: message_id=%s elapsed=%.2fs", payload.effective_message_id, perf_counter() - started_at)
            return {"status": "ok", "action": "help"}

        now_utc = payload.created_at.astimezone(UTC) if payload.created_at else datetime.now(UTC)
        now_local = now_utc.astimezone(self.timezone)

        if payload.is_thread:
            messages = self._collect_thread_messages(payload)
            existing_summary = ""
            created_titles: list[str] = []
            state = None
        else:
            state, messages = self._collect_daily_chat_context(payload, now_local)
            existing_summary = state.rolling_summary if state else ""
            created_titles = [reminder.title for reminder in state.created_reminders[-10:]] if state else []

        rendered_messages = self._render_messages(messages)
        intent_hints = self._build_intent_hints(request_text, rendered_messages)
        logger.info(
            "Context collected: message_id=%s messages=%s rendered_chars=%s summary_chars=%s created_today=%s elapsed=%.2fs",
            payload.effective_message_id,
            len(messages),
            len(rendered_messages),
            len(existing_summary),
            len(created_titles),
            perf_counter() - started_at,
        )

        try:
            decision = self.llm.analyze(
                request_text=request_text,
                rendered_messages=rendered_messages,
                existing_summary=existing_summary,
                created_titles=created_titles,
                now_local=now_local,
                intent_hints=intent_hints,
            )
        except LLMError as exc:
            logger.exception("LLM error while analyzing webhook")
            self._safe_reply(payload, f"Не смог разобрать запрос: {exc}")
            self.store.mark_processed(webhook_key)
            return {"status": "error", "reason": "llm_error"}
        except Exception as exc:  # pragma: no cover
            logger.exception("Unexpected LLM failure")
            self._safe_reply(payload, f"Не смог обратиться к модели: {exc}")
            self.store.mark_processed(webhook_key)
            return {"status": "error", "reason": "llm_request_failed"}

        logger.info(
            "LLM decision: message_id=%s action=%s reminders=%s reply_chars=%s elapsed=%.2fs",
            payload.effective_message_id,
            decision.action,
            len(decision.reminders),
            len(decision.reply_message or ""),
            perf_counter() - started_at,
        )

        reminders_to_create, skipped_titles = self._filter_duplicate_reminders(
            decision.reminders,
            state.created_reminders if state else [],
        )

        result: dict[str, object] = {"status": "ok", "action": decision.action}
        created_records: list[ReminderRecord] = []

        if decision.action == "create_reminders":
            if not reminders_to_create and skipped_titles:
                message = self._build_duplicate_reply(skipped_titles, decision.reply_message)
                self._safe_reply(payload, message)
                result["action"] = "noop"
                result["reason"] = "duplicates_only"
            elif not reminders_to_create:
                message = decision.reply_message or "Не увидел, какие именно напоминания нужно создать. Уточни формулировку."
                self._safe_reply(payload, message)
                result = {"status": "error", "reason": "empty_reminders"}
            else:
                members = self._fetch_chat_members(payload.root_chat_id or payload.chat_id)
                global_assignees = self._extract_mentioned_members(request_text, members)
                created_ids: list[int] = []
                logger.info(
                    "Creating reminders: message_id=%s requested=%s filtered=%s skipped=%s global_assignees=%s elapsed=%.2fs",
                    payload.effective_message_id,
                    len(decision.reminders),
                    len(reminders_to_create),
                    len(skipped_titles),
                    [member.id for member in global_assignees],
                    perf_counter() - started_at,
                )

                for reminder in reminders_to_create:
                    performer_ids = self._resolve_performer_ids(
                        request_text=request_text,
                        members=members,
                        global_assignees=global_assignees,
                        reminder=reminder,
                        fallback_user_id=payload.user_id,
                    )
                    reminder_payload = self.pachca.create_reminder(
                        chat_id=payload.root_chat_id or payload.chat_id,
                        content=self._build_reminder_content(reminder, members, performer_ids),
                        due_at=reminder.due_at,
                        all_day=reminder.all_day,
                        priority=reminder.priority,
                        performer_ids=performer_ids or None,
                    )
                    reminder_id = reminder_payload.get("data", {}).get("id")
                    created_ids.append(reminder_id)
                    created_records.append(
                        ReminderRecord(
                            title=reminder.title,
                            reminder_id=reminder_id,
                            due_at=reminder.due_at,
                        )
                    )

                reply = self._build_created_reply(
                    created=reminders_to_create,
                    created_ids=created_ids,
                    skipped_titles=skipped_titles,
                    llm_reply=decision.reply_message,
                )
                self._safe_reply(payload, reply)
                result["created_count"] = len(created_records)
                result["reminder_ids"] = created_ids
        elif decision.action in {"ask_followup", "reply"}:
            if decision.reply_message:
                self._safe_reply(payload, decision.reply_message)
        else:
            if decision.reply_message:
                self._safe_reply(payload, decision.reply_message)

        if not payload.is_thread:
            new_state = self._update_state(
                payload=payload,
                previous=state,
                now_local=now_local,
                decision=decision,
                created_reminders=created_records,
            )
            self.store.save_state(new_state)

        self.store.mark_processed(webhook_key)
        logger.info(
            "Webhook handled: message_id=%s action=%s created=%s elapsed=%.2fs",
            payload.effective_message_id,
            result.get("action"),
            len(created_records),
            perf_counter() - started_at,
        )
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
        stripped = content.strip()
        for alias in self.settings.pachca_bot_aliases:
            pattern = re.compile(rf"^{re.escape(alias)}(?:$|[\s,.:;!?-])", re.IGNORECASE)
            if pattern.search(stripped):
                return alias
        return None

    def _is_self_message(self, user_id: int | None) -> bool:
        if not user_id:
            return False
        bot_user_id = self._get_bot_user_id()
        return bool(bot_user_id and user_id == bot_user_id)

    def _get_bot_user_id(self) -> int | None:
        if self._bot_user_id is not None:
            return self._bot_user_id
        try:
            payload = self.pachca.get_profile()
        except Exception:
            logger.exception("Failed to fetch Pachca bot profile")
            return None
        user_id = payload.get("data", {}).get("id")
        self._bot_user_id = int(user_id) if user_id else None
        logger.info("Resolved bot user_id=%s", self._bot_user_id)
        return self._bot_user_id

    @staticmethod
    def _strip_trigger(content: str, alias: str | None) -> str:
        if not alias:
            return content.strip()
        pattern = re.compile(re.escape(alias), re.IGNORECASE)
        return pattern.sub("", content, count=1).strip(" ,:\n\t")

    def _is_help_request(self, content: str, request_text: str) -> bool:
        content_lower = content.strip().lower()
        request_lower = request_text.strip().lower()
        direct_commands = {"/help", "/start", "help", "помощь"}
        after_ai_help = {
            "help",
            "помощь",
            "что умеешь",
            "что ты умеешь",
            "команды",
            "как пользоваться",
        }
        return content_lower in direct_commands or request_lower in after_ai_help

    def _extract_improvement_request(self, request_text: str) -> str | None:
        lowered = request_text.lower().strip()
        prefixes = ("#доработка:", "#доработка ", "#improvement:", "#improvement ")
        for prefix in prefixes:
            if lowered.startswith(prefix):
                original = request_text.strip()
                return original[len(prefix) :].strip(" \n\t:-")
        return None

    def _build_help_text(self) -> str:
        return (
            "Я умею отвечать как AI и создавать напоминания в Пачке.\n\n"
            "Как пользоваться:\n"
            "- /ai что можно приготовить на ужин?\n"
            "- /ai объясни коротко, что важно в сообщениях выше\n"
            "- /ai создай напоминание мне на сегодня 19:00 купить продукты\n"
            "- /ai создай напоминания по задачам выше на завтра 18:00\n"
            "- /ai придумай план и создай напоминание по нему\n\n"
            "Ответственные:\n"
            "- если написать @участника чата, напоминание будет назначено ему\n"
            "- если отметить нескольких через @, назначу всем\n"
            "- если @ нет, назначаю напоминание автору команды\n\n"
            "Подсказки:\n"
            "- /help или /ai help — показать эту справку\n"
            "- /ai #доработка: что улучшить в агенте — записать идею в журнал доработок\n"
            "- можно ссылаться на список выше: \"создай напоминания по задачам выше\"\n"
            "- если срока не хватает, я должен уточнить его вопросом"
        )

    def _append_improvement_request(self, payload: WebhookMessage, improvement_text: str, now_text: str | None) -> None:
        timestamp = now_text or datetime.now(self.timezone).isoformat()
        path: Path = self.settings.improvements_log_path
        lines = [
            f"## {timestamp}",
            f"- chat_id: {payload.chat_id}",
            f"- message_id: {payload.effective_message_id}",
            f"- user_id: {payload.user_id}",
            f"- entity_type: {payload.entity_type}",
            f"- entity_id: {payload.entity_id}",
            f"- request: {improvement_text}",
            "",
        ]
        with path.open("a", encoding="utf-8") as file:
            file.write("\n".join(lines))

    def _build_intent_hints(self, request_text: str, rendered_messages: str) -> dict[str, bool]:
        lowered = request_text.lower()
        return {
            "asks_for_multiple": any(
                phrase in lowered
                for phrase in (
                    "напоминания",
                    "несколько напоминаний",
                    "по задачам выше",
                    "по списку выше",
                    "по пунктам выше",
                )
            ),
            "references_previous_list": any(
                phrase in lowered
                for phrase in ("выше", "этим задачам", "по списку", "по пунктам")
            ),
            "has_list_in_context": bool(re.search(r"(?m)^\s*(?:\d+[.)]|[a-zа-я][.)]|[-*])\s+", rendered_messages, re.IGNORECASE)),
            "looks_like_general_question": any(
                token in lowered
                for token in ("что", "как", "почему", "придумай", "посоветуй", "объясни", "проанализируй")
            ),
            "has_mentions": "@" in request_text,
        }

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
        cutoff_message_id = state.last_task_request_message_id if state else None

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
        seen_cursors: set[str] = set()
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
            if cursor in seen_cursors:
                logger.warning("Stopping message scan because cursor repeated: chat_id=%s cursor=%s", chat_id, cursor)
                break
            seen_cursors.add(cursor)

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
        head_budget = max(800, int(budget * 0.35))
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

    @staticmethod
    def _normalize_person_name(name: str | None) -> str:
        if not name:
            return ""
        normalized = name.strip().lower().replace("ё", "е")
        normalized = re.sub(r"\s+", " ", normalized)
        return re.sub(r"[^\wа-я ]+", "", normalized)

    @staticmethod
    def _due_bucket(due_at: str | None) -> str:
        if not due_at:
            return ""
        return due_at[:16]

    def _filter_duplicate_reminders(
        self,
        reminders: list[ReminderDraft],
        existing_records: list[ReminderRecord],
    ) -> tuple[list[ReminderDraft], list[str]]:
        existing_keys = {
            (self._normalize_title(record.title), self._due_bucket(record.due_at))
            for record in existing_records
        }
        seen_now: set[tuple[str, str]] = set()
        filtered: list[ReminderDraft] = []
        skipped: list[str] = []

        for reminder in reminders:
            key = (self._normalize_title(reminder.title), self._due_bucket(reminder.due_at))
            if not key[0]:
                continue
            if key in existing_keys or key in seen_now:
                skipped.append(reminder.title)
                continue
            filtered.append(reminder)
            seen_now.add(key)

        return filtered, skipped

    def _fetch_chat_members(self, chat_id: int) -> list[ChatMember]:
        members: list[ChatMember] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        pages = 0

        while True:
            batch, cursor = self.pachca.list_chat_members(chat_id, cursor=cursor)
            members.extend(batch)
            pages += 1
            if not cursor:
                break
            if cursor in seen_cursors:
                logger.warning("Stopping member scan because cursor repeated: chat_id=%s cursor=%s pages=%s", chat_id, cursor, pages)
                break
            seen_cursors.add(cursor)
            if pages >= 20:
                logger.warning("Stopping member scan because max pages reached: chat_id=%s pages=%s", chat_id, pages)
                break

        return members

    def _extract_mentioned_members(self, request_text: str, members: list[ChatMember]) -> list[ChatMember]:
        if "@" not in request_text or not members:
            return []

        unique_first_names: dict[str, int] = {}
        unique_nicknames: dict[str, int] = {}
        for member in members:
            first_key = self._normalize_person_name(member.first_name)
            if first_key:
                unique_first_names[first_key] = unique_first_names.get(first_key, 0) + 1
            nick_key = self._normalize_person_name(member.nickname)
            if nick_key:
                unique_nicknames[nick_key] = unique_nicknames.get(nick_key, 0) + 1

        variants: list[tuple[str, str, int]] = []
        for member in members:
            candidate_values = [member.full_name]
            first_key = self._normalize_person_name(member.first_name)
            nick_key = self._normalize_person_name(member.nickname)
            if first_key and unique_first_names.get(first_key) == 1:
                candidate_values.append(member.first_name)
            if nick_key and unique_nicknames.get(nick_key) == 1:
                candidate_values.append(member.nickname or "")

            seen_local: set[str] = set()
            for value in candidate_values:
                normalized = self._normalize_person_name(value)
                if not normalized or normalized in seen_local:
                    continue
                seen_local.add(normalized)
                variants.append((value.strip(), normalized, member.id))

        variants.sort(key=lambda item: len(item[1]), reverse=True)
        matched_ids: list[int] = []

        for original_value, _, member_id in variants:
            pattern = re.compile(rf"(?<!\w)@{re.escape(original_value)}(?=$|[^\wа-яё])", re.IGNORECASE)
            if pattern.search(request_text) and member_id not in matched_ids:
                matched_ids.append(member_id)

        return [member for member in members if member.id in matched_ids]

    def _resolve_members_by_hint(self, hint: str | None, members: list[ChatMember]) -> list[ChatMember]:
        normalized_hint = self._normalize_person_name(hint)
        if not normalized_hint:
            return []

        exact_matches: list[ChatMember] = []
        partial_matches: list[ChatMember] = []
        for member in members:
            variants = {
                self._normalize_person_name(member.full_name),
                self._normalize_person_name(member.first_name),
                self._normalize_person_name(member.nickname),
            }
            variants.discard("")
            if normalized_hint in variants:
                exact_matches.append(member)
            elif any(normalized_hint in variant or variant in normalized_hint for variant in variants):
                partial_matches.append(member)

        if exact_matches:
            return exact_matches
        if len(partial_matches) == 1:
            return partial_matches
        return []

    def _resolve_performer_ids(
        self,
        *,
        request_text: str,
        members: list[ChatMember],
        global_assignees: list[ChatMember],
        reminder: ReminderDraft,
        fallback_user_id: int | None,
    ) -> list[int]:
        hint_matches = self._resolve_members_by_hint(reminder.assignee_hint, members)
        if hint_matches:
            return [member.id for member in hint_matches]
        if global_assignees:
            return [member.id for member in global_assignees]
        if fallback_user_id:
            return [fallback_user_id]
        return []

    def _member_names_by_ids(self, members: list[ChatMember], performer_ids: list[int]) -> list[str]:
        id_set = set(performer_ids)
        names = [member.full_name or member.first_name for member in members if member.id in id_set]
        return [name for name in names if name]

    def _build_reminder_content(
        self,
        reminder: ReminderDraft,
        members: list[ChatMember],
        performer_ids: list[int],
    ) -> str:
        parts = [reminder.title.strip()]
        details = (reminder.details or "").strip()

        if details:
            parts.append(details)

        assignee_names = self._member_names_by_ids(members, performer_ids)
        if assignee_names:
            label = ", ".join(assignee_names[:4])
            if len(assignee_names) > 4:
                label += f" и ещё {len(assignee_names) - 4}"
            if not details or label.lower() not in details.lower():
                parts.append(f"Ответственные: {label}")
        elif reminder.assignee_hint:
            parts.append(f"Для: {reminder.assignee_hint}")

        return "\n\n".join(parts)

    @staticmethod
    def _format_due(due_at: str | None) -> str:
        return due_at or "без срока"

    def _build_created_reply(
        self,
        *,
        created: list[ReminderDraft],
        created_ids: list[int],
        skipped_titles: list[str],
        llm_reply: str,
    ) -> str:
        if llm_reply:
            base = llm_reply.strip()
        elif len(created) == 1:
            reminder_id = created_ids[0] if created_ids else None
            suffix = f" #{reminder_id}" if reminder_id else ""
            base = f"Создал напоминание{suffix}: {created[0].title}. Срок: {self._format_due(created[0].due_at)}"
        else:
            preview = "\n".join(
                f"- {item.title} ({self._format_due(item.due_at)})"
                for item in created[:6]
            )
            extra = f"\nИ ещё {len(created) - 6} шт." if len(created) > 6 else ""
            base = f"Создал {len(created)} напоминания:\n{preview}{extra}"

        if skipped_titles:
            unique_skipped = list(dict.fromkeys(skipped_titles))
            skipped_preview = ", ".join(unique_skipped[:3])
            tail = f"\nПропустил дубли: {skipped_preview}"
            if len(unique_skipped) > 3:
                tail += f" и ещё {len(unique_skipped) - 3}"
            base += tail

        return base

    @staticmethod
    def _build_duplicate_reply(skipped_titles: list[str], llm_reply: str) -> str:
        if llm_reply:
            return llm_reply
        unique_skipped = list(dict.fromkeys(skipped_titles))
        if len(unique_skipped) == 1:
            return f'Напоминание "{unique_skipped[0]}" уже создавалось сегодня.'
        preview = ", ".join(unique_skipped[:4])
        extra = f" и ещё {len(unique_skipped) - 4}" if len(unique_skipped) > 4 else ""
        return f"Похоже, эти напоминания уже создавались сегодня: {preview}{extra}."

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
        decision: AgentDecision,
        created_reminders: list[ReminderRecord],
    ) -> ConversationState:
        date_key = now_local.date().isoformat()
        state_reminders = list(previous.created_reminders) if previous else []
        state_reminders.extend(created_reminders)

        rolling_summary = decision.updated_summary or (previous.rolling_summary if previous else "")
        last_task_request_message_id = previous.last_task_request_message_id if previous else None
        last_task_request_at = previous.last_task_request_at if previous else None

        if created_reminders:
            last_task_request_message_id = payload.effective_message_id
            last_task_request_at = now_local.isoformat()

        return ConversationState(
            chat_id=payload.chat_id,
            date=date_key,
            last_task_request_message_id=last_task_request_message_id,
            last_task_request_at=last_task_request_at,
            rolling_summary=rolling_summary,
            created_reminders=state_reminders[-50:],
        )
