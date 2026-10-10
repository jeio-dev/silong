"use strict";

// Durable on-phone report store backed by IndexedDB. Loaded before report.js.
//
// Two object stores, both keyed by client_id:
//   pending:  { client_id, text, reporter, queued_at } — saved before sending,
//             kept until the hub confirms. Records are stored as-is (never
//             serialized), leaving room for Blob payloads in a later phase.
//   receipts: { client_id, id, confirmed_at } — the hub reference only,
//             without report contents.
//
// Every write resolves only after its transaction completes and rejects on
// abort/error, so a resolved save or acknowledge is durable. `acknowledge`
// inserts the receipt and removes the pending row in one transaction. The
// wrapper never deletes, expires or edits unconfirmed reports on its own.

const Outbox = (() => {
  const DB_NAME = "silong-outbox";
  const DB_VERSION = 1;
  const PENDING = "pending";
  const RECEIPTS = "receipts";
  // A blocked or wedged open must surface, not hang the form forever.
  const OPEN_TIMEOUT_MS = 5000;

  let dbPromise = null;

  function open() {
    if (dbPromise) return dbPromise;
    dbPromise = new Promise((resolve, reject) => {
      let settled = false;
      const fail = (error) => {
        if (settled) return;
        settled = true;
        dbPromise = null; // allow a later call to retry the open
        reject(error);
      };
      let request;
      try {
        request = indexedDB.open(DB_NAME, DB_VERSION);
      } catch (error) {
        fail(error);
        return;
      }
      const timer = setTimeout(
        () => fail(new Error("The saved-reports store did not open in time.")),
        OPEN_TIMEOUT_MS
      );
      request.onupgradeneeded = (event) => {
        const db = event.target.result;
        if (!db.objectStoreNames.contains(PENDING)) {
          db.createObjectStore(PENDING, { keyPath: "client_id" });
        }
        if (!db.objectStoreNames.contains(RECEIPTS)) {
          db.createObjectStore(RECEIPTS, { keyPath: "client_id" });
        }
      };
      request.onsuccess = (event) => {
        clearTimeout(timer);
        const db = event.target.result;
        if (settled) {
          // A blocked or late open may still hand over a connection; drop it
          // so a later retry opens cleanly.
          db.close();
          return;
        }
        settled = true;
        // Another tab is upgrading the database: give up the connection and
        // let the next call reopen.
        db.onversionchange = () => {
          db.close();
          dbPromise = null;
        };
        resolve(db);
      };
      request.onerror = (event) => {
        clearTimeout(timer);
        fail(request.error || new Error("Could not open the saved-reports store."));
      };
      request.onblocked = (event) => {
        clearTimeout(timer);
        fail(new Error("The saved-reports store is blocked by another open tab."));
      };
    });
    return dbPromise;
  }

  // Resolve only when `work(transaction)` finishes and the transaction
  // completes; reject on abort or a synchronously thrown error.
  function complete(transaction, work) {
    return new Promise((resolve, reject) => {
      transaction.oncomplete = () => resolve();
      transaction.onabort = () =>
        reject(transaction.error || new Error("The saved-reports store did not accept the change."));
      transaction.onerror = () =>
        reject(transaction.error || new Error("The saved-reports store failed."));
      try {
        work(transaction);
      } catch (error) {
        reject(error);
      }
    });
  }

  // Read every row of one store; resolves on the request succeeding.
  function readAll(storeName) {
    return open().then((db) => {
      return new Promise((resolve, reject) => {
        const transaction = db.transaction(storeName, "readonly");
        const request = transaction.objectStore(storeName).getAll();
        request.onsuccess = () => resolve(request.result);
        request.onerror = () =>
          reject(request.error || new Error("Reading the saved-reports store failed."));
      });
    });
  }

  function byQueuedThenId(a, b) {
    if (a.queued_at !== b.queued_at) return a.queued_at < b.queued_at ? -1 : 1;
    return a.client_id < b.client_id ? -1 : a.client_id > b.client_id ? 1 : 0;
  }

  return {
    // Put `record` (client_id/text/reporter/queued_at) into the pending store.
    // Resolves only after the transaction commits.
    save(record) {
      return open().then(
        (db) =>
          complete(db.transaction(PENDING, "readwrite"), (transaction) => {
            transaction.objectStore(PENDING).put(record);
          })
      );
    },

    // Pending reports, oldest first (queued_at, then client_id).
    listPending() {
      return readAll(PENDING).then((rows) => rows.sort(byQueuedThenId));
    },

    // Confirmed references (id and confirmed_at), without report contents.
    listReceipts() {
      return readAll(RECEIPTS);
    },

    // Record the hub's positive integer reference for `clientId` and remove
    // the pending row atomically. Safe to repeat; the reference is stable.
    acknowledge(clientId, id) {
      return open().then(
        (db) =>
          complete(db.transaction([PENDING, RECEIPTS], "readwrite"), (transaction) => {
            transaction.objectStore(RECEIPTS).put({
              client_id: clientId,
              id,
              confirmed_at: new Date().toISOString(),
            });
            transaction.objectStore(PENDING).delete(clientId);
          })
      );
    },
  };
})();

// One global namespace shared with report.js.
window.Outbox = Outbox;