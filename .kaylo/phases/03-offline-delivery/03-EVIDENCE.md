# 03 Offline delivery evidence

Integrated host check for phase 03 T5, recorded 2026-10-09 (UTC 2026-10-09T20:38Z). Synthetic reports only. These observations establish behavior on this development host. They are not the Windows-native hub, a Galaxy A54, real Wi-Fi or an internet-disconnected demonstration; those remain phase 05.

## Environment

- Host: Ubuntu 26.04.1 LTS under WSL2 (Linux 6.18.33.2-microsoft-standard-WSL2) on the AMD Ryzen 5 2400G / Windows 11 Pro machine recorded in phase 02. Host clock zone PDT (UTC-7), so "today" counts used that local date (2026-10-09).
- Hub: CPython 3.14.7 in `.venv` with the pinned `requirements.txt` (fastapi 0.143.0, uvicorn 0.54.0, httpx 0.28.1, python-multipart 0.0.32, qrcode 8.2), branch `phase/03-offline-delivery` at `ddb9d8d` plus uncommitted README/plan documentation only (no application code changed in T5).
- Browser: headless Chromium 156.0.8078.4 driven by Playwright from `/tmp/aarista-chart-verify/node_modules/playwright`, Node v24.21.0. Phone page: 412x915 viewport with mobile and touch emulation. Board: 1280x900.
- Hub address: the WSL interface address `172.23.56.13` on a free port, with `SILONG_HUB_URL` unset, so the phone URL and QR came from the board's own request address. The phone page was opened at the same address.
- Model: `SILONG_MODEL=gemma4:e2b` with `OLLAMA_HOST` pointing at a fake local HTTP server (no real model). Its `/api/ps` lists `gemma4:e2b` (or holds the request when told to); its `/api/chat` answers the warm-up at once, returns HTTP 500 for the first extraction and never answers later extractions (held). This deterministically establishes failed and held work; it is not a latency benchmark.

## Commands

From the repository root with the virtual environment active:

| Check | Result |
| --- | --- |
| `python -m unittest discover -s tests -v` | 199 tests, OK |
| `node tests/browser_outbox.cjs` | exit 0, "all assertions passed" |
| `node tests/browser_delivery.cjs` | exit 0, "all assertions passed (4 POST attempts)" |
| `node tests/browser_header.cjs` | exit 0, "all assertions passed" |
| `node .kaylo/phases/03-offline-delivery/03-journey.cjs` | exit 0 on two consecutive runs; figures below are from the second |
| `git diff --check` | clean |

`03-journey.cjs` is the integrated journey below. It starts its own hub on a temporary SQLite database and fake Ollama, prints its observations as JSON, asserts the outcomes, then stops the hub and deletes the database. Instead of a person clicking through Chromium DevTools, the plan's manual observation was scripted with the same browser controls: `context.setOffline` for the network, request interception for blocked and lost answers. Times are wall-clock milliseconds measured by the script, from the browser seeing the phone's completed POST to the board card being present (checked every 50 ms).

## Observations

