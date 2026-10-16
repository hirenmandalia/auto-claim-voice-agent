"""Render the latest eval results into one HTML release report.

    .venv/bin/python evals/render_report.py [--base http://127.0.0.1:4177] [--out evals/reports/report.html]

Inputs: the newest evals/reports/pipeline-*.json and live-*.json, the rules suite (run here, no network),
server health (if the server is up), and the git revision. The page is self-contained and theme-aware, so it can
be published as an artifact, attached to a release, or opened from the repo. run_evals.sh calls this at the end."""
import argparse, datetime, html, io, json, pathlib, subprocess, sys, unittest, urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
REPORTS = ROOT / "evals" / "reports"


def latest(prefix, key):
    """Newest result per case/scenario across all reports, so a single re-run (--only) refreshes just that row.
    Returns (records in first-seen order, label describing the newest source file)."""
    files = sorted(REPORTS.glob(f"{prefix}-*.json"))
    if not files:
        return None, None
    merged, order = {}, []
    for f in files:
        for rec in json.loads(f.read_text()):
            if rec.get("_meta"): continue
            k = rec[key]
            if k not in merged:
                order.append(k)
            merged[k] = {**rec, "_source": f.name}
    return [merged[k] for k in order], files[-1].name


def run_rules():
    sys.path[:0] = [str(ROOT), str(ROOT / "live_demo")]
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"), pattern="test_*.py")
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream, verbosity=0).run(suite)
    failed = [(str(t), tb.strip().splitlines()[-1][:200]) for t, tb in result.failures + result.errors]
    return {"ran": result.testsRun, "failed": failed}


def health(base):
    try:
        with urllib.request.urlopen(f"{base}/api/health", timeout=4) as r:
            return json.load(r)
    except Exception:  # noqa: BLE001
        return None


def git_rev():
    try:
        sha = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, text=True).strip()
        dirty = subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True).strip()
        return sha + (" (uncommitted changes)" if dirty else "")
    except Exception:  # noqa: BLE001
        return "unknown"


def e(x):
    return html.escape(str(x))


def pill(ok, text=None):
    cls = "ok" if ok else "bad"
    return f'<span class="pill {cls}">{e(text or ("PASS" if ok else "FAIL"))}</span>'


