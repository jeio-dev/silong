"""FastAPI application: report receipt, the ordered board list and staff
corrections for the local hub.

Receipt never waits for, or contacts, any model. A report is acknowledged
only after it is committed to SQLite.
"""

import json
import logging
import re
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Callable, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from silong import db, rules
from silong.config import Config, load_config

logger = logging.getLogger(__name__)

UUID_PATTERN = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE
)
STORAGE_ERROR = "The hub could not save or read reports right now. Please try again."
STATIC_DIR = Path(__file__).resolve().parent / "static"
NOT_FOUND = "No report has that id."
PATCH_KEYS = ("fields", "resolved")
# Report ids are positive and at most SQLite's largest integer; others cannot name a report.
MAX_ID = 2**63 - 1


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


def _parse_patch(body) -> tuple[Optional[dict], Optional[bool]]:
    """Return (validated fields or None, resolved or None) from a PATCH body.

    Raises a 422 with a one-line `detail` naming the problem.
    """
    if not isinstance(body, dict) or not body:
        raise HTTPException(422, "Send a JSON object with fields and/or resolved.")
    unknown = sorted(str(key) for key in body if key not in PATCH_KEYS)
    if unknown:
        raise HTTPException(422, f"Unknown key(s): {', '.join(unknown)}; send fields and/or resolved.")
    resolved = body.get("resolved")
    if "resolved" in body and not isinstance(resolved, bool):
        raise HTTPException(422, "resolved must be true or false.")
    fields = None
    if "fields" in body:
        try:
            fields = rules.validate_fields(body["fields"])
        except ValueError as error:
            raise HTTPException(422, f"Invalid fields: {error}.")
    return fields, resolved


def _start_worker(
    config: Config,
    extract: Optional[Callable[[str], dict]],
    warm_up: Optional[Callable[[], None]],
):
    """Start the extraction worker. Imported here so a hub without a model never
    loads the worker, the Ollama client or httpx."""
    from silong.worker import Worker

    if extract is None:
        from silong import extract as extraction

        model = config.model

        def extract(text: str) -> dict:
            return extraction.extract(text, model)

        if warm_up is None:

            def warm_up() -> None:
                extraction.warm_up(model)

    worker = Worker(config.db_path, extract, warm_up)
    worker.start()
    return worker


def create_app(
    config: Optional[Config] = None,
    *,
    extract: Optional[Callable[[str], dict]] = None,
    warm_up: Optional[Callable[[], None]] = None,
) -> FastAPI:
    """Build the app. `config` defaults to the SILONG_* environment.

    When `config.model` is set, a worker thread extracts queued reports with
    `extract(text) -> fields` (default: Ollama with that model). `warm_up` runs
    once when the worker starts; it defaults to loading the Ollama model, or to
    nothing when `extract` is injected. With no model, reports stay queued.
    """
    if config is None:
        config = load_config()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db.init_db(config.db_path)
        requeued = db.recover_interrupted(config.db_path)
        if requeued:
            logger.info("Requeued %d report(s) interrupted by the last stop", requeued)
        app.state.worker = _start_worker(config, extract, warm_up) if config.model else None
        try:
            yield
        finally:
            if app.state.worker is not None:
                await run_in_threadpool(app.state.worker.stop)

    app = FastAPI(title="Silong", lifespan=lifespan)
    app.state.config = config
    app.state.worker = None

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
        if app.state.worker is not None:
            app.state.worker.wake()
        return {"id": row["id"], "status": row["status"]}

    @app.get("/api/reports")
    async def list_reports() -> list[dict]:
        try:
            rows = await run_in_threadpool(db.list_reports, config.db_path)
        except sqlite3.Error:
            logger.exception("Could not list reports")
            raise HTTPException(503, STORAGE_ERROR)
        return [_report_view(row) for row in rules.order_reports(rows)]

    @app.patch("/api/reports/{report_id}")
    async def update_report(report_id: int, request: Request) -> dict:
        """Staff correction: `fields` (all nine, urgency recomputed; also completes an
        unprocessed or failed report) and/or `resolved` (resolve or reopen)."""
        try:
            body = await request.json()
        except ValueError:
            body = None
        fields, resolved = _parse_patch(body)
        if not 1 <= report_id <= MAX_ID:
            raise HTTPException(404, NOT_FOUND)
        try:
            if fields is not None:
                urgency, reason = rules.classify(fields)
                row = await run_in_threadpool(
                    db.update_fields, config.db_path, report_id, fields, urgency, reason, resolved
                )
            else:
                row = await run_in_threadpool(db.set_resolved, config.db_path, report_id, resolved)
        except sqlite3.Error:
            logger.exception("Could not update report %s", report_id)
            raise HTTPException(503, STORAGE_ERROR)
        if row is None:
            raise HTTPException(404, NOT_FOUND)
        return _report_view(row)

    @app.post("/api/reports/{report_id}/retry")
    async def retry_report(report_id: int) -> dict:
        """Requeue a failed report for extraction; any other state is a 409."""
        if not 1 <= report_id <= MAX_ID:
            raise HTTPException(404, NOT_FOUND)
        try:
            row, retried = await run_in_threadpool(db.retry_failed, config.db_path, report_id)
        except sqlite3.Error:
            logger.exception("Could not retry report %s", report_id)
            raise HTTPException(503, STORAGE_ERROR)
        if row is None:
            raise HTTPException(404, NOT_FOUND)
        if not retried:
            raise HTTPException(409, f"Only a failed report can be retried; this one is {row['status']}.")
        if app.state.worker is not None:
            app.state.worker.wake()
        return _report_view(row)

    @app.get("/", include_in_schema=False)
    async def report_page() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/board", include_in_schema=False)
    async def board_page() -> FileResponse:
        return FileResponse(STATIC_DIR / "board.html")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app
