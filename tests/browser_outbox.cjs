"use strict";

// Browser verification of silong/static/outbox.js against Chromium's real
// IndexedDB at a synthetic HTTP origin. Uses only synthetic reports.
//
// Run: node tests/browser_outbox.cjs
// Optional overrides: PLAYWRIGHT_MODULE (a directory containing `playwright`)
// and CHROMIUM_EXECUTABLE (the chrome binary). The documented defaults are the
// paths recorded in the phase 03 plan.

const assert = require("node:assert");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");

const PLAYWRIGHT_MODULE =
  process.env.PLAYWRIGHT_MODULE || "/tmp/aarista-chart-verify/node_modules/playwright";
const CHROMIUM_EXECUTABLE =
  process.env.CHROMIUM_EXECUTABLE ||
  "/home/jeio/.cache/ms-playwright/chromium-1248/chrome-linux64/chrome";
// This host's Chromium needs libnspr4/libnss3, which are not installed as
// system packages. They were extracted beside the prior browser verification,
// so point the loader at them (CHROMIUM_LIB_DIR overrides).
const CHROMIUM_LIB_DIR =
  process.env.CHROMIUM_LIB_DIR || "/tmp/aarista-chart-verify/libs/root/usr/lib/x86_64-linux-gnu";

const ROOT = path.resolve(__dirname, "..");
const OUTBOX_PATH = path.join(ROOT, "silong", "static", "outbox.js");
const OUTBOX_SOURCE = fs.readFileSync(OUTBOX_PATH, "utf8");

const PAGE_HTML = `<!doctype html><html><head><meta charset="utf-8"></head>
<body><p id="ready">ready</p><script src="/outbox.js"></script></body></html>`;

let playwright;
try {
  playwright = require(PLAYWRIGHT_MODULE);
} catch (error) {
  console.error("Playwright is unavailable at " + PLAYWRIGHT_MODULE + "; set PLAYWRIGHT_MODULE.");
  process.exit(2);
}

function rec(id, text, queuedAt) {
  return {
    client_id: id,
    text,
    reporter: text.includes("Ana") ? "Ana" : null,
    queued_at: queuedAt,
  };
}

// A stable synthetic UUID v4 string for a given tag (distinct per tag).
function uuid(tag) {
  const hex = "0123456789abcdef";
  let seed = 0;
  for (let i = 0; i < tag.length; i++) seed = (seed * 31 + tag.charCodeAt(i)) & 0xffff;
  let body = "";
  for (let i = 0; i < 32; i++) body += hex[(seed + i * 13) % 16];
  return (
    body.slice(0, 8) + "-" + body.slice(8, 12) + "-4" + body.slice(13, 16) +
    "-8" + body.slice(17, 20) + "-" + body.slice(20, 32)
  );
}

