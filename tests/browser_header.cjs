"use strict";

// Browser verification of the staff board header against a real hub with
// synthetic rows and a mocked local Ollama status. Synthetic reports only.
//
// Run: node tests/browser_header.cjs
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

function dbExec(db, sql) {
  const code = "import sqlite3,sys;c=sqlite3.connect(sys.argv[1]);c.execute(sys.argv[2]);c.commit()";
  execFileSync(PYTHON, ["-c", code, db, sql], { encoding: "utf8" });
}

function seedRows(db, rows) {
  const code =
    "import sqlite3,json,sys;c=sqlite3.connect(sys.argv[1]);" +
    "rows=json.loads(sys.argv[2]);" +
    "c.executemany('INSERT INTO reports (client_id,created_at,raw_text,status,urgency,fields_json,resolved) VALUES (?,?,?,?,?,?,?)'," +
    "[(r['client_id'],r['created_at'],r['raw_text'],r['status'],r.get('urgency'),r.get('fields_json'),r.get('resolved',0)) for r in rows]);" +
    "c.commit()";
  execFileSync(PYTHON, ["-c", code, db, JSON.stringify(rows)], { encoding: "utf8" });
}

async function startHub(extraEnv) {
  const port = await freePort();
  const db = path.join(fs.mkdtempSync(path.join("/tmp", "silong-header-")), "hub.db");
  const env = { ...process.env, SILONG_PORT: String(port), SILONG_DB: db };
  delete env.SILONG_MODEL;
  delete env.OLLAMA_HOST;
  Object.assign(env, extraEnv);
  const proc = spawn(PYTHON, ["-m", "silong"], { cwd: ROOT, env, stdio: ["ignore", "pipe", "pipe"] });
  let log = "";
  proc.stdout.on("data", (chunk) => (log += chunk));
  proc.stderr.on("data", (chunk) => (log += chunk));
  const origin = "http://127.0.0.1:" + port;
  for (let i = 0; i < 100; i++) {
    try {
      await new Promise((resolve, reject) => {
        const req = http.get(origin + "/api/reports", (res) => {
          res.resume();
          res.statusCode === 200 ? resolve() : reject(new Error());
        });
        req.on("error", reject);
      });
      return { port, db, origin, proc, stop: () => proc.kill() };
    } catch (error) {
      await new Promise((resolve) => setTimeout(resolve, 150));
    }
  }
  throw new Error("hub did not start: " + log);
}

function fieldsJson(needs) {
  return JSON.stringify({
    location: "Room 1",
    in_center: true,
    on_behalf: false,
    headcount: null,
    vulnerable: [],
    injured: false,
    injury_note: null,
    needs,
    summary: "Synthetic report.",
  });
}

