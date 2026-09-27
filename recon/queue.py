"""Persistent pacing for manually controlled browser searches."""

import math
import webbrowser
from pathlib import Path
from datetime import datetime, timedelta, timezone

from .providers import PROVIDERS

# Planned runs not yet searched; searched runs awaiting link resolution are excluded.
UNSEARCHED = ("status='planned' AND id NOT IN (SELECT run_id FROM pending_captures) AND provider IN (" +
              ",".join(f"'{name}'" for name in PROVIDERS) + ")")

# Each newly reported CAPTCHA adds this much to the minimum request gap.
CAPTCHA_BACKOFF_SECONDS = 5


class SearchQueue:
    def __init__(self, db, clock=None, opener=None):
        self.db = db
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.opener = opener or webbrowser.open

    def _state(self):
        return self.db.require("search_queue", 1)

    def _event(self, event, state, notes="", run_id=None):
        self.db.insert("queue_events", {
            "run_id": run_id if run_id is not None else state["active_run_id"],
            "event": event, "occurred_at": self.clock().isoformat(),
            "interval_seconds": state["interval_seconds"], "notes": notes,
        })

    def _candidate(self, state):
        if state["active_run_id"] is not None:
            return self.db.require("runs", state["active_run_id"])
        rows = self.db.rows(f"SELECT * FROM runs WHERE {UNSEARCHED} ORDER BY id LIMIT 1")
        return rows[0] if rows else None

    def _waiting(self, state):
        if not state["next_allowed_at"]:
            return 0
        return max(0, math.ceil((datetime.fromisoformat(state["next_allowed_at"]) - self.clock()).total_seconds()))

    def _collector_running(self):
        """True while a collect or resolve process holds this database's browser lock."""
        try:
            import fcntl
        except ImportError:
            return False
        database = self.db.conn.execute("PRAGMA database_list").fetchone()[2]
        lock = Path(database + ".collector.lock")
        if not database or not lock.exists():
            return False
        with lock.open("a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(handle, fcntl.LOCK_UN)
        return False

    def status(self):
        state = self._state()
        run = self._candidate(state)
        active = state["active_run_id"] and not state["paused"]
        running = self._collector_running() if active else False
        remaining = self.db.rows(f"SELECT COUNT(*) AS count FROM runs WHERE {UNSEARCHED}")[0]["count"]
        links = self.db.rows("SELECT COUNT(*) AS count FROM pending_captures")[0]["count"]
        return {**state, "wait_seconds": self._waiting(state), "remaining_runs": remaining,
                "runs_awaiting_links": links,
                "next_run": run, "action": "paused" if state["paused"] else
                "finished" if run is None else ("in_progress" if running else "interrupted") if state["active_run_id"] else
                "cooldown" if self._waiting(state) else "ready",
                **({"note": f"Run {state['active_run_id']} was started but not finished; "
                            "the next collect redoes it from its first page."} if active and not running else {})}

    def pause(self, captcha=False, notes=""):
        with self.db.conn:
            self.db.conn.execute("BEGIN IMMEDIATE")
            state = self._state()
            reason = "captcha" if captcha else "manual"
            # Repeated reports of the same unresolved challenge must not compound backoff.
            if captcha and state["pause_reason"] != "captcha":
                state["interval_seconds"] = min(86400, state["interval_seconds"] + CAPTCHA_BACKOFF_SECONDS)
                until = self.clock() + timedelta(seconds=state["interval_seconds"])
                if state["next_allowed_at"]:
                    until = max(until, datetime.fromisoformat(state["next_allowed_at"]))
                self.db.conn.execute("UPDATE search_queue SET interval_seconds=?,next_allowed_at=? WHERE id=1",
                                     (state["interval_seconds"], until.isoformat()))
            if state["pause_reason"] == "captcha":
                reason = "captcha"
            self.db.conn.execute("UPDATE search_queue SET paused=1,pause_reason=? WHERE id=1", (reason,))
            self._event("captcha" if captcha else "paused", state, notes)
        return self.status()

    def resume(self):
        with self.db.conn:
            self.db.conn.execute("BEGIN IMMEDIATE")
            state = self._state()
            if state["paused"]:
                self.db.conn.execute("UPDATE search_queue SET paused=0,pause_reason='' WHERE id=1")
                self._event("resumed", state, "User resumed after " + state["pause_reason"])
        return self.status()

    def configure(self, interval):
        if not 1 <= interval <= 86400:
            raise ValueError("interval must be between 1 and 86400 seconds")
        with self.db.conn:
            self.db.conn.execute("BEGIN IMMEDIATE")
            state = self._state()
            until = state["next_allowed_at"]
            if state["last_opened_at"]:
                target = datetime.fromisoformat(state["last_opened_at"]) + timedelta(seconds=interval)
                if until:
                    target = max(target, datetime.fromisoformat(until))
                until = target.isoformat()
            self.db.conn.execute("UPDATE search_queue SET interval_seconds=?,next_allowed_at=? WHERE id=1", (interval, until))
            state["interval_seconds"] = interval
            self._event("configured", state)
        return self.status()

    def next(self, open_browser=False, reopen=False, run_id=None, navigation_url=None):
        if reopen and not open_browser:
            raise ValueError("--reopen requires --open")
        # Reserve and commit before touching the browser. A crash cannot lose our place
        # or let another CLI process issue the same search automatically.
        with self.db.conn:
            self.db.conn.execute("BEGIN IMMEDIATE")
            state = self._state()
            run = self.db.require("runs", run_id) if run_id is not None else self._candidate(state)
            if run is None:
                return {**self.status(), "browser_opened": False}
            provider = PROVIDERS.get(run["provider"])
            if provider is None:
                raise ValueError(f"Search provider is not installed: {run['provider']}")
            url = navigation_url or provider.search_url(run["query"], run["country"], run["language"])
            result = {"run_id": run["id"], "query": run["query"], "url": url, "browser_opened": False}
            if not open_browser:
                return {**self.status(), **result}
            if state["paused"]:
                return {**result, "action": "paused", "pause_reason": state["pause_reason"]}
            if run["status"] == "complete":
                raise ValueError("This run is complete; plan a new run to repeat the search")
            if state["active_run_id"] is not None:
                if state["active_run_id"] != run["id"]:
                    raise ValueError(f"Complete active run {state['active_run_id']} before opening another")
                if not reopen:
                    return {**result, "action": "in_progress", "notes": "Continue the current browser tab; use --reopen only if it needs reopening."}
            waiting = self._waiting(state)
            if waiting:
                return {**result, "action": "cooldown", "wait_seconds": waiting, "next_allowed_at": state["next_allowed_at"]}
            opened_at = self.clock()
            self.db.conn.execute("""UPDATE search_queue SET active_run_id=?,last_opened_at=?,next_allowed_at=? WHERE id=1""",
                                 (run["id"], opened_at.isoformat(), (opened_at + timedelta(seconds=state["interval_seconds"])).isoformat()))
            self._event("open_requested", state, run_id=run["id"])
        try:
            opened = bool(self.opener(url))
        except Exception as error:
            with self.db.conn:
                self._event("browser_failed", state, str(error), run_id=run["id"])
            raise ValueError("Browser launch failed; progress is saved. Retry with next --open --reopen after the cooldown") from error
        with self.db.conn:
            self._event("browser_opened" if opened else "browser_failed", state, run_id=run["id"])
        return {**result, "browser_opened": opened, "action": "opened" if opened else "browser_failed",
                "notes": "Capture results, then mark this run complete." if opened else
                "Progress is saved. Retry with next --open --reopen after the cooldown."}
