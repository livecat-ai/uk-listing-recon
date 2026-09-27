"""Search navigation only; providers do not own storage or result formats."""

from typing import Protocol
from urllib.parse import urlencode


class SearchProvider(Protocol):
    name: str

    def search_url(self, query: str, country: str, language: str) -> str: ...


class GoogleManual:
    name = "google-manual"

    def search_url(self, query: str, country: str, language: str) -> str:
        return "https://www.google.com/search?" + urlencode(
            {"q": query, "gl": country, "hl": language}
        )


PROVIDERS: dict[str, SearchProvider] = {"google-manual": GoogleManual()}
# Providers queried through an official API; they have their own collect command
# and never enter the browser queue.
API_PROVIDERS = ("brave-api",)

QUERY_TEMPLATES = {
    "name": "{company}",
    "market-history": "{company} stock market history",
    "delisted": "{company} delisted",
    "acquisition": "{company} merger acquisition",
    "name-change": "{company} name change",
    "identifiers": "{company} SEDOL ISIN",
    "what-happened": "what happened to {company}",
}
