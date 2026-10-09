# Silong

Offline evacuation-center hub. A computer at the center serves a local web app and runs all AI locally; phones on the same Wi-Fi submit reports without internet access. See `PRD.md` for the product.

## Why local inference

During typhoons and floods, evacuation centers can lose internet access exactly when staff most need to find injured people, vulnerable evacuees and unmet needs among many free-text reports. A cloud model is unreachable then, and evacuees' reports would otherwise leave the center. Silong therefore runs speech-to-text and report extraction on the hub computer itself (Ollama and optional faster-whisper, added in later phases), so triage keeps working with only local Wi-Fi and report contents stay on the hub and the reporters' phones.

## Status: phase 01, durable text intake

This is an intermediate, text-only slice, not the finished product or a demo build. What works now:

- A phone on the same network opens the report form, enters Taglish text and an optional name, and gets a hub reference number only after the report is saved in SQLite.
- Sending the same submission again (the form's Try again button, which reuses the submission's ID) returns the original reference and never creates a second report, including after the hub restarts.
- The staff board polls every two seconds and lists each report's original text, name, reference, receipt time and status in a "Not yet triaged" section. Reports stay queued: nothing is classified or given an urgency yet.
- No model, Whisper or internet connection is needed to submit or view reports.

Not built yet (later phases): local AI extraction, urgency rules and sorting, staff edits, resolve and retry (phase 02); the phone's automatic offline outbox and retries, the on-screen hub URL and QR code, and readiness/count displays (phase 03); audio reports (phase 04). In this phase the form sends once and shows "Not confirmed" if the hub does not answer; the reporter must keep the page open and press Try again. Nothing is saved on the phone.

## Setup

Requires Python 3.11 or newer. Run from the repository root.

```sh
python3 -m venv .venv
. .venv/bin/activate            # Windows: .venv\Scripts\activate
python -m pip install -r requirements.txt
```

On Debian/Ubuntu, `python3 -m venv` fails if the `python3-venv` package is missing. Install that package, or create the environment with [uv](https://docs.astral.sh/uv/) instead: `uv venv --seed .venv`, then activate it and install as above.

The base install needs internet access once, for `pip`. Faster-whisper, qrcode and Ollama are not needed for this phase.

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
| `SILONG_MODEL` | Ollama model tag | none (AI triage not configured) |
| `SILONG_WHISPER` | `small`, `base`, `off` | `off` |
| `SILONG_PORT` | 1–65535 | `8000` |
| `SILONG_DB` | SQLite file path | `silong.db` |

An invalid `SILONG_PORT` or `SILONG_WHISPER` is rejected at startup with a message naming the variable. In this phase neither `SILONG_MODEL` nor `SILONG_WHISPER` is used: no model or Whisper is loaded.

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

The suite uses temporary databases and synthetic reports only. It includes a check that starts the real hub, submits a report, restarts the hub on the same database and repeats the submission, expecting the same reference and one stored report.

## Tested platforms

Checked so far:

- Hub: Ubuntu 26.04.1 LTS under WSL2 (Linux 6.18) with CPython 3.14.7 in a uv-created virtual environment (`uv venv --seed`; this host lacks `python3-venv`), using the pinned versions in `requirements.txt`. Ollama was not running during checks.
- Browser: Chromium 156 on the same host, emulating a 412×915 phone screen, reaching the hub through the WSL network address over plain HTTP.

Not yet tested: Windows 11 (including the firewall steps above), the Galaxy A54 or any real phone, a phone on separate hardware over real Wi-Fi, Python 3.11–3.13, and the plain `python3 -m venv` path. The Windows 11 Ryzen hub with a Galaxy A54 and internet disconnected is the planned demonstration setup and will be verified in phase 05. No performance figures are claimed for this phase.
