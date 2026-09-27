"""Explicit research actions; all command output is JSON."""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from .analysis import report
from .db import Database, INFORMATION_TYPES, SCHEMA_VERSION, now, read_records, write_csv
from .providers import API_PROVIDERS, PROVIDERS, QUERY_TEMPLATES
from .queue import SearchQueue


def parser():
    root = argparse.ArgumentParser(description="Corporate-history search reconnaissance")
    root.add_argument("--db", default="data/recon.sqlite3", help="SQLite path (default: data/recon.sqlite3)")
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="Initialise the database")
    commands.add_parser("types", help="List supported information types")
    commands.add_parser("companies", help="List companies")
    collect = commands.add_parser("collect", help="Automatically capture pending searches in a visible browser")
    collect.add_argument("--limit", type=int, default=0, help="Maximum searches this session; 0 means all pending")
    collect.add_argument("--channel", choices=("firefox", "chrome", "chromium", "msedge"), default="firefox")
    collect.add_argument("--resolve-now", action="store_true",
                         help="Look up result links during collection instead of leaving them for resolve")
    brave = commands.add_parser("collect-brave", help="Collect planned brave-api searches through the Brave Search API")
    brave.add_argument("--limit", type=int, default=0, help="Maximum searches this session; 0 means all planned")
    resolve = commands.add_parser("resolve", help="Look up saved result links for searched runs, at a steady pace")
    resolve.add_argument("--limit", type=int, default=0, help="Maximum searches to finish; 0 means all awaiting links")
    resolve.add_argument("--interval", type=int, default=5, help="Minimum seconds between link lookups (default 5)")
    resolve.add_argument("--channel", choices=("firefox", "chrome", "chromium", "msedge"), default="firefox")
    add = commands.add_parser("company-add")
    add.add_argument("name")
    add.add_argument("--notes", default="")
    imp = commands.add_parser("companies-import", help="Import CSV or a JSON array with name/company and optional notes")
    imp.add_argument("file")
    plan = commands.add_parser("plan", help="Create search runs for one company or the whole sample")
    plan.add_argument("--company", type=int, help="Company ID; omit for all companies")
    plan.add_argument("--provider", choices=(*PROVIDERS, *API_PROVIDERS), default="google-manual")
    plan.add_argument("--query", help="Custom query; omit for the seven default formulations")
    plan.add_argument("--formulation", help="Stable label used to compare custom queries")
    plan.add_argument("--country", default="uk")
    plan.add_argument("--language", default="en")
    plan.add_argument("--depth", type=int, default=10)
    plan.add_argument("--notes", default="")
    runs = commands.add_parser("runs")
    runs.add_argument("--company", type=int)
    search = commands.add_parser("search", help="Show search URL; optionally open browser")
    search.add_argument("run", type=int)
    search.add_argument("--open", action="store_true")
    search.add_argument("--reopen", action="store_true", help="Explicitly reopen the active run after its cooldown")
    next_search = commands.add_parser("next", help="Show the current/next pending search; optionally open it")
    next_search.add_argument("--open", action="store_true")
    next_search.add_argument("--reopen", action="store_true")
    queue = commands.add_parser("queue", help="Persistent queue status, pacing, and manual pauses")
    queue_commands = queue.add_subparsers(dest="queue_command", required=True)
    queue_commands.add_parser("status")
    pause = queue_commands.add_parser("pause")
    pause.add_argument("--captcha", action="store_true", help="Report a CAPTCHA and add 5 seconds to spacing")
    pause.add_argument("--notes", default="")
    queue_commands.add_parser("resume", help="Resume after a break or after manually clearing a CAPTCHA")
    config = queue_commands.add_parser("config")
    config.add_argument("--interval", type=int, required=True, help="Minimum seconds between browser launches")
    complete = commands.add_parser("complete", help="Mark capture complete, including a zero-result search")
    complete.add_argument("run", type=int)
    complete.add_argument("--searched-at", default=None, help="ISO timestamp with timezone; defaults to now")
    imp = commands.add_parser("results-import", help="Import a provider-neutral CSV or JSON array")
    imp.add_argument("run", type=int)
    imp.add_argument("file")
    results = commands.add_parser("results")
    results.add_argument("--run", type=int)
    results.add_argument("--unreviewed", action="store_true")
    review = commands.add_parser("review", help="Replace usefulness and optionally tags/notes")
    review.add_argument("result", type=int)
    review.add_argument("--useful", choices=("yes", "no", "unknown"), required=True)
    review.add_argument("--types", nargs="*", choices=INFORMATION_TYPES)
    review.add_argument("--notes")
    review.add_argument("--page-type")
    fact = commands.add_parser("fact-add")
    fact.add_argument("company", type=int)
    fact.add_argument("--type", choices=INFORMATION_TYPES, required=True)
    fact.add_argument("--subject", required=True)
    fact.add_argument("--predicate", required=True)
    fact.add_argument("--value", required=True)
    fact.add_argument("--date", default="")
    fact.add_argument("--status", choices=("draft", "verified", "disputed"), default="draft")
    fact.add_argument("--notes", default="")
    status = commands.add_parser("fact-status")
    status.add_argument("fact", type=int)
    status.add_argument("status", choices=("draft", "verified", "disputed"))
    evidence = commands.add_parser("evidence-add")
    evidence.add_argument("fact", type=int)
    source = evidence.add_mutually_exclusive_group(required=True)
    source.add_argument("--result", type=int)
    source.add_argument("--url")
    evidence.add_argument("--quotation", default="")
    evidence.add_argument("--locator", default="", help="Page number, section, or paragraph")
    evidence.add_argument("--notes", default="")
    history = commands.add_parser("history")
    history.add_argument("company", type=int)
    stats = commands.add_parser("report")
    stats.add_argument("--long-tail-max", type=int, default=2)
    export = commands.add_parser("export", help="Write relational CSV, flat results, snapshot JSON and reports")
    export.add_argument("directory")
    return root


