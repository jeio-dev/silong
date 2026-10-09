# Silong

Offline evacuation-center hub. A computer at the center serves a local web app and runs all AI locally; phones on the same Wi-Fi submit reports without internet access. See `PRD.md` for the product.

## Why local inference

During typhoons and floods, evacuation centers can lose internet access exactly when staff most need to find injured people, vulnerable evacuees and unmet needs among many free-text reports. A cloud model is unreachable then, and evacuees' reports would otherwise leave the center. Silong therefore runs speech-to-text and report extraction on the hub computer itself (Ollama and optional faster-whisper, added in later phases), so triage keeps working with only local Wi-Fi and report contents stay on the hub and the reporters' phones.

## Status: phase 02, local AI triage and staff corrections

This is an intermediate, text-only slice, not the finished product or a demo build. What works now:

- A phone on the same network opens the report form, enters Taglish text and an optional name, and gets a hub reference number only after the report is saved in SQLite. Receipt never waits for the model.
- Sending the same submission again (the form's Try again button, which reuses the submission's ID) returns the original reference and never creates a second report, including after the hub restarts.
- When `SILONG_MODEL` is set, a worker inside the hub process takes the oldest queued report, extracts its fields (location, in center, reporting for others, headcount, vulnerable people, injury, needs, short English summary) with a local Ollama model, and fixed rules in code assign the urgency (Critical, High, Medium, Low) with a plain-language reason. The model never picks the urgency.
- The staff board polls every two seconds and shows three sections: "Not yet triaged" (queued, extracting or failed reports, never labelled with an urgency), "Triaged" (most urgent first, with the reason, extracted fields, measured extraction time and the original text) and "Resolved". Staff can edit any extracted field (the urgency is recomputed), complete an untriaged report by hand, retry a failed one, and mark reports resolved or reopen them.
- Stopping the hub while a report is being extracted leaves it to be extracted again on the next start. Originals, staff edits and resolved state are kept.

Not built yet (later phases): the phone's automatic offline outbox and retries, the on-screen hub URL and QR code, readiness and count displays (phase 03); audio reports (phase 04). In this phase the form sends once and shows "Not confirmed" if the hub does not answer; the reporter must keep the page open and press Try again. Nothing is saved on the phone.

## Setup

Requires Python 3.11 or newer. Run from the repository root.

```sh
python3 -m venv .venv
. .venv/bin/activate            # Windows: .venv\Scripts\activate
python -m pip install -r requirements.txt
```

On Debian/Ubuntu, `python3 -m venv` fails if the `python3-venv` package is missing. Install that package, or create the environment with [uv](https://docs.astral.sh/uv/) instead: `uv venv --seed .venv`, then activate it and install as above.

The base install needs internet access once, for `pip`. AI triage also needs Ollama and a downloaded model (see [Local AI triage](#local-ai-triage)); download both while the hub still has internet. Faster-whisper and qrcode are not needed for this phase.

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

An invalid `SILONG_PORT` or `SILONG_WHISPER` is rejected at startup with a message naming the variable. `SILONG_WHISPER` is not used yet: no Whisper is loaded in this phase. `OLLAMA_HOST` is Ollama's own variable; the hub reads it to find Ollama. A bare host gets `http://` and port 11434, and a listen-all value such as `0.0.0.0` (which tells the Ollama server to accept connections from the network) is treated as `127.0.0.1`. An unusable `OLLAMA_HOST` does not stop the hub; each report's triage fails with a message saying so.

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

Open `http://localhost:8000/board` on the hub. It refreshes every two seconds and warns when it cannot reach the hub.

- **Not yet triaged** is shown first, so new and failed reports stay visible. Cards here never show an urgency.
- **Triaged** lists reports from most to least urgent; within an urgency, the oldest report comes first. Each card shows the urgency word and colour, the reason, the extracted fields, the summary, "Extraction took N.N s" (measured on the hub for that report), "Edited by staff" when staff changed it, and the original text under **Original report**.
- **Edit** (or **Complete by hand** for a report without fields) opens a form for all fields. Saving recomputes the urgency. If the report changes while the form is open, the form says so instead of being replaced.
- **Mark resolved** moves a report to the collapsed **Resolved** section; **Reopen** brings it back.

Extraction can be wrong. Staff should read the original text of every report and correct the fields when needed.

## Connect phones

1. Connect the hub computer and the phones to the same Wi-Fi network. Internet access is not needed.
2. Find the hub's address on that network:
   - Windows: run `ipconfig` and use the **IPv4 Address** of the Wi-Fi adapter (for example `192.168.1.20`).
   - Linux: run `hostname -I` or `ip -4 addr` and use the address of the Wi-Fi interface.
3. On a phone, open `http://<hub address>:8000/` (use your `SILONG_PORT` if you changed it). Type `http://`; the hub does not use HTTPS.
4. On the hub, open `http://localhost:8000/board` for the staff board.

Automatic address detection and a QR code arrive in phase 03.

**Windows network note.** Windows Defender Firewall usually blocks inbound connections on networks set to **Public**. Set the Wi-Fi network's profile to **Private** (Settings > Network & internet > Wi-Fi > the network > Network profile type), and when Windows Defender Firewall asks on the first start, allow Python on private networks. If no prompt appears, add an inbound rule allowing TCP on the hub's port (8000 by default) for private networks. Hotel or guest Wi-Fi with client isolation also blocks phones from reaching the hub. These steps have not been verified on a Windows machine yet (see below).

If the hub runs inside WSL, its Linux address is a virtual interface that phones cannot reach without extra Windows port forwarding. Run the hub directly on Windows for phone use.

## Checks

```sh
python -m unittest discover -s tests -v
```

The suite uses temporary databases and synthetic reports only and never contacts Ollama: tests replace the model with fakes. It includes a check that starts the real hub, submits a report, restarts the hub on the same database and repeats the submission, expecting the same reference and one stored report.

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

Not yet tested: the hub running directly on Windows 11 (including the firewall steps above), the Galaxy A54 or any real phone, a phone on separate hardware over real Wi-Fi, Python 3.11–3.13, and the plain `python3 -m venv` path. The Windows 11 Ryzen hub with a Galaxy A54 and internet disconnected is the planned demonstration setup and will be verified in phase 05. No performance figures beyond the measured times above are claimed.
