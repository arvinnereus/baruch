/* LocalFellow MVP frontend */
const $ = (s) => document.querySelector(s);
const api = async (path, opts = {}) => {
  const r = await fetch("/api" + path, {
    headers: { "Content-Type": "application/json" }, ...opts,
  });
  if (!r.ok) throw new Error((await r.json()).error || r.statusText);
  return r.json();
};

let current = null;      // meeting id
let detail = null;       // full detail payload
let pollTimer = null, recTimer = null;

/* ---------- generic helpers ---------- */
// async-safe button: disables while the handler runs (no double-fires)
const busy = (btn, fn) => async (...a) => {
  if (btn.disabled) return;
  btn.disabled = true;
  try { await fn(...a); } finally { btn.disabled = false; }
};
const setLbl = (btn, text) => {
  const l = btn.querySelector(".lbl");
  if (l) l.textContent = text; else btn.textContent = text;
};

// modal: confirm, single-input, or dropdown-choice prompt
function modal({ title, text = "", input = false, placeholder = "", value = "",
                 okLabel = "OK", select = null }) {
  return new Promise((resolve) => {
    const dlg = $("#modal"), inp = $("#modal-input"), sel = $("#modal-select");
    $("#modal-title").textContent = title;
    $("#modal-text").textContent = text;
    $("#modal-ok").textContent = okLabel;
    inp.hidden = !input;
    inp.placeholder = placeholder;
    inp.value = value;
    sel.hidden = !select;
    if (select) {
      sel.innerHTML = select.map(o =>
        `<option value="${esc(o.id)}">${esc(o.name)}</option>`).join("");
      if (value) sel.value = value;
    }
    dlg.returnValue = "";
    dlg.showModal();
    if (input) inp.focus();
    dlg.addEventListener("close", function h() {
      dlg.removeEventListener("close", h);
      if (dlg.returnValue !== "ok") return resolve(null);
      resolve(select ? sel.value : input ? inp.value.trim() : true);
    });
  });
}

