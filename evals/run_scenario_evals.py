"""End-to-end scenario evals: a Gemini-simulated claimant with a persona and ground truth talks to the REAL agent
(server + Gemini Live over the WebSocket), and the conversation is judged three ways:

  1. Deterministic: route, closing, nothing asked after the close, checklist coverage, forbidden questions,
     camera request, guard phrases, reply latency.
  2. Extraction accuracy (tracked metric, not a gate): the claim notes vs the scenario's ground truth.
  3. Judge rubric (tracked, not a gate): warmth, no repetition, collected what the claimant had, no advice.

Prereq: local server on :4177 (see evals/README.md). Run:
  .venv/bin/python evals/run_scenario_evals.py [--only id] [--repeat 1] [--base http://127.0.0.1:4177]
About 1-2 min and a few cents per scenario. Writes evals/reports/scenario-<timestamp>.json."""
import argparse, asyncio, base64, io, json, pathlib, re, sys, time
from datetime import datetime

import httpx, websockets

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "live_demo"), str(ROOT)]
import server as srv  # noqa: E402  (loads .env, exposes the guard regexes)
from google.genai import types  # noqa: E402

SCENARIOS_DIR = ROOT / "evals" / "scenarios"
REPORTS = ROOT / "evals" / "reports"

def _git_rev():
    import subprocess
    try: return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, text=True).strip()
    except Exception: return "unknown"

ACCOUNT = {"policyholder_name": "Jordan Lee", "policy_number": "AUTO-90210", "vehicle": "2013 Toyota Sienna"}
CLOSING = re.compile(r"packet is (now )?ready|review (it )?in the app|once you submit", re.I)
DECLINE = re.compile(r"only (take|handle|help with|accept)[^.]{0,40}auto|auto (insurance )?claims only|not (an )?auto|can't help with (that|this) here|contact (the )?insurer for that", re.I)
ASK = {  # how the agent asks for each checklist item, used for coverage and for "should not ask"
    "photos": re.compile(r"photo|picture|camera|show me", re.I),
    "police": re.compile(r"police|incident report|exchange form|report number", re.I),
    "other_driver": re.compile(r"other driver|other party|their (details|information|insurance|card)|license plate|exchange(d)? (information|details)", re.I),
    "other_driver_exchange": re.compile(r"other driver|other party|exchange(d)? (information|details|insurance)|their insurance", re.I),
}
REASK = {"date": re.compile(r"\b(when did (this|it|that|the accident) happen|what (date|day|time) did)\b", re.I),
         "location": re.compile(r"\bwhere did (this|it|that|the accident) happen\b", re.I),
         "contact": re.compile(r"\b(phone or (by )?email|best way to reach)\b", re.I),
         "police": ASK["police"]}
CAMERA_RETRY = re.compile(r"(can't|cannot|unable to|not able to|having trouble|still can't)[^.?]{0,40}(see|make out)|get (a bit |a little )?closer|hold (the phone|it) closer|more light|turn on a light|better lit|well.?lit|adjust(ing)? the light", re.I)
MEDICAL = re.compile(r"medical (care|attention|help)|see a doctor|get checked", re.I)
ADVICE = re.compile(r"\b(apply ice|rest your|ibuprofen|you should (replace|repair)|do it yourself|body shop i recommend|would cost|premium would|rate would)\b", re.I)


def frame(gray=150):
    from PIL import Image
    buf = io.BytesIO(); Image.new("RGB", (512, 384), (gray, gray, gray)).save(buf, "JPEG"); return base64.b64encode(buf.getvalue()).decode()


