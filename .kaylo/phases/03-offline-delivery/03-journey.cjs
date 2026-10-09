"use strict";

// Phase 03 T5 integrated journey (evidence script; not part of the test gate).
// Run from the repository root: node .kaylo/phases/03-offline-delivery/03-journey.cjs
// Overrides: PLAYWRIGHT_MODULE, CHROMIUM_EXECUTABLE, CHROMIUM_LIB_DIR,
// JOURNEY_LAN_IP (default: first `hostname -I` address), JOURNEY_OUT (screenshots).
// Real hub (temporary SQLite DB) reached at this host's LAN IPv4 address, a
// fake local Ollama whose first extraction fails and later ones are held,
// a phone-sized report page and a desktop board. Synthetic reports only.
// Prints measured observations as JSON and exits non-zero on a failed check.

const assert = require("node:assert");
const fs = require("node:fs");
const http = require("node:http");
const net = require("node:net");
const path = require("node:path");
const { spawn, execFileSync } = require("node:child_process");

const PLAYWRIGHT_MODULE = process.env.PLAYWRIGHT_MODULE || "/tmp/aarista-chart-verify/node_modules/playwright";
const CHROMIUM_EXECUTABLE =
  process.env.CHROMIUM_EXECUTABLE || "/home/jeio/.cache/ms-playwright/chromium-1248/chrome-linux64/chrome";
const CHROMIUM_LIB_DIR = process.env.CHROMIUM_LIB_DIR || "/tmp/aarista-chart-verify/libs/root/usr/lib/x86_64-linux-gnu";
const ROOT = path.resolve(__dirname, "..", "..", "..");
const PYTHON = path.join(ROOT, ".venv", "bin", "python");
const OUT = process.env.JOURNEY_OUT || require("node:os").tmpdir();
const LAN_IP = process.env.JOURNEY_LAN_IP || execFileSync("hostname", ["-I"], { encoding: "utf8" }).trim().split(/\s+/)[0];
const { chromium } = require(PLAYWRIGHT_MODULE);

const observed = { lan_ip: LAN_IP, steps: [] };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const note = (step, data) => { observed.steps.push({ step, ...data }); };

function freePort() {
  const s = net.createServer();
  return new Promise((r) => s.listen(0, "0.0.0.0", () => { const p = s.address().port; s.close(() => r(p)); }));
}

function py(code, ...args) {
  return execFileSync(PYTHON, ["-c", code, ...args], { encoding: "utf8", cwd: ROOT });
}
function dbRows(db) {
  return JSON.parse(py(
    "import sqlite3,json,sys;c=sqlite3.connect(sys.argv[1]);c.row_factory=sqlite3.Row;" +
    "print(json.dumps([dict(r) for r in c.execute('SELECT id,client_id,status,urgency,resolved,edited FROM reports ORDER BY id')]))", db));
}

function request(method, url, body, headers = {}) {
  return new Promise((resolve, reject) => {
    const started = Date.now();
    const req = http.request(url, { method, headers }, (res) => {
      const chunks = [];
      res.on("data", (c) => chunks.push(c));
      res.on("end", () => resolve({ ms: Date.now() - started, status: res.statusCode, body: Buffer.concat(chunks) }));
    });
    req.on("error", reject);
    req.end(body);
  });
}

