from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import FastAPI, HTTPException, Request

from app.config import load_settings
from app.models import WebhookMessage
from app.service import AgentService

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

settings = load_settings()
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


@app.post("/webhook")
async def webhook(request: Request) -> dict:
    raw_body = await request.body()
    try:
        payload = WebhookMessage.model_validate_json(raw_body)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid webhook payload: {exc}") from exc

    signature = request.headers.get("Pachca-Signature")

    try:
        return service.handle_webhook(payload, raw_body, signature)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except Exception as exc:  # pragma: no cover
        logging.exception("Unhandled webhook error")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
