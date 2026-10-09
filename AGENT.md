# AGENT.md

Guidance for coding agents working in this repository. `CLAUDE.md` imports this file.

## Project

Silong is an offline evacuation-center hub. A computer at the center serves a local web app and runs all AI locally. Phones on the same Wi-Fi (no internet) submit Taglish reports by text or optional audio. Staff see a live triage board sorted by code-based urgency rules. Hackathon deadline: 2026-10-10, 10:00 AM Manila; code freezes then.

## Sources of truth

- `PRD.md`: the agreed product. It **supersedes** `IDEA.md` wherever they conflict (IndexedDB outbox rather than `localStorage`, nullable `in_center`/`on_behalf`, outside-center rescue rule first, one extraction over text + transcript, Windows/A54-only verification).
- `IDEA.md`: original idea; still the reference for the `reports` schema, API table, and extraction schema.
- `ROADMAP.md`: phase list. Its `Current:` line links to the active phase plan under `.kaylo/phases/`.
- `GITFLOW.md`: branching, commit, push and PR rules. `RELEASING.md`: guided release steps.
- `.kaylo/`: Kaylo planning state (plans, preferences, planning history). Update it through the Kaylo skills, not by hand.

## Current status

No application code exists yet. Phase 01 (durable text intake) is planned, with tasks T1–T5 not started. Implementation is blocked until the user confirms the phase summary. A plan or review does not authorize building.

## Planned layout and commands

These come from the Phase 01 plan and do not exist until T1/T3 create them:

- Package `silong/`: `config.py`, `db.py`, `app.py` (FastAPI app factory, lifespan DB init), `__main__.py`, and `static/` (`index.html`, `board.html`, `report.js`, `board.js`, `style.css`).
- Tests in `tests/`, using standard-library `unittest`, FastAPI `TestClient` and temporary SQLite databases.
- Setup: `python3 -m venv .venv && . .venv/bin/activate && python -m pip install -r requirements.txt`
- Run: `python -m silong` (binds `0.0.0.0`, one process, no reload)
- Gate: `python -m unittest discover -s tests -v`. It must pass, with tests actually discovered, before any implementation commit.

On this Linux host only `python3` is on PATH. Run `python` commands inside the activated virtual environment.

## Non-negotiables

- Stack: Python 3.11+, FastAPI/uvicorn, stdlib `sqlite3`, `httpx` to local Ollama, optional `faster-whisper` (CPU int8), `qrcode`, plain HTML and vanilla JS with one shared CSS file. No frontend build step, CDN assets, service workers, auth, HTTPS, or cloud inference.
- One app process with a worker thread. The SQLite `reports` table is also the job queue.
- Persist the raw report before acknowledging receipt. Never block receipt on AI. A repeated `client_id` returns the original report and creates no duplicate.
- Urgency is computed by code (first match wins, with a plain-language reason), not by the model. Queued/failed reports are never shown as Low.
- Ollama calls: `keep_alive: -1`, `temperature: 0`, `think: false`, `format` set to the JSON schema, fixed prompt prefix with report text last.
- If Whisper is unavailable, the server keeps running and the form hides audio.
- Every displayed timing is measured. Never fabricate benchmarks or claim platform checks (Windows 11 Ryzen hub, Galaxy A54) that weren't run.
- Config via `SILONG_MODEL`, `SILONG_WHISPER` (`small`/`base`/`off`), `SILONG_PORT` (8000), `SILONG_DB` (`silong.db`).
- Render report content with `textContent` or DOM creation, never `innerHTML`.

## Data and privacy

Use only synthetic reports in tests, fixtures, examples and evidence. Never commit SQLite databases (including `-journal`/`-wal`/`-shm`), uploaded audio, or real evacuee information.

## Git

Follow `GITFLOW.md`. In short:

- Work on `phase/NN-slug` branches, never directly on `main`.
- Make one commit per verified task: `type(scope): summary (NN-TN)`, using only the listed scopes. Ask before adding a scope.
- Stage files by name. Never use `git add -A` or `git add .`. The user's untracked documents (`IDEA.md`, `PRD.md`, `GITFLOW.md`, `RELEASING.md`, `CLAUDE.md`, this file) stay unstaged unless the user assigns them to a commit.
- Ask first before merging to `main`, tagging, releasing, force-pushing, rewriting pushed history, deleting branches, or opening issues.
