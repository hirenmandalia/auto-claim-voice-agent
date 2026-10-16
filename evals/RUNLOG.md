# Run log

One entry per evaluation run that mattered, newest first: what was run, what it found, what we changed, and the commit that
carried the change. The index page (`evals/reports/index.html`, rebuilt by `evals/render_report.py`) renders this file next
to the per-run reports, so the numbers and the story sit together.

Format: `## <date> · <run> · <result>` then **Findings** / **Fixes** / **Commit** lines.

## 2026-10-16 · scenarios, third run · in progress
**Run:** `evals/run_scenario_evals.py`, all twelve personas, after the second run's fixes.
**Findings:** pending.

## 2026-10-16 · scenarios, second run · 4/12 passed · extraction 78%
**Run:** `evals/run_scenario_evals.py` (report `scenario-20261016-105144.json`), after the first run's fixes.
**Findings:** glass, hit-and-run, single-vehicle and the declined home claim now pass (sub-type checklist and decline
route confirmed). New causes behind the rest: the agent never gives up on an unclear camera view (ten or more "get
closer, more light" requests in a row, which also blew the turn budget and produced one 24 s reply); an off-topic
decline ("I can only help with auto claim questions here") was mistaken for a declined claim and routed the curb-scrape
call out of scope; the closing fired while the claimant was still showing the camera, so the agent asked for a better
view after the closing line; one injured-caller run stalled after the second answer with no reply from the agent (the
guard caught the model narrating its own plan); the volunteers-everything limit of three questions was too tight once
the agent correctly asks to see the offered photos and card.
**Fixes:** camera patience capped at two adjustment requests (`CAMERA_RETRY` + move-on prompt, prompt rule);
`SCOPE_DECLINE` only counts a decline that names a non-auto policy; closing prompt waits while the camera is on and no
camera requests after the close; a dropped silence placeholder still owes a reply when the claimant just spoke; the
simulator now gives up on the camera after two requests like a real caller; volunteers limit raised to five.
**Commit:** pending.

## 2026-10-16 · scenarios, first run · 3/12 passed · extraction 87%
**Run:** `evals/run_scenario_evals.py` (report `scenario-20261016-102655.json`). First time the twelve persona scenarios ran.
**Findings:** deer, glass and hail claims were asked about police reports or another driver (one-size-fits-all checklist);
a declined home claim stayed routed as "policy review" because no facts were collected; the "volunteers everything" caller
mentioned photos and the other driver's card and the agent closed without asking to see them; the injured caller was asked
for names of injured people and treatment locations; the off-topic caller's call stalled on "finishing my notes" with the
date unresolved; four scenarios looped on the camera because the simulated caller had nothing to show; blank and
`{pause}` transcription tokens leaked into transcripts.
**Fixes:** sub-type aware checklist (`policies.auto_subtype`, `required_docs_for`); injury checklist item removed;
`offered_but_not_yet_collected` blocks closing until the agent asks to see offered items; `SCOPE_DECLINE` sets the route
out of scope; finishing-notes cue requires all core facts; wider silence-placeholder guard; simulator now shows a drawn
damage panel or insurance card when it offers the camera.
**Commit:** pending.

## 2026-10-16 · phone run, side impact with neck pain · escalated but never closed
**Findings:** escalation fired correctly, but the one-shot closing prompt produced the safety line instead of the closing,
so the agent answered "are you done?" with "finishing up my notes" twice. In the follow-up eval the agent also asked for
receipts and estimates.
**Fixes:** escalation-aware closing (urgent human review + medical care + standard closing); sticky `closed`; off-checklist
guard; injury follow-ups hidden from the agent's open items; one medical reminder rule; scenario `injury_escalates_and_closes`.
**Commit:** 0fbb99b.

## 2026-10-15 · phone run, tree on the Sienna · invented status before the close
**Findings:** before the notes confirmed the date and address, the agent said "your claim packet is now being submitted
for a human review" and "we will get in touch", and suggested discussing repair estimates later.
**Fixes:** `PREMATURE_CLOSE` guard active until closing is allowed; "are we done?" rule; live eval probes with
"Are we done here?" before the closing.
**Commit:** 0fbb99b.

## 2026-10-15 · phone run · "no speech" spoken aloud; other driver's details deferred to the app
**Findings:** the agent said "No speech" when it had nothing to say after a prompt; told the caller they could provide the
other driver's details in the app instead of collecting them.
**Fixes:** `SILENCE_PLACEHOLDER` guard (no steer-back); collect-what-they-have rule; `camera_request` to the phone when the
agent asks to see something; scenario `collects_other_driver_on_camera`.
**Commit:** 6dfa2bb.

## 2026-10-15 · live evals, first full gate · 4/6 then 6/6
**Run:** `./run_evals.sh` (reports `live-20261015-154457.json` → `live-20261015-160759.json`).
**Findings:** `state.closed` was never pushed after the closing line (Review & submit stayed hidden); a stray CJK glyph in a
transcript; the gate script reported ALL GREEN despite failures (grep in the pipe hid the exit code).
**Fixes:** state pushed on the closing line; stray-glyph stripping with a three-character drift threshold; `set -o pipefail`.
**Commit:** 7062fca.

## 2026-10-15 · phone runs through the day · turn-taking, language, lighting, latency
**Findings (in order):** re-asked questions a moment later (tool results made the model speak twice); re-asked over the
caller's answer and said "wait patiently for the model"; Spanish and Devanagari drift; closed then asked for a repair
estimate; never closed because "other driver's insurance details" stayed as a missing fact; asked for the policy number;
"Escalated to a person" on a fender-bender; dark photo not flagged; 20–30 s silence before each reply.
**Fixes:** silent packet sync + mute nudge; silence timed from end of playback with mic-activity cancel; no prompts
mid-response; meta-talk guard; language pinned on speech and both transcripts; closing gate on core facts; already-known
list; present-only escalation; gain-aware darkness with a vision usability check; thinking disabled on extraction
(~15 s → ~6 s); notes never block the reply (<1 s first words).
**Commit:** 7062fca.
