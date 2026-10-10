"use strict";

// Browser verification of the phone intake end to end: a real hub (temp
// SQLite, no model) plus Chromium with real IndexedDB, with POST delivery
// controlled through Playwright route interception. Synthetic reports only.
//
// Run: node tests/browser_delivery.cjs
// Overrides: PLAYWRIGHT_MODULE, CHROMIUM_EXECUTABLE, CHROMIUM_LIB_DIR.

const assert = require("node:assert");
const fs = require("node:fs");
const http = require("node:http");
const net = require("node:net");
const path = require("node:path");
const { spawn, execFileSync } = require("node:child_process");

const PLAYWRIGHT_MODULE =
  process.env.PLAYWRIGHT_MODULE || "/tmp/aarista-chart-verify/node_modules/playwright";
const CHROMIUM_EXECUTABLE =
  process.env.CHROMIUM_EXECUTABLE ||
  "/home/jeio/.cache/ms-playwright/chromium-1248/chrome-linux64/chrome";
const CHROMIUM_LIB_DIR =
  process.env.CHROMIUM_LIB_DIR || "/tmp/aarista-chart-verify/libs/root/usr/lib/x86_64-linux-gnu";

const ROOT = path.resolve(__dirname, "..");
const PYTHON = path.join(ROOT, ".venv", "bin", "python");

let playwright;
try {
  playwright = require(PLAYWRIGHT_MODULE);
} catch (error) {
  console.error("Playwright is unavailable at " + PLAYWRIGHT_MODULE + "; set PLAYWRIGHT_MODULE.");
  process.exit(2);
}

function freePort() {
  const server = net.createServer();
  return new Promise((resolve) => {
    server.listen(0, "127.0.0.1", () => {
      const port = server.address().port;
      server.close(() => resolve(port));
    });
  });
}

function dbQuery(db, sql) {
  const code =
    "import sqlite3,json,sys;c=sqlite3.connect(sys.argv[1]);print(json.dumps(c.execute(sys.argv[2]).fetchall()))";
  return JSON.parse(execFileSync(PYTHON, ["-c", code, db, sql], { encoding: "utf8" }));
}

function uuidFromPostData(body) {
  const match = body && body.match(/name="client_id"\r\n\r\n([0-9a-f-]{36})/i);
  return match ? match[1] : null;
}

async function waitText(page, selector, text, timeout = 12000) {
  await page.waitForFunction(
    ({ sel, want }) => {
      const node = document.querySelector(sel);
      return node && node.textContent.includes(want);
    },
    { sel: selector, want: text },
    { timeout }
  );
}

