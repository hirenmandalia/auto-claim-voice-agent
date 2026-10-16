// Mobile app shell for the Insurance Claim Live Agent Team demo.
// Same backend protocol as ../app.js, wrapped in the insurer app's screens.

const $ = (sel) => document.querySelector(sel);
const screens = [...document.querySelectorAll("[data-screen]")];
const claimSheet = $("#claimSheet");
const callStatus = $("#callStatus");
const progressBar = $("#progressBar");
const agentBubble = $("#agentBubble");
const youBubble = $("#youBubble");
const orb = $("#orb");
const notesEl = $("#notes");
const stampEl = $("#stamp");
const evidenceWrap = $("#evidenceWrap");
const evidenceEl = $("#evidence");
const neededWrap = $("#neededWrap");
const neededEl = $("#needed");
const transcriptEl = $("#transcript");
const micButton = $("#micButton");
const cameraButton = $("#cameraButton");
const keyboardButton = $("#keyboardButton");
const cameraStage = $("#cameraStage");
const cameraPreview = $("#cameraPreview");
const frameCanvas = $("#frameCanvas");
const textForm = $("#textForm");
const textInput = $("#textInput");
const reviewBtn = $("#reviewBtn");
const claimScroll = $("#claimScroll");
const notesPill = $("#notesPill");

// The signed-in account. In the real app this comes from the session; here it matches the mock policy directory.
const ACCOUNT = {
  policyholder_name: "Jordan Lee",
  policy_number: "AUTO-90210",
  vehicle: "2013 Toyota Sienna",
};

const API_ORIGIN = window.location.origin;
const WS_ORIGIN = API_ORIGIN.replace(/^http/, "ws");
const FRAME_INTERVAL_MS = 1000;
const FRAME_WIDTH = 512;
// Phone mic hygiene: cancel the agent's own voice from the speaker, suppress room noise.
const MIC_CONSTRAINTS = { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 };
// Client-side barge-in gate: while the agent is speaking, only speech clearly louder than the
// room gets through; everything else is sent as silence so the stream stays continuous.
const BARGE_RMS = 0.08;          // loud, close speech only (the phone speaker rarely reaches this after echo cancellation)
const BARGE_HOLD_CHUNKS = 5;     // ~430 ms of sustained sound before it counts as an interruption
const NOISE_FLOOR_RMS = 0.006;   // below this, send silence even when the agent is quiet
const ECHO_TAIL_MS = 700;        // keep the mic muted briefly after the agent stops: speaker echo trails the audio
let loudChunks = 0;
let lastPlaybackEnd = 0;
let reportedSpeaking = false;
let lastActivitySent = 0;
function reportActivity(speaking) {
  // Tell the server the moment the phone hears the claimant, so a quiet-check never fires over their words.
  if (!liveSocket || liveSocket.readyState !== WebSocket.OPEN) return;
  const now = performance.now();
  if (speaking === reportedSpeaking && now - lastActivitySent < 1000) return;
  if (speaking !== reportedSpeaking || speaking) {
    reportedSpeaking = speaking; lastActivitySent = now;
    liveSocket.send(JSON.stringify({ type: "activity", speaking }));
  }
}
function reportPlayback(playing) {
  if (!liveSocket || liveSocket.readyState !== WebSocket.OPEN) return;
  liveSocket.send(JSON.stringify({ type: "playback", playing }));
}
let bargeInAllowed = false;      // mirrors the server's FNOL_BARGE_IN; fetched from /api/health

const routeLabels = {
  emergency_escalation: ["Escalated to a person", "danger"],
  policy_review: ["Policy review", "warning"],
  needs_docs: ["In progress", "warning"],
  special_investigation: ["Under review", "info"],
  ready_for_adjuster: ["Ready for adjuster", "success"],
  out_of_scope: ["Auto claims only", "danger"],
};
const blockerQuestions = {
  policyholder_name: "Your name",
  policy_number: "Policy number",
  contact_method: "Phone or email?",
  date_of_loss: "When it happened",
  loss_location: "Where it happened",
  loss_description: "What happened",
};
const emptyState = {
  route: "needs_docs", progress: 0, fields: {}, transcript: [], events: [], tool_activity: [],
  missing_blockers: [], documents: [], evidence_photos: [], camera_notes: [], sketch: null, policy: null,
  handoff: {}, packet_markdown: "",
};

