import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from recon.cli import execute, parser
from recon.db import Database, SCHEMA_VERSION
from recon.queue import SearchQueue


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "research.sqlite3"
        self.db = Database(self.path)
        company = self.db.add_company("Example PLC")
        self.first, self.second = self.db.plan(company, [("name", "Example"), ("delisted", "Example delisted")], "google-manual")
        self.time = datetime(2026, 9, 26, 12, tzinfo=timezone.utc)
        self.opener = Mock(return_value=True)
        self.queue = SearchQueue(self.db, clock=lambda: self.time, opener=self.opener)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def restart(self):
        self.db.close()
        self.db = Database(self.path)
        self.queue = SearchQueue(self.db, clock=lambda: self.time, opener=self.opener)

    def test_restart_keeps_active_run_and_cannot_repeat_by_accident(self):
        self.assertEqual(self.queue.next(open_browser=True)["run_id"], self.first)
        self.db.import_results(self.first, [{"rank": 1, "title": "History", "url": "https://example.com"}])
        self.restart()
        self.assertEqual(self.queue.next(open_browser=True)["action"], "in_progress")
        self.assertEqual(len(self.db.result_records()), 1)
        self.opener.assert_called_once()
        self.db.complete(self.first, self.time.isoformat())
        self.assertEqual(self.queue.next(open_browser=True)["action"], "cooldown")
        self.time += timedelta(seconds=60)
        self.assertEqual(self.queue.next(open_browser=True)["run_id"], self.second)
        self.assertEqual(self.opener.call_count, 2)

    def test_captcha_pause_persists_and_backoff_is_once_per_challenge(self):
        self.queue.next(open_browser=True)
        self.assertEqual(self.queue.pause(captcha=True)["interval_seconds"], 65)
        self.queue.pause(captcha=True)
        self.restart()
        self.assertEqual(self.queue.status()["interval_seconds"], 65)
        self.assertEqual(self.queue.next(open_browser=True, reopen=True)["action"], "paused")
        self.queue.resume()
        self.assertEqual(self.queue.next(open_browser=True, reopen=True)["action"], "cooldown")
        self.time += timedelta(seconds=65)
        self.assertEqual(self.queue.next(open_browser=True)["action"], "in_progress")
        self.assertEqual(self.queue.next(open_browser=True, reopen=True)["action"], "opened")
        self.assertEqual(self.queue.pause(captcha=True)["interval_seconds"], 70)

    def test_status_distinguishes_running_collector_from_interrupted_search(self):
        from recon.collector import collector_lock
        path = self.db.conn.execute("PRAGMA database_list").fetchone()[2]
        self.queue.next(open_browser=True)
        self.assertEqual(self.queue.status()["action"], "interrupted")
        self.assertIn("redoes it", self.queue.status()["note"])
        with collector_lock(path):
            status = self.queue.status()
            self.assertEqual((status["action"], "note" in status), ("in_progress", False))

    def test_manual_pause_does_not_slow_down_or_clear_captcha(self):
        self.queue.pause()
        self.assertEqual(self.queue.status()["interval_seconds"], 60)
        self.queue.resume()
        self.queue.pause(captcha=True)
        self.queue.pause()
        self.queue.pause(captcha=True)
        self.assertEqual(self.queue.status()["interval_seconds"], 65)
        self.assertEqual(self.queue.status()["pause_reason"], "captcha")

    def test_browser_failure_and_crash_preserve_position_and_cooldown(self):
        self.opener.return_value = False
        self.assertEqual(self.queue.next(open_browser=True)["action"], "browser_failed")
        self.restart()
        self.assertEqual(self.queue.status()["active_run_id"], self.first)
        self.assertEqual(self.queue.next(open_browser=True, reopen=True)["action"], "cooldown")
        self.time += timedelta(seconds=60)
        self.opener.side_effect = KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            self.queue.next(open_browser=True, reopen=True)
        self.restart()
        self.assertEqual(self.queue.next(open_browser=True)["action"], "in_progress")

    def test_completion_during_pause_keeps_next_search_paused(self):
        self.queue.next(open_browser=True)
        self.queue.pause(captcha=True)
        self.db.complete(self.first, self.time.isoformat())
        self.assertEqual(self.queue.next(open_browser=True)["action"], "paused")
        self.queue.resume()
        self.time += timedelta(seconds=65)
        self.assertEqual(self.queue.next(open_browser=True)["run_id"], self.second)

    def test_direct_search_open_obeys_active_run_and_pacing(self):
        with patch("recon.queue.webbrowser.open", return_value=True) as browser:
            execute(self.db, parser().parse_args(["search", str(self.first), "--open"]))
            with self.assertRaisesRegex(ValueError, "active run"):
                execute(self.db, parser().parse_args(["search", str(self.second), "--open"]))
            self.db.complete(self.first, self.time.isoformat())
            response = execute(self.db, parser().parse_args(["search", str(self.second), "--open"]))
            self.assertEqual(response["action"], "cooldown")
            browser.assert_called_once()

    def test_preview_does_not_advance_or_launch_browser(self):
        self.assertEqual(self.queue.next()["run_id"], self.first)
        self.assertIsNone(self.queue.status()["active_run_id"])
        self.opener.assert_not_called()

    def test_other_process_cannot_open_reserved_run(self):
        other = Database(self.path)
        try:
            other_queue = SearchQueue(other, clock=lambda: self.time, opener=self.opener)
            self.queue.next(open_browser=True)
            self.assertEqual(other_queue.next(open_browser=True)["action"], "in_progress")
            self.opener.assert_called_once()
        finally:
            other.close()

    def test_config_increases_cooldown_and_rejects_invalid_intervals(self):
        self.queue.next(open_browser=True)
        self.assertEqual(self.queue.configure(120)["wait_seconds"], 120)
        self.assertEqual(self.queue.configure(30)["wait_seconds"], 120)
        for invalid in (0, -1, 86401):
            with self.assertRaises(ValueError):
                self.queue.configure(invalid)

    def test_finished_and_exported_audit_trail(self):
        self.queue.next(open_browser=True)
        self.db.complete(self.first, self.time.isoformat())
        self.db.complete(self.second, self.time.isoformat())
        self.assertEqual(self.queue.next(open_browser=True)["action"], "finished")
        output = Path(self.temp.name) / "export"
        self.db.export(output)
        snapshot = json.loads((output / "snapshot.json").read_text())
        self.assertEqual(snapshot["schema_version"], SCHEMA_VERSION)
        self.assertEqual([e["event"] for e in snapshot["queue_events"]], ["open_requested", "browser_opened", "completed"])

    def test_version_one_upgrade_preserves_existing_research(self):
        self.db.close()
        with sqlite3.connect(self.path) as connection:
            connection.executescript("DROP TABLE queue_events; DROP TABLE search_queue; PRAGMA user_version=1;")
        self.db = Database(self.path)
        self.assertEqual(len(self.db.rows("SELECT * FROM runs")), 2)
        self.assertEqual(self.db.require("search_queue", 1)["interval_seconds"], 60)
        self.assertEqual(self.db.conn.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)


if __name__ == "__main__":
    unittest.main()
