"use strict";

// Staff board: polls the hub every two seconds. After a failed poll the last
// successful view stays on screen with a visible stale warning. Cards are kept
// by report id and rebuilt only when that report changes, so an expanded
// original survives later polls. Action progress and messages are kept by
// report id too, so a card rebuilt while its action runs still shows them. The server sends reports
// in board order; the board only splits them into sections and never re-sorts.
// A card whose edit form is open is never rebuilt by a poll; if its report
// changes meanwhile, the form says so. A card that moves to another section,
// or arrives after the first load, is briefly outlined so staff notice it.

const POLL_MS = 2000;
const POLL_TIMEOUT_MS = 5000;

const connection = document.getElementById("connection");
const empty = document.getElementById("empty");
const SECTIONS = {
  unclassified: {
    list: document.getElementById("unclassified"),
    count: document.getElementById("unclassified-count"),
    empty: document.getElementById("unclassified-empty"),
  },
  triaged: {
    list: document.getElementById("triaged"),
    count: document.getElementById("triaged-count"),
    empty: document.getElementById("triaged-empty"),
  },
  resolved: { list: document.getElementById("resolved"), count: document.getElementById("resolved-count"), empty: null },
};

// Hub status header, polled independently of the report list.
const statusNote = document.getElementById("status-note");
const phoneUrl = document.getElementById("phone-url");
const phoneUrlWrap = document.getElementById("phone-url-wrap");
const phoneUrlUnavailable = document.getElementById("phone-url-unavailable");
const phoneQr = document.getElementById("phone-qr");
const modelStatus = document.getElementById("model-status");
const workerNote = document.getElementById("worker-note");
const audioStatus = document.getElementById("audio-status");
const queueStatus = document.getElementById("queue-status");
const processingStatus = document.getElementById("processing-status");
const reportsToday = document.getElementById("reports-today");
const localDate = document.getElementById("local-date");
const urgencyCounts = document.getElementById("urgency-counts");
const URGENCY_ORDER = ["critical", "high", "medium", "low"];

// Triaged-lane urgency filter and a search over every card's text. Both only
// hide cards; a card whose edit form is open always stays visible.
const search = document.getElementById("search");
const filterButtons = document.querySelectorAll("button[data-filter]");
const filterNote = document.getElementById("filter-note");
let urgencyFilter = "all";

// Unclassified reports never carry an urgency word.
const STATUS_LABELS = {
  queued: "Queued: waiting for triage, no urgency assigned",
  extracting: "Extracting: no urgency assigned yet",
  failed: "Triage failed: no urgency assigned",
};
const URGENCY_LABELS = { critical: "Critical", high: "High", medium: "Medium", low: "Low" };
const TERMS = { pwd: "PWD", milk_formula: "milk formula" };
const HUB_UNREACHABLE = "Could not reach the hub. Please try again.";
const CHANGED_NOTICE = "This report changed since you opened the editor. Cancel and edit again to see the latest version.";
// Same values and order as silong/rules.py.
const VULNERABLE = ["senior", "pwd", "infant", "child", "pregnant", "sick"];
const NEEDS = ["rescue", "medical", "medicine", "food", "water", "milk_formula", "hygiene", "other"];
const BLANK_FIELDS = {
  location: null, in_center: null, on_behalf: null, headcount: null, vulnerable: [],
  injured: false, injury_note: null, needs: [], summary: "",
};

const cards = new Map(); // report id -> { node, key, section }
const actionStates = new Map(); // report id -> { pending, message, error }
const editors = new Map(); // report id -> { report, form, notice, feedback, pending }
let lastReports = [];
let lastSuccess = null;
let requested = 0;
let rendered = 0;
let renderedOnce = false; // no highlight for cards present on the first load

// Status header state (kept across failed polls, with a stale warning).
let lastStatus = null;
let statusEverLoaded = false;
let statusInFlight = false;
let lastQrUrl = null;

