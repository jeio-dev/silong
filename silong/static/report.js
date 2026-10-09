"use strict";

// Phone report form with a durable IndexedDB outbox (Outbox, loaded first).
//
// Submissions are saved on this phone before any network send. While the tab
// stays open, a single sender (one request at a time) drains the durable
// pending queue oldest-first and retries every five seconds between passes.
// The hub is idempotent per client_id, so a lost response or two tabs sending
// the same row still produce one report and one reference. A report whose save
// fails is sent exactly once immediately and, if that fails, is never put into
// the automatic loop until a later save succeeds.

const SEND_TIMEOUT_MS = 20000;
const RETRY_MS = 5000;
const PREVIEW_CHARS = 80;

const form = document.getElementById("report-form");
const reporterInput = document.getElementById("reporter");
const textInput = document.getElementById("text");
const sendButton = document.getElementById("send");
const newReportButton = document.getElementById("new-report");
const feedback = document.getElementById("feedback");
const pendingCount = document.getElementById("pending-count");
const receivedCount = document.getElementById("received-count");
const pendingList = document.getElementById("pending-list");
const receivedList = document.getElementById("received-list");
const storageNote = document.getElementById("storage-note");
const retryNowButton = document.getElementById("retry-now");

// In-memory state, refreshed from IndexedDB on each send pass.
let pendingRows = []; // durable rows { client_id, text, reporter, queued_at }
let unsavedItems = []; // { client_id, text, reporter, queued_at } whose save failed
let receipts = []; // { client_id, id, confirmed_at, unrecorded }
let rowErrors = new Map(); // client_id -> last failure message
let readFailed = false; // a durable read failed; pending count is unavailable
let passInFlight = false;
let passTimer = null;
let frozenFor = null; // client_id currently frozen into the form, or null

// One sender per tab: every request is serialized through this chain.
let chain = Promise.resolve();
function serialized(task) {
  const run = chain.then(task, task);
  chain = run.then(() => {}, () => {});
  return run;
}

// UUID v4 from getRandomValues, which works over plain local HTTP.
function uuidV4() {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
  return [hex.slice(0, 8), hex.slice(8, 12), hex.slice(12, 16), hex.slice(16, 20), hex.slice(20)].join("-");
}

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function showFeedback(kind, lines) {
  feedback.className = "feedback " + kind;
  feedback.replaceChildren();
  for (const line of lines) {
    const paragraph = element("p");
    if (typeof line === "string") {
      paragraph.textContent = line;
    } else {
      paragraph.textContent = line.text;
      paragraph.className = line.className;
    }
    feedback.append(paragraph);
  }
}

function setFrozen(frozen, clientId) {
  reporterInput.readOnly = frozen;
  textInput.readOnly = frozen;
  sendButton.hidden = frozen;
  newReportButton.hidden = !frozen;
  frozenFor = frozen ? clientId : null;
}

function preview(text) {
  const oneLine = text.replace(/\s+/g, " ").trim();
  return oneLine.length > PREVIEW_CHARS ? oneLine.slice(0, PREVIEW_CHARS) + "…" : oneLine;
}

function setStorageWarning(message) {
  storageNote.textContent = message || "";
  if (message) {
    storageNote.className = "warning";
    storageNote.hidden = false;
  } else {
    storageNote.className = "";
    storageNote.hidden = true;
  }
}

function upsertReceipt(clientId, id, unrecorded) {
  const existing = receipts.find((receipt) => receipt.client_id === clientId);
  if (existing) {
    existing.id = id;
    existing.unrecorded = unrecorded;
    return;
  }
  receipts.push({ client_id: clientId, id, confirmed_at: new Date().toISOString(), unrecorded });
}

function removeDurable(clientId) {
  pendingRows = pendingRows.filter((row) => row.client_id !== clientId);
  rowErrors.delete(clientId);
}

