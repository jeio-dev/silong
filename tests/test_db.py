import sqlite3
import tempfile
import threading
import unittest
import uuid
from contextlib import closing
from pathlib import Path
from unittest import mock

from silong import db

UNCLASSIFIED_COLUMNS = (
    "audio_path",
    "transcript",
    "fields_json",
    "urgency",
    "urgency_reason",
    "asr_ms",
    "llm_ms",
    "error",
)

# Synthetic reports only.
TEXT_A = "Lola ko sa Room 3, hindi makalakad, wala nang gatas ang baby."
TEXT_B = "Kailangan ng tubig sa Room 5, tatlong bata."


class DbTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "reports.db"
        db.init_db(self.path)

    def row_count(self):
        with closing(sqlite3.connect(self.path)) as conn:
            return conn.execute("SELECT COUNT(*) FROM reports").fetchone()[0]


class InsertTest(DbTestCase):
    def test_first_insert_creates_one_queued_unclassified_row(self):
        client_id = str(uuid.uuid4())
        row, created = db.insert_report(self.path, client_id, TEXT_A, reporter="Ana")

        self.assertTrue(created)
        self.assertIsInstance(row["id"], int)
        self.assertEqual(row["client_id"], client_id)
        self.assertEqual(row["raw_text"], TEXT_A)
        self.assertEqual(row["reporter"], "Ana")
        self.assertEqual(row["status"], "queued")
        self.assertEqual(row["edited"], 0)
        self.assertEqual(row["resolved"], 0)
        for column in UNCLASSIFIED_COLUMNS:
            with self.subTest(column=column):
                self.assertIsNone(row[column])
        self.assertTrue(row["created_at"].endswith("+00:00"))
        self.assertEqual(self.row_count(), 1)
        self.assertEqual(db.list_reports(self.path), [row])

    def test_original_text_is_stored_unmodified(self):
        text = "  May sugatan sa labas.\n<script>alert(1)</script>  "
        row, _ = db.insert_report(self.path, str(uuid.uuid4()), text)
        self.assertEqual(row["raw_text"], text)
        self.assertIsNone(row["reporter"])

    def test_repeated_uuid_returns_original_and_keeps_one_row(self):
        client_id = str(uuid.uuid4())
        first, created_first = db.insert_report(self.path, client_id, TEXT_A, "Ana")
        second, created_second = db.insert_report(self.path, client_id, TEXT_B, "Ben")

        self.assertTrue(created_first)
        self.assertFalse(created_second)
        self.assertEqual(second, first)
        self.assertEqual(second["raw_text"], TEXT_A)
        self.assertEqual(second["reporter"], "Ana")
        self.assertEqual(self.row_count(), 1)

    def test_distinct_uuids_get_distinct_increasing_ids(self):
        ids = [db.insert_report(self.path, str(uuid.uuid4()), TEXT_A)[0]["id"] for _ in range(5)]
        self.assertEqual(len(set(ids)), 5)
        self.assertEqual(ids, sorted(ids))

    def test_ids_are_never_reused(self):
        first, _ = db.insert_report(self.path, str(uuid.uuid4()), TEXT_A)
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("DELETE FROM reports WHERE id = ?", (first["id"],))
        second, _ = db.insert_report(self.path, str(uuid.uuid4()), TEXT_B)
        self.assertGreater(second["id"], first["id"])


class ConcurrencyTest(DbTestCase):
    def run_threads(self, client_ids):
        barrier = threading.Barrier(len(client_ids))
        results = [None] * len(client_ids)
        errors = []

        def worker(index, client_id):
            try:
                barrier.wait()
                results[index] = db.insert_report(self.path, client_id, f"{TEXT_A} #{index}")
            except Exception as exc:  # surfaced below
                errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=(i, cid)) for i, cid in enumerate(client_ids)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertEqual(errors, [])
        return results

    def test_overlapping_inserts_of_one_uuid_yield_one_row(self):
        client_id = str(uuid.uuid4())
        results = self.run_threads([client_id] * 12)

        ids = {row["id"] for row, _ in results}
        self.assertEqual(len(ids), 1)
        self.assertEqual(sum(created for _, created in results), 1)
        winner = next(row for row, created in results if created)
        for row, _ in results:
            self.assertEqual(row, winner)
        self.assertEqual(self.row_count(), 1)

    def test_overlapping_inserts_of_distinct_uuids_get_distinct_ids(self):
        results = self.run_threads([str(uuid.uuid4()) for _ in range(12)])
        ids = [row["id"] for row, _ in results]
        self.assertEqual(len(set(ids)), 12)
        self.assertTrue(all(created for _, created in results))
        self.assertEqual(self.row_count(), 12)


