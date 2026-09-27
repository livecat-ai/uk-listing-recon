import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from recon.collector import Collector, collector_lock
from recon.db import Database
from recon.google import GoogleCollector, RedirectChallenge
from recon.queue import SearchQueue


def result(number=1):
    return {"title": f"History {number}", "url": f"https://example.com/{number}", "snippet": "Acquisition details"}


def observation(records, next_url=None, exhausted=False):
    return {"state": "results" if records else "empty", "results": records,
            "next_url": next_url, "exhausted": exhausted}


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path = self.root / "research.sqlite3"
        self.db = Database(self.path)
        company = self.db.add_company("Example")
        self.first, self.second = self.db.plan(company, [("name", "Example"), ("delisted", "Example delisted")], "google-manual", depth=2)
        self.time = datetime(2026, 9, 26, tzinfo=timezone.utc)
        self.page = Mock()
        self.page.url = "https://search.example/first"
        self.page.goto.return_value = Mock(status=200)
        self.page.content.return_value = "<html>Saved page</html>"
        self.page.wait_for_timeout.side_effect = self.advance
        self.adapter = Mock(version="test-1")
        self.adapter.initial_url.return_value = self.page.url
        self.adapter.inspect.return_value = observation([result(1), result(2)])
        self.adapter.resolve.side_effect = lambda page, records: records
        self.prompt = Mock(return_value="")
        self.build_collector()

    def advance(self, milliseconds):
        self.time += timedelta(milliseconds=milliseconds)

    def build_collector(self):
        self.queue = SearchQueue(self.db, clock=lambda: self.time)
        self.collector = Collector(self.db, self.page, self.root / "captures", prompt=self.prompt,
            log=Mock(), queue=self.queue, adapters={"google-manual": lambda: self.adapter})

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def test_automatic_completion_and_restart_skip_finished_searches(self):
        self.assertEqual(self.collector.run(limit=1)["completed_this_session"], 1)
        records = self.db.result_records()
        self.assertEqual([r["rank"] for r in records], [1, 2])
        self.assertTrue(all(r["useful"] is None for r in records))
        capture = self.db.rows("SELECT * FROM captures")[0]
        manifest = json.loads(Path(capture["manifest_path"]).read_text())
        self.assertTrue(Path(manifest["pages"][0]["html"]).exists())
        self.db.close()
        self.db = Database(self.path)
        self.build_collector()
        self.assertEqual(self.collector.run()["status"], "finished")
        self.assertEqual(len(self.db.result_records()), 4)
        self.assertEqual(self.page.goto.call_count, 2)
        self.prompt.assert_not_called()

    def test_captcha_requires_human_and_resumes_at_slower_rate(self):
        self.adapter.inspect.return_value = {"state": "captcha", "results": []}

        def solve(_):
            self.assertEqual(self.queue.status()["pause_reason"], "captcha")
            self.assertEqual(self.db.require("runs", self.first)["status"], "planned")
            self.adapter.inspect.return_value = observation([result(1), result(2)])
            return ""

        self.prompt.side_effect = solve
        self.assertEqual(self.collector.run(limit=1)["completed_this_session"], 1)
        self.assertEqual(self.queue.status()["interval_seconds"], 65)
        self.assertFalse(self.queue.status()["paused"])
        self.page.goto.assert_called_once()

    def test_captcha_on_later_page_that_returns_to_first_page_reloads_intended_page(self):
        second = "https://search.example/second"
        state = {"page": 1}
        first_page = observation([result(1)], second)
        def navigate(url, **kwargs):
            state["page"] = 2 if url == second else 1
            self.page.url = url
            self.adapter.inspect.return_value = first_page if state["page"] == 1 else \
                ({"state": "captcha", "results": []} if not state.get("solved") else observation([result(2)], exhausted=True))
            return Mock(status=200)
        def solve(_):
            state["solved"] = True
            self.page.url = self.adapter.initial_url.return_value
            self.adapter.inspect.return_value = first_page
            return ""
        self.page.goto.side_effect = navigate
        self.prompt.side_effect = solve
        self.adapter.same_page.side_effect = lambda current, requested: current == requested
        self.assertEqual(self.collector.run(limit=1)["completed_this_session"], 1)
        self.assertEqual([r["url"] for r in self.db.result_records()], ["https://example.com/1", "https://example.com/2"])
        self.assertEqual(self.page.goto.call_count, 3)

    def test_quitting_captcha_keeps_pending_run_and_no_empty_success(self):
        self.adapter.inspect.return_value = {"state": "captcha", "results": []}
        self.prompt.return_value = "q"
        self.assertEqual(self.collector.run()["status"], "paused")
        self.assertEqual(self.db.result_records(), [])
        self.assertEqual(self.db.require("runs", self.first)["status"], "planned")
        self.assertTrue(self.queue.status()["paused"])

    def test_pagination_obeys_pacing_and_combines_ranks(self):
        pages = [observation([result(1)], "https://search.example/second"), observation([result(2)], exhausted=True)]
        def navigate(*args, **kwargs):
            self.adapter.inspect.return_value = pages.pop(0)
            return Mock(status=200)
        self.page.goto.side_effect = navigate
        self.assertEqual(self.collector.run(limit=1)["completed_this_session"], 1)
        self.assertEqual([r["rank"] for r in self.db.result_records()], [1, 2])
        self.assertGreaterEqual((self.time - datetime(2026, 9, 26, tzinfo=timezone.utc)).total_seconds(), 60)

    def test_unknown_page_is_never_counted_as_zero_results(self):
        self.adapter.inspect.return_value = {"state": "unknown", "results": []}
        self.prompt.return_value = "q"
        self.collector.run(limit=1)
        self.assertEqual(self.db.rows("SELECT * FROM captures"), [])
        self.assertEqual(self.db.require("runs", self.first)["status"], "planned")

    def test_fewer_results_without_end_marker_remain_pending(self):
        self.adapter.inspect.return_value = observation([result(1)])
        self.assertEqual(self.collector.run(limit=1)["status"], "paused")
        self.assertEqual(self.db.result_records(), [])
        self.assertTrue(self.queue.status()["paused"])

    def test_confirmed_zero_results_complete_automatically(self):
        self.adapter.inspect.return_value = observation([], exhausted=True)
        self.assertEqual(self.collector.run(limit=1)["completed_this_session"], 1)
        self.assertEqual(self.db.rows("SELECT completion_reason FROM captures")[0]["completion_reason"], "no_results")

    def test_atomic_capture_rolls_back_when_completion_fails(self):
        with patch.object(self.db, "complete", side_effect=ValueError("Failed completion")):
            with self.assertRaisesRegex(ValueError, "Failed completion"):
                self.collector.run(limit=1)
        self.assertEqual(self.db.result_records(), [])
        self.assertEqual(self.db.rows("SELECT * FROM captures"), [])
        self.assertEqual(self.db.require("runs", self.first)["status"], "planned")

    def test_partial_manual_capture_is_preserved(self):
        self.db.import_results(self.first, [{**result(), "rank": 1}])
        self.assertEqual(self.collector.run()["status"], "paused")
        self.page.goto.assert_not_called()
        self.assertEqual(len(self.db.result_records()), 1)

    def test_navigation_failures_back_off_and_leave_search_pending(self):
        self.page.goto.side_effect = OSError("Connection lost")
        outcome = self.collector.run(limit=1)
        self.assertEqual(outcome["status"], "paused")
        self.assertEqual(self.page.goto.call_count, 3)
        self.assertEqual(self.queue.status()["interval_seconds"], 480)
        self.assertEqual(self.db.require("runs", self.first)["status"], "planned")
        self.assertEqual(self.db.result_records(), [])

    def test_interrupt_after_first_page_restarts_only_current_search(self):
        first_page = observation([result(1)], "https://search.example/second")
        def navigate(*args, **kwargs):
            if self.page.goto.call_count == 2:
                raise KeyboardInterrupt
            self.adapter.inspect.return_value = first_page
            return Mock(status=200)
        self.page.goto.side_effect = navigate
        self.assertEqual(self.collector.run(limit=1)["status"], "paused")
        self.assertEqual(self.db.result_records(), [])
        self.assertEqual(self.queue.status()["active_run_id"], self.first)
        self.page.goto.side_effect = None
        self.adapter.inspect.return_value = observation([result(1), result(2)])
        self.assertEqual(self.collector.run(limit=1)["completed_this_session"], 1)
        self.assertEqual(len(self.db.result_records()), 2)
        self.assertEqual(self.db.require("runs", self.second)["status"], "planned")

    def paginate(self, *pages):
        pages = list(pages)
        def navigate(*args, **kwargs):
            self.adapter.inspect.return_value = pages.pop(0)
            return Mock(status=200)
        self.page.goto.side_effect = navigate

    def test_repeat_on_later_page_is_skipped_and_next_result_used(self):
        repeat = {**result(1), "url": "https://example.com/1#:~:text=history"}
        self.paginate(observation([result(1)], "https://search.example/second"),
                      observation([repeat, result(2)], "https://search.example/third"))
        self.assertEqual(self.collector.run(limit=1)["completed_this_session"], 1)
        self.assertEqual([r["url"] for r in self.db.result_records()], ["https://example.com/1", "https://example.com/2"])
        manifest = json.loads(Path(self.db.rows("SELECT manifest_path FROM captures")[0]["manifest_path"]).read_text())
        self.assertEqual(manifest["skipped_duplicates"][0]["page"], 2)
        self.assertEqual(self.page.goto.call_count, 2)

    def test_repeats_that_leave_run_short_continue_to_next_page(self):
        self.paginate(observation([result(1)], "https://search.example/second"),
                      observation([result(1)], "https://search.example/third"),
                      observation([result(2)], exhausted=True))
        self.collector.run(limit=1)
        self.assertEqual([r["rank"] for r in self.db.result_records()], [1, 2])
        self.assertEqual(self.page.goto.call_count, 3)

    def test_repeats_before_confirmed_end_complete_as_exhausted(self):
        self.paginate(observation([result(1)], "https://search.example/second"),
                      observation([result(1)], exhausted=True))
        self.collector.run(limit=1)
        self.assertEqual(len(self.db.result_records()), 1)
        self.assertEqual(self.db.rows("SELECT completion_reason FROM captures")[0]["completion_reason"], "results_exhausted")

    def test_repeats_without_end_or_next_page_leave_run_pending(self):
        page = observation([result(1), result(1)])
        page["results"][1] = {**result(1)}
        self.adapter.inspect.return_value = page
        self.assertEqual(self.collector.run(limit=1)["status"], "paused")
        self.assertEqual(self.db.result_records(), [])
        self.assertEqual(self.db.require("runs", self.first)["status"], "planned")

    def test_only_kept_results_are_resolved(self):
        self.adapter.inspect.return_value = observation([result(1), result(2), result(3)])
        self.collector.run(limit=1)
        self.assertEqual(len(self.adapter.resolve.call_args.args[1]), 2)

    def test_captcha_while_resolving_links_waits_for_human(self):
        attempts = []
        def resolve(page, records):
            attempts.append(1)
            if len(attempts) == 1:
                raise RedirectChallenge("CAPTCHA while resolving")
            return records
        self.adapter.resolve.side_effect = resolve
        self.assertEqual(self.collector.run(limit=1)["completed_this_session"], 1)
        self.prompt.assert_called_once()
        self.assertEqual(self.queue.status()["interval_seconds"], 65)
        self.assertFalse(self.queue.status()["paused"])

    def test_unresolvable_link_leaves_run_pending(self):
        self.adapter.resolve.side_effect = ValueError("HTTP 200")
        self.assertEqual(self.collector.run(limit=1)["status"], "paused")
        self.assertEqual(self.db.result_records(), [])
        self.assertEqual(self.db.require("runs", self.first)["status"], "planned")
        self.assertTrue(self.queue.status()["paused"])

    def overview(self):
        return {"text": "Example plc was acquired. [Example]", "error": "", "sources": [
            {"position": 1, "site": "Example", "title": "Deal", "excerpt": "Agreed deal", "url": None,
             "redirect": "https://www.google.com/goto?url=A"},
            {"position": 2, "site": "Other", "title": "Report", "excerpt": "", "url": None,
             "redirect": "https://www.google.com/goto?url=B"}]}

    def test_ai_overview_and_sources_saved_with_capture(self):
        self.adapter.inspect.return_value = {**observation([result(1), result(2)]), "ai_overview": self.overview()}
        self.adapter.resolve_sources.side_effect = lambda page, sources: [
            {**sources[0], "url": "https://www.example.com/deal"}, sources[1]]
        self.assertEqual(self.collector.run(limit=1)["completed_this_session"], 1)
        overview = self.db.rows("SELECT * FROM ai_overviews")[0]
        self.assertEqual((overview["run_id"], overview["text"]), (self.first, "Example plc was acquired. [Example]"))
        sources = self.db.rows("SELECT * FROM ai_overview_sources ORDER BY position")
        self.assertEqual([(s["url"], s["domain"]) for s in sources], [("https://www.example.com/deal", "example.com"), (None, "")])
        self.assertEqual(sources[1]["redirect_url"], "https://www.google.com/goto?url=B")
        manifest = json.loads(Path(self.db.rows("SELECT manifest_path FROM captures")[0]["manifest_path"]).read_text())
        self.assertEqual(manifest["ai_overview"]["sources"][0]["url"], "https://www.example.com/deal")
        self.assertEqual(len(self.db.result_records()), 2)

    def test_late_ai_overview_is_awaited_and_empty_one_is_flagged(self):
        empty = {"text": "", "sources": [], "error": ""}
        pages = [{**observation([result(1), result(2)]), "ai_overview": empty}] * 3
        self.adapter.inspect.side_effect = lambda page, run: pages.pop(0) if pages else \
            {**observation([result(1), result(2)]), "ai_overview": self.overview()}
        self.adapter.resolve_sources.side_effect = lambda page, sources: sources
        self.collector.run(limit=1)
        self.assertEqual(self.db.rows("SELECT text FROM ai_overviews")[0]["text"], "Example plc was acquired. [Example]")
        self.adapter.inspect.side_effect = None
        self.adapter.inspect.return_value = {**observation([result(1), result(2)]), "ai_overview": empty}
        self.collector.run(limit=1)
        flagged = self.db.rows("SELECT * FROM ai_overviews WHERE run_id=?", (self.second,))[0]
        self.assertEqual(flagged["text"], "")
        self.assertIn("empty", flagged["error"])

    def test_ai_overview_arriving_after_results_is_captured(self):
        without = observation([result(1), result(2)])
        reads = [without, without, without]
        self.adapter.inspect.side_effect = lambda page, run: reads.pop(0) if reads else \
            {**without, "ai_overview": self.overview()}
        self.adapter.resolve_sources.side_effect = lambda page, sources: sources
        self.collector.run(limit=1)
        self.assertEqual(self.db.rows("SELECT text FROM ai_overviews")[0]["text"], "Example plc was acquired. [Example]")

    def test_streaming_ai_overview_is_awaited_until_complete(self):
        partial = {**self.overview(), "text": "Example plc was", "complete": False}
        reads = [partial] * 4
        self.adapter.inspect.side_effect = lambda page, run: {**observation([result(1), result(2)]),
            "ai_overview": reads.pop(0) if reads else {**self.overview(), "complete": True}}
        self.adapter.resolve_sources.side_effect = lambda page, sources: sources
        self.collector.run(limit=1)
        saved = self.db.rows("SELECT text, error FROM ai_overviews")[0]
        self.assertEqual((saved["text"], saved["error"]), ("Example plc was acquired. [Example]", ""))
        self.adapter.inspect.side_effect = None
        self.adapter.inspect.return_value = {**observation([result(1), result(2)]), "ai_overview": partial}
        self.collector.run(limit=1)
        flagged = self.db.rows("SELECT * FROM ai_overviews WHERE run_id=?", (self.second,))[0]
        self.assertIn("incomplete", flagged["error"])

    def test_no_ai_overview_stores_nothing_extra(self):
        self.collector.run(limit=1)
        self.assertEqual(self.db.rows("SELECT * FROM ai_overviews"), [])
        self.adapter.resolve_sources.assert_not_called()

    def test_captcha_while_resolving_overview_sources_waits_for_human(self):
        self.adapter.inspect.return_value = {**observation([result(1), result(2)]), "ai_overview": self.overview()}
        calls = []
        def resolve(page, sources):
            calls.append(1)
            if len(calls) == 1:
                raise RedirectChallenge("CAPTCHA")
            return sources
        self.adapter.resolve_sources.side_effect = resolve
        self.assertEqual(self.collector.run(limit=1)["completed_this_session"], 1)
        self.prompt.assert_called_once()
        self.assertEqual(len(self.db.rows("SELECT * FROM ai_overview_sources")), 2)

    def test_collector_lock_excludes_second_process_and_releases(self):
        with collector_lock(self.path):
            with self.assertRaisesRegex(ValueError, "already using"):
                with collector_lock(self.path):
                    self.fail("Second collector acquired lock")
        with collector_lock(self.path):
            pass


