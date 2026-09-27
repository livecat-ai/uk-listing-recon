"""Brave Search API collection: an independent web index, queried through its official API.

No browser, CAPTCHA or redirect lookups are involved; results carry their own URLs.
The API key is read from the BRAVE_SEARCH_API_KEY environment variable or, on macOS,
from the login keychain (service "uk-listing-recon-brave"). It is never logged or saved.
"""

import hashlib
import json
import os
import re
import subprocess
import sys
import time
from html import unescape
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import uuid4

from .collector import page_key
from .db import now

ENDPOINT = "https://api.search.brave.com/res/v1/web/search"
KEYCHAIN_SERVICE = "uk-listing-recon-brave"
PROVIDER = "brave-api"
VERSION = "brave-api-1"
# Brave returns at most 20 results per request and pages by offset (0-9).
PAGE_SIZE, MAX_OFFSET = 20, 9
COUNTRIES = {"uk": "GB"}


class BraveStopped(Exception):
    pass


def api_key():
    key = os.environ.get("BRAVE_SEARCH_API_KEY", "").strip()
    if key:
        return key
    try:
        found = subprocess.run(["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
                               capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        found = None
    if found and found.returncode == 0 and found.stdout.strip():
        return found.stdout.strip()
    raise ValueError("No Brave Search API key found. Set BRAVE_SEARCH_API_KEY or add it to the keychain "
                     f"as service '{KEYCHAIN_SERVICE}' (see README).")


def http_get(url, key, timeout=30):
    request = Request(url, headers={"Accept": "application/json", "X-Subscription-Token": key,
                                    "User-Agent": "uk-listing-recon/0.1 (research)"})
    try:
        with urlopen(request, timeout=timeout) as response:
            return response.status, dict(response.headers), response.read()
    except HTTPError as error:
        return error.code, dict(error.headers or {}), error.read()


def plain(text):
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", "", text or ""))).strip()


class BraveCollector:
    def __init__(self, db, captures, key=None, fetch=http_get, sleep=time.sleep, log=None, interval=1.1):
        self.db = db
        self.captures = Path(captures).resolve()
        self.key = key
        self.fetch = fetch
        self.sleep = sleep
        self.log = log or (lambda message: print(message, file=sys.stderr, flush=True))
        self.interval = interval
        self.last_request = None
        self.requests = 0

    def params(self, run, offset):
        return {"q": run["query"], "country": COUNTRIES.get(run["country"], run["country"].upper()),
                "search_lang": run["language"], "count": PAGE_SIZE, "offset": offset,
                "safesearch": "off", "text_decorations": "false"}

    def request(self, run, offset):
        url = ENDPOINT + "?" + urlencode(self.params(run, offset))
        for attempt in range(4):
            if self.last_request is not None:
                wait = self.interval - (time.monotonic() - self.last_request)
                if wait > 0:
                    self.sleep(wait)
            self.last_request = time.monotonic()
            status, headers, body = self.fetch(url, self.key)
            self.requests += 1
            if status == 200:
                return url, body
            if status in (401, 403):
                raise BraveStopped(f"Brave rejected the API key (HTTP {status}). Check the key and plan.")
            if status == 429 or status >= 500:
                delay = float(headers.get("Retry-After") or 2 ** (attempt + 1))
                self.log(f"Run {run['id']}: Brave returned HTTP {status}; retrying in {delay:.0f} seconds.")
                self.sleep(delay)
                continue
            raise BraveStopped(f"Brave returned HTTP {status}: {body[:200]!r}")
        raise BraveStopped("Brave kept refusing requests; progress is saved. Try again later.")

    def collect_run(self, run):
        if self.db.rows("SELECT id FROM results WHERE run_id=?", (run["id"],)):
            raise BraveStopped(f"Run {run['id']} already has results; finish it manually or plan a new run.")
        folder = self.captures / f"run-{run['id']}" / uuid4().hex
        folder.mkdir(parents=True)
        records, seen, pages, skipped = [], set(), [], []
        searched_at, reason = now(), None
        for offset in range(MAX_OFFSET + 1):
            url, body = self.request(run, offset)
            path = folder / f"page-{offset + 1:03d}.json"
            path.write_bytes(body)
            data = json.loads(body)
            pages.append({"url": url, "saved_at": now(), "json": str(path),
                          "sha256": hashlib.sha256(body).hexdigest()})
            web = (data.get("web") or {}).get("results") or []
            for item in web:
                if len(records) == run["depth"]:
                    break
                if not item.get("url") or not item.get("title"):
                    continue
                if page_key(item["url"]) in seen:
                    skipped.append(item["url"])
                    continue
                seen.add(page_key(item["url"]))
                records.append({"rank": len(records) + 1, "title": plain(item["title"]), "url": item["url"],
                                "snippet": plain(item.get("description")), "page_date": item.get("age") or "",
                                "page_type": "pdf" if item["url"].split("?")[0].lower().endswith(".pdf") else "unknown"})
            if len(records) >= run["depth"]:
                reason = "requested_depth"
                break
            if not (data.get("query") or {}).get("more_results_available") or not web:
                reason = "results_exhausted" if records else "no_results"
                break
        if reason is None:
            reason = "results_exhausted"
        manifest = folder / "manifest.json"
        manifest.write_text(json.dumps({"run_id": run["id"], "query": run["query"], "provider": PROVIDER,
            "extractor_version": VERSION, "searched_at": searched_at, "completion_reason": reason,
            "pages": pages, "results": records, "skipped_duplicates": skipped}, indent=2) + "\n", encoding="utf-8")
        self.db.save_capture(run["id"], records, searched_at, reason, manifest, VERSION, method="api")
        return len(records)

    def run(self, limit=0):
        done = 0
        try:
            for run in self.db.rows("SELECT * FROM runs WHERE provider=? AND status='planned' ORDER BY id", (PROVIDER,)):
                if limit and done >= limit:
                    break
                count = self.collect_run(run)
                done += 1
                self.log(f"Run {run['id']} complete: {count} results from Brave ({run['query']}).")
        except (BraveStopped, KeyboardInterrupt) as error:
            return {"status": "stopped", "completed_this_session": done, "requests": self.requests,
                    "message": str(error) or "Interrupted; run collect-brave again to continue."}
        remaining = self.db.rows("SELECT COUNT(*) AS n FROM runs WHERE provider=? AND status='planned'", (PROVIDER,))[0]["n"]
        return {"status": "finished" if not remaining else "batch_complete", "completed_this_session": done,
                "requests": self.requests, "remaining_runs": remaining}


def collect_brave(db, database_path, limit=0):
    if limit < 0:
        raise ValueError("limit must be nonnegative; 0 means all planned Brave searches")
    root = Path(database_path).resolve().parent
    return BraveCollector(db, root / "captures", key=api_key()).run(limit)
