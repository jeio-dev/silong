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
const MAX_AUDIO_BYTES = 20 * 1024 * 1024; // matches the hub's limit

const form = document.getElementById("report-form");
const reporterInput = document.getElementById("reporter");
const textInput = document.getElementById("text");
const audioField = document.getElementById("audio-field");
const audioInput = document.getElementById("audio");
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
let pendingRows = []; // durable rows { client_id, text, reporter, queued_at, audio? }
let unsavedItems = []; // { client_id, text, reporter, queued_at } whose save failed
let receipts = []; // { client_id, id, confirmed_at, unrecorded }
let rowErrors = new Map(); // client_id -> last failure message
let readFailed = false; // a durable read failed; pending count is unavailable
let receiptsReadFailed = false; // stored references could not be read; count is unavailable
let passInFlight = false;
let passTimer = null;
let frozenFor = null; // client_id currently frozen into the form, or null
let saving = false; // a submission's durable save has not settled yet
let feedbackFor = null; // client_id whose submission the feedback describes, or null

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

// `owner` ties the message to one submission so that only that report's
// later confirmation replaces it; any other message clears the tie.
function showFeedback(kind, lines, owner = null) {
  feedbackFor = owner;
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
  audioInput.disabled = frozen;
  sendButton.hidden = frozen;
  newReportButton.hidden = !frozen;
  frozenFor = frozen ? clientId : null;
}

function confirmedText(confirmedAt) {
  const date = new Date(confirmedAt);
  if (Number.isNaN(date.getTime())) return null;
  const time = date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  return "Confirmed / Natanggap " + time;
}

function preview(item) {
  const oneLine = (item.text || "").replace(/\s+/g, " ").trim();
  const text = oneLine.length > PREVIEW_CHARS ? oneLine.slice(0, PREVIEW_CHARS) + "…" : oneLine;
  if (!item.audio) return text;
  return text ? "[Voice recording] " + text : "[Voice recording]";
}

function showReceived(id) {
  showFeedback("success", [
    "Received by the hub. / Natanggap na.",
    { text: "Reference #" + id, className: "reference" },
    "Show this number to staff if you are asked about your report.",
  ]);
}

// Lock the form while a submission's save settles, so one tap is one report
// (one UUID) and nothing typed meanwhile is cleared with the submitted text.
function setSaving(on) {
  saving = on;
  sendButton.disabled = on;
  reporterInput.readOnly = on || frozenFor !== null;
  textInput.readOnly = on || frozenFor !== null;
  audioInput.disabled = on || frozenFor !== null;
}

