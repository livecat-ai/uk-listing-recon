"""Descriptive metrics over recorded observations, not search-engine recall."""

from collections import Counter, defaultdict


def rate(numerator, denominator):
    return numerator / denominator if denominator else None


def report(db, long_tail_max=2):
    results = db.result_records()
    by_domain = defaultdict(list)
    by_type = defaultdict(list)
    by_run = defaultdict(list)
    for result in results:
        by_domain[result["domain"]].append(result)
        by_run[result["run_id"]].append(result)
        for information_type in result["information_types"]:
            by_type[(result["domain"], information_type)].append(result)

    def counts(records):
        reviewed = sum(r["useful"] is not None for r in records)
        useful = sum(r["useful"] == 1 for r in records)
        return {"encounters": len(records), "reviewed": reviewed,
                "unreviewed": len(records) - reviewed, "useful": useful,
                "useful_per_reviewed": rate(useful, reviewed)}

    domains = [{"domain": domain, **counts(records),
                "unique_urls": len({r["url"] for r in records}),
                "companies": len({r["company_id"] for r in records})}
               for domain, records in sorted(by_domain.items())]
    source_types = [{"domain": domain, "information_type": kind, **counts(records)}
                    for (domain, kind), records in sorted(by_type.items())]
    runs = db.rows("SELECT * FROM runs ORDER BY id")
    groups = defaultdict(list)
    for run in runs:
        key = tuple(run[k] for k in ("provider", "formulation", "country", "language", "depth"))
        groups[key].append(run)
    queries = []
    for key, group in sorted(groups.items()):
        complete = [r for r in group if r["status"] == "complete"]
        records = [record for run in complete for record in by_run[run["id"]]]
        useful = sum(r["useful"] == 1 for r in records)
        reciprocal = [1 / min((r["rank"] for r in by_run[run["id"]] if r["useful"] == 1), default=float("inf"))
                      for run in complete]
        queries.append(dict(zip(("provider", "formulation", "country", "language", "depth"), key)) | {
            "planned_runs": len(group) - len(complete), "completed_runs": len(complete),
            "companies": len({r["company_id"] for r in complete}), **counts(records),
            "runs_with_useful_results": sum(v > 0 for v in reciprocal),
            "observed_success_rate": rate(sum(v > 0 for v in reciprocal), len(complete)),
            "useful_per_completed_run": rate(useful, len(complete)),
            "observed_precision_at_depth": rate(useful, sum(r["depth"] for r in complete)),
            "observed_mrr": rate(sum(reciprocal), len(complete)),
        })

    facts = db.rows("SELECT * FROM facts ORDER BY id")
    evidence = db.rows("SELECT * FROM evidence ORDER BY id")
    fact_domains = defaultdict(set)
    for source in evidence:
        fact_domains[source["fact_id"]].add(source["domain"])
    unique = []
    for fact in facts:
        domains_for_fact = fact_domains[fact["id"]]
        if fact["status"] == "verified" and len(domains_for_fact) == 1:
            domain = next(iter(domains_for_fact))
            encounters = len(by_domain[domain])
            if encounters <= long_tail_max:
                unique.append({"domain": domain, "encounters": encounters,
                    "found_in_search": encounters > 0, "company_id": fact["company_id"],
                    "fact_id": fact["id"], "information_type": fact["information_type"],
                    "subject": fact["subject"], "predicate": fact["predicate"], "value": fact["value"]})
    return {
        "definitions": {
            "domain": "Lowercase hostname, leading www. removed; other subdomains stay separate.",
            "domain_and_type_scope": "All captured results, including planned runs; each appearance counts.",
            "query_scope": "Completed runs only, grouped by provider, formulation, country, language and depth.",
            "useful_per_reviewed": "Useful / reviewed; unreviewed excluded. Null means no denominator.",
            "observed_precision_at_depth": "Useful / total requested result slots of completed runs. Missing and unreviewed slots contribute no known usefulness.",
            "observed_mrr": "Mean reciprocal rank of first known useful result per completed run; zero if none known.",
            "long_tail": f"At most {long_tail_max} recorded encounters and sole recorded evidence domain for a verified fact. Uniqueness is within this dataset, not the web.",
        },
        "totals": {"companies": len(db.rows("SELECT id FROM companies")),
                   "runs": len(runs), "completed_runs": sum(r["status"] == "complete" for r in runs),
                   **counts(results), "facts": len(facts),
                   "fact_statuses": dict(Counter(f["status"] for f in facts)),
                   "unsupported_verified_facts": sum(f["status"] == "verified" and not fact_domains[f["id"]] for f in facts)},
        "domains_by_frequency": sorted(domains, key=lambda d: (-d["encounters"], d["domain"])),
        "domains_by_useful_results": sorted(domains, key=lambda d: (-d["useful"], d["domain"])),
        "source_usefulness_by_type": source_types,
        "query_effectiveness": queries,
        "long_tail_unique_facts": unique,
    }