1. **URL and QR agree.** The board, opened at `http://172.23.56.13:34403/board`, showed and linked `http://172.23.56.13:34403/`. The SVG from `/api/qr` (4,868 bytes, no remote references) was byte-identical to `status.make_qr_svg` for the displayed URL. Through `127.0.0.1` the same hub reported `hub_url: null` and `/api/qr` returned 409, so loopback is never advertised. No QR was decoded by a scanner or phone; identity with the generator output is the evidence.
2. **Failed extraction is visible.** Report A got Reference #1; its card appeared on the board 1,356 ms after receipt and became "Triage failed: no urgency assigned" with "Error: Ollama returned HTTP 500: synthetic extractor failure".
3. **Held extraction does not block receipt.** Report B got Reference #2 while its extraction was held; card after 1,898 ms, labelled "Extracting: no urgency assigned yet", header "Processing: 1".
4. **Held status probe.** With Ollama's `/api/ps` held, `/api/status` answered in 1,018 ms with `model_state: unavailable`, while a POST sent at the same time was stored in 15 ms (Reference #3).
5. **Offline save and retry.** With the board's status poll also held and the phone context offline, reports C and D were submitted: the form said "Saved on this phone; waiting to send", the pending count reached (2), and each row showed "Sending failed: Could not reach the hub. Check the Wi-Fi connection." Over 12.2 s offline there were 7 failed POST attempts and no hub rows for either ID. A draft typed into the text box kept its text and focus through those retries.
6. **Reload and reconnect.** A reload while offline failed with `net::ERR_INTERNET_DISCONNECTED` (no offline copy of the page). With the network back but POSTs still refused, loading the same address restored both pending rows with the same two UUIDs. After POSTs were allowed, both were delivered in 5,037 ms (the five-second retry), one database row per UUID (Reference #4 and #5). Their board cards appeared 575 ms after the last receipt even though the board's status request was still held; the header showed "Status may be out of date; the values shown are the last received."
7. **Lost answer.** For report E, the hub stored the POST but the phone's response was dropped. The next attempt (2 POSTs in total) confirmed it 5,231 ms after submission with Reference #6, equal to the single database row for that UUID.
8. **Counts match the database.** After the status hold was released the header recovered within 129 ms: "Queued: 4", "Processing: 1", "6 report(s)", all urgency counts 0; `/api/status` gave the same numbers, and the database held 1 failed, 1 extracting and 4 queued rows.
9. **Staff workflow.** On failed report #1, Complete by hand with "In center: no" and need "rescue" gave Critical, "rescue needed outside center", "Edited by staff", and Critical 1 in the header. Editing the need to water recomputed Medium, "needs water" (Critical 0, Medium 1). Mark resolved moved it to Resolved and the Medium count to 0; "Today" stayed at 6. The database row ended `done`, `medium`, resolved, edited.
10. **No errors or outside requests.** No page errors; every request from both pages went to the hub address. The hub log recorded "Report 1 failed extraction: Ollama returned HTTP 500: synthetic extractor failure".

Screenshots (`journey-board-1280.png`, `journey-phone-412.png`) were inspected after the run and not committed: no horizontal overflow, header counts and QR readable on the desktop board, phone lists and controls readable at 412 px.

## Observation for review

After a normally saved report is delivered, the phone's feedback line still says "Saved on this phone; waiting to send" while that report already appears under "Received by the hub" with its reference and "Waiting to send" shows (0) (observations 2 and 6, visible in the phone screenshot). The lists are correct; only the earlier message is not replaced. This is T3 behavior and was not changed in this documentation task.

## Not run here

- Windows-native hub, Windows firewall, Galaxy A54 browser storage, any physical phone, real Wi-Fi with internet disconnected (phase 05).
- Decoding or scanning the QR code with a phone or independent decoder.
- Board readability on a projector or across a room.
- Real Ollama extraction during this journey (phase 02 holds the measured model results).

## T6 UI polish (2026-10-09)

Same host and Chromium 156.0.8078.4 as above, synthetic reports only.

- Gate: `python -m unittest discover -s tests -v`, 201 tests OK. `node tests/browser_outbox.cjs`, `node tests/browser_delivery.cjs` (20 POST attempts) and `node tests/browser_header.cjs` all passed. `node --check` passed on both scripts, and `git diff --check` was clean.
- Journey rerun (`node .kaylo/phases/03-offline-delivery/03-journey.cjs`, exit 0, no page errors or non-hub requests). In the 1280×900 board screenshot the status header ends at about 352 px, where the same state's header ended at about 600 px before T6. In the 412 px phone screenshot, references read #6 down to #1, each with "Confirmed / Natanggap HH:MM", and "Try again now" is hidden while nothing is waiting.
- Scratch visual check (not committed), run against a temp-DB hub with `SILONG_MODEL` unset and `SILONG_HUB_URL=http://192.168.1.20:<port>`, using five synthetic reports: two completed by hand (Critical, High), one set to failed in SQLite, two queued. Measured at 1280×900: header bottom 321 px, first card bottom 610 px (fully in view), urgency counts on one row, no horizontal overflow, QR 140 px loaded from `/api/qr`. "Complete by hand" is `secondary` on queued cards and primary on the failed card. The Critical card background is rgb(252, 239, 238). Empty-state lines are hidden while their sections have cards. No card was outlined on the first load. A report completed after load moved to Triaged with `just-changed`, and the class was gone 3 s later. The inline editor on the Critical card stayed open across a poll. No overflow at 412×915. With reduced motion, a new card's `animation-name` was `none`. No page errors.
- Not run: projector or across-room readability, A54 or Windows rendering, physical QR scan.
