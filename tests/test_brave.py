import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from recon.brave import BraveCollector
from recon.db import Database
from recon.queue import SearchQueue


def page(results, more=False):
    return json.dumps({"query": {"more_results_available": more},
                       "web": {"results": results}}).encode()


def item(n, url=None):
    return {"title": f"<strong>History</strong> {n}", "url": url or f"https://example.com/{n}",
            "description": f"Acquired &amp; delisted {n}", "age": "5 March 2025"}


class BraveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / "research.sqlite3")
        company = self.db.add_company("Example")
        self.google = self.db.plan(company, [("name", "Example")], "google-manual", depth=3)[0]
        self.run_id = self.db.plan(company, [("name", "Example")], "brave-api", depth=3)[0]
        self.responses = []
        self.fetch = Mock(side_effect=lambda url, key: self.responses.pop(0))
        self.sleep = Mock()

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def collector(self):
        return BraveCollector(self.db, Path(self.temp.name) / "captures", key="secret",
                              fetch=self.fetch, sleep=self.sleep, log=Mock(), interval=0)

    def test_single_request_fills_depth_with_clean_text_and_no_key_saved(self):
        self.responses = [(200, {}, page([item(1), item(2, "https://example.com/1#top"), item(3), item(4)], more=True))]
        outcome = self.collector().run()
        self.assertEqual((outcome["status"], outcome["requests"]), ("finished", 1))
        records = self.db.result_records()
        self.assertEqual([r["url"] for r in records], ["https://example.com/1", "https://example.com/3", "https://example.com/4"])
        self.assertEqual((records[0]["title"], records[0]["snippet"], records[0]["page_date"]),
                         ("History 1", "Acquired & delisted 1", "5 March 2025"))
        capture = self.db.rows("SELECT * FROM captures")[0]
        self.assertEqual((capture["method"], capture["extractor_version"]), ("api", "brave-api-1"))
        url = self.fetch.call_args.args[0]
        self.assertIn("country=GB", url)
        self.assertNotIn("secret", url)
        folder = Path(capture["manifest_path"]).parent
        self.assertFalse(any("secret" in p.read_text() for p in folder.iterdir()))

    def test_pages_until_depth_or_exhausted(self):
        self.responses = [(200, {}, page([item(1)], more=True)), (200, {}, page([item(2)], more=False))]
        self.collector().run()
        self.assertEqual(self.db.rows("SELECT completion_reason FROM captures")[0]["completion_reason"], "results_exhausted")
        self.assertIn("offset=1", self.fetch.call_args.args[0])

    def test_rate_limit_retries_and_bad_key_stops_leaving_run_planned(self):
        self.responses = [(429, {"Retry-After": "3"}, b""), (200, {}, page([item(1), item(2), item(3)]))]
        self.collector().run()
        self.sleep.assert_any_call(3.0)
        company = self.db.rows("SELECT id FROM companies")[0]["id"]
        other = self.db.plan(company, [("delisted", "Example delisted")], "brave-api", depth=3)[0]
        self.responses = [(401, {}, b"")]
        self.assertEqual(self.collector().run()["status"], "stopped")
        self.assertEqual(self.db.require("runs", other)["status"], "planned")

    def test_browser_queue_ignores_api_runs(self):
        with self.db.conn:
            self.db.conn.execute("UPDATE runs SET status='complete' WHERE id=?", (self.google,))
        status = SearchQueue(self.db).status()
        self.assertEqual((status["next_run"], status["remaining_runs"]), (None, 0))


if __name__ == "__main__":
    unittest.main()
