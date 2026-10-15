#!/bin/bash
# Release gate for the Auto Claim Voice Agent. Run from the project folder before every release.
#   ./run_evals.sh            rules + pipeline + live (live needs the local server on :4177)
#   ./run_evals.sh quick      rules only (no network, seconds)
#   ./run_evals.sh --judge    also score tone with the LLM judge
cd "$(dirname "$0")"
set -o pipefail   # a failing runner must fail the gate even though its output is piped through grep
PY=.venv/bin/python
status=0
echo "== 1/3 rules (no network)"; $PY -m unittest tests.test_demo_rules tests.test_regressions tests.test_live_turns tests.test_avatar 2>&1 | tail -3 || status=1
[ "$1" = "quick" ] && exit $status
echo; echo "== 2/3 notes pipeline (Gemini Flash)"; $PY evals/run_pipeline_evals.py 2>&1 | grep -vE "AFC|not recommended|HTTP Request|Sending out|Response received" || status=1
echo; echo "== 3/3 live conversation (Gemini Live via local server)"
if curl -s -m 3 http://127.0.0.1:4177/api/health >/dev/null; then
  $PY evals/run_live_evals.py ${1:+$1} 2>&1 | grep -vE "AFC|not recommended|HTTP Request|Sending out|Response received" || status=1
else
  echo "SKIPPED: no server on 127.0.0.1:4177 (start the insurance-claim-agent preview or uvicorn first)"; status=1
fi
echo; [ $status = 0 ] && echo "ALL GREEN" || echo "FAILURES — do not release"; exit $status
