"""Opt-in real-browser tests; all requests are fulfilled locally, never by Google.

Run with RECON_BROWSER_TESTS=1 .venv/bin/python -m unittest discover -s tests -v
"""

import os
import tempfile
import unittest
from pathlib import Path

from recon.collector import Collector
from recon.db import Database
from recon.google import GoogleCollector


@unittest.skipUnless(os.environ.get("RECON_BROWSER_TESTS") == "1", "Set RECON_BROWSER_TESTS=1 for local browser tests")
class GoogleBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(Path(__file__).resolve().parents[1] / "data/browser-engines"))
        cls.playwright = sync_playwright().start()
        channel = os.environ.get("RECON_TEST_CHANNEL", "firefox")
        try:
            if channel == "firefox":
                cls.browser = cls.playwright.firefox.launch(headless=True)
            else:
                cls.browser = cls.playwright.chromium.launch(channel=None if channel == "bundled" else channel, headless=True)
        except Exception:
            cls.playwright.stop()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.context = self.browser.new_context()
        self.context.route("**/*", lambda route: route.fulfill(status=200, content_type="text/html", body=self.html))
        self.page = self.context.new_page()
        self.adapter = GoogleCollector()
        self.run = {"query": "Example", "country": "uk", "language": "en"}

    def tearDown(self):
        self.context.close()

    def inspect(self, html, url="https://www.google.com/search?q=Example"):
        self.html = html
        self.page.goto(url)
        return self.adapter.inspect(self.page, self.run)

    def test_results_exclude_ads_preserve_snippets_and_unwrap_urls(self):
        data = self.inspect('''<input name="q" value="Example"><div id="search"><div id="rso">
            <div id="tads"><div class="g"><a href="https://ads.example"><h3>Advertisement</h3></a></div></div>
            <div class="MjjYud"><a href="https://example.com/history"><h3>Company history</h3></a>
                <div class="VwiC3b">Acquired by Buyer</div><time datetime="2001-06-15">15 June 2001</time></div>
            <div class="g"><a href="/url?q=https%3A%2F%2Fexample.org%2Freport.pdf"><h3>Annual report</h3></a>
                <div class="IsZvec">Historical listing details</div></div>
            </div></div><a id="pnnext" href="/search?q=Example&amp;start=10">Next</a>''')
        self.assertEqual(data["state"], "results")
        self.assertEqual(len(data["results"]), 2)
        self.assertEqual(data["results"][0]["snippet"], "Acquired by Buyer")
        self.assertEqual(data["results"][0]["page_date"], "2001-06-15")
        self.assertEqual(data["results"][1]["page_type"], "pdf")
        self.assertEqual(data["next_url"], "https://www.google.com/search?q=Example&start=10")

    def test_opaque_redirect_links_are_kept_for_resolution(self):
        data = self.inspect('''<input name="q" value="Example"><div id="rso">
            <div class="MjjYud"><a href="/goto?url=CAESopaque"><h3>Filing history</h3></a><div class="VwiC3b">Filings</div></div>
            <div class="MjjYud"><a href="/url?sa=t&amp;url=CAEStoken"><h3>Trust</h3></a>
                <div class="VwiC3b"><span class="YrbPuc"><span>113 pages</span></span><span class="YrbPuc"><span>23 Dec 2025</span></span> — NAV</div></div>
            <div class="MjjYud"><a href="/search?q=Other"><h3>Related search</h3></a></div>
            </div><a id="pnnext" href="/search?q=Example&amp;start=10">Next</a>''')
        self.assertEqual(data["state"], "results")
        self.assertEqual([r["redirect"] for r in data["results"]],
                         ["https://www.google.com/goto?url=CAESopaque", "https://www.google.com/url?sa=t&url=CAEStoken"])
        self.assertIsNone(data["results"][0]["url"])
        self.assertEqual(data["results"][0]["snippet"], "Filings")
        self.assertEqual(data["results"][0]["page_date"], "")
        self.assertEqual(data["results"][1]["page_date"], "23 Dec 2025")

    def test_ai_overview_text_includes_collapsed_part_and_sources(self):
        card = '''<li><a aria-label="Filing history - Companies House. Opens in a new tab." href="/goto?url=CAESfiling"></a>
            <span>Find and update company information</span><span>Filing history - Companies House</span>
            <span>19 Jul 2018, GAZ2, Final Gazette dissolved</span></li>'''
        data = self.inspect(f'''<input name="q" value="Example"><div role="heading">AI Overview</div>
            <div data-subtree="aimc"><h3>AI Mode reply for Example</h3><div class="pWvJNd"><div>
              <div>Example plc was dissolved in 2018.<span class="WBgIic"><a class="PMDqCb" aria-label="Companies House" href="/goto?url=CAESchip"></a>Companies House +1</span></div>
              <div role="heading">Key Details</div>
              <ul aria-hidden="true"><li>Dissolution: 19 July 2018.</li><li>Acquirer: Buyer plc.</li></ul></div></div>
            <div><div role="dialog"><ul>{card}</ul></div><ul>{card}
              <li><a aria-label="Report. Opens in a new tab." href="https://example.org/report.pdf"></a><span>Example</span><span>Report</span><span>Annual report</span></li></ul></div></div>
            <div>AI responses may include mistakes.</div>
            <div id="rso"><div class="MjjYud"><a href="https://example.com"><h3>History</h3></a></div></div>''')
        overview = data["ai_overview"]
        self.assertEqual(overview["text"], "Example plc was dissolved in 2018. [Companies House +1]\nKey Details\n"
                                           "- Dissolution: 19 July 2018.\n- Acquirer: Buyer plc.")
        self.assertEqual((overview["error"], overview["complete"]), ("", True))
        self.assertEqual([(s["position"], s["site"], s["title"]) for s in overview["sources"]],
                         [(1, "Find and update company information", "Filing history - Companies House"), (2, "Example", "Report")])
        self.assertEqual(overview["sources"][0]["excerpt"], "19 Jul 2018, GAZ2, Final Gazette dissolved")
        self.assertEqual(overview["sources"][0]["redirect"], "https://www.google.com/goto?url=CAESfiling")
        self.assertIsNone(overview["sources"][0]["url"])
        self.assertEqual(overview["sources"][1]["url"], "https://example.org/report.pdf")
        self.assertEqual(data["state"], "results")

    def test_missing_or_unrecognised_ai_overview(self):
        page = '<input name="q" value="Example"><div id="rso"><div class="g"><a href="https://example.com"><h3>History</h3></a></div></div>'
        self.assertIsNone(self.inspect(page)["ai_overview"])
        self.assertEqual(self.inspect('<div role="heading">AI Overview</div>' + page)["ai_overview"]["error"],
                         "Overview shown but not recognised")

    def test_challenge_consent_empty_and_unknown_are_distinct(self):
        self.assertEqual(self.inspect('<form id="captcha-form">Verify</form>')["state"], "captcha")
        self.assertEqual(self.inspect('<h1>Before you continue to Google</h1>')["state"], "consent")
        self.assertEqual(self.inspect('<div id="topstuff">Your search did not match any documents.</div>')["state"], "empty")
        self.assertEqual(self.inspect('<h1>Temporary error</h1>')["state"], "unknown")

    def test_wrong_query_or_unrecognised_candidate_prevents_completion(self):
        self.assertEqual(self.inspect('<div id="rso"><a href="https://example.com"><h3>Unknown layout</h3></a></div>')["state"], "unknown")
        self.assertEqual(self.inspect('<div id="topstuff">No results found for Other</div>', 'https://www.google.com/search?q=Other')["state"], "unknown")

    def test_local_browser_end_to_end_without_manual_completion(self):
        self.html = '<input name="q" value="Example"><div id="rso"><div class="g"><a href="https://example.com"><h3>History</h3></a><div class="VwiC3b">Listing history</div></div></div>'
        with tempfile.TemporaryDirectory() as folder:
            db = Database(Path(folder) / "research.sqlite3")
            try:
                company = db.add_company("Example")
                run = db.plan(company, [("name", "Example")], "google-manual", depth=1)[0]
                outcome = Collector(db, self.page, Path(folder) / "captures", log=lambda _: None).run()
                self.assertEqual(outcome["status"], "finished")
                self.assertEqual(db.require("runs", run)["status"], "complete")
                self.assertEqual(db.result_records()[0]["snippet"], "Listing history")
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()