let liveSocket = null;
let connectionPromise = null;
let generation = 0;
let processing = false;
let micPending = false;
let cameraPending = false;
let resetting = false;
const playbackSources = new Set();
let audioContext = null;
let inputProcessor = null;
let inputSource = null;
let audioStream = null;
let cameraStream = null;
let frameTimer = null;
let isRecording = false;
let nextPlaybackTime = 0;
let sessionId = null;
let state = null;
let autoStartVoice = false;

/* ---------- navigation ---------- */
function go(name) {
  for (const el of screens) el.classList.toggle("active", el.dataset.screen === name);
  window.scrollTo(0, 0);
}
document.addEventListener("click", (event) => {
  const target = event.target.closest("[data-go]");
  if (target) go(target.dataset.go);
});
for (const el of document.querySelectorAll("[data-open-claim]")) el.addEventListener("click", () => { claimSheet.hidden = false; });
$("#closeSheet").addEventListener("click", () => { claimSheet.hidden = true; });
claimSheet.addEventListener("click", (event) => { if (event.target === claimSheet) claimSheet.hidden = true; });
$("#startVoiceClaim").addEventListener("click", () => beginClaim(true));
$("#startTextClaim").addEventListener("click", () => beginClaim(false));
$("#endClaim").addEventListener("click", () => {
  if (claimantHasSpoken() && !confirm("Leave this claim? Your notes are kept for 30 minutes.")) return;
  disconnectLive();
  go("home");
});
$("#doneHome").addEventListener("click", () => go("home"));
reviewBtn.addEventListener("click", finishClaim);

async function beginClaim(withVoice) {
  claimSheet.hidden = true;
  autoStartVoice = withVoice;
  textForm.hidden = withVoice;
  keyboardButton.classList.toggle("on", !withVoice);
  go("claim");
  // Unlock audio on this tap so the first agent reply can play on iOS.
  await unlockAudio();
  await createSession();
  // Connect right away in both modes so the agent's spoken greeting plays as the screen opens.
  if (withVoice) startLiveVoice();
  else {
    connectLive().catch((error) => appendSystem(error.message));
    textInput.focus();
  }
}

/* ---------- helpers ---------- */
function escapeHtml(value) {
  return String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#039;");
}
function isFilled(field) {
  if (!field || field.status === "missing") return false;
  return !/^(missing:|not captured|not specified|unknown|none)/i.test(String(field.value || "").trim());
}
function shortBlocker(blocker) {
  if (blockerQuestions[blocker]) return blockerQuestions[blocker];
  if (/contact/i.test(String(blocker))) return blockerQuestions.contact_method;
  let text = String(blocker).replace(/\s*\([^)]*\)/g, "").replace(/\?$/, "").trim();
  return text.length > 40 ? `${text.slice(0, 38).trim()}…` : text;
}
function claimantHasSpoken() {
  return (state?.transcript || []).some((turn) => turn.speaker === "Claimant" && String(turn.text || "").trim());
}
function setStatus(text, tone = "") {
  callStatus.textContent = text;
  callStatus.className = `status ${tone}`.trim();
}
function setState(nextState) {
  const previous = state || emptyState;
  state = { ...emptyState, ...nextState, tool_activity: nextState.tool_activity ?? previous.tool_activity };
  render();
}

/* ---------- rendering ---------- */
function render() {
  renderBubbles();
  renderNotes();
  renderEvidence();
  renderNeeded();
  renderStamp();
  renderTranscript();
  const progress = Number(state.progress || 0);
  progressBar.style.width = `${Math.max(4, progress)}%`;
  reviewBtn.hidden = state.route === "out_of_scope" || !(state.closed || state.closing_allowed || state.route === "ready_for_adjuster" || progress >= 80);
  const busy = processing || (state.tool_activity || []).some((item) => item.phase === "running");
  orb.classList.toggle("thinking", busy && !orb.classList.contains("speaking"));
  setTakingNotes(busy && !orb.classList.contains("speaking") && claimantHasSpoken());
  $("#downloadPacket").href = sessionId ? `${API_ORIGIN}/api/sessions/${sessionId}/packet` : "#";
}

