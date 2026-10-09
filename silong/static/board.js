"use strict";

// Staff board: polls the hub every two seconds. After a failed poll the last
// successful view stays on screen with a visible stale warning.

const POLL_MS = 2000;
const POLL_TIMEOUT_MS = 5000;

const connection = document.getElementById("connection");
const list = document.getElementById("unclassified");
const count = document.getElementById("unclassified-count");
const empty = document.getElementById("empty");

const STATUS_LABELS = {
  queued: "Queued: waiting for triage, no urgency assigned",
  extracting: "Extracting: no urgency assigned yet",
  failed: "Triage failed: no urgency assigned",
};

let lastSuccess = null;

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

function reportCard(report) {
  const item = element("li", "report");
  const header = element("div", "report-header");
  header.append(element("span", "reference", "#" + report.id));
  header.append(element("span", "status status-" + report.status, STATUS_LABELS[report.status] || "Status: " + report.status));
  item.append(header);
  item.append(element("p", "report-text", report.raw_text));
  const meta = element("p", "report-meta");
  meta.textContent = (report.reporter ? "From " + report.reporter + " · " : "Name not given · ") + receivedText(report.created_at);
  item.append(meta);
  if (report.error) item.append(element("p", "report-error", "Error: " + report.error));
  return item;
}

function render(reports) {
  list.replaceChildren(...reports.map(reportCard));
  count.textContent = "(" + reports.length + ")";
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

async function poll() {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), POLL_TIMEOUT_MS);
  try {
    const response = await fetch("/api/reports", { cache: "no-store", signal: controller.signal });
    if (!response.ok) throw new Error("HTTP " + response.status);
    const reports = await response.json();
    if (!Array.isArray(reports)) throw new Error("unexpected response");
    render(reports);
    lastSuccess = new Date();
    showConnection(true);
  } catch (error) {
    showConnection(false);
  } finally {
    clearTimeout(timer);
    setTimeout(poll, POLL_MS);
  }
}

poll();
