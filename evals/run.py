"""Eval runner.

Runs every case (the 5 scenarios from the brief plus added cases) through the
real pipeline, checks property assertions, and writes expected-vs-actual
results to evals/results/<label>.md and .json.

Examples
    python -m evals.run --provider mock                 # rules baseline, no API key
    python -m evals.run --provider anthropic --repeat 3 --label v1
    python -m evals.run --only S2,S4 --label v2-rerun   # rerun the affected cases after a fix
    python -m evals.run --base-url https://your-app.onrender.com   # against a deployment
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from evals.assertions import AssertionResult, run_assertion  # noqa: E402

CASES_PATH = ROOT / "evals" / "cases.json"
SCENARIOS_PATH = ROOT / "app" / "data" / "scenarios.json"
TABLE_PATH = ROOT / "app" / "data" / "verified_stock.json"


def load_cases(only: set[str] | None) -> list[dict]:
    scenarios = {s["id"]: s for s in json.loads(SCENARIOS_PATH.read_text(encoding="utf-8"))}
    cases = []
    for case in json.loads(CASES_PATH.read_text(encoding="utf-8")):
        if only and case["scenario"] not in only:
            continue
        if "text" in case:  # eval-only case (e.g. regression probes) defined inline
            cases.append({"simulate_failure": None, **case})
        else:
            cases.append({**scenarios[case["scenario"]], **case})
    return cases


class InProcessTarget:
    def __init__(self, provider: str | None):
        if provider:
            os.environ["LLM_PROVIDER"] = provider
        from app.config import load_settings
        from app.llm import build_llm_client
        from app.pipeline import TriagePipeline

        settings = load_settings()
        self.client = build_llm_client(settings)
        self.pipeline = TriagePipeline(self.client)
        self.description = f"in-process / {self.client.provider} / {self.client.model}"

    def analyze(self, text: str, simulate_failure: str | None) -> dict:
        return self.pipeline.run(text, simulate_failure=simulate_failure).model_dump(mode="json")


class HttpTarget:
    def __init__(self, base_url: str):
        import httpx2

        self.base_url = base_url.rstrip("/")
        self.http = httpx2.Client(timeout=90)
        cfg = self.http.get(f"{self.base_url}/api/config").json()
        self.description = f"{self.base_url} / {cfg['provider']} / {cfg['model']}"

    def analyze(self, text: str, simulate_failure: str | None) -> dict:
        body = {"text": text}
        if simulate_failure:
            body["simulate_failure"] = simulate_failure
        for _ in range(6):
            response = self.http.post(f"{self.base_url}/api/analyze", json=body)
            if response.status_code == 429:
                wait = int(response.headers.get("Retry-After", "10"))
                print(f"    rate limited, waiting {wait}s", flush=True)
                time.sleep(wait)
                continue
            response.raise_for_status()
            return response.json()
        raise RuntimeError("still rate limited after several retries")


def summarize(out: dict) -> str:
    action = out["recommended_next_action"]["type"]
    statuses = sorted({e["status"] for e in out["evidence"] if e["source"] == "field_update"})
    parts = [
        f"category={out['issue_category']}",
        f"urgency={out['urgency']}",
        f"review={str(out['needs_human_review']).lower()}",
        f"action={action}",
        f"evidence={'/'.join(statuses) or 'none'}",
    ]
    if out["meta"]["degraded"]:
        parts.append("degraded=true")
    return ", ".join(parts)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run SalesEasy Copilot evals")
    parser.add_argument("--provider", choices=["mock", "anthropic", "openai_compatible"], help="override LLM_PROVIDER")
    parser.add_argument("--base-url", help="evaluate a running deployment instead of in-process")
    parser.add_argument("--repeat", type=int, default=1, help="runs per case (LLM output varies; 3 is a good default)")
    parser.add_argument("--only", help="comma-separated scenario ids, e.g. S2,S4")
    parser.add_argument("--label", help="results file name (default: <provider>-<timestamp>)")
    parser.add_argument("--out", default=str(ROOT / "evals" / "results"))
    args = parser.parse_args()

    only = {s.strip() for s in args.only.split(",")} if args.only else None
    cases = load_cases(only)
    table = json.loads(TABLE_PATH.read_text(encoding="utf-8"))["rows"]
    target = HttpTarget(args.base_url) if args.base_url else InProcessTarget(args.provider)
    provider_tag = target.description.split(" / ")[1]
    label = args.label or f"{provider_tag}-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"

    print(f"Target: {target.description}")
    print(f"Cases: {len(cases)}  repeats: {args.repeat}\n")

    report = []
    for case in cases:
        runs = []
        for i in range(args.repeat):
            started = time.perf_counter()
            try:
                out = target.analyze(case["text"], case.get("simulate_failure"))
                results = [run_assertion(a, out, case["text"], table) for a in case["assertions"]]
                error = None
            except Exception as exc:  # report, don't crash the whole run
                out, results, error = None, [AssertionResult("request", False, f"{type(exc).__name__}: {exc}")], str(exc)
            elapsed = int((time.perf_counter() - started) * 1000)
            passed = all(r.passed for r in results)
            runs.append({"run": i + 1, "passed": passed, "elapsed_ms": elapsed, "error": error,
                         "assertions": [asdict(r) for r in results], "output": out})
            mark = "PASS" if passed else "FAIL"
            print(f"  {case['scenario']:<3} run {i + 1}: {mark}  ({elapsed} ms)  {summarize(out) if out else error}")
            for r in results:
                if not r.passed:
                    print(f"        x {r.check}: {r.detail}")
        n_pass = sum(r["passed"] for r in runs)
        report.append({**{k: case[k] for k in ("scenario", "group", "title", "text", "expected")},
                       "simulate_failure": case.get("simulate_failure"), "assertions": case["assertions"],
                       "pass_count": n_pass, "runs": runs})

    total_cases = len(report)
    cases_all_pass = sum(1 for c in report if c["pass_count"] == len(c["runs"]))
    total_runs = sum(len(c["runs"]) for c in report)
    runs_pass = sum(c["pass_count"] for c in report)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    meta = {"label": label, "target": target.description, "date_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "repeat": args.repeat, "cases_passed": cases_all_pass, "cases_total": total_cases,
            "runs_passed": runs_pass, "runs_total": total_runs}
    (out_dir / f"{label}.json").write_text(json.dumps({"meta": meta, "cases": report}, indent=2, ensure_ascii=False), encoding="utf-8")
    (out_dir / f"{label}.md").write_text(render_markdown(meta, report), encoding="utf-8")

    print(f"\n{cases_all_pass}/{total_cases} cases passed every run ({runs_pass}/{total_runs} runs).")
    print(f"Wrote {out_dir / (label + '.md')}")
    return 0 if cases_all_pass == total_cases else 1


def _cell(s: str) -> str:
    return s.replace("|", "\\|").replace("\n", " ")


def render_markdown(meta: dict, report: list[dict]) -> str:
    lines = [
        f"# Eval results: {meta['label']}",
        "",
        f"- Target: `{meta['target']}`",
        f"- Date (UTC): {meta['date_utc']}",
        f"- Runs per case: {meta['repeat']}",
        f"- Result: **{meta['cases_passed']}/{meta['cases_total']} cases passed every run** ({meta['runs_passed']}/{meta['runs_total']} runs)",
        "",
        "| Case | Scenario | Expected | Actual (last run) | Pass rate | Result |",
        "|---|---|---|---|---|---|",
    ]
    for c in report:
        last = c["runs"][-1]
        actual = summarize(last["output"]) if last["output"] else f"error: {last['error']}"
        name = c["title"] + (f" (simulated: {c['simulate_failure']})" if c["simulate_failure"] else "")
        result = "PASS" if c["pass_count"] == len(c["runs"]) else "FAIL"
        lines.append(f"| {c['scenario']} | {_cell(name)} | {_cell(c['expected'])} | {_cell(actual)} | {c['pass_count']}/{len(c['runs'])} | {result} |")

    lines += ["", "## Pass/fail criteria per case", ""]
    for c in report:
        lines.append(f"**{c['scenario']}: {c['title']}**  ")
        lines.append(f"Input: `{_cell(c['text'])}`")
        lines.append("")
        last = c["runs"][-1]
        for a in last["assertions"]:
            lines.append(f"- {'PASS' if a['passed'] else 'FAIL'} `{a['check']}`: {a['detail']}")
        lines.append("")

    failures = [(c, r) for c in report for r in c["runs"] if not r["passed"]]
    lines += ["## Failures", ""]
    if not failures:
        lines.append("None.")
    for c, r in failures:
        lines.append(f"### {c['scenario']} run {r['run']}")
        for a in r["assertions"]:
            if not a["passed"]:
                lines.append(f"- `{a['check']}`: {a['detail']}")
        if r["output"]:
            lines.append(f"- review_reason: {r['output'].get('review_reason')}")
            lines.append(f"- action details: {r['output']['recommended_next_action']['details']}")
            lines.append(f"- guardrail notes: {r['output']['meta']['guardrail_notes']}")
        lines.append("")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    raise SystemExit(main())