class GoogleRedirectTests(unittest.TestCase):
    def setUp(self):
        self.page = Mock()
        self.adapter = GoogleCollector()
        self.adapter.redirect_gap_ms = 0
        self.redirect = "https://www.google.com/goto?url=TOKEN"

    def respond(self, status, location):
        self.page.context.request.get.return_value = Mock(status=status, headers={"location": location})

    def test_location_header_becomes_result_url_without_following(self):
        self.respond(302, "https://example.org/annual-report.PDF")
        records = self.adapter.resolve(self.page, [
            {"title": "Direct", "url": "https://example.com/", "redirect": None},
            {"title": "Report", "url": None, "redirect": self.redirect, "page_type": "unknown"}])
        self.assertEqual(records[0]["url"], "https://example.com/")
        self.assertEqual(records[1]["url"], "https://example.org/annual-report.PDF")
        self.assertEqual(records[1]["page_type"], "pdf")
        self.page.context.request.get.assert_called_once_with(self.redirect, max_redirects=0, timeout=30000)

    def test_unresolvable_overview_source_keeps_redirect_only(self):
        self.page.context.request.get.side_effect = [Mock(status=200, headers={}),
            Mock(status=302, headers={"location": "https://example.com/deal"})]
        sources = self.adapter.resolve_sources(self.page, [
            {"position": 1, "title": "A", "url": None, "redirect": self.redirect},
            {"position": 2, "title": "B", "url": None, "redirect": self.redirect},
            {"position": 3, "title": "C", "url": "https://example.org/", "redirect": None}])
        self.assertEqual([s["url"] for s in sources], [None, "https://example.com/deal", "https://example.org/"])
        self.page.context.request.get.side_effect = None
        self.respond(302, "https://www.google.com/sorry/index")
        with self.assertRaises(RedirectChallenge):
            self.adapter.resolve_sources(self.page, [{"position": 1, "title": "A", "url": None, "redirect": self.redirect}])

    def test_same_page_compares_query_and_result_offset(self):
        page2 = "https://www.google.com/search?q=Example&start=10"
        self.assertTrue(self.adapter.same_page("https://www.google.com/search?q=Example&start=10&sei=x", page2))
        self.assertFalse(self.adapter.same_page("https://www.google.com/search?q=Example&sei=x", page2))
        self.assertTrue(self.adapter.same_page("https://www.google.com/search?q=Example", "https://www.google.com/search?q=Example&gl=uk"))

    def test_sorry_redirect_is_a_challenge_and_other_responses_fail(self):
        self.respond(302, "https://www.google.com/sorry/index?continue=x")
        with self.assertRaises(RedirectChallenge):
            self.adapter.resolve(self.page, [{"title": "A", "url": None, "redirect": self.redirect}])
        self.respond(200, "")
        with self.assertRaisesRegex(ValueError, "HTTP 200"):
            self.adapter.resolve(self.page, [{"title": "A", "url": None, "redirect": self.redirect}])


if __name__ == "__main__":
    unittest.main()
