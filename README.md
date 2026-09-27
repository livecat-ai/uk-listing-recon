# UK Listing Recon

A Python research notebook in command-line form for discovering which web sources
and query formulations help reconstruct the history of historical UK-listed companies.
Stores observations, human judgements, and evidence-backed benchmark facts in SQLite.

Python 3.10+. The database and manual commands have no runtime dependencies;
automatic collection uses Playwright and a visible Firefox browser by default.

## Automatic Collection

The supplied sample is already imported and its 700 searches are planned. From the
repository, start collecting with the installed virtual environment:

```sh
.venv/bin/python -m recon collect
```

This opens a separate Firefox research profile, runs pending searches one at a time,
captures titles, URLs, snippets and any recognised displayed dates, and marks each
search complete automatically. A URL already captured earlier in the same search (Google
sometimes repeats a result on the next page) is skipped and the next result used;
skipped repeats are listed in the capture manifest. Fragments (`#...`) are ignored
when comparing URLs. Repeats across different searches are kept. You do not need to import results or mark searches
complete. For a smaller session, use `.venv/bin/python -m recon collect --limit 10`.

When a CAPTCHA appears, collection pauses, beeps, and prompts in the terminal.
Solve it in the browser and press Enter in the terminal. The collector checks the
page before continuing and adds 5 seconds to the request gap. Consent screens also
pause for your choice. The app never solves challenges or chooses consent for you.

Ctrl+C stops with completed searches saved. Run the same command again to continue.
An interrupted search is collected again from its first page; completed searches
are not repeated. If you quit at a CAPTCHA prompt, or the collector stopped on a
page it could not interpret, inspect the problem, run
`.venv/bin/python -m recon queue resume`, then restart collection.

The default minimum gap is 60 seconds between page requests, including pagination.
The collector waits automatically. Use `queue config --interval SECONDS` to change
it. Navigation failures retry up to three times with doubled spacing. Only one
collector may run against a database at a time. The collector currently supports
macOS/Linux. It uses Playwright's bundled Firefox, installed locally in
`data/browser-engines/`; no Firefox account is needed. Installed Chrome or Edge
remain available using `--channel chrome` or `--channel msedge`. Browser profiles
are kept separate per browser. For bundled Chromium, install it with
`PLAYWRIGHT_BROWSERS_PATH="$PWD/data/browser-engines" .venv/bin/python -m playwright install chromium`
and use `--channel chromium`.

Search completion means the requested result depth was captured, or the page
explicitly indicated that results were exhausted. Unknown layouts and errors are
not treated as zero results. Google markup can change; the extractor recognises
common organic-result layouts and stops for inspection when it cannot confirm a
capture. Validate the first small batch before relying on its rankings for research.

Google may link results through opaque `/goto?url=<token>` redirects that hide the
destination. Resolving one means requesting that Google redirect without following
it and recording the `Location` header as the URL; the target site is never visited.

By default `collect` does not resolve links. It saves each search's result pages and
redirect links (two Google requests per search instead of about seventeen) and marks
the search as awaiting links. It keeps a later page's extra results as spares, so
repeats found once links are resolved can be replaced. Then resolve them separately,
at a steady pace, whenever collection is not running:

```sh
.venv/bin/python -m recon resolve --interval 5
```

`resolve` looks up each search's links in order until it has the requested depth of
distinct pages, resolves the AI Overview sources, then records the search exactly as
immediate collection would. Each looked-up link is cached, so the same link is never
requested twice and an interrupted pass loses nothing. A CAPTCHA opens Google's check
in the research browser and waits for you. A link that cannot be resolved (for
example, one that has expired) leaves its search awaiting links and is reported;
other searches continue. Links tested three hours after collection still resolved;
resolve on the same day to be safe. `queue status` shows `runs_awaiting_links`. Until
resolved, a search is excluded from results and metrics, and is not searched again.
`collect` and `resolve` share the research browser, so run one at a time. A search
that ends short of its depth because of repeats is recorded as `short_after_duplicates`.
Use `collect --resolve-now` to resolve links during collection instead (two seconds apart).

