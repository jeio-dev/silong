# Silong

Offline evacuation-center hub. A computer at the center serves a local web app and runs all AI locally; phones on the same Wi-Fi submit reports without internet access. See `PRD.md` for the product.

## Why local inference

During typhoons and floods, evacuation centers can lose internet access exactly when staff most need to find injured people, vulnerable evacuees and unmet needs among many free-text reports. A cloud model is unreachable then, and evacuees' reports would otherwise leave the center. Silong therefore runs speech-to-text and report extraction on the hub computer itself (Ollama and optional faster-whisper, added in later phases), so triage keeps working with only local Wi-Fi and report contents stay on the hub and the reporters' phones.

## Status: phase 03, offline delivery and hub visibility

This is an intermediate, text-only slice, not the finished product or a demo build. What works now:

- A phone on the same network opens the report form, enters Taglish text and an optional name, and the report is **saved in the phone's browser (IndexedDB) before it is sent**. Receipt never waits for the model.
- While the page stays open, the form retries every five seconds (one send at a time, oldest first) and shows which reports are "Waiting to send" and which are "Received by the hub" with their reference numbers. Saved reports survive a reload and resume at the same address. Sending the same report again never creates a second copy, even from two tabs or after the hub restarts.
- If the phone cannot reach the hub, reports stay pending and keep retrying. If the phone's own storage fails, the form says so, tries that report once immediately, and (if that fails) keeps the text visible for a manual retry rather than promising automatic delivery.
- When `SILONG_MODEL` is set, a worker inside the hub process takes the oldest queued report, extracts its fields (location, in center, reporting for others, headcount, vulnerable people, injury, needs, short English summary) with a local Ollama model, and fixed rules in code assign the urgency (Critical, High, Medium, Low) with a plain-language reason. The model never picks the urgency.
- The staff board polls every two seconds and shows three sections: "Not yet triaged" (queued, extracting or failed reports, never labelled with an urgency), "Triaged" (most urgent first, with the reason, extracted fields, measured extraction time and the original text) and "Resolved". Staff can edit any extracted field (the urgency is recomputed), complete an untriaged report by hand, retry a failed one, and mark reports resolved or reopen them.
- A status header on the board shows the phones' URL and a matching local QR code, the triage model's loaded state (checked against Ollama, never claimed as correct or fast), text-only audio availability, queued/processing counts, reports received today (hub's local date) and unresolved urgency counts. It is polled independently of the report list, so a slow status never delays new cards.
- Stopping the hub while a report is being extracted leaves it to be extracted again on the next start. Originals, staff edits and resolved state are kept.

Not built yet (later phases): audio reports (phase 04) and the target-device/disconnected demonstration (phase 05).

## Setup

Requires Python 3.11 or newer. Run from the repository root.

```sh
python3 -m venv .venv
. .venv/bin/activate            # Windows: .venv\Scripts\activate
python -m pip install -r requirements.txt
```

On Debian/Ubuntu, `python3 -m venv` fails if the `python3-venv` package is missing. Install that package, or create the environment with [uv](https://docs.astral.sh/uv/) instead: `uv venv --seed .venv`, then activate it and install as above.

The base install needs internet access once, for `pip`. AI triage also needs Ollama and a downloaded model (see [Local AI triage](#local-ai-triage)); download both while the hub still has internet. Faster-whisper is not needed for this phase; `qrcode` is installed with the base requirements (it is used only to draw the QR code).

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
| `SILONG_WHISPER` | `small`, `base`, `off` | `off` |
| `SILONG_PORT` | 1–65535 | `8000` |
| `SILONG_DB` | SQLite file path | `silong.db` |
| `OLLAMA_HOST` | Ollama's address: `host`, `host:port` or a full `http://` URL | `http://127.0.0.1:11434` |
| `SILONG_HUB_URL` | The address phones should use, for example `http://192.168.1.20:8000` | none (the board's own address, see [Connect phones](#connect-phones)) |

An invalid `SILONG_PORT`, `SILONG_WHISPER` or `SILONG_HUB_URL` is rejected at startup with a message naming the variable. `SILONG_HUB_URL` must be plain `http://` with a numeric IPv4 address that is not `127.x` or `0.0.0.0`, an optional port and no path; hostnames, `localhost` and `https://` are refused. Without a port it means port 80, so include `:8000` (or your `SILONG_PORT`). The hub only checks the format: it cannot tell whether phones can actually reach that address. `SILONG_WHISPER` is not used yet: no Whisper is loaded in this phase. `OLLAMA_HOST` is Ollama's own variable; the hub reads it to find Ollama. A bare host gets `http://` and port 11434, and a listen-all value such as `0.0.0.0` (which tells the Ollama server to accept connections from the network) is treated as `127.0.0.1`. An unusable `OLLAMA_HOST` does not stop the hub; each report's triage fails with a message saying so.

## Local AI triage

Triage runs only on the hub, through [Ollama](https://ollama.com). Install Ollama and download the model **before going offline**; nothing is downloaded at run time.

1. Install Ollama on the hub. On Windows: `winget install Ollama.Ollama` (or the installer from ollama.com), then start Ollama.
2. Download the model: `ollama pull gemma4:e2b`. The download is about 4.6 GB. `gemma4:e2b` is the model chosen for this project after the five-report test below; any other tag that `ollama pull` accepts can be set instead.
3. Start the hub with the model set:

   ```sh
   SILONG_MODEL=gemma4:e2b python -m silong
   ```

   On Windows PowerShell: `$env:SILONG_MODEL = "gemma4:e2b"`, then `python -m silong`. When Ollama runs on the same computer with its default settings, leave `OLLAMA_HOST` unset.

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
- **Audio**: "Text only: audio unavailable" (audio arrives in phase 04).
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
   - Linux: run `hostname -I` or `ip -4 addr` and use the address of the Wi-Fi interface.
3. On the hub, open the board at `http://<hub address>:8000/board` (use your `SILONG_PORT` if you changed it). The header then shows `http://<hub address>:8000/` and a matching QR code. The hub does not look up its own network adapters; it shows the address the board was opened with. If you prefer to keep the board on `localhost`, start the hub with `SILONG_HUB_URL=http://<hub address>:8000` instead (PowerShell: `$env:SILONG_HUB_URL = "http://192.168.1.20:8000"`).
4. On a phone, scan the QR code or type the address shown. Type `http://`; the hub does not use HTTPS.

### How the phone form delivers reports

- Each report is saved in the phone browser's storage (IndexedDB) first, then sent. "Waiting to send" lists saved reports the hub has not confirmed yet, with the reason for the last failure; "Received by the hub" lists reference numbers. Both lists survive a reload.
- While the page is open, the form tries again every five seconds, one report at a time, oldest first. A request that gets no answer is abandoned after 20 seconds, so one stuck request can delay the next attempts by up to that long. Try again now, shown while any report is waiting, starts an attempt at once. References are listed newest first with the time the hub confirmed them.
- Each report keeps the same ID on every attempt, so if the hub saved it but the answer was lost, the next attempt gets the original reference instead of a second copy. Two open tabs on the same phone may both send a report; the hub still stores it once.
- Saved reports belong to the exact address that was opened (for example `http://192.168.1.20:8000`). If the hub's address or port changes, the phone opens a new, empty list; reopen the old address to finish sending.
- Nothing is sent while the page is closed. A reload or first load without a connection to the hub cannot open the page at all (there is no offline copy of the page); once the same address loads again, saved reports resume.
- If the phone's own storage fails, the form says so, sends that report once immediately and, if that also fails, keeps its text on screen with "Not saved on this phone; not confirmed; no automatic retry" and a button to try again. It is not retried automatically.
- Reports cannot be edited or cancelled from the phone once submitted.

**Windows network note.** Windows Defender Firewall usually blocks inbound connections on networks set to **Public**. Set the Wi-Fi network's profile to **Private** (Settings > Network & internet > Wi-Fi > the network > Network profile type), and when Windows Defender Firewall asks on the first start, allow Python on private networks. If no prompt appears, add an inbound rule allowing TCP on the hub's port (8000 by default) for private networks. Hotel or guest Wi-Fi with client isolation also blocks phones from reaching the hub. These steps have not been verified on a Windows machine yet (see below).

If the hub runs inside WSL, its Linux address is a virtual interface that phones cannot reach without extra Windows port forwarding. Run the hub directly on Windows for phone use.

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

### Five-report model test

Run on the hardware above with five synthetic Taglish reports, one run per model. Full results, including every expected and actual field, are in [`.kaylo/phases/02-local-triage/02-MODELS.md`](.kaylo/phases/02-local-triage/02-MODELS.md).

| Model | Priority matched | Fields matched | Cold load | Extraction time per report (measured) |
| --- | --- | --- | --- | --- |
| `gemma4:e2b` | 5/5 | 34/40 | 24.0 s | 33.9, 9.4, 9.7, 7.7, 7.5 s |
| `qwen3:4b` | 5/5 | 37/40 | 13.7 s | 63.9, 19.0, 19.3, 16.0, 17.2 s |

`gemma4:e2b` was chosen, with a target of at most 15 s of extraction time per report once the model is loaded. In that test it met the target on 4 of 5 reports; the first report after loading took 33.9 s. In the end-to-end check above, one report took 18.8 s and one 10.4 s. The target is therefore not met consistently, and these are single runs, not a benchmark.

### Limitations

- Extraction runs on the CPU and takes seconds to tens of seconds per report; reports wait in order while one is extracted.
- The model makes mistakes. In the test above both models missed a stated headcount and added a medical need the report did not state, `qwen3:4b` left one location empty, and `gemma4:e2b` got one "reporting for others" flag wrong and shortened or reworded locations. Staff must review cards and correct them.
- All checks so far ran the hub inside WSL against Ollama on Windows, not the hub directly on Windows.
- Setting `OLLAMA_HOST=0.0.0.0` on Windows makes Ollama reachable by every device on the same Wi-Fi while it runs. It is needed only for a hub inside WSL. For the Windows-native hub, remove that Windows variable, restart Ollama, and leave `OLLAMA_HOST` unset so both use `127.0.0.1`.

- The offline checks switched off the browser's network, not Wi-Fi; the phone was Chromium emulating a phone screen on the hub itself.

Not yet tested: the hub running directly on Windows 11 (including the firewall steps above), the Galaxy A54 or any real phone (including its browser storage and scanning the QR code), a phone on separate hardware over real Wi-Fi with internet disconnected, readability of the board on a projector or across a room, Python 3.11–3.13, and the plain `python3 -m venv` path. The Windows 11 Ryzen hub with a Galaxy A54 and internet disconnected is the planned demonstration setup and will be verified in phase 05. No performance figures beyond the measured times above are claimed.