function renderBubbles() {
  const turns = state.transcript || [];
  const lastAgent = [...turns].reverse().find((t) => t.speaker === "Agent" && String(t.text).trim());
  const lastYou = [...turns].reverse().find((t) => t.speaker === "Claimant" && String(t.text).trim());
  const lastSystem = turns.length && turns[turns.length - 1].speaker === "System" ? turns[turns.length - 1] : null;
  if (lastSystem) agentBubble.textContent = lastSystem.text;
  else if (lastAgent) agentBubble.textContent = lastAgent.text;
  else agentBubble.textContent = "Connecting you to Wrenfield Claims…";
  const youIsLatest = lastYou && turns.indexOf(lastYou) > turns.indexOf(lastAgent);
  youBubble.hidden = !youIsLatest;
  if (youIsLatest) youBubble.textContent = lastYou.text;
}

function buildNotes() {
  const f = state.fields || {};
  const notes = [];
  const record = state.policy;
  if (record) {
    if (record.found) {
      const active = record.status === "active";
      notes.push({ cls: active ? "" : "flag", text: active ? `${record.policy_line} policy ${record.policy_number} verified` : `${record.policy_line}, ${record.status}. Needs a person to review.` });
    } else {
      notes.push({ cls: "flag", text: `Policy ${record.policy_number || ""} not found` });
    }
  }
  const when = isFilled(f.date) ? f.date.value : "";
  const where = isFilled(f.location) ? f.location.value : "";
  if (when) notes.push({ text: `When: ${when}` });
  if (where) notes.push({ text: `Where: ${where}` });
  if (isFilled(f.description)) notes.push({ text: f.description.value });
  if (isFilled(f.injuries)) notes.push({ cls: f.injuries.status === "urgent" ? "flag" : "", text: f.injuries.status === "urgent" ? `Injury: ${f.injuries.value}` : f.injuries.value });
  if (isFilled(f.contact)) notes.push({ text: `Reach at ${f.contact.value}` });
  if (isFilled(f.photos)) notes.push({ text: `Has: ${f.photos.value}` });
  if (isFilled(f.police)) notes.push({ text: `Report: ${f.police.value}` });
  if (claimantHasSpoken()) {
    const seen = new Set();
    for (const blocker of state.missing_blockers || []) {
      const q = shortBlocker(blocker);
      const key = q.toLowerCase().replace(/[^a-z]/g, "");
      if (seen.has(key) || seen.size >= 3) continue;
      seen.add(key);
      notes.push({ cls: "blank", text: q });
    }
  }
  if (!notes.length) notes.push({ cls: "empty", text: "Your claim fills in here as you talk." });
  return notes;
}
function renderNotes() {
  const html = buildNotes().map((n) => `<li class="${n.cls || ""}">${escapeHtml(n.text)}</li>`).join("");
  if (notesEl.innerHTML !== html) notesEl.innerHTML = html;
}

function renderEvidence() {
  const photos = state.evidence_photos || [];
  const sketch = state.sketch;
  const cards = photos.map((p) => `
    <figure data-key="${escapeHtml(p.id)}">
      <img src="${p.data_url}" alt="Evidence photo" />
      <figcaption>${escapeHtml(p.caption)}</figcaption>
      <span class="tag ${p.confirmed ? "" : "unconfirmed"}">${p.lighting === "poor" ? "Too dark, retake" : p.confirmed ? "Confirmed" : "Needs a closer look"}</span>
    </figure>`);
  if (sketch) cards.push(`
    <figure data-key="sketch-${sketch.version}">
      <img src="${sketch.data_url}" alt="Sketch of the incident" />
      <figcaption>Sketch from your description</figcaption>
      <span class="tag sketch">Illustration</span>
    </figure>`);
  const currentKeys = [...evidenceEl.querySelectorAll("[data-key]")].map((el) => el.dataset.key).join("|");
  const nextKeys = [...photos.map((p) => p.id), sketch ? `sketch-${sketch.version}` : ""].filter(Boolean).join("|");
  if (currentKeys !== nextKeys) evidenceEl.innerHTML = cards.join("");
  evidenceWrap.hidden = !cards.length;
}

function renderNeeded() {
  const items = [];
  for (const blocker of state.missing_blockers || []) items.push({ text: shortBlocker(blocker), cls: "" });
  for (const doc of state.documents || []) items.push({ text: doc.item, cls: doc.already_provided ? "done" : "" });
  const seen = new Set();
  const unique = items.filter((i) => { const k = i.text.toLowerCase(); if (seen.has(k)) return false; seen.add(k); return true; });
  neededEl.innerHTML = unique.map((i) => `<span class="${i.cls}">${escapeHtml(i.text)}</span>`).join("");
  neededWrap.hidden = !items.length || !claimantHasSpoken();
}

