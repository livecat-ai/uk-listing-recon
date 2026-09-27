import csv
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from recon.analysis import report
from recon.db import Database, domain_of, read_records
from recon.providers import GoogleManual, QUERY_TEMPLATES


class ResearchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = Database(self.root / "research.sqlite3")
        self.company = self.db.add_company("Example Holdings plc")
        self.run = self.db.plan(self.company, [("name", "Example Holdings plc")], "google-manual", depth=3)[0]

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def result(self, rank=1, **kwargs):
        return {"rank": rank, "title": "Example history", "url": f"https://www.example.com/{rank}", **kwargs}

    def test_import_rolls_back_entire_batch_on_duplicate_rank(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.import_results(self.run, [self.result(), self.result()])
        self.assertEqual(self.db.result_records(), [])

    def test_invalid_tags_and_depth_leave_no_partial_rows(self):
        for bad in (self.result(4), self.result(2, information_types=["invented"]), self.result(1.5)):
            with self.assertRaises(ValueError):
                self.db.import_results(self.run, [self.result(), bad])
            self.assertEqual(self.db.result_records(), [])

    def test_review_can_clear_tags_and_reset_to_unreviewed(self):
        result = self.db.import_results(self.run, [self.result(useful=True, information_types=["isin", "isin"])])[0]
        self.db.review(result, None, [], "Needs another look")
        row = self.db.result_records()[0]
        self.assertIsNone(row["useful"])
        self.assertEqual(row["information_types"], [])
        self.assertEqual(row["notes"], "Needs another look")

    def test_missing_title_rejected_and_empty_export_keeps_columns(self):
        with self.assertRaisesRegex(ValueError, "title"):
            self.db.import_results(self.run, [{"rank": 1, "url": "https://example.com"}])
        self.db.export(self.root / "empty")
        self.db.import_results(self.run, [self.result()])
        self.db.export(self.root / "populated")
        with (self.root / "empty/search_results.csv").open() as empty:
            with (self.root / "populated/search_results.csv").open() as populated:
                self.assertEqual(next(csv.reader(empty)), next(csv.reader(populated)))

    def test_query_denominators_include_empty_runs_not_planned_runs(self):
        self.db.import_results(self.run, [self.result(1), self.result(2, useful=True), self.result(3, useful=False)])
        empty, pending = self.db.plan(self.company, [("name", "Example"), ("name", "Example")], "google-manual", depth=3)
        self.db.import_results(pending, [self.result(useful=True)])
        for run in (self.run, empty):
            self.db.complete(run, "2026-09-26T10:00:00Z")
        row = report(self.db)["query_effectiveness"][0]
        self.assertEqual(row["completed_runs"], 2)
        self.assertEqual(row["planned_runs"], 1)
        self.assertEqual(row["useful_per_reviewed"], 0.5)
        self.assertEqual(row["observed_precision_at_depth"], 1 / 6)
        self.assertEqual(row["observed_success_rate"], 0.5)
        self.assertEqual(row["observed_mrr"], 0.25)

    def test_unreviewed_only_rates_are_null(self):
        self.db.import_results(self.run, [self.result()])
        stats = report(self.db)
        self.assertIsNone(stats["domains_by_frequency"][0]["useful_per_reviewed"])
        self.assertIsNone(stats["query_effectiveness"][0]["observed_mrr"])

    def test_fact_evidence_cannot_cross_company_boundary(self):
        result = self.db.import_results(self.run, [self.result()])[0]
        other = self.db.add_company("Other plc")
        fact = self.db.add_fact(other, "isin", "Other plc", "isin", "EXAMPLE")
        with self.assertRaisesRegex(ValueError, "same company"):
            self.db.add_evidence(fact, result_id=result)

    def test_unique_information_is_based_on_facts_not_just_tags(self):
        result = self.db.import_results(self.run, [self.result(useful=True, information_types=["acquisition"])])[0]
        fact = self.db.add_fact(self.company, "acquisition", "Example", "acquired_by", "Buyer", status="verified")
        self.db.add_evidence(fact, result_id=result)
        self.db.add_evidence(fact, url="https://example.com/second-source")
        self.assertEqual(len(report(self.db)["long_tail_unique_facts"]), 1)
        self.db.add_evidence(fact, url="https://another.example/source")
        self.assertEqual(report(self.db)["long_tail_unique_facts"], [])

    def test_version_3_database_gains_overview_tables_and_keeps_data(self):
        company = self.db.add_company("Upgrade Example")
        self.db.conn.execute("DROP TABLE ai_overview_sources")
        self.db.conn.execute("DROP TABLE ai_overviews")
        self.db.conn.execute("PRAGMA user_version=3")
        self.db.conn.commit()
        path = self.db.conn.execute("PRAGMA database_list").fetchone()[2]
        self.db.close()
        self.db = Database(path)
        self.assertEqual(self.db.require("companies", company)["name"], "Upgrade Example")
        self.assertEqual(self.db.rows("SELECT * FROM ai_overviews"), [])
        self.db.export(self.root / "upgraded")
        self.assertTrue((self.root / "upgraded/ai_overview_sources.csv").is_file())

    def test_export_preserves_evidence_and_csv_special_characters(self):
        notes = 'A comma, a "quote"\nand a new line'
        result = self.db.import_results(self.run, [self.result(notes=notes, information_types=["isin", "sedol"])])[0]
        fact = self.db.add_fact(self.company, "isin", "Example", "isin", "EXAMPLE")
        self.db.add_evidence(fact, result_id=result, quotation=notes, locator="page 2")
        self.db.export(self.root / "export")
        snapshot = json.loads((self.root / "export/snapshot.json").read_text())
        self.assertEqual(snapshot["evidence"][0]["quotation"], notes)
        records = read_records(self.root / "export/search_results.csv")
        other_run = self.db.plan(self.company, [("name", "Example")], "google-manual")[0]
        self.db.import_results(other_run, records)
        self.assertEqual(self.db.result_records()[1]["notes"], notes)
        self.assertEqual(self.db.result_records()[1]["information_types"], ["isin", "sedol"])

    def test_company_import_atomic_and_foreign_keys_enforced(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.import_companies([{"name": "New plc"}, {"name": "Example Holdings plc"}])
        self.assertEqual(len(self.db.rows("SELECT * FROM companies")), 1)
        with self.assertRaises(sqlite3.IntegrityError), self.db.conn:
            self.db.conn.execute("INSERT INTO result_types VALUES (999,'isin')")

    def test_company_header_import_preserves_names(self):
        ids = self.db.import_companies([{"company": "A & B PLC"}, {"name": "C PLC", "notes": "Sample"}])
        self.assertEqual(self.db.require("companies", ids[0])["name"], "A & B PLC")
        self.assertEqual(self.db.require("companies", ids[1])["notes"], "Sample")

    def test_invalid_or_conflicting_company_fields_roll_back(self):
        for bad in ({"company": " "}, {"notes": "No name"}, {"name": "A", "company": "B"}):
            with self.assertRaises(ValueError):
                self.db.import_companies([{"company": "New PLC"}, bad])
            self.assertEqual(len(self.db.rows("SELECT * FROM companies")), 1)

    def test_provider_encodes_literal_query(self):
        query = 'A&B "Holdings" + plc'
        url = GoogleManual().search_url(query, "uk", "en")
        self.assertEqual(parse_qs(urlsplit(url).query)["q"], [query])
        self.assertEqual(len(QUERY_TEMPLATES), 7)
        self.assertEqual(domain_of("https://WWW.Example.co.uk/a"), "example.co.uk")
        self.assertEqual(domain_of("https://news.example.co.uk/a"), "news.example.co.uk")

    def test_search_timestamp_requires_timezone(self):
        with self.assertRaises(ValueError):
            self.db.complete(self.run, "2026-09-26")
        self.assertEqual(self.db.require("runs", self.run)["status"], "planned")

    def test_cli_end_to_end(self):
        database = self.root / "cli.sqlite3"

        def cli(*args, expect=0):
            process = subprocess.run([sys.executable, "-m", "recon", "--db", str(database), *args], capture_output=True, text=True)
            self.assertEqual(process.returncode, expect, process.stderr)
            return json.loads(process.stdout) if expect == 0 else process.stderr

        cli("companies-import", "examples/companies.csv")
        self.assertEqual(len(cli("plan")["run_ids"]), 7)
        self.assertIn("google.com/search?", cli("search", "1")["url"])
        cli("results-import", "1", "examples/results.json")
        cli("review", "1", "--useful", "yes", "--types", "acquisition")
        cli("complete", "1")
        cli("fact-add", "1", "--type", "acquisition", "--subject", "Example Holdings plc", "--predicate", "acquired_by", "--value", "Example Buyer plc")
        cli("evidence-add", "1", "--result", "1")
        cli("fact-status", "1", "verified")
        self.assertEqual(len(cli("history", "1")["facts"][0]["evidence"]), 1)
        self.assertEqual(cli("report")["totals"]["useful"], 1)
        cli("export", str(self.root / "cli-export"))
        self.assertTrue((self.root / "cli-export/report.json").is_file())
        cli("results-import", "1", "examples/results.json", expect=2)


if __name__ == "__main__":
    unittest.main()