function timeText(date) {
  return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function receivedText(createdAt) {
  const date = new Date(createdAt);
  if (Number.isNaN(date.getTime())) return "Received " + createdAt;
  return "Received " + date.toLocaleString([], {
    month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit",
  });
}

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function yesNo(value) {
  if (value === true) return "yes";
  if (value === false) return "no";
  return "unknown";
}

function term(value) {
  return TERMS[value] || value;
}

function terms(values) {
  return values.length ? values.map(term).join(", ") : "none stated";
}

function sectionOf(report) {
  if (report.resolved) return "resolved";
  return report.urgency ? "triaged" : "unclassified";
}

// The extracted fields as compact label/value chips. Facts that add nothing
// (unknown headcount, nobody injured, no vulnerable group) are left out; the
// edit form still shows every field.
function fieldList(fields) {
  const rows = [["Location", fields.location === null ? "unknown" : fields.location]];
  if (fields.in_center !== null) rows.push(["In center", yesNo(fields.in_center)]);
  if (fields.on_behalf) rows.push(["For others", "yes"]);
  if (fields.headcount !== null) rows.push(["People", String(fields.headcount)]);
  if (fields.vulnerable.length) rows.push(["Vulnerable", terms(fields.vulnerable)]);
  if (fields.injured) rows.push(["Injured", fields.injury_note ? fields.injury_note : "yes"]);
  rows.push(["Needs", terms(fields.needs)]);
  const list = element("dl", "fields");
  for (const [name, value] of rows) {
    const row = element("div", "field-row");
    row.append(element("dt", "", name), element("dd", "", value));
    list.append(row);
  }
  return list;
}

// Typed text and, for a voice report, its transcript: both originals stay
// visible for staff to check against the extracted fields.
function original(report, collapsed) {
  const parts = [];
  if (report.raw_text && report.raw_text.trim()) parts.push(element("p", "report-text", report.raw_text));
  if (report.has_audio) {
    const transcript = element("p", "report-text transcript");
    transcript.append(element("strong", "", "Voice: "));
    transcript.append(report.transcript === null || report.transcript === undefined
      ? "recording received, not yet transcribed"
      : report.transcript.trim() || "(no speech heard)");
    parts.push(transcript);
  }
  if (!collapsed) {
    const box = element("div", "original-open");
    box.append(...parts);
    return box;
  }
  const details = element("details", "original");
  details.append(element("summary", "", report.has_audio ? "Original report and voice transcript" : "Original report"), ...parts);
  return details;
}

function actionButton(label, action, className, run) {
  const button = element("button", className, label);
  button.type = "button";
  button.dataset.action = action;
  button.addEventListener("click", run);
  return button;
}

// Show a report's action progress or last action message on its card.
function showActionState(card, id) {
  const state = actionStates.get(id) || {};
  const feedback = card.querySelector(".card-feedback");
  feedback.className = state.error ? "card-feedback error" : "card-feedback";
  feedback.textContent = state.message || "";
  for (const button of card.querySelectorAll("button[data-action]")) button.disabled = Boolean(state.pending);
}

function setActionState(id, state) {
  actionStates.set(id, state);
  const entry = cards.get(id);
  if (entry) showActionState(entry.node, id);
}

function reportCard(report) {
  const section = sectionOf(report);
  const item = element("li", "report");
  item.dataset.id = report.id;
  const header = element("div", "report-header");

  if (report.urgency) {
    const label = URGENCY_LABELS[report.urgency] || report.urgency;
    item.classList.add("urgency-" + report.urgency);
    header.append(element("span", "urgency-label", label));
    const reason = report.urgency_reason || "";
    header.append(element("span", "reason", reason.startsWith(label + ": ") ? reason.slice(label.length + 2) : reason));
  } else {
    header.append(element("span", "status status-" + report.status, STATUS_LABELS[report.status] || "Status: " + report.status));
  }
  header.append(element("span", "reference", "#" + report.id));
  item.append(header);

  if (report.urgency && report.fields) {
    item.append(fieldList(report.fields));
    if (report.fields.summary) item.append(element("p", "report-summary", report.fields.summary));
  }

  item.append(original(report, section !== "unclassified"));
  if (report.error && !report.urgency) item.append(element("p", "report-error", "Error: " + report.error));

  const meta = [report.reporter ? "From " + report.reporter : "Name not given", receivedText(report.created_at)];
  if (report.edited) meta.push("Edited by staff");
  if (report.asr_ms !== null && report.asr_ms !== undefined) meta.push("Transcription took " + (report.asr_ms / 1000).toFixed(1) + " s");
  if (report.llm_ms !== null && report.llm_ms !== undefined) meta.push("Extraction took " + (report.llm_ms / 1000).toFixed(1) + " s");
  const footer = element("div", "report-footer");
  footer.append(element("p", "report-meta", meta.join(" · ")));

  const feedback = element("p", "card-feedback");
  feedback.setAttribute("role", "status");
  const actions = element("div", "actions");
  const path = "/api/reports/" + report.id;
  if (report.status === "failed" && section === "unclassified") {
    actions.append(actionButton("Retry triage", "retry", "", () => act(report.id, "POST", path + "/retry")));
  }
  if (report.resolved) {
    actions.append(actionButton("Reopen", "reopen", "secondary", () => act(report.id, "PATCH", path, { resolved: false })));
  } else {
    // Waiting reports keep their manual completion quiet; a failed one invites it.
    const editClass = report.fields || report.status === "failed" ? "" : "secondary";
    actions.append(actionButton(report.fields ? "Edit" : "Complete by hand", "edit", editClass, () => openEditor(item, report)));
    actions.append(actionButton("Mark resolved", "resolve", "secondary", () => act(report.id, "PATCH", path, { resolved: true })));
  }
  footer.append(actions);
  item.append(footer, feedback);
  showActionState(item, report.id);
  return item;
}

function triState(value) {
  if (value === true) return "yes";
  if (value === false) return "no";
  return "";
}

function labelled(id, text, control) {
  const wrap = element("div", "edit-field");
  const label = element("label", "", text);
  label.htmlFor = id;
  control.id = id;
  wrap.append(label, control);
  return wrap;
}

function textInput(name, value) {
  const input = element("input");
  input.type = "text";
  input.name = name;
  input.value = value === null ? "" : value;
  return input;
}

function yesNoSelect(name, value) {
  const select = element("select");
  select.name = name;
  for (const [optionValue, text] of [["", "unknown"], ["yes", "yes"], ["no", "no"]]) {
    const option = element("option", "", text);
    option.value = optionValue;
    select.append(option);
  }
  select.value = triState(value);
  return select;
}

function checkbox(id, name, value, text, checked) {
  const label = element("label", "choice");
  const input = element("input");
  input.type = "checkbox";
  input.id = id;
  input.name = name;
  input.value = value;
  input.checked = checked;
  label.htmlFor = id;
  label.append(input, document.createTextNode(text));
  return label;
}

function checkboxGroup(prefix, name, legend, choices, selected) {
  const group = element("fieldset", "choices");
  group.append(element("legend", "", legend));
  for (const choice of choices) {
    const text = term(choice);
    group.append(checkbox(prefix + name + "-" + choice, name, choice, text.charAt(0).toUpperCase() + text.slice(1), selected.includes(choice)));
  }
  return group;
}

// The inline form for all nine fields, prefilled from the report's fields or,
// for a report with none, blank.
function editForm(report) {
  const fields = report.fields || BLANK_FIELDS;
  const prefix = "edit-" + report.id + "-";
  const form = element("form", "edit-form");
  form.noValidate = true;
  form.setAttribute("aria-label", "Edit report #" + report.id);
  const headcount = element("input");
  headcount.type = "number";
  headcount.name = "headcount";
  headcount.min = "0";
  headcount.step = "1";
  headcount.inputMode = "numeric";
  headcount.value = fields.headcount === null ? "" : String(fields.headcount);
  const injured = checkbox(prefix + "injured", "injured", "yes", "Injured", fields.injured);
  const summary = element("textarea");
  summary.name = "summary";
  summary.rows = 2;
  summary.value = fields.summary || "";
  const notice = element("p", "edit-notice");
  notice.setAttribute("role", "status");
  const feedback = element("p", "card-feedback");
  feedback.setAttribute("role", "status");
  const buttons = element("div", "actions");
  const save = element("button", "", "Save");
  save.type = "submit";
  const cancel = element("button", "secondary", "Cancel");
  cancel.type = "button";
  cancel.addEventListener("click", () => closeEditor(report.id));
  buttons.append(save, cancel);
  const people = element("div", "edit-grid");
  people.append(
    labelled(prefix + "location", "Location", textInput("location", fields.location)),
    labelled(prefix + "in_center", "In center", yesNoSelect("in_center", fields.in_center)),
    labelled(prefix + "on_behalf", "Reporting for others", yesNoSelect("on_behalf", fields.on_behalf)),
    labelled(prefix + "headcount", "People (blank if unknown)", headcount),
  );
  const injury = element("div", "edit-grid");
  const injuredWrap = element("div", "edit-field");
  injuredWrap.append(injured);
  injury.append(injuredWrap, labelled(prefix + "injury_note", "Injury note", textInput("injury_note", fields.injury_note)));
  form.append(
    notice,
    people,
    checkboxGroup(prefix, "vulnerable", "Vulnerable", VULNERABLE, fields.vulnerable),
    injury,
    checkboxGroup(prefix, "needs", "Needs", NEEDS, fields.needs),
    labelled(prefix + "summary", "Summary (at most 20 words)", summary),
    buttons,
    feedback,
  );
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    saveEdit(report.id);
  });
  return { form, notice, feedback };
}