async function main() {
  // Fake local Ollama. /api/ps lists the configured model (or is held when
  // psHeld). /api/chat: warm-up (no messages) answers at once; the first
  // extraction gets HTTP 500, every later extraction is held (never answered).
  const ollamaPort = await freePort();
  let psHeld = false;
  let extractions = 0;
  const heldResponses = [];
  const ollama = http.createServer((req, res) => {
    let body = "";
    req.on("data", (c) => (body += c));
    req.on("end", () => {
      if (req.url === "/api/ps") {
        if (psHeld) { heldResponses.push(res); return; }
        res.writeHead(200, { "Content-Type": "application/json" });
        res.end(JSON.stringify({ models: [{ name: "gemma4:e2b" }] }));
        return;
      }
      if (req.url === "/api/chat") {
        const messages = JSON.parse(body || "{}").messages || [];
        if (messages.length === 0) {
          res.writeHead(200, { "Content-Type": "application/json" });
          res.end(JSON.stringify({ message: { role: "assistant", content: "" } }));
          return;
        }
        extractions += 1;
        if (extractions === 1) {
          res.writeHead(500, { "Content-Type": "application/json" });
          res.end(JSON.stringify({ error: "synthetic extractor failure" }));
          return;
        }
        heldResponses.push(res);
        return;
      }
      res.writeHead(404); res.end();
    });
  });
  await new Promise((r) => ollama.listen(ollamaPort, "127.0.0.1", r));

  const port = await freePort();
  const dbDir = fs.mkdtempSync("/tmp/silong-t5-journey-");
  const db = path.join(dbDir, "hub.db");
  const env = { ...process.env, SILONG_PORT: String(port), SILONG_DB: db, SILONG_MODEL: "gemma4:e2b",
    OLLAMA_HOST: "http://127.0.0.1:" + ollamaPort };
  delete env.SILONG_HUB_URL;
  const hub = spawn(PYTHON, ["-m", "silong"], { cwd: ROOT, env, stdio: ["ignore", "pipe", "pipe"] });
  let hubLog = "";
  hub.stdout.on("data", (c) => (hubLog += c));
  hub.stderr.on("data", (c) => (hubLog += c));
  const origin = "http://" + LAN_IP + ":" + port;
  for (let i = 0; ; i++) {
    try { const r = await request("GET", origin + "/api/reports"); if (r.status === 200) break; } catch (e) { /* not up */ }
    if (i > 100) throw new Error("hub did not start: " + hubLog);
    await sleep(150);
  }
  observed.hub = { origin, db_dir: dbDir, env: { SILONG_MODEL: "gemma4:e2b", SILONG_HUB_URL: "(unset)", OLLAMA_HOST: "fake local server" } };

  const browser = await chromium.launch({ executablePath: CHROMIUM_EXECUTABLE, headless: true,
    env: fs.existsSync(path.join(CHROMIUM_LIB_DIR, "libnspr4.so")) ? { ...process.env, LD_LIBRARY_PATH: CHROMIUM_LIB_DIR } : process.env });
  observed.browser = "Chromium " + browser.version();
  const errors = [];
  const foreign = [];
  const watch = (page, name) => {
    page.on("pageerror", (e) => errors.push(name + ": " + e.message));
    page.on("request", (r) => { if (!r.url().startsWith(origin + "/")) foreign.push(name + ": " + r.url()); });
  };

  const boardCtx = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const board = await boardCtx.newPage();
  watch(board, "board");
  const phoneCtx = await browser.newContext({ viewport: { width: 412, height: 915 }, isMobile: true, hasTouch: true });
  const phone = await phoneCtx.newPage();
  watch(phone, "phone");

  const text = (page, sel) => page.evaluate((s) => document.querySelector(s)?.textContent ?? null, sel);
  const waitFor = (page, fn, arg, timeout = 15000) => page.waitForFunction(fn, arg, { timeout, polling: 50 });
  const cardVisible = (id) => waitFor(board, (n) => document.querySelector('li.report[data-id="' + n + '"]') !== null, id, 8000);
  const pendingIds = () => phone.evaluate(() => Outbox.listPending().then((rows) => rows.map((r) => r.client_id)));

  // Phone POST outcomes as observed by the browser.
  const posts = [];
  phone.on("requestfinished", async (r) => {
    if (r.method() === "POST" && r.url().endsWith("/api/reports")) {
      const resp = await r.response();
      posts.push({ at: Date.now(), status: resp && resp.status(), client_id: new URLSearchParams(r.postData() || "").get("client_id") });
    }
  });
  phone.on("requestfailed", (r) => {
    if (r.method() === "POST" && r.url().endsWith("/api/reports")) posts.push({ at: Date.now(), failed: r.failure()?.errorText });
  });

  // 1. Board at the LAN address: URL, QR, readiness.
  await board.goto(origin + "/board");
  await waitFor(board, () => document.querySelector("#connection")?.textContent.includes("Connected"));
  await waitFor(board, () => document.querySelector("#model-status")?.textContent.includes("Loaded in Ollama"));
  const shownUrl = await board.getAttribute("#phone-url", "href");
  assert.strictEqual(shownUrl, origin + "/", "board shows the LAN request origin as the phone URL");
  await waitFor(board, () => { const i = document.querySelector("#phone-qr"); return i && !i.hidden && i.complete && i.naturalWidth > 0; });
  const qrServed = (await request("GET", origin + "/api/qr")).body;
  const qrExpected = Buffer.from(py("import sys;from silong import status;sys.stdout.write(status.make_qr_svg(sys.argv[1]).decode())", shownUrl));
  assert.ok(qrServed.equals(qrExpected), "served QR equals qrcode output for the displayed URL");
  const qrLoopback = await request("GET", "http://127.0.0.1:" + port + "/api/qr");
  const statusLoopback = JSON.parse((await request("GET", "http://127.0.0.1:" + port + "/api/status")).body);
  note("board_url_qr", { shown_url: shownUrl, model_status: await text(board, "#model-status"), audio: await text(board, "#audio-status"),
    qr_bytes: qrServed.length, qr_matches_regenerated: true, qr_has_remote_ref: /https?:\/\/(?!www\.w3\.org)/.test(qrServed.toString()),
    loopback_qr_status: qrLoopback.status, loopback_hub_url: statusLoopback.hub_url });

  // 2. Report A: receipt, then the extractor fails; board shows it as failed.
  await phone.goto(origin + "/");
  await waitFor(phone, () => typeof window.Outbox !== "undefined" && document.querySelector("#pending-count")?.textContent === "(0)");
  async function submit(body) {
    await phone.fill("#text", body);
    await phone.click("#send");
  }
  await submit("Synthetic test A: may sugatan sa Room 4, kailangan ng tulong.");
  await waitFor(phone, () => document.querySelector("#received-list")?.textContent.includes("Reference #1"));
  const aPostAt = posts.at(-1).at;
  await cardVisible(1);
  const aCardMs = Date.now() - aPostAt;
  await waitFor(board, () => document.querySelector('li.report[data-id="1"] .status')?.textContent.includes("failed"), null, 8000);
  note("report_A_failed", { feedback_after_receipt: await text(phone, "#feedback"), received_list: await text(phone, "#received-list"), card_after_receipt_ms: aCardMs,
    board_label: await text(board, 'li.report[data-id="1"] .status'), board_error: await text(board, 'li.report[data-id="1"] .report-error') });

  // 3. Report B: receipt while its extraction is held; board shows it processing.
  await submit("Synthetic test B: walang tubig ang pamilya sa Room 6.");
  await waitFor(phone, () => document.querySelector("#received-list")?.textContent.includes("Reference #2"));
  const bPostAt = posts.at(-1).at;
  const bReceiptMs = posts.at(-1).at; void bReceiptMs;
  await cardVisible(2);
  const bCardMs = Date.now() - bPostAt;
  await waitFor(board, () => document.querySelector("#processing-status")?.textContent === "Processing: 1");
  note("report_B_held", { extraction_requests_seen: extractions, held_open: heldResponses.length, card_after_receipt_ms: bCardMs,
    board_label: await text(board, 'li.report[data-id="2"] .status'), processing: await text(board, "#processing-status") });

  // 4. Hold the board's status poll and Ollama /api/ps; phone goes offline.
  let statusHeld = true;
  await board.route("**/api/status", (route) => { if (statusHeld) return; return route.continue(); });
  psHeld = true;
  const statusWhilePsHeld = request("GET", origin + "/api/status");
  const receiptWhilePsHeld = request("POST", origin + "/api/reports",
    "client_id=00000000-0000-4000-8000-0000000000f1&text=" + encodeURIComponent("Synthetic test F: direct receipt while status probe held."),
    { "Content-Type": "application/x-www-form-urlencoded" });
  const [sHeld, rHeld] = await Promise.all([statusWhilePsHeld, receiptWhilePsHeld]);
  const sHeldBody = JSON.parse(sHeld.body);
  note("probe_held", { status_ms: sHeld.ms, model_state: sHeldBody.model_state, receipt_ms: rHeld.ms, receipt_status: rHeld.status,
    receipt_id: JSON.parse(rHeld.body).id });
  const fId = JSON.parse(rHeld.body).id;
  await cardVisible(fId);

  await phoneCtx.setOffline(true);
  const offlineAt = Date.now();
  await submit("Synthetic test C: dalawang matanda sa labas, baha na.");
  await waitFor(phone, () => document.querySelector("#pending-count")?.textContent === "(1)");
  const cFeedback = await text(phone, "#feedback");
  await submit("Synthetic test D: kailangan ng gatas para sa sanggol sa Room 2.");
  await waitFor(phone, () => document.querySelector("#pending-count")?.textContent === "(2)");
  const offlineIds = await pendingIds();
  await sleep(6500);
  const draftKept = await phone.evaluate(() => { const t = document.querySelector("#text"); t.value = "draft not sent"; t.focus(); return true; });
  await sleep(5500);
  const failedAttempts = posts.filter((p) => p.at > offlineAt && p.failed).length;
  note("offline_pending", { feedback_after_submit: cFeedback, pending_ids: offlineIds, held_offline_ms: Date.now() - offlineAt,
    pending_count_after: await text(phone, "#pending-count"), failed_post_attempts_while_offline: failedAttempts,
    draft_and_focus_kept: draftKept && (await phone.evaluate(() => document.activeElement?.id === "text" && document.querySelector("#text").value === "draft not sent")),
    pending_errors: await phone.evaluate(() => Array.from(document.querySelectorAll("#pending-list li"), (li) => li.textContent)),
    hub_rows_for_pending: dbRows(db).filter((r) => offlineIds.includes(r.client_id)).length });
  assert.strictEqual(offlineIds.length, 2);

  // 5. Reload attempts: offline (page cannot load), then online but POSTs blocked.
  let offlineReload = "loaded";
  try { await phone.reload({ timeout: 5000 }); } catch (e) { offlineReload = e.message.split("\n")[0]; }
  await phoneCtx.setOffline(false);
  let blockPosts = true;
  await phone.route("**/api/reports", (route) => {
    if (route.request().method() === "POST" && blockPosts) return route.abort("connectionrefused");
    return route.continue();
  });
  await phone.goto(origin + "/");
  await waitFor(phone, () => document.querySelector("#pending-count")?.textContent === "(2)");
  const restoredIds = await pendingIds();
  const restoredList = await phone.evaluate(() => Array.from(document.querySelectorAll("#pending-list li"), (li) => li.textContent));
  assert.deepStrictEqual(restoredIds.slice().sort(), offlineIds.slice().sort(), "same UUIDs after reload");
  blockPosts = false;
  const reconnectAt = Date.now();
  await waitFor(phone, () => document.querySelector("#pending-count")?.textContent === "(0)", null, 20000);
  const drainMs = Date.now() - reconnectAt;
  const rowsAfter = dbRows(db);
  const offlineRows = rowsAfter.filter((r) => offlineIds.includes(r.client_id));
  const cdIds = offlineRows.map((r) => r.id);
  const lastPostAt = posts.filter((p) => p.status === 200).at(-1).at;
  for (const id of cdIds) await cardVisible(id);
  const cdCardMs = Date.now() - lastPostAt;
  note("reconnect", { offline_reload: offlineReload, restored_ids: restoredIds, restored_list: restoredList, drain_after_unblock_ms: drainMs,
    rows_per_uuid: offlineIds.map((id) => offlineRows.filter((r) => r.client_id === id).length), row_ids: cdIds,
    received_list: await phone.evaluate(() => Array.from(document.querySelectorAll("#received-list li"), (li) => li.textContent)),
    cards_visible_after_last_receipt_ms_with_status_held: cdCardMs,
    board_status_note: await text(board, "#status-note") });
  assert.deepStrictEqual(offlineRows.length, 2);

  // 6. Lost response: the hub commits, the phone never sees the answer.
  let dropped = 0;
  await phone.unroute("**/api/reports");
  await phone.route("**/api/reports", async (route) => {
    if (route.request().method() === "POST" && dropped === 0) {
      dropped += 1;
      await route.fetch();
      return route.abort("connectionreset");
    }
    return route.continue();
  });
  const lostAt = Date.now();
  await submit("Synthetic test E: may lagnat ang bata sa Room 9.");
  await waitFor(phone, () => document.querySelector("#pending-count")?.textContent === "(1)");
  const eId = (await pendingIds())[0];
  await waitFor(phone, () => document.querySelector("#pending-count")?.textContent === "(0)", null, 20000);
  const eRows = dbRows(db).filter((r) => r.client_id === eId);
  const eReceipt = await phone.evaluate((cid) => Outbox.listReceipts().then((rs) => rs.find((r) => r.client_id === cid)), eId);
  note("lost_response", { dropped_responses: dropped, confirm_after_drop_ms: Date.now() - lostAt,
    post_attempts: posts.filter((p) => p.at >= lostAt).length, hub_rows: eRows.length, row_id: eRows[0]?.id, receipt_id: eReceipt?.id });
  assert.strictEqual(eRows.length, 1);
  assert.strictEqual(eReceipt.id, eRows[0].id);
  await cardVisible(eRows[0].id);

  // 7. Release the status hold; header counts must match the database.
  statusHeld = false;
  await board.unroute("**/api/status");
  psHeld = false;
  const released = Date.now();
  await waitFor(board, () => document.querySelector("#status-note")?.hidden === true, null, 10000);
  await waitFor(board, () => document.querySelector("#queue-status")?.textContent === "Queued: 4", null, 10000);
  const headerAfter = {
    queue: await text(board, "#queue-status"), processing: await text(board, "#processing-status"),
    today: await text(board, "#reports-today"), urgency: await text(board, "#urgency-counts"), model: await text(board, "#model-status"),
  };
  const statusNow = JSON.parse((await request("GET", origin + "/api/status")).body);
  const rowsNow = dbRows(db);
  note("counts", { header_recovered_ms: Date.now() - released, header: headerAfter,
    api: { queue_length: statusNow.queue_length, processing_count: statusNow.processing_count, reports_today: statusNow.reports_today,
      urgency_counts: statusNow.urgency_counts, local_date: statusNow.local_date },
    db_statuses: rowsNow.map((r) => r.id + ":" + r.status) });
  assert.strictEqual(statusNow.queue_length, rowsNow.filter((r) => r.status === "queued").length);
  assert.strictEqual(statusNow.processing_count, rowsNow.filter((r) => r.status === "extracting").length);
  assert.strictEqual(statusNow.reports_today, rowsNow.length);

  // 8. Staff workflow on failed report #1: complete by hand, edit, resolve.
  await board.click('li.report[data-id="1"] [data-action="edit"]');
  await board.waitForSelector('li.report[data-id="1"] .edit-form');
  await board.selectOption("#edit-1-in_center", "no");
  await board.check("#edit-1-needs-rescue");
  await board.click('li.report[data-id="1"] .edit-form button[type="submit"]');
  await waitFor(board, () => document.querySelector('#triaged li.report[data-id="1"] .urgency-label')?.textContent === "Critical", null, 10000);
  const afterComplete = { label: await text(board, '#triaged li.report[data-id="1"] .urgency-label'), reason: await text(board, '#triaged li.report[data-id="1"] .reason'),
    notes: await text(board, '#triaged li.report[data-id="1"] .report-notes') };
  await waitFor(board, () => document.querySelector("#urgency-counts")?.textContent.includes("Critical1"), null, 10000);
  const urgencyAfterComplete = await text(board, "#urgency-counts");

  await board.click('li.report[data-id="1"] [data-action="edit"]');
  await board.waitForSelector('li.report[data-id="1"] .edit-form');
  await board.uncheck("#edit-1-needs-rescue");
  await board.check("#edit-1-needs-water");
  await board.click('li.report[data-id="1"] .edit-form button[type="submit"]');
  await waitFor(board, () => document.querySelector('#triaged li.report[data-id="1"] .urgency-label')?.textContent === "Medium", null, 10000);
  const afterEdit = { label: await text(board, '#triaged li.report[data-id="1"] .urgency-label'), reason: await text(board, '#triaged li.report[data-id="1"] .reason') };
  await waitFor(board, () => document.querySelector("#urgency-counts")?.textContent.includes("Critical0") &&
    document.querySelector("#urgency-counts")?.textContent.includes("Medium1"), null, 10000);
  const urgencyAfterEdit = await text(board, "#urgency-counts");

  await board.click('li.report[data-id="1"] [data-action="resolve"]');
  await waitFor(board, () => document.querySelector('#resolved li.report[data-id="1"]') !== null, null, 10000);
  await waitFor(board, () => document.querySelector("#urgency-counts")?.textContent.includes("Medium0"), null, 10000);
  note("staff_workflow", { after_complete: afterComplete, urgency_after_complete: urgencyAfterComplete, after_edit: afterEdit,
    urgency_after_edit: urgencyAfterEdit, urgency_after_resolve: await text(board, "#urgency-counts"),
    db_row1: dbRows(db).find((r) => r.id === 1), today_after: await text(board, "#reports-today") });

  // 9. Screenshots for same-host layout inspection.
  await board.screenshot({ path: path.join(OUT, "journey-board-1280.png"), fullPage: true });
  await phone.screenshot({ path: path.join(OUT, "journey-phone-412.png"), fullPage: true });
  observed.page_errors = errors;
  observed.non_hub_requests = foreign;
  observed.hub_log_failure_line = hubLog.split("\n").find((l) => l.includes("failed extraction")) || null;
  assert.deepStrictEqual(errors, []);
  assert.deepStrictEqual(foreign, []);

  console.log(JSON.stringify(observed, null, 2));
  await browser.close();
  hub.kill();
  for (const r of heldResponses) r.destroy();
  ollama.close();
  fs.rmSync(dbDir, { recursive: true, force: true });
}

main().catch((e) => { console.error(e); console.error(JSON.stringify(observed, null, 2)); process.exit(1); });
