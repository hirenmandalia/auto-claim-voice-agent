#!/bin/bash
# Release gate for the Auto Claim Voice Agent. Run from the project folder before every release.
#   ./run_evals.sh            rules + pipeline + live (live needs the local server on :4177)
#   ./run_evals.sh quick      rules only (no network, seconds)
#   ./run_evals.sh full       everything above plus the twelve end-to-end persona scenarios (~20 min)
#   ./run_evals.sh --judge    also score tone with the LLM judge on the live layer
cd "$(dirname "$0")"
set -o pipefail   # a failing runner must fail the gate even though its output is piped through grep
PY=.venv/bin/python
status=0
echo "== 1/4 rules (no network)"; $PY -m unittest tests.test_demo_rules tests.test_regressions tests.test_live_turns tests.test_avatar 2>&1 | tail -3 || status=1
[ "$1" = "quick" ] && exit $status
echo; echo "== 2/4 notes pipeline (Gemini Flash)"; $PY evals/run_pipeline_evals.py 2>&1 | grep -vE "AFC|not recommended|HTTP Request|Sending out|Response received" || status=1
echo; echo "== 3/4 live conversation (Gemini Live via local server)"
if curl -s -m 3 http://127.0.0.1:4177/api/health >/dev/null; then
  $PY evals/run_live_evals.py $( [ "$1" = "--judge" ] && echo --judge ) 2>&1 | grep -vE "AFC|not recommended|HTTP Request|Sending out|Response received" || status=1
else
  echo "SKIPPED: no server on 127.0.0.1:4177 (start the insurance-claim-agent preview or uvicorn first)"; status=1
fi
if [ "$1" = "full" ]; then
  echo; echo "== 4/4 end-to-end scenarios (simulated claimant, ~20 min)"
  if curl -s -m 3 http://127.0.0.1:4177/api/health >/dev/null; then
    $PY evals/run_scenario_evals.py 2>&1 | grep -vE "AFC|not recommended|HTTP Request|Sending out|Response received" || status=1
  else
    echo "SKIPPED: no server on 127.0.0.1:4177"; status=1
  fi
fi
echo; echo "== report"; $PY evals/render_report.py --history 2>/dev/null | tail -2
[ $status = 0 ] && echo "ALL GREEN" || echo "FAILURES — do not release"; exit $status