// The storage note lists every storage problem that is still current.
function renderStorageNote() {
  const messages = [];
  if (readFailed) messages.push("Pending reports could not be read on this phone right now. Reports are not lost.");
  if (receiptsReadFailed) {
    messages.push("References received earlier could not be read on this phone right now. They are not lost.");
  }
  if (unsavedItems.length) {
    messages.push(
      "A report could not be saved on this phone. It is sent once right away; if that fails there is no automatic retry."
    );
  }
  const unrecorded = receipts.filter((receipt) => receipt.unrecorded);
  if (unrecorded.length) {
    const resent = unrecorded.some((receipt) => pendingRows.some((row) => row.client_id === receipt.client_id));
    messages.push(
      "The hub received a report, but this phone could not record the reference." +
        (resent ? " The report is safe and will be sent again." : "")
    );
  }
  storageNote.textContent = messages.join(" ");
  storageNote.className = messages.length ? "warning" : "";
  storageNote.hidden = !messages.length;
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
  const waiting = pendingRows.length + unsavedItems.length;
  pendingCount.textContent = readFailed ? "unavailable" : "(" + waiting + ")";
  // Nothing to retry once the queue is known to be empty.
  retryNowButton.hidden = !readFailed && waiting === 0;
  pendingList.replaceChildren();
  receivedList.replaceChildren();

  for (const item of unsavedItems) {
    const card = element("li", "report unsaved");
    const header = element("div", "report-header");
    header.append(element("span", "status", "Not saved on this phone"));
    card.append(header, element("p", "report-text", preview(item)));
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
    card.append(element("p", "report-text", preview(row)));
    const error = rowErrors.get(row.client_id);
    if (error) card.append(element("p", "report-meta", "Sending failed: " + error));
    pendingList.append(card);
  }

  // Newest confirmation first.
  for (const receipt of receipts.slice().sort((a, b) => (a.confirmed_at < b.confirmed_at ? 1 : -1))) {
    const card = element("li", "report receipt");
    card.append(element("span", "reference", "Reference #" + receipt.id));
    const confirmed = confirmedText(receipt.confirmed_at);
    if (confirmed) card.append(element("p", "report-meta", confirmed));
    if (receipt.unrecorded) {
      card.append(
        element("p", "report-meta", "Confirmed by the hub, but could not be saved on this phone; it may send again.")
      );
    }
    receivedList.append(card);
  }

  receivedCount.textContent = receiptsReadFailed ? "unavailable" : "(" + receipts.length + ")";
  renderStorageNote();
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
  if (payload.audio) body.append("audio", payload.audio, payload.audio_name || "recording");
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
      // Only a successful reply with a positive integer reference confirms receipt.
      if (response.ok && Number.isInteger(data.id) && data.id > 0) id = data.id;
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
    } catch (error) {
      unrecorded = true;
    }
    upsertReceipt(row.client_id, result.id, unrecorded);
    // With a failed acknowledgement the durable pending row is still there and
    // is resent later; only a committed acknowledgement removes it.
    if (!unrecorded) removeDurable(row.client_id);
    if (feedbackFor === row.client_id) showReceived(result.id);
  } else {
    rowErrors.set(row.client_id, failureMessage(result));
  }
  renderQueues();
}

async function deliverRow(row) {
  const result = await serialized(() => postReport(row));
  await handleDelivered(row, result);
}

// Re-read stored references (also picks up those another tab recorded). On
// failure keep what is known and show the count as unavailable until a read
// works; known confirmations not yet in storage are kept either way.
async function refreshReceipts() {
  let stored;
  try {
    stored = await Outbox.listReceipts();
  } catch (error) {
    receiptsReadFailed = true;
    return;
  }
  receiptsReadFailed = false;
  const storedIds = new Set(stored.map((receipt) => receipt.client_id));
  receipts = stored
    .map((receipt) => ({
      client_id: receipt.client_id,
      id: receipt.id,
      confirmed_at: receipt.confirmed_at,
      unrecorded: false,
    }))
    .concat(receipts.filter((receipt) => !storedIds.has(receipt.client_id)));
}

