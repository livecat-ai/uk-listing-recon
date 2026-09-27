"""Resolve saved Google redirect links for searches captured with deferred links.

Collection saves each result's opaque redirect instead of looking it up, so a search
costs two page requests. This pass looks the redirects up at a steady, configurable
pace, drops repeated pages, and then records the search exactly as immediate
collection would. Looked-up links are cached, so an interrupted pass loses nothing.
"""

import json
import sys
import time
from pathlib import Path

from .collector import page_key, research_page
from .google import COLLECTORS, RedirectChallenge


class ResolutionStopped(Exception):
    pass


class LinkResolver:
    def __init__(self, db, page, prompt=input, log=None, interval=5, adapters=None, clock=time.monotonic):
        if not 1 <= interval <= 3600:
            raise ValueError("interval must be between 1 and 3600 seconds")
        self.db = db
        self.page = page
        self.prompt = prompt
        self.log = log or (lambda message: print(message, file=sys.stderr, flush=True))
        self.interval = interval
        self.adapters = adapters or COLLECTORS
        self.clock = clock
        self.last_lookup = None
        self.lookups = 0

    def pace(self):
        if self.last_lookup is not None:
            remaining = self.interval - (self.clock() - self.last_lookup)
            if remaining > 0:
                self.page.wait_for_timeout(int(remaining * 1000))
        self.last_lookup = self.clock()

    def lookup(self, adapter, record):
        """Return the destination URL, or raise ValueError if the redirect cannot be resolved."""
        if record.get("url"):
            return record["url"]
        if not record.get("redirect"):
            raise ValueError(f"No link saved for: {record.get('title', '')}")
        cached = self.db.cached_link(record["redirect"])
        if cached:
            return cached
        while True:
            self.pace()
            try:
                url = adapter.resolve(self.page, [{**record, "url": None}])[0]["url"]
            except RedirectChallenge as error:
                self.challenge(error)
                continue
            self.lookups += 1
            self.db.cache_link(record["redirect"], url)
            self.log(f"  {self.lookups} looked up: {url[:100]}")
            return url

    def challenge(self, error):
        # Show Google's check in the research browser so the researcher can clear it.
        if error.location:
            try:
                self.page.goto(error.location, wait_until="domcontentloaded", timeout=45000)
            except Exception:
                pass
        self.log(f"\aGoogle showed a CAPTCHA while resolving links. No further lookups will be sent.")
        try:
            answer = self.prompt("Solve the CAPTCHA in the browser. Press Enter to retry, or q to stop: ")
        except EOFError:
            raise ResolutionStopped("User input is needed; run resolve in an interactive terminal.") from None
        if answer.strip().lower() in ("q", "quit", "stop"):
            raise ResolutionStopped("Stopped with progress saved. Run resolve again to continue.")

    def finish(self, pending):
        run = self.db.require("runs", pending["run_id"])
        payload = pending["payload"]
        adapter = self.adapters[run["provider"]]()
        sources = len((payload.get("ai_overview") or {}).get("sources", []))
        self.log(f"Run {run['id']}: resolving links for {run['query']!r} "
                 f"(up to {min(run['depth'], len(payload['candidates']))} results, {sources} overview sources).")
        results, seen, skipped = [], set(), []
        for candidate in payload["candidates"]:
            if len(results) == run["depth"]:
                break
            url = self.lookup(adapter, candidate)
            if page_key(url) in seen:
                skipped.append({**candidate, "url": url})
                continue
            seen.add(page_key(url))
            page_type = "pdf" if url.split("#")[0].split("?")[0].lower().endswith(".pdf") else candidate.get("page_type", "unknown")
            results.append({**candidate, "url": url, "page_type": page_type, "rank": len(results) + 1})
        reason = payload["reason"]
        if reason == "requested_depth" and len(results) < run["depth"]:
            reason = "short_after_duplicates"
        overview = payload.get("ai_overview")
        if overview:
            sources = []
            for source in overview.get("sources", []):
                try:
                    sources.append({**source, "url": self.lookup(adapter, source)})
                except RedirectChallenge:
                    raise
                except Exception:
                    sources.append(source)
            overview = {**overview, "sources": sources}
        manifest = Path(payload["manifest"])
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            data.update(links="resolved", completion_reason=reason, results=results,
                        skipped_after_resolution=skipped, ai_overview=overview)
            manifest.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        except OSError as error:
            self.log(f"Run {run['id']}: could not update manifest ({error}); saving to the database anyway.")
        self.db.save_capture(run["id"], results, payload["searched_at"], reason, manifest,
                             payload["extractor_version"], overview)
        return len(results)

    def run(self, limit=0):
        finished, failed = 0, []
        try:
            for pending in self.db.pending_captures():
                if limit and finished >= limit:
                    break
                try:
                    count = self.finish(pending)
                except (ResolutionStopped, KeyboardInterrupt):
                    raise
                except Exception as error:
                    # e.g. an expired redirect: keep the search pending and report it.
                    self.db.pending_failed(pending["run_id"], str(error))
                    failed.append({"run_id": pending["run_id"], "error": str(error)})
                    self.log(f"Run {pending['run_id']}: could not resolve links ({error}); left pending.")
                    continue
                finished += 1
                self.log(f"Run {pending['run_id']} resolved: {count} results saved.")
        except (ResolutionStopped, KeyboardInterrupt) as error:
            return {"status": "stopped", "resolved_this_session": finished, "lookups": self.lookups,
                    "failed": failed, "message": str(error) or "Interrupted; run resolve again to continue."}
        remaining = len(self.db.pending_captures())
        return {"status": "finished" if not remaining else "batch_complete", "resolved_this_session": finished,
                "lookups": self.lookups, "failed": failed, "runs_awaiting_links": remaining}


def resolve_browser(db, database_path, limit=0, interval=5, channel="firefox"):
    if limit < 0:
        raise ValueError("limit must be nonnegative; 0 means all searches awaiting links")
    if not db.pending_captures():
        return {"status": "finished", "resolved_this_session": 0, "lookups": 0, "failed": [], "runs_awaiting_links": 0}
    with research_page(database_path, channel) as page:
        return LinkResolver(db, page, interval=interval).run(limit)
