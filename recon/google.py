"""All Google-specific DOM knowledge lives in this adapter."""

from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .providers import PROVIDERS


class RedirectChallenge(Exception):
    """Google answered a result redirect with a CAPTCHA instead of a destination."""

    def __init__(self, message, location=""):
        super().__init__(message)
        self.location = location


class GoogleCollector:
    version = "google-dom-5"
    # Pause between redirect lookups; each is a small request to Google itself.
    redirect_gap_ms = 2000

    def __init__(self):
        self.script = Path(__file__).with_name("google_extract.js").read_text()

    def initial_url(self, run):
        return PROVIDERS["google-manual"].search_url(run["query"], run["country"], run["language"])

    def validate_url(self, url, run):
        parsed = urlsplit(url)
        return (parsed.scheme == "https" and parsed.hostname == "www.google.com" and
                parsed.path == "/search" and parse_qs(parsed.query).get("q") == [run["query"]])

    def same_page(self, current, requested):
        """After a CAPTCHA, Google may return to page 1 instead of the requested page."""
        start = lambda url: parse_qs(urlsplit(url).query).get("start", ["0"])
        return self.validate_url(current, {"query": parse_qs(urlsplit(requested).query).get("q", [""])[0]}) and \
            start(current) == start(requested)

    def validate_redirect(self, url):
        parsed = urlsplit(url or "")
        return parsed.scheme == "https" and parsed.hostname == "www.google.com" and parsed.path in ("/goto", "/url")

    def inspect(self, page, run):
        result = page.evaluate(self.script)
        if result["state"] in ("captcha", "consent"):
            return result
        if not self.validate_url(page.url, run) or result.get("query") != run["query"]:
            return {"state": "unknown", "results": [], "error": "Unexpected search URL or query"}
        if result.get("next_url") and not self.validate_url(result["next_url"], run):
            return {"state": "unknown", "results": [], "error": "Unexpected pagination URL"}
        if any(r.get("redirect") and not self.validate_redirect(r["redirect"]) for r in result["results"]):
            return {"state": "unknown", "results": [], "error": "Unexpected result redirect URL"}
        return result

    def resolve(self, page, records):
        """Read each opaque redirect's Location header without visiting the target site."""
        resolved, looked_up = [], False
        for record in records:
            if not record.get("redirect") or record.get("url"):
                resolved.append(record)
                continue
            if looked_up:
                page.wait_for_timeout(self.redirect_gap_ms)
            looked_up = True
            response = page.context.request.get(record["redirect"], max_redirects=0, timeout=30000)
            location = response.headers.get("location", "")
            target = urlsplit(location)
            if target.hostname and target.hostname.endswith("google.com") and target.path.startswith("/sorry"):
                raise RedirectChallenge("Google showed a CAPTCHA while resolving result links", location)
            if response.status not in (301, 302, 303, 307, 308) or target.scheme not in ("http", "https") or not target.hostname:
                raise ValueError(f"Could not resolve result link (HTTP {response.status}): {record['title']}")
            resolved.append({**record, "url": location,
                             "page_type": "pdf" if target.path.lower().endswith(".pdf") else record.get("page_type", "unknown")})
        return resolved

    def resolve_sources(self, page, sources):
        """Resolve AI Overview source links; an unresolvable link keeps its redirect only."""
        resolved = []
        for source in sources:
            if not source.get("redirect") or not self.validate_redirect(source["redirect"]):
                resolved.append(source)
                continue
            page.wait_for_timeout(self.redirect_gap_ms)
            try:
                resolved.extend(self.resolve(page, [source]))
            except RedirectChallenge:
                raise
            except Exception:
                resolved.append(source)
        return resolved


COLLECTORS = {"google-manual": GoogleCollector}