def execute(db, args):
    cmd = args.command
    if cmd == "collect":
        from .collector import collect_browser
        return collect_browser(db, args.db, args.limit, args.channel, defer_links=not args.resolve_now)
    if cmd == "collect-brave":
        from .brave import collect_brave
        return collect_brave(db, args.db, args.limit)
    if cmd == "resolve":
        from .resolver import resolve_browser
        return resolve_browser(db, args.db, args.limit, args.interval, args.channel)
    if cmd == "init":
        return {"database": str(Path(args.db).resolve()), "schema_version": SCHEMA_VERSION}
    if cmd == "types":
        return INFORMATION_TYPES
    if cmd == "companies":
        return db.rows("SELECT * FROM companies ORDER BY id")
    if cmd == "company-add":
        return {"company_id": db.add_company(args.name, args.notes)}
    if cmd == "companies-import":
        return {"company_ids": db.import_companies(read_records(args.file))}
    if cmd == "plan":
        if args.depth < 1:
            raise ValueError("depth must be positive")
        if args.query is not None and (not args.formulation or not args.query.strip()):
            raise ValueError("A custom query needs nonempty --query and --formulation")
        if args.formulation and args.query is None:
            raise ValueError("--formulation requires --query")
        companies = [db.require("companies", args.company)] if args.company else db.rows("SELECT * FROM companies ORDER BY id")
        if not companies:
            raise ValueError("Add or import companies first")
        ids = []
        for company in companies:
            queries = [(args.formulation, args.query)] if args.query else [(name, template.format(company=company["name"])) for name, template in QUERY_TEMPLATES.items()]
            ids.extend(db.plan(company["id"], queries, args.provider, args.country, args.language, args.depth, args.notes))
        return {"run_ids": ids}
    if cmd == "runs":
        return db.rows("SELECT * FROM runs" + (" WHERE company_id=?" if args.company else "") + " ORDER BY id", (args.company,) if args.company else ())
    if cmd in ("search", "next"):
        return SearchQueue(db).next(args.open, args.reopen, args.run if cmd == "search" else None)
    if cmd == "queue":
        queue = SearchQueue(db)
        if args.queue_command == "pause":
            return queue.pause(args.captcha, args.notes)
        if args.queue_command == "resume":
            return queue.resume()
        if args.queue_command == "config":
            return queue.configure(args.interval)
        return queue.status()
    if cmd == "complete":
        db.complete(args.run, args.searched_at or now())
        return db.require("runs", args.run)
    if cmd == "results-import":
        return {"result_ids": db.import_results(args.run, read_records(args.file))}
    if cmd == "results":
        return [r for r in db.result_records() if (args.run is None or r["run_id"] == args.run) and (not args.unreviewed or r["useful"] is None)]
    if cmd == "review":
        db.review(args.result, None if args.useful == "unknown" else args.useful, args.types, args.notes, args.page_type)
        return db.require("results", args.result)
    if cmd == "fact-add":
        return {"fact_id": db.add_fact(args.company, args.type, args.subject, args.predicate, args.value, args.date, args.status, args.notes)}
    if cmd == "fact-status":
        db.require("facts", args.fact)
        with db.conn:
            db.conn.execute("UPDATE facts SET status=? WHERE id=?", (args.status, args.fact))
        return db.require("facts", args.fact)
    if cmd == "evidence-add":
        return {"evidence_id": db.add_evidence(args.fact, args.url, args.result, args.quotation, args.locator, args.notes)}
    if cmd == "history":
        return db.history(args.company)
    if cmd == "report":
        if args.long_tail_max < 0:
            raise ValueError("long-tail-max must be nonnegative")
        return report(db, args.long_tail_max)
    if cmd == "export":
        db.export(args.directory)
        stats = report(db)
        directory = Path(args.directory)
        (directory / "report.json").write_text(json.dumps(stats, indent=2) + "\n", encoding="utf-8")
        for name, records in stats.items():
            if isinstance(records, list) and records:
                write_csv(directory / f"report_{name}.csv", records, list(records[0]))
            elif isinstance(records, list):
                # Do not retain stale rows when re-exporting after review changes.
                (directory / f"report_{name}.csv").write_text("", encoding="utf-8")
        return {"directory": str(directory.resolve())}
    raise ValueError(f"Unknown command: {cmd}")


def main(argv=None):
    args = parser().parse_args(argv)
    db = None
    try:
        db = Database(args.db)
        print(json.dumps(execute(db, args), indent=2))
        return 0
    except (ValueError, KeyError, TypeError, OSError, sqlite3.Error) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 2
    finally:
        if db:
            db.close()