def damage_frame(kind="damage"):
    """A plausible 'what the phone sees' frame: a silver door panel with a dark dent/scratch, or an insurance card."""
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGB", (512, 384), (118, 122, 128)); d = ImageDraw.Draw(img)
    if kind == "card":
        d.rectangle([80, 90, 432, 300], fill=(245, 245, 240), outline=(60, 60, 60), width=3)
        try: f = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial Bold.ttf", 22); g = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", 18)
        except Exception: f = g = None
        d.text((100, 105), "PROOF OF AUTO INSURANCE", fill=(30, 60, 120), font=f)
        d.text((100, 150), "Insured: Alex Morgan", fill=(20, 20, 20), font=g); d.text((100, 180), "Policy: 448-2201-7", fill=(20, 20, 20), font=g)
        d.text((100, 210), "Vehicle: 2019 Honda Civic  Plate 8ABC123", fill=(20, 20, 20), font=g); d.text((100, 250), "Effective 01/01/2026 - 12/31/2026", fill=(20, 20, 20), font=g)
    else:
        d.rectangle([40, 60, 472, 330], fill=(176, 180, 186), outline=(90, 90, 95), width=4)      # door panel
        d.ellipse([300, 150, 360, 190], fill=(60, 60, 64))                                        # door handle
        d.line([90, 120, 420, 230], fill=(40, 38, 36), width=9)                                    # long scratch
        d.ellipse([150, 200, 260, 290], fill=(110, 112, 118), outline=(70, 70, 74), width=3)       # dent shading
    buf = io.BytesIO(); img.save(buf, "JPEG", quality=85); return base64.b64encode(buf.getvalue()).decode()


class Line:
    def __init__(self, who, text, t): self.who, self.text, self.t = who, text, t