// Read the form as a full field object. The server validates it; only a
// headcount the browser cannot read as a number is caught here.
function readForm(form) {
  const named = (name) => form.elements.namedItem(name);
  const fromSelect = (name) => ({ yes: true, no: false })[named(name).value] ?? null;
  const checked = (name) => [...form.querySelectorAll('input[name="' + name + '"]:checked')].map((input) => input.value);
  const headcount = named("headcount");
  if (headcount.validity.badInput) throw new Error("People must be a whole number, or blank if unknown.");
  return {
    location: named("location").value,
    in_center: fromSelect("in_center"),
    on_behalf: fromSelect("on_behalf"),
    headcount: headcount.value === "" ? null : Number(headcount.value),
    vulnerable: checked("vulnerable"),
    injured: named("injured").checked,
    injury_note: named("injury_note").value,
    needs: checked("needs"),
    summary: named("summary").value,
  };
}

// The parts of a report whose change while its form is open shows the notice.
function editorKey(report) {
  return JSON.stringify([report.status, report.fields, report.urgency, report.resolved]);
}

function openEditor(card, report) {
  if (editors.has(report.id) || (actionStates.get(report.id) || {}).pending) return;
  const editor = { report, pending: false, ...editForm(report) };
  editors.set(report.id, editor);
  actionStates.delete(report.id);
  card.querySelector(".actions").hidden = true;
  card.querySelector(".card-feedback").hidden = true;
  card.append(editor.form);
  editor.form.querySelector("input, select, textarea").focus();
}

