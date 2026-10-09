"use strict";

// Staff board: polls the hub every two seconds. After a failed poll the last
// successful view stays on screen with a visible stale warning. Cards are kept
// by report id and rebuilt only when that report changes, so an expanded
// original survives later polls. Action progress and messages are kept by
// report id too, so a card rebuilt while its action runs still shows them. The server sends reports
// in board order; the board only splits them into sections and never re-sorts.

const POLL_MS = 2000;
const POLL_TIMEOUT_MS = 5000;

const connection = document.getElementById("connection");
const empty = document.getElementById("empty");
const SECTIONS = {
  unclassified: { list: document.getElementById("unclassified"), count: document.getElementById("unclassified-count") },
  triaged: { list: document.getElementById("triaged"), count: document.getElementById("triaged-count") },
  resolved: { list: document.getElementById("resolved"), count: document.getElementById("resolved-count") },
};

// Unclassified reports never carry an urgency word.
const STATUS_LABELS = {
  queued: "Queued: waiting for triage, no urgency assigned",
  extracting: "Extracting: no urgency assigned yet",
  failed: "Triage failed: no urgency assigned",
};
const URGENCY_LABELS = { critical: "Critical", high: "High", medium: "Medium", low: "Low" };
const TERMS = { pwd: "PWD", milk_formula: "milk formula" };
const HUB_UNREACHABLE = "Could not reach the hub. Please try again.";

const cards = new Map(); // report id -> { node, key }
const actionStates = new Map(); // report id -> { pending, message, error }
let lastSuccess = null;
let requested = 0;
let rendered = 0;

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

function terms(values) {
  return values.length ? values.map((value) => TERMS[value] || value).join(", ") : "none stated";
}

function sectionOf(report) {
  if (report.resolved) return "resolved";
  return report.urgency ? "triaged" : "unclassified";
}

function fieldList(fields) {
  const rows = [
    ["Location", fields.location === null ? "unknown" : fields.location],
    ["In center", yesNo(fields.in_center)],
    ["Reporting for others", yesNo(fields.on_behalf)],
    ["People", fields.headcount === null ? "unknown" : String(fields.headcount)],
    ["Vulnerable", terms(fields.vulnerable)],
    ["Injured", fields.injured ? "yes" + (fields.injury_note ? ": " + fields.injury_note : "") : "no"],
    ["Needs", terms(fields.needs)],
  ];
  if (fields.summary) rows.push(["Summary", fields.summary]);
  const list = element("dl", "fields");
  for (const [name, value] of rows) {
    const row = element("div", "field-row");
    row.append(element("dt", "", name), element("dd", "", value));
    list.append(row);
  }
  return list;
}

function original(report, collapsed) {
  const text = element("p", "report-text", report.raw_text);
  if (!collapsed) return text;
  const details = element("details", "original");
  details.append(element("summary", "", "Original report"), text);
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
  header.append(element("span", "reference", "#" + report.id));

  if (report.urgency) {
    const label = URGENCY_LABELS[report.urgency] || report.urgency;
    item.classList.add("urgency-" + report.urgency);
    header.append(element("span", "urgency-label", label));
    const reason = report.urgency_reason || "";
    header.append(element("span", "reason", reason.startsWith(label + ": ") ? reason.slice(label.length + 2) : reason));
  } else {
    header.append(element("span", "status status-" + report.status, STATUS_LABELS[report.status] || "Status: " + report.status));
  }
  item.append(header);

  if (report.urgency && report.fields) item.append(fieldList(report.fields));
  const notes = [];
  if (report.edited) notes.push("Edited by staff");
  if (report.llm_ms !== null && report.llm_ms !== undefined) notes.push("Extraction took " + (report.llm_ms / 1000).toFixed(1) + " s");
  if (notes.length) item.append(element("p", "report-notes", notes.join(" · ")));

  item.append(original(report, section !== "unclassified"));
  const meta = element("p", "report-meta");
  meta.textContent = (report.reporter ? "From " + report.reporter + " · " : "Name not given · ") + receivedText(report.created_at);
  item.append(meta);
  if (report.error && !report.urgency) item.append(element("p", "report-error", "Error: " + report.error));

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
    actions.append(actionButton("Mark resolved", "resolve", "secondary", () => act(report.id, "PATCH", path, { resolved: true })));
  }
  item.append(actions, feedback);
  showActionState(item, report.id);
  return item;
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

// Put `nodes` into `list` in order, moving only nodes that are out of place.
function syncList(list, nodes) {
  nodes.forEach((node, index) => {
    if (list.children[index] !== node) list.insertBefore(node, list.children[index] || null);
  });
  while (list.children.length > nodes.length) list.lastElementChild.remove();
}

function render(reports) {
  const groups = { unclassified: [], triaged: [], resolved: [] };
  const seen = new Set();
  const refocus = [];
  for (const report of reports) {
    const key = JSON.stringify(report);
    let entry = cards.get(report.id);
    if (!entry || entry.key !== key) {
      const node = reportCard(report);
      if (entry) refocus.push(carryOver(entry.node, node));
      entry = { node, key };
      cards.set(report.id, entry);
    }
    seen.add(report.id);
    groups[sectionOf(report)].push(entry.node);
  }
  for (const id of [...cards.keys()]) if (!seen.has(id)) cards.delete(id);
  for (const [name, section] of Object.entries(SECTIONS)) {
    syncList(section.list, groups[name]);
    section.count.textContent = "(" + groups[name].length + ")";
  }
  for (const button of refocus) if (button) button.focus();
  empty.hidden = reports.length > 0;
}

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

// Run one card action. While it runs, every card shown for that report (even
// one rebuilt by a poll) shows "Saving…" with its buttons disabled; the result
// goes to whichever card is current when it finishes.
async function act(id, method, path, body) {
  if ((actionStates.get(id) || {}).pending) return;
  setActionState(id, { pending: true, message: "Saving…", error: false });
  try {
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
    await load();
    setActionState(id, { pending: false, message: "", error: false });
  } catch (error) {
    setActionState(id, { pending: false, message: error.message, error: true });
  }
}

async function poll() {
  await load();
  setTimeout(poll, POLL_MS);
}

poll();