class Session:
    """The phone's protocol with text turns instead of audio."""

    def __init__(self, base):
        self.base = base; self.http = httpx.AsyncClient(base_url=base, timeout=30)
        self.id = None; self.ws = None; self.events = []; self.lines = []; self.state = {}
        self.first_chunk_at = None; self.consumed = 0; self._reader = None; self.latencies = []

    async def start(self):
        r = await self.http.post("/api/sessions", json={"app_context": ACCOUNT}, headers={"Origin": self.base}); r.raise_for_status()
        self.id = r.json()["session_id"]
        cookies = "; ".join(f"{k}={v}" for k, v in self.http.cookies.items())
        self.ws = await websockets.connect(self.base.replace("http", "ws") + f"/ws/live?session_id={self.id}",
                                           additional_headers={"Origin": self.base, "Cookie": cookies}, max_size=4_000_000)
        self._reader = asyncio.create_task(self._read())
        await self.wait(lambda: any(e["type"] == "ready" for e in self.events), 25)

    async def _read(self):
        try:
            async for raw in self.ws:
                m = json.loads(raw); m["_t"] = time.monotonic(); self.events.append(m)
                if m["type"] == "transcript" and m["speaker"] == "Agent":
                    if self.first_chunk_at is None: self.first_chunk_at = m["_t"]
                    if m.get("final"): self.lines.append(Line("Agent", m["text"], m["_t"]))
                elif m["type"] == "state": self.state = m["state"]
        except Exception:  # noqa: BLE001
            pass

    async def say(self, text):
        self.lines.append(Line("Claimant", text, time.monotonic()))
        self.first_chunk_at = None; self.t_say = time.monotonic()
        await self.ws.send(json.dumps({"type": "text", "text": text, "id": f"t{int(time.time()*1000)}"}))

    async def wait(self, cond, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if cond(): return True
            await asyncio.sleep(0.2)
        return False

    def agent_lines(self): return [l for l in self.lines if l.who == "Agent"]

    async def next_agent(self, timeout=25):
        agent = self.agent_lines()
        ok = await self.wait(lambda: len(self.agent_lines()) > self.consumed, timeout)
        if not ok: return None
        line = self.agent_lines()[self.consumed]; self.consumed += 1
        if self.first_chunk_at and getattr(self, "t_say", None) and self.first_chunk_at > self.t_say:
            self.latencies.append(self.first_chunk_at - self.t_say); self.t_say = None
        return line.text

    async def close(self):
        if self._reader: self._reader.cancel()
        try: await self.ws.close()
        except Exception: pass
        try:
            r = await self.http.get(f"/api/sessions/{self.id}", headers={"Origin": self.base}); final = r.json().get("state", {}) if r.status_code == 200 else self.state
        except Exception:  # noqa: BLE001
            final = self.state
        try: await self.http.delete(f"/api/sessions/{self.id}", headers={"Origin": self.base})
        except Exception: pass
        await self.http.aclose()
        return final


async def flash_json(prompt, schema=None):
    cfg = types.GenerateContentConfig(response_mime_type="application/json", thinking_config=types.ThinkingConfig(thinking_budget=0))
    r = await srv._client().aio.models.generate_content(model=srv.MODEL, contents=prompt, config=cfg)
    return json.loads(r.text)


async def claimant_reply(sc, transcript, agent_line, off_topic_queue):
    """The simulated claimant: answers in character from the scenario facts, volunteers per the persona, never invents."""
    facts = "\n".join(f"- {k}: {v}" for k, v in sc["facts"].items())
    volunteer = "In your FIRST answer, volunteer everything relevant in one breath (what happened, when, where, injuries, contact preference, what you have)." if sc["persona"].get("volunteer_up_front") else "Answer only what was asked, in one or two short sentences; do not volunteer other facts unless asked."
    off_topic = f"Before answering, first say this off-topic line exactly once, then wait: {off_topic_queue[0]!r}" if off_topic_queue else ""
    history = "\n".join(f"{l.who}: {l.text}" for l in transcript[-12:])
    prompt = f"""You are role-playing an insurance customer on a voice call with an AI claims agent. Stay in character; never mention being an AI or a test.
Who you are: Jordan Lee, policy AUTO-90210, 2013 Toyota Sienna. Style: {sc['persona']['style']}.
The true facts of your situation (use only these; if asked something not covered, say you don't know or don't have it):
{facts}
{volunteer}
{off_topic}
If the agent asks you to turn on the camera or show something and you have it, say "Sure, here it is" (and nothing else) and set offered_camera=true. If you don't have it, say so plainly.
If you are already showing something and the agent describes what it sees, say "That's it, that's the damage" (or "that's the card") and nothing else.
If the agent asks you to adjust the camera (closer, more light, different angle): the first time, say "Okay, how about now?"; the second time or later, say "I can't get a better shot right now, can we move on?" and set gave_up_camera=true.
If the agent has given a closing message (your packet is ready / review it in the app) or declined your claim as not an auto claim, reply with a brief goodbye and set done=true.
Never ask the agent to repeat itself. Keep each reply under 30 words.

Conversation so far:
{history}
Agent just said: {agent_line}

Return JSON: {{"say": "<what you say next>", "done": <true if the call is over>, "offered_camera": <true if you just said you're showing something>, "gave_up_camera": <true if you just said you can't get a better shot>}}"""
    try:
        out = await flash_json(prompt)
        return str(out.get("say", "")).strip() or "Okay.", bool(out.get("done")), bool(out.get("offered_camera")), bool(out.get("gave_up_camera"))
    except Exception as exc:  # noqa: BLE001
        return "Sorry, could you say that again?", False, False, False


async def judge(sc, transcript):
    text = "\n".join(f"{l.who}: {l.text}" for l in transcript)
    prompt = f"""Score this insurance claim intake call. The caller's true situation: {json.dumps(sc['facts'])}.
Rate the AGENT only, 1 (poor) to 5 (excellent), as JSON with integer fields:
warmth (kind, human, not robotic), no_repetition (never re-asks or restates), collected_what_caller_had (asked to see or capture items the caller said they had, instead of deferring them),
no_advice (gave no medical, legal, repair or pricing advice), efficiency (no unnecessary questions for this kind of claim), plus "worst_moment": one short quote or "none".

{text}"""
    try:
        return await flash_json(prompt)
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)[:120]}


def extraction_accuracy(sc, state):
    truth = sc.get("truth") or {}
    if not truth: return None
    f = state.get("fields", {}) or {}
    def val(k): return str((f.get(k) or {}).get("value", "")).lower()
    checks = {}
    if "date_of_loss" in truth: checks["date"] = truth["date_of_loss"] in val("date")
    if "location_keywords" in truth: checks["location"] = any(k in val("location") for k in truth["location_keywords"])
    if "cause_keywords" in truth: checks["cause"] = any(k in (val("description") + " " + val("cause")) for k in truth["cause_keywords"])
    if "injury_status" in truth:
        inj = val("injuries"); status = (f.get("injuries") or {}).get("status", "")
        checks["injuries"] = ("urgent" == status or re.search(r"hurt|pain|injur", inj)) if truth["injury_status"] == "present" else bool(re.search(r"no |none|not |denied", inj) or not inj)
    if "contact_method" in truth: checks["contact"] = truth["contact_method"] in val("contact")
    if "other_party" in truth:
        od = val("otherDriver") + " " + val("description")
        checks["other_party"] = {"known": bool(re.search(r"insurance|card|plate|exchang|other driver", od)), "fled": bool(re.search(r"fled|drove off|left|hit and run|unknown", od)),
                                 "none": not re.search(r"other driver|other party", val("otherDriver")), "unknown": True}.get(truth["other_party"], True)
    return {"checks": checks, "score": round(100 * sum(checks.values()) / max(1, len(checks)))}


