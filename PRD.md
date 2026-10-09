# Silong PRD

Status: Agreed with the user's requested revisions

Source: `IDEA.md`, plus the user's decisions during definition and subsequent scope corrections. The corrections below supersede conflicting details in `IDEA.md`. Silong is a working name.

## Problem and user

During typhoons and floods, evacuation centers can lose internet access while barangay staff still need to identify and respond to urgent needs. Paper registration and unsorted free-text reports make injuries, vulnerable people, and unmet needs difficult to find promptly.

- **Reporter:** an evacuee or barangay runner using a phone to describe a situation in Taglish, by text or an optional audio upload.
- **Staff:** a barangay worker using the hub screen to review reports, correct extracted details, and mark cases resolved.

## Outcome and journey

A computer at an evacuation center hosts a local web application and runs all AI inference locally. Phones connect over local Wi-Fi without internet. Staff see a live board ordered by explicit urgency rules, with the source report and a readable explanation for each priority.

1. Staff start the prepared hub and display its local URL and QR code.
2. A reporter connects to the same Wi-Fi, opens the form, and enters text, uploads audio when supported, or supplies both. Their name is optional.
3. Submission first saves the report in a browser outbox. The form distinguishes reports waiting to send from reports confirmed by the hub.
4. The hub persists the original submission and returns a reference number without waiting for AI. Repeated delivery of the same submission returns the same reference number.
5. A background worker transcribes audio when present, extracts structured details, and applies code-based urgency rules. The report is visible to staff while processing and if processing fails.
6. Staff review priority, its reason, extracted details, originals, and measured processing times. They can correct fields, retry failed processing, and resolve a case.

## Included

### Report submission and offline delivery

- Phone form with a text box, optional reporter name, and optional audio file capture/upload over local HTTP.
- Large tap targets and Taglish-friendly labels, usable one-handed on the Samsung Galaxy A54 browser.
- **User decision:** use IndexedDB for the browser outbox, storing audio blobs directly alongside report text and metadata. Use a small vanilla JavaScript wrapper without an external library or CDN dependency.
- The outbox assigns a stable client-generated UUID to each submission, retries every five seconds while the tab is open, and shows a pending count and confirmed reference numbers. Delivery waits for the browser save to complete, and pending records are removed only after the hub confirms receipt.
- Outbox behavior applies to every supported submission type, including audio when enabled. If the IndexedDB save fails, visibly report the storage failure, still attempt one immediate send, and warn that this report will not retry automatically if that send fails.
- The server saves raw text and any audio upload before acknowledging receipt or running AI. A repeated client UUID creates no duplicate report.
- When transcription is unavailable, the server continues to run and the form hides audio submission.
- **Default:** require nonempty text or an audio attachment; the reporter name alone is not a report.

### Local extraction and prioritization

- A persistent queue processes the oldest waiting report, one at a time. Reports and pending work survive a hub restart. On startup, recover interrupted work by resetting reports stuck in `transcribing` or `extracting` to `queued`, preserving originals, staff edits, and resolved state.
- Optional local transcription followed by local LLM extraction into the fixed fields below.
- **User decision, revised:** when text and audio are both present, concatenate typed text and transcript into one input and extract once. Preserve and show both originals on the card; staff correct mistakes. There is no automated contradiction detection or provisional-urgency path.
- Extraction uses only stated information. Missing location, headcount, and injury details stay unknown; missing lists stay empty. Headcount is never guessed.
- `location` describes where the people in need are, rather than necessarily where the reporter is. `in_center` records whether those people are inside the evacuation center; `on_behalf` records whether the reporter is reporting for other people. Either Boolean is null when the report does not establish it.
- The fixed prompt includes two to three Taglish examples. The English summary is at most 20 words.
- **Default:** `injured` is false when no injury is stated, reflecting “no injury reported” rather than a confirmed absence of injury.
- Failed transcription or extraction preserves the report, shows a failed status and useful error, and permits retry or manual completion.
- Staff edits recompute urgency and its explanation.
- **Default:** staff corrections persist and are not silently replaced by a later worker result; resolving a report persists across refreshes and restarts.

| Field | Required representation |
| --- | --- |
| `location` | String or null; where the people in need are |
| `in_center` | Boolean or null; whether the people in need are inside the evacuation center |
| `on_behalf` | Boolean or null; whether the reporter is reporting for other people |
| Headcount | Integer or null |
| Vulnerable people | Zero or more of `senior`, `pwd`, `infant`, `child`, `pregnant`, `sick` |
| Injury reported | Boolean |
| Injury note | String or null |
| Needs | Zero or more of `rescue`, `medical`, `medicine`, `food`, `water`, `milk_formula`, `hygiene`, `other` |
| Summary | English string, at most 20 words |

Urgency is computed by code, with the first matching rule winning:

| Priority | Condition |
| --- | --- |
| Critical | Rescue needed outside center: needs includes `rescue` and `in_center` is false |
| Critical | Rescue or medical need, or an injury reported |
| High | Any vulnerable person together with medicine, milk/formula, water, or food need |
| Medium | Any other stated need |
| Low | No condition above matches |

Each priority has a plain-language reason, such as “Critical: rescue needed outside center,” “Critical: injury reported,” or “High: infant + milk_formula.” The outside-center rule runs first so that reason is preserved; an unknown center location still permits the general rescue rule to apply. Unresolved classified reports sort by urgency, then oldest receipt first, then larger headcount.

### Staff board and hub status

- Board polls every two seconds and is readable from across a room.
- Cards show urgency and reason, the people-in-need location, `in_center`, `on_behalf`, headcount, vulnerable-person tags, needs, summary, processing status, and measured transcription/extraction times when available. Unknown location flags are distinguishable from false.
- Original text and transcript remain accessible, with audio retained on the hub. When both text and audio were submitted, the card shows both originals for staff review.
- Staff can edit every extracted field, resolve reports, and retry failed processing. Resolved reports move to a separate section.
- Header shows the current local hub URL, its QR code, model readiness, transcription availability, queue length, reports received today, and counts per urgency.
- **Default:** queued, processing, failed, and manually incomplete reports remain conspicuous in a separate unclassified section until they have an urgency; they are never silently treated as low priority.
- **Default:** “reports today” uses the hub's local calendar date, and urgency counts describe unresolved classified reports.
- Every timing displayed is measured. Missing measurements remain unavailable rather than being estimated.

### Demonstration and documentation

- Demonstrate phone submission, local AI processing, and staff review with internet access visibly disconnected while local Wi-Fi remains available.
- Provide setup instructions for the tested hub, phone connection instructions, and a Windows firewall/network note. State exactly which platforms have been tested.
- README explains why local inference is necessary, discloses models, frameworks, libraries, and AI-assisted development tools, and reports tested hardware and actual measured times.
- README states limitations: Wi-Fi coverage, Taglish transcription accuracy, a single hub, and the requirement to keep the loaded phone tab open for automatic retries.
- Describe a field kit of a prepared laptop and travel router; demonstrate with the phone hotspot or home router.
- **Default:** demonstration and public repository examples use synthetic reports and exclude operational databases, uploaded recordings, and real evacuee information.

## Out of scope

- Authentication, multiple-hub synchronization, SMS, maps, packaged mobile apps, HTTPS, and service workers.
- Automated text/audio contradiction detection, multiple extractions to compare sources, and provisional urgency for conflicting sources.
- Cross-platform verification and Termux validation for this demo.
- First-time page loading without connectivity to the hub, or background delivery after the phone tab closes.
- Cloud inference and remote storage of report contents.
- RAG “Tanong” Q&A, duplicate-family detection, and CSV export are deferred stretch capabilities. They enter scope only after the required product is complete and recorded, with an explicit scope update.

## Constraints

These constraints come from `IDEA.md` and the user's explicit revisions; they are not new stack recommendations.

- **Deadline:** October 10, 2026, at 10:00 AM Manila time, interpreted using the current project date. Code freezes at the deadline; the submission repository must be public.
- **Submission:** screening uses the GitHub repository and a demo video on X or LinkedIn, tagging Devin/Cognition and `#AppBuildersPH`.
- **Hackathon:** substantially built during the event, meaningful inference runs locally, tools and models are disclosed, and benchmarks are measured rather than fabricated. Preserve evidence of when development occurred.
- **Primary hardware:** Windows 11, Ryzen 5 2400G, CPU-only inference; Samsung Galaxy A54 browser as the phone client.
- **Network:** Galaxy A54 hotspot with mobile data disabled, or a router with its WAN/fiber disconnected. Required models and dependencies are prepared before disconnection.
- **User decision, revised portability scope:** keep the code portable through environment configuration, optional Whisper, and light dependencies. Target-hardware verification is limited to the Windows 11 Ryzen 5 2400G hub, with the A54 as client. README discloses tested platforms; unchanged operation on other platforms or Termux is not a demo requirement or a verified claim.
- **Specified stack:** Python 3.11+, FastAPI/uvicorn, local Ollama over HTTP with `httpx`, optional `faster-whisper` using CPU int8, standard-library SQLite, `qrcode`, plain HTML/vanilla JavaScript, and one shared CSS file. No frontend build step.
- **Specified architecture:** one Python application process with a worker thread; Ollama is a separate local inference service. A SQLite reports table also serves as the persistent queue.
- Bind the application to `0.0.0.0` so LAN phones can reach it. Show a usable LAN URL and matching QR code.
- Ollama requests use `keep_alive: -1`, `temperature: 0`, `think: false`, and the extraction JSON schema as `format`. Keep the system prompt/examples as a fixed prefix and place report content last.
- Configuration uses `SILONG_MODEL`, `SILONG_WHISPER` (`small`, `base`, or `off`), `SILONG_PORT` (default 8000), and `SILONG_DB` (default `silong.db`).
- **User decision, replacing the source's `localStorage` choice:** use IndexedDB for the outbox, including audio blobs, over the local HTTP connection. Browser storage failures remain visible; no external script fetch is required.
- Report contents remain on the hub and the submitting browser while queued. No per-report cloud service cost.

## Success criteria

**User decision:** criteria 1, 2, 3, 5, and 7 gate demo readiness. Criteria 4, 6, 8, 9, and 10 are measure-and-disclose: record actual outcomes, unfinished checks, and limitations, protecting the demo gates when time is short. Included capabilities remain in scope; an unverified or incomplete non-gating capability is disclosed rather than claimed as passing.

1. **Demo-gating:** with mobile data/WAN disconnected and the hub prepared, the A54 opens the local form, submits a Taglish report, receives a reference number, and staff see its locally extracted fields and rule-based priority.
2. **Demo-gating:** when the phone loses connectivity to the hub after loading the form, a supported report is saved visibly as pending in IndexedDB. With the tab open, reconnecting delivers it automatically, confirms its reference number, and creates exactly one database report despite retries. The saved payload survives a page reload; if the reload happens while the hub is unreachable, pending reports resume sending the next time the form loads.
3. **Demo-gating:** a deliberately slow or unavailable model does not delay submission acknowledgement until inference finishes. The raw report is persisted and visible on the next successful board refresh while AI is pending or failed.
4. **Measure-and-disclose:** when audio is enabled, an audio-only report is transcribed and extracted locally. A report containing both text and audio concatenates typed text and transcript for exactly one extraction and shows both originals on the card. When transcription cannot load, text reporting and the board still work and the form hides audio.
5. **Demo-gating:** representative fixtures demonstrate all four urgency levels, first-match precedence, displayed reasons, and sorting. An outside-center rescue receives “Critical: rescue needed outside center”; an unknown center location still receives Critical for rescue under the general rule. `location` refers to the people in need, and `on_behalf` identifies reports for others. Staff edits change priority when the matching rule changes.
6. **Measure-and-disclose:** a failed report stays visible and can be retried or manually completed. Restarting the hub retains reports, resets interrupted `transcribing`/`extracting` rows to `queued`, resumes their work, and preserves staff corrections and resolved states.
7. **Demo-gating:** the board updates within its two-second polling cadence after a server-side change, shows pending/failed work clearly, separates resolved cases, and displays a QR code that opens the reporter form from the A54.
8. **Measure-and-disclose:** a five-report Taglish test on the Ryzen 5 2400G records source inputs, expected extraction/priority, actual outputs, and measured per-report extraction times. **User decision:** select the model and processing-speed acceptance target after this test; the resulting target and observed accuracy must be recorded before performance is claimed as accepted.
9. **Measure-and-disclose:** test a roughly 15-second audio clip on the primary hub. Prefer Whisper `small` if its measured transcription time is under approximately six seconds; otherwise evaluate `base`, following the source's selection rule. Disclose measured timing and transcription accuracy.
10. **Measure-and-disclose:** setup and the disconnected demonstration follow the README. Disclose the actual Windows hub/A54 checks, exact model tags/sizes, dependencies, development tools, measured results, and limitations. Other platforms are labeled untested. Public artifacts use synthetic reports and exclude real evacuee data.

## Open questions

- **Evidence-dependent:** exact Ollama model tag and the text-processing time target, to be selected after the five-report latency and accuracy test. Candidate tags in `IDEA.md` are candidates, not verified availability or performance claims.
- **Evidence-dependent:** Whisper size and supported audio formats on the A54 and target hub, including usable audio size/duration limits and browser outbox capacity.

## Repository baseline

At definition, the repository contains an empty tracked `README.md` and the user's untracked `IDEA.md`. No application code, existing PRD, roadmap, or phase plan exists. Implementation choices already specified in the idea are recorded above; build order, tasks, API details, and database design remain inputs to `/kaylo:plan` rather than implementation performed by this definition.