async function main() {
  const server = http.createServer((req, res) => {
    if (req.url === "/outbox.js") {
      res.writeHead(200, { "Content-Type": "text/javascript" });
      res.end(OUTBOX_SOURCE);
      return;
    }
    if (req.url === "/") {
      res.writeHead(200, { "Content-Type": "text/html" });
      res.end(PAGE_HTML);
      return;
    }
    res.writeHead(404);
    res.end();
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const origin = "http://127.0.0.1:" + server.address().port;

  const browser = await playwright.chromium.launch({
    executablePath: CHROMIUM_EXECUTABLE,
    headless: true,
    env: fs.existsSync(path.join(CHROMIUM_LIB_DIR, "libnspr4.so"))
      ? { ...process.env, LD_LIBRARY_PATH: CHROMIUM_LIB_DIR }
      : process.env,
  });

  try {
    const context = await browser.newContext();

    async function openPage() {
      const page = await context.newPage();
      await page.goto(origin + "/");
      await page.waitForFunction(() => typeof window.Outbox !== "undefined");
      return page;
    }

    const page = await openPage();

    // 1. save resolves only after commit; the row is readable back.
    const a = rec(uuid("a"), "Lola ko sa Room 3, walang gatas", 10);
    await page.evaluate((r) => window.Outbox.save(r), a);
    let pending = await page.evaluate(() => window.Outbox.listPending());
    assert.deepStrictEqual(pending, [a], "saved pending row is returned");

    // 2. An aborted write rejects and leaves prior data intact.
    await page.evaluate(() => {
      const original = IDBObjectStore.prototype.put;
      IDBObjectStore.prototype.put = function patched() {
        IDBObjectStore.prototype.put = original;
        throw new DOMException("quota exceeded", "AbortError");
      };
    });
    const b = rec(uuid("b"), "Kailangan ng tubing sa Room 5", 20);
    await assert.rejects(page.evaluate((r) => window.Outbox.save(r), b), /AbortError|did not accept/);
    pending = await page.evaluate(() => window.Outbox.listPending());
    assert.deepStrictEqual(pending, [a], "aborted save did not add a row");

    // 3. Acknowledge atomically replaces the pending row with a reference.
    await page.evaluate((r) => window.Outbox.acknowledge(r.client_id, 101), a);
    pending = await page.evaluate(() => window.Outbox.listPending());
    assert.deepStrictEqual(pending, [], "acknowledge removed the pending row");
    let receipts = await page.evaluate(() => window.Outbox.listReceipts());
    assert.strictEqual(receipts.length, 1);
    assert.strictEqual(receipts[0].id, 101);
    assert.strictEqual(receipts[0].client_id, a.client_id);
    assert.strictEqual(typeof receipts[0].confirmed_at, "string");

    // 4. Reload: receipts and (if any) pending survive.
    await page.reload();
    await page.waitForFunction(() => typeof window.Outbox !== "undefined");
    receipts = await page.evaluate(() => window.Outbox.listReceipts());
    assert.strictEqual(receipts.length, 1, "receipt survives reload");

    // 5. Ack failure keeps the pending row and adds no receipt.
    const c = rec(uuid("c"), "May sugat ang tatay ko sa paa", 30);
    await page.evaluate((r) => window.Outbox.save(r), c);
    await page.evaluate(() => {
      const original = IDBObjectStore.prototype.put;
      IDBObjectStore.prototype.put = function patchedPut() {
        IDBObjectStore.prototype.put = original;
        throw new DOMException("write blocked", "AbortError");
      };
    });
    await assert.rejects(
      page.evaluate((r) => window.Outbox.acknowledge(r.client_id, 202), c),
      /AbortError|did not accept/
    );
    pending = await page.evaluate(() => window.Outbox.listPending());
    assert.deepStrictEqual(pending, [c], "failed acknowledge keeps the pending row");
    receipts = await page.evaluate(() => window.Outbox.listReceipts());
    assert.strictEqual(receipts.length, 1, "failed acknowledge added no receipt");

    // 6. A later acknowledge succeeds and repeated acknowledgement is harmless.
    await page.evaluate((r) => window.Outbox.acknowledge(r.client_id, 202), c);
    await page.evaluate((r) => window.Outbox.acknowledge(r.client_id, 202), c);
    receipts = await page.evaluate(() => window.Outbox.listReceipts());
    assert.strictEqual(receipts.length, 2, "one receipt per client_id");
    pending = await page.evaluate(() => window.Outbox.listPending());
    assert.strictEqual(pending.length, 0);

    // 7. Two tabs in the same context and origin share the store.
    const page2 = await openPage();
    const d = rec(uuid("d"), "Kailangan ng gamot", 40);
    await page2.evaluate((r) => window.Outbox.save(r), d);
    pending = await page.evaluate(() => window.Outbox.listPending());
    assert.deepStrictEqual(pending, [d], "second tab's save is visible in the first");

    // 8. A separate browser context (separate profile) has isolated storage.
    const otherContext = await browser.newContext();
    const isolated = await otherContext.newPage();
    await isolated.goto(origin + "/");
    await isolated.waitForFunction(() => typeof window.Outbox !== "undefined");
    const isolatedPending = await isolated.evaluate(() => window.Outbox.listPending());
    const isolatedReceipts = await isolated.evaluate(() => window.Outbox.listReceipts());
    assert.deepStrictEqual(isolatedPending, [], "fresh context has no pending rows");
    assert.deepStrictEqual(isolatedReceipts, [], "fresh context has no receipts");
    await otherContext.close();

    // 9. A wedged open settles visibly (bounded), never hanging the caller.
    const blockedContext = await browser.newContext();
    const blockedPage = await blockedContext.newPage();
    await blockedPage.goto(origin + "/");
    await blockedPage.waitForFunction(() => typeof window.Outbox !== "undefined");
    await blockedPage.evaluate(() => {
      Object.defineProperty(window, "indexedDB", {
        value: {
          open() {
            // A request that never fires success/error/blocked.
            return { onsuccess: null, onerror: null, onupgradeneeded: null, onblocked: null };
          },
        },
        configurable: true,
      });
    });
    const startedAt = Date.now();
    await assert.rejects(
      blockedPage.evaluate((r) => window.Outbox.save(r), rec(uuid("z"), "blocked", 1)),
      /did not open in time/
    );
    assert.ok(Date.now() - startedAt < 9000, "blocked open settled promptly");
    await blockedContext.close();

    console.log("browser_outbox.cjs: all assertions passed");
  } finally {
    await browser.close();
    server.close();
  }
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});