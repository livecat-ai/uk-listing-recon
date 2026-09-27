"""Small, explicit persistence API. SQLite remains directly inspectable."""

import csv
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

INFORMATION_TYPES = (
    "company-identity", "company-name", "listing-delisting", "tidm", "sedol",
    "isin", "acquisition", "merger", "demerger-spinoff", "take-private",
    "administration-liquidation", "successor-predecessor", "other-corporate-event",
)
SCHEMA_VERSION = 5
TABLES = ("companies", "runs", "results", "result_types", "facts", "evidence", "search_queue", "queue_events", "captures",
          "ai_overviews", "ai_overview_sources", "pending_captures", "link_cache")
RESULT_COLUMNS = (
    "id", "run_id", "rank", "title", "url", "domain", "snippet", "page_date",
    "page_type", "useful", "notes", "captured_at", "company_id", "company",
    "query", "formulation", "provider", "country", "language", "depth",
    "searched_at", "run_status", "information_types",
)


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def domain_of(url):
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("URL must be an absolute HTTP(S) URL without credentials")
    host = parsed.hostname.encode("idna").decode("ascii").lower()
    if any(c.isspace() for c in host):
        raise ValueError("Invalid URL hostname")
    return host.removeprefix("www.")


def check_type(value):
    if value not in INFORMATION_TYPES:
        raise ValueError(f"Unknown information type: {value}")
    return value


def useful_value(value):
    if value is None or value == "":
        return None
    if isinstance(value, str):
        value = value.lower()
    if value in (True, 1, "true", "yes", "1"):
        return 1
    if value in (False, 0, "false", "no", "0"):
        return 0
    raise ValueError("useful must be true, false, or blank/null")