async function runPass() {
  if (passInFlight) return;
  passInFlight = true;
  try {
    let rows = null;
    try {
      rows = await Outbox.listPending();
      readFailed = false;
    } catch (error) {
      readFailed = true;
    }
    await refreshReceipts();
    // Another tab may have confirmed the report this feedback describes.
    const confirmed = receipts.find((receipt) => receipt.client_id === feedbackFor);
    if (confirmed) showReceived(confirmed.id);
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
    audioInput.value = "";
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
}

// A submitted report whose durable save failed: one immediate send, then no
// automatic retry until a later save succeeds. Like durable delivery, its
// outcome only replaces feedback that still describes this report.
async function handleSaveFailure(payload) {
  unsavedItems.push(payload);
  setFrozen(true, payload.client_id);
  renderQueues();
  const result = await serialized(() => postReport(payload));
  if (result.id !== null) {
    await confirmUnsaved(payload, result);
    if (feedbackFor === payload.client_id) {
      showReceived(result.id);
      textInput.focus();
    }
  } else {
    rowErrors.set(payload.client_id, failureMessage(result));
    if (feedbackFor === payload.client_id) {
      showFeedback(
        "error",
        [
          "Not saved on this phone; not confirmed; no automatic retry. / Hindi na-save sa telepono; hindi kumpirmado; walang awtomatikong pag-retry.",
          "Your text is kept below. Try again when this phone's storage works, or press the button on the report above.",
        ],
        payload.client_id
      );
    }
    renderQueues();
  }
}

async function retryUnsaved(item) {
  if (!unsavedItems.some((each) => each.client_id === item.client_id)) return;
  showFeedback("info", ["Trying again… / Sinusubukan muli…"], item.client_id);
  try {
    await Outbox.save(item);
  } catch (error) {
    // Storage still failing: one more immediate send with the same UUID.
    const result = await serialized(() => postReport(item));
    if (result.id !== null) {
      await confirmUnsaved(item, result);
      if (feedbackFor === item.client_id) showReceived(result.id);
    } else {
      rowErrors.set(item.client_id, failureMessage(result));
      if (feedbackFor === item.client_id) {
        showFeedback("error", ["Still not saved or confirmed. / Hindi pa rin na-save o kumpirmado."], item.client_id);
      }
    }
    renderQueues();
    return;
  }
  // Save now worked: the item becomes a normal durable pending report.
  const current = feedbackFor === item.client_id;
  clearUnsaved(item.client_id);
  pendingRows.push(item);
  renderQueues();
  if (current) {
    showFeedback(
      "success",
      ["Saved on this phone; waiting to send. / Na-save sa teleponong ito; naghihintay na maipadala."],
      item.client_id
    );
    textInput.focus();
  }
  triggerPassNow();
}

async function submitReport() {
  // One report per submission: ignore repeats while its save settles, and a
  // frozen unsaved report is retried with its own button, never resubmitted.
  if (saving || frozenFor !== null) return;
  const audio = !audioField.hidden && audioInput.files.length ? audioInput.files[0] : null;
  if (!textInput.value.trim() && !audio) {
    showFeedback("error", ["Please write what is happening. / Isulat kung ano ang nangyayari."]);
    textInput.focus();
    return;
  }
  if (audio && audio.size > MAX_AUDIO_BYTES) {
    showFeedback("error", ["The recording is too large (limit 20 MB). Please record a shorter message."]);
    return;
  }
  const payload = {
    client_id: uuidV4(),
    text: textInput.value,
    reporter: reporterInput.value.trim() || null,
    queued_at: Date.now(),
  };
  if (audio) {
    // Stored in IndexedDB as a Blob so the recording retries like text does.
    payload.audio = audio;
    payload.audio_name = audio.name || "recording";
  }
  showFeedback("info", ["Saving… / Nagse-save…"], payload.client_id);
  setSaving(true);
  try {
    await Outbox.save(payload);
  } catch (error) {
    setSaving(false);
    await handleSaveFailure(payload);
    return;
  }
  setSaving(false);
  pendingRows.push(payload);
  textInput.value = ""; // the name is kept for the next report
  audioInput.value = "";
  rowErrors.delete(payload.client_id);
  renderQueues();
  showFeedback(
    "success",
    ["Saved on this phone; waiting to send. / Na-save sa teleponong ito; naghihintay na maipadala."],
    payload.client_id
  );
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
  audioInput.value = "";
  showFeedback("info", ["The unsent report stays listed above under “Waiting to send”. Now write a new report below."]);
  textInput.focus();
});

retryNowButton.addEventListener("click", () => {
  triggerPassNow();
});

// Each pass reads pending reports and stored references before sending.
runPass();

// Show the recording field only when the hub says it can transcribe audio.
// Until a check succeeds the form stays text-only; a failed check is retried.
async function checkAudio() {
  try {
    const response = await fetch("/api/status", { cache: "no-store" });
    const status = await response.json();
    audioField.hidden = !(response.ok && status.audio_available === true);
  } catch (error) {
    setTimeout(checkAudio, 30000);
  }
}
checkAudio();
