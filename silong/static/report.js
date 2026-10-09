"use strict";

// Phone report form. A submission keeps one client_id until the hub
// confirms it, so a retry after an uncertain send cannot create a duplicate.

const SEND_TIMEOUT_MS = 20000;

const form = document.getElementById("report-form");
const reporterInput = document.getElementById("reporter");
const textInput = document.getElementById("text");
const sendButton = document.getElementById("send");
const retryButton = document.getElementById("retry");
const newReportButton = document.getElementById("new-report");
const feedback = document.getElementById("feedback");

// The submission awaiting confirmation: { client_id, text, reporter }, or null.
let pending = null;
let sending = false;

// UUID v4 from getRandomValues, which works over plain local HTTP
// (crypto.randomUUID needs a secure context).
function uuidV4() {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
  return [hex.slice(0, 8), hex.slice(8, 12), hex.slice(12, 16), hex.slice(16, 20), hex.slice(20)].join("-");
}

function showFeedback(kind, lines) {
  feedback.className = "feedback " + kind;
  feedback.replaceChildren();
  for (const line of lines) {
    const p = document.createElement("p");
    if (typeof line === "string") {
      p.textContent = line;
    } else {
      p.textContent = line.text;
      p.className = line.className;
    }
    feedback.append(p);
  }
}

// Freeze the entered report while it is pending, so a retry sends exactly it.
function setFrozen(frozen) {
  reporterInput.readOnly = frozen;
  textInput.readOnly = frozen;
  sendButton.hidden = frozen;
  retryButton.hidden = !frozen;
  newReportButton.hidden = !frozen;
}

function setSending(value) {
  sending = value;
  sendButton.disabled = value;
  retryButton.disabled = value;
  newReportButton.disabled = value;
  form.setAttribute("aria-busy", String(value));
}

async function send() {
  if (sending || !pending) return;
  setSending(true);
  setFrozen(true);
  showFeedback("info", ["Sending… / Ipinapadala…"]);

  const body = new FormData();
  body.append("client_id", pending.client_id);
  body.append("text", pending.text);
  if (pending.reporter) body.append("reporter", pending.reporter);

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), SEND_TIMEOUT_MS);
  let response = null;
  let result = null;
  try {
    response = await fetch("/api/reports", { method: "POST", body, signal: controller.signal });
    result = await response.json();
  } catch (error) {
    result = null;
  } finally {
    clearTimeout(timer);
    setSending(false);
  }

  if (response && response.ok && result && Number.isInteger(result.id)) {
    pending = null;
    textInput.value = "";
    setFrozen(false);
    showFeedback("success", [
      "Received by the hub. / Natanggap na.",
      { text: "Reference #" + result.id, className: "reference" },
      "Show this number to staff if you are asked about your report.",
    ]);
    feedback.focus();
    return;
  }

  if (response && response.status === 422) {
    // The hub rejected this submission and stored nothing; let the reporter edit it.
    pending = null;
    setFrozen(false);
    const detail = result && typeof result.detail === "string" ? result.detail : "The hub could not accept this report.";
    showFeedback("error", ["Not sent. / Hindi naipadala.", detail]);
    feedback.focus();
    return;
  }

  // No confirmation: the hub may or may not have the report. Keep the same
  // submission so Try again cannot create a second copy.
  showFeedback("error", [
    "Not confirmed. Your report may not have reached the hub. / Hindi pa kumpirmado.",
    "Your text is kept below. Check that you are on the center's Wi-Fi, then press Try again.",
  ]);
  feedback.focus();
}

form.addEventListener("submit", (event) => {
  event.preventDefault();
  if (sending || pending) return;
  if (!textInput.value.trim()) {
    showFeedback("error", ["Please write what is happening. / Isulat kung ano ang nangyayari."]);
    textInput.focus();
    return;
  }
  pending = {
    client_id: uuidV4(),
    text: textInput.value,
    reporter: reporterInput.value.trim(),
  };
  send();
});

retryButton.addEventListener("click", send);

newReportButton.addEventListener("click", () => {
  if (sending) return;
  // Abandons the unconfirmed submission; the next send gets a new client_id.
  pending = null;
  textInput.value = "";
  setFrozen(false);
  showFeedback("info", ["Your previous report was not confirmed. Write a new report below."]);
  textInput.focus();
});