class Database:
    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1, 2, 3, 4, SCHEMA_VERSION):
            self.conn.close()
            raise ValueError(f"Unsupported database schema version {version}")
        schema = Path(__file__).with_name("schema.sql").read_text()
        self.conn.executescript(f"BEGIN IMMEDIATE;\n{schema}\nPRAGMA user_version={SCHEMA_VERSION};\nCOMMIT;")

    def close(self):
        self.conn.close()

    @contextmanager
    def transaction(self):
        """A savepoint composes with an outer capture transaction without committing it."""
        name = "tx_" + uuid4().hex
        self.conn.execute(f"SAVEPOINT {name}")
        try:
            yield
        except BaseException:
            self.conn.execute(f"ROLLBACK TO {name}")
            self.conn.execute(f"RELEASE {name}")
            raise
        else:
            self.conn.execute(f"RELEASE {name}")

    def rows(self, sql, params=()):
        return [dict(row) for row in self.conn.execute(sql, params)]

    def require(self, table, id_):
        if table not in TABLES:
            raise ValueError("Unknown table")
        rows = self.rows(f"SELECT * FROM {table} WHERE id=?", (id_,))
        if not rows:
            raise ValueError(f"No {table} record with ID {id_}")
        return rows[0]

    def insert(self, table, values):
        # Table and column names come only from this module, never from imports.
        columns = ",".join(values)
        placeholders = ",".join("?" for _ in values)
        return self.conn.execute(
            f"INSERT INTO {table} ({columns}) VALUES ({placeholders})", tuple(values.values())
        ).lastrowid

    def add_company(self, name, notes=""):
        with self.conn:
            return self.insert("companies", {"name": name.strip(), "notes": notes})

    def import_companies(self, records):
        ids = []
        with self.conn:
            for record in records:
                name = record.get("name", record.get("company"))
                if not isinstance(name, str) or not name.strip():
                    raise ValueError("Each company needs a nonempty name or company field")
                if "name" in record and "company" in record:
                    company = record["company"]
                    if not isinstance(company, str) or name.strip() != company.strip():
                        raise ValueError("The name and company fields must agree when both are supplied")
                ids.append(self.insert("companies", {"name": name.strip(), "notes": record.get("notes", "")}))
        return ids

    def plan(self, company_id, queries, provider, country="uk", language="en", depth=10, notes=""):
        self.require("companies", company_id)
        with self.conn:
            return [self.insert("runs", {
                "company_id": company_id, "query": query, "formulation": formulation,
                "provider": provider, "country": country, "language": language,
                "depth": depth, "created_at": now(), "notes": notes,
            }) for formulation, query in queries]

    def complete(self, run_id, searched_at):
        self.require("runs", run_id)
        timestamp = datetime.fromisoformat(searched_at.replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            raise ValueError("searched-at must include a timezone, e.g. 2026-09-26T14:00:00Z")
        with self.transaction():
            self.conn.execute("UPDATE runs SET status='complete', searched_at=? WHERE id=?",
                              (timestamp.astimezone(timezone.utc).isoformat(), run_id))
            queue = self.require("search_queue", 1)
            if queue["active_run_id"] == run_id:
                self.conn.execute("UPDATE search_queue SET active_run_id=NULL WHERE id=1")
                self.insert("queue_events", {"run_id": run_id, "event": "completed",
                    "occurred_at": now(), "interval_seconds": queue["interval_seconds"], "notes": ""})

    def import_results(self, run_id, records):
        run = self.require("runs", run_id)
        ids = []
        with self.transaction():
            for record in records:
                if not isinstance(record.get("title"), str) or not record["title"].strip():
                    raise ValueError("Each result needs a nonempty title")
                if not isinstance(record.get("url"), str):
                    raise ValueError("Each result needs a URL string")
                rank = int(record["rank"])
                if isinstance(record["rank"], bool) or str(rank) != str(record["rank"]).strip():
                    raise ValueError("rank must be an integer")
                if not 1 <= rank <= run["depth"]:
                    raise ValueError(f"rank must be between 1 and run depth {run['depth']}")
                types = record.get("information_types", [])
                if isinstance(types, str):
                    types = [t.strip() for t in types.split(";") if t.strip()]
                types = [check_type(t) for t in types]
                values = {k: record.get(k, "") for k in ("title", "url", "snippet", "page_date", "notes")}
                values.update(run_id=run_id, rank=rank, domain=domain_of(values["url"]),
                              page_type=record.get("page_type") or "unknown",
                              useful=useful_value(record.get("useful")), captured_at=now())
                result_id = self.insert("results", values)
                self.conn.executemany("INSERT INTO result_types VALUES (?,?)", [(result_id, t) for t in set(types)])
                ids.append(result_id)
        return ids

    def save_capture(self, run_id, records, searched_at, reason, manifest_path, extractor_version, overview=None, method="browser"):
        with self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            self.conn.execute("DELETE FROM pending_captures WHERE run_id=?", (run_id,))
            run = self.require("runs", run_id)
            if run["status"] == "complete":
                raise ValueError("Run is already complete")
            if self.rows("SELECT id FROM results WHERE run_id=?", (run_id,)):
                raise ValueError("Run already has manually captured results; finish it manually or create a new run")
            self.import_results(run_id, records)
            self.insert("captures", {"run_id": run_id, "method": method,
                "extractor_version": extractor_version, "captured_at": now(),
                "completion_reason": reason, "result_count": len(records),
                "manifest_path": str(manifest_path)})
            if overview:
                self.save_overview(run_id, overview, extractor_version)
            self.complete(run_id, searched_at)

    def save_pending(self, run_id, payload):
        """Record a searched run whose links still need resolving, and release the queue."""
        with self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            if self.require("runs", run_id)["status"] == "complete":
                raise ValueError("Run is already complete")
            self.insert("pending_captures", {"run_id": run_id, "payload": json.dumps(payload), "created_at": now()})
            queue = self.require("search_queue", 1)
            if queue["active_run_id"] == run_id:
                self.conn.execute("UPDATE search_queue SET active_run_id=NULL WHERE id=1")
            self.insert("queue_events", {"run_id": run_id, "event": "searched", "occurred_at": now(),
                "interval_seconds": queue["interval_seconds"], "notes": "Links pending resolution"})

    def pending_captures(self):
        rows = self.rows("SELECT * FROM pending_captures ORDER BY run_id")
        return [{**row, "payload": json.loads(row["payload"])} for row in rows]

    def pending_failed(self, run_id, error):
        with self.conn:
            self.conn.execute("UPDATE pending_captures SET attempts=attempts+1, last_error=? WHERE run_id=?", (error, run_id))

    def cached_link(self, redirect_url):
        rows = self.rows("SELECT url FROM link_cache WHERE redirect_url=?", (redirect_url,))
        return rows[0]["url"] if rows else None

    def cache_link(self, redirect_url, url):
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO link_cache VALUES (?,?,?)", (redirect_url, url, now()))

    def save_overview(self, run_id, overview, extractor_version):
        overview_id = self.insert("ai_overviews", {"run_id": run_id, "text": overview.get("text", ""),
            "extractor_version": extractor_version, "captured_at": now(), "error": overview.get("error", "")})
        for source in overview.get("sources", []):
            url = source.get("url")
            try:
                domain = domain_of(url) if url else ""
            except ValueError:
                url, domain = None, ""
            self.insert("ai_overview_sources", {"overview_id": overview_id, "position": source["position"],
                "site": source.get("site", ""), "title": source.get("title", ""),
                "excerpt": source.get("excerpt", ""), "url": url, "domain": domain,
                "redirect_url": source.get("redirect") or ""})
        return overview_id

    def review(self, result_id, useful, types=None, notes=None, page_type=None):
        self.require("results", result_id)
        if types is not None:
            types = [check_type(t) for t in types]
        with self.conn:
            self.conn.execute("UPDATE results SET useful=? WHERE id=?", (useful_value(useful), result_id))
            for key, value in (("notes", notes), ("page_type", page_type)):
                if value is not None:
                    self.conn.execute(f"UPDATE results SET {key}=? WHERE id=?", (value, result_id))
            if types is not None:
                self.conn.execute("DELETE FROM result_types WHERE result_id=?", (result_id,))
                self.conn.executemany("INSERT INTO result_types VALUES (?,?)", [(result_id, t) for t in set(types)])

    def add_fact(self, company_id, information_type, subject, predicate, value, event_date="", status="draft", notes=""):
        self.require("companies", company_id)
        check_type(information_type)
        if not all(v.strip() for v in (subject, predicate, value)):
            raise ValueError("subject, predicate, and value cannot be blank")
        with self.conn:
            return self.insert("facts", {"company_id": company_id, "information_type": information_type,
                "subject": subject, "predicate": predicate, "value": value, "event_date": event_date,
                "status": status, "notes": notes})

    def add_evidence(self, fact_id, url=None, result_id=None, quotation="", locator="", notes=""):
        fact = self.require("facts", fact_id)
        if result_id is not None:
            result = self.require("results", result_id)
            run = self.require("runs", result["run_id"])
            if run["company_id"] != fact["company_id"]:
                raise ValueError("Evidence result and fact must belong to the same company")
            if url and url != result["url"]:
                raise ValueError("Evidence URL must match the linked result")
            url = result["url"]
        if not url:
            raise ValueError("Supply a URL or a result ID")
        with self.conn:
            return self.insert("evidence", {"fact_id": fact_id, "url": url, "domain": domain_of(url),
                "result_id": result_id, "quotation": quotation, "locator": locator,
                "accessed_at": now(), "notes": notes})

    def history(self, company_id):
        company = self.require("companies", company_id)
        facts = self.rows("SELECT * FROM facts WHERE company_id=? ORDER BY event_date,id", (company_id,))
        for fact in facts:
            fact["evidence"] = self.rows("SELECT * FROM evidence WHERE fact_id=? ORDER BY id", (fact["id"],))
        return {"company": company, "facts": facts}

    def result_records(self):
        records = self.rows("""SELECT r.*, c.id AS company_id, c.name AS company,
            s.query, s.formulation, s.provider, s.country, s.language, s.depth,
            s.searched_at, s.status AS run_status
            FROM results r JOIN runs s ON s.id=r.run_id JOIN companies c ON c.id=s.company_id
            ORDER BY s.id,r.rank""")
        for record in records:
            record["information_types"] = [r["information_type"] for r in self.rows(
                "SELECT information_type FROM result_types WHERE result_id=? ORDER BY information_type", (record["id"],))]
        return records

    def export(self, directory):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        snapshot = {"schema_version": SCHEMA_VERSION, "exported_at": now()}
        for table in TABLES:
            records = self.rows(f"SELECT * FROM {table} ORDER BY rowid")
            snapshot[table] = records
            columns = [r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")]
            write_csv(directory / f"{table}.csv", records, columns)
        (directory / "snapshot.json").write_text(json.dumps(snapshot, indent=2) + "\n", encoding="utf-8")
        records = self.result_records()
        write_csv(directory / "search_results.csv", records, RESULT_COLUMNS)


def write_csv(path, records, columns):
    with Path(path).open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for record in records:
            writer.writerow({k: ";".join(v) if isinstance(v, list) else v for k, v in record.items()})


def read_records(path):
    path = Path(path)
    with path.open(encoding="utf-8-sig", newline="") as handle:
        if path.suffix.lower() == ".csv":
            return list(csv.DictReader(handle))
        records = json.load(handle)
    if not isinstance(records, list) or not all(isinstance(r, dict) for r in records):
        raise ValueError("Import JSON must be an array of objects")
    return records