function renderStamp() {
  const [label, tone] = routeLabels[state.route] || [state.route, "warning"];
  stampEl.hidden = !claimantHasSpoken();
  stampEl.textContent = label;
  if (stampEl.dataset.route !== state.route) {
    stampEl.dataset.route = state.route;
    stampEl.className = `stamp ${tone}`;
  }
}

function renderTranscript() {
  transcriptEl.innerHTML = (state.transcript || []).map((turn) => {
    const cls = turn.speaker === "Agent" ? "agent" : turn.speaker === "System" ? "system" : "claimant";
    const who = turn.speaker === "Agent" ? "Agent" : turn.speaker === "System" ? "Note" : "You";
    return `<div class="turn ${cls}"><span class="who">${who}</span>${escapeHtml(turn.text)}</div>`;
  }).join("");
}

function appendSystem(text) {
  setState({ ...state, transcript: [...(state.transcript || []), { id: crypto.randomUUID(), speaker: "System", text }] });
}

/* ---------- state merge (same as desktop demo) ---------- */
function mergeTranscript(authoritative, local) {
  const byId = new Map((authoritative || []).map((t) => [t.id, t]));
  for (const t of local || []) if (!byId.has(t.id)) byId.set(t.id, t);
  return [...byId.values()];
}
function mergeToolActivity(authoritative, local) {
  const finished = new Set(["done", "error", "cancelled"]);
  const byId = new Map();
  for (const item of authoritative || []) byId.set(item.id, item);
  for (const item of local || []) {
    const current = byId.get(item.id);
    if (!current || (finished.has(item.phase) && !finished.has(current.phase))) byId.set(item.id, item);
  }
  return [...byId.values()];
}
function applyServerState(nextState) {
  if (nextState.session_id !== sessionId) return;
  setState({ ...nextState, transcript: mergeTranscript(nextState.transcript, state?.transcript), tool_activity: mergeToolActivity(nextState.tool_activity, state?.tool_activity) });
}
function applyToolEvent(message) {
  const { type, ...entry } = message;
  setState({ ...state, tool_activity: [...(state.tool_activity || []).filter((i) => i.id !== entry.id), entry] });
}
function upsertStreamingTurn(speaker, text, final = false, id = crypto.randomUUID()) {
  if (!String(text || "").trim()) return;
  const transcript = [...(state.transcript || [])];
  const index = transcript.findIndex((t) => t.id === id);
  const turn = { id, speaker, text, streaming: !final };
  if (index >= 0) transcript[index] = turn; else transcript.push(turn);
  setState({ ...state, transcript });
}

/* ---------- session + socket ---------- */
async function api(path, options = {}) {
  const response = await fetch(`${API_ORIGIN}${path}`, { headers: { "Content-Type": "application/json" }, ...options });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.detail || `Request failed with status ${response.status}`);
  return payload;
}

async function createSession() {
  if (resetting) return;
  resetting = true;
  const previous = sessionId || sessionStorage.getItem("claimSession");
  disconnectLive();
  const epoch = generation;
  setStatus("Connecting…");
  sessionId = null;
  setState(emptyState);
  try {
    if (previous) await api(`/api/sessions/${previous}`, { method: "DELETE" }).catch(() => {});
    const payload = await api("/api/sessions", { method: "POST", body: JSON.stringify({ app_context: ACCOUNT }) });
    if (epoch !== generation) return;
    sessionId = payload.session_id;
    sessionStorage.setItem("claimSession", sessionId);
    setState(payload.state);
    api("/api/health").then((h) => { bargeInAllowed = Boolean(h.barge_in); }).catch(() => {});
    setStatus(payload.has_api_key ? "Ready" : "Service unavailable", payload.has_api_key ? "" : "danger");
  } catch (error) {
    setStatus("Can't reach Wrenfield Claims", "danger");
    appendSystem(error.message);
  } finally {
    resetting = false;
  }
}

