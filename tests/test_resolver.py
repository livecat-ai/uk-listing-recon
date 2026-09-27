import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from recon.collector import Collector
from recon.db import Database
from recon.google import RedirectChallenge
from recon.queue import SearchQueue
from recon.resolver import LinkResolver


def candidate(number, url=None):
    return {"title": f"History {number}", "url": url, "redirect": f"https://www.google.com/goto?url=T{number}",
            "snippet": "Acquisition details"}


class DeferredCollectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = Database(self.root / "research.sqlite3")
        company = self.db.add_company("Example")
        self.first, self.second = self.db.plan(company, [("name", "Example"), ("delisted", "Example delisted")],
                                               "google-manual", depth=2)
        self.page = Mock(url="https://search.example/first")
        self.page.goto.return_value = Mock(status=200)
        self.page.content.return_value = "<html>Saved page</html>"
        self.adapter = Mock(version="test-1")
        self.adapter.initial_url.return_value = self.page.url
        self.destinations = {f"https://www.google.com/goto?url=T{n}": f"https://example.com/{n}" for n in range(1, 6)}
        self.adapter.resolve.side_effect = lambda page, records: [
            {**records[0], "url": self.destinations[records[0]["redirect"]]}]
        self.prompt = Mock(return_value="")

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def collect(self, pages, overview=None):
        pages = list(pages)
        def navigate(*args, **kwargs):
            page = pages.pop(0)
            self.adapter.inspect.return_value = {"state": "results", "results": page[0], "next_url": page[1],
                                                 "exhausted": page[1] is None, "ai_overview": overview}
            return Mock(status=200)
        self.page.goto.side_effect = navigate
        queue = SearchQueue(self.db)
        queue.configure(1)
        collector = Collector(self.db, self.page, self.root / "captures", prompt=self.prompt, log=Mock(),
                              queue=queue, adapters={"google-manual": lambda: self.adapter}, defer_links=True)
        return collector.run(limit=1)

    def resolver(self, **kwargs):
        return LinkResolver(self.db, self.page, prompt=self.prompt, log=Mock(), interval=1,
                            adapters={"google-manual": lambda: self.adapter}, clock=lambda: 0, **kwargs)

    def test_collection_saves_links_without_lookups_and_keeps_spares(self):
        self.collect([([candidate(1)], "https://search.example/second"), ([candidate(2), candidate(3)], None)])
        self.adapter.resolve.assert_not_called()
        self.assertEqual(self.page.goto.call_count, 2)
        pending = self.db.pending_captures()[0]
        self.assertEqual([c["redirect"][-2:] for c in pending["payload"]["candidates"]], ["T1", "T2", "T3"])
        self.assertEqual(self.db.require("runs", self.first)["status"], "planned")
        status = SearchQueue(self.db).status()
        self.assertEqual((status["next_run"]["id"], status["runs_awaiting_links"], status["active_run_id"]),
                         (self.second, 1, None))

    def test_resolve_finishes_search_replacing_repeats_with_spares(self):
        self.destinations["https://www.google.com/goto?url=T2"] = "https://example.com/1#:~:text=again"
        self.collect([([candidate(1), candidate(2), candidate(3)], None)],
                     overview={"text": "Acquired. [Example]", "error": "", "sources": [
                         {"position": 1, "title": "Deal", "url": None, "redirect": "https://www.google.com/goto?url=T1"},
                         {"position": 2, "title": "Report", "url": None, "redirect": "https://www.google.com/goto?url=T4"}]})
        outcome = self.resolver().run()
        self.assertEqual((outcome["status"], outcome["resolved_this_session"]), ("finished", 1))
        self.assertEqual([r["url"] for r in self.db.result_records()], ["https://example.com/1", "https://example.com/3"])
        self.assertEqual(self.db.require("runs", self.first)["status"], "complete")
        self.assertEqual(self.db.pending_captures(), [])
        sources = self.db.rows("SELECT url FROM ai_overview_sources ORDER BY position")
        self.assertEqual([s["url"] for s in sources], ["https://example.com/1", "https://example.com/4"])
        # T1 was looked up once and reused for the overview source.
        self.assertEqual(outcome["lookups"], 4)
        manifest = json.loads(Path(self.db.rows("SELECT manifest_path FROM captures")[0]["manifest_path"]).read_text())
        self.assertEqual((manifest["links"], len(manifest["skipped_after_resolution"])), ("resolved", 1))

    def test_failed_link_leaves_search_pending_and_cached_links_survive(self):
        self.collect([([candidate(1), candidate(2)], None)])
        def resolve(page, records):
            if records[0]["redirect"].endswith("T2"):
                raise ValueError("Could not resolve result link (HTTP 200)")
            return [{**records[0], "url": "https://example.com/1"}]
        self.adapter.resolve.side_effect = resolve
        outcome = self.resolver().run()
        self.assertEqual(outcome["failed"][0]["run_id"], self.first)
        self.assertEqual(self.db.pending_captures()[0]["attempts"], 1)
        self.assertEqual(self.db.result_records(), [])
        self.assertEqual(self.db.cached_link("https://www.google.com/goto?url=T1"), "https://example.com/1")

    def test_captcha_opens_check_and_retries_after_researcher(self):
        self.collect([([candidate(1), candidate(2)], None)])
        calls = []
        def resolve(page, records):
            calls.append(1)
            if len(calls) == 1:
                raise RedirectChallenge("CAPTCHA", "https://www.google.com/sorry/index")
            return [{**records[0], "url": self.destinations[records[0]["redirect"]]}]
        self.adapter.resolve.side_effect = resolve
        self.assertEqual(self.resolver().run()["resolved_this_session"], 1)
        self.page.goto.assert_called_with("https://www.google.com/sorry/index", wait_until="domcontentloaded", timeout=45000)
        self.prompt.assert_called_once()

    def test_quitting_at_captcha_keeps_search_pending(self):
        self.collect([([candidate(1), candidate(2)], None)])
        self.adapter.resolve.side_effect = RedirectChallenge("CAPTCHA")
        self.prompt.return_value = "q"
        self.assertEqual(self.resolver().run()["status"], "stopped")
        self.assertEqual(len(self.db.pending_captures()), 1)

    def test_lookups_are_paced(self):
        self.collect([([candidate(1), candidate(2)], None)])
        times = iter([0, 0.5, 0.5, 5])
        resolver = LinkResolver(self.db, self.page, log=Mock(), interval=3,
                                adapters={"google-manual": lambda: self.adapter}, clock=lambda: next(times))
        self.page.wait_for_timeout.reset_mock()
        resolver.run()
        self.page.wait_for_timeout.assert_called_once_with(2500)


if __name__ == "__main__":
    unittest.main()
