from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request

from app.config import load_settings
from app.models import WebhookMessage
from app.service import AgentService

settings = load_settings()
log_path = settings.database_path.parent / "agent.log"
log_path.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(Path(log_path), encoding="utf-8"),
    ],
)
service = AgentService(settings)
app = FastAPI(title="Pachca AI Agent")


@app.get("/")
def healthcheck() -> dict:
    return {
        "ok": True,
        "time": datetime.now(UTC).isoformat(),
        "dry_run": settings.dry_run,
        "model": settings.aitunnel_model,
        "aliases": list(settings.pachca_bot_aliases),
    }


def _process_webhook(payload: WebhookMessage, raw_body: bytes, signature: str | None) -> None:
    try:
        service.handle_webhook(payload, raw_body, signature)
    except Exception:
        logging.exception("Background webhook processing failed")


@app.post("/webhook")
async def webhook(request: Request, background_tasks: BackgroundTasks) -> dict:
    raw_body = await request.body()
    try:
        payload = WebhookMessage.model_validate_json(raw_body)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid webhook payload: {exc}") from exc

    signature = request.headers.get("Pachca-Signature")

    try:
        service._verify_signature(payload, raw_body, signature)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc

    background_tasks.add_task(_process_webhook, payload, raw_body, signature)
    return {"status": "accepted"}
