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
    let mode = "pass"; // pass | hold | abort | fail503 | fail422 | malformed | commitThen500
    let postCount = 0;
    let commitThen500Left = 0;
    const held = [];
    function installRoutes(pg) {
      return pg.route("**/api/reports", async (route) => {
        if (route.request().method() !== "POST") return route.continue();
        postCount += 1;
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
        if (mode === "commitThen500" && commitThen500Left > 0) {
          commitThen500Left -= 1;
          await route.fetch(); // the hub commits; the response is then lost
          return route.fulfill({ status: 500, contentType: "application/json", body: '{"detail":"lost"}' });
        }
        return route.continue();
      });
    }

    const contextPage = async () => {
      const page = await context.newPage();
      page.on("pageerror", (error) => pageErrors.push(error.message));
      page.on("request", (request) => seenUrls.push(request.url()));
      await installRoutes(page);
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

    // --- layout, labels, tap targets, safe requests ---
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