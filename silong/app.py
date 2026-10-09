"""FastAPI application: report receipt and listing for the local hub.

Receipt never waits for, or contacts, any model. A report is acknowledged
only after it is committed to SQLite.
"""

import json
import logging
import re
import sqlite3
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from silong import db
from silong.config import Config, load_config

logger = logging.getLogger(__name__)

UUID_PATTERN = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE
)
STORAGE_ERROR = "The hub could not save or read reports right now. Please try again."


def _report_view(row: dict) -> dict:
    """A stored row as the board sees it; server-side paths are not exposed."""
    view = {key: value for key, value in row.items() if key not in ("fields_json", "audio_path")}
    view["fields"] = json.loads(row["fields_json"]) if row["fields_json"] else None
    view["edited"] = bool(row["edited"])
    view["resolved"] = bool(row["resolved"])
    return view


def _form_text(form, name: str) -> Optional[str]:
    value = form.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise HTTPException(422, f"{name} must be a text field, not a file.")
    return value


def create_app(config: Optional[Config] = None) -> FastAPI:
    """Build the app. `config` defaults to the SILONG_* environment."""
    if config is None:
        config = load_config()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db.init_db(config.db_path)
        yield

    app = FastAPI(title="Silong", lifespan=lifespan)
    app.state.config = config

    @app.post("/api/reports")
    async def create_report(request: Request) -> dict:
        async with request.form() as form:
            if "audio" in form:
                raise HTTPException(422, "Audio reports are not supported yet; send text.")
            client_id = _form_text(form, "client_id")
            text = _form_text(form, "text")
            reporter = _form_text(form, "reporter")

        if client_id is None or not UUID_PATTERN.fullmatch(client_id):
            raise HTTPException(422, "client_id must be a UUID string.")
        if text is None or not text.strip():
            raise HTTPException(422, "Report text is required.")
        reporter = (reporter or "").strip() or None

        try:
            row, _created = await run_in_threadpool(
                db.insert_report, config.db_path, client_id.lower(), text, reporter
            )
        except sqlite3.Error:
            logger.exception("Could not store report")
            raise HTTPException(503, STORAGE_ERROR)
        return {"id": row["id"], "status": row["status"]}

    @app.get("/api/reports")
    async def list_reports() -> list[dict]:
        try:
            rows = await run_in_threadpool(db.list_reports, config.db_path)
        except sqlite3.Error:
            logger.exception("Could not list reports")
            raise HTTPException(503, STORAGE_ERROR)
        return [_report_view(row) for row in rows]

    return app