function renderQueues() {
  pendingCount.textContent = readFailed ? "unavailable" : "(" + (pendingRows.length + unsavedItems.length) + ")";
  pendingList.replaceChildren();
  receivedList.replaceChildren();

  for (const item of unsavedItems) {
    const card = element("li", "report unsaved");
    const header = element("div", "report-header");
    header.append(element("span", "status", "Not saved on this phone"));
    card.append(header, element("p", "report-text", preview(item.text)));
    const error = rowErrors.get(item.client_id);
    if (error) card.append(element("p", "report-error", error));
    const actions = element("div", "actions");
    const retry = element("button", "", "Try again / Subukan ulit");
    retry.type = "button";
    retry.addEventListener("click", () => retryUnsaved(item));
    actions.append(retry);
    card.append(actions);
    pendingList.append(card);
  }

  for (const row of pendingRows) {
    const card = element("li", "report pending");
    card.append(element("p", "report-text", preview(row.text)));
    const error = rowErrors.get(row.client_id);
    if (error) card.append(element("p", "report-meta", "Sending failed: " + error));
    pendingList.append(card);
  }

  for (const receipt of receipts.slice().sort((a, b) => (a.confirmed_at < b.confirmed_at ? -1 : 1))) {
    const card = element("li", "report receipt");
    card.append(element("span", "reference", "Reference #" + receipt.id));
    if (receipt.unrecorded) {
      card.append(
        element("p", "report-meta", "Confirmed by the hub, but could not be saved on this phone; it may send again.")
      );
    }
    receivedList.append(card);
  }

  receivedCount.textContent = "(" + receipts.length + ")";
}

function failureMessage(result) {
  if (result.status === 422) return result.detail || "The hub could not accept this report.";
  if (result.status === 503) return result.detail || "The hub could not save the report right now.";
  if (result.status === 0) return "Could not reach the hub. Check the Wi-Fi connection.";
  if (result.status >= 200 && result.status < 300) return "The hub did not return a reference number.";
  return "The hub did not confirm the report (HTTP " + result.status + ").";
}

// Send `payload` once and report the outcome. Never rejects.
async function postReport(payload) {
  const body = new FormData();
  body.append("client_id", payload.client_id);
  body.append("text", payload.text);
  if (payload.reporter) body.append("reporter", payload.reporter);
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), SEND_TIMEOUT_MS);
  let status = 0;
  let id = null;
  let detail = null;
  try {
    const response = await fetch("/api/reports", { method: "POST", body, signal: controller.signal });
    status = response.status;
    let data = null;
    try {
      data = await response.json();
    } catch (error) {
      data = null;
    }
    if (data && typeof data === "object") {
      if (Number.isInteger(data.id)) id = data.id;
      if (typeof data.detail === "string") detail = data.detail;
    }
  } catch (error) {
    status = 0;
  } finally {
    clearTimeout(timer);
  }
  return { status, id, detail };
}

// A durable pending row was answered by the hub in some way; record it.
async function handleDelivered(row, result) {
  if (result.id !== null) {
    let unrecorded = false;
    try {
      await Outbox.acknowledge(row.client_id, result.id);
      setStorageWarning(null);
    } catch (error) {
      unrecorded = true;
      setStorageWarning(
        "The hub received this report, but this phone could not record the reference." +
          " The report is safe and will be sent again."
      );
    }
    upsertReceipt(row.client_id, result.id, unrecorded);
    // With a failed acknowledgement the durable pending row is still there and
    // is resent later; only a committed acknowledgement removes it.
    if (!unrecorded) removeDurable(row.client_id);
  } else {
    rowErrors.set(row.client_id, failureMessage(result));
  }
  renderQueues();
}

async function deliverRow(row) {
  const result = await serialized(() => postReport(row));
  await handleDelivered(row, result);
}

async function runPass() {
  if (passInFlight) return;
  passInFlight = true;
  try {
    let rows = null;
    try {
      rows = await Outbox.listPending();
      readFailed = false;
      setStorageWarning(null);
    } catch (error) {
      readFailed = true;
      setStorageWarning("Pending reports could not be read on this phone right now. Reports are not lost.");
    }
    if (rows) {
      pendingRows = rows;
      renderQueues();
      for (const row of pendingRows) {
        await deliverRow(row);
      }
    } else {
      renderQueues();
    }
  } finally {
    passInFlight = false;
    schedulePass();
  }
}

function schedulePass() {
  if (passTimer !== null) return;
  passTimer = setTimeout(() => {
    passTimer = null;
    runPass();
  }, RETRY_MS);
}

function triggerPassNow() {
  if (passInFlight) return;
  if (passTimer !== null) {
    clearTimeout(passTimer);
    passTimer = null;
  }
  runPass();
}

// Remove an unsaved item and give the form back to a fresh report when the
// item was the one frozen into it.
function clearUnsaved(clientId) {
  unsavedItems = unsavedItems.filter((item) => item.client_id !== clientId);
  rowErrors.delete(clientId);
  if (frozenFor === clientId) {
    setFrozen(false, null);
    textInput.value = "";
  }
}