def evaluate(sc, S, final_state, transcript, extras):
    ex = sc["expect"]; fails = []; notes = []
    agent = [l.text for l in transcript if l.who == "Agent"]; joined = " ".join(agent)
    route = final_state.get("route")
    if ex.get("route") and route != ex["route"]: fails.append(f"route {route!r}, expected {ex['route']!r}")
    first_close = next((i for i, t in enumerate(agent) if CLOSING.search(t)), None)
    if ex.get("closes"):
        if first_close is None: fails.append("never gave the closing line")
        elif any("?" in t for t in agent[first_close + 1:]): fails.append("asked a question after the closing")
        if first_close is not None and not final_state.get("closed"): fails.append("state.closed stayed false after the closing line")
    else:
        if first_close is not None: fails.append("gave a closing line for a claim that should not close")
    if ex.get("declines") and not DECLINE.search(joined): fails.append("did not decline the non-auto loss")
    for item in ex.get("should_ask", []):
        if not ASK[item].search(joined): fails.append(f"never asked about {item}")
    for item in ex.get("should_not_ask", []):
        hit = next((t for t in agent if ASK[item].search(t) and "?" in t), None)
        if hit: fails.append(f"asked about {item} although irrelevant: {hit!r}")
    if ex.get("camera_request") and not any(e["type"] == "camera_request" for e in S.events): fails.append("no camera_request sent to the phone")
    for k in ex.get("no_reask", []):
        if sum(1 for t in agent if REASK[k].search(t)) > 0: fails.append(f"re-asked {k} after it was volunteered")
    if ex.get("max_agent_questions") and sum("?" in t for t in agent) > ex["max_agent_questions"]:
        fails.append(f"{sum('?' in t for t in agent)} questions asked, limit {ex['max_agent_questions']}")
    if ex.get("medical_reminders_max") and sum(1 for t in agent if MEDICAL.search(t)) > ex["medical_reminders_max"]:
        fails.append(f"medical reminder repeated {sum(1 for t in agent if MEDICAL.search(t))} times")
    if ex.get("must_decline_off_topic"):
        if ADVICE.search(joined): fails.append(f"gave advice or a quote: {ADVICE.search(joined).group(0)!r}")
    for name, rx in (("meta-talk", srv.META_TALK), ("premature status", srv.PREMATURE_CLOSE), ("off-checklist request", srv.OFF_CHECKLIST_ASK)):
        hit = next((t for t in agent if rx.search(t)), None)
        if hit and not (name == "premature status" and first_close is not None and agent.index(hit) >= first_close): fails.append(f"{name} in transcript: {hit!r}")
    if re.search(r"\b(policy number|your full name)\b", joined, re.I): fails.append("asked for name or policy number")
    if S.latencies:
        worst = max(S.latencies); notes.append(f"first words: median {sorted(S.latencies)[len(S.latencies)//2]:.1f}s, worst {worst:.1f}s")
        if worst > 5.0: fails.append(f"a reply took {worst:.1f}s to start")
    retries = sum(1 for t in agent if CAMERA_RETRY.search(t))
    if retries > 3: fails.append(f"kept asking to adjust the camera {retries} times instead of moving on")
    if extras.get("timed_out"): fails.append("conversation did not finish within the turn/time budget")
    return fails, notes


async def run_one(sc, base):
    S = Session(base); transcript = S.lines; extras = {}; off_topic = list(sc["persona"].get("off_topic_lines", []))
    t0 = time.monotonic()
    try:
        await S.start()
        line = await S.next_agent(15)
        turns = 0; done = False
        while line is not None and turns < 14 and time.monotonic() - t0 < 240:
            if "?" not in line and not CLOSING.search(line) and not DECLINE.search(line):
                nxt = await S.next_agent(14)  # acknowledgment only: the question or closing follows
                if nxt is not None: line = nxt; continue
            say, done, offered, gave_up = await claimant_reply(sc, transcript, line, off_topic)
            if off_topic and say.startswith(off_topic[0][:12]): off_topic.pop(0)
            await S.say(say); turns += 1
            if offered and not extras.get("camera_on"):
                # The persona is showing something: stream a plausible frame so the agent can pin it and move on.
                extras["camera_on"] = True
                kind = "card" if re.search(r"card|plate", (sc["facts"].get("other_party", "") + " " + say), re.I) and re.search(r"card|driver|plate", line, re.I) else "damage"
                await S.ws.send(json.dumps({"type": "camera_state", "enabled": True}))
                img = damage_frame(kind)
                for _ in range(6): await S.ws.send(json.dumps({"type": "video", "data": img})); await asyncio.sleep(1)
            if gave_up and extras.get("camera_on"):
                extras["camera_on"] = False; extras["camera_retries"] = extras.get("camera_retries", 0) + 1
                await S.ws.send(json.dumps({"type": "camera_state", "enabled": False}))
            if done: break
            line = await S.next_agent(30)
        if not done and line is not None and not CLOSING.search(" ".join(l.text for l in S.agent_lines())):
            await S.wait(lambda: CLOSING.search(" ".join(l.text for l in S.agent_lines())) is not None, 40)
            await S.next_agent(5)
        if not done and turns >= 14: extras["timed_out"] = True
        await asyncio.sleep(3)
    except Exception as exc:  # noqa: BLE001
        extras["error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
    final_state = await S.close()
    fails, notes = evaluate(sc, S, final_state, transcript, extras)
    if extras.get("error"): fails.append(extras["error"])
    acc = extraction_accuracy(sc, final_state)
    jd = await judge(sc, transcript) if transcript else {}
    return {"id": sc["id"], "title": sc["title"], "tests": sc["tests"], "pass": not fails, "failures": fails, "notes": notes,
            "route": final_state.get("route"), "closed": bool(final_state.get("closed")), "seconds": round(time.monotonic() - t0, 1),
            "extraction": acc, "judge": jd, "transcript": [{"who": l.who, "text": l.text} for l in transcript]}


async def main(base, only, repeat):
    results = []
    for path in sorted(SCENARIOS_DIR.glob("*.json")):
        sc = json.loads(path.read_text())
        if only and only not in sc["id"]: continue
        for i in range(repeat):
            r = await run_one(sc, base); r["run"] = i + 1; results.append(r)
            acc = f"{r['extraction']['score']}%" if r.get("extraction") else "n/a"
            js = r.get("judge") or {}; jv = ", ".join(f"{k}={v}" for k, v in js.items() if isinstance(v, int))
            print(f"{'PASS' if r['pass'] else 'FAIL'}  {sc['id']:34s} {r['seconds']:5.0f}s  route={r['route']}  extraction={acc}  judge[{jv}]")
            for f_ in r["failures"]: print(f"        - {f_}")
            for n in r["notes"]: print(f"        · {n}")
    REPORTS.mkdir(exist_ok=True)
    out = REPORTS / f"scenario-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    out.write_text(json.dumps(results + [{"_meta": True, "git_rev": _git_rev(), "generated": datetime.now().isoformat(timespec="seconds")}], indent=2))
    passed = sum(r["pass"] for r in results)
    accs = [r["extraction"]["score"] for r in results if r.get("extraction")]
    print(f"\n{passed}/{len(results)} passed · extraction accuracy avg {sum(accs)//max(1,len(accs))}% · report: {out.relative_to(ROOT)}")
    return passed == len(results)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--base", default="http://127.0.0.1:4177"); ap.add_argument("--only"); ap.add_argument("--repeat", type=int, default=1)
    a = ap.parse_args(); sys.exit(0 if asyncio.run(main(a.base, a.only, a.repeat)) else 1)