function connectLive() {
  if (connectionPromise) return connectionPromise;
  if (!sessionId || resetting) return Promise.reject(new Error("Still connecting, one moment."));
  const epoch = generation;
  const socket = new WebSocket(`${WS_ORIGIN}/ws/live?session_id=${encodeURIComponent(sessionId)}`);
  liveSocket = socket;
  connectionPromise = new Promise((resolve, reject) => {
    let ready = false;
    const timeout = setTimeout(() => { reject(new Error("Connection timed out.")); socket.close(); }, 20000);
    const current = () => epoch === generation && liveSocket === socket;
    socket.onmessage = (event) => {
      if (!current()) return;
      const message = JSON.parse(event.data);
      if (message.session_id !== sessionId) return;
      if (message.type === "ready") { ready = true; clearTimeout(timeout); setStatus("Live", "live"); resolve(); }
      else if (message.type === "processing") { processing = message.active; render(); }
      else if (message.type === "transcript") upsertStreamingTurn(message.speaker, message.text, message.final, message.id);
      else if (message.type === "tool") applyToolEvent(message);
      else if (message.type === "audio") playPcm24(message.data);
      else if (message.type === "state") applyServerState(message.state);
      else if (message.type === "interrupted") stopPlayback();
      else if (message.type === "lighting") setLighting(message.dark, message.issue);
      else if (message.type === "camera_request") requestCamera();
      else if (message.type === "error") {
        processing = false;
        appendSystem(message.message);
        if (!ready) { reject(new Error(message.message)); socket.close(); }
      }
    };
    socket.onerror = () => { clearTimeout(timeout); reject(new Error("Connection failed.")); socket.close(); };
    socket.onclose = () => {
      clearTimeout(timeout);
      if (!ready) reject(new Error("Connection ended before it was ready."));
      if (!current()) return;
      liveSocket = null; connectionPromise = null;
      stopLiveVoice(false); stopCamera(); stopPlayback(); clearBusy();
      setStatus("Disconnected. Tap Talk to reconnect.", "warn");
    };
  });
  return connectionPromise;
}

function clearBusy() {
  processing = false;
  if (!state) return;
  state.tool_activity = (state.tool_activity || []).map((i) => i.phase === "running" ? { ...i, phase: "cancelled" } : i);
  render();
}
function disconnectLive() {
  generation += 1;
  const socket = liveSocket;
  liveSocket = null; connectionPromise = null;
  stopLiveVoice(false); stopCamera(); stopPlayback();
  if (socket) socket.close();
  if (state) clearBusy();
}

/* ---------- "taking notes" cue: soft keystrokes while the claim notes are being written ---------- */
let typingTimer = null;
let typingNoise = null;
function keystroke() {
  if (!audioContext || audioContext.state !== "running") return;
  if (!typingNoise) {
    const len = Math.floor(audioContext.sampleRate * 0.03);
    typingNoise = audioContext.createBuffer(1, len, audioContext.sampleRate);
    const d = typingNoise.getChannelData(0);
    for (let i = 0; i < len; i += 1) d[i] = (Math.random() * 2 - 1) * Math.pow(1 - i / len, 3);
  }
  const src = audioContext.createBufferSource();
  src.buffer = typingNoise;
  const filter = audioContext.createBiquadFilter();
  filter.type = "bandpass"; filter.frequency.value = 1800 + Math.random() * 1400; filter.Q.value = 1.2;
  const gain = audioContext.createGain();
  gain.gain.value = 0.035 + Math.random() * 0.02;
  src.connect(filter); filter.connect(gain); gain.connect(audioContext.destination);
  src.start();
}
function setTakingNotes(active) {
  notesPill.hidden = !active;
  if (active && !typingTimer) {
    const tick = () => { keystroke(); typingTimer = setTimeout(tick, 70 + Math.random() * 140 + (Math.random() < 0.15 ? 400 : 0)); };
    typingTimer = setTimeout(tick, 200);
  } else if (!active && typingTimer) {
    clearTimeout(typingTimer); typingTimer = null;
  }
}