// Confirm an unsaved item after a successful send, recording the reference.
async function confirmUnsaved(payload, result) {
  let unrecorded = false;
  try {
    await Outbox.acknowledge(payload.client_id, result.id);
  } catch (error) {
    unrecorded = true;
  }
  clearUnsaved(payload.client_id);
  upsertReceipt(payload.client_id, result.id, unrecorded);
  renderQueues();
  return unrecorded;
}

// A submitted report whose durable save failed: one immediate send, then no
// automatic retry until a later save succeeds.
async function handleSaveFailure(payload) {
  unsavedItems.push(payload);
  setFrozen(true, payload.client_id);
  setStorageWarning(
    "This report could not be saved on this phone. It will be sent once now; if that fails there is no automatic retry."
  );
  renderQueues();
  const result = await serialized(() => postReport(payload));
  if (result.id !== null) {
    const unrecorded = await confirmUnsaved(payload, result);
    if (!unrecorded) setStorageWarning(null);
    showFeedback("success", [
      "Received by the hub. / Natanggap na.",
      { text: "Reference #" + result.id, className: "reference" },
      "Show this number to staff if you are asked about your report.",
    ]);
    textInput.focus();
  } else {
    rowErrors.set(payload.client_id, failureMessage(result));
    showFeedback("error", [
      "Not saved on this phone; not confirmed; no automatic retry. / Hindi na-save sa telepono; hindi kumpirmado; walang awtomatikong pag-retry.",
      "Your text is kept below. Try again when this phone's storage works, or press the button on the report above.",
    ]);
    renderQueues();
  }
}

async function retryUnsaved(item) {
  if (!unsavedItems.some((each) => each.client_id === item.client_id)) return;
  showFeedback("info", ["Trying again… / Sinusubukan muli…"]);
  try {
    await Outbox.save(item);
  } catch (error) {
    // Storage still failing: one more immediate send with the same UUID.
    const result = await serialized(() => postReport(item));
    if (result.id !== null) {
      await confirmUnsaved(item, result);
      showFeedback("success", ["Received by the hub. / Natanggap na.", { text: "Reference #" + result.id, className: "reference" }]);
    } else {
      rowErrors.set(item.client_id, failureMessage(result));
      showFeedback("error", ["Still not saved or confirmed. / Hindi pa rin na-save o kumpirmado."]);
    }
    renderQueues();
    return;
  }
  // Save now worked: the item becomes a normal durable pending report.
  clearUnsaved(item.client_id);
  pendingRows.push(item);
  setStorageWarning(null);
  renderQueues();
  showFeedback("success", ["Saved on this phone; waiting to send. / Na-save sa teleponong ito; naghihintay na maipadala."]);
  triggerPassNow();
  textInput.focus();
}

async function submitReport() {
  if (!textInput.value.trim()) {
    showFeedback("error", ["Please write what is happening. / Isulat kung ano ang nangyayari."]);
    textInput.focus();
    return;
  }
  const payload = {
    client_id: uuidV4(),
    text: textInput.value,
    reporter: reporterInput.value.trim() || null,
    queued_at: Date.now(),
  };
  showFeedback("info", ["Saving… / Nagse-save…"]);
  try {
    await Outbox.save(payload);
  } catch (error) {
    await handleSaveFailure(payload);
    return;
  }
  pendingRows.push(payload);
  textInput.value = ""; // the name is kept for the next report
  rowErrors.delete(payload.client_id);
  setStorageWarning(null);
  renderQueues();
  showFeedback("success", [
    "Saved on this phone; waiting to send. / Na-save sa teleponong ito; naghihintay na maipadala.",
  ]);
  triggerPassNow();
  textInput.focus();
}

form.addEventListener("submit", (event) => {
  event.preventDefault();
  submitReport();
});

newReportButton.addEventListener("click", () => {
  setFrozen(false, null);
  textInput.value = "";
  showFeedback("info", ["The unsent report stays listed above under “Waiting to send”. Now write a new report below."]);
  textInput.focus();
});

retryNowButton.addEventListener("click", () => {
  triggerPassNow();
});

async function init() {
  try {
    const stored = await Outbox.listReceipts();
    receipts = stored.map((receipt) => ({
      client_id: receipt.client_id,
      id: receipt.id,
      confirmed_at: receipt.confirmed_at,
      unrecorded: false,
    }));
  } catch (error) {
    setStorageWarning("References received earlier could not be read on this phone right now.");
  }
  renderQueues();
  runPass();
}

init();