// Close the form and show the card from the latest data (`report` when the
// hub just returned it), then focus its Edit button.
function closeEditor(id, report) {
  if (!editors.delete(id)) return;
  if (report) lastReports = lastReports.map((item) => (item.id === id ? report : item));
  const entry = cards.get(id);
  if (entry) entry.key = null;
  render(lastReports);
  const card = cards.get(id);
  const edit = card && card.node.querySelector('[data-action="edit"]');
  if (edit) edit.focus();
}

async function saveEdit(id) {
  const editor = editors.get(id);
  if (!editor || editor.pending) return;
  const showFeedback = (message, error) => {
    editor.feedback.className = error ? "card-feedback error" : "card-feedback";
    editor.feedback.textContent = message;
  };
  let fields;
  try {
    fields = readForm(editor.form);
  } catch (error) {
    showFeedback(error.message, true);
    return;
  }
  // Lock every control while saving so nothing typed now is lost when the
  // form closes; a failed save unlocks it with the values and focus kept.
  const focused = editor.form.contains(document.activeElement) ? document.activeElement : null;
  const lock = (locked) => {
    for (const control of editor.form.elements) control.disabled = locked;
  };
  editor.pending = true;
  lock(true);
  showFeedback("Saving…", false);
  let report;
  try {
    report = await send("PATCH", "/api/reports/" + id, { fields });
  } catch (error) {
    editor.pending = false;
    lock(false);
    showFeedback(error.message, true);
    if (focused) focused.focus();
    return;
  }
  closeEditor(id, report);
  await load();
}

// Keep what staff were doing on a card when a poll rebuilds it.
function carryOver(oldCard, newCard) {
  const oldOriginal = oldCard.querySelector("details.original");
  const newOriginal = newCard.querySelector("details.original");
  if (oldOriginal && newOriginal) newOriginal.open = oldOriginal.open;
  const focused = oldCard.contains(document.activeElement) && document.activeElement.dataset.action;
  if (focused) return newCard.querySelector('[data-action="' + focused + '"]');
  return null;
}

// Outline a card once; the class goes when the animation ends (with reduced
// motion there is no animation and the class is harmless).
function highlight(node) {
  node.classList.add("just-changed");
  node.addEventListener("animationend", () => node.classList.remove("just-changed"), { once: true });
}

// Put `nodes` into `list` in order, moving only nodes that are out of place.
function syncList(list, nodes) {
  nodes.forEach((node, index) => {
    if (list.children[index] !== node) list.insertBefore(node, list.children[index] || null);
  });
  while (list.children.length > nodes.length) list.lastElementChild.remove();
}

