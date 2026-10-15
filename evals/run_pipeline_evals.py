"""Notes-pipeline evals: run each transcript fixture through the real ADK claim graph and check the
facts, route and closing decision. Needs GOOGLE_API_KEY (reads .env). ~10 s per case.

Run:  .venv/bin/python evals/run_pipeline_evals.py [--only name]
Exit code 1 if any case fails. Writes evals/reports/pipeline-<timestamp>.json."""
import argparse, asyncio, json, logging, pathlib, re, sys, time
from datetime import datetime

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "live_demo"), str(ROOT)]
logging.getLogger().setLevel(logging.ERROR)
import server  # noqa: E402  (loads .env)
from agent import run_claim_workflow  # noqa: E402
from live_tools import summarize_workflow_for_voice  # noqa: E402

FIXTURES = ROOT / "evals" / "fixtures" / "transcripts"
REPORTS = ROOT / "evals" / "reports"


def check(expect, wf, summary):
    claim = wf["normalized_claim"]; route = wf["fraud_safety_gate"]["final_routing_decision"]
    failures = []
    def need(cond, msg):
        if not cond: failures.append(msg)
    if "route" in expect: need(route == expect["route"], f"route={route} expected {expect['route']}")
    if "route_not" in expect: need(route != expect["route_not"], f"route={route} must not be {expect['route_not']}")
    if "closing_allowed" in expect: need(summary["closing_allowed"] == expect["closing_allowed"], f"closing_allowed={summary['closing_allowed']} core_open={summary['core_facts_still_needed']} docs_unasked={summary['documents_not_yet_asked_about']}")
    if "core_missing" in expect: need(summary["core_facts_still_needed"] == expect["core_missing"], f"core_facts_still_needed={summary['core_facts_still_needed']}")
    if "contact_method_contains" in expect: need(expect["contact_method_contains"] in str(claim.get("contact_method", "")).lower(), f"contact_method={claim.get('contact_method')!r}")
    if "date_of_loss" in expect: need(str(claim.get("date_of_loss", "")).startswith(expect["date_of_loss"]), f"date_of_loss={claim.get('date_of_loss')!r}")
    if "policy_number" in expect: need(re.sub(r"[^A-Z0-9]", "", str(claim.get("policy_number", "")).upper()) == re.sub(r"[^A-Z0-9]", "", expect["policy_number"]), f"policy_number={claim.get('policy_number')!r}")
    if "policyholder_name_contains" in expect: need(expect["policyholder_name_contains"].lower() in str(claim.get("policyholder_name", "")).lower(), f"policyholder_name={claim.get('policyholder_name')!r}")
    return failures


async def run(only=None):
    results = []
    for path in sorted(FIXTURES.glob("*.json")):
        if only and only not in path.stem: continue
        case = json.loads(path.read_text())
        t0 = time.monotonic()
        try:
            wf = await run_claim_workflow(case["text"], session_id=f"eval-{path.stem}", received_evidence=[])
            summary = summarize_workflow_for_voice(wf, case["text"])
            failures = check(case["expect"], wf, summary)
            detail = {"route": wf["fraud_safety_gate"]["final_routing_decision"], "missing": wf["field_validation"]["missing_fields"], "closing_allowed": summary["closing_allowed"]}
        except Exception as exc:  # noqa: BLE001
            failures, detail = [f"exception: {type(exc).__name__}: {str(exc)[:200]}"], {}
        results.append({"case": path.stem, "why": case["why"], "seconds": round(time.monotonic() - t0, 1), "pass": not failures, "failures": failures, **detail})
        print(f"{'PASS' if not failures else 'FAIL'}  {path.stem:34s} {results[-1]['seconds']:5.1f}s  {'; '.join(failures) or detail}")
    REPORTS.mkdir(exist_ok=True)
    out = REPORTS / f"pipeline-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    out.write_text(json.dumps(results, indent=2))
    passed = sum(r["pass"] for r in results)
    print(f"\n{passed}/{len(results)} passed  (report: {out.relative_to(ROOT)})")
    return passed == len(results)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--only")
    sys.exit(0 if asyncio.run(run(ap.parse_args().only)) else 1)