async function waitText(page, selector, text, timeout = 15000) {
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
  const browser = await playwright.chromium.launch({
    executablePath: CHROMIUM_EXECUTABLE,
    headless: true,
    env: fs.existsSync(path.join(CHROMIUM_LIB_DIR, "libnspr4.so"))
      ? { ...process.env, LD_LIBRARY_PATH: CHROMIUM_LIB_DIR }
      : process.env,
  });

  // Mocked Ollama /api/ps: toggles between loaded / another model / down.
  const ollamaPort = await freePort();
  let ollamaState = "loaded";
  const ollama = http.createServer((req, res) => {
    if (req.method === "GET" && req.url === "/api/ps") {
      if (ollamaState === "down") {
        res.writeHead(500, { "Content-Type": "application/json" });
        res.end('{"error":"no"}');
        return;
      }
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ models: [{ name: ollamaState === "other" ? "qwen3:4b" : "gemma4:e2b" }] }));
      return;
    }
    res.writeHead(404);
    res.end();
  });
  await new Promise((resolve) => ollama.listen(ollamaPort, "127.0.0.1", resolve));

  const hubA = await startHub({ SILONG_HUB_URL: "http://192.168.50.7:8000" }); // no model
  const hubB = await startHub({
    SILONG_MODEL: "gemma4:e2b",
    OLLAMA_HOST: "http://127.0.0.1:" + ollamaPort,
    SILONG_HUB_URL: "http://192.168.50.77:8000",
  });

  try {
    // Seed hub A with precisely-controlled synthetic rows (no worker runs).
    const now = new Date().toISOString();
    const uuid = (tag) =>
      "00000000-0000-4000-8000-" + String(tag).padStart(12, "0");
    seedRows(hubA.db, [
      { client_id: uuid("1"), created_at: now, raw_text: "Queued report", status: "queued" },
      { client_id: uuid("2"), created_at: now, raw_text: "Extracting report", status: "extracting" },
      { client_id: uuid("3"), created_at: now, raw_text: "Critical report", status: "done", urgency: "critical", fields_json: fieldsJson(["rescue"]) },
      { client_id: uuid("4"), created_at: now, raw_text: "Resolved high", status: "done", urgency: "high", fields_json: fieldsJson(["water"]), resolved: 1 },
      { client_id: uuid("5"), created_at: now, raw_text: "Medium report", status: "done", urgency: "medium", fields_json: fieldsJson(["food"]) },
      { client_id: uuid("6"), created_at: now, raw_text: "Low report", status: "done", urgency: "low", fields_json: fieldsJson([]) },
      { client_id: uuid("7"), created_at: now, raw_text: "Failed report", status: "failed" },
    ]);

    const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
    const errors = [];
    const qrRequests = [];
    const page = await context.newPage();
    page.on("pageerror", (error) => errors.push(error.message));
    page.on("request", (request) => {
      if (request.url().endsWith("/api/qr")) qrRequests.push(request.url());
    });
    await page.goto(hubA.origin + "/board");
    await waitText(page, "#connection", "Connected");

    // --- header contract: URL/QR, model, audio, counts. ---
    await waitText(page, "#phone-url", "http://192.168.50.7:8000/");
    assert.strictEqual(await page.getAttribute("#phone-url", "href"), "http://192.168.50.7:8000/");
    await page.waitForFunction(
      () => {
        const img = document.querySelector("#phone-qr");
        return img && !img.hidden && img.complete && img.naturalWidth > 0;
      },
      null,
      { timeout: 15000 }
    );
    assert.ok(qrRequests.length >= 1, "QR image was requested");
    assert.strictEqual(await page.textContent("#model-status"), "No model configured; reports stay queued for triage.");
    assert.strictEqual(await page.textContent("#audio-status"), "Text only: audio unavailable");
    assert.strictEqual(await page.textContent("#queue-status"), "Queued: 1");
    assert.strictEqual(await page.textContent("#processing-status"), "Processing: 1");
    assert.strictEqual(await page.textContent("#reports-today"), "7 report(s)");
    assert.strictEqual(await page.textContent("#local-date"), now.slice(0, 10));
    const urgencyText = await page.textContent("#urgency-counts");
    assert.ok(urgencyText.includes("Critical") && urgencyText.includes("1"), "critical count 1");
    assert.ok(urgencyText.includes("High0"), "resolved high excluded");
    assert.ok(urgencyText.includes("Medium") && urgencyText.includes("1"), "medium count 1");
    assert.ok(urgencyText.includes("Low") && urgencyText.includes("1"), "low count 1");

    // --- all four sections, unclassified first. ---
    const sections = await page.evaluate(() =>
      ["unclassified", "triaged", "resolved"].map((id) => document.querySelector('ol[id="' + id + '"]') !== null)
    );
    assert.deepStrictEqual(sections, [true, true, true], "three sections present");
    // Reading order: unclassified is left of triaged when the lanes sit side by
    // side, otherwise above it; both come before the resolved section.
    const order = await page.evaluate(() => {
      const box = (id) => document.getElementById(id).getBoundingClientRect();
      const [unclassified, triaged, resolved] = [box("unclassified"), box("triaged"), box("resolved-heading")];
      const sideBySide = unclassified.right <= triaged.left;
      return {
        unclassFirst: sideBySide || unclassified.top < triaged.top,
        resolvedLast: resolved.top > Math.max(unclassified.bottom, triaged.bottom),
      };
    });
    assert.ok(order.unclassFirst, "unclassified section is first");
    assert.ok(order.resolvedLast, "resolved section comes after both lanes");

    // Unclassified cards never carry an urgency label.
    const unclassUrgency = await page.evaluate(
      () => document.querySelectorAll("#unclassified .report .urgency-label").length
    );
    assert.strictEqual(unclassUrgency, 0, "unclassified cards show no urgency label");

    // --- workflow: expanded original survives polls. ---
    const firstTriaged = page.locator("#triaged .report").first();
    await firstTriaged.locator("details.original summary").click();
    await page.waitForFunction(
      () => document.querySelector("#triaged .report details.original")?.open === true,
      null,
      { timeout: 5000 }
    );
    await new Promise((resolve) => setTimeout(resolve, 2500)); // let polls run
    const stillOpen = await page.evaluate(
      () => document.querySelector("#triaged .report details.original")?.open === true
    );
    assert.ok(stillOpen, "expanded original survives a poll");

    // --- workflow: editor shows the changed notice. ---
    await page.evaluate(() => {
      document.querySelector("#triaged .report [data-action='edit']").click();
    });
    await page.waitForSelector("#triaged .report .edit-form");
    await new Promise((resolve, reject) => {
      const req = http.request(
        hubA.origin + "/api/reports/3",
        { method: "PATCH", headers: { "Content-Type": "application/json" } },
        (res) => {
          res.resume();
          resolve();
        }
      );
      req.on("error", reject);
      req.end(JSON.stringify({ fields: JSON.parse(fieldsJson(["rescue", "medical"])) }));
    });
    await waitText(page, "#triaged .report .edit-notice", "changed since you opened");

    // Close the editor cleanly (Save with unchanged form after reloading data).
    await page.evaluate(() => {
      const form = document.querySelector("#triaged .report .edit-form");
      form.querySelector('button[type="submit"]').click();
    });
    await page.waitForFunction(() => !document.querySelector("#triaged .report .edit-form"), null, { timeout: 10000 });

    // --- workflow: resolve moves the card to the resolved section. ---
    const triagedBefore = await page.evaluate(() => document.querySelectorAll("#triaged .report").length);
    await page.evaluate(() => {
      document.querySelector("#triaged .report [data-action='resolve']").click();
    });
    await page.waitForFunction(
      (before) => document.querySelectorAll("#triaged .report").length === before - 1,
      triagedBefore,
      { timeout: 10000 }
    );
    await page.evaluate(() => {
      const summary = document.querySelector(".resolved-section > summary");
      if (summary) summary.click();
    });
    const resolvedCount = await page.evaluate(() => document.querySelectorAll("#resolved .report").length);
    assert.strictEqual(resolvedCount, 2, "resolved card moved to the resolved section");

    // --- workflow: a failed save shows the error and keeps the draft. ---
    await page.route("**/api/reports/*", async (route) => {
      if (route.request().method() === "PATCH") {
        return route.fulfill({ status: 503, contentType: "application/json", body: '{"detail":"disk busy"}' });
      }
      return route.continue();
    });
    await page.evaluate(() => {
      document.querySelector("#triaged .report [data-action='edit']").click();
    });
    await page.waitForSelector("#triaged .report .edit-form");
    await page.evaluate(() => {
      const form = document.querySelector("#triaged .report .edit-form");
      form.querySelector('button[type="submit"]').click();
    });
    await waitText(page, "#triaged .report .edit-form .card-feedback", "disk busy", 10000);
    const formStillOpen = await page.evaluate(() => Boolean(document.querySelector("#triaged .report .edit-form")));
    assert.ok(formStillOpen, "failed save keeps the editor and draft open");
    await page.unroute("**/api/reports/*");

    // --- QR is fetched once while the URL is stable, changes with the URL, and
    // disappears when no URL is available. ---
    const qrRequestsOnPage = qrRequests.length;
    await new Promise((resolve) => setTimeout(resolve, 5000)); // several polls
    assert.strictEqual(qrRequests.length, qrRequestsOnPage, "stable URL does not re-fetch the QR");

    // A different advertised URL yields a different QR (two hub overrides).
    const getQr = (origin) =>
      new Promise((resolve, reject) => {
        http.get(origin + "/api/qr", (res) => {
          const chunks = [];
          res.on("data", (chunk) => chunks.push(chunk));
          res.on("end", () => resolve(Buffer.concat(chunks)));
        }).on("error", reject);
      });
    const qrA = await getQr(hubA.origin);
    const qrB = await getQr(hubB.origin);
    assert.ok(qrA.includes("svg"), "QR A is an SVG");
    assert.ok(!qrA.equals(qrB), "different URLs produce different QR images");

    // The board on hub B shows its own URL and QR.
    const hubBpage = await context.newPage();
    hubBpage.on("pageerror", (error) => errors.push(error.message));
    await hubBpage.goto(hubB.origin + "/board");
    await waitText(hubBpage, "#connection", "Connected");
    await waitText(hubBpage, "#phone-url", "http://192.168.50.77:8000/");

    // When the status reports no URL, the QR and link disappear with a hint.
    const page2 = await context.newPage();
    page2.on("pageerror", (error) => errors.push(error.message));
    const noUrlStatus = {
      hub_url: null,
      model: null,
      model_state: "not_configured",
      model_checked_at: null,
      worker_running: false,
      audio_available: false,
      queue_length: 0,
      processing_count: 0,
      reports_today: 0,
      local_date: now.slice(0, 10),
      urgency_counts: { critical: 0, high: 0, medium: 0, low: 0 },
      snapshot_at: new Date().toISOString(),
    };
    await page2.route("**/api/status", (route) =>
      route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(noUrlStatus) })
    );
    await page2.goto(hubA.origin + "/board");
    await waitText(page2, "#connection", "Connected");
    await waitText(page2, "#phone-url-unavailable", "set SILONG_HUB_URL", 10000);
    assert.ok(await page2.evaluate(() => document.querySelector("#phone-qr").hidden), "QR hidden without a URL");
    assert.ok(await page2.evaluate(() => document.querySelector("#phone-url-wrap").hidden), "link hidden without a URL");

    // --- stale status: counts stay, a warning appears; a held status never blocks cards. ---
    await waitText(hubBpage, "#model-status", "Loaded in Ollama: gemma4:e2b", 15000);
    assert.ok((await hubBpage.textContent("#model-status")).includes("last checked"));
    assert.ok(await hubBpage.evaluate(() => document.querySelector("#worker-note").hidden), "worker note hidden while running");

    ollamaState = "other";
    await waitText(hubBpage, "#model-status", "Not loaded: gemma4:e2b", 15000);
    ollamaState = "down";
    await waitText(hubBpage, "#model-status", "Model unavailable", 15000);
    ollamaState = "loaded";
    await waitText(hubBpage, "#model-status", "Loaded in Ollama: gemma4:e2b", 15000);

    // worker not running while a model is loaded: shown distinctly.
    let mockB = {
      hub_url: null,
      model: "gemma4:e2b",
      model_state: "loaded",
      model_checked_at: new Date().toISOString(),
      worker_running: false,
      audio_available: false,
      queue_length: 2,
      processing_count: 0,
      reports_today: 5,
      local_date: now.slice(0, 10),
      urgency_counts: { critical: 0, high: 0, medium: 0, low: 0 },
      snapshot_at: new Date().toISOString(),
    };
    await hubBpage.route("**/api/status", (route) =>
      route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(mockB) })
    );
    await waitText(hubBpage, "#worker-note", "not running", 15000);
    assert.strictEqual(await hubBpage.textContent("#queue-status"), "Queued: 2");
    await hubBpage.unroute("**/api/status");
    // Wait for a real poll (worker note must disappear when running again).
    await hubBpage.waitForFunction(() => document.querySelector("#worker-note").hidden, null, { timeout: 15000 });

    // stale status keeps last values; a held status still lets cards update.
    let holdStatus = false;
    await hubBpage.route("**/api/status", (route) => {
      if (holdStatus) return; // never fulfill: held
      return route.continue();
    });
    holdStatus = true;
    const beforeCards = await hubBpage.evaluate(() => document.querySelectorAll("#unclassified .report").length);
    // A new report arrives on the hub while status is held.
    await new Promise((resolve, reject) => {
      const body = encodeURIComponent("Sunog malapit sa palengke");
      const req = http.request(
        hubB.origin + "/api/reports",
        { method: "POST", headers: { "Content-Type": "application/x-www-form-urlencoded" } },
        (res) => {
          res.resume();
          resolve();
        }
      );
      req.on("error", reject);
      req.end("client_id=bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb&text=" + body);
    });
    await hubBpage.waitForFunction(
      (before) => document.querySelectorAll("#unclassified .report").length > before,
      beforeCards,
      { timeout: 6000 }
    );
    holdStatus = false;
    await hubBpage.unroute("**/api/status");

    // 503 status: counts remain (stale warning), never zeroed.
    await hubBpage.route("**/api/status", (route) =>
      route.fulfill({ status: 503, contentType: "application/json", body: '{"detail":"no"}' })
    );
    await waitText(hubBpage, "#status-note", "out of date", 15000);
    assert.ok((await hubBpage.textContent("#model-status")).includes("Loaded in Ollama"), "model value retained");
    assert.strictEqual(await hubBpage.textContent("#queue-status"), "Queued: 0", "counts retained unchanged");

    // --- layout: no horizontal overflow at 1280x900 and 412x915. ---
    for (const size of [{ width: 1280, height: 900 }, { width: 412, height: 915 }]) {
      await page.setViewportSize(size);
      await new Promise((resolve) => setTimeout(resolve, 300));
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth);
      assert.strictEqual(overflow, false, "no horizontal overflow at " + size.width + "x" + size.height);
    }

    assert.strictEqual(errors.length, 0, "no page errors: " + errors.join(" | "));
    console.log("browser_header.cjs: all assertions passed");
  } finally {
    await browser.close();
    hubA.stop();
    hubB.stop();
    ollama.close();
    try {
      fs.rmSync(path.dirname(hubA.db), { recursive: true, force: true });
      fs.rmSync(path.dirname(hubB.db), { recursive: true, force: true });
    } catch (error) {
      // best effort cleanup
    }
  }
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});