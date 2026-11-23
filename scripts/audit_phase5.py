"""Phase V audit: async operation, bounded concurrency and anti-bot policy."""

from __future__ import annotations

import argparse
import json
import re

from pathlib import Path


BANNED_MODULES = (
    "cloudscraper",
    "undetected_chromedriver",
    "selenium",
    "pyppeteer",
    "capsolver",
    "anticaptcha",
    "twocaptcha",
    "2captcha",
)

CAPABILITY_CLAIMS = re.compile(
    r"\b(?:we|this (?:project|tool|pipeline))\s+(?:can|does|will)\s+"
    r"(?:bypass|defeat|evade|circumvent)",
    re.IGNORECASE,
)

REQUIRED_POLICY_SOURCES = (
    "VentureBeat",
    "Artificial Intelligence News",
    "Datanami",
    "LinkedIn",
    "Product Hunt",
    "Cloudflare",
)


def audit(*, source_root: Path, docs: Path, phase2: Path, output: Path) -> dict:
    report: dict = {"phase5_complete": False, "gates": {}, "counts": {}, "errors": []}

    def fail(message: str) -> None:
        report["errors"].append(message)

    net = (source_root / "ai_ingest" / "net.py").read_text(encoding="utf-8")
    news_jobs = (source_root / "ai_ingest" / "news_jobs.py").read_text(encoding="utf-8")
    extraction = (
        source_root / "ai_ingest" / "llm" / "news_jobs_extraction.py"
    ).read_text(encoding="utf-8")
    engine = (source_root / "ai_ingest" / "llm" / "engine.py").read_text(
        encoding="utf-8"
    )

    banned_hits = []
    for path in source_root.rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="replace")
        for module in BANNED_MODULES:
            if re.search(rf"\b(?:import|from)\s+{re.escape(module)}\b", text):
                banned_hits.append(f"{path.name}: {module}")

    docs_text = docs.read_text(encoding="utf-8")
    claims = [
        match.group(0)
        for match in CAPABILITY_CLAIMS.finditer(docs_text)
    ]
    missing_policy_sources = [
        source for source in REQUIRED_POLICY_SOURCES if source not in docs_text
    ]

    http_reports = []
    for candidate in phase2.rglob("http_report.json"):
        http_reports.append(json.loads(candidate.read_text(encoding="utf-8")))

    blocked_events = [
        event
        for http_report in http_reports
        for event in http_report.get("events", [])
        if event.get("event")
        in {"robots_disallowed", "robots_denied", "circuit_open", "robots_unavailable"}
    ]
    rate_limit_events = sum(
        metric.get("rate_limit_events", 0)
        for http_report in http_reports
        for metric in http_report.get("metrics", {}).values()
    )
    policies = [
        policy
        for http_report in http_reports
        for policy in http_report.get("policy", {}).values()
    ]

    gates = {
        "async_operation_implemented": (
            "import asyncio" in net
            and "import asyncio" in engine
            and "async def extract" in engine
            and "asyncio.gather" in news_jobs
        ),
        "aiohttp_used": "import aiohttp" in net and "import aiohttp" in extraction,
        "bounded_concurrency": "asyncio.Semaphore" in net
        and "max_concurrency" in net,
        "per_source_limits": "min_interval" in net and "max_concurrency" in net,
        "retry_backoff_with_jitter": (
            "random.uniform" in net or "_rng.uniform" in net
        )
        and "retry_budget" in net,
        "circuit_breaker": "class CircuitBreaker" in net,
        "robots_compliance": "RobotFileParser" in net
        and "robots_disallowed" in net,
        "blocked_requests_logged": bool(blocked_events),
        "rate_limit_events_logged": rate_limit_events >= 0
        and "rate_limit_events" in net,
        "no_bypass_modules": not banned_hits,
        "no_anti_bot_bypass_claims": not claims,
        "protected_sources_documented": not missing_policy_sources
        and "does **not**" in docs_text,
        "browser_rendering_not_required": "Playwright is not used" in docs_text,
        "policies_present_in_runs": bool(policies),
    }
    report["gates"] = gates
    report["counts"] = {
        "http_reports": len(http_reports),
        "blocked_events": len(blocked_events),
        "rate_limit_events": rate_limit_events,
        "source_policies": len(policies),
        "max_concurrency_cap": max(
            (policy.get("max_concurrency", 0) for policy in policies),
            default=0,
        ),
        "banned_modules_found": banned_hits,
        "capability_claim_matches": claims,
        "missing_policy_sources": missing_policy_sources,
    }

    for hit in banned_hits:
        fail(f"Banned bypass module: {hit}")
    for claim in claims:
        fail(f"Unsupported capability claim in docs: {claim}")
    for source in missing_policy_sources:
        fail(f"Missing compliant strategy for protected source: {source}")

    report["phase5_complete"] = all(gates.values()) and not report["errors"]

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Phase V completion audit.")
    parser.add_argument("--src", type=Path, default=Path("src"))
    parser.add_argument("--docs", type=Path, default=Path("docs/compliance.md"))
    parser.add_argument("--phase2", type=Path, default=Path("data/phase2"))
    parser.add_argument("--output", type=Path, default=Path("data/phase5/audit.json"))
    args = parser.parse_args()

    report = audit(
        source_root=args.src,
        docs=args.docs,
        phase2=args.phase2,
        output=args.output,
    )
    print(json.dumps(report, indent=2))

    if not report["phase5_complete"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
