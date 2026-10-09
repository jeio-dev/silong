# Silong: Rough Idea

Working name. "Silong" means shelter. Rename freely.

## One-liner

An offline evacuation-center hub: a computer at the evac center runs local AI, phones connect to it over Wi-Fi with no internet, people report their situation in Taglish by text or voice, and staff get a live triage board that puts the most urgent cases on top.

## Problem

During typhoons and floods, evac centers lose mobile data first. Barangay staff register evacuees on paper and retype the lists later, so seniors, PWDs, infants, and the injured are found by whoever happens to walk past them. Free-text reports ("lola ko sa Room 3, hindi makalakad, wala nang gatas ang baby") are useful to a human but impossible to sort when there are 200 of them.

## Why local AI (the submission answer)

- **Offline:** in the scenario there is no internet, so a cloud AI API is not slower, it is unavailable. Every AI step runs on the hub.
- **Privacy:** reports contain names, health conditions, and the locations of vulnerable people. That data stays on one machine in the room.
- **Cost:** zero per report, so it works for a barangay with no budget.
- **Proof:** the demo kills the internet on camera and the product keeps working.

If the local AI is removed, the product is a pile of unsorted text. That is the point.

## Users

- **Reporter:** an evacuee or a barangay runner with a phone. Opens a web page, types or records a report, gets a reference number.
- **Staff:** barangay worker at the hub screen. Watches the triage board, corrects mistakes, marks cases resolved.

## Hackathon constraints

