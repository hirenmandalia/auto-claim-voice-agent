# Evaluation framework — Auto Claim Voice Agent

Three layers, cheapest first. Run everything with `./run_evals.sh` from the project folder;
`./run_evals.sh quick` runs only the no-network layer.

| Layer | Command | Needs | Time | Catches |
|---|---|---|---|---|
| 1. Rules | `.venv/bin/python -m unittest tests.test_demo_rules` | nothing | seconds | escalation logic, checklist filtering, contact preference, closing gate, already-known list, language pin, tool scheduling, sketch off, barge-in default, dark-frame metric, meta-talk guard, LAN access, account context |
| 2. Notes pipeline | `.venv/bin/python evals/run_pipeline_evals.py` | GOOGLE_API_KEY | ~1 min | what the real extractor + rules produce for fixture transcripts: route, missing facts, contact/date/policy capture, whether the claim can close |
| 3. Live conversation | `.venv/bin/python evals/run_live_evals.py` | local server on :4177 + key | ~6 min | greeting speaks first by name, reply < 3 s, one reply per turn, no re-asking known facts, call order and closing line, `state.closed` (Review & submit), 10 s gentle re-ask once, claimant voice cancels re-ask, dark camera → asks for light, no technical talk, no language drift |

| 4. End-to-end scenarios | `.venv/bin/python evals/run_scenario_evals.py` | local server on :4177 + key | ~20 min | twelve persona scenarios (two-party collision, hit and run, single vehicle, glass, vandalism, hail, animal strike, injury, non-auto loss, off-topic caller, volunteers everything, other driver's card in hand) played by a Gemini-simulated claimant against the real agent. Deterministic pass/fail on route, closing, checklist coverage, forbidden questions, guards and latency; extraction accuracy vs ground truth and a judge rubric are tracked metrics. `./run_evals.sh full` includes this layer. |

## Where to see the history

- `evals/reports/index.html`: every run, newest first, with pass counts, extraction accuracy, judge score, and the code revision, each linking to its own report page with full transcripts. Rebuild with `.venv/bin/python evals/render_report.py --history` (the gate script does this).
- `evals/RUNLOG.md`: the narrative, one entry per run that mattered: what it found, what we changed, which commit. It is rendered into the index page. Add an entry whenever a run leads to a fix.
- `evals/reports/*.json`: the raw records, tracked in git so the history survives machines and sessions.

Reports land in `evals/reports/` as JSON with every agent line, so a failure can be read without re-running.
`--judge` on the live runner adds a Gemini-scored tone rubric (warmth, conciseness, no repetition, natural flow) per scenario.

## Incident → test map

| Incident seen on the phone | Guarded by |
|---|---|
| Re-asked the same question a moment later (tool result made it speak twice) | rules `test_packet_sync_is_silent_other_tools_continue`; live `latency_single_reply` |
| Re-asked over the claimant's answer; "wait patiently for the model" | live `voice_cancels_reask`, `quiet_reask`; rules `test_meta_talk_guard`; `common_checks` meta/demo scan |
| Switched to Spanish / Devanagari transcript | rules `test_language_pinned_...`; live `common_checks` non-Latin scan |
| Closed before asking for documents, then asked for a repair estimate | live `full_call_closes` (no question after closing); rules `ClosingGate` |
| Never closed, no Review & submit (other-driver details stuck as missing) | pipeline `other_driver_fled_closes`; rules `test_third_party_open_item_does_not_block`; live `full_call_closes` (`state.closed`) |
| "Specific email address" blocker after choosing email | pipeline `contact_preference_only`; rules `test_contact_preference_is_complete` |
| Asked for policy number / date again | rules `test_already_known_list_is_populated`; live `common_checks` (`ASKS_POLICY`), `latency_single_reply` (`ASKS_DATE_AGAIN`); pipeline `policy_never_missing` |
| "Escalated to a person" on a fender-bender | pipeline `injury_unanswered_no_escalation`, `real_injury_escalates`; rules `EscalationRules` |
| Dark photo not flagged | rules `test_dark_frames_detected_bright_frames_pass`; live `dark_camera` |
| 20–30 s silence before replying (tool blocked on notes) | live `latency_single_reply` (first words < 3 s) |
| Sketches appearing | rules `test_sketch_tool_off_by_default` |
| Phone could not connect (LAN) | rules `test_lan_mode_accepts_private_addresses_only_when_enabled` |

## Release checklist

1. `./run_evals.sh` → ALL GREEN. A red live scenario is usually real; re-run `--only <name>` once before deciding, since Gemini Live has some run-to-run variance.
2. Restart the phone server (`./run_phone.sh`) so it runs the release build, and confirm `/api/health` shows the key and three tools.
3. Manual phone pass (cannot be automated):
   - Greeting audible on the phone speaker with ringer on.
   - Talk over the agent mid-sentence once: it stops and answers you (barge-in), and room noise alone does not interrupt it.
   - Ask a question, say nothing for ~10 s: one gentle re-ask, then silence.
   - Show a dark photo: amber "Too dark" overlay and the agent asks for light; show a lit one: overlay clears, photo pinned and tagged.
   - Show two different angles: two pins in the Evidence strip.
   - Finish a claim: the closing line, "Review & submit" visible, tapping it shows the summary screen.
   - Certificate still trusted after any Wi-Fi/IP change (regenerate `certs/` if the Mac's address changed).
4. Skim `evals/reports/live-*.json` agent lines for tone; run `--judge` if the wording changed.

## Adding a scenario

Add a JSON file under `evals/scenarios/` with `persona` (style, whether they volunteer everything, optional off-topic lines), `facts` (what the simulated claimant knows and has), `expect` (route, closes, should_ask / should_not_ask checklist items, camera_request, limits) and `truth` (ground truth for extraction accuracy). The simulator only ever answers from `facts`, so a scenario is reproducible in substance even though the wording varies.

## Adding a case

- New extraction/route behavior → add a JSON under `evals/fixtures/transcripts/` with `text`, `why`, and `expect` keys (see existing files for the supported expectations).
- New conversational behavior → add a `scenario_*` coroutine in `run_live_evals.py` and register it in `SCENARIOS`. Keep each scenario on its own session and assert on `c.agent_turns`, `c.events`, and `c.state`.
- New deterministic rule → a test in `tests/test_demo_rules.py`, named after the incident.
