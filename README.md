# Pachca AI Agent

FastAPI webhook-агент для Пачки, который:

- получает исходящие webhook-события от бота;
- собирает контекст из чата или треда;
- вызывает LLM через AITUNNEL;
- создаёт напоминание в Пачке со сроком;
- отвечает обратно в чат.

## Что умеет сейчас

- триггерится по команде бота (`/ai` и `@ai` по умолчанию);
- в тредах читает весь тред;
- в обычных чатах хранит дневную память:
  - первый вызов за день читает сообщения с начала дня;
  - следующий вызов читает только новые сообщения после прошлого вызова;
  - хранит короткую rolling summary в SQLite;
- не плодит точные дубли напоминаний в рамках одного дня;
- при создании назначает ответственным автора сообщения, чтобы уведомления приходили человеку, а не боту;
- умеет работать в `PACHCA_DRY_RUN=true`, чтобы безопасно прогонять тесты без создания реальных напоминаний.

## Переменные окружения

Скопируй `.env.example` в `.env` локально или положи эти же значения в отдельный env-файл на сервере:

```env
AITUNNEL_API_KEY=...
AITUNNEL_MODEL=deepseek-chat
AITUNNEL_BASE_URL=https://api.aitunnel.ru/v1
PACHCA_ACCESS_TOKEN=...
PACHCA_SIGNING_SECRET=...
PACHCA_BOT_ALIASES=@ai,/ai
AGENT_TIMEZONE=Europe/Moscow
MAX_CONTEXT_CHARS=8000
MAX_MESSAGES_PER_SCAN=200
PACHCA_DRY_RUN=false
DATABASE_PATH=/var/lib/pachca-ai-agent/agent.db
```

`.env` и реальные ключи в git не коммитятся. В репозиторий уходит только код и `.env.example`.

### Что важно

- `PACHCA_ACCESS_TOKEN` — токен бота или персональный токен со скоупами `messages:read`, `messages:create`, `tasks:create`.
- `PACHCA_SIGNING_SECRET` — `Signing secret` из вкладки исходящего webhook у бота.
- `PACHCA_BOT_ALIASES` — список триггеров через запятую. По умолчанию: `@ai,/ai`.
- `DATABASE_PATH` — путь к SQLite базе на сервере. Лучше вынести её за пределы репозитория.

## Локальный запуск

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Проверка health endpoint:

```bash
curl http://127.0.0.1:8000/
```

## Деплой на свой сервер

1. Клонируй репозиторий на сервер, например в `/opt/pachca-ai-agent`.
2. Создай виртуальное окружение и поставь зависимости:

```bash
cd /opt/pachca-ai-agent
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

3. Создай env-файл, например `/etc/pachca-ai-agent.env`, и заполни переменные из `.env.example`.
4. Скопируй [deploy/pachca-ai-agent.service.example](deploy/pachca-ai-agent.service.example) в `/etc/systemd/system/pachca-ai-agent.service` и поправь `User`, `Group`, `WorkingDirectory`, `EnvironmentFile`.
5. Запусти сервис:

```bash
sudo systemctl daemon-reload
sudo systemctl enable pachca-ai-agent
sudo systemctl start pachca-ai-agent
sudo systemctl status pachca-ai-agent
```

6. Выставь наружу HTTPS URL через Nginx или Caddy и укажи его в Пачке как webhook:

```text
https://bot.example.com/webhook
```

## Пример webhook payload

```json
{
  "event": "new",
  "type": "message",
  "webhook_timestamp": 1744618734,
  "chat_id": 918264,
  "content": "@ai создай задачу по обсуждению до завтра 14:00",
  "user_id": 134412,
  "id": 56431,
  "created_at": "2025-04-14T08:18:54.000Z",
  "parent_message_id": null,
  "entity_type": "discussion",
  "entity_id": 918264,
  "thread": null,
  "url": "https://app.pachca.com/chats/124511?message=56431"
}
```

## Как агент отвечает

- если дедлайн понятен — создаёт напоминание и пишет подтверждение;
- если данных для плана или напоминания не хватает — просит уточнить;
- если похожая задача уже создавалась сегодня — сообщает, что дубль пропущен;
- `/help` и `/ai help` не используются как рабочие команды и не влияют на контекст.

## Ограничения текущей версии

- формы (`/views/open`) пока не подключены;
- поиск ответственного по имени пока не реализован;
- при очень длинном первом вызове за день контекст ужимается по схеме `начало + конец`.