When Google shows an AI Overview, the collector also saves it, separately from the
ranked results: `ai_overviews` holds the full text (including the part behind
"Show more", which is already in the page) with inline citation labels such as
`[Companies House +1]`, and `ai_overview_sources` holds each listed source's site,
title, excerpt and resolved URL. Source links are resolved like result links; one
that cannot be resolved keeps only its Google redirect and never stops collection.
If the overview area is still empty, the collector waits up to ten seconds, then
saves it with an explanatory `error`. Searches captured with extractor `google-dom-4`
or later that have no `ai_overviews` row showed no overview. Overviews are
machine-generated summaries: treat them as leads and verify against their sources.

Usefulness and information-type judgements stay unreviewed until assessed. The
collector gathers search observations; it does not verify corporate-history claims.
PDF page type is inferred from the URL; other page types remain unknown. Dates and
snippets absent from recognised markup remain blank, not invented.

Each page has an HTML snapshot and extracted JSON under `data/captures/`, with a
per-search manifest and HTML hashes. Results, capture metadata, and completion are
committed together in SQLite. Firefox state is kept in `data/recon-firefox-profile/`,
separate from your usual browser profile. Keep the captures with the database when
archiving a benchmark; exports reference snapshots but do not embed their HTML.
The database still accepts the existing `google-manual` provider name; `captures`
records distinguish browser collection and its extractor version.

