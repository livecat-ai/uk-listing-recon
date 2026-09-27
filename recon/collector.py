"""Browser-independent collection workflow with atomic per-search checkpoints."""

import hashlib
import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urldefrag
from uuid import uuid4

from .db import now
from .google import COLLECTORS, RedirectChallenge
from .queue import SearchQueue


def page_key(url):
    """Two result URLs are the same page if they differ only by #fragment."""
    return urldefrag(url)[0]


class CollectionPaused(Exception):
    pass


class Collector:
    def __init__(self, db, page, captures, prompt=input, log=None, queue=None, adapters=None, defer_links=False):
        self.db = db
        self.page = page
        self.captures = Path(captures).resolve()
        self.prompt = prompt
        self.log = log or (lambda message: print(message, file=sys.stderr, flush=True))
        self.queue = queue or SearchQueue(db, opener=self.navigate)
        self.queue.opener = self.navigate
        self.adapters = adapters or COLLECTORS
        # Deferred: save Google redirect links and resolve them later with `resolve`.
        self.defer_links = defer_links

    def navigate(self, url):
        response = self.page.goto(url, wait_until="domcontentloaded", timeout=45000)
        if response and response.status >= 500:
            raise ValueError(f"Search server returned HTTP {response.status}")
        return True

    def wait_and_open(self, run, url):
        for attempt in range(3):
            while True:
                state = self.queue.status()
                if state["paused"]:
                    raise CollectionPaused("Queue is paused. Use queue resume before restarting collection.")
                if state["wait_seconds"]:
                    self.log(f"Run {run['id']}: waiting {state['wait_seconds']} seconds before requesting a page.")
                    while self.queue.status()["wait_seconds"] and not self.queue.status()["paused"]:
                        self.page.wait_for_timeout(1000)
                    continue
                break
            try:
                outcome = self.queue.next(True, True, run["id"], navigation_url=url)
                if outcome["action"] == "opened":
                    return
                if outcome["action"] in ("paused", "in_progress"):
                    raise CollectionPaused(f"Queue requires attention: {outcome['action']}")
            except ValueError as error:
                self.log(f"Run {run['id']}: navigation failed ({error}); attempt {attempt + 1}/3.")
                self.queue.configure(min(86400, self.queue.status()["interval_seconds"] * 2))
        self.queue.pause(notes="Navigation failed after three attempts")
        raise CollectionPaused("Navigation failed. Progress is saved; check the browser and connection before resuming.")

    def read_page(self, adapter, run):
        previous = None
        for _ in range(40):
            try:
                current = adapter.inspect(self.page, run)
            except Exception:
                # Human consent/challenge actions may replace the execution context.
                self.page.wait_for_timeout(500)
                continue
            if current["state"] in ("captcha", "consent"):
                return current
            if current["state"] in ("results", "empty") and current == previous:
                return current
            previous = current
            self.page.wait_for_timeout(500)
        return previous or {"state": "unknown", "results": []}

    def save_page(self, folder, page_number, observation):
        token = f"page-{page_number:03d}-{uuid4().hex[:8]}"
        html = self.page.content()
        html_path = folder / f"{token}.html"
        html_path.write_text(html, encoding="utf-8")
        metadata = {"url": self.page.url, "saved_at": now(), "html": str(html_path),
                    "sha256": hashlib.sha256(html.encode("utf-8")).hexdigest(),
                    "extraction": observation}
        (folder / f"{token}.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
        return metadata

    def human_help(self, observation, adapter, run, folder, page_number):
        while observation["state"] not in ("results", "empty"):
            kind = observation["state"]
            self.save_page(folder, page_number, observation)
            self.queue.pause(captcha=kind == "captcha", notes=f"Collector needs help: {kind}")
            self.log(f"\aRun {run['id']} needs attention: {kind}. No further searches will be sent.")
            try:
                answer = self.prompt("Handle the CAPTCHA/consent or inspect the page in the browser. Press Enter to recheck, or q to stop: ")
            except EOFError:
                raise CollectionPaused("User input is needed; run collect in an interactive terminal.") from None
            if answer.strip().lower() in ("q", "quit", "stop"):
                raise CollectionPaused("Stopped with progress saved. Use queue resume, then collect to continue.")
            observation = self.read_page(adapter, run)
        self.queue.resume()
        return observation

    def resolve_links(self, adapter, run, folder, page_number, observation, records):
        # Only records that will be kept are resolved, to limit extra requests.
        if not hasattr(adapter, "resolve"):
            return records
        while True:
            try:
                return adapter.resolve(self.page, records)
            except RedirectChallenge as error:
                self.save_page(folder, page_number, {**observation, "state": "captcha", "error": str(error)})
                self.queue.pause(captcha=True, notes=str(error))
                self.log(f"\aRun {run['id']} needs attention: {error}. No further searches will be sent.")
                try:
                    answer = self.prompt("Solve the CAPTCHA in the browser (reload the page if needed). Press Enter to retry, or q to stop: ")
                except EOFError:
                    raise CollectionPaused("User input is needed; run collect in an interactive terminal.") from None
                if answer.strip().lower() in ("q", "quit", "stop"):
                    raise CollectionPaused("Stopped with progress saved. Use queue resume, then collect to continue.")
                self.queue.resume()
            except Exception as error:
                self.save_page(folder, page_number, {**observation, "state": "unknown", "error": str(error)})
                self.queue.pause(notes=str(error))
                raise CollectionPaused(f"Could not resolve result links ({error}). Snapshot saved; this run remains pending.") from error

    def find_overview(self, adapter, run, overview):
        # Overviews often arrive after the results; look again before deciding there is none.
        for attempt in range(11):
            if overview:
                return overview
            if attempt:
                self.page.wait_for_timeout(500)
            try:
                overview = adapter.inspect(self.page, run).get("ai_overview")
            except Exception:
                continue
        return overview

    def capture_overview(self, adapter, run, folder, page_number, observation):
        # The overview is recorded alongside results; failing to resolve its sources
        # never blocks the search. Only a CAPTCHA needs the researcher.
        overview = observation["ai_overview"]
        # The overview can still be generating after the results are stable, and it can
        # pause mid-stream; wait for Google's completion marker rather than for stability.
        for _ in range(60):
            if overview.get("error") or (overview.get("text") and overview.get("complete", True)):
                break
            self.page.wait_for_timeout(500)
            try:
                overview = adapter.inspect(self.page, run).get("ai_overview") or overview
            except Exception:
                continue
        if not overview.get("text") and not overview.get("error"):
            overview = {**overview, "error": "Overview area shown but empty (not generated or still loading)"}
        elif not overview.get("complete", True) and not overview.get("error"):
            overview = {**overview, "error": "Overview may be incomplete: still generating after 30 seconds"}
        if self.defer_links or not hasattr(adapter, "resolve_sources"):
            return overview
        while True:
            try:
                return {**overview, "sources": adapter.resolve_sources(self.page, overview.get("sources", []))}
            except RedirectChallenge as error:
                self.save_page(folder, page_number, {**observation, "state": "captcha", "error": str(error)})
                self.queue.pause(captcha=True, notes=str(error))
                self.log(f"\aRun {run['id']} needs attention: {error}. No further searches will be sent.")
                try:
                    answer = self.prompt("Solve the CAPTCHA in the browser (reload the page if needed). Press Enter to retry, or q to stop: ")
                except EOFError:
                    raise CollectionPaused("User input is needed; run collect in an interactive terminal.") from None
                if answer.strip().lower() in ("q", "quit", "stop"):
                    raise CollectionPaused("Stopped with progress saved. Use queue resume, then collect to continue.")
                self.queue.resume()

    def collect_run(self, run):
        if self.db.rows("SELECT id FROM results WHERE run_id=?", (run["id"],)):
            raise CollectionPaused(f"Run {run['id']} already has manual results. Finish it manually before collecting automatically.")
        factory = self.adapters.get(run["provider"])
        if factory is None:
            raise ValueError(f"No browser collector for provider {run['provider']}")
        adapter = factory()
        folder = self.captures / f"run-{run['id']}" / uuid4().hex
        folder.mkdir(parents=True)
        records, pages, visited, seen, duplicates = [], [], set(), set(), []
        url = adapter.initial_url(run)
        searched_at = None
        reason = None
        overview = None
        for page_number in range(1, 21):
            if url in visited:
                raise CollectionPaused("Pagination repeated a page; inspect the saved snapshots before resuming.")
            visited.add(url)
            self.wait_and_open(run, url)
            if searched_at is None:
                searched_at = now()
            observation = self.read_page(adapter, run)
            for _ in range(3):
                if observation["state"] in ("results", "empty"):
                    break
                observation = self.human_help(observation, adapter, run, folder, page_number)
                if not hasattr(adapter, "same_page") or adapter.same_page(self.page.url, url):
                    break
                # Solving a CAPTCHA can land on a different page; request the intended one again.
                self.log(f"Run {run['id']}: returned to a different page after the check; reloading page {page_number}.")
                self.wait_and_open(run, url)
                observation = self.read_page(adapter, run)
            # Completing a search requires either the requested depth or affirmative
            # evidence of exhaustion; a missing selector alone is never an empty result.
            if observation["state"] == "empty" and records:
                raise CollectionPaused("A later page unexpectedly reports no matches; leaving this run pending.")
            if (len(records) + len(observation["results"]) < run["depth"] and
                    not observation.get("next_url") and not observation.get("exhausted")):
                observation = {**observation, "state": "unknown", "error": "Cannot confirm end of results or find the next page"}
                self.save_page(folder, page_number, observation)
                self.queue.pause(notes=observation["error"])
                raise CollectionPaused("Could not confirm the end of results. Snapshot saved; this run remains pending.")
            # A later page can repeat an earlier result; each URL is ranked once per run.
            kept, skipped, pending = [], [], observation["results"]
            while pending and len(records) + len(kept) < run["depth"]:
                need = run["depth"] - len(records) - len(kept)
                if self.defer_links:
                    # Keep the whole page: spares replace repeats found once links are resolved.
                    batch, pending = pending, []
                else:
                    batch, pending = pending[:need], pending[need:]
                    batch = self.resolve_links(adapter, run, folder, page_number, observation, batch)
                for record in batch:
                    key = page_key(record.get("url") or record["redirect"])
                    if key in seen:
                        skipped.append(record)
                    else:
                        seen.add(key)
                        kept.append(record)
            observation = {**observation, "results": kept, "skipped_duplicates": skipped}
            if page_number == 1:
                observation = {**observation, "ai_overview": self.find_overview(adapter, run, observation.get("ai_overview"))}
            if observation.get("ai_overview") and overview is None:
                overview = self.capture_overview(adapter, run, folder, page_number, observation)
                observation = {**observation, "ai_overview": overview}
            duplicates.extend({**record, "page": page_number} for record in skipped)
            pages.append(self.save_page(folder, page_number, observation))
            for record in kept:
                records.append({**record, "rank": len(records) + 1})
            if len(records) >= run["depth"]:
                reason = "requested_depth"
                break
            if not observation.get("next_url"):
                if not observation.get("exhausted"):
                    self.queue.pause(notes="Duplicates left the run short with no next page")
                    raise CollectionPaused("Could not confirm the end of results. Snapshot saved; this run remains pending.")
                reason = "no_results" if not records else "results_exhausted"
                break
            url = observation["next_url"]
        if reason is None:
            raise CollectionPaused("Reached the 20-page limit without completing capture; leaving this run pending.")
        manifest = folder / "manifest.json"
        if self.defer_links:
            payload = {"searched_at": searched_at, "reason": reason, "manifest": str(manifest),
                       "extractor_version": adapter.version, "candidates": records, "ai_overview": overview}
            manifest.write_text(json.dumps({"run_id": run["id"], "query": run["query"], "links": "pending",
                "pages": pages, "skipped_duplicates": duplicates, **payload}, indent=2) + "\n", encoding="utf-8")
            self.db.save_pending(run["id"], payload)
            self.log(f"Run {run['id']} searched: {len(records)} results, links saved for resolve. "
                     f"{self.queue.status()['remaining_runs']} searches remain.")
            return
        manifest.write_text(json.dumps({"run_id": run["id"], "query": run["query"],
            "extractor_version": adapter.version, "searched_at": searched_at,
            "completion_reason": reason, "pages": pages, "results": records,
            "skipped_duplicates": duplicates, "ai_overview": overview}, indent=2) + "\n", encoding="utf-8")
        self.db.save_capture(run["id"], records, searched_at, reason, manifest, adapter.version, overview)
        self.log(f"Run {run['id']} complete: {len(records)} results saved. {self.queue.status()['remaining_runs']} searches remain.")

    def run(self, limit=0):
        completed = 0
        try:
            while not limit or completed < limit:
                state = self.queue.status()
                if state["paused"]:
                    raise CollectionPaused("Queue is paused. Use queue resume, then collect to continue.")
                run = state["next_run"]
                if run is None:
                    break
                self.log(f"Collecting run {run['id']}: {run['query']}")
                self.collect_run(run)
                completed += 1
        except (CollectionPaused, KeyboardInterrupt) as error:
            if isinstance(error, CollectionPaused) and not self.queue.status()["paused"]:
                self.queue.pause(notes=str(error))
            return {"status": "paused", "completed_this_session": completed,
                    "message": str(error) or "Interrupted; restart collect to continue the active search."}
        return {"status": "finished" if not self.queue.status()["remaining_runs"] else "batch_complete",
                "completed_this_session": completed, "remaining_runs": self.queue.status()["remaining_runs"]}


@contextmanager
def collector_lock(database_path):
    # The OS releases this lock even after a crash; stale lock files are harmless.
    import fcntl

    path = Path(str(database_path) + ".collector.lock")
    with path.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("Another collector is already using this database") from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


@contextmanager
def research_page(database_path, channel="firefox"):
    """The visible research browser; one process at a time may use it."""
    try:
        from playwright.sync_api import sync_playwright, Error
    except ImportError:
        raise ValueError('Install browser support first: python -m pip install -e ".[browser]"') from None
    root = Path(database_path).resolve().parent
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(root / "browser-engines"))
    profile = root / (Path(database_path).stem + f"-{channel}-profile")
    with collector_lock(Path(database_path).resolve()), sync_playwright() as playwright:
        try:
            browser_type = playwright.firefox if channel == "firefox" else playwright.chromium
            options = {} if channel == "firefox" else {"channel": channel}
            with browser_type.launch_persistent_context(
                    str(profile), **options, headless=False, locale="en-GB",
                    viewport={"width": 1280, "height": 900}) as context:
                yield context.pages[0] if context.pages else context.new_page()
        except Error as error:
            raise ValueError(f"Browser stopped; progress is saved and the current item remains pending. {error}") from error


def collect_browser(db, database_path, limit=0, channel="firefox", defer_links=True):
    if limit < 0:
        raise ValueError("limit must be nonnegative; 0 means all pending searches")
    root = Path(database_path).resolve().parent
    with research_page(database_path, channel) as page:
        return Collector(db, page, root / "captures", defer_links=defer_links).run(limit)