async function main() {
  const port = await freePort();
  const db = path.join(fs.mkdtempSync(path.join("/tmp", "silong-delivery-")), "hub.db");
  const env = { ...process.env, SILONG_PORT: String(port), SILONG_DB: db };
  delete env.SILONG_MODEL;
  delete env.OLLAMA_HOST;
  const hub = spawn(PYTHON, ["-m", "silong"], { cwd: ROOT, env, stdio: ["ignore", "pipe", "pipe"] });
  let hubLog = "";
  hub.stdout.on("data", (chunk) => (hubLog += chunk));
  hub.stderr.on("data", (chunk) => (hubLog += chunk));
  const origin = "http://127.0.0.1:" + port;

  async function waitReady() {
    const deadline = Date.now() + 15000;
    while (Date.now() < deadline) {
      try {
        await new Promise((resolve, reject) => {
          const req = http.get(origin + "/api/reports", (res) => {
            res.resume();
            res.statusCode === 200 ? resolve() : reject(new Error("status " + res.statusCode));
          });
          req.on("error", reject);
        });
        return;
      } catch (error) {
        await new Promise((resolve) => setTimeout(resolve, 150));
      }
    }
    throw new Error("hub did not start: " + hubLog);
  }

  const browser = await playwright.chromium.launch({
    executablePath: CHROMIUM_EXECUTABLE,
    headless: true,
    env: fs.existsSync(path.join(CHROMIUM_LIB_DIR, "libnspr4.so"))
      ? { ...process.env, LD_LIBRARY_PATH: CHROMIUM_LIB_DIR }
      : process.env,
  });

  try {
    await waitReady();
    const context = await browser.newContext({ viewport: { width: 412, height: 915 } });
    const pageErrors = [];
    const seenUrls = [];

    // Delivery control: route POST /api/reports through a mode switch. The same
    // handler is installed on every page so two tabs share one control point.
    // zeroId | negativeId | errorWithId reply with an id that is not a valid
    // confirmation (zero, negative, or on an error status).
    let mode = "pass"; // pass | hold | abort | fail503 | fail422 | malformed | commitThen500 | zeroId | negativeId | errorWithId
    let postCount = 0;
    let commitThen500Left = 0;
    const held = [];
    const postIds = []; // client_id of every POST, in order
    function installRoutes(pg) {
      return pg.route("**/api/reports", async (route) => {
        if (route.request().method() !== "POST") return route.continue();
        postCount += 1;
        postIds.push(uuidFromPostData(route.request().postData()));
        if (mode === "hold") {
          held.push(route);
          return;
        }
        if (mode === "abort") return route.abort();
        if (mode === "fail503")
          return route.fulfill({ status: 503, contentType: "application/json", body: '{"detail":"hub busy"}' });
        if (mode === "fail422")
          return route.fulfill({ status: 422, contentType: "application/json", body: '{"detail":"bad report"}' });
        if (mode === "malformed")
          return route.fulfill({ status: 200, contentType: "application/json", body: "not-json" });
        if (mode === "zeroId")
          return route.fulfill({ status: 200, contentType: "application/json", body: '{"id":0}' });
        if (mode === "negativeId")
          return route.fulfill({ status: 200, contentType: "application/json", body: '{"id":-5}' });
        if (mode === "errorWithId")
          return route.fulfill({ status: 503, contentType: "application/json", body: '{"id":123,"detail":"hub busy"}' });
        if (mode === "commitThen500" && commitThen500Left > 0) {
          commitThen500Left -= 1;
          await route.fetch(); // the hub commits; the response is then lost
          return route.fulfill({ status: 500, contentType: "application/json", body: '{"detail":"lost"}' });
        }
        return route.continue();
      });
    }

    const contextPage = async (setup) => {
      const page = await context.newPage();
      page.on("pageerror", (error) => pageErrors.push(error.message));
      page.on("request", (request) => seenUrls.push(request.url()));
      await installRoutes(page);
      if (setup) await setup(page);
      await page.goto(origin + "/");
      await page.waitForFunction(() => typeof window.Outbox !== "undefined");
      return page;
    };
    const page = await contextPage();

    async function releaseHeld() {
      const pending = held.splice(0);
      for (const each of pending) await each.continue();
    }

    async function submit(text, reporter) {
      await page.fill("#text", text);
      if (reporter) await page.fill("#reporter", reporter);
      await page.click("#send");
    }

    async function pendingClientIds() {
      return page.evaluate(() => window.Outbox.listPending().then((rows) => rows.map((row) => row.client_id)));
    }

    async function waitFor(check, what, timeout = 12000) {
      const deadline = Date.now() + timeout;
      while (!check()) {
        if (Date.now() > deadline) throw new Error("timed out waiting for " + what);
        await new Promise((resolve) => setTimeout(resolve, 50));
      }
    }

    async function storedReceiptIds(pg = page) {
      return pg.evaluate(() => window.Outbox.listReceipts().then((rows) => rows.map((row) => row.id)));
    }

    async function receivedTotal(pg = page) {
      return Number((await pg.locator("#received-count").textContent()).replace(/\D/g, ""));
    }

    // --- 1. save-before-send: the row is durable while the send is held. ---
    mode = "hold";
    await submit("Lola ko sa Room 3, walang gatas ang baby.", "Ana");
    await waitText(page, "#feedback", "Saved on this phone; waiting to send");
    assert.strictEqual((await pendingClientIds()).length, 1, "saved durably before the send returned");
    assert.ok(postCount >= 1, "the send was attempted");
    assert.strictEqual(await page.locator("#pending-count").textContent(), "(1)");
    await releaseHeld();
    mode = "pass";
    await waitText(page, "#received-list", "Reference #1");
    assert.strictEqual(await page.locator("#pending-count").textContent(), "(0)");

    // --- 2. offline: multiple pending, retention through a reload, reconnect. ---
    mode = "abort";
    await submit("Kailangan ng tubing sa Room 5, tatlong bata.");
    await submit("May sugat ang tatay ko sa paa, kailangan ng gamot.");
    await waitText(page, "#pending-count", "(2)");
    const offlineIds = await pendingClientIds();
    assert.strictEqual(offlineIds.length, 2, "two saved pending reports");
    await waitText(page, "#pending-list", "Could not reach the hub");
    const postsBeforeReload = postCount;
    await new Promise((resolve) => setTimeout(resolve, 6000)); // > 5 s retry interval
    assert.ok(postCount > postsBeforeReload, "retries run while offline");

    await page.reload();
    await page.waitForFunction(() => typeof window.Outbox !== "undefined");
    await waitText(page, "#pending-count", "(2)", 10000);
    const afterReloadIds = await pendingClientIds();
    assert.deepStrictEqual(afterReloadIds.sort(), offlineIds.slice().sort(), "same UUIDs survive reload");

    mode = "pass";
    await waitText(page, "#received-list", "Reference #", 15000);
    await page.waitForFunction(
      () => document.querySelectorAll("#received-list .report").length >= 3,
      null,
      { timeout: 15000 }
    );
    await page.waitForFunction(() => document.querySelector("#pending-count").textContent === "(0)");
    const offlineRows = dbQuery(db, "SELECT client_id, raw_text FROM reports ORDER BY id");
    assert.strictEqual(offlineRows.length, 3, "one hub row per submission");
    for (const id of offlineIds) {
      assert.ok(offlineRows.some((row) => row[0] === id), "server stored the same client_id: " + id);
    }

    // --- 3. lost response after commit is re-sent safely (deduplicated). ---
    commitThen500Left = 1;
    mode = "commitThen500";
    await submit("Nasira ang bahay namin sa Purok 2.");
    await waitText(page, "#pending-count", "(1)", 10000);
    await waitText(page, "#pending-list", "did not confirm");
    commitThen500Left = 0;
    mode = "pass";
    await waitText(page, "#received-list", "Reference #", 15000);
    await page.waitForFunction(() => document.querySelector("#pending-count").textContent === "(0)");
    const lostRows = dbQuery(db, "SELECT client_id FROM reports");
    assert.strictEqual(lostRows.length, 4, "lost-response retry created no duplicate row");

    // --- 4. two tabs sending the same row does not duplicate it. ---
    mode = "hold";
    await submit("Sunog sa tabi ng barangay hall, kailangan ng tulong.");
    const tab1Ids = await pendingClientIds();
    const tab1Id = tab1Ids[tab1Ids.length - 1];
    const page2 = await contextPage();
    await page2.waitForFunction(
      () => document.querySelector("#pending-count") && document.querySelector("#pending-count").textContent.includes("1"),
      null,
      { timeout: 10000 }
    );
    await waitText(page2, "#pending-list", "Sunog");
    await releaseHeld();
    mode = "pass";
    await waitText(page, "#received-list", "Reference #", 15000);
    await waitText(page2, "#received-list", "Reference #", 15000);
    await page.waitForFunction(() => document.querySelector("#pending-count").textContent === "(0)");
    const fireCount = dbQuery(db, "SELECT COUNT(*) FROM reports WHERE client_id = '" + tab1Id + "'")[0][0];
    assert.strictEqual(fireCount, 1, "two tabs produced exactly one hub row");
    await page2.close(); // leave a single active sender for the remaining tests

    // --- 5. server failures stay visible and unconfirmed, then drain. ---
    mode = "fail503";
    await submit("Walang kuryente sa Room 1.");
    await waitText(page, "#pending-count", "(1)", 10000);
    await waitText(page, "#pending-list", "hub busy");
    mode = "fail422";
    await submit("Puno na ang palikuran sa Room 2.");
    await waitText(page, "#pending-count", "(2)", 10000);
    await waitText(page, "#pending-list", "bad report");
    mode = "malformed";
    await submit("Nasira ang bubong sa Room 9.");
    await waitText(page, "#pending-count", "(3)", 10000);
    await waitText(page, "#pending-list", "did not return a reference");
    mode = "pass";
    await page.waitForFunction(() => document.querySelector("#pending-count").textContent === "(0)", null, { timeout: 20000 });

    // --- 6. a failed durable save sends once and never auto-retries. ---
    await page.evaluate(() => {
      const original = window.Outbox.save;
      window.Outbox.save = function failOnce() {
        window.Outbox.save = original;
        return Promise.reject(new Error("QuotaExceededError"));
      };
    });
    postCount = 0;
    mode = "abort";
    await submit("Kulang na sa pagkain ang Room 6.");
    await waitText(page, "#feedback", "Not saved on this phone", 10000);
    await waitText(page, "#pending-list", "Not saved on this phone");
    const postsAfterFail = postCount;
    assert.strictEqual(postsAfterFail, 1, "exactly one immediate send after a failed save");
    await new Promise((resolve) => setTimeout(resolve, 6500));
    assert.strictEqual(postCount, postsAfterFail, "no automatic retry for the unsaved report");
    assert.strictEqual(await page.locator("#pending-count").textContent(), "(1)");
    assert.ok(await page.locator("#text").getAttribute("readonly") !== null, "form is frozen");

    mode = "pass";
    await page.click("#pending-list button");
    await waitText(page, "#received-list", "Reference #", 15000);
    await page.waitForFunction(() => document.querySelector("#pending-count").textContent === "(0)");

    // --- 7. a failed acknowledgement keeps the reference and retries safely. ---
    await page.evaluate(() => {
      const original = window.Outbox.acknowledge;
      window.Outbox.acknowledge = function failOnce() {
        window.Outbox.acknowledge = original;
        return Promise.reject(new Error("QuotaExceededError"));
      };
    });
    await submit("Walang diaper para sa sanggol sa Room 4.");
    await waitText(page, "#received-list", "Reference #", 10000);
    await waitText(page, "#storage-note", "could not record the reference");
    assert.ok(
      await page.evaluate(() => document.querySelector("#received-list").textContent.includes("may send again")),
      "receipt is marked as unrecordable"
    );
    mode = "hold"; // hold the scheduled safe resend so the durable row is observable
    await new Promise((resolve) => setTimeout(resolve, 5500));
    assert.strictEqual((await pendingClientIds()).length, 1, "durable pending row is kept for a safe resend");
    await releaseHeld();
    mode = "pass";
    await page.waitForFunction(() => document.querySelector("#pending-count").textContent === "(0)", null, { timeout: 20000 });
    const diaperNote = await page.evaluate(() => document.querySelector("#received-list").textContent.includes("may send again"));
    assert.strictEqual(diaperNote, false, "later acknowledgement cleared the warning");
    const diaperCount = dbQuery(db, "SELECT COUNT(*) FROM reports WHERE raw_text LIKE '%diaper%'")[0][0];
    assert.strictEqual(diaperCount, 1, "safe resend did not duplicate the report");

    // --- 8. R1: an id is a confirmation only on success and only when positive. ---
    postCount = 0;
    mode = "zeroId";
    await submit("R1 synthetic: may tumutulo sa bubong ng Room 7.");
    await waitFor(() => postCount >= 1, "zero-id reply");
    await waitText(page, "#pending-list", "did not return a reference");
    mode = "errorWithId";
    await page.click("#retry-now");
    await waitText(page, "#pending-list", "hub busy");
    mode = "negativeId";
    await page.click("#retry-now");
    await waitText(page, "#pending-list", "did not return a reference");
    assert.strictEqual(await page.locator("#pending-count").textContent(), "(1)", "invalid replies keep the report pending");
    assert.strictEqual((await pendingClientIds()).length, 1, "invalid replies keep the durable payload");
    let received = await page.locator("#received-list").textContent();
    for (const bad of ["Reference #0", "Reference #-5", "Reference #123"]) {
      assert.ok(!received.includes(bad), "no false reference: " + bad);
    }
    let stored = await storedReceiptIds();
    assert.ok(!stored.some((id) => id <= 0 || id === 123), "no false reference stored: " + stored.join(","));
    assert.ok(!(await page.locator("#feedback").textContent()).includes("Received by the hub"));
    mode = "pass";
    await page.waitForFunction(() => document.querySelector("#pending-count").textContent === "(0)", null, { timeout: 20000 });
    assert.strictEqual(dbQuery(db, "SELECT COUNT(*) FROM reports WHERE raw_text LIKE 'R1 synthetic:%'")[0][0], 1);

    // R1 on the storage-failure path: the immediate send and the manual retry.
    await page.evaluate(() => {
      const original = window.Outbox.save;
      let left = 2;
      window.Outbox.save = function failTwice(record) {
        if (left <= 0) return original(record);
        left -= 1;
        if (left === 0) window.Outbox.save = original;
        return Promise.reject(new Error("QuotaExceededError"));
      };
    });
    mode = "errorWithId";
    await submit("R1 synthetic fallback: walang tubig sa Room 8.");
    await waitText(page, "#feedback", "Not saved on this phone", 10000);
    mode = "zeroId";
    await page.click("#pending-list button");
    await waitText(page, "#feedback", "Still not saved or confirmed", 10000);
    await waitText(page, "#pending-list", "Not saved on this phone");
    received = await page.locator("#received-list").textContent();
    assert.ok(!received.includes("Reference #0") && !received.includes("Reference #123"), "fallback recorded no false reference");
    stored = await storedReceiptIds();
    assert.ok(!stored.some((id) => id <= 0 || id === 123), "fallback stored no false reference");
    assert.ok(await page.locator("#text").getAttribute("readonly") !== null, "unsaved report stays frozen");
    mode = "pass";
    await page.click("#pending-list button"); // save works again: becomes a durable report
    await waitText(page, "#feedback", "Received by the hub", 15000);
    await page.waitForFunction(() => document.querySelector("#pending-count").textContent === "(0)", null, { timeout: 15000 });
    assert.strictEqual(dbQuery(db, "SELECT COUNT(*) FROM reports WHERE raw_text LIKE 'R1 synthetic fallback:%'")[0][0], 1);

    // --- 9. R2: repeated submission during a held save is one report and one UUID. ---
    await page.evaluate(() => {
      const original = window.Outbox.save;
      window.__saveCalls = 0;
      window.Outbox.save = function heldSave(record) {
        window.__saveCalls += 1;
        window.Outbox.save = original;
        return new Promise((resolve) => (window.__releaseSave = resolve)).then(() => original(record));
      };
    });
    const r2Start = postIds.length;
    await page.fill("#text", "R2 synthetic: dalawang beses pinindot, isang ulat lang.");
    await page.evaluate(() => {
      const form = document.getElementById("report-form");
      form.requestSubmit();
      form.requestSubmit();
      document.getElementById("send").click();
    });
    await waitText(page, "#feedback", "Saving");
    assert.strictEqual(await page.evaluate(() => window.__saveCalls), 1, "one save for repeated taps");
    assert.ok(await page.evaluate(() => document.getElementById("send").disabled), "send is locked while saving");
    await page.focus("#text");
    await page.keyboard.type(" extra");
    assert.strictEqual(
      await page.inputValue("#text"),
      "R2 synthetic: dalawang beses pinindot, isang ulat lang.",
      "the submitted snapshot cannot be edited while its save settles"
    );
    await page.evaluate(() => window.__releaseSave());
    await waitText(page, "#feedback", "Received by the hub", 15000);
    assert.strictEqual(await page.inputValue("#text"), "", "submitted text is cleared after its save");
    assert.ok(await page.evaluate(() => !document.getElementById("send").disabled && !document.getElementById("text").readOnly));
    assert.strictEqual(new Set(postIds.slice(r2Start)).size, 1, "one UUID was sent for the repeated taps");
    assert.strictEqual(dbQuery(db, "SELECT COUNT(*) FROM reports WHERE raw_text LIKE 'R2 synthetic:%'")[0][0], 1);

    // A draft typed while a background send is held survives its receipt.
    mode = "hold";
    await submit("R2 synthetic background: kailangan ng kumot sa Room 10.");
    await waitText(page, "#feedback", "Saved on this phone; waiting to send");
    await waitFor(() => held.length >= 1, "held background send");
    await page.fill("#text", "R2 draft in progress");
    await page.focus("#text");
    mode = "pass";
    await releaseHeld();
    await waitText(page, "#feedback", "Received by the hub", 15000);
    assert.strictEqual(await page.inputValue("#text"), "R2 draft in progress", "draft kept across the receipt");
    assert.strictEqual(await page.evaluate(() => document.activeElement.id), "text", "focus kept across the receipt");
    await page.fill("#text", "");

    // --- 10. R3: confirmation replaces only its own submission's feedback. ---
    mode = "hold";
    await submit("R3 synthetic A: may lagnat ang bata sa Room 11.");
    await waitFor(() => held.length >= 1, "held send A");
    await submit("R3 synthetic B: walang ilaw sa Room 12.");
    await waitText(page, "#feedback", "Saved on this phone; waiting to send");
    const beforeA = await receivedTotal();
    await held.shift().continue(); // A is confirmed; B is still waiting
    await page.waitForFunction(
      (want) => Number(document.querySelector("#received-count").textContent.replace(/\D/g, "")) === want,
      beforeA + 1,
      { timeout: 10000 }
    );
    let feedbackText = await page.locator("#feedback").textContent();
    assert.ok(feedbackText.includes("waiting to send"), "A's receipt does not overwrite B's feedback: " + feedbackText);
    await waitFor(() => held.length >= 1, "held send B");
    mode = "pass";
    await releaseHeld();
    await waitText(page, "#feedback", "Received by the hub", 15000);
    const idB = dbQuery(db, "SELECT id FROM reports WHERE raw_text LIKE 'R3 synthetic B:%'")[0][0];
    assert.ok((await page.locator("#feedback").textContent()).includes("Reference #" + idB), "feedback shows B's reference");

    mode = "hold";
    await submit("R3 synthetic C: kailangan ng tubig sa Room 13.");
    await waitFor(() => held.length >= 1, "held send C");
    await page.click("#send"); // blank: a newer validation message
    await waitText(page, "#feedback", "Please write what is happening");
    const beforeC = await receivedTotal();
    mode = "pass";
    await releaseHeld();
    await page.waitForFunction(
      (want) => Number(document.querySelector("#received-count").textContent.replace(/\D/g, "")) === want,
      beforeC + 1,
      { timeout: 10000 }
    );
    feedbackText = await page.locator("#feedback").textContent();
    assert.ok(feedbackText.includes("Please write what is happening"), "a background receipt keeps the newer message");

    // An unsaved report's late outcome (immediate send, then manual retry)
    // never replaces a newer report's feedback; its reference is still listed.
    async function failSaves(count) {
      await page.evaluate((left) => {
        const original = window.Outbox.save;
        window.Outbox.save = function failing(record) {
          if (left <= 0) return original(record);
          left -= 1;
          if (left === 0) window.Outbox.save = original;
          return Promise.reject(new Error("QuotaExceededError"));
        };
      }, count);
    }
    async function newerReportThenRelease(olderText, newerText) {
      await page.click("#new-report");
      await submit(newerText);
      await waitText(page, "#feedback", "Saved on this phone; waiting to send");
      await held.shift().continue(); // the older unsaved report is confirmed now
      const olderQuery = "SELECT id FROM reports WHERE raw_text = '" + olderText + "'";
      await waitFor(() => dbQuery(db, olderQuery).length === 1, "older row");
      const olderId = dbQuery(db, olderQuery)[0][0];
      await waitText(page, "#received-list", "Reference #" + olderId, 10000);
      const text = await page.locator("#feedback").textContent();
      assert.ok(text.includes("waiting to send") && !text.includes("Reference #" + olderId), "older unsaved receipt kept the newer feedback: " + text);
      await waitFor(() => held.length >= 1, "held newer send");
      mode = "pass";
      await releaseHeld();
      await waitText(page, "#feedback", "Received by the hub", 15000);
      const newerId = dbQuery(db, "SELECT id FROM reports WHERE raw_text = '" + newerText + "'")[0][0];
      assert.ok((await page.locator("#feedback").textContent()).includes("Reference #" + newerId), "newer report's own reference shown");
      await page.waitForFunction(() => document.querySelector("#pending-count").textContent === "(0)", null, { timeout: 15000 });
    }

    await failSaves(1);
    mode = "hold";
    await submit("R3 synthetic D: unsaved, immediate send held.");
    await waitFor(() => held.length >= 1, "held immediate send D");
    await newerReportThenRelease("R3 synthetic D: unsaved, immediate send held.", "R3 synthetic E: newer saved report.");

    await failSaves(2);
    mode = "fail503";
    await submit("R3 synthetic F: unsaved, manual retry held.");
    await waitText(page, "#feedback", "Not saved on this phone", 10000);
    mode = "hold";
    await page.click("#pending-list .unsaved button");
    await waitFor(() => held.length >= 1, "held manual retry F");
    await newerReportThenRelease("R3 synthetic F: unsaved, manual retry held.", "R3 synthetic G: newer saved report.");

    // --- layout, labels, tap targets ---
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth);
    assert.strictEqual(overflow, false, "no horizontal overflow at 412x915");
    for (const selector of ["#send", "#retry-now", "#new-report"]) {
      const box = await page.evaluate((sel) => {
        const node = document.querySelector(sel);
        const rect = node.getBoundingClientRect();
        return { hidden: node.hidden, height: rect.height, width: rect.width };
      }, selector);
      if (!box.hidden) {
        assert.ok(box.height >= 44 && box.width >= 44, selector + " is at least 44 px");
      }
    }
    assert.ok(await page.evaluate(() => Boolean(document.querySelector('label[for="reporter"]'))));
    assert.ok(await page.evaluate(() => Boolean(document.querySelector('label[for="text"]'))));

    // --- 11. R4: stored references recover after a failed read, merged with known ones. ---
    const seedCid = "00000000-0000-4000-8000-000000000027";
    await page.evaluate((cid) => window.Outbox.acknowledge(cid, 27), seedCid);
    await page.close(); // page3 is the only sender from here on
    const instrument = `
window.__failReceipts = true;
window.__failAck = false;
(() => {
  const listReceipts = Outbox.listReceipts;
  const acknowledge = Outbox.acknowledge;
  Outbox.listReceipts = () => (window.__failReceipts ? Promise.reject(new Error("read failed")) : listReceipts());
  Outbox.acknowledge = (clientId, id) =>
    window.__failAck ? Promise.reject(new Error("QuotaExceededError")) : acknowledge(clientId, id);
})();`;
    const page3 = await contextPage((pg) =>
      pg.route("**/static/outbox.js", async (route) => {
        const response = await route.fetch();
        return route.fulfill({ response, body: (await response.text()) + "\n" + instrument });
      })
    );
    await waitText(page3, "#received-count", "unavailable");
    await waitText(page3, "#storage-note", "References received earlier could not be read");
    await page3.evaluate(() => (window.__failAck = true));
    await page3.fill("#text", "R4 synthetic: kailangan ng gamot sa Room 14.");
    await page3.click("#send");
    await waitText(page3, "#feedback", "Received by the hub", 15000);
    await waitText(page3, "#storage-note", "could not record the reference");
    assert.strictEqual(await page3.locator("#received-count").textContent(), "unavailable", "count stays unknown while reads fail");
    const idR4 = dbQuery(db, "SELECT id FROM reports WHERE raw_text LIKE 'R4 synthetic:%'")[0][0];

    await page3.evaluate(() => (window.__failReceipts = false)); // storage reads recover
    await waitText(page3, "#received-list", "Reference #27", 8000);
    const storedTotal = (await storedReceiptIds(page3)).length;
    await page3.waitForFunction(
      (want) => document.querySelector("#received-count").textContent === want,
      "(" + (storedTotal + 1) + ")",
      { timeout: 8000 }
    );
    received = await page3.locator("#received-list").textContent();
    assert.ok(received.includes("Reference #" + idR4) && received.includes("may send again"), "known unrecorded reference kept");
    const note = await page3.locator("#storage-note").textContent();
    assert.ok(!note.includes("References received earlier"), "read warning cleared after recovery: " + note);

    await page3.evaluate(() => (window.__failAck = false));
    await page3.waitForFunction(() => document.querySelector("#pending-count").textContent === "(0)", null, { timeout: 15000 });
    await page3.waitForFunction(() => document.querySelector("#storage-note").hidden, null, { timeout: 8000 });
    assert.strictEqual(dbQuery(db, "SELECT COUNT(*) FROM reports WHERE raw_text LIKE 'R4 synthetic:%'")[0][0], 1);

    // Another tab's acknowledgement appears without a reload.
    const page4 = await contextPage();
    await page4.evaluate(() => window.Outbox.acknowledge("00000000-0000-4000-8000-000000000028", 28));
    await page4.close();
    await waitText(page3, "#received-list", "Reference #28", 8000);

    // --- safe rendering and requests across every page ---
    assert.strictEqual(pageErrors.length, 0, "no page errors: " + pageErrors.join(" | "));
    for (const url of seenUrls) {
      assert.ok(url.startsWith(origin), "only local hub requests: " + url);
    }

    console.log("browser_delivery.cjs: all assertions passed (" + postCount + " POST attempts)");
  } finally {
    await browser.close();
    hub.kill();
    try {
      fs.rmSync(path.dirname(db), { recursive: true, force: true });
    } catch (error) {
      // best effort cleanup
    }
  }
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});