class PersistenceTest(DbTestCase):
    def test_reinitialization_and_new_connections_keep_rows(self):
        a, _ = db.insert_report(self.path, str(uuid.uuid4()), TEXT_A, "Ana")
        b, _ = db.insert_report(self.path, str(uuid.uuid4()), TEXT_B)
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("UPDATE reports SET resolved = 1, edited = 1 WHERE id = ?", (b["id"],))
        b = {**b, "resolved": 1, "edited": 1}

        db.init_db(self.path)
        db.init_db(self.path)

        self.assertEqual(db.list_reports(self.path), [a, b])

        again, created = db.insert_report(self.path, a["client_id"], TEXT_B, "Ben")
        self.assertFalse(created)
        self.assertEqual(again, a)
        self.assertEqual(self.row_count(), 2)

    def test_committed_rows_are_visible_to_a_plain_connection(self):
        row, _ = db.insert_report(self.path, str(uuid.uuid4()), TEXT_A)
        with closing(sqlite3.connect(self.path)) as conn:
            stored = conn.execute(
                "SELECT raw_text, status FROM reports WHERE id = ?", (row["id"],)
            ).fetchone()
        self.assertEqual(stored, (TEXT_A, "queued"))


class OrderingTest(DbTestCase):
    def test_list_is_oldest_first_with_id_tie_breaker(self):
        rows = [db.insert_report(self.path, str(uuid.uuid4()), TEXT_A)[0] for _ in range(3)]
        same_time = "2026-10-09T00:00:00.000000+00:00"
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("UPDATE reports SET created_at = ?", (same_time,))
            conn.execute(
                "UPDATE reports SET created_at = ? WHERE id = ?",
                ("2026-10-08T23:59:59.000000+00:00", rows[2]["id"]),
            )

        listed = [row["id"] for row in db.list_reports(self.path)]
        self.assertEqual(listed, [rows[2]["id"], rows[0]["id"], rows[1]["id"]])
        self.assertEqual([row["id"] for row in db.list_reports(self.path)], listed)

    def test_empty_database_lists_nothing(self):
        self.assertEqual(db.list_reports(self.path), [])


class FailureTest(DbTestCase):
    def test_locked_database_raises_and_stores_nothing(self):
        holder = sqlite3.connect(self.path, isolation_level=None)
        self.addCleanup(holder.close)
        holder.execute("BEGIN EXCLUSIVE")

        with mock.patch.object(db, "BUSY_TIMEOUT", 0.1):
            with self.assertRaises(sqlite3.OperationalError):
                db.insert_report(self.path, str(uuid.uuid4()), TEXT_A)

        holder.execute("ROLLBACK")
        self.assertEqual(self.row_count(), 0)

    def test_failed_insert_raises_and_leaves_no_row(self):
        with closing(sqlite3.connect(self.path)) as conn:
            conn.execute(
                "CREATE TRIGGER fail_insert AFTER INSERT ON reports"
                " BEGIN SELECT RAISE(ABORT, 'simulated storage failure'); END"
            )

        with self.assertRaises(sqlite3.DatabaseError):
            db.insert_report(self.path, str(uuid.uuid4()), TEXT_A)
        self.assertEqual(self.row_count(), 0)

    def test_failed_commit_is_not_returned_as_acknowledged(self):
        real_connect = db.connect

        class FailingCommit:
            def __init__(self, conn):
                self._conn = conn

            def execute(self, sql, *args):
                if sql == "COMMIT":
                    raise sqlite3.OperationalError("simulated commit failure")
                return self._conn.execute(sql, *args)

            def __getattr__(self, name):
                return getattr(self._conn, name)

        with mock.patch.object(db, "connect", lambda path: FailingCommit(real_connect(path))):
            with self.assertRaises(sqlite3.OperationalError):
                db.insert_report(self.path, str(uuid.uuid4()), TEXT_A)
        self.assertEqual(self.row_count(), 0)

    def test_missing_directory_raises(self):
        missing = self.path.parent / "absent" / "reports.db"
        with self.assertRaises(sqlite3.OperationalError):
            db.insert_report(missing, str(uuid.uuid4()), TEXT_A)


if __name__ == "__main__":
    unittest.main()