function render(reports) {
  lastReports = reports;
  const groups = { unclassified: [], triaged: [], resolved: [] };
  const seen = new Set();
  const refocus = [];
  for (const report of reports) {
    const key = JSON.stringify(report);
    let entry = cards.get(report.id);
    const editor = editors.get(report.id);
    if (editor && entry) {
      // Keep the open form (and the card's section) as staff left it.
      if (editorKey(report) !== editorKey(editor.report)) editor.notice.textContent = CHANGED_NOTICE;
      seen.add(report.id);
      groups[sectionOf(editor.report)].push(entry.node);
      continue;
    }
    if (!entry || entry.key !== key) {
      const node = reportCard(report);
      const section = sectionOf(report);
      if (entry) refocus.push(carryOver(entry.node, node));
      if (entry ? entry.section !== section : renderedOnce) highlight(node);
      entry = { node, key, section };
      cards.set(report.id, entry);
    }
    seen.add(report.id);
    groups[sectionOf(report)].push(entry.node);
  }
  for (const id of [...cards.keys()]) if (!seen.has(id)) cards.delete(id);
  // Moving a card blurs whatever is focused in it, such as an open form's field.
  const active = document.activeElement;
  for (const [name, section] of Object.entries(SECTIONS)) {
    syncList(section.list, groups[name]);
    section.count.textContent = "(" + groups[name].length + ")";
    if (section.empty) section.empty.hidden = groups[name].length > 0 || reports.length === 0;
  }
  if (active !== document.activeElement && active && active.isConnected) active.focus({ preventScroll: true });
  for (const button of refocus) if (button) button.focus();
  empty.hidden = reports.length > 0;
  applyFilters();
  renderedOnce = true;
}

function applyFilters() {
  const query = search.value.trim().toLowerCase().replace(/^#/, "");
  let shown = 0;
  let total = 0;
  for (const [id, entry] of cards) {
    const urgencyMatch = entry.section !== "triaged" || urgencyFilter === "all" || entry.node.classList.contains("urgency-" + urgencyFilter);
    const textMatch = !query || String(id) === query || entry.node.textContent.toLowerCase().includes(query);
    entry.node.hidden = !editors.has(id) && !(urgencyMatch && textMatch);
    total += 1;
    if (!entry.node.hidden) shown += 1;
  }
  const filtering = Boolean(query) || urgencyFilter !== "all";
  filterNote.hidden = !filtering;
  filterNote.textContent = filtering ? "Showing " + shown + " of " + total + " reports" : "";
}

function setUrgencyFilter(value) {
  urgencyFilter = value;
  for (const button of filterButtons) button.setAttribute("aria-pressed", String(button.dataset.filter === value));
  applyFilters();
}

for (const button of filterButtons) button.addEventListener("click", () => setUrgencyFilter(button.dataset.filter));
search.addEventListener("input", applyFilters);

function showConnection(ok) {
  if (ok) {
    connection.className = "connection ok";
    connection.textContent = "Connected. Updated " + timeText(lastSuccess) + ".";
  } else if (lastSuccess) {
    connection.className = "connection stale";
    connection.textContent = "Cannot reach the hub. Showing reports from " + timeText(lastSuccess) + "; they may be out of date.";
  } else {
    connection.className = "connection stale";
    connection.textContent = "Cannot reach the hub. No reports loaded yet.";
  }
}

async function fetchWithTimeout(path, options = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), POLL_TIMEOUT_MS);
  try {
    return await fetch(path, { ...options, cache: "no-store", signal: controller.signal });
  } finally {
    clearTimeout(timer);
  }
}

// Fetch and show the list. A response older than one already shown is dropped.
async function load() {
  const sequence = ++requested;
  try {
    const response = await fetchWithTimeout("/api/reports");
    if (!response.ok) throw new Error("HTTP " + response.status);
    const reports = await response.json();
    if (!Array.isArray(reports)) throw new Error("unexpected response");
    if (sequence < rendered) return;
    rendered = sequence;
    render(reports);
    lastSuccess = new Date();
    showConnection(true);
  } catch (error) {
    if (sequence >= rendered) showConnection(false);
  }
}