/* ---------- audio ---------- */
async function unlockAudio() {
  if (!window.AudioContext && !window.webkitAudioContext) return;
  audioContext = audioContext || new (window.AudioContext || window.webkitAudioContext)();
  try { await audioContext.resume(); } catch (_) {}
}
function stopPlayback() {
  for (const source of playbackSources) { try { source.stop(); } catch (_) {} }
  playbackSources.clear();
  nextPlaybackTime = audioContext?.currentTime || 0;
  lastPlaybackEnd = performance.now();
  if (orb.classList.contains("speaking")) reportPlayback(false);
  orb.classList.remove("speaking");
}
function playPcm24(base64) {
  audioContext = audioContext || new (window.AudioContext || window.webkitAudioContext)();
  const binary = atob(base64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
  const pcm = new Int16Array(bytes.buffer);
  const audioBuffer = audioContext.createBuffer(1, pcm.length, 24000);
  const channel = audioBuffer.getChannelData(0);
  for (let i = 0; i < pcm.length; i += 1) channel[i] = pcm[i] / 32768;
  const source = audioContext.createBufferSource();
  source.buffer = audioBuffer;
  source.connect(audioContext.destination);
  const startAt = Math.max(audioContext.currentTime, nextPlaybackTime);
  playbackSources.add(source);
  source.onended = () => { playbackSources.delete(source); if (!playbackSources.size) { orb.classList.remove("speaking"); lastPlaybackEnd = performance.now(); reportPlayback(false); } };
  source.start(startAt);
  nextPlaybackTime = startAt + audioBuffer.duration;
  if (!orb.classList.contains("speaking")) reportPlayback(true);
  orb.classList.add("speaking");
  orb.classList.remove("thinking");
  setTakingNotes(false);
}

async function sendClaimantTurn(text) {
  const epoch = generation;
  try {
    await unlockAudio();
    await connectLive();
    if (epoch !== generation) return;
    const id = crypto.randomUUID();
    liveSocket.send(JSON.stringify({ type: "text", text, id }));
    upsertStreamingTurn("Claimant", text, true, id);
    processing = true;
    render();
  } catch (error) {
    if (epoch !== generation) return;
    textInput.value = text;
    appendSystem(`${error.message} Your message is back in the box; send it again to retry.`);
  }
}

function wireMic(stream) {
  audioStream = stream;
  inputSource = audioContext.createMediaStreamSource(stream);
  inputProcessor = audioContext.createScriptProcessor(4096, 1, 1);
  inputProcessor.onaudioprocess = (event) => {
    event.outputBuffer.getChannelData(0).fill(0);
    if (!liveSocket || liveSocket.readyState !== WebSocket.OPEN) return;
    const samples = event.inputBuffer.getChannelData(0);
    const rms = rmsOf(samples);
    const agentSpeaking = playbackSources.size > 0 || (performance.now() - lastPlaybackEnd) < ECHO_TAIL_MS;
    let passThrough;
    if (agentSpeaking && !bargeInAllowed) {
      loudChunks = 0;
      passThrough = false;        // half-duplex: the agent finishes, then the claimant talks
    } else if (agentSpeaking) {
      loudChunks = rms >= BARGE_RMS ? loudChunks + 1 : 0;
      passThrough = loudChunks >= BARGE_HOLD_CHUNKS;
    } else {
      loudChunks = 0;
      passThrough = rms >= NOISE_FLOOR_RMS;
    }
    const pcm16 = passThrough ? resampleToPcm16(samples, audioContext.sampleRate, 16000) : silencePcm16(samples.length, audioContext.sampleRate, 16000);
    liveSocket.send(JSON.stringify({ type: "audio", data: arrayBufferToBase64(pcm16.buffer) }));
    reportActivity(passThrough && rms >= BARGE_RMS * 0.5);
  };
  inputSource.connect(inputProcessor);
  inputProcessor.connect(audioContext.destination);
}

async function startLiveVoice() {
  if (micPending || isRecording) return;
  const epoch = generation;
  if (!navigator.mediaDevices?.getUserMedia || !(window.AudioContext || window.webkitAudioContext)) {
    appendSystem("This browser can't use the microphone here. Tap Type instead.");
    return;
  }
  try {
    micPending = true;
    setStatus("Connecting…");
    await unlockAudio();
    await connectLive();
    const stream = await navigator.mediaDevices.getUserMedia({ audio: MIC_CONSTRAINTS });
    if (epoch !== generation || !liveSocket || liveSocket.readyState !== WebSocket.OPEN) { stream.getTracks().forEach((t) => t.stop()); return; }
    await audioContext.resume();
    wireMic(stream);
    isRecording = true;
    micButton.classList.add("on");
    micButton.querySelector(".ctl-label").textContent = "Listening";
    setStatus("Live · listening", "live");
  } catch (error) {
    const denied = error.name === "NotAllowedError" || /denied|permission/i.test(error.message);
    appendSystem(denied ? "Microphone access was turned off. Allow it in Settings, or tap Type instead." : `Voice failed: ${error.message}`);
    setStatus("Microphone unavailable", "warn");
    stopLiveVoice(false);
  } finally { micPending = false; }
}

function stopLiveVoice(closeSocket = true) {
  isRecording = false;
  micButton.classList.remove("on");
  micButton.querySelector(".ctl-label").textContent = "Talk";
  inputProcessor?.disconnect();
  inputSource?.disconnect();
  audioStream?.getTracks().forEach((t) => t.stop());
  inputProcessor = null; inputSource = null; audioStream = null;
  if (closeSocket) {
    disconnectLive();
    setStatus("Paused. Tap Talk to continue.");
  }
}

/* ---------- camera ---------- */
async function requestCamera() {
  // The agent asked the claimant to show something. Try to open the camera; if the browser wants a tap
  // (iOS Safari does), make the Camera button impossible to miss instead.
  if (cameraStream || cameraPending) return;
  try {
    await startCamera();
    if (!cameraStream) throw new Error("camera did not start");
  } catch (_) {
    cameraButton.classList.add("wanted");
    cameraButton.querySelector(".ctl-label").textContent = "Tap to open";
  }
}
function setLighting(dark, issue) {
  const tag = cameraStage.querySelector(".camera-tag");
  tag.classList.toggle("dark", Boolean(dark));
  const hint = issue ? issue.replace(/^too dark:\s*/i, "Too dark. ") : "Too dark. Add light or use the flashlight";
  tag.innerHTML = dark ? `<span class="rec-dot"></span> ${escapeHtml(hint)}` : '<span class="rec-dot"></span> Agent can see this';
}
async function startCamera() {
  if (cameraPending || cameraStream) return;
  const epoch = generation;
  if (!navigator.mediaDevices?.getUserMedia) { appendSystem("This browser can't use the camera here."); return; }
  try {
    cameraPending = true;
    await unlockAudio();
    await connectLive();
    // iOS Safari keeps only one capture stream alive: request audio and video together when the mic is on.
    const wantAudio = isRecording;
    const stream = await navigator.mediaDevices.getUserMedia({ audio: wantAudio ? MIC_CONSTRAINTS : false, video: { width: { ideal: 640 }, height: { ideal: 480 }, facingMode: "environment" } });
    if (epoch !== generation || !liveSocket || liveSocket.readyState !== WebSocket.OPEN) { stream.getTracks().forEach((t) => t.stop()); return; }
    if (wantAudio && stream.getAudioTracks().length) {
      inputProcessor?.disconnect(); inputSource?.disconnect();
      audioStream?.getTracks().forEach((t) => t.stop());
      wireMic(new MediaStream(stream.getAudioTracks()));
    }
    cameraStream = new MediaStream(stream.getVideoTracks());
    cameraStream.getVideoTracks().forEach((t) => t.addEventListener("ended", stopCamera, { once: true }));
    liveSocket.send(JSON.stringify({ type: "camera_state", enabled: true }));
    cameraPreview.srcObject = cameraStream;
    cameraStage.hidden = false;
    cameraButton.classList.remove("wanted");
    cameraButton.classList.add("on");
    cameraButton.querySelector(".ctl-label").textContent = "Stop";
    frameTimer = window.setInterval(sendFrame, FRAME_INTERVAL_MS);
    claimScroll.scrollTo({ top: 0, behavior: "smooth" });
    if (!isRecording) setStatus("Camera on", "live");
  } catch (error) {
    const denied = error.name === "NotAllowedError" || /denied|permission/i.test(error.message);
    if (denied) appendSystem("Camera access was turned off. Allow it in Settings to show the damage.");
    stopCamera();
    throw error;
  } finally { cameraPending = false; }
}

function stopCamera() {
  const wasOn = Boolean(cameraStream);
  if (frameTimer) window.clearInterval(frameTimer);
  frameTimer = null;
  cameraStream?.getTracks().forEach((t) => t.stop());
  cameraStream = null;
  cameraPreview.srcObject = null;
  cameraStage.hidden = true;
  setLighting(false);
  cameraButton.classList.remove("on");
  cameraButton.querySelector(".ctl-label").textContent = "Camera";
  if (wasOn && liveSocket?.readyState === WebSocket.OPEN) {
    liveSocket.send(JSON.stringify({ type: "camera_state", enabled: false }));
    if (!isRecording) setStatus("Live", "live");
  }
}

function sendFrame() {
  if (!cameraStream || !liveSocket || liveSocket.readyState !== WebSocket.OPEN) return;
  if (!cameraPreview.videoWidth) return;
  const scale = FRAME_WIDTH / cameraPreview.videoWidth;
  frameCanvas.width = FRAME_WIDTH;
  frameCanvas.height = Math.round(cameraPreview.videoHeight * scale);
  frameCanvas.getContext("2d").drawImage(cameraPreview, 0, 0, frameCanvas.width, frameCanvas.height);
  liveSocket.send(JSON.stringify({ type: "video", data: frameCanvas.toDataURL("image/jpeg", 0.6).split(",")[1] }));
}

function rmsOf(samples) {
  let sum = 0;
  for (let i = 0; i < samples.length; i += 1) sum += samples[i] * samples[i];
  return Math.sqrt(sum / (samples.length || 1));
}
function silencePcm16(length, inputRate, outputRate) {
  return new Int16Array(Math.floor(length / (inputRate / outputRate)));
}
function resampleToPcm16(input, inputRate, outputRate) {
  const ratio = inputRate / outputRate;
  const outputLength = Math.floor(input.length / ratio);
  const pcm = new Int16Array(outputLength);
  for (let i = 0; i < outputLength; i += 1) {
    const index = i * ratio;
    const before = Math.floor(index);
    const after = Math.min(before + 1, input.length - 1);
    const weight = index - before;
    const sample = Math.max(-1, Math.min(1, input[before] * (1 - weight) + input[after] * weight));
    pcm[i] = sample < 0 ? sample * 0x8000 : sample * 0x7fff;
  }
  return pcm;
}
function arrayBufferToBase64(buffer) {
  let binary = "";
  const bytes = new Uint8Array(buffer);
  for (let i = 0; i < bytes.byteLength; i += 1) binary += String.fromCharCode(bytes[i]);
  return btoa(binary);
}

/* ---------- finish ---------- */
function finishClaim() {
  const f = state.fields || {};
  const rows = [];
  const add = (label, value) => { if (value) rows.push(`<div class="li"><span>${label}</span><strong>${escapeHtml(value)}</strong></div>`); };
  add("Policy", ACCOUNT.policy_number);
  add("Vehicle", ACCOUNT.vehicle);
  add("What happened", isFilled(f.description) ? f.description.value : "");
  add("When", isFilled(f.date) ? f.date.value : "");
  add("Where", isFilled(f.location) ? f.location.value : "");
  add("Injuries", isFilled(f.injuries) ? f.injuries.value : "");
  const photos = (state.evidence_photos || []).length;
  add("Photos", photos ? `${photos} pinned` : "");
  add("Status", (routeLabels[state.route] || [state.route])[0]);
  $("#doneSummary").innerHTML = rows.join("");
  $("#claimNumber").textContent = `CLM-${String(sessionId || "").slice(0, 6).toUpperCase() || "000000"}`;
  stopLiveVoice(false);
  stopCamera();
  if (liveSocket?.readyState === WebSocket.OPEN) liveSocket.send(JSON.stringify({ type: "close" }));
  go("done");
}

/* ---------- wiring ---------- */
micButton.addEventListener("click", () => {
  if (isRecording) { stopLiveVoice(false); setStatus("Paused. Tap Talk to continue."); }
  else startLiveVoice();
});
cameraButton.addEventListener("click", () => (cameraStream ? stopCamera() : startCamera().catch(() => {})));
keyboardButton.addEventListener("click", () => {
  textForm.hidden = !textForm.hidden;
  keyboardButton.classList.toggle("on", !textForm.hidden);
  if (!textForm.hidden) textInput.focus();
});
textForm.addEventListener("submit", (event) => {
  event.preventDefault();
  const value = textInput.value.trim();
  if (!value) return;
  textInput.value = "";
  sendClaimantTurn(value);
});
textInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter") { event.preventDefault(); textForm.requestSubmit(); }
});
window.addEventListener("pagehide", disconnectLive);
