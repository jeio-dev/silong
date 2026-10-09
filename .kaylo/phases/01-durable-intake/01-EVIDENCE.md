# 01 Durable intake: verification evidence

Synthetic data only. Every database used below was a temporary file outside the repository and was deleted afterwards. Full per-task records, including failed attempts and review history, are in each task's `Result` in `01-PLAN.md`; this file summarizes them for the phase PR and records the T5 end-to-end run.

## Checked host

- Ubuntu 26.04.1 LTS under WSL2, Linux 6.18.33.2-microsoft-standard-WSL2, x86_64.
- System Python 3.14.4 without `python3-venv`; virtual environments created with uv 0.12.19 (`uv venv --seed`), CPython 3.14.7, pip 26.2.1.
- Installed from `requirements.txt`: fastapi 0.143.0, starlette 1.7.0 (transitive), uvicorn 0.54.0, httpx 0.28.1, python-multipart 0.0.32.
- Browser: Chromium 156.0.8078.4 (Playwright's Chrome for Testing build), headed on the WSLg display, driven over the DevTools protocol by a scratch Node 24.21.0 script. Phone checks used 412×915 mobile emulation with touch input.
- Ollama was not running during any check (`curl localhost:11434` exit 7). faster-whisper and qrcode were not installed.

Not checked: Windows 11 or the Ryzen hub, the Galaxy A54 or any real phone, a second device over real Wi-Fi, Python 3.11–3.13, separate-process write concurrency, and power loss. No performance figures are claimed. The one timing below is a single measurement on this host.

## Per-task summary

| Task | Commit | Main evidence |
| --- | --- | --- |
| T1 runtime, config, gate | `bbe47b3` | 13 config tests; fresh install reproduced the four pins; ignore rules cover `.db`/`.sqlite`/`.sqlite3` and their side files (R2 fixed and rechecked). |
| T2 durable dedup storage | `a1818b1` | 15 DB tests on file-backed temporary DBs: 12-thread same-UUID insert gives one row, reopen keeps rows, write failures raise with zero rows; a mutation removing `ON CONFLICT` failed the suite. |
| T3 receipt/list API, launcher | `7ae3b92` | 25 API tests with outbound sockets blocked; 422/503 paths insert nothing; launcher binds `0.0.0.0` in one process even with `WEB_CONCURRENCY=2` (R3 fixed and rechecked). |
| T4 form and board pages | `c2f653a` | 7 page tests; browser run 28/28 (reference only after storage, markup shown as text, stale board view, same-UUID retry after non-delivery and lost acknowledgement, local-only requests, 44 px+ targets); reviewer rerun 28/28. |
| T5 README, restart proof | uncommitted | See below. |

## T5 end-to-end run, 2026-10-09

**Fresh install following README.** `python3 -m venv <scratch>/venv-plain` failed (exit 1: ensurepip unavailable, `python3.14-venv` not installed), as the README warns. The documented fallback `uv venv --seed <scratch>/venv`, then `python -m pip install -r requirements.txt`, installed exactly the pinned versions (exit 0). `pip check` found no broken requirements, and `import fastapi, uvicorn, httpx, python_multipart` succeeded. With that environment, `python -m unittest discover -s tests -v` ran 62 tests, all passing.

**Automated restart test.** New `tests/test_api.py` `RestartIntegrationTest` starts the real `python -m silong` on a temporary DB and free port. It submits a synthetic report, loads `/board`, and stops the hub. It then restarts the hub on the same DB and repeats the UUID with different text. It expects the same `{"id", "status": "queued"}`, one row, and an unchanged stored row, including `created_at`. It passed in the full gate and in 5 separate reruns. Mutation check: making `init_db` drop the table on startup failed the test; `silong/db.py` was restored (`git diff` empty).

**Browser run (11/11 checks passed).** The hub was started from the fresh environment with `SILONG_PORT=8771`, a scratch `SILONG_DB`, `SILONG_MODEL=qwen3:4b` and `SILONG_WHISPER=small` (neither loaded), with Ollama absent. The browser used the hub's WSL address `http://172.23.56.13:8771/`, a non-secure HTTP context.

1. In the phone-size form, a synthetic Taglish report with the name "Ana (synthetic)" was sent by tap. The form showed Reference #1 only after the API listed that row with the exact text. The board card appeared 1835 ms after sending, labelled "Queued: waiting for triage, no urgency assigned".
2. With the hub stopped, the board kept card #1 and showed its stale warning. After a restart on the same DB, the board reconnected. The API held the one report unchanged, with the same `created_at`.
3. A multipart POST repeating report #1's UUID with different text returned 200 `{"id":1,"status":"queued"}`. One row remained, with the original text.
4. A `window.fetch` wrapper let the next POST reach the hub but threw before the form saw the response. The form showed "Not confirmed" while the hub had stored row #3. The wrapper was removed, and the hub was stopped and restarted. The form's Try again then returned Reference #3, with that UUID stored once and two rows in total.
5. The board listed exactly two cards (#1 and #3), with no "Low" anywhere on the page. Every browser request went to the hub address. There were no page exceptions or dialogs. The board and form screenshots were inspected visually.

Reference #2 is absent because the repeated-UUID insert in step 3 consumed an AUTOINCREMENT value. This T2 behavior is already recorded under T4: references stay distinct and are never reused, but they can have gaps.

Script issues during this run, not implementation repairs: the first browser run stopped because the copied helper code lacked `referenceOf`. In the new restart test, a first assertion expected exit code 0, but uvicorn 0.54 re-raises SIGTERM after a graceful shutdown (exit −15). The test now asserts the logged "Application shutdown complete".

The check script and screenshots stayed in the session scratchpad. They are not committed because they depend on host-specific browser and library paths.
