# Silong

**An offline report and triage hub for evacuation centers.** One computer at the center serves a local web app and runs all AI locally. Phones on the same Wi-Fi send Taglish reports with no internet, and staff see a live board sorted by urgency.

*Silong* is Tagalog for shelter, the space under the eaves where people take cover from rain.

| | |
| --- | --- |
| **Project name** | Silong |
| **Hackathon** | #AppBuildersPH (Devin / Cognition) |
| **Demo video** | https://youtu.be/xeNiQS--CiI (see [Demo video](#demo-video) for what it shows) |
| **Built** | 2026-10-09 to 2026-10-10. The git history starts at the first commit on 2026-10-09. |
| **Runs on** | One hub computer (Python + Ollama) and any phone browser on the same Wi-Fi |
| **Internet needed** | Only once, to install Python packages, Ollama and the model. Never while it runs. |

## Problem

When a typhoon or flood hits, evacuation centers often lose mobile data and internet just as reports pile up: a family trapped on a roof outside the center, a baby with no milk formula, someone with a wound. These reports arrive by word of mouth, on paper or as text messages, in mixed Tagalog and English (Taglish). Staff have to read them all and decide by hand who needs help first.

Cloud AI could sort these reports, but it is unreachable when the internet is down. Evacuees' personal details would also leave the center. A tool for this setting has to keep working with only local Wi-Fi, run on an ordinary laptop, and never lose a report even when the phone's connection drops.

## Description

Silong turns one laptop into a local hub:

1. **Reporters** (evacuees or barangay runners) scan a QR code on the staff screen and open a simple form on their phone. They type what is happening in Taglish, with an optional name.
2. **Every report is saved on the phone first** (in the browser's IndexedDB), then sent. If the Wi-Fi drops, the phone shows the report as *Waiting to send* and retries every five seconds while the page is open. Resending never creates a duplicate, and the hub confirms each report with a reference number.
3. **The hub stores the original report before doing anything else**, so receipt never waits for the AI.
4. **A local language model** (through Ollama, on the hub's CPU) pulls out the details: where the people are, whether they are inside the center, whether the reporter is speaking for others, how many people, vulnerable people (infant, pregnant, elderly, disabled), injuries, needs, and a short English summary.
5. **Fixed rules in code, not the model, set the urgency.** The first rule that matches wins: **Critical** for a rescue needed outside the center, then for any rescue, medical need or injury. **High** for a vulnerable person who also needs medicine, milk formula, water or food. **Medium** for any other need, and **Low** when no need is stated. Every card shows the plain-language reason.
6. **Staff see a live board** that refreshes every two seconds. Untriaged and failed reports are always visible and never labelled Low. Triaged reports are sorted most urgent first. Staff can correct any field (the urgency is recomputed), complete a report by hand, retry a failed one, and mark reports resolved.

The model never decides who goes first. It only extracts fields, and staff can see and correct every one of them.

### What is built

| Area | Status |
| --- | --- |
| Text reports with phone outbox (IndexedDB), automatic retries, duplicate-safe references | Built and tested |
| Local AI extraction with Ollama, code-based urgency rules, sorting, staff edits, resolve, retry, restart recovery | Built and tested |
| Staff board with a status header showing the phone URL, a local QR code, model state, queue and urgency counts | Built and tested |
| Audio reports with local Whisper transcription | **Not built.** It was planned (`SILONG_WHISPER` is accepted but has no effect) and was dropped for the deadline. The form is text only. |
| Verification on the target demo hardware (hub directly on Windows 11, Galaxy A54 over Wi-Fi with internet off) | **Not done.** See [Tested platforms](#tested-platforms). |

## Demo video

Watch it on YouTube: https://youtu.be/xeNiQS--CiI

The video is an **animated recreation made with Remotion, not a screen recording of the running app**. Its phone form and staff board are mockups rebuilt from this repository's HTML and CSS, with the same text, colours and layout. The report texts are the synthetic fixtures in `tests/fixtures/five_reports.json`. The fields, urgencies and reasons on the mock board are the values `qwen3:4b` returned for those texts in the measured five-report test (see [Models](#models)), with urgency computed by `silong/rules.py`. The mock board header names `qwen3:4b`, while the hub's recommended model is `gemma4:e2b`. The voiceover was generated with ElevenLabs. The video's source is not part of this repository.

## Models

All inference runs on the hub through [Ollama](https://ollama.com). Nothing is sent to a cloud service.

| Model | Role | Details (from Ollama `/api/tags`) | Measured on the test hub |
| --- | --- | --- | --- |
| `gemma4:e2b` (Google Gemma 4 E2B) | **Chosen** extraction model; set with `SILONG_MODEL=gemma4:e2b` | digest `b37049369adf`, 4,587,156,505 bytes (about 4.6 GB), Q4_K_M | Priority 5/5, fields 34/40; cold load 24.0 s; extraction 7.5–9.7 s once warm, 33.9 s for the first report |
| `qwen3:4b` (Alibaba Qwen3 4B) | Compared, not chosen (more accurate, about twice as slow) | digest `359d7dd4bcda`, 2,497,293,931 bytes, Q4_K_M | Priority 5/5, fields 37/40; cold load 13.7 s; extraction 16.0–19.3 s once warm, 63.9 s for the first report |
| Whisper (faster-whisper) | Planned for audio; **not used** | — | — |

The model is called with `temperature: 0`, `think: false`, `keep_alive: -1` (stays loaded) and a JSON schema as the output `format`. The prompt has a fixed prefix with the report text last. Full results, including every expected and actual field, are in [Five-report model test](#five-report-model-test). Check each model's page on ollama.com for its license.

## Tools

**Runtime stack** (pinned in `requirements.txt`):

- Python 3.11+ (tested with CPython 3.14.7)
- [FastAPI](https://fastapi.tiangolo.com) 0.143.0 and [uvicorn](https://www.uvicorn.org) 0.54.0: web server
- [httpx](https://www.python-httpx.org) 0.28.1: client for the local Ollama API
- python-multipart 0.0.32: form parsing
- [qrcode](https://github.com/lincolnloop/python-qrcode) 8.2: QR code drawn on the hub, no internet needed
- SQLite through the Python standard library: storage and job queue
- [Ollama](https://ollama.com) 0.40.2: local model runtime
- Plain HTML, vanilla JavaScript and one CSS file. No frontend framework, build step, CDN or service worker.

**Testing:** Python `unittest` with FastAPI `TestClient`. The browser checks use Node with [Playwright](https://playwright.dev) driving headless Chromium.

**AI-assisted development tools** (disclosed per the hackathon rules):

- [Claude Code](https://claude.com/claude-code) (Anthropic), including a plan/build/review workflow kept in `.kaylo/`
- Codex / ChatGPT (OpenAI)
- [OpenCode](https://opencode.ai)
- [ElevenLabs](https://elevenlabs.io): voiceover for the demo video only

**Demo video:** [Remotion](https://www.remotion.dev) (React video framework). Its music, rain and sound effects were synthesized by a script, with no stock audio.

## Assets

| Asset | Source | License |
| --- | --- | --- |
| Inter font, weights 400/600/700/800 (`silong/static/fonts/`) | [Inter](https://rsms.me/inter/) by Rasmus Andersson, bundled so the app needs no internet | SIL Open Font License 1.1 (`silong/static/fonts/Inter-LICENSE.txt`) |
| Silong icon (`silong/static/silong-icon.svg`, inline logo) | Made for this project | Part of this project |
| QR code on the board | Generated on the hub at runtime with `qrcode` | — |
| Taglish reports in tests, fixtures and the video | Synthetic, written for this project. No real people, places or evacuee data. | Part of this project |
| Demo video voiceover | Generated with ElevenLabs | ElevenLabs terms |
| Demo video music and sound effects | Synthesized by script for the video | Part of this project |

No databases, uploaded recordings or real reports are committed; `.gitignore` excludes them.

## Why local inference

During typhoons and floods, evacuation centers can lose internet access exactly when staff most need to find injured people, vulnerable evacuees and unmet needs among many free-text reports. A cloud model is unreachable then, and evacuees' reports would otherwise leave the center. Silong therefore runs report extraction on the hub computer itself, so triage keeps working with only local Wi-Fi and report contents stay on the hub and the reporters' phones.

## Setup

You need:

- **Python 3.11 or newer**
- **Ollama** and the `gemma4:e2b` model (about 4.6 GB download)
- A Wi-Fi network that the hub and the phones can join, such as a travel router, a home router or a phone hotspot. It does not need internet access.

Do every download **while the hub still has internet**. Nothing is downloaded at run time. Run all commands from the repository root.

> Only Linux (Ubuntu under WSL2) has been tested; see [Tested platforms](#tested-platforms). The Windows and macOS steps below follow the same commands but have **not** been run on those systems.

### Windows 10/11 (PowerShell)

1. Install Python 3.11+ from [python.org](https://www.python.org/downloads/windows/) (tick *Add python.exe to PATH*) or with `winget install Python.Python.3.12`.
2. Install Ollama and download the model:

   ```powershell
   winget install Ollama.Ollama
   ollama pull gemma4:e2b
   ```

   Ollama starts in the background after installing; if `ollama pull` cannot connect, start Ollama from the Start menu.
3. Create the virtual environment and install the packages:

   ```powershell
   py -3 -m venv .venv
   .venv\Scripts\Activate.ps1
   python -m pip install -r requirements.txt
   ```

   If PowerShell refuses to run `Activate.ps1`, run `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` first (this lasts for the current window only).
4. Start the hub:

   ```powershell
   $env:SILONG_MODEL = "gemma4:e2b"
   python -m silong
   ```

5. Allow phones through the firewall. Set the Wi-Fi network's profile to **Private** (Settings > Network & internet > Wi-Fi > the network > Network profile type). When Windows Defender Firewall asks on first start, allow Python on private networks. If no prompt appears, add the rule yourself in an administrator PowerShell:

   ```powershell
   New-NetFirewallRule -DisplayName "Silong hub" -Direction Inbound -Protocol TCP -LocalPort 8000 -Action Allow -Profile Private
   ```

6. Find the hub's address with `ipconfig` (the **IPv4 Address** of the Wi-Fi adapter, for example `192.168.1.20`) and open `http://192.168.1.20:8000/board` on the hub. Phones scan the QR code shown there.

Leave `OLLAMA_HOST` unset when Ollama runs on the same computer. Run the hub directly on Windows, not inside WSL: phones cannot reach a hub inside WSL without extra port forwarding.

### macOS (Terminal)

1. Install Python 3.11+ from [python.org](https://www.python.org/downloads/macos/) or with [Homebrew](https://brew.sh): `brew install python@3.12`.
2. Install Ollama from [ollama.com/download](https://ollama.com/download) (or `brew install ollama`), start it, and download the model:

   ```sh
   ollama pull gemma4:e2b
   ```

3. Create the virtual environment and install the packages:

   ```sh
   python3 -m venv .venv
   . .venv/bin/activate
   python -m pip install -r requirements.txt
   ```

4. Start the hub:

   ```sh
   SILONG_MODEL=gemma4:e2b python -m silong
   ```

   If macOS asks whether Python may accept incoming network connections, click **Allow**.
5. Find the hub's address with `ipconfig getifaddr en0` (Wi-Fi is usually `en0`), then open `http://<address>:8000/board` on the hub. Phones scan the QR code shown there.

### Linux (tested on Ubuntu under WSL2)

1. Install Python 3.11+ and the venv module from your distribution (Debian/Ubuntu: `sudo apt install python3 python3-venv`).
2. Install Ollama and download the model:

   ```sh
   curl -fsSL https://ollama.com/install.sh | sh
   ollama pull gemma4:e2b
   ```

3. Create the virtual environment and install the packages:

   ```sh
   python3 -m venv .venv
   . .venv/bin/activate
   python -m pip install -r requirements.txt
   ```

   If `python3 -m venv` fails because `python3-venv` is missing and you cannot install it, use [uv](https://docs.astral.sh/uv/) instead: `uv venv --seed .venv`, then activate and install as above. (This is how the tested host was set up.)
4. Start the hub:

   ```sh
   SILONG_MODEL=gemma4:e2b python -m silong
   ```

5. If a firewall is active, allow the port (for example `sudo ufw allow 8000/tcp`). Find the hub's address with `hostname -I` or `ip -4 addr` and open `http://<address>:8000/board` on the hub. Phones scan the QR code shown there.

### Field kit

For a real center, prepare a laptop with everything above installed and the model downloaded, plus a battery-powered travel router. The router gives the hub and the phones a shared Wi-Fi network without any internet connection. A phone hotspot with mobile data turned off works the same way for a demonstration.

## Start the hub

With the virtual environment activated:

```sh
python -m silong
```

The hub runs as one process, listens on every network interface (`0.0.0.0`) on port 8000, and creates `silong.db` in the current directory if it does not exist. Existing reports in that file are kept. Stop it with Ctrl+C. To use another port or database file, set the variables below first, for example:

```sh
SILONG_PORT=8080 SILONG_DB=/path/to/reports.db python -m silong
```

On Windows PowerShell: `$env:SILONG_PORT = "8080"`, then `python -m silong`.

Do not commit the database file or its `-journal`, `-wal` and `-shm` side files; they hold reports. `.gitignore` excludes them.

## Configuration

Set these environment variables before starting the hub. Blank or absent values use the defaults.

| Variable | Values | Default |
| --- | --- | --- |
| `SILONG_MODEL` | Ollama model tag, for example `gemma4:e2b` | none (no AI triage; reports stay queued) |
| `SILONG_WHISPER` | `small`, `base`, `off` | `off` (accepted but unused: audio is not built) |
| `SILONG_PORT` | 1–65535 | `8000` |
| `SILONG_DB` | SQLite file path | `silong.db` |
| `OLLAMA_HOST` | Ollama's address: `host`, `host:port` or a full `http://` URL | `http://127.0.0.1:11434` |
| `SILONG_HUB_URL` | The address phones should use, for example `http://192.168.1.20:8000` | none (the board's own address, see [Connect phones](#connect-phones)) |

An invalid `SILONG_PORT`, `SILONG_WHISPER` or `SILONG_HUB_URL` is rejected at startup with a message naming the variable. `SILONG_HUB_URL` must be plain `http://` with a numeric IPv4 address that is not `127.x` or `0.0.0.0`, an optional port and no path; hostnames, `localhost` and `https://` are refused. Without a port it means port 80, so include `:8000` (or your `SILONG_PORT`). The hub only checks the format: it cannot tell whether phones can actually reach that address. `OLLAMA_HOST` is Ollama's own variable; the hub reads it to find Ollama. A bare host gets `http://` and port 11434, and a listen-all value such as `0.0.0.0` (which tells the Ollama server to accept connections from the network) is treated as `127.0.0.1`. An unusable `OLLAMA_HOST` does not stop the hub; each report's triage fails with a message saying so.

## Local AI triage

How the worker behaves:

- It runs as one thread inside the hub process and handles one report at a time, oldest first. On start it sends one request to load the model and keep it loaded; a failure there is only logged.
- Receipt never waits for it: the phone gets its reference as soon as the report is saved, even if Ollama is slow, stopped or missing.
- If Ollama cannot be reached, replies with an error, or returns something that does not fit the report fields, the report is marked "Triage failed" with a short message on the board. Staff can press **Retry triage** or complete it by hand.
- Without `SILONG_MODEL`, no worker starts. Reports stay in "Not yet triaged" and staff can complete them by hand.
- If the hub stops while a report is being extracted, that report is queued again on the next start and extracted from the beginning. A report that staff already edited is never overwritten by a later model result.
- The urgency comes from fixed rules in code, first match wins: Critical when rescue is needed outside the center, then for rescue, medical need or injury; High for a vulnerable person together with medicine, milk formula, water or food; Medium for any other need; Low when no need is stated. Each card shows the rule that matched.

## Staff board

Open the board on the hub at `http://<hub Wi-Fi address>:8000/board` (see [Connect phones](#connect-phones)); `http://localhost:8000/board` also works but then shows no phone address unless `SILONG_HUB_URL` is set. The report list refreshes every two seconds and warns when it cannot reach the hub.

The status header at the top is refreshed every two seconds as well, separately from the report list, so a slow or failed status check never holds back new cards:

- **Phones report here**: the address phones should open, as a link and a QR code drawn on the hub (no internet needed). When no usable address is known (board opened through `localhost`, and no `SILONG_HUB_URL`), the link and QR are hidden and the header says how to fix it.
- **Model**: "Loaded in Ollama" or "Not loaded", with the time Ollama was last asked, or "Model unavailable" if Ollama did not give a usable answer within one second. This only says whether the model is in Ollama's memory; it does not mean extraction is correct or fast. "The triage worker is not running" appears separately if the worker thread has stopped. Without `SILONG_MODEL` it says no model is configured.
- **Audio**: "Text only: audio unavailable".
- **Queue**: *Queued* counts reports waiting for triage; *Processing* counts the one being extracted. Failed and staff-completed reports are not counted here.
- **Today**: every report received since midnight on the hub's clock (local date shown), including resolved and failed ones.
- **Unresolved by urgency**: Critical, High, Medium and Low counts of triaged reports that are not resolved.

All counts come from one read of the database. If a status check fails, the last values stay on screen with "Status may be out of date; the values shown are the last received." They are never replaced with zeros.

- **Not yet triaged** is shown first (on wide screens, as a lane to the left of **Triaged**; on narrow screens, above it), so new and failed reports stay visible. Cards here never show an urgency; their state (Queued, Extracting, Triage failed) is shown as a labelled pill. A card that arrives or moves to another section after the board loads is outlined briefly (no animation when the device asks for reduced motion).
- **Triaged** lists reports from most to least urgent; within an urgency, the oldest report comes first. Each card shows the urgency word and colour, the reason, the extracted fields, the summary, "Extraction took N.N s" (measured on the hub for that report), "Edited by staff" when staff changed it, and the original text under **Original report**.
- **Edit** (or **Complete by hand** for a report without fields) opens a form for all fields. Saving recomputes the urgency. If the report changes while the form is open, the form says so instead of being replaced.
- **Mark resolved** moves a report to the collapsed **Resolved** section; **Reopen** brings it back.

Extraction can be wrong. Staff should read the original text of every report and correct the fields when needed.

## Connect phones

1. Connect the hub computer and the phones to the same Wi-Fi network. Internet access is not needed.
2. Find the hub's address on that network:
   - Windows: run `ipconfig` and use the **IPv4 Address** of the Wi-Fi adapter (for example `192.168.1.20`).
   - macOS: run `ipconfig getifaddr en0`.
   - Linux: run `hostname -I` or `ip -4 addr` and use the address of the Wi-Fi interface.
3. On the hub, open the board at `http://<hub address>:8000/board` (use your `SILONG_PORT` if you changed it). The header then shows `http://<hub address>:8000/` and a matching QR code. The hub does not look up its own network adapters; it shows the address the board was opened with. If you prefer to keep the board on `localhost`, start the hub with `SILONG_HUB_URL=http://<hub address>:8000` instead (PowerShell: `$env:SILONG_HUB_URL = "http://192.168.1.20:8000"`).
4. On a phone, scan the QR code or type the address shown. Type `http://`; the hub does not use HTTPS.

Hotel or guest Wi-Fi with client isolation blocks phones from reaching the hub; use your own router or a hotspot.

### How the phone form delivers reports

- Each report is saved in the phone browser's storage (IndexedDB) first, then sent. "Waiting to send" lists saved reports the hub has not confirmed yet, with the reason for the last failure; "Received by the hub" lists reference numbers. Both lists survive a reload.
- While the page is open, the form tries again every five seconds, one report at a time, oldest first. A request that gets no answer is abandoned after 20 seconds, so one stuck request can delay the next attempts by up to that long. Try again now, shown while any report is waiting, starts an attempt at once. References are listed newest first with the time the hub confirmed them.
- Each report keeps the same ID on every attempt, so if the hub saved it but the answer was lost, the next attempt gets the original reference instead of a second copy. Two open tabs on the same phone may both send a report; the hub still stores it once.
- Saved reports belong to the exact address that was opened (for example `http://192.168.1.20:8000`). If the hub's address or port changes, the phone opens a new, empty list; reopen the old address to finish sending.
- Nothing is sent while the page is closed. A reload or first load without a connection to the hub cannot open the page at all (there is no offline copy of the page); once the same address loads again, saved reports resume.
- If the phone's own storage fails, the form says so, sends that report once immediately and, if that also fails, keeps its text on screen with "Not saved on this phone; not confirmed; no automatic retry" and a button to try again. It is not retried automatically.
- Reports cannot be edited or cancelled from the phone once submitted.

## Checks

```sh
python -m unittest discover -s tests -v
```

The suite uses temporary databases and synthetic reports only and never contacts Ollama: tests replace the model with fakes. It includes a check that starts the real hub, submits a report, restarts the hub on the same database and repeats the submission, expecting the same reference and one stored report.

Browser checks for the phone outbox and the board header run in headless Chromium through Playwright against a real hub on a temporary database, with a fake local Ollama where needed:

```sh
node tests/browser_outbox.cjs     # IndexedDB storage: saves, aborted writes, reloads, two tabs
node tests/browser_delivery.cjs   # phone form: offline, retries, lost answers, failures, layout
node tests/browser_header.cjs     # board header: URL/QR, model state, counts, stale status, card actions
```

They need Node and the Python virtual environment in `.venv`. Playwright and Chromium are not part of `requirements.txt`; point the scripts at existing copies with `PLAYWRIGHT_MODULE` (the `playwright` package directory), `CHROMIUM_EXECUTABLE` and, if Chromium needs extra libraries, `CHROMIUM_LIB_DIR`. The defaults are paths on the development machine.

To repeat the five-report model test against a running Ollama (synthetic Taglish reports in `tests/fixtures/five_reports.json`):

```sh
python -m silong.bench --model gemma4:e2b --out results.md
```

It unloads any loaded model first, records the environment and the cold-load time, then prints each report's expected and actual fields, priority and measured extraction time.

## Tested platforms

Checked so far:

- Hub: Ubuntu 26.04.1 LTS under WSL2 (Linux 6.18) with CPython 3.14.7 in a uv-created virtual environment (`uv venv --seed`; this host lacks `python3-venv`), using the pinned versions in `requirements.txt`, on an AMD Ryzen 5 2400G running Windows 11 Pro.
- Ollama: 0.40.2 running natively on Windows on the same machine, CPU only. Because the hub ran inside WSL, Ollama was set to listen on the network (`OLLAMA_HOST=0.0.0.0` on Windows) and the hub reached it at the WSL gateway address (`OLLAMA_HOST=http://<gateway>:11434`, from `ip route`).
- Browser: Chromium 156 on the same host, emulating a 412×915 phone screen, reaching the hub through the WSL network address over plain HTTP.
- End-to-end check (2026-10-09) with `SILONG_MODEL=gemma4:e2b`, model already loaded: a synthetic report submitted from the phone-sized form was triaged on the board as High ("infant + milk_formula") with measured extraction time 18.8 s. The hub was stopped while a second synthetic report was being extracted; after restart the report was queued again, extracted, and shown as Critical ("rescue needed outside center") with measured extraction time 10.4 s, above the first report.
- Offline delivery check (2026-10-09, phase 03): on the same host, the hub was opened at its WSL network address with a fake local Ollama that failed the first extraction and held the next one. A phone-sized page saved two synthetic reports while its browser was offline, kept them through 12 s of failed retries and a reload, and delivered both within about 5 s of reconnecting, one database row each. A lost answer was resent with the same ID and got the original reference. New and failed reports appeared on the board within 2 s of receipt while the status check was held, and the header counts matched the database. The QR served by the hub was identical to one generated for the address shown; no phone scanned it. Details: [`.kaylo/phases/03-offline-delivery/03-EVIDENCE.md`](.kaylo/phases/03-offline-delivery/03-EVIDENCE.md).

Not tested: the hub running directly on Windows 11 (including the firewall steps above), macOS, any Linux other than Ubuntu under WSL2, the Galaxy A54 or any real phone (including its browser storage and scanning the QR code), a phone on separate hardware over real Wi-Fi with internet disconnected, readability of the board on a projector or across a room, Python 3.11–3.13, and the plain `python3 -m venv` path. No performance figures beyond the measured times in this README are claimed.

### Five-report model test

Run on the hardware above with five synthetic Taglish reports, one run per model. Full results, including every expected and actual field, are in [`.kaylo/phases/02-local-triage/02-MODELS.md`](.kaylo/phases/02-local-triage/02-MODELS.md).

| Model | Priority matched | Fields matched | Cold load | Extraction time per report (measured) |
| --- | --- | --- | --- | --- |
| `gemma4:e2b` | 5/5 | 34/40 | 24.0 s | 33.9, 9.4, 9.7, 7.7, 7.5 s |
| `qwen3:4b` | 5/5 | 37/40 | 13.7 s | 63.9, 19.0, 19.3, 16.0, 17.2 s |

`gemma4:e2b` was chosen, with a target of at most 15 s of extraction time per report once the model is loaded. In that test it met the target on 4 of 5 reports; the first report after loading took 33.9 s. In the end-to-end check above, one report took 18.8 s and one 10.4 s. The target is therefore not met consistently, and these are single runs, not a benchmark.

## Limitations

- Text only. Audio reports and Whisper transcription were planned but not built.
- Extraction runs on the CPU and takes seconds to tens of seconds per report; reports wait in order while one is extracted.
- The model makes mistakes. In the test above both models missed a stated headcount and added a medical need the report did not state, `qwen3:4b` left one location empty, and `gemma4:e2b` got one "reporting for others" flag wrong and shortened or reworded locations. Staff must review cards and correct them.
- One hub. If the hub computer fails, reports on it are unavailable until it is back; there is no second hub or sync.
- Reach is limited to the Wi-Fi network's coverage.
- Phones retry automatically only while the form stays open. A report saved while the page is closed is sent the next time the same address loads.
- The board and API have no login. Anyone on the same Wi-Fi can open the board, so use a network only staff and reporters can join.
- All checks so far ran the hub inside WSL against Ollama on Windows, not the hub directly on Windows. The offline checks switched off the browser's network, not Wi-Fi, and the phone was Chromium emulating a phone screen on the hub itself.
- Setting `OLLAMA_HOST=0.0.0.0` on Windows makes Ollama reachable by every device on the same Wi-Fi while it runs. It is needed only for a hub inside WSL. For the Windows-native hub, remove that Windows variable, restart Ollama, and leave `OLLAMA_HOST` unset so both use `127.0.0.1`.
- The demo video is an animated recreation, not a recording of the running app (see [Demo video](#demo-video)).

## Citation

```bibtex
@software{jeio-dev2026silong,
  author = {Dela Cruz, R-John},
  title  = {Silong: An offline report and triage hub for evacuation centers},
  year   = {2026},
  url    = {https://github.com/jeio-dev/silong}
}
```