- Deadline: 10:00 AM, Oct 10 (Manila). Code freeze at deadline; repo must be public.
- Screening is by GitHub repo + demo video (X or LinkedIn, tag Devin / Cognition, #AppBuildersPH). Live pitch only for the top 10 to 15.
- Rules: substantially built during the hackathon, meaningful inference runs locally, all models/frameworks/tools disclosed in the README, no fake benchmarks.
- Commit small and often. The commit history is the proof of when it was built.

## Hardware

- **Hub (dev and demo):** Windows 11 PC, Ryzen 5 2400G, CPU-only inference.
- **Client:** Samsung Galaxy A54 browser.
- **Network:** A54 hotspot with mobile data off, or home router with the WAN/fiber cable unplugged.
- Must also run unchanged on any other machine with Python + Ollama (borrowed laptop, or Termux on Android as a last resort).

## Scope

### Must (the demo depends on these)

1. Phone report form: text box, optional audio upload, optional reporter name.
2. Offline outbox on the phone: reports are saved in the browser and retried until the hub confirms them.
3. Server stores the raw report immediately and returns a reference number. Never blocks on AI.
4. Background worker: optional transcription (Whisper), then extraction (LLM) into a fixed JSON schema.
5. Urgency computed by code rules from the extracted fields, with a human-readable reason.
6. Triage board: auto-refresh, sorted by urgency, vulnerable-person tags, original text visible, editable fields, resolve button, processing time per report.
7. Board shows a QR code of the hub's current URL and a status strip (models loaded, queue length, reports today).

### Stretch (only after Must is done and recorded)

- "Tanong" tab: local RAG Q&amp;A over a loaded evac / first-aid guide.
- Duplicate detection (same family reporting twice).
- CSV export of the roster.

### Out of scope

Auth, multi-hub sync, SMS, maps, mobile app packaging, HTTPS, service workers.

## Architecture

Single Python process. No build step, no framework on the frontend.

```
Phone browser ──(local Wi-Fi, HTTP)──> FastAPI server
                                          │
                                          ├─ SQLite (reports table is also the job queue)
                                          │
                                          └─ Worker thread
                                               ├─ faster-whisper (optional, audio → text)
                                               ├─ Ollama HTTP API (text → JSON fields)
                                               └─ rules.py (fields → urgency + reason)
Board browser (on the hub) ──polls──> FastAPI server

```

### Stack

- Python 3.11+, FastAPI, uvicorn
- Ollama (local LLM server), called over its HTTP API with `httpx`
- faster-whisper, int8 on CPU (optional import)
- SQLite via the standard library
- `qrcode` for the board QR
- Plain HTML + vanilla JS + one CSS file for both pages

### Config (environment variables)

- `SILONG_MODEL`: Ollama model tag (decided after the latency test; candidates `qwen3.5:4b`, Gemma 4 E2B/E4B, fallback `qwen3:4b`)
- `SILONG_WHISPER`: `small`, `base`, or `off`
- `SILONG_PORT`: default 8000
- `SILONG_DB`: default `silong.db`

### Non-negotiables

- Bind to `0.0.0.0`, not `127.0.0.1`.
- Ollama calls use `keep_alive: -1`, `temperature: 0`, `think: false`, and `format` set to the JSON schema.
- System prompt and few-shot examples are a fixed prefix; the report text always comes last (so Ollama reuses the cached prefix).
- If faster-whisper fails to import, the server still runs and the form hides the audio option.
- Raw report is saved before any AI runs. A failed AI step leaves the report visible on the board with status `failed` and a retry button, so staff can fill it in manually.
- Every timing shown on screen is measured, not estimated.

## Data model

One table, `reports`:


| Column          | Type         | Notes                                                        |
| --------------- | ------------ | ------------------------------------------------------------ |
| id              | integer PK   | Reference number shown to the reporter                       |
| client\_id      | text unique  | UUID generated on the phone; makes retries idempotent        |
| created\_at     | text         | ISO timestamp, set on receipt                                |
| reporter        | text null    | Optional name                                                |
| raw\_text       | text null    | Typed report                                                 |
| audio\_path     | text null    | Saved upload                                                 |
| transcript      | text null    | Whisper output                                               |
| status          | text         | `queued` → `transcribing` → `extracting` → `done` / `failed` |
| fields\_json    | text null    | Extracted fields (see schema)                                |
| urgency         | text null    | `critical` / `high` / `medium` / `low`                       |
| urgency\_reason | text null    | Which rule fired, in plain words                             |
| edited          | integer      | 1 if staff changed any field                                 |
| resolved        | integer      | 0 / 1                                                        |
| asr\_ms         | integer null | Measured transcription time                                  |
| llm\_ms         | integer null | Measured extraction time                                     |
| error           | text null    | Last failure message                                         |


The worker picks the oldest `queued` row, one at a time. Because the queue lives in the table, a restart loses nothing.

## Extraction schema

```json
{
  "location": "string or null",
  "headcount": "integer or null",
  "vulnerable": ["senior", "pwd", "infant", "child", "pregnant", "sick"],
  "injured": "boolean",
  "injury_note": "string or null",
  "needs": ["rescue", "medical", "medicine", "food", "water", "milk_formula", "hygiene", "other"],
  "summary": "string, English, max 20 words"
}

```

Prompt rules: extract only what the report states, use null or an empty list when not stated, never guess a headcount. Include 2 to 3 Taglish few-shot examples in the fixed prefix.

## Urgency rules (code, not the model)

Evaluated top to bottom; first match wins.

1. **Critical:** needs includes `rescue` or `medical`, or `injured` is true.
2. **High:** any vulnerable person, and needs includes `medicine`, `milk_formula`, `water`, or `food`.
3. **Medium:** any need at all.
4. **Low:** everything else.

Sort the board by urgency, then oldest first (longest waiting), then larger headcount. Store the reason string, e.g. "Critical: injury reported" or "High: infant + milk_formula". Staff edits re-run the rules.

## API


| Method | Path                      | Purpose                                                                                                                       |
| ------ | ------------------------- | ----------------------------------------------------------------------------------------------------------------------------- |
| GET    | `/`                       | Phone report form                                                                                                             |
| POST   | `/api/reports`            | Multipart: `client_id`, `text?`, `audio?`, `reporter?`. Returns `{id, status}`. Same `client_id` returns the existing report. |
| GET    | `/board`                  | Staff triage board                                                                                                            |
| GET    | `/api/reports`            | List for the board, already sorted                                                                                            |
| PATCH  | `/api/reports/{id}`       | Edit fields or set resolved; recomputes urgency                                                                               |
| POST   | `/api/reports/{id}/retry` | Re-queue a failed report                                                                                                      |
| GET    | `/api/status`             | Model name, Whisper on/off, queue length, hub URL                                                                             |
| GET    | `/api/qr.png`             | QR code of the hub URL (detects its own LAN IP at startup)                                                                    |


## Phone form behavior

- Audio via `<input type="file" accept="audio/*" capture>` (works over plain HTTP, unlike `getUserMedia`).
- On submit: save to a `localStorage` outbox with a fresh `client_id`, then try to send.
- Retry every 5 seconds while anything is pending. Show "2 reports waiting to send" and each confirmed reference number.
- All storage calls wrapped in try/catch. Tab must stay open; no service worker (needs HTTPS).
- Large tap targets, works one-handed, Taglish-friendly labels.

## Board behavior

- Polls `/api/reports` every 2 seconds.
- Card: urgency color + reason, location, headcount, vulnerable tags, needs, summary, original text or transcript (collapsible), status, `asr_ms` / `llm_ms`.
- Inline edit of any field; resolve button moves the card to a resolved section.
- Header: QR code, hub URL, status strip, counts per urgency.
- Readable from across a room.

## Build order

1. **Skeleton (target by 10 PM):** FastAPI app, SQLite table, form posts text, board lists raw reports. Verified from the A54 over the hotspot.
2. **AI loop (target by 1 AM):** worker, Ollama extraction with schema, rules, sorted board with reasons and timings. Then Whisper behind the optional flag.
3. **Demo polish (target by 3 AM):** offline outbox, QR + status strip, edit/resolve, failed/retry state.
4. **Morning (7 to 9:30 AM):** record video, README, fresh-clone test, make public, post.

## README must include

- One-paragraph "why local" answer, at the top.
- Disclosure: Ollama + model tag, faster-whisper + model size, FastAPI, SQLite, qrcode, AI-assisted development tools used.
- Setup steps a judge can follow on a fresh Windows/Mac/Linux machine, including `ollama pull`, firewall note (allow inbound on the port / set network to Private), and how to connect a phone.
- Tested hardware and measured per-report times.
- Limitations: Wi-Fi range (one hall per access point; runners and the offline outbox cover the rest), Whisper accuracy on heavy Taglish, single hub only.
- Deployment note: field kit is a pre-configured travel router + any laptop; the demo uses a phone hotspot.

## Open decisions

- Model tag: pick after the 5-report latency and accuracy test.
- Whisper size: `small` if a 15-second clip transcribes in under ~6 seconds on the 2400G, else `base`.