"""Live conversation evals: a scripted claimant drives the REAL server (and Gemini Live) over the
WebSocket, the way the phone does, and checks the behaviors that broke during phone testing.

Prereq: the local server running, e.g. the "insurance-claim-agent" preview or
  .venv/bin/python -m uvicorn live_demo.server:app --host 127.0.0.1 --port 4177
Run:  .venv/bin/python evals/run_live_evals.py [--base http://127.0.0.1:4177] [--only name] [--judge]
Each scenario is its own session. ~1-2 min per scenario. Writes evals/reports/live-<timestamp>.json.

What is NOT covered here (needs a real phone): speaker echo, barge-in over room noise, certificate
trust, mic permissions. See evals/README.md for the manual phone checklist."""
import argparse, asyncio, base64, io, json, pathlib, re, sys, time
from datetime import datetime

import httpx, websockets

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "live_demo"), str(ROOT)]
from server import META_TALK  # noqa: E402

ACCOUNT = {"policyholder_name": "Jordan Lee", "policy_number": "AUTO-90210", "vehicle": "2013 Toyota Sienna"}
CLOSING = re.compile(r"packet is (now )?ready|review (it )?in the app|once you submit", re.I)
PREMATURE = re.compile(r"being submitted|has been submitted|submitted (for|to) (a )?(human )?review|sent (to|for) (an? )?(adjuster|review)|reviewer will|will (get in touch|reach out|contact you|be in touch)", re.I)
ASKS_POLICY = re.compile(r"\b(policy number|what is your name|your full name|confirm your name)\b", re.I)
ASKS_DATE_AGAIN = re.compile(r"\b(when did (this|it|the accident) happen|what (date|day) did)\b", re.I)
DARK_WORDS = re.compile(r"\b(dark|light|lighting|flashlight|brighter)\b", re.I)


def dark_jpeg():
    from PIL import Image
    buf = io.BytesIO(); Image.new("RGB", (512, 384), (18, 18, 18)).save(buf, "JPEG"); return base64.b64encode(buf.getvalue()).decode()


def bright_jpeg():
    from PIL import Image
    buf = io.BytesIO(); Image.new("RGB", (512, 384), (150, 150, 150)).save(buf, "JPEG"); return base64.b64encode(buf.getvalue()).decode()


class Claimant:
    """Minimal stand-in for the phone page: same protocol, text turns instead of audio."""

    def __init__(self, base):
        self.base = base; self.http = httpx.AsyncClient(base_url=base, timeout=30)
        self.session_id = None; self.ws = None; self.agent_turns = []; self.events = []; self.state = {}
        self.first_agent_chunk_at = None; self._reader = None; self.consumed = 0

    async def start(self):
        r = await self.http.post("/api/sessions", json={"app_context": ACCOUNT}, headers={"Origin": self.base}); r.raise_for_status()
        self.session_id = r.json()["session_id"]
        cookies = "; ".join(f"{k}={v}" for k, v in self.http.cookies.items())
        self.ws = await websockets.connect(self.base.replace("http", "ws") + f"/ws/live?session_id={self.session_id}",
                                           additional_headers={"Origin": self.base, "Cookie": cookies}, max_size=4_000_000)
        self._reader = asyncio.create_task(self._read())
        await self.wait_for(lambda: any(e["type"] == "ready" for e in self.events), 25, "ready")

    async def _read(self):
        try:
            async for raw in self.ws:
                m = json.loads(raw); m["_t"] = time.monotonic(); self.events.append(m)
                if m["type"] == "transcript" and m["speaker"] == "Agent":
                    if self.first_agent_chunk_at is None: self.first_agent_chunk_at = m["_t"]
                    if m.get("final"): self.agent_turns.append({"text": m["text"], "t": m["_t"]})
                elif m["type"] == "state": self.state = m["state"]
        except Exception:  # noqa: BLE001
            pass

    async def say(self, text):
        self.first_agent_chunk_at = None; self.t_say = time.monotonic()
        await self.ws.send(json.dumps({"type": "text", "text": text, "id": f"t{int(time.time()*1000)}"}))

    async def send(self, **msg):
        await self.ws.send(json.dumps(msg))

    async def wait_for(self, cond, timeout, what):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if cond(): return True
            await asyncio.sleep(0.2)
        return False

    async def wait_agent_turn(self, timeout=20):
        n = len(self.agent_turns)
        ok = await self.wait_for(lambda: len(self.agent_turns) > n, timeout, "agent turn")
        return self.agent_turns[-1]["text"] if ok else None

    async def next_agent_turn(self, timeout=25):
        """Next agent turn not yet consumed by the scenario (does not skip turns that arrived while we waited)."""
        ok = await self.wait_for(lambda: len(self.agent_turns) > self.consumed, timeout, "agent turn")
        if not ok: return None
        turn = self.agent_turns[self.consumed]["text"]; self.consumed += 1
        return turn

    def agent_text(self):
        return " ".join(t["text"] for t in self.agent_turns)

    async def close(self):
        if self._reader: self._reader.cancel()
        try: await self.ws.close()
        except Exception: pass
        await self.http.delete(f"/api/sessions/{self.session_id}", headers={"Origin": self.base})
        await self.http.aclose()


async def common_checks(c, failures):
    text = c.agent_text()
    if META_TALK.search(text): failures.append(f"meta-talk leaked: {META_TALK.search(text).group(0)!r}")
    if re.search(r"\bdemo\b", text, re.I): failures.append("agent said 'demo'")
    if ASKS_POLICY.search(text): failures.append(f"asked for name/policy: {ASKS_POLICY.search(text).group(0)!r}")
    order = [t["text"] for t in c.agent_turns]
    first_close = next((i for i, t in enumerate(order) if CLOSING.search(t)), len(order))
    early = next((t for t in order[:first_close] if PREMATURE.search(t)), None)
    if early: failures.append(f"invented submission/review status before the closing: {early!r}")
    foreign = re.findall(r"[\u0400-\u04FF\u0590-\u05FF\u0600-\u06FF\u0900-\u0DFF\u3040-\u30FF\u3400-\u9FFF\uAC00-\uD7AF]", text)
    if len(foreign) >= 3: failures.append(f"non-Latin script in agent transcript: {''.join(foreign[:8])!r}")
    elif foreign: print(f"      note: stray glyph(s) {''.join(foreign)!r} in transcript (sanitized server-side; not a failure)")


async def scenario_greeting(c, f):
    """Agent speaks first, by name, within 12 s, asks about safety, and does not speak twice unprompted."""
    t0 = time.monotonic()
    first = await c.wait_agent_turn(12)
    if not first: return f.append("no spoken greeting within 12 s")
    if "jordan" not in first.lower(): f.append(f"greeting lacks first name: {first!r}")
    if "safe" not in first.lower(): f.append(f"greeting lacks safety question: {first!r}")
    await asyncio.sleep(6)
    if len(c.agent_turns) > 1: f.append(f"spoke again unprompted within 6 s: {c.agent_turns[1]['text']!r}")


async def scenario_latency_and_single_reply(c, f):
    """Reply starts within 3 s of the claimant's turn; exactly one reply, then it waits."""
    await c.wait_agent_turn(12)
    await c.say("Yes, all safe. A shopping cart hit my van at the Safeway on Coleman Ave in San Jose this morning and scratched the passenger door. Email is best.")
    ok = await c.wait_for(lambda: c.first_agent_chunk_at is not None, 10, "first chunk")
    if not ok: return f.append("no reply within 10 s")
    latency = c.first_agent_chunk_at - c.t_say
    print(f"      first words after {latency:.1f} s")
    if latency > 4.0: f.append(f"first words took {latency:.1f} s (limit 4.0)")
    n_before = len(c.agent_turns); await c.wait_agent_turn(15); await asyncio.sleep(8)
    extra = len(c.agent_turns) - n_before
    reply = c.agent_turns[n_before]["text"] if len(c.agent_turns) > n_before else ""
    if extra > 1 and "?" in reply: f.append(f"spoke {extra} times after one claimant turn although the first reply already asked a question")
    if ASKS_DATE_AGAIN.search(c.agent_text()): f.append("re-asked the date after it was given")


async def scenario_full_call_closes(c, f):
    """Facts, then answer whatever the agent asks (checklist only), expect the closing line, state.closed, no question after."""
    await c.wait_agent_turn(12)
    await c.say("Yes, all safe. Another car hit me from behind at a red light on Coleman Ave in San Jose this morning. Email is best.")
    answers = [(re.compile(r"photo|picture|camera|show me", re.I), "I cannot get to the car right now, so no photos at the moment."),
               (re.compile(r"police|report|exchange", re.I), "No, I did not file a police report."),
               (re.compile(r"other driver|other party|witness|their (details|information|insurance)|anyone else", re.I), "They drove off before I could get anything, and nobody saw it.")]
    off_checklist = re.compile(r"estimate|receipt|invoice|medical|mileage|VIN\b|odometer", re.I)
    turns = 0; c.consumed = len(c.agent_turns)
    while turns < 8 and not CLOSING.search(c.agent_text()):
        reply = await c.next_agent_turn(25)
        if reply is None: break
        if off_checklist.search(reply): f.append(f"asked for an off-checklist item: {reply!r}")
        if CLOSING.search(reply): break
        if "?" not in reply: continue  # acknowledgment only; the next turn (question or closing) follows
        line = next((a for pat, a in answers if pat.search(reply)), "No, I don't have that.")
        await c.say(line); turns += 1
    if not CLOSING.search(c.agent_text()):
        await c.say("Okay. Are we done here?")  # the question that produced invented status language on the phone
        await c.next_agent_turn(25)
    closed = await c.wait_for(lambda: CLOSING.search(c.agent_text()) is not None, 45, "closing")
    if not closed: return f.append(f"no closing line after {turns} answers; last agent: {c.agent_turns[-1]['text'] if c.agent_turns else None!r}")
    await c.wait_for(lambda: c.state.get("closed"), 20, "state.closed")
    if not c.state.get("closed"): f.append(f"state.closed never became true (Review & submit hidden); closing_allowed={c.state.get('closing_allowed')} missing={c.state.get('missing_blockers')} docs={[d['item'] for d in c.state.get('documents', []) if not d.get('already_provided')]}")
    n = len(c.agent_turns); await asyncio.sleep(15)
    order = [t["text"] for t in c.agent_turns]
    first_close = next((i for i, t in enumerate(order) if CLOSING.search(t)), None)
    if first_close is not None and any("?" in t for t in order[first_close + 1:]): f.append(f"question after the closing line: {[t for t in order[first_close + 1:] if '?' in t][0]!r}")


async def scenario_quiet_reask(c, f):
    """After a question and ~10 s of silence: exactly one gentle re-ask, in different words, then silence."""
    await c.wait_agent_turn(12)
    # The gentle re-ask applies to a question the agent asked in its first reply to a claimant turn. A question
    # produced by the 10 s "you didn't ask anything" follow-up is already the second utterance for that turn and
    # deliberately gets no further re-ask. So find a first-reply question before testing the silence.
    lines = ["Yes, all safe. Another car hit me from behind at a red light on Coleman Ave this morning.",
             "Nobody was hurt, and email is the best way to reach me.",
             "I don't have photos yet."]
    q = None
    for line in lines:
        await c.say(line); c.consumed = len(c.agent_turns)
        first = await c.next_agent_turn(25)
        if first and "?" in first: q = first; break
        await c.next_agent_turn(25)  # consume the follow-up question for this turn, then try a fresh turn
    if not q: return f.append("agent never asked a question in a first reply across three turns")
    await c.send(type="playback", playing=False)
    n = len(c.agent_turns); t0 = time.monotonic()
    got = await c.wait_for(lambda: len(c.agent_turns) > n, 30, "re-ask")
    if not got: return f.append("no gentle re-ask within 30 s of silence")
    dt = c.agent_turns[-1]["t"] - t0
    if dt < 8: f.append(f"re-ask fired after only {dt:.1f} s (expected ≥ ~10 s)")
    if c.agent_turns[-1]["text"].strip().lower() == q.strip().lower(): f.append("re-ask was verbatim, not rephrased")
    n2 = len(c.agent_turns); await asyncio.sleep(16)
    if len(c.agent_turns) > n2: f.append(f"asked a third time after continued silence: {c.agent_turns[-1]['text']!r}")


async def scenario_voice_cancels_reask(c, f):
    """Claimant activity inside the silence window must cancel the re-ask (incident: re-ask over the answer)."""
    await c.wait_agent_turn(12)
    await c.say("Yes, all safe. A cart hit my van at Safeway on Coleman Ave this morning.")
    await c.wait_agent_turn(20); await c.send(type="playback", playing=False)
    await asyncio.sleep(5); await c.send(type="activity", speaking=True); await asyncio.sleep(1)
    await c.say("It scratched the passenger door, nobody was hurt.")
    reply = await c.wait_agent_turn(20)
    if not reply: return f.append("no reply to the follow-up")
    if re.search(r"no rush|take your time", reply, re.I) and "scratch" not in reply.lower() and "door" not in reply.lower():
        f.append(f"responded with a re-ask instead of to the claimant's words: {reply!r}")


async def scenario_dark_camera(c, f):
    """Three dark frames → lighting event + the agent asks for light; bright frames clear it."""
    await c.wait_agent_turn(12)
    await c.send(type="camera_state", enabled=True)
    d = dark_jpeg()
    for _ in range(4): await c.send(type="video", data=d); await asyncio.sleep(1)
    got = await c.wait_for(lambda: any(e["type"] == "lighting" and e.get("dark") for e in c.events), 10, "lighting")
    if not got: f.append("no lighting{dark:true} event after 4 dark frames")
    spoke = await c.wait_for(lambda: DARK_WORDS.search(c.agent_text()) is not None, 20, "asks for light")
    if not spoke: f.append(f"agent did not ask for better light; said: {c.agent_turns[-1]['text'] if c.agent_turns else None!r}")
    b = bright_jpeg()
    for _ in range(3): await c.send(type="video", data=b); await asyncio.sleep(1)
    cleared = await c.wait_for(lambda: any(e["type"] == "lighting" and not e.get("dark") for e in c.events), 10, "clear")
    if not cleared: f.append("lighting never cleared after bright frames")
    await c.send(type="camera_state", enabled=False)


async def scenario_collects_other_driver_on_camera(c, f):
    """Claimant HAS the other driver's details: the agent asks to see them on camera (and the app gets a camera_request),
    never defers them to 'the app' or 'later'."""
    await c.wait_agent_turn(12)
    await c.say("Yes, all safe. Another car hit me from behind at a red light on Coleman Ave in San Jose this morning. Email is best.")
    c.consumed = len(c.agent_turns)
    for _ in range(4):
        reply = await c.next_agent_turn(25)
        if reply is None: break
        if re.search(r"other driver|other party|their (details|information|insurance)|exchange", reply, re.I):
            await c.say("Yes, I have the other driver's insurance card and their license plate right here."); break
        if "?" in reply: await c.say("No, not right now.")
    else:
        return f.append("agent never asked about the other driver")
    c.consumed = len(c.agent_turns)
    got_ask = await c.wait_for(lambda: any(re.search(r"camera|show me|hold (it|the card) up", t["text"], re.I) for t in c.agent_turns[c.consumed:]), 25, "camera ask")
    if not got_ask: return f.append(f"did not ask to see the details on camera; said: {c.agent_turns[-1]['text'] if c.agent_turns else None!r}")
    if not await c.wait_for(lambda: any(e["type"] == "camera_request" for e in c.events), 5, "camera_request"): f.append("no camera_request event sent to the phone")
    bad = next((t["text"] for t in c.agent_turns if re.search(r"\b(in the app|later|to the adjuster)\b", t["text"], re.I) and re.search(r"provide|upload|add|submit", t["text"], re.I)), None)
    if bad: f.append(f"deferred the details instead of collecting them: {bad!r}")


async def scenario_injury_escalates_and_closes(c, f):
    """Claimant reports neck pain: the route escalates, the agent gives safety guidance, still works the checklist, and closes
    with the urgent-review wording instead of stalling on 'finishing my notes'."""
    await c.wait_agent_turn(12)
    await c.say("We're out of the car, but my neck hurts a little. Another car hit us from the side a few minutes ago in Pleasanton near Valley Avenue and Santa Rita Road. Phone is best.")
    c.consumed = len(c.agent_turns)
    answers = [(re.compile(r"hurt|injur|pain|medical|doctor", re.I), "It's just a sore neck, I can talk."),
               (re.compile(r"photo|picture|camera|show me", re.I), "I can't get to the car right now."),
               (re.compile(r"police|report|exchange", re.I), "No police report."),
               (re.compile(r"other driver|other party|witness|their (details|information|insurance)|anyone else", re.I), "The other driver gave me nothing and left, and there were no witnesses.")]
    turns = 0
    while turns < 8 and not CLOSING.search(c.agent_text()):
        reply = await c.next_agent_turn(25)
        if reply is None: break
        if CLOSING.search(reply): break
        if "?" not in reply: continue
        line = next((a for pat, a in answers if pat.search(reply)), "No, I don't have that.")
        await c.say(line); turns += 1
    if not CLOSING.search(c.agent_text()):
        await c.say("Are you done? Do you need anything else from me?"); await c.next_agent_turn(25)
    closed = await c.wait_for(lambda: CLOSING.search(c.agent_text()) is not None, 45, "closing")
    if not closed: return f.append(f"no closing line; last agent: {c.agent_turns[-1]['text'] if c.agent_turns else None!r}")
    await c.wait_for(lambda: c.state.get("closed"), 20, "state.closed")
    if c.state.get("route") != "emergency_escalation": f.append(f"route is {c.state.get('route')!r}, expected emergency_escalation after a reported injury")
    if not c.state.get("closed"): f.append("state.closed never became true")
    closing = next((t["text"] for t in c.agent_turns if CLOSING.search(t["text"])), "")
    if not re.search(r"urgent|immediate|human (representative|review)|medical", closing, re.I): f.append(f"closing lacks the urgent-review/medical wording: {closing!r}")
    stalls = [t["text"] for t in c.agent_turns if re.search(r"finishing up my notes", t["text"], re.I)]
    if len(stalls) > 1: f.append(f"stalled on 'finishing my notes' {len(stalls)} times")


SCENARIOS = {
    "greeting": scenario_greeting,
    "latency_single_reply": scenario_latency_and_single_reply,
    "full_call_closes": scenario_full_call_closes,
    "quiet_reask": scenario_quiet_reask,
    "voice_cancels_reask": scenario_voice_cancels_reask,
    "dark_camera": scenario_dark_camera,
    "collects_other_driver_on_camera": scenario_collects_other_driver_on_camera,
    "injury_escalates_and_closes": scenario_injury_escalates_and_closes,
}


async def judge(transcript_text):
    """Optional LLM-judge for tone: warm, concise, no repetition. Uses Gemini Flash with thinking off."""
    import server as srv
    from google.genai import types
    prompt = ("Rate this insurance claim intake agent's lines (1-5 each): warmth, conciseness, no_repetition, natural_flow. "
              "Return JSON {warmth, conciseness, no_repetition, natural_flow, worst_line, reason}.\n\n" + transcript_text)
    r = await srv._client().aio.models.generate_content(model=srv.MODEL, contents=prompt,
        config=types.GenerateContentConfig(response_mime_type="application/json", thinking_config=types.ThinkingConfig(thinking_budget=0)))
    return json.loads(r.text)


async def run(base, only=None, use_judge=False):
    results = []
    for name, fn in SCENARIOS.items():
        if only and only not in name: continue
        c = Claimant(base); failures = []; t0 = time.monotonic()
        try:
            await c.start(); await fn(c, failures); await common_checks(c, failures)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"exception: {type(exc).__name__}: {str(exc)[:200]}")
        transcript = [t["text"] for t in c.agent_turns]
        rec = {"scenario": name, "doc": fn.__doc__, "seconds": round(time.monotonic() - t0, 1), "pass": not failures, "failures": failures, "agent_turns": transcript}
        if use_judge and transcript:
            try: rec["judge"] = await judge("\n".join(f"Agent: {t}" for t in transcript))
            except Exception as exc:  # noqa: BLE001
                rec["judge"] = {"error": str(exc)[:120]}
        results.append(rec)
        print(f"{'PASS' if not failures else 'FAIL'}  {name:22s} {rec['seconds']:5.1f}s  {'; '.join(failures)}")
        if use_judge and "judge" in rec: print(f"      judge: {rec['judge']}")
        await c.close()
    pathlib.Path(ROOT / "evals" / "reports").mkdir(exist_ok=True)
    out = ROOT / "evals" / "reports" / f"live-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    out.write_text(json.dumps(results + [{"_meta": True, "git_rev": _git_rev(), "generated": datetime.now().isoformat(timespec="seconds")}], indent=2))
    passed = sum(r["pass"] for r in results)
    print(f"\n{passed}/{len(results)} passed  (report: {out.relative_to(ROOT)})")
    return passed == len(results)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--base", default="http://127.0.0.1:4177"); ap.add_argument("--only"); ap.add_argument("--judge", action="store_true")
    a = ap.parse_args(); sys.exit(0 if asyncio.run(run(a.base, a.only, a.judge)) else 1)