On another checkout, set up browser support first:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[browser]'
PLAYWRIGHT_BROWSERS_PATH="$PWD/data/browser-engines" .venv/bin/python -m playwright install firefox
```

## Brave Search API

Brave runs an independent web index, so it tests how complete the Google-derived
source list is. It is queried through its official API: no browser, CAPTCHA or link
lookups, one request per search in most cases. Brave has no AI Overview equivalent here.

Store the API key in the macOS keychain (you are prompted for it; it is not echoed,
saved to a file, or kept in shell history):

```sh
security add-generic-password -a "$USER" -s uk-listing-recon-brave -w
```

Alternatively set `BRAVE_SEARCH_API_KEY` in the environment. Plan the same seven
queries against Brave, then collect (Brave searches never enter the browser queue):

```sh
.venv/bin/python -m recon plan --provider brave-api
.venv/bin/python -m recon collect-brave --limit 5
```

Requests are spaced 1.1 seconds apart, and rate-limit responses are retried. The
raw JSON response is saved under `data/captures/` with a hash; the key never is.
Results are stored like Google's, with provider `brave-api` and capture method `api`,
so reports compare providers separately. Page dates are Brave's displayed age text.

## Manual Commands

These remain available for inspection, hand-entered results, and corrections:

```sh
python3 -m recon --help
python3 -m recon init
```

Optionally install with `python3 -m pip install -e .` to use the `recon` command.
The default database is `data/recon.sqlite3`, relative to the working directory.
Use `python3 -m recon --db /absolute/path/research.sqlite3 ...` to select another.

## First Research Session

Import your sample as CSV (`name,notes`) or a JSON array of objects. The `company`
header is also accepted in place of `name`; notes are optional. For the supplied
100-share sample, run `python3 -m recon companies-import research_sample.csv` on a
fresh database, then `python3 -m recon plan` to prepare its 700 searches.

The following walkthrough uses fictional example data in a fresh database:

```sh
python3 -m recon companies-import examples/companies.csv
python3 -m recon companies
python3 -m recon plan --company 1 --depth 10
python3 -m recon runs --company 1
python3 -m recon search 1 --open
```

The examples are explicitly fictional. Replace them with your research sample.
Omit `--company` when planning to create seven runs per company across the sample.
Running `plan` again creates independent repeat observations. Default formulations:

- Company name alone
- Company name + `stock market history`
- Company name + `delisted`
- Company name + `merger acquisition`
- Company name + `name change`
- Company name + `SEDOL ISIN`
- `what happened to` + company name

Custom queries need a stable formulation label, used to aggregate across companies:

```sh
python3 -m recon plan --company 1 --query '"Example Holdings" annual report' --formulation annual-report
```

The manual `search` command opens a Google URL in your normal browser only when
`--open` is supplied. `collect` uses the automatic workflow above. Navigation lives
in `recon/providers.py`; Google extraction is isolated in `google.py` and
`google_extract.js`. Persistence, imports, reviews, and analysis use neutral records.
Add another provider and collector adapter to support another search service.

## Manual Queue Controls

The queue saves its position and pacing in SQLite. Open the next pending search:

```sh
python3 -m recon queue status
python3 -m recon next --open
```

Capture its results with `results-import`, then use `complete RUN_ID` when capture
is finished. The next `next --open` selects the following planned run. Merely opening
a search never marks it complete. Without `--open`, `next` previews the current or
next search without launching anything.

The default minimum gap between browser launches is 60 seconds. If it is too soon,
the command returns `cooldown` with `wait_seconds`; it does not sleep or launch a
search later in the background. Repeat the command when ready. Configure the gap:

```sh
python3 -m recon queue config --interval 90
```

If Google displays a CAPTCHA, report it to the queue:

```sh
python3 -m recon queue pause --captcha
# Clear the CAPTCHA yourself in the existing browser tab, then:
python3 -m recon queue resume
```

Reporting a CAPTCHA pauses browser launches and adds 5 seconds to the minimum gap
(60 -> 65 -> 70 seconds), capped at one day. Reporting the same
unresolved challenge again does not increase it twice. The paused state survives a
restart. Resume keeps the increased spacing and any remaining cooldown. It does
not reopen the current search: continue capturing from the existing tab, then mark
that run complete. These are configurable pacing defaults, not guaranteed safe rates.

For an ordinary break, use `queue pause` without `--captcha`, then `queue resume`;
this leaves the interval unchanged. Lowering the configured interval affects future
launches but does not shorten an existing cooldown.

After closing the terminal or restarting the machine, run `queue status` and
`next` to find your place. If the current browser tab was lost, explicitly reopen it:

```sh
python3 -m recon next --open --reopen
```

The queue records a launch reservation before calling the browser, so a crash cannot
silently skip the run or launch it twice on restart. An unsuccessful browser launch
also keeps that reservation and requires explicit reopening after the cooldown.
Only one run can be active per database, and direct `search RUN_ID --open` uses the
same pacing. Completed runs require a new plan to repeat their search.

In manual mode, CAPTCHA detection and clearance are manual. The `collect` command
detects common CAPTCHA pages and asks you to clear them.
Queue events record launch requests, browser handoffs, pauses, resumes, interval
changes, and completion. A browser handoff is not proof that results loaded. All
events and queue state are included in exports. The queue controls requests made
through this app, not searches you enter separately in the browser.

## Capture And Review

Record organic web results in order, excluding ads and answer panels. Keep the
chosen policy consistent across runs and note deviations in the run's `--notes`.
Record every result in the chosen depth, including irrelevant results. Copy fields
into a CSV using `examples/results-template.csv` or a JSON array like the example:

```sh
python3 -m recon results-import 1 examples/results.json
python3 -m recon results --unreviewed
python3 -m recon review 1 --useful yes --types acquisition listing-delisting --page-type announcement --notes 'Confirms buyer and cancellation date'
python3 -m recon complete 1 --searched-at 2026-09-26T15:30:00+01:00
```

IDs in these examples assume a fresh database; commands return the actual IDs as JSON.
The required import fields are `rank`, `title`, and `url`. Optional fields are
`snippet`, `page_date`, `page_type`, `useful`, `information_types`, and `notes`.
CSV types are separated with semicolons; JSON uses an array. Usefulness accepts
true/false, yes/no, or 1/0; blank/null means unreviewed. Dates on pages are preserved
as observed text, allowing partial or uncertain dates; explain ambiguity in notes.
Page type is free text, e.g. `announcement`, `news`, `annual-report`, `pdf`, `registry`.
Title and snippet are preserved separately from your notes.

Company, query, provider, locale, requested depth, and search timestamp belong to the
run. `captured_at` records entry time. `complete` records the actual search time
(defaults to now) and includes empty runs in query statistics. A planned run is
excluded from query-effectiveness metrics.

Ranks are positive, unique within a run, and cannot exceed its depth. Imports are
atomic: one invalid row rolls back the batch. Duplicate imports fail instead of
silently replacing observations. Repeated URLs at distinct ranks or across runs are
retained. To revise usefulness use `review`; `--types` with no values clears tags,
and `--useful unknown` resets the judgement. Other corrections can be made directly
in SQLite; retain a backup and explain material corrections in notes.

Country (`uk`) and language (`en`) default on each plan. These URL hints do not
eliminate localisation, personalisation, or changes over time. Record browser/login
conditions, search date, and unusual result layouts in run notes for comparisons.

## Structured History

Each benchmark fact is a subject/predicate/value statement with an information type,
optional event date, review status, and notes. Values are strings, preserving leading
zeros in identifiers. Use ISO dates (`YYYY-MM-DD`, `YYYY-MM`, `YYYY`) when known;
leave unknown dates blank and describe uncertainty in notes.

```sh
python3 -m recon types
python3 -m recon fact-add 1 --type acquisition --subject 'Example Holdings plc' --predicate acquired_by --value 'Example Buyer plc' --date 2001-06-15
python3 -m recon evidence-add 1 --result 1 --quotation 'Example supporting text' --locator 'Announcement, paragraph 2'
python3 -m recon fact-status 1 verified
python3 -m recon history 1
```

Use `evidence-add FACT_ID --url URL` for evidence discovered outside the recorded
searches. A fact may have several supporting URLs. Each evidence row keeps an access
timestamp, quotation, locator, and notes; linking a result from another company's
run is rejected. Facts start as `draft`, and may become `verified` or `disputed`.
Verification is the researcher's judgement, not an automated claim. Reports flag
verified facts without evidence. Use verified, supported facts as the benchmark;
keep draft and disputed claims for further investigation.

Information types cover identity, names, listing/delisting, TIDM, SEDOL, ISIN,
acquisition, merger, demerger/spinoff, take-private, administration/liquidation,
successor/predecessor, and other corporate events. Predicates distinguish directions
and subtypes, e.g. `previous_name`, `new_name`, `listed_on`, `delisted_from`,
`succeeded_by`, or `entered_administration`. Include listing venue, security class,
effective date, legal entity identity, and identifier validity periods when relevant;
do not assume a company, a security, and a listing are the same thing.

## Analysis And Exports

```sh
python3 -m recon report --long-tail-max 2
python3 -m recon export exports/session-01
```

Reports contain domain frequency, useful-result counts, source usefulness by type,
query effectiveness, and rare sources supporting unique verified facts. Definitions
are included in report JSON. Key interpretation rules:

- Domain counts count appearances, alongside distinct URLs and companies. Hostnames
  are lowercased and stripped of leading `www.`; other subdomains remain distinct.
- Usefulness rate is useful / reviewed. Unreviewed is separate; no denominator is
  `null`, not zero. Information-type rates apply to tagged results only.
- Query groups share provider, formulation, country, language, and depth. They show
  completed run counts, company counts, success rate, useful results per run,
  observed precision at the requested depth, and mean reciprocal rank of the first
  known useful result. Empty completed runs count; planned runs do not.
- Precision and MRR reflect known useful results. Unreviewed or uncaptured positions
  contribute no known usefulness. Complete capture and review before comparing.
  Repeated runs weight the aggregate; use balanced company samples across queries.
- A long-tail source has at most the specified number of captured appearances and
  is the sole recorded evidence domain for a verified fact. Sources found outside
  searches have zero appearances and are labelled accordingly. Unique means unique
  within your evidence collection, not a claim that no other web source has it.

Export writes all thirteen relational tables as CSV, a flat `search_results.csv`, a full
`snapshot.json` with schema version and foreign keys, `report.json`, and report CSVs.
No-result report CSVs are empty. Exports overwrite files with the same names; use a
new directory per frozen benchmark. JSON snapshots retain the complete linked
dataset; snapshot restoration is not yet a CLI operation. CSV imports accept company
lists and result rows. SQLite and JSON remain inspectable with standard tools.

## Development

```sh
python3 -m unittest discover -s tests -v
# Optional browser integration tests, using local simulated pages only:
RECON_BROWSER_TESTS=1 .venv/bin/python -m unittest discover -s tests -v
```

Modules: `db.py` owns persistence and neutral records; `providers.py` search
navigation; `queue.py` persistent pacing; `collector.py` automated capture;
`google.py` / `google_extract.js` Google extraction and redirect resolution; `resolver.py` deferred link resolution; `analysis.py` metric definitions;
`cli.py` research commands. `schema.sql` is the version-5 schema. Opening an older
database adds new tables in a transaction, preserving its existing research.
Local data and exports are ignored by Git.
No web UI, source-page crawling, or independent search engine is included.
