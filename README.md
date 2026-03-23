# Pachca AI Agent

FastAPI webhook-агент для Пачки, который:

- получает исходящие webhook-события от бота;
- собирает контекст из чата или треда;
- вызывает LLM через AITUNNEL;
- создаёт задачу в Пачке со сроком;
- отвечает обратно в чат.

## Что умеет сейчас

- триггерится по упоминанию бота (`@ai` по умолчанию);
- в тредах читает весь тред;
- в обычных чатах хранит дневную память:
  - первый вызов за день читает сообщения с начала дня;
  - следующий вызов читает только новые сообщения после прошлого вызова;
  - хранит короткую rolling summary в SQLite;
- не плодит точные дубли задач в рамках одного дня;
- умеет работать в `PACHCA_DRY_RUN=true`, чтобы безопасно прогонять тесты без создания реальных задач.

## Переменные окружения

Скопируй `.env.example` в `.env` и заполни значения:

```env
AITUNNEL_API_KEY=...
AITUNNEL_MODEL=deepseek-chat
PACHCA_ACCESS_TOKEN=...
PACHCA_SIGNING_SECRET=...
PACHCA_BOT_ALIASES=@ai,/ai
AGENT_TIMEZONE=Europe/Moscow
MAX_CONTEXT_CHARS=8000
MAX_MESSAGES_PER_SCAN=200
PACHCA_DRY_RUN=false
DATABASE_PATH=data/agent.db
```

`.env` в репозиторий не коммитится: он уже добавлен в `.gitignore`. Для open-source репозитория основной путь такой:

1. в GitHub пушится только код и `.env.example`;
2. реальные значения добавляются в Railway через `Variables`;
3. Railway сам прокидывает их приложению при запуске.

### Что важно

- `PACHCA_ACCESS_TOKEN` — токен бота или персональный токен со скоупами `messages:read`, `messages:create`, `tasks:create`.
- `PACHCA_SIGNING_SECRET` — `Signing secret` из вкладки исходящего webhook у бота.
- `PACHCA_BOT_ALIASES` — список триггеров через запятую. По умолчанию: `@ai,/ai`.

## Локальный запуск

```bash
python -m venv .venv
. .venv/Scripts/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Проверка health endpoint:

```bash
curl http://127.0.0.1:8000/
```

## Railway

1. Создай новый сервис из этого репозитория.
2. В `Variables` добавь реальные значения:
   - `AITUNNEL_API_KEY`
   - `AITUNNEL_MODEL`
   - `PACHCA_ACCESS_TOKEN`
   - `PACHCA_SIGNING_SECRET`
   - `PACHCA_BOT_ALIASES`
   - `AGENT_TIMEZONE`
   - `MAX_CONTEXT_CHARS`
   - `MAX_MESSAGES_PER_SCAN`
   - `PACHCA_DRY_RUN`
   - `DATABASE_PATH`
3. Railway поднимет сервис по `Procfile`.
4. Укажи в Пачке URL вида `https://<your-service>.up.railway.app/webhook`.

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

- если дедлайн понятен — создаёт задачу и пишет подтверждение;
- если дедлайн не указан или неясен — просит уточнить;
- если команда не похожа на создание задачи — отвечает подсказкой;
- если похожая задача уже создавалась сегодня — сообщает, что дубль пропущен.

## Ограничения текущей версии

- формы (`/views/open`) пока не подключены;
- поиск ответственного по имени пока не реализован;
- при очень длинном первом вызове за день контекст ужимается по схеме `начало + конец`.