// Send a change to the hub and return the updated report. Throws an Error
// carrying the server's message (or the unreachable message) on failure.
async function send(method, path, body) {
  let response;
  try {
    response = await fetchWithTimeout(path, {
      method,
      headers: body ? { "Content-Type": "application/json" } : {},
      body: body ? JSON.stringify(body) : undefined,
    });
  } catch (error) {
    throw new Error(HUB_UNREACHABLE);
  }
  if (!response.ok) {
    let message = "The hub could not do this (HTTP " + response.status + ").";
    try {
      const data = await response.json();
      if (typeof data.detail === "string") message = data.detail;
    } catch (error) {
      // Keep the generic message.
    }
    throw new Error(message);
  }
  try {
    return await response.json();
  } catch (error) {
    return null;
  }
}

// Run one card action. While it runs, every card shown for that report (even
// one rebuilt by a poll) shows "Saving…" with its buttons disabled; the result
// goes to whichever card is current when it finishes.
async function act(id, method, path, body) {
  if ((actionStates.get(id) || {}).pending) return;
  setActionState(id, { pending: true, message: "Saving…", error: false });
  try {
    await send(method, path, body);
    await load();
    setActionState(id, { pending: false, message: "", error: false });
  } catch (error) {
    setActionState(id, { pending: false, message: error.message, error: true });
  }
}

function modelText(status) {
  if (!status.model) return "No model configured; reports stay queued for triage.";
  const checked = status.model_checked_at ? " (last checked " + timeText(new Date(status.model_checked_at)) + ")" : "";
  if (status.model_state === "loaded") return "Loaded in Ollama: " + status.model + checked;
  if (status.model_state === "not_loaded") return "Not loaded: " + status.model + checked;
  if (status.model_state === "unavailable") return "Model unavailable: " + status.model + checked;
  return "Model: " + status.model;
}

// Show the last successful status. Counts come only from that response; a
// failed or held status poll leaves them untouched and just shows a warning.
function renderStatus() {
  const status = lastStatus;
  if (!status) return;

  if (status.hub_url) {
    phoneUrl.textContent = status.hub_url;
    phoneUrl.href = status.hub_url;
    phoneUrlWrap.hidden = false;
    phoneUrlUnavailable.hidden = true;
    if (status.hub_url !== lastQrUrl) {
      lastQrUrl = status.hub_url;
      phoneQr.src = "/api/qr";
      phoneQr.hidden = false;
    }
  } else {
    phoneUrlWrap.hidden = true;
    phoneQr.hidden = true;
    if (lastQrUrl !== null) {
      lastQrUrl = null;
      phoneQr.removeAttribute("src");
    }
    phoneUrlUnavailable.hidden = false;
    phoneUrlUnavailable.textContent =
      "No phone address is shown. Open this board from the hub's Wi-Fi IPv4 address (for example http://192.168.1.20/board) or set SILONG_HUB_URL to that address.";
  }

  modelStatus.textContent = modelText(status);
  workerNote.hidden = !(status.model && status.model_state === "loaded" && !status.worker_running);
  if (!workerNote.hidden) workerNote.textContent = "The triage worker is not running.";
  audioStatus.textContent = status.audio_available ? "Audio accepted" : "Text only: audio unavailable";
  queueStatus.textContent = "Queued: " + status.queue_length;
  processingStatus.textContent = "Processing: " + status.processing_count;
  reportsToday.textContent = status.reports_today + " report(s)";
  localDate.textContent = status.local_date;

  urgencyCounts.replaceChildren();
  for (const level of URGENCY_ORDER) {
    const item = element("li", "urgency-" + level);
    item.append(element("span", "urgency-label", URGENCY_LABELS[level] || level));
    item.append(element("span", "count", String(status.urgency_counts[level] ?? 0)));
    urgencyCounts.append(item);
  }
}

function setStatusStale(stale) {
  statusNote.hidden = !stale;
  if (stale) {
    statusNote.textContent = "Status may be out of date; the values shown are the last received.";
  }
}

async function loadStatus() {
  if (statusInFlight) return;
  statusInFlight = true;
  try {
    const response = await fetchWithTimeout("/api/status");
    if (!response.ok) throw new Error("HTTP " + response.status);
    const data = await response.json();
    if (!data || typeof data !== "object" || typeof data.snapshot_at !== "string") throw new Error("unexpected response");
    lastStatus = data;
    statusEverLoaded = true;
    setStatusStale(false);
    renderStatus();
  } catch (error) {
    if (statusEverLoaded) setStatusStale(true);
  } finally {
    statusInFlight = false;
  }
}

async function pollStatus() {
  await loadStatus();
  setTimeout(pollStatus, POLL_MS);
}

async function poll() {
  await load();
  setTimeout(poll, POLL_MS);
}

poll();
pollStatus();