/* ---------- sidebar ---------- */
async function refreshList() {
  let meetings;
  try {
    meetings = await api("/meetings");
    $("#offline").hidden = true;
  } catch {
    // server unreachable: show it honestly instead of freezing stale UI
    $("#offline").hidden = false;
    updatePill([]);
    $("#cal-popup").hidden = true;
    return;
  }
  updatePill(meetings);
  const list = $("#meeting-list");
  list.innerHTML = "";
  for (const m of meetings) {
    const el = document.createElement("div");
    el.className = "mcard" + (m.id === current ? " active" : "");
    const when = new Date(m.created_at * 1000).toLocaleString([], {
      month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
    const dur = m.duration_s ? `${Math.floor(m.duration_s / 60)}m` : "";
    el.innerHTML = `<div class="t"></div>
      <div class="s"><span>${when}</span><span>${dur}</span>
      <span class="badge ${m.status}">${label(m.status)}</span></div>`;
    el.querySelector(".t").textContent = m.title;
    el.onclick = () => openMeeting(m.id);
    list.appendChild(el);
  }
}
const label = (s) => ({ idle: "new", recording: "REC", paused: "paused",
  processing: "transcribing", noting: "writing note", ready: "recap ready",
  ready_no_note: "transcript ready", error: "error" }[s] || s);

/* ---------- meeting detail ---------- */
async function openMeeting(id) {
  current = id;
  detail = await api("/meetings/" + id);
  for (const v of ["empty-state", "ask-view", "voices-view"]) $("#" + v).hidden = true;
  $("#detail").hidden = false;
  // sensible default tab: recap-ready meetings open on AI Note, fresh ones on Agenda
  selectTab(detail.transcript && detail.transcript.length ? "note" : "agenda");
  renderDetail();
  refreshList();
  schedulePoll();
}

function renderDetail() {
  const m = detail.meeting;
  $("#m-title").value = m.title;
  $("#m-mode").value = m.mode;
  $("#m-template").value = m.template_id || "general";
  $("#m-context").value = m.context || "";
  const when = new Date(m.created_at * 1000).toLocaleString();
  $("#m-sub").textContent = `${when}${m.duration_s ? " · " + fmtDur(m.duration_s) : ""}` +
    (detail.note && detail.note.model ? ` · note by ${detail.note.model}` : "");

  const rec = m.status === "recording", paused = m.status === "paused";
  $("#btn-record").hidden = rec || paused;
  $("#btn-pause").hidden = !rec;
  $("#btn-resume").hidden = !paused;
  $("#btn-stop").hidden = !rec && !paused;
  $("#m-mode").disabled = m.status !== "idle";
  $("#rec-banner").hidden = !rec && !paused;
  $("#rec-state").textContent = paused ? "Paused" : "Recording";
  $("#rec-dot").style.animationPlayState = paused ? "paused" : "running";
  if (rec) startRecTimer(m.record_started_at, m.recorded_s || 0);
  else stopRecTimer();
  if (paused) $("#rec-timer").textContent = fmtClock(m.recorded_s || 0);
  $("#rec-tracks").textContent = (m.tracks || []).join(" + ");

  const busy = m.status === "processing" || m.status === "noting";
  $("#proc-banner").hidden = !busy && m.status !== "error";
  if (busy) $("#proc-banner").textContent =
    m.status === "processing" ? "⏳ Transcribing…" : "✍️ Writing AI note…";
  if (m.status === "error") $("#proc-banner").textContent = "⚠️ " + (m.error || "error");

  const hasAudio = ["ready", "ready_no_note", "noting"].includes(m.status) &&
    !m.audio_purged;
  $("#player").hidden = !hasAudio;
  if (hasAudio) $("#player").src = `/api/meetings/${m.id}/audio`;
  if (m.audio_purged) $("#m-sub").textContent +=
    " · audio removed by retention policy (transcript & note kept)";

  const ag = detail.agenda || {};
  $("#ag-talking").value = ag.talking_points || "";
  $("#ag-actions").value = ag.action_items || "";
  $("#ag-notepad").value = ag.notepad || "";

  renderNote();
  renderTranscript();
}

const fmtDur = (s) => `${Math.floor(s / 60)}m ${s % 60}s`;
const esc = (t) => { const d = document.createElement("div"); d.textContent = t; return d.innerHTML; };
const tsChip = (ts) => ts ? `<span class="ts" data-ts="${ts}">${ts}</span>` : "";

const md = (t) => esc(t).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>");

function renderNote() {
  const n = detail.note, body = $("#note-body");
  if (!n) { body.innerHTML = '<p class="muted">No AI note yet — record or upload audio.</p>'; return; }
  if (n.error) { body.innerHTML = `<p class="muted">Note unavailable: ${esc(n.error)}</p>`; return; }

  // unlabeled-speaker banner (Fellow parity)
  const unassigned = [...new Set((detail.transcript || []).map(s => s.speaker))]
    .filter(sp => /^(Speaker \d+|Others)$/.test(sp));
  let h = unassigned.length ? `<div class="note-banner">${unassigned.length} speaker${
    unassigned.length > 1 ? "s have" : " has"} not been named yet — open the Transcript tab and click a speaker name to label.</div>` : "";
  const m2 = detail.meeting;
  if (m2.gdoc_path) h += `<div class="note-banner">Saved to Google Drive: ${esc(m2.gdoc_path)}</div>`;
  if (m2.obsidian_path) h += `<div class="note-banner">Saved to Obsidian vault: ${esc(
    m2.obsidian_path.split("/").slice(-2).join("/"))}</div>`;
  if (m2.calendar_writeback) h += `<div class="note-banner">${
    m2.calendar_writeback === "ok" ? "Debrief added to the calendar event."
    : "Calendar write-back: " + esc(m2.calendar_writeback)}</div>`;

  h += `<h2>Summary</h2><div class="note-summary">${
    (n.summary || "").split(/\n\n+/).map(p => `<p>${md(p)}</p>`).join("")}</div>`;
  if ("action_items" in n) {
    h += "<h2>Action items</h2>";
    h += (n.action_items || []).length
      ? "<ul>" + n.action_items.map(a => `<li>☐ ${md(a.text)}${tsChip(a.ts)}</li>`).join("") + "</ul>"
      : '<p class="muted">No action items were detected in this meeting.</p>';
  }
  if ("decisions" in n) {
    h += "<h2>Decisions</h2>";
    h += (n.decisions || []).length
      ? "<ul>" + n.decisions.map(d => `<li>${md(d.text)}${tsChip(d.ts)}</li>`).join("") + "</ul>"
      : '<p class="muted">No decisions were detected in this meeting.</p>';
  }
  for (const sec of n.sections || []) {
    h += `<h2>${md(sec.title)}</h2>`;
    h += (sec.bullets || []).length
      ? "<ul>" + sec.bullets.map(b => `<li>${md(b.text)}${tsChip(b.ts)}</li>`).join("") + "</ul>"
      : '<p class="muted">None noted.</p>';
  }
  if ((n.topics || []).length) h += "<h1 class='topics-head'>Topics</h1>";
  for (const t of n.topics || []) {
    h += `<h2>${md(t.title || "Topic")}</h2><ul>` + (t.bullets || []).map(b =>
      `<li>${md(b.text)}${tsChip(b.ts)}</li>`).join("") + "</ul>";
  }
  body.innerHTML = h;
  body.querySelectorAll(".ts").forEach(el => el.onclick = () => seek(el.dataset.ts));
}

function renderTranscript() {
  const q = $("#t-search").value.trim().toLowerCase();
  const body = $("#transcript-body");
  const segs = detail.transcript || [];
  if (!segs.length) { body.innerHTML = '<p class="muted">No transcript yet.</p>'; return; }
  body.innerHTML = "";
  segs.forEach((s, i) => {
    if (q && !s.text.toLowerCase().includes(q) && !s.speaker.toLowerCase().includes(q)) return;
    const el = document.createElement("div");
    el.className = "utt";
    const initial = (s.speaker || "?")[0].toUpperCase();
    const text = q ? esc(s.text).replace(new RegExp(`(${q.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")})`, "ig"),
      '<span class="hl">$1</span>') : esc(s.text);
    el.innerHTML = `<div class="avatar">${initial}</div><div>
      <div class="utt-head"><span class="speaker" data-i="${i}">${esc(s.speaker)}</span>
      <span class="ts" data-ts="${msToTs(s.start_ms)}">${msToTs(s.start_ms)}</span></div>
      <div class="text">${text}</div></div>`;
    el.querySelector(".speaker").onclick = () => renameSpeaker(i, s.speaker);
    el.querySelector(".ts").onclick = (e) => seek(e.target.dataset.ts);
    body.appendChild(el);
  });
}

const msToTs = (ms) => { const s = Math.floor(ms / 1000);
  return `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`; };
function seek(ts) {
  const [m, s] = ts.split(":").map(Number);
  const p = $("#player");
  if (!p.hidden) { p.currentTime = m * 60 + s; p.play(); }
}

async function renameSpeaker(index, from) {
  const dlg = $("#rename-dialog"), input = $("#rename-input");
  $("#rename-title").textContent = `Rename "${from}"`;
  // suggestions: saved people + names already used in this transcript
  let people = [];
  try { people = await api("/people"); } catch {}
  const local = [...new Set((detail.transcript || []).map(s => s.speaker))]
    .filter(n => !/^(Speaker \d+|Others|Me)$/.test(n));
  $("#people-list").innerHTML = [...new Set([...people, ...local])]
    .map(n => `<option value="${esc(n)}">`).join("");
  input.value = from.startsWith("Speaker ") || from === "Others" ? "" : from;
  $("#rename-all").checked = true;
  dlg.returnValue = "";
  dlg.showModal();
  input.focus();
  dlg.addEventListener("close", async function handler() {
    dlg.removeEventListener("close", handler);
    const to = input.value.trim();
    if (dlg.returnValue !== "ok" || !to || to === from) return;
    const body = { from, to };
    if (!$("#rename-all").checked) body.segment_index = index;
    const r = await api(`/meetings/${current}/rename_speaker`, {
      method: "POST", body: JSON.stringify(body) });
    detail.transcript = r.transcript;
    renderTranscript();
    renderNote(); // unlabeled-speaker banner may change
  });
}

/* ---------- copy ---------- */
const copy = async (text, btn) => {
  await navigator.clipboard.writeText(text);
  const l = btn.querySelector(".lbl"), old = l.textContent;
  l.textContent = "Copied ✓";
  setTimeout(() => (l.textContent = old), 1200);
};
$("#copy-note").onclick = () => copy(detail.note_md ||
  document.querySelector("#note-body").innerText, $("#copy-note"));
$("#copy-transcript").onclick = () => copy(
  (detail.transcript || []).map(s => `${s.speaker} (${msToTs(s.start_ms)}): ${s.text}`).join("\n"),
  $("#copy-transcript"));
$("#copy-agenda").onclick = () => copy(
  `# ${detail.meeting.title} — Agenda\n\n## Talking Points\n${$("#ag-talking").value}\n\n` +
  `## Action Items\n${$("#ag-actions").value}\n\n## Notepad\n${$("#ag-notepad").value}`,
  $("#copy-agenda"));

/* ---------- actions ---------- */
$("#btn-new").onclick = busy($("#btn-new"), async () => {
  const m = await api("/meetings", { method: "POST", body: JSON.stringify({}) });
  await refreshList();
  openMeeting(m.id);
});
$("#btn-record").onclick = busy($("#btn-record"), async () => {
  // first Record press on a fresh meeting: choose the note template up front
  if (detail.meeting.status === "idle" && !(detail.transcript || []).length) {
    let templates = [];
    try { templates = await api("/templates"); } catch {}
    if (templates.length) {
      const tid = await modal({ title: "What kind of note?",
        text: "Choose how the AI note will be structured after this recording. You can change it later and regenerate.",
        select: templates, value: $("#m-template").value || "general",
        okLabel: "Start recording" });
      if (tid === null) return;  // cancelled — don't record
      $("#m-template").value = tid;
    }
  }
  await saveHeader();
  const r = await api(`/meetings/${current}/start`, { method: "POST" });
  if (r.already_recording) {
    await modal({ title: "Already recording",
      text: `"${r.title}" is recording right now. Stop it before starting another.`,
      okLabel: "Open it" });
    openMeeting(r.id);
    return;
  }
  detail.meeting = r;
  renderDetail(); refreshList(); schedulePoll();
});
$("#btn-pause").onclick = busy($("#btn-pause"), async () => {
  detail.meeting = await api(`/meetings/${current}/pause`, { method: "POST" });
  renderDetail(); refreshList();
});
$("#btn-resume").onclick = busy($("#btn-resume"), async () => {
  detail.meeting = await api(`/meetings/${current}/resume`, { method: "POST" });
  renderDetail(); refreshList(); schedulePoll();
});
$("#btn-stop").onclick = busy($("#btn-stop"), async () => {
  detail.meeting = await api(`/meetings/${current}/stop`, { method: "POST" });
  renderDetail(); refreshList(); schedulePoll();
});

/* overflow menu + delete (moved out of primary actions to avoid misclicks) */
$("#btn-menu").onclick = (e) => {
  e.stopPropagation();
  $("#menu-pop").hidden = !$("#menu-pop").hidden;
};
document.addEventListener("click", () => { $("#menu-pop").hidden = true; });
$("#btn-delete").onclick = async () => {
  $("#menu-pop").hidden = true;
  const ok = await modal({ title: "Move to trash?",
    text: `"${detail.meeting.title}" moves to data/trash/ and can be restored manually.`,
    okLabel: "Move to trash" });
  if (!ok) return;
  await api(`/meetings/${current}`, { method: "DELETE" });
  current = null; $("#detail").hidden = true; $("#empty-state").hidden = false;
  refreshList();
};
$("#gdoc-note").onclick = busy($("#gdoc-note"), async () => {
  const btn = $("#gdoc-note");
  setLbl(btn, "Saving…");
  try {
    const r = await api(`/meetings/${current}/export_gdoc`, { method: "POST" });
    setLbl(btn, "Saved to Drive ✓");
    btn.title = r.path;
  } catch (err) {
    setLbl(btn, "Failed: " + err.message);
  }
  setTimeout(() => setLbl(btn, "Save to Google Doc"), 2500);
});
$("#regen-note").onclick = busy($("#regen-note"), async () => {
  detail.meeting = await api(`/meetings/${current}/regenerate_note`, { method: "POST" });
  renderDetail(); schedulePoll();
});
$("#upload-input").onchange = async (e) => {
  const file = e.target.files[0];
  if (!file) return;
  const m = await api("/meetings", { method: "POST",
    body: JSON.stringify({ title: file.name.replace(/\.[^.]+$/, "") }) });
  const fd = new FormData();
  fd.append("file", file);
  await fetch(`/api/meetings/${m.id}/upload`, { method: "POST", body: fd });
  await refreshList();
  openMeeting(m.id);
  e.target.value = "";
};

async function saveHeader() {
  detail.meeting = await api(`/meetings/${current}`, { method: "PATCH",
    body: JSON.stringify({ title: $("#m-title").value, context: $("#m-context").value,
      mode: $("#m-mode").value, template_id: $("#m-template").value }) });
}
$("#m-title").onchange = saveHeader;
$("#m-context").onchange = saveHeader;
$("#m-mode").onchange = saveHeader;
$("#m-template").onchange = async () => {
  await saveHeader();
  // template applies to the note — offer instant regeneration if one exists
  if (detail.note && !detail.note.error &&
      detail.meeting.template_id !== detail.note.template) {
    const ok = await modal({ title: "Regenerate note?",
      text: `Rebuild the AI note with the "${$("#m-template").selectedOptions[0].text}" template now?`,
      okLabel: "Regenerate" });
    if (ok) $("#regen-note").click();
  }
};

async function loadTemplates() {
  try {
    const ts = await api("/templates");
    $("#m-template").innerHTML = ts.map(t =>
      `<option value="${esc(t.id)}">${esc(t.name)}</option>`).join("");
  } catch {}
}

let agendaTimer = null;
for (const id of ["ag-talking", "ag-actions", "ag-notepad"]) {
  $("#" + id).oninput = () => {
    clearTimeout(agendaTimer);
    agendaTimer = setTimeout(() => api(`/meetings/${current}/agenda`, {
      method: "POST", body: JSON.stringify({
        talking_points: $("#ag-talking").value,
        action_items: $("#ag-actions").value,
        notepad: $("#ag-notepad").value }) }), 600);
  };
}
$("#t-search").oninput = renderTranscript;

/* ---------- tabs ---------- */
function selectTab(name) {
  document.querySelectorAll("#tabs button").forEach(x =>
    x.classList.toggle("active", x.dataset.tab === name));
  for (const t of ["agenda", "note", "transcript"])
    $("#tab-" + t).hidden = t !== name;
}
document.querySelectorAll("#tabs button").forEach(b =>
  b.onclick = () => selectTab(b.dataset.tab));

/* ---------- polling ---------- */
function schedulePoll() {
  clearInterval(pollTimer);
  pollTimer = setInterval(async () => {
    if (!current) return;
    const st = detail.meeting.status;
    if (["recording", "processing", "noting"].includes(st)) {
      const fresh = await api("/meetings/" + current);
      if (fresh.meeting.status !== st) { detail = fresh; renderDetail(); refreshList(); }
      else detail.meeting = fresh.meeting;
    }
  }, 2000);
}
const fmtClock = (t) =>
  `${String(Math.floor(t / 60)).padStart(2, "0")}:${String(t % 60).padStart(2, "0")}`;
function startRecTimer(startedAt, baseSeconds) {
  clearInterval(recTimer);
  recTimer = setInterval(() => {
    const t = baseSeconds + Math.floor(Date.now() / 1000) - startedAt;
    $("#rec-timer").textContent = fmtClock(t);
  }, 500);
}
const stopRecTimer = () => clearInterval(recTimer);

/* ---------- Google Calendar (ICS) ---------- */
let calEvents = [], dismissed = new Set();

$("#cal-connect").onclick = async () => {
  const url = await modal({ title: "Connect Google Calendar",
    text: "Paste your calendar's private ICS URL (Google Calendar → Settings → your calendar → 'Secret address in iCal format'). It stays on this Mac.",
    input: true, placeholder: "https://calendar.google.com/calendar/ical/…",
    okLabel: "Connect" });
  if (!url) return;
  await api("/settings", { method: "POST", body: JSON.stringify({ ics_url: url }) });
  refreshCalendar();
  refreshSetupCard();
};

async function refreshCalendar() {
  let d;
  try { d = await api("/calendar/today"); } catch { return; }
  const box = $("#cal-events");
  $("#cal-connect").textContent = d.connected ? "↻" : "connect";
  if (!d.connected) { box.innerHTML = '<div class="muted" style="font-size:12px">Not connected</div>'; return; }
  if (d.error) { box.innerHTML = `<div class="muted" style="font-size:12px">⚠️ ${esc(d.error)}</div>`; return; }
  calEvents = d.events || [];
  box.innerHTML = calEvents.length ? "" :
    '<div class="muted" style="font-size:12px">No meetings today</div>';
  const now = Date.now() / 1000;
  for (const ev of calEvents) {
    const el = document.createElement("div");
    el.className = "cal-ev" + (ev.start_epoch + 3600 < now ? " past" : "");
    el.innerHTML = `<span class="when">${ev.start_hm}</span><span class="t"></span>
      <button class="recbtn">● rec</button>`;
    el.querySelector(".t").textContent = ev.title;
    el.title = (ev.attendees || []).join(", ");
    el.querySelector(".recbtn").onclick = () => recordEvent(ev);
    box.appendChild(el);
  }
  checkPopup();
}

async function recordEvent(ev) {
  const m = await api("/calendar/record", { method: "POST",
    body: JSON.stringify({ event: ev, mode: "online" }) });
  dismissed.add(ev.uid || ev.title);
  $("#cal-popup").hidden = true;
  if (m.already_recording) {
    await modal({ title: "Already recording",
      text: `"${m.title}" is recording right now.`, okLabel: "Open it" });
  }
  await refreshList();
  openMeeting(m.id);
}

function checkPopup() {
  const now = Date.now() / 1000;
  const next = calEvents.find(ev =>
    !dismissed.has(ev.uid || ev.title) &&
    ev.start_epoch - now < 180 && now - ev.start_epoch < 600);
  if (!next) { $("#cal-popup").hidden = true; return; }
  // don't nag while ANY meeting is recording (not just the open one —
  // the popup was nagging mid-class while viewing another meeting)
  if (pillMeeting) { $("#cal-popup").hidden = true; return; }
  $("#cal-popup-title").textContent = next.title || "Meeting";
  const mins = Math.round((next.start_epoch - now) / 60);
  $("#cal-popup-when").textContent =
    mins > 0 ? `in ${mins} min` : mins === 0 ? "now" : `${-mins} min ago`;
  $("#cal-popup-att").textContent = (next.attendees || []).join(", ");
  $("#cal-popup-rec").onclick = () => recordEvent(next);
  $("#cal-popup-dismiss").onclick = () => {
    dismissed.add(next.uid || next.title);
    $("#cal-popup").hidden = true;
  };
  $("#cal-popup").hidden = false;
}

setInterval(refreshCalendar, 5 * 60 * 1000);
setInterval(checkPopup, 30 * 1000);

/* ---------- library search ---------- */
let libTimer = null;
$("#lib-search").oninput = () => {
  clearTimeout(libTimer);
  const q = $("#lib-search").value.trim();
  if (!q) { $("#lib-results").hidden = true; $("#meeting-list").hidden = false; return; }
  libTimer = setTimeout(async () => {
    const hits = await api("/search?q=" + encodeURIComponent(q));
    const box = $("#lib-results");
    box.innerHTML = hits.length ? "" : '<div class="muted" style="padding:8px">No matches</div>';
    for (const h of hits) {
      const el = document.createElement("div");
      el.className = "hit";
      el.innerHTML = `<div class="h-title"></div><div class="h-snip">${
        esc(h.snippet).replace(/\[(.+?)\]/g, "<b>$1</b>")} <span class="muted">· ${h.ts}</span></div>`;
      el.querySelector(".h-title").textContent =
        `${h.meeting_title}${h.speaker ? " — " + h.speaker : ""}`;
      el.onclick = () => { openMeeting(h.meeting_id); };
      box.appendChild(el);
    }
    box.hidden = false;
    $("#meeting-list").hidden = true;
  }, 300);
};

/* ---------- Ask panel ---------- */
let askHistory = [];
$("#btn-ask").onclick = () => {
  showView("ask-view");
  $("#ask-input").focus();
};
$("#ask-form").onsubmit = async (e) => {
  e.preventDefault();
  if ($("#ask-send").disabled) return;
  const q = $("#ask-input").value.trim();
  if (!q) return;
  $("#ask-send").disabled = true;
  $("#ask-input").value = "";
  const log = $("#ask-log");
  const u = document.createElement("div");
  u.className = "msg user"; u.textContent = q; log.appendChild(u);
  const t = document.createElement("div");
  t.className = "msg assistant thinking";
  t.textContent = "Searching your meetings…"; log.appendChild(t);
  t.scrollIntoView();
  try {
    const r = await api("/ask", { method: "POST",
      body: JSON.stringify({ question: q, history: askHistory.slice(-6) }) });
    t.classList.remove("thinking");
    t.innerHTML = md(r.answer);
    if (r.steps && r.steps.length) {
      t.innerHTML += `<span class="tools-line">🔎 ${r.steps.map(s =>
        s.tool + (s.args.query ? `("${esc(s.args.query)}")` : "")).join(" → ")
        } · ${esc(r.model)}</span>`;
    }
    askHistory.push({ role: "user", content: q },
                    { role: "assistant", content: r.answer });
  } catch (err) {
    t.classList.remove("thinking");
    t.textContent = "Error: " + err.message;
  }
  $("#ask-send").disabled = false;
  t.scrollIntoView();
};

/* ---------- People & Voices panel ---------- */
function showView(name) {
  for (const v of ["detail", "ask-view", "voices-view", "empty-state"])
    $("#" + v).hidden = v !== name;
  if (name !== "detail") current = null;
}
async function setMyName() {
  const name = await modal({ title: "Your name",
    text: "Used to label your mic channel on calls and to match your voice.",
    input: true, placeholder: "e.g. Arvin", okLabel: "Save" });
  if (!name) return false;
  await api("/settings", { method: "POST", body: JSON.stringify({ my_name: name }) });
  refreshSetupCard();
  refreshVoices();
  return true;
}

async function refreshVoices() {
  let d;
  try { d = await api("/voices"); } catch { return; }
  $("#v-myname").textContent = d.my_name || "not set";
  const list = $("#v-list");
  if (!d.people.length) {
    list.innerHTML = '<p class="muted">No voices enrolled yet — enroll below, or rename a speaker in any transcript.</p>';
  } else {
    list.innerHTML = "";
    for (const p of d.people) {
      const el = document.createElement("div");
      el.className = "v-person";
      const srcs = [p.enrolled ? `${p.enrolled} enrolled` : "",
                    p.learned ? `${p.learned} learned from meetings` : ""]
                   .filter(Boolean).join(" · ");
      const when = p.updated ? new Date(p.updated * 1000).toLocaleDateString(
        [], { month: "short", day: "numeric" }) : "";
      el.innerHTML = `<div class="avatar">${esc(p.name[0].toUpperCase())}</div>
        <div class="info"><div class="who"></div>
        <div class="meta">${p.prints} voiceprint${p.prints > 1 ? "s" : ""} — ${srcs}${
          when ? " · updated " + when : ""}</div></div>
        <button class="linkbtn" aria-label="Delete ${esc(p.name)}'s voiceprints">
          <svg class="ic sm"><use href="#i-trash"/></svg></button>`;
      el.querySelector(".who").textContent = p.name;
      el.querySelector("button").onclick = async () => {
        const ok = await modal({ title: `Forget ${p.name}'s voice?`,
          text: "Their voiceprints are deleted; renaming them in a future transcript re-learns the voice.",
          okLabel: "Delete" });
        if (!ok) return;
        await api("/voices/" + encodeURIComponent(p.name), { method: "DELETE" });
        refreshVoices();
      };
      list.appendChild(el);
    }
  }
  // suggestions for the enroll input
  try {
    const people = await api("/people");
    $("#people-list").innerHTML = [...new Set([d.my_name, ...people].filter(Boolean))]
      .map(n => `<option value="${esc(n)}">`).join("");
  } catch {}
}

function countdown(total, label) {
  $("#v-progress").hidden = false;
  const t0 = Date.now();
  const iv = setInterval(() => {
    const gone = (Date.now() - t0) / 1000;
    const left = Math.max(0, total - gone);
    $("#v-bar").style.inset = `0 ${Math.max(0, 100 - gone / total * 100)}% 0 0`;
    $("#v-count").textContent = left > 0 ? `${label} — ${Math.ceil(left)} s…` : "Processing…";
  }, 200);
  return () => { clearInterval(iv); $("#v-progress").hidden = true; };
}

// live-personalize the reading script with the name being enrolled
$("#v-name").oninput = () => {
  const n = $("#v-name").value.trim() || "[your name]";
  document.querySelectorAll(".v-script-name").forEach(el => el.textContent = n);
};

$("#v-setname").onclick = setMyName;
$("#v-record").onclick = busy($("#v-record"), async () => {
  let name = $("#v-name").value.trim();
  if (!name) {
    const d = await api("/voices");
    name = d.my_name;
    if (!name) { if (!await setMyName()) return; name = (await api("/voices")).my_name; }
    $("#v-name").value = name;
  }
  const res = $("#v-result");
  res.textContent = ""; res.className = "muted";
  const stop = countdown(20, `Recording ${name} — speak now`);
  try {
    const r = await api("/voices/enroll", { method: "POST",
      body: JSON.stringify({ name }) });
    res.textContent = `Voice captured for ${r.name}: ${r.prints_added} voiceprints, ` +
      `level ${r.level_db} dB (${r.quality === "good" ? "good level ✓"
        : "a bit quiet — consider re-recording closer to the mic"})`;
    res.className = r.quality === "good" ? "v-good" : "v-warn";
  } catch (err) {
    res.textContent = err.message;
    res.className = "v-bad";
  }
  stop();
  refreshVoices();
  refreshSetupCard();
});
$("#v-test").onclick = busy($("#v-test"), async () => {
  const res = $("#v-test-result");
  res.textContent = ""; res.className = "muted";
  const stop = countdown(5, "Listening — speak now");
  try {
    const r = await api("/voices/test", { method: "POST", body: JSON.stringify({}) });
    res.textContent = r.message;
    res.className = r.match ? "v-good" : (r.quality === "silent" ? "v-bad" : "v-warn");
  } catch (err) {
    res.textContent = err.message;
    res.className = "v-bad";
  }
  stop();
});
$("#btn-voices").onclick = () => { showView("voices-view"); refreshVoices(); };
const enrollVoice = () => { showView("voices-view"); refreshVoices(); };

/* ---------- first-run setup card ---------- */
async function refreshSetupCard() {
  const card = $("#setup-card");
  if (localStorage.getItem("lf-setup-dismissed")) { card.hidden = true; return; }
  let s = {};
  try { s = await api("/settings"); } catch { return; }
  const states = { name: !!s.my_name, voice: !!s.has_voiceprints, cal: !!s.ics_url_set };
  let allDone = true;
  for (const [step, done] of Object.entries(states)) {
    const item = card.querySelector(`[data-step="${step}"]`);
    item.classList.toggle("done", done);
    const b = item.querySelector("button");
    if (b) b.hidden = done;
    allDone = allDone && done;
  }
  card.hidden = allDone;
}
$("#setup-name").onclick = setMyName;
$("#setup-voice").onclick = () => enrollVoice();
$("#setup-cal").onclick = () => $("#cal-connect").click();
$("#setup-dismiss").onclick = () => {
  localStorage.setItem("lf-setup-dismissed", "1");
  $("#setup-card").hidden = true;
};

/* ---------- global recording pill ---------- */
let pillMeeting = null, pillTimerIv = null;
function updatePill(meetings) {
  const m = (meetings || []).find(x => x.status === "recording" || x.status === "paused");
  const pill = $("#rec-pill");
  pillMeeting = m || null;
  if (!m) { pill.hidden = true; clearInterval(pillTimerIv); return; }
  pill.hidden = false;
  pill.classList.toggle("paused", m.status === "paused");
  $("#pill-open").textContent = m.title;
  $("#pill-pause").hidden = m.status !== "recording";
  $("#pill-resume").hidden = m.status !== "paused";
  clearInterval(pillTimerIv);
  const base = m.recorded_s || 0, started = m.record_started_at || 0;
  const tick = () => $("#pill-timer").textContent = fmtClock(
    m.status === "recording" ? base + Math.floor(Date.now() / 1000) - started : base);
  tick();
  if (m.status === "recording") pillTimerIv = setInterval(tick, 500);
}
$("#pill-open").onclick = () => pillMeeting && openMeeting(pillMeeting.id);
$("#pill-pause").onclick = busy($("#pill-pause"), async () => {
  if (!pillMeeting) return;
  await api(`/meetings/${pillMeeting.id}/pause`, { method: "POST" });
  await refreshList();
  if (current === pillMeeting.id) openMeeting(current);
});
$("#pill-resume").onclick = busy($("#pill-resume"), async () => {
  if (!pillMeeting) return;
  await api(`/meetings/${pillMeeting.id}/resume`, { method: "POST" });
  await refreshList();
  if (current === pillMeeting.id) openMeeting(current);
});
$("#pill-stop").onclick = busy($("#pill-stop"), async () => {
  if (!pillMeeting) return;
  const id = pillMeeting.id;
  await api(`/meetings/${id}/stop`, { method: "POST" });
  await refreshList();
  openMeeting(id);
});

/* ---------- self-update (never interrupts a recording) ---------- */
async function checkUpdate() {
  let s;
  try { s = await api("/update_status"); } catch { return; }
  const banner = $("#update-banner"), btn = $("#btn-update");
  banner.hidden = !(s.update_available || s.pending);
  if (s.pending) {
    $("#update-text").textContent =
      "Update queued — it will apply automatically when the current recording/processing finishes.";
    btn.hidden = true;
  } else {
    $("#update-text").textContent = "A software update for LocalFellow is ready.";
    btn.hidden = false;
    setLbl(btn, "Update now");
  }
}
$("#btn-update").onclick = busy($("#btn-update"), async () => {
  const r = await api("/update", { method: "POST", body: JSON.stringify({}) });
  await modal({ title: r.queued ? "Update queued" : "Updating",
    text: r.message, okLabel: "OK" });
  if (!r.queued) setTimeout(() => location.reload(), 10000);
  checkUpdate();
});
setInterval(checkUpdate, 5 * 60 * 1000);
checkUpdate();

// keep the global pill fresh even when the user isn't interacting
setInterval(() => { if (pillMeeting) refreshList(); }, 10000);
// and keep retrying while the server is unreachable
setInterval(() => { if (!$("#offline").hidden) refreshList(); }, 15000);

refreshList();
refreshCalendar();
refreshSetupCard();
loadTemplates();