def render(rules, pipeline, pipe_name, live, live_name, scen, scen_name, hlth, base):
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    rules_ok = not rules["failed"]
    pipe_ok = bool(pipeline) and all(c["pass"] for c in pipeline)
    live_ok = bool(live) and all(s["pass"] for s in live)
    scen_ok = (not scen) or all(r["pass"] for r in scen)
    gate = rules_ok and pipe_ok and live_ok and scen_ok
    verdict = "Release gate: GREEN" if gate else "Release gate: RED"
    accs = [r["extraction"]["score"] for r in (scen or []) if r.get("extraction")]
    acc_avg = (sum(accs) // len(accs)) if accs else None
    judge_keys = ("warmth", "no_repetition", "collected_what_caller_had", "no_advice", "efficiency")
    jvals = [r["judge"][k] for r in (scen or []) for k in judge_keys if isinstance((r.get("judge") or {}).get(k), int)]
    judge_avg = round(sum(jvals) / len(jvals), 1) if jvals else None
    latency = next((float(s["seconds"]) for s in (live or []) if s["scenario"] == "latency_single_reply"), None)
    first_words = None
    for s in live or []:
        for line in s.get("agent_turns", []):
            pass
    tiles = f"""
    <div class="tiles">
      <div class="tile {'good' if rules_ok else 'bad'}"><div class="k">Rules</div><div class="v">{rules['ran'] - len(rules['failed'])}<small>/{rules['ran']}</small></div><div class="s">no network</div></div>
      <div class="tile {'good' if pipe_ok else 'bad' if pipeline else 'none'}"><div class="k">Notes pipeline</div><div class="v">{sum(c['pass'] for c in pipeline) if pipeline else '–'}<small>/{len(pipeline) if pipeline else '–'}</small></div><div class="s">{e(pipe_name or 'no report')}</div></div>
      <div class="tile {'good' if live_ok else 'bad' if live else 'none'}"><div class="k">Live conversation</div><div class="v">{sum(s['pass'] for s in live) if live else '–'}<small>/{len(live) if live else '–'}</small></div><div class="s">{e(live_name or 'no report')}</div></div>
      <div class="tile {'good' if scen_ok and scen else 'bad' if scen else 'none'}"><div class="k">Scenarios</div><div class="v">{sum(r['pass'] for r in scen) if scen else '–'}<small>/{len(scen) if scen else '–'}</small></div><div class="s">{e(scen_name or 'no report')}</div></div>
      <div class="tile metric"><div class="k">Extraction accuracy</div><div class="v">{acc_avg if acc_avg is not None else '–'}<small>%</small></div><div class="s">tracked, not gated</div></div>
      <div class="tile metric"><div class="k">Judge score</div><div class="v">{judge_avg if judge_avg is not None else '–'}<small>/5</small></div><div class="s">tracked, not gated</div></div>
      <div class="tile {'good' if gate else 'bad'}"><div class="k">Verdict</div><div class="v small">{'GREEN' if gate else 'RED'}</div><div class="s">{'ship it' if gate else 'do not release'}</div></div>
    </div>"""

    if hlth:
        hrows = "".join(f"<tr><th>{e(k)}</th><td>{e(v)}</td></tr>" for k, v in [
            ("API key loaded", hlth.get("has_api_key")), ("Voice model", hlth.get("live_model")), ("Notes / vision model", hlth.get("model")),
            ("Tools offered", ", ".join(hlth.get("tools", []))), ("Scope", hlth.get("scope")), ("Barge-in", hlth.get("barge_in")),
            ("Sketch model", hlth.get("sketch_model")), ("Storage", hlth.get("storage", "memory_only"))])
        health_html = f'<div class="tbl"><table class="kv">{hrows}</table></div>'
    else:
        health_html = f'<p class="muted">No server answered at {e(base)} while this report was built. Start the local server to capture its health.</p>'

    rules_html = "<p class='muted'>All rules passed.</p>" if rules_ok else "<div class='tbl'><table><tr><th>Test</th><th>Failure</th></tr>" + "".join(
        f"<tr><td><code>{e(t)}</code></td><td>{e(m)}</td></tr>" for t, m in rules["failed"]) + "</table></div>"

    def pipe_rows():
        for c in pipeline or []:
            detail = "; ".join(c["failures"]) if c["failures"] else f"route {c.get('route')} · missing {c.get('missing')} · closing {c.get('closing_allowed')}"
            yield f"<tr><td>{pill(c['pass'])}</td><td><code>{e(c['case'])}</code><div class='why'>{e(c['why'])}</div></td><td class='num'>{c['seconds']:.1f} s</td><td>{e(detail)}</td></tr>"
    pipe_html = "<div class='tbl'><table><tr><th></th><th>Case</th><th>Time</th><th>Result</th></tr>" + "".join(pipe_rows()) + "</table></div>" if pipeline else "<p class='muted'>No pipeline report found.</p>"

    def live_rows():
        for s in live or []:
            turns = "".join(f"<li>{e(t)}</li>" for t in s.get("agent_turns", []))
            judge = s.get("judge")
            jhtml = ""
            if judge and "error" not in judge:
                jhtml = "<div class='judge'>" + " ".join(f"<span><b>{e(k)}</b> {e(v)}</span>" for k, v in judge.items() if k not in ("worst_line", "reason")) + (f"<div class='why'>{e(judge.get('reason',''))}</div>" if judge.get("reason") else "") + "</div>"
            fails = "<ul class='fails'>" + "".join(f"<li>{e(f)}</li>" for f in s["failures"]) + "</ul>" if s["failures"] else ""
            yield f"""<details class="scenario {'ok' if s['pass'] else 'bad'}"><summary>{pill(s['pass'])} <code>{e(s['scenario'])}</code> <span class="num">{s['seconds']:.1f} s · {e(s.get('_source',''))}</span><span class="doc">{e((s.get('doc') or '').strip().splitlines()[0] if s.get('doc') else '')}</span></summary>{fails}{jhtml}<ol class="turns">{turns}</ol></details>"""
    def scen_rows():
        for r in scen or []:
            acc = r.get("extraction") or {}
            checks = " ".join(f'<span class="chk {"ok" if v else "bad"}">{e(k)}</span>' for k, v in (acc.get("checks") or {}).items())
            jd = r.get("judge") or {}
            jrow = " ".join(f"<span><b>{e(k.replace('_',' '))}</b> {jd[k]}</span>" for k in judge_keys if isinstance(jd.get(k), int))
            worst = jd.get("worst_moment"); worst_html = f"<div class='why'>Judge's worst moment: {e(worst)}</div>" if worst and str(worst).lower() != "none" else ""
            fails = "<ul class='fails'>" + "".join(f"<li>{e(x)}</li>" for x in r["failures"]) + "</ul>" if r["failures"] else ""
            notes = "".join(f"<div class='why'>{e(n)}</div>" for n in r.get("notes", []))
            convo = "".join(f"<li class='{l['who'].lower()}'><b>{e(l['who'])}</b> {e(l['text'])}</li>" for l in r.get("transcript", []))
            yield f"""<details class="scenario {'ok' if r['pass'] else 'bad'}"><summary>{pill(r['pass'])} <code>{e(r['id'])}</code> <span class="num">{r['seconds']:.0f} s · route {e(r.get('route'))} · extraction {acc.get('score','–')}%</span><span class="doc">{e(r['title'])}: {e(r['tests'])}</span></summary>{fails}{notes}<div class="judge">{jrow}</div>{worst_html}<div class="checks">{checks}</div><ol class="turns convo">{convo}</ol></details>"""
    scen_html = "".join(scen_rows()) if scen else "<p class='muted'>No scenario report found. Run <code>evals/run_scenario_evals.py</code> (about 20 minutes for all twelve).</p>"
    live_html = "".join(live_rows()) if live else "<p class='muted'>No live report found. Start the local server and run <code>evals/run_live_evals.py</code>.</p>"

    return f"""<title>Claim Agent Release Report</title>
<meta name="claude:viewport-fit-sides" content="cover">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap">
<style>
/* Layout: verdict tiles first, then one collapsible block per layer; scenarios expand to their full transcript. */
:root{{--bg:#f6f5f1;--surface:#fff;--ink:#1d2130;--muted:#5f667a;--line:#e2e0d8;--accent:#1f5f8b;--accent-soft:#e3eef6;--ok:#146c43;--ok-soft:#e1f3e8;--bad:#b42318;--bad-soft:#fde8e6;--none-soft:#ebeae4;
--body:"IBM Plex Sans",-apple-system,"Segoe UI",sans-serif;--mono:"IBM Plex Mono",ui-monospace,Menlo,monospace}}
@media (prefers-color-scheme:dark){{:root:not([data-theme="light"]){{--bg:#15171d;--surface:#1d2027;--ink:#e8e9ee;--muted:#a3a8b8;--line:#2f333d;--accent:#7fb3dc;--accent-soft:#1f3140;--ok:#7fd4a1;--ok-soft:#173326;--bad:#f08b82;--bad-soft:#3a1f1d;--none-soft:#262a33;color-scheme:dark}}}}
:root[data-theme="dark"]{{--bg:#15171d;--surface:#1d2027;--ink:#e8e9ee;--muted:#a3a8b8;--line:#2f333d;--accent:#7fb3dc;--accent-soft:#1f3140;--ok:#7fd4a1;--ok-soft:#173326;--bad:#f08b82;--bad-soft:#3a1f1d;--none-soft:#262a33;color-scheme:dark}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font-family:var(--body);line-height:1.5}}
.wrap{{max-width:1040px;margin:0 auto;padding-block:26px 60px;padding-left:max(16px,env(safe-area-inset-left,0px));padding-right:max(16px,env(safe-area-inset-right,0px))}}
.eyebrow{{font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);font-weight:600}}h1{{font-size:clamp(24px,3.5vw,32px);margin:4px 0 6px;font-weight:600;text-wrap:balance}}
.meta{{display:flex;flex-wrap:wrap;gap:6px 18px;font-size:13px;color:var(--muted);margin-bottom:18px}}.meta code{{font-family:var(--mono);color:var(--ink);font-size:12px}}
.verdict{{display:inline-block;font-weight:600;font-size:14px;padding:6px 12px;border-radius:8px;margin-bottom:16px;background:{'var(--ok-soft)' if gate else 'var(--bad-soft)'};color:{'var(--ok)' if gate else 'var(--bad)'}}}
.tiles{{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px;margin-bottom:28px}}
.tile{{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:14px 16px;border-top:4px solid var(--line)}}.tile.good{{border-top-color:var(--ok)}}.tile.bad{{border-top-color:var(--bad)}}.tile.none{{border-top-color:var(--muted)}}
.tile .k{{font-size:12px;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);font-weight:600}}.tile .v{{font-family:var(--mono);font-size:30px;font-weight:500;font-variant-numeric:tabular-nums;line-height:1.2;margin:4px 0}}.tile .v.small{{font-size:22px}}.tile .v small{{font-size:14px;color:var(--muted)}}.tile .s{{font-size:12px;color:var(--muted)}}
h2{{font-size:19px;margin:26px 0 8px;font-weight:600}}.muted{{color:var(--muted);font-size:14px}}
.tbl{{overflow-x:auto;background:var(--surface);border:1px solid var(--line);border-radius:10px}}table{{border-collapse:collapse;width:100%;font-size:13.5px}}th,td{{text-align:left;padding:9px 12px;border-bottom:1px solid var(--line);vertical-align:top}}th{{font-size:12px;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);font-weight:600}}tr:last-child td{{border-bottom:none}}table.kv th{{width:200px;text-transform:none;letter-spacing:0;font-size:13.5px;color:var(--muted)}}
.num{{font-family:var(--mono);font-variant-numeric:tabular-nums;white-space:nowrap;color:var(--muted);font-size:13px}}.why{{font-size:12.5px;color:var(--muted)}}
code{{font-family:var(--mono);font-size:12.5px;background:var(--accent-soft);color:var(--accent);padding:1px 5px;border-radius:4px}}
.pill{{display:inline-block;font-size:11px;font-weight:700;letter-spacing:.04em;padding:2px 8px;border-radius:999px}}.pill.ok{{background:var(--ok-soft);color:var(--ok)}}.pill.bad{{background:var(--bad-soft);color:var(--bad)}}
details.scenario{{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:10px 14px;margin-bottom:8px;border-left:4px solid var(--ok)}}details.scenario.bad{{border-left-color:var(--bad)}}
summary{{cursor:pointer;display:flex;flex-wrap:wrap;gap:10px;align-items:center;font-size:14px}}summary .doc{{flex-basis:100%;color:var(--muted);font-size:12.5px}}
ol.turns{{margin:10px 0 2px;padding-left:22px;font-size:13.5px}}ol.turns li{{margin:4px 0;color:var(--ink)}}ul.fails{{margin:8px 0;padding-left:20px;color:var(--bad);font-size:13.5px}}
.judge{{display:flex;flex-wrap:wrap;gap:12px;font-size:13px;margin:8px 0}}.tile.metric{{border-top-color:var(--accent)}}
.checks{{display:flex;flex-wrap:wrap;gap:6px;margin:6px 0}}.chk{{font-size:11px;font-weight:600;padding:2px 7px;border-radius:999px}}.chk.ok{{background:var(--ok-soft);color:var(--ok)}}.chk.bad{{background:var(--bad-soft);color:var(--bad)}}
ol.convo li.claimant{{color:var(--muted)}}ol.convo li b{{font-weight:600;margin-right:4px}}.judge b{{color:var(--muted);font-weight:600}}
footer{{margin-top:34px;border-top:1px solid var(--line);padding-top:12px;font-size:12.5px;color:var(--muted)}}
</style>
<div class="wrap">
<div class="eyebrow">Auto claim voice agent</div>
<h1>Release report</h1>
<div class="meta"><span>Built {e(now)}</span><span>Revision <code>{e(git_rev())}</code></span><span>Server <code>{e(base)}</code></span></div>
<div class="verdict">{e(verdict)}</div>
{tiles}
<h2>Server health at report time</h2>{health_html}
<h2>Rules</h2><p class="muted">{rules['ran']} deterministic tests, no network.</p>{rules_html}
<h2>Notes pipeline</h2><p class="muted">Transcript fixtures through the real extractor. Each case names the incident it guards.</p>{pipe_html}
<h2>End-to-end scenarios</h2><p class="muted">A Gemini-simulated claimant with a persona and ground truth talks to the real agent. Pass/fail is deterministic (route, closing, checklist coverage, forbidden questions, guards, latency). Extraction accuracy and the judge rubric are tracked metrics. Expand a scenario for the whole conversation.</p>{scen_html}
<h2>Live conversation</h2><p class="muted">Scripted claimant against the real server and Gemini Live. Expand a scenario for the agent's exact lines. Live results vary run to run; re-run a single red scenario before treating it as a regression.</p>{live_html}
<footer>Generated by <code>evals/render_report.py</code> from the newest JSON reports in <code>evals/reports/</code>. Rules are re-run at render time; pipeline and live layers are read from their reports.</footer>
</div>
"""


STYLE_HISTORY = """
.runs th,.runs td{white-space:nowrap}.runs td.story{white-space:normal}
.log h3{font-size:15px;margin:18px 0 4px;font-weight:600}.log p{margin:4px 0;font-size:13.5px;max-width:80ch}.log b{color:var(--muted);font-weight:600}
.kind{font-size:11px;font-weight:700;letter-spacing:.04em;padding:2px 8px;border-radius:999px;background:var(--accent-soft);color:var(--accent)}
"""


def load_reports():
    """Every JSON report, newest first, with the summary numbers the index shows."""
    rows = []
    for f in sorted(REPORTS.glob("*-*.json"), reverse=True):
        kind, _, stamp = f.stem.partition("-")
        try: data = json.loads(f.read_text())
        except Exception: continue
        meta = next((r for r in data if isinstance(r, dict) and r.get("_meta")), {})
        recs = [r for r in data if isinstance(r, dict) and not r.get("_meta")]
        if not recs: continue
        passed = sum(1 for r in recs if r.get("pass")); total = len(recs)
        accs = [r["extraction"]["score"] for r in recs if r.get("extraction")]
        jk = ("warmth", "no_repetition", "collected_what_caller_had", "no_advice", "efficiency")
        jv = [r["judge"][k] for r in recs for k in jk if isinstance((r.get("judge") or {}).get(k), int)]
        when = datetime.datetime.strptime(stamp, "%Y%m%d-%H%M%S").strftime("%Y-%m-%d %H:%M") if len(stamp) == 15 else stamp
        rows.append({"file": f.name, "kind": kind, "when": when, "passed": passed, "total": total, "ids": [r.get("id") or r.get("scenario") or r.get("case") for r in recs],
                     "acc": (sum(accs) // len(accs)) if accs else None, "judge": round(sum(jv) / len(jv), 1) if jv else None, "git": meta.get("git_rev"), "recs": recs})
    return rows


def runlog_html():
    path = ROOT / "evals" / "RUNLOG.md"
    if not path.exists(): return "<p class='muted'>No run log yet.</p>"
    out = []
    for line in path.read_text().splitlines():
        if line.startswith("## "): out.append(f"<h3>{e(line[3:])}</h3>")
        elif line.startswith("# ") or not line.strip(): continue
        else:
            t = e(line); 
            import re as _re
            t = _re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t); t = _re.sub(r"`(.+?)`", r"<code>\1</code>", t)
            out.append(f"<p>{t}</p>")
    return "".join(out)


def run_page(row):
    """A standalone page for one run, reusing the scenario/live renderers."""
    kind = row["kind"]; recs = row["recs"]
    if kind == "scenario": page = render({"ran": 0, "failed": []}, None, None, None, None, recs, row["file"], None, "")
    elif kind == "live": page = render({"ran": 0, "failed": []}, None, None, recs, row["file"], None, None, None, "")
    else: page = render({"ran": 0, "failed": []}, recs, row["file"], None, None, None, None, None, "")
    page = page.replace("<title>Claim Agent Release Report</title>", f"<title>Run {e(row['when'])} {e(kind)}</title>")
    page = page.replace("<h1>Release report</h1>", f"<h1>{e(kind.capitalize())} run · {e(row['when'])}</h1><p class='muted'><a href='index.html'>← all runs</a> · {e(row['file'])}{' · ' + e(row['git']) if row['git'] else ''}</p>")
    # Standalone file: give it a proper document head (the index page gets one from the publisher, these do not).
    return '<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover"></head><body style="margin:0">' + page + "</body></html>"


def build_history():
    import re as _re
    css = _re.search(r"<style>(.*?)</style>", render({"ran": 0, "failed": []}, None, None, None, None, None, None, None, ""), _re.S).group(1)
    rows = load_reports()
    for row in rows:
        (REPORTS / f"run-{row['file'][:-5]}.html").write_text(run_page(row))
    trs = "".join(f"<tr><td class='num'>{e(r['when'])}</td><td><span class='kind'>{e(r['kind'])}</span></td><td>{pill(r['passed'] == r['total'], str(r['passed']) + '/' + str(r['total']))}</td>"
                  f"<td class='num'>{str(r['acc']) + '%' if r['acc'] is not None else '–'}</td><td class='num'>{r['judge'] if r['judge'] is not None else '–'}</td><td class='num'>{e(r['git'] or '–')}</td>"
                  f"<td class='story'><a href='run-{e(r['file'][:-5])}.html'>open</a> · {e(', '.join(x for x in r['ids'] if x)[:90])}</td></tr>" for r in rows)
    page = f"""<title>Claim Agent Eval History</title>
<meta name="claude:viewport-fit-sides" content="cover">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap">
<style>{css}{STYLE_HISTORY}</style>
<div class="wrap">
<div class="eyebrow">Auto claim voice agent</div><h1>Evaluation history</h1>
<p class="muted">Every recorded run, newest first, with the story of what each one found and what changed. Click a run for its full report and transcripts.</p>
<h2>Runs</h2><div class="tbl"><table class="runs"><tr><th>When</th><th>Layer</th><th>Result</th><th>Extraction</th><th>Judge</th><th>Code</th><th>Report</th></tr>{trs}</table></div>
<h2>Run log: findings and fixes</h2><div class="log">{runlog_html()}</div>
<footer>Generated by <code>evals/render_report.py --history</code> from <code>evals/reports/*.json</code> and <code>evals/RUNLOG.md</code>.</footer>
</div>"""
    (REPORTS / "index.html").write_text(page)
    return len(rows)


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--base", default="http://127.0.0.1:4177"); ap.add_argument("--out", default=str(REPORTS / "report.html"))
    ap.add_argument("--history", action="store_true", help="also rebuild evals/reports/index.html and one page per run")
    a = ap.parse_args()
    if a.history:
        n = build_history(); print(f"history: {REPORTS / 'index.html'} ({n} runs)")
    pipeline, pn = latest("pipeline", "case"); live, ln = latest("live", "scenario"); scen, sn = latest("scenario", "id")
    page = render(run_rules(), pipeline, pn, live, ln, scen, sn, health(a.base), a.base)
    out = pathlib.Path(a.out); out.parent.mkdir(exist_ok=True); out.write_text(page)
    print(f"report: {out}")


if __name__ == "__main__":
    main()
