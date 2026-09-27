CREATE TABLE IF NOT EXISTS companies (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE CHECK(length(trim(name)) > 0),
    notes TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    company_id INTEGER NOT NULL REFERENCES companies(id),
    query TEXT NOT NULL,
    formulation TEXT NOT NULL,
    provider TEXT NOT NULL,
    country TEXT NOT NULL,
    language TEXT NOT NULL,
    depth INTEGER NOT NULL CHECK(depth > 0),
    created_at TEXT NOT NULL,
    searched_at TEXT,
    status TEXT NOT NULL DEFAULT 'planned' CHECK(status IN ('planned','complete')),
    notes TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS results (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    rank INTEGER NOT NULL CHECK(rank > 0),
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    domain TEXT NOT NULL,
    snippet TEXT NOT NULL DEFAULT '',
    page_date TEXT NOT NULL DEFAULT '',
    page_type TEXT NOT NULL DEFAULT 'unknown',
    useful INTEGER CHECK(useful IN (0,1)),
    notes TEXT NOT NULL DEFAULT '',
    captured_at TEXT NOT NULL,
    UNIQUE(run_id, rank)
);
CREATE TABLE IF NOT EXISTS result_types (
    result_id INTEGER NOT NULL REFERENCES results(id),
    information_type TEXT NOT NULL,
    PRIMARY KEY(result_id, information_type)
);
CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY,
    company_id INTEGER NOT NULL REFERENCES companies(id),
    information_type TEXT NOT NULL,
    subject TEXT NOT NULL,
    predicate TEXT NOT NULL,
    value TEXT NOT NULL,
    event_date TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','verified','disputed')),
    notes TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS evidence (
    id INTEGER PRIMARY KEY,
    fact_id INTEGER NOT NULL REFERENCES facts(id),
    url TEXT NOT NULL,
    domain TEXT NOT NULL,
    result_id INTEGER REFERENCES results(id),
    quotation TEXT NOT NULL DEFAULT '',
    locator TEXT NOT NULL DEFAULT '',
    accessed_at TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS results_domain ON results(domain);
CREATE INDEX IF NOT EXISTS runs_company ON runs(company_id);
CREATE INDEX IF NOT EXISTS facts_company ON facts(company_id);
CREATE INDEX IF NOT EXISTS evidence_fact ON evidence(fact_id);

CREATE TABLE IF NOT EXISTS search_queue (
    id INTEGER PRIMARY KEY CHECK(id = 1),
    active_run_id INTEGER REFERENCES runs(id),
    interval_seconds INTEGER NOT NULL DEFAULT 60 CHECK(interval_seconds BETWEEN 1 AND 86400),
    paused INTEGER NOT NULL DEFAULT 0 CHECK(paused IN (0,1)),
    pause_reason TEXT NOT NULL DEFAULT '',
    last_opened_at TEXT,
    next_allowed_at TEXT
);
INSERT OR IGNORE INTO search_queue(id) VALUES (1);
CREATE TABLE IF NOT EXISTS queue_events (
    id INTEGER PRIMARY KEY,
    run_id INTEGER REFERENCES runs(id),
    event TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    interval_seconds INTEGER NOT NULL,
    notes TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS captures (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL UNIQUE REFERENCES runs(id),
    method TEXT NOT NULL,
    extractor_version TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    completion_reason TEXT NOT NULL,
    result_count INTEGER NOT NULL,
    manifest_path TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ai_overviews (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL UNIQUE REFERENCES runs(id),
    text TEXT NOT NULL,
    extractor_version TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    error TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS ai_overview_sources (
    id INTEGER PRIMARY KEY,
    overview_id INTEGER NOT NULL REFERENCES ai_overviews(id),
    position INTEGER NOT NULL CHECK(position > 0),
    site TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    excerpt TEXT NOT NULL DEFAULT '',
    url TEXT,
    domain TEXT NOT NULL DEFAULT '',
    redirect_url TEXT NOT NULL DEFAULT '',
    UNIQUE(overview_id, position)
);
-- Searches whose result pages are captured but whose Google redirect links are not
-- yet resolved. The run stays planned (outside all metrics) until `resolve` finishes it.
CREATE TABLE IF NOT EXISTS pending_captures (
    run_id INTEGER PRIMARY KEY REFERENCES runs(id),
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS link_cache (
    redirect_url TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    resolved_at TEXT NOT NULL
);
