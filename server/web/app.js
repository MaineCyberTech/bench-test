"use strict";
/* bench-test control plane — single pane of glass (vanilla JS, no build step) */

const GROUPS = ["gpu", "cpu", "ram", "disk", "combined", "fullsoak", "net", "nvenc", "llm", "fan", "sys", "gfx"];
const ALL_GROUPS = ["gpu", "cpu", "ram", "disk", "combined", "fullsoak", "net", "nvenc", "llm", "fan", "sys", "gfx"];
const gset = (...on) => Object.fromEntries(ALL_GROUPS.map((g) => [g, on.includes(g) ? 1 : 0]));
const DEF_SPIN = { gpu: 600, combined: 300, fullsoak: 300, gfx: 60 };
const PRESETS = {
  "Quick check (short soaks)": { groups: gset("gpu", "cpu", "ram", "disk", "combined", "fullsoak", "net", "nvenc", "sys", "gfx"), spin: { gpu: 60, combined: 60, fullsoak: 60, gfx: 30 } },
  "System info + 60s soak (all)": { groups: gset("sys", "fullsoak"), spin: { ...DEF_SPIN, fullsoak: 60 } },
  "Full run (defaults)": { groups: gset(...ALL_GROUPS), spin: { ...DEF_SPIN } },
  "GPU compute": { groups: gset("gpu", "sys"), spin: { ...DEF_SPIN, gpu: 60 } },
  "CPU + RAM + disk": { groups: gset("cpu", "ram", "disk", "sys"), spin: { ...DEF_SPIN } },
  "Storage + network": { groups: gset("disk", "net", "sys"), spin: { ...DEF_SPIN } },
  "Video / graphics": { groups: gset("nvenc", "gfx", "sys"), spin: { ...DEF_SPIN } },
  "LLM inference (Ollama)": { groups: gset("llm", "sys"), spin: { ...DEF_SPIN } },
  "Fan response": { groups: gset("fan", "sys"), spin: { ...DEF_SPIN } },
  "Soak only (long)": { groups: gset("gpu", "fullsoak", "sys"), spin: { ...DEF_SPIN, gpu: 1800, fullsoak: 1800 } },
  "Full-system soak (long)": { groups: gset("fullsoak", "sys"), spin: { ...DEF_SPIN, fullsoak: 1800 } },
};
const C = { temp: "#ef6b73", power: "#4c9aff", util: "#46c98d", fan: "#e5b567" };
const SERIES = {
  temp_c: ["temp °C", "#ef6b73"], power_w: ["power W", "#4c9aff"], gpu_util: ["GPU %", "#46c98d"],
  fan_pct: ["fan %", "#e5b567"], mem_util: ["VRAM %", "#b48ead"], sm_mhz: ["SM MHz", "#8fa1b3"],
  mem_mhz: ["MEM MHz", "#7f9cf5"], vram_used_mib: ["VRAM MiB", "#e5b567"],
  cpu_util: ["CPU %", "#63b3ed"], cpu_temp_c: ["CPU °C", "#f6ad55"], ram_used: ["RAM GiB", "#68d391"],
  load1: ["load1", "#d6bcfa"], nvme_c: ["NVMe °C", "#fc8181"], cpu_max_core_c: ["CPU core max", "#f6ad55"],
  temp_mem_c: ["VRAM temp °C", "#feb2b2"], gr_mhz: ["GPU core MHz", "#4fd1c5"],
  gpu_enc_pct: ["enc %", "#f6e05e"], gpu_dec_pct: ["dec %", "#faf089"],
  power_limit_w: ["power cap W", "#90cdf4"], video_mhz: ["NVENC MHz", "#b48ead"],
  temp_limit_c: ["thermal limit °C", "#fc8181"], pcie_gen: ["PCIe gen", "#a0aec0"],
  pcie_width: ["PCIe width", "#a0aec0"], throttle: ["throttled", "#f6ad55"],
  disk_read_mbs: ["disk R MB/s", "#68d391"], disk_write_mbs: ["disk W MB/s", "#f6ad55"],
  net_rx_mbs: ["net rx MB/s", "#63b3ed"], net_tx_mbs: ["net tx MB/s", "#4fd1c5"],
  cpu_pkg_w: ["CPU W", "#f687b3"], cpu_mhz: ["CPU MHz", "#9ae6b4"], psi_io: ["io stall", "#faf089"],
};
const CHART_PRESETS = {
  "temp · power · util": ["temp_c", "power_w", "gpu_util"],
  "temp · fan · power": ["temp_c", "fan_pct", "power_w"],
  "VRAM · SM clock": ["vram_used_mib", "sm_mhz", "mem_util"],
  "CPU · RAM": ["cpu_util", "ram_used", "load1"],
  "CPU temp · NVMe": ["cpu_temp_c", "cpu_max_core_c", "nvme_c"],
  "headroom · throttle": ["temp_c", "temp_limit_c", "throttle"],
  "clocks · video": ["sm_mhz", "gr_mhz", "video_mhz"],
  "disk · net": ["disk_read_mbs", "disk_write_mbs", "net_rx_mbs"],
  "CPU power · clocks": ["cpu_pkg_w", "cpu_util", "cpu_temp_c"],
};
const GAUGES = [["temp_c", "temp °C"], ["power_w", "power W"], ["gpu_util", "GPU %"],
                ["fan_pct", "fan %"], ["mem_util", "VRAM %"], ["cpu_temp_c", "CPU °C"]];

let SHOW_OFFLINE = localStorage.getItem("benchd_show_offline") === "1";
let SORT = { key: "started", dir: -1 };

let TOKEN = new URLSearchParams(location.search).get("token") || localStorage.getItem("benchd_token") || "";
let STATE = { runners: [], active: [], recent: [], metrics: [] };
let TEL = {};               // run_id -> [sample dicts]
let LOGS = {};              // run_id -> [line strings]
let CHART = {};             // run_id -> selected chart preset
let HOSTS = [];
let SCHEDULES = [];
let es = null;

/* ------------------------------------------------------------------ dom -- */
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = (v, d = 1) => (v == null || isNaN(v)) ? "—" : (+v).toFixed(d);
const dur = (s) => s == null ? "—" : (s < 60 ? s.toFixed(0) + "s"
  : s < 3600 ? Math.floor(s / 60) + "m" + Math.round(s % 60) + "s"
  : Math.floor(s / 3600) + "h" + Math.round((s % 3600) / 60) + "m");
const ago = (t) => { if (!t) return "—"; const d = Date.now() / 1000 - t;
  return d < 60 ? Math.round(d) + "s ago" : d < 3600 ? Math.round(d / 60) + "m ago" : Math.round(d / 3600) + "h ago"; };
const ts = (t) => t ? new Date(t * 1000).toLocaleString() : "—";
const runDur = (r) => {
  if (!r.ended) return dur(Date.now() / 1000 - r.started);
  const d = r.ended - r.started;
  return d > 0 ? dur(d) : "—";
};
const sysLine = (r) => {
  const sy = r.system || {};
  const cpu = (sy.cpu || "").replace(/\((R|TM)\)/g, "").replace(/\s*CPU\s*@.*/i, "").trim();
  const gpu = (sy.gpu || r.gpu || "").replace(/NVIDIA GeForce\s*/i, "");
  const parts = [cpu, sy.ram_gib ? sy.ram_gib + " GiB" : null, gpu].filter(Boolean);
  return parts.join(" · ");
};
function toast(msg, kind) {
  let t = $("#toast");
  if (!t) { t = document.createElement("div"); t.id = "toast"; document.body.appendChild(t); }
  t.className = "toast show " + (kind || "");
  t.textContent = msg;
  clearTimeout(window._toast); window._toast = setTimeout(() => { t.className = "toast"; }, 4000);
}

/* ------------------------------------------------------------------ api -- */
async function api(path, method = "GET", body) {
  const opt = { method, headers: { Authorization: "Bearer " + TOKEN } };
  if (body) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
  const r = await fetch("/api/v1" + path, opt);
  if (r.status === 401) { askToken(); throw new Error("unauthorized"); }
  if (!r.ok) throw new Error("HTTP " + r.status);
  return r.json();
}

/* -------------------------------------------------------------- token --- */
function askToken() {
  openModal(`<h3>Access token</h3>
    <p class="muted">This control plane requires a bearer token.</p>
    <div class="formrow"><input id="tokInput" style="flex:1" placeholder="paste token"
      value="${esc(TOKEN)}"/></div>
    <div class="formrow" style="justify-content:flex-end">
      <button class="accent" id="tokSave">save</button></div>`);
  $("#tokSave").onclick = () => {
    TOKEN = $("#tokInput").value.trim();
    localStorage.setItem("benchd_token", TOKEN);
    closeModal(); connect(); refresh();
  };
}

/* ----------------------------------------------------------------- sse -- */
function connect() {
  if (es) es.close();
  es = new EventSource("/api/v1/stream?token=" + encodeURIComponent(TOKEN));
  es.onopen = () => setConn(true);
  es.onerror = () => setConn(false);
  es.addEventListener("snapshot", () => { setConn(true); refresh(); });
  es.addEventListener("runner", () => queueRefresh());
  es.addEventListener("run", () => queueRefresh());
  es.addEventListener("phase", (e) => onPhase(JSON.parse(e.data)));
  es.addEventListener("telemetry", (e) => onTelemetry(JSON.parse(e.data)));
  es.addEventListener("command", () => queueRefresh());
  es.addEventListener("log", (e) => onLog(JSON.parse(e.data)));
  es.addEventListener("schedule", () => renderSchedules());
  es.addEventListener("event", () => {});
}
function onLog(d) {
  const buf = LOGS[d.run_id] || (LOGS[d.run_id] = []);
  for (const l of (d.lines || [])) buf.push(l);
  if (buf.length > 2000) buf.splice(0, buf.length - 2000);
  const card = document.querySelector(`[data-active="${d.run_id}"]`);
  if (card) paintConsole(card, d.run_id);
}
function setConn(on) {
  $("#connDot").className = "dot " + (on ? "on" : "off");
  $("#connText").textContent = on ? "live" : "disconnected";
  const b = $("#banner");
  if (b) b.classList.toggle("show", !on && !!TOKEN);
}

/* --------------------------------------------------------------- state -- */
function _scrollState() {
  const consoles = {};
  document.querySelectorAll("[data-active]").forEach((el) => {
    const pre = el.querySelector(".console");
    if (pre) consoles[el.dataset.active] = pre.scrollTop;
  });
  const tc = document.querySelector(".tablecard");
  return { y: window.scrollY, consoles, table: tc ? tc.scrollTop : 0 };
}
function _restoreScroll(st) {
  const apply = () => {
    window.scrollTo(0, st.y);
    document.querySelectorAll("[data-active]").forEach((el) => {
      const pre = el.querySelector(".console");
      if (pre && st.consoles[el.dataset.active] != null) pre.scrollTop = st.consoles[el.dataset.active];
    });
    const tc = document.querySelector(".tablecard");
    if (tc) tc.scrollTop = st.table;
  };
  apply();
  requestAnimationFrame(apply);  // again after the browser re-lays out
}
let _refreshTimer = null;
function queueRefresh(delay) {
  if (_refreshTimer) return;
  _refreshTimer = setTimeout(() => { _refreshTimer = null; refresh(); }, delay || 1200);
}
async function refresh() {
  try {
    const s = await api("/state");
    STATE = s;
    HOSTS = [...new Set(STATE.recent.map((r) => r.host).filter(Boolean))].sort();
    for (const a of STATE.active) {
      ensureTel(a.id);
      if (!LOGS[a.id] && a.logs_tail) LOGS[a.id] = a.logs_tail.map((l) => l.line);
    }
    const st = _scrollState();
    renderAll();
    _restoreScroll(st);
  } catch (e) { /* offline */ }
}

function ensureTel(id) {
  if (TEL[id]) return;
  TEL[id] = [];
  api("/runs/" + id + "/telemetry?max=600").then((d) => {
    TEL[id] = (d.rows || []).filter((x) => x.elapsed != null);
    renderActive();
  }).catch(() => {});
}

function onTelemetry(d) {
  const s = d.sample;
  if (!s || typeof s !== "object") return;
  const buf = TEL[d.run_id] || (TEL[d.run_id] = []);
  buf.push(s);
  if (buf.length > 900) buf.shift();
  const card = document.querySelector(`[data-active="${d.run_id}"]`);
  if (card) { paintGauges(card, s); drawRunChart(card, d.run_id); }
}

function onPhase(p) {
  const card = document.querySelector(`[data-active="${p.run_id}"]`);
  if (card) queueRefresh(300);
}

/* -------------------------------------------------------------- render -- */
function renderAll() { renderTop(); renderRunners(); renderActive(); renderHistory(); renderTrends(); renderCompareOptions(); renderSchedules(); }

function machineKey(r) { return r.machine_id || ("name:" + (r.name || "")); }

function renderTop() {
  const online = STATE.runners.filter((r) => r.online).length;
  const machines = new Set(STATE.runners.map(machineKey)).size;
  $("#topstats").innerHTML = [
    ["machines", machines], ["runners", STATE.runners.length], ["online", online],
    ["active runs", STATE.active.length], ["history", STATE.recent.length],
  ].map(([k, v]) => `<div class="tstat"><div class="v">${v}</div><div class="k">${k}</div></div>`).join("");
  document.title = STATE.active.length ? `(${STATE.active.length}) bench-test` : "bench-test control plane";
  const off = STATE.runners.length - online;
  $("#runnerCount").innerHTML = `${online} online · ${machines} machine${machines === 1 ? "" : "s"}` +
    (off ? ` · <button class="badge toggle" data-toggle-off>${SHOW_OFFLINE ? "hide" : "show"} ${off} offline</button>` : "");
  $("#activeCount").textContent = `${STATE.active.length} running`;
}

function runnerCard(r) {
  const cls = r.status === "busy" ? "busy" : (r.online ? "online" : "offline");
  const caps = r.capabilities || {};
  const run = STATE.active.find((a) => a.id === r.current_run);
  const phase = run && run.phases.length ? run.phases[run.phases.length - 1] : null;
  const disabled = r.online ? "" : "disabled";
  const badges = [
    `<span class="badge kind">${esc(r.kind || "runner")}</span>`,
    r.instance ? `<span class="badge instance">${esc(r.instance)}</span>` : "",
    r.ephemeral ? `<span class="badge ephemeral">ephemeral</span>` : "",
    r.uid ? `<span class="uid">#${esc(r.uid)}</span>` : "",
  ].join("");
  const dev = [caps.gpu || "no GPU", caps.cpu, caps.ram_gib ? caps.ram_gib + " GiB" : "",
               caps.driver ? "driver " + caps.driver : ""].filter(Boolean).join(" · ");
  return `<div class="runner ${r.online ? "" : "off"} ${r.status === "busy" ? "busy" : ""}" data-runner="${r.id}">
      <div class="rhead">
        <span class="statusp ${cls}"></span>
        <div style="flex:1;min-width:0"><div class="rname">${esc(r.label || r.name)}</div></div>
      </div>
      <div class="rmeta row">${badges}</div>
      <div class="rmeta">${esc(dev || "no hardware detail")}</div>
      ${phase ? `<div class="phasechip"><span class="d"></span>${esc(phase.grp)} · ${esc(phase.label || "")}</div>` : ""}
      <div class="ract" style="margin-top:12px">
        <button class="accent" data-start="${r.id}" ${disabled}>Start run</button>
        <button data-stop="${r.id}" ${run ? "" : "disabled"}>Stop</button>
        <button class="ghost" data-update="${r.id}" ${disabled}>Update</button>
        <button class="ghost" data-fan="${r.id}" ${disabled}>Fan curve</button>
        <button class="ghost power" data-power="${r.id}" data-powerkind="reboot" ${disabled}
                title="Reboot this machine">reboot</button>
        <button class="ghost power" data-power="${r.id}" data-powerkind="shutdown" ${disabled}
                title="Power off this machine">shut down</button>
        <button class="ghost forget" data-forget="${r.id}" title="Remove this runner from the fleet">forget</button>
      </div>
      <div class="rmeta" style="margin-top:8px">last seen ${ago(r.last_seen)}${r.git ? " · git " + esc(String(r.git).slice(0, 7)) : ""}</div>
    </div>`;
}

function renderRunners() {
  const el = $("#runners");
  if (!STATE.runners.length) {
    el.innerHTML = `<div class="card empty">No runners registered yet. Start an agent with <span class="mono">python bench/agent.py --server … --token …</span></div>`;
    return;
  }
  const rank = (r) => (r.status === "busy" ? 0 : r.online ? 1 : 2);
  let list = STATE.runners.slice().sort((a, b) =>
    rank(a) - rank(b) || String(a.label || a.name).localeCompare(String(b.label || b.name)) ||
    String(a.instance || "").localeCompare(String(b.instance || "")));
  if (!SHOW_OFFLINE) list = list.filter((r) => r.online);
  if (!list.length) { el.innerHTML = `<div class="card empty">All runners offline — use “show offline”.</div>`; return; }
  el.innerHTML = list.map(runnerCard).join("");
}

function runCardHTML(a) {
  const preset = CHART[a.id] || Object.keys(CHART_PRESETS)[0];
  return `<div class="runbox" data-active="${a.id}">
      <div class="rh">
        <span class="statusp busy"></span>
        <div class="h">${esc(a.host)} · ${esc(a.label || "run")}</div>
        <span class="muted mono elapsed"></span>
        <span class="muted mono sysline">${esc(sysLine(a))}</span>
        <div style="margin-left:auto" class="ract">
          <button class="danger" data-stop="${a.runner_id}">Stop</button>
          <button class="ghost" data-detail="${a.id}">details</button>
        </div>
      </div>
      <div class="gauges">
        ${GAUGES.map(([k, label]) => `<div class="gauge"><div class="gv" data-g="${k}">—</div><div class="gk">${esc(label)}</div></div>`).join("")}
      </div>
      <div class="chartrow">
        <canvas class="runchart" height="180"></canvas>
        <select class="csel" data-chart="${a.id}">${Object.keys(CHART_PRESETS).map((k) => `<option${k === preset ? " selected" : ""}>${esc(k)}</option>`).join("")}</select>
      </div>
      <div class="phases"></div>
      <div class="consolewrap">
        <div class="chead"><span class="muted">live console</span>
          <form class="term" data-exec="${a.runner_id}">
            <input placeholder="run a command on this runner…" autocomplete="off">
            <button class="accent" type="submit">run</button>
          </form>
        </div>
        <pre class="console mono"></pre>
      </div>
    </div>`;
}

function paintHeader(card, a) {
  const el = card.querySelector(".rh .elapsed");
  if (el && a.started) el.textContent = dur(Date.now() / 1000 - a.started) + " elapsed";
}
function updatePhases(card, phases) {
  const el = card.querySelector(".phases");
  if (!el) return;
  const st = el.scrollTop;
  el.innerHTML = (phases || []).map(phaseRow).join("");
  el.scrollTop = st;
}
function paintConsole(card, id) {
  const pre = card.querySelector(".console");
  if (!pre) return;
  const atBottom = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 40;
  pre.textContent = (LOGS[id] || []).slice(-300).join("\n");
  if (atBottom) pre.scrollTop = pre.scrollHeight;
}

function renderActive() {
  const el = $("#activeRuns");
  const ids = STATE.active.map((a) => a.id);
  const existing = Array.from(el.querySelectorAll("[data-active]")).map((x) => x.dataset.active);
  const same = ids.length > 0 && ids.length === existing.length && ids.every((id) => existing.includes(id));
  if (!same) {
    // rebuild only when the set of runs changed
    if (!STATE.active.length) { el.innerHTML = `<div class="card muted">No runs in progress.</div>`; return; }
    el.innerHTML = STATE.active.map(runCardHTML).join("");
  }
  // update in place (no innerHTML churn -> no scroll jump)
  STATE.active.forEach((a) => {
    const card = el.querySelector(`[data-active="${a.id}"]`);
    if (!card) return;
    paintGauges(card, a.latest || {});
    paintHeader(card, a);
    updatePhases(card, a.phases || []);
    paintConsole(card, a.id);
    drawRunChart(card, a.id);
  });
}

function phaseRow(p) {
  const ic = p.status === "running" ? "▶" : p.status === "warn" ? "!" : p.status === "error" ? "✕" : "✓";
  const t = (p.t0 && p.t1) ? dur(p.t1 - p.t0) : (p.t0 ? dur(Date.now() / 1000 - p.t0) : "");
  return `<div class="phase ${p.status === "running" ? "run" : p.status === "warn" ? "warn" : "ok"}">
    <span class="ic">${ic}</span><span class="g">${esc(p.grp)}</span>
    <span>${esc(p.label || "")}</span><span class="muted">${t}</span></div>`;
}

function paintGauges(card, v) {
  GAUGES.forEach(([k]) => {
    const e = card.querySelector(`[data-g="${k}"]`);
    if (e) e.textContent = fmt(v[k], 0);
  });
}

/* ------------------------------------------------------------ history --- */
const HIST_SORT = {
  host: (r) => r.host || "", preset: (r) => r.preset || "", label: (r) => r.label || "",
  started: (r) => r.started || 0, status: (r) => r.status || "",
  dur: (r) => (r.ended && r.started) ? (r.ended - r.started) : -1,
  gpu: (r) => numOr(r.summary && r.summary.gpu_soak_tflops, -1),
  cpu: (r) => numOr(r.summary && r.summary.cpu_soak_gflops, -1),
  ram: (r) => numOr(r.summary && r.summary.ram_soak_gbps, -1),
  peak: (r) => peakTemp(r) ?? -1,
};
const numOr = (v, d) => (v == null || isNaN(v)) ? d : +v;
function peakTemp(r) {
  const s = r.summary || {};
  const t = (s.gpu_soak_telemetry && s.gpu_soak_telemetry.temp_c) || (s.telemetry && s.telemetry.temp_c);
  return Array.isArray(t) ? t[t.length - 1] : null;
}
function sortArrow(key) {
  if (SORT.key !== key) return "";
  return `<span class="arw">${SORT.dir < 0 ? "▼" : "▲"}</span>`;
}
async function renderHistory() {
  try {
    const host = $("#histHost").value;
    const d = await api("/runs?limit=300" + (host ? "&host=" + encodeURIComponent(host) : ""));
    const q = ($("#histSearch").value || "").toLowerCase();
    const st = $("#histStatus").value;
    let rows = d.runs.filter((r) => !q || JSON.stringify(r).toLowerCase().includes(q));
    if (st) rows = rows.filter((r) => r.status === st);
    const get = HIST_SORT[SORT.key] || HIST_SORT.started;
    rows = rows.slice().sort((a, b) => {
      const x = get(a), y = get(b);
      if (typeof x === "string" || typeof y === "string") return String(x).localeCompare(String(y)) * SORT.dir;
      return (x - y) * SORT.dir;
    });
    $("#histCount").textContent = `${rows.length} run${rows.length === 1 ? "" : "s"}`;
    $$("#histTable th.sortable").forEach((th) => {
      th.innerHTML = th.textContent.replace(/[▲▼]\s*$/, "").trim() + sortArrow(th.dataset.sort);
    });
    const tb = $("#histTable tbody");
    tb.innerHTML = rows.map((r) => {
      const s = r.summary || {};
      const peak = peakTemp(r);
      return `<tr data-detail="${r.id}">
        <td>${esc(r.host)}</td>
        <td class="syscol" title="${esc(sysLine(r))}">${esc(sysLine(r))}</td>
        <td>${esc(r.preset || "—")}</td>
        <td>${esc(r.label || "")}</td>
        <td class="muted" title="${ts(r.started)}">${ago(r.started)}</td>
        <td class="num">${runDur(r)}</td>
        <td><span class="pill ${esc(r.status)}">${esc(r.status)}</span></td>
        <td class="num">${fmt(s.gpu_soak_tflops)}</td>
        <td class="num">${fmt(s.cpu_soak_gflops, 0)}</td>
        <td class="num">${fmt(s.ram_soak_gbps)}</td>
        <td class="num">${fmt(peak, 0)}</td></tr>`;
    }).join("") || `<tr><td colspan="11" class="empty">No matching runs.</td></tr>`;
    fillSelect("#histHost", HOSTS, "all hosts");
  } catch (e) { /* ignore */ }
}

/* ------------------------------------------------------------- trends --- */
async function renderTrends() {
  const msel = $("#trendMetric");
  if (!msel.options.length && STATE.metrics.length) {
    msel.innerHTML = STATE.metrics.map(([k, l]) => `<option value="${esc(k)}">${esc(l)}</option>`).join("");
    $("#trendMetric2").innerHTML = `<option value="">+ compare metric…</option>` +
      STATE.metrics.map(([k, l]) => `<option value="${esc(k)}">${esc(l)}</option>`).join("");
    fillSelect("#trendHost", HOSTS, "all hosts");
    fillSelect("#cmpA", [], ""); 
  }
  const host = $("#trendHost").value;
  const m1 = msel.value, m2 = $("#trendMetric2").value;
  if (!m1) return;
  const metrics = [m1, m2].filter(Boolean);
  try {
    const d = await api("/trends?host=" + encodeURIComponent(host) + metrics.map((m) => "&metric=" + encodeURIComponent(m)).join(""));
    const colors = ["#4c9aff", "#46c98d", "#e5b567"];
    const series = metrics.map((m, i) => ({
      name: labelFor(m), color: colors[i % colors.length],
      points: d.series[m].map((p) => ({ x: p.t, y: p.v })),
    }));
    drawChart($("#trendChart"), series, { xTime: true, yLabel: metrics.map(labelFor).join(" / ") });
    $("#trendLegend").innerHTML = series.map((s) =>
      `<span><i class="sw" style="background:${s.color}"></i>${esc(s.name)} · ${s.points.length} pts</span>`).join("");
  } catch (e) { /* ignore */ }
}
function labelFor(m) { const f = STATE.metrics.find((x) => x[0] === m); return f ? f[1] : m; }

/* ------------------------------------------------------------ compare --- */
function renderCompareOptions() {
  const finished = STATE.recent.filter((r) => r.status !== "running");
  const opts = finished.slice(0, 60).map((r) =>
    `<option value="${r.id}">${esc(r.host)} · ${esc(r.label || "")} · ${ts(r.started)}</option>`).join("");
  const a = $("#cmpA"), b = $("#cmpB");
  if (a.dataset.filled === String(finished.length)) return;
  a.innerHTML = opts; b.innerHTML = opts;
  if (finished.length > 1) { a.selectedIndex = 1; b.selectedIndex = 0; }
  a.dataset.filled = String(finished.length);
}
async function runCompare() {
  const A = $("#cmpA").value, B = $("#cmpB").value;
  if (!A || !B) return;
  const [ra, rb] = await Promise.all([api("/runs/" + A), api("/runs/" + B)]);
  const keys = [["gpu_soak_tflops", "GPU soak (TFLOPS)", 1], ["cpu_soak_gflops", "CPU soak (GFLOPS)", 0],
    ["ram_soak_gbps", "RAM soak (GB/s)", 1], ["disk_seqwrite_mbps", "Disk seq write (MB/s)", 0],
    ["net.download_mbps", "Network download (Mbps)", 0]];
  const get = (s, p) => p.split(".").reduce((o, k) => (o == null ? null : o[k]), s);
  const row = (label, a, b, dec) => {
    const d = (a != null && b != null && a !== 0) ? ((b - a) / Math.abs(a)) * 100 : null;
    const cls = d == null ? "flat" : d > 1 ? "up" : d < -1 ? "down" : "flat";
    return `<tr><td>${esc(label)}</td><td class="mono">${fmt(a, dec)}</td><td class="mono">${fmt(b, dec)}</td>
      <td class="mono delta ${cls}">${d == null ? "—" : (d > 0 ? "+" : "") + d.toFixed(1) + "%"}</td></tr>`;
  };
  $("#cmpResult").innerHTML = `
    <div class="card"><h3>${esc(ra.host)} · ${esc(ra.label || "")}</h3>
      <div class="muted">${ts(ra.started)} · ${esc(ra.gpu || "")}</div></div>
    <div class="card"><h3>${esc(rb.host)} · ${esc(rb.label || "")}</h3>
      <div class="muted">${ts(rb.started)} · ${esc(rb.gpu || "")}</div></div>
    <div class="card" style="grid-column:1/-1"><table>
      <thead><tr><th>metric</th><th>A</th><th>B</th><th>Δ</th></tr></thead>
      <tbody>${keys.map(([k, l, d]) => row(l, get(ra.summary, k), get(rb.summary, k), d)).join("")}</tbody>
    </table></div>`;
}

/* ------------------------------------------------------------ charts ---- */
function drawChart(cv, series, opt = {}) {
  const dpr = window.devicePixelRatio || 1;
  const w = cv.clientWidth, h = cv.height;
  cv.width = w * dpr; cv.height = h * dpr;
  const g = cv.getContext("2d"); g.scale(dpr, dpr); g.clearRect(0, 0, w, h);
  const pl = 52, pr = 18, pt = 18, pb = 28;
  const x0 = pl, y0 = h - pb, x1 = w - pr, y1 = pt;
  g.strokeStyle = "#232b37"; g.fillStyle = "#8592a6"; g.font = "11px monospace";
  let maxY = 1, minY = 0, n = 0;
  series.forEach((s) => s.points.forEach((p) => { maxY = Math.max(maxY, p.y); minY = Math.min(minY, p.y); n = Math.max(n, s.points.length); }));
  maxY = niceMax(maxY);
  for (let i = 0; i <= 4; i++) {
    const y = y0 - (y0 - y1) * i / 4;
    g.beginPath(); g.moveTo(x0, y); g.lineTo(x1, y); g.stroke();
    g.fillText(String(Math.round(minY + (maxY - minY) * i / 4)), 8, y + 4);
  }
  if (!n) { g.fillText("no data", x0 + 8, pt + 12); return; }
  const allX = [].concat(...series.map((s) => s.points.map((p) => p.x)));
  const xmin = Math.min(...allX), xmax = Math.max(...allX);
  const sx = (x) => x0 + (x1 - x0) * (xmax === xmin ? 0.5 : (x - xmin) / (xmax - xmin));
  const sy = (y) => y0 - (y0 - y1) * ((y - minY) / (maxY - minY));
  series.forEach((s) => {
    if (s.points.length < 2) return;
    g.strokeStyle = s.color; g.lineWidth = 2; g.beginPath();
    s.points.forEach((p, i) => { const X = sx(p.x), Y = sy(p.y); i ? g.lineTo(X, Y) : g.moveTo(X, Y); });
    g.stroke();
  });
  if (opt.xTime && xmax > xmin) {
    g.fillStyle = "#8592a6";
    [xmin, (xmin + xmax) / 2, xmax].forEach((t) => g.fillText(new Date(t * 1000).toLocaleDateString(), sx(t) - 26, h - 8));
  }
  cv._chart = { series, sx, xmin, xmax, opt };
  attachHover(cv);
}
function tipEl() {
  let t = $("#charttip");
  if (!t) { t = document.createElement("div"); t.id = "charttip"; t.className = "charttip"; document.body.appendChild(t); }
  return t;
}
function showTip(x, y, text) {
  const t = tipEl();
  t.textContent = text; t.style.display = "block";
  const w = t.offsetWidth || 170;
  t.style.left = Math.max(8, Math.min(x + 14, window.innerWidth - w - 12)) + "px";
  t.style.top = Math.max(8, y + 14) + "px";
}
function hideTip() { const t = $("#charttip"); if (t) t.style.display = "none"; }
function attachHover(cv) {
  if (cv._hover) return; cv._hover = true;
  cv.addEventListener("mousemove", (e) => {
    const ch = cv._chart;
    if (!ch || !ch.series || !ch.series.length) return;
    const mx = e.clientX - cv.getBoundingClientRect().left;
    let best = null, bd = Infinity;
    ch.series.forEach((s) => s.points.forEach((p) => {
      const d = Math.abs(ch.sx(p.x) - mx); if (d < bd) { bd = d; best = p.x; }
    }));
    if (best == null) return;
    const tol = (ch.xmax - ch.xmin) * 0.05 + 1e-9;
    const lines = [ch.opt && ch.opt.xTime ? new Date(best * 1000).toLocaleString()
                                           : "t = " + fmt(best, 0) + "s"];
    ch.series.forEach((s) => {
      let v = null, d = Infinity;
      s.points.forEach((p) => { const dd = Math.abs(p.x - best); if (dd < d) { d = dd; v = p.y; } });
      if (d <= tol) lines.push(s.name + "  " + fmt(v, 1));
    });
    showTip(e.clientX, e.clientY, lines.join("\n"));
  });
  cv.addEventListener("mouseleave", hideTip);
}
function niceMax(v) { const p = Math.pow(10, Math.floor(Math.log10(v || 1))); return Math.ceil((v || 1) / p) * p; }

function drawRunChart(card, runId) {
  const cv = card.querySelector(".runchart");
  if (!cv) return;
  const buf = (TEL[runId] || []).slice(-240);
  const preset = CHART[runId] || Object.keys(CHART_PRESETS)[0];
  const keys = CHART_PRESETS[preset] || [];
  const series = keys.map((k) => {
    const meta = SERIES[k] || [k, "#4c9aff"];
    return { name: meta[0], color: meta[1],
             points: buf.filter((b) => b[k] != null).map((b) => ({ x: b.elapsed, y: b[k] })) };
  });
  drawChart(cv, series);
}

/* ------------------------------------------------------------ actions --- */
async function startRun(runnerId) {
  const presetKeys = Object.keys(PRESETS);
  openModal(`<h3>Start run</h3>
    <div class="formrow"><label>Preset</label>
      <select id="mPreset">${presetKeys.map((k) => `<option>${esc(k)}</option>`).join("")}</select></div>
    <div class="formrow"><label>Groups</label><div class="chk" id="mGroups">
      ${GROUPS.map((g) => `<label><input type="checkbox" value="${g}" checked>${g}</label>`).join("")}</div></div>
    <div class="formrow"><label>Durations (s)</label>
      GPU soak <input id="mGpu" type="number" value="600" style="width:80px">
      combined <input id="mComb" type="number" value="300" style="width:80px">
      full soak <input id="mFull" type="number" value="300" style="width:80px">
      graphics <input id="mGfx" type="number" value="60" style="width:80px"></div>
    <div class="formrow" style="justify-content:flex-end">
      <button class="ghost" id="mCancel">cancel</button>
      <button class="accent" id="mGo">start</button></div>`);
  const applyPreset = () => {
    const p = PRESETS[$("#mPreset").value] || {};
    const groups = p.groups || gset(...ALL_GROUPS);
    $$("#mGroups input").forEach((i) => { i.checked = !!groups[i.value]; });
    const spin = p.spin || DEF_SPIN;
    $("#mGpu").value = spin.gpu; $("#mComb").value = spin.combined;
    $("#mFull").value = spin.fullsoak; $("#mGfx").value = spin.gfx;
  };
  $("#mPreset").onchange = applyPreset;
  applyPreset();
  $("#mCancel").onclick = closeModal;
  $("#mGo").onclick = async () => {
    const skip = $$("#mGroups input").filter((i) => !i.checked).map((i) => i.value);
    const payload = {
      label: $("#mPreset").value, preset: $("#mPreset").value, skip,
      gpu_soak: +$("#mGpu").value, combined: +$("#mComb").value,
      fullsoak: +$("#mFull").value, gfx: +$("#mGfx").value,
    };
    closeModal();
    try {
      await api("/runners/" + runnerId + "/commands", "POST", { kind: "start", payload });
      toast("run queued: " + payload.label);
      refresh();
    } catch (e) { toast("start failed: " + e.message, "err"); }
  };
}
async function cmd(runnerId, kind, payload) {
  try {
    await api("/runners/" + runnerId + "/commands", "POST", { kind, payload: payload || {} });
    toast(kind + " sent");
    refresh();
  } catch (e) { toast(kind + " failed: " + e.message, "err"); }
}
async function forgetRunner(id) {
  const r = STATE.runners.find((x) => x.id === id) || {};
  if (!confirm("Remove " + (r.label || r.name || id) + " from the fleet? Its run history is kept.")) return;
  try { await api("/runners/" + id, "DELETE"); toast("runner removed"); refresh(); }
  catch (e) { toast("remove failed: " + e.message, "err"); }
}
async function powerRunner(id, kind) {
  const r = STATE.runners.find((x) => x.id === id) || {};
  const name = r.label || r.name || id;
  const busy = STATE.active.some((a) => a.runner_id === id);
  const verb = kind === "reboot" ? "Reboot" : "Shut down";
  const extra = busy ? "\n\nA run is in progress and will be stopped first." : "";
  if (!confirm(verb + " " + name + "?" + extra + "\n\nThis powers off the machine.")) return;
  try {
    await api("/runners/" + id + "/commands", "POST", { kind, payload: {} });
    toast(verb.toLowerCase() + " sent to " + name);
    refresh();
  } catch (e) { toast(kind + " failed: " + e.message, "err"); }
}

async function showDetail(runId) {
  const r = await api("/runs/" + runId);
  const s = r.summary || {};
  const sy = r.system || {};
  const tel = await api("/runs/" + runId + "/telemetry?max=900");
  const pts = (n) => (tel.rows || []).filter((x) => x[n] != null).map((x) => ({ x: x.elapsed, y: x[n] }));
  const st = (r.telemetry_stats && r.telemetry_stats.stats) || {};
  const dv = (r.telemetry_stats && r.telemetry_stats.derived) || {};
  const prev = await findPrev(r);
  const ps = prev ? (prev.summary || {}) : {};
  const at = (o, path) => path.split(".").reduce((x, k) => (x == null ? null : x[k]), o);
  const dltOf = (path) => (prev ? dltHtml(at(s, path), at(ps, path)) : "");
  const mat = s.gpu_matmul_tflops || {}, cmat = s.cpu_matmul_gflops || {};
  const avgW = st.power_w && st.power_w.avg;
  const peak = st.temp_c ? st.temp_c.max : null;
  const tlim = st.temp_limit_c ? st.temp_limit_c.max : null;
  const headroom = (peak != null && tlim != null) ? Math.round(tlim - peak) + " °C" : "—";
  const perfW = (s.gpu_soak_tflops && avgW) ? (s.gpu_soak_tflops / avgW).toFixed(2) + " TF/W" : "—";
  const thr = st.throttle ? Math.round(st.throttle.avg * 100) + "%" : "—";
  const tiles = [
    ["GPU soak", fmt(s.gpu_soak_tflops) + " TF", dltOf("gpu_soak_tflops")],
    ["GPU fp16", fmt(mat.fp16) + " TF", dltOf("gpu_matmul_tflops.fp16")],
    ["GPU fp32", fmt(mat.fp32) + " TF", dltOf("gpu_matmul_tflops.fp32")],
    ["GPU bf16", fmt(mat.bf16) + " TF", dltOf("gpu_matmul_tflops.bf16")],
    ["CPU soak", fmt(s.cpu_soak_gflops, 0) + " GF", dltOf("cpu_soak_gflops")],
    ["CPU fp64", fmt(cmat.fp64, 0) + " GF", dltOf("cpu_matmul_gflops.fp64")],
    ["RAM soak", fmt(s.ram_soak_gbps) + " GB/s", dltOf("ram_soak_gbps")],
    ["Disk write", fmt(s.disk_seqwrite_mbps, 0) + " MB/s", dltOf("disk_seqwrite_mbps")],
    ["Network", fmt(s.net && s.net.download_mbps, 0) + " Mb/s", dltOf("net.download_mbps")],
    ["peak temp", fmt(peak, 0) + " °C", ""],
  ];
  const derived = [
    ["perf/watt", perfW],
    ["avg power", fmt(avgW, 0) + " W"],
    ["energy", dv.energy_j != null ? (dv.energy_j / 1000).toFixed(1) + " kJ" : "—"],
    ["thermal headroom", dv.thermal_headroom_c != null ? dv.thermal_headroom_c + " °C" : headroom],
    ["time throttled", dv.throttled_pct != null ? dv.throttled_pct + "%" : thr],
    ["clock residency", dv.clock_residency_pct != null ? dv.clock_residency_pct + "%" : "—"],
    ["power headroom", dv.power_headroom_w != null ? dv.power_headroom_w + " W" : "—"],
  ];
  const remarks = [];
  const v = s.fullsoak && s.fullsoak.result && s.fullsoak.result.verdict;
  if (v) remarks.push(v.clean ? "full-system soak: clean"
    : "full-system soak: NOT clean" + (v.exits ? " (exits " + JSON.stringify(v.exits) + ")" : "")
      + (v.throttle_active ? " · throttling" : ""));
  const warns = (r.events || []).filter((e) => e.level === "warn");
  warns.slice(0, 6).forEach((w) => remarks.push("warn: " + w.message));
  const sysrows = [
    ["host", r.host], ["os", sy.platform || r.platform], ["kernel", sy.kernel],
    ["cpu", sy.cpu], ["cores / threads", [sy.cores, sy.threads].filter(Boolean).join(" / ")],
    ["ram", sy.ram_gib ? sy.ram_gib + " GiB" : null], ["gpu", sy.gpu || r.gpu],
    ["driver", sy.driver || r.driver], ["vram", sy.vram],
    ["board", sy.board], ["bios", sy.bios && (sy.bios.version + " " + (sy.bios.date || ""))],
    ["torch", r.torch], ["git", r.git],
  ].filter((x) => x[1] != null && x[1] !== "");
  const statKeys = Object.keys(st).filter((k) => k !== "elapsed" && k !== "ts");
  const statRows = statKeys.map((k) => {
    const meta = SERIES[k] || [k, "#8592a6"];
    const a = st[k];
    return `<tr><td>${esc(meta[0])}</td><td class="num">${fmt(a.min, 1)}</td>
      <td class="num">${fmt(a.avg, 1)}</td><td class="num">${fmt(a.max, 1)}</td>
      <td class="num muted">${fmt(a.std, 2)}</td><td class="num">${fmt(a.p95, 1)}</td>
      <td class="num">${fmt(a.p99, 1)}</td><td class="muted mono">${a.n}</td></tr>`;
  }).join("");
  openModal(`<h3>${esc(r.host)} · ${esc(r.label || "run")} <span class="pill ${esc(r.status)}">${esc(r.status)}</span></h3>
    <div class="muted">${ts(r.started)} · ${runDur(r)} · ${esc(sy.gpu || r.gpu || "")} ${sy.driver || r.driver ? "· driver " + esc(sy.driver || r.driver) : ""}${prev ? ` · <span class="delta flat">vs ${ts(prev.started)}</span>` : ""}</div>
    ${(s.verdicts || []).length ? `<div class="remarks verdicts">${s.verdicts.map((x) =>
      `<div class="v-${esc(x.level)}"><b>${x.level === "good" ? "✓" : x.level === "warn" ? "!" : "✕"}</b> ${esc(x.text)}</div>`).join("")}</div>` : ""}
    ${remarks.length ? `<div class="remarks">${remarks.map((x) => `<div>${esc(x)}</div>`).join("")}</div>` : ""}
    <div class="kv" style="margin:14px 0">
      ${tiles.map(([k, val, d]) => `<div class="kvitem"><div class="k">${esc(k)}</div><div class="v">${esc(val)}${d || ""}</div></div>`).join("")}
    </div>
    <div class="kv" style="margin:0 0 14px">
      ${derived.map(([k, val]) => `<div class="kvitem"><div class="k">${esc(k)}</div><div class="v">${esc(val)}</div></div>`).join("")}
    </div>
    <div class="chartrow"><canvas id="dChart" height="220"></canvas>
      <select class="csel" id="dChartSel">${Object.keys(CHART_PRESETS).map((k) => `<option>${esc(k)}</option>`).join("")}</select></div>
    <h4 class="sechead">Results</h4>
    <table class="kvtable">${resultRows(s).map(([k, v]) => `<tr><th>${esc(k)}</th><td>${esc(String(v))}</td></tr>`).join("") || `<tr><td class="muted">no structured results</td></tr>`}</table>
    <h4 class="sechead">System</h4>
    <table class="kvtable">${sysrows.map(([k, val]) => `<tr><th>${esc(k)}</th><td>${esc(String(val))}</td></tr>`).join("")}</table>
    ${Object.keys(sy.inventory || {}).length ? `<details><summary class="muted">full inventory</summary>
      <pre class="mono muted invjson">${esc(JSON.stringify(sy.inventory, null, 1))}</pre></details>` : ""}
    <h4 class="sechead">Telemetry (min / avg / max · std · p95 / p99)</h4>
    <table class="statstable"><thead><tr><th>metric</th><th>min</th><th>avg</th><th>max</th><th>std</th><th>p95</th><th>p99</th><th>n</th></tr></thead>
      <tbody>${statRows || `<tr><td colspan="8" class="muted">no telemetry</td></tr>`}</tbody></table>
    <h4 class="sechead">Phases</h4>
    <div class="phases" style="max-height:300px">${r.phases.map(phaseRow).join("")}</div>
    ${r.logs && r.logs.length ? `<h4 class="sechead">Console</h4>
      <pre class="console mono">${esc(r.logs.map((l) => l.line).join("\n"))}</pre>` : ""}
    ${r.events && r.events.length ? `<details><summary class="muted">${r.events.length} log events</summary>
      <pre class="mono muted" style="max-height:200px;overflow:auto;font-size:11px">${esc(r.events.map((e) => e.message).join("\n"))}</pre></details>` : ""}
    <div class="formrow" style="gap:8px;flex-wrap:wrap;margin-top:12px">
      <span class="muted">downloads:</span>
      <a class="dl" href="/api/v1/runs/${runId}/telemetry.csv?token=${encodeURIComponent(TOKEN)}">telemetry.csv</a>
      ${(r.artifacts || []).map((a) => `<a class="dl" href="/api/v1/runs/${runId}/artifact/${encodeURIComponent(a.name)}?token=${encodeURIComponent(TOKEN)}">${esc(a.name)} <span class="muted">${a.size >= 1024 ? Math.round(a.size / 1024) + "K" : (a.size || 0) + "B"}</span></a>`).join("")}
    </div>
    <div class="formrow" style="justify-content:flex-end"><button class="ghost" onclick="closeModal()">close</button></div>`);
  let dpreset = Object.keys(CHART_PRESETS)[0];
  const drawDetail = () => {
    $("#dChartSel").value = dpreset;
    drawChart($("#dChart"), (CHART_PRESETS[dpreset] || []).map((k) => {
      const meta = SERIES[k] || [k, "#4c9aff"];
      return { name: meta[0], color: meta[1], points: pts(k) };
    }), {});
  };
  drawDetail();
  $("#dChartSel").onchange = (e) => { dpreset = e.target.value; drawDetail(); };
}

/* flatten a run summary into label/value rows so nothing measured stays hidden */
function resultRows(summary) {
  const skip = new Set(["host", "timestamp", "telemetry", "samples", "gpu_soak_telemetry"]);
  const rows = [];
  const walk = (prefix, obj) => {
    for (const [k, v] of Object.entries(obj || {})) {
      if (skip.has(k)) continue;
      const key = prefix ? prefix + "." + k : k;
      if (v && typeof v === "object" && !Array.isArray(v)) walk(key, v);
      else if (Array.isArray(v)) rows.push([key, v.join(" / ")]);
      else if (v !== null && v !== undefined && v !== "") rows.push([key, v]);
    }
  };
  walk("", summary);
  return rows;
}
function dltHtml(cur, prev) {
  if (cur == null || prev == null || isNaN(cur) || isNaN(prev) || +prev === 0) return "";
  const d = (+cur - +prev) / Math.abs(+prev) * 100;
  const cls = d > 1 ? "up" : d < -1 ? "down" : "flat";
  return ` <span class="delta ${cls}">${d > 0 ? "+" : ""}${d.toFixed(1)}%</span>`;
}
async function findPrev(r) {
  try {
    const d = await api("/runs?limit=200&host=" + encodeURIComponent(r.host));
    return d.runs.filter((x) => x.id !== r.id && x.preset === r.preset && x.started && r.started && x.started < r.started)
      .sort((a, b) => b.started - a.started)[0] || null;
  } catch (e) { return null; }
}

/* ----------------------------------------------------------- schedules --- */
async function renderSchedules() {
  try {
    const d = await api("/schedules");
    SCHEDULES = d.schedules || [];
  } catch (e) { return; }
  const rname = (id) => { const r = STATE.runners.find((x) => x.id === id); return r ? (r.label || r.name) + (r.uid ? " #" + r.uid : "") : String(id).slice(0, 8); };
  $("#schedTable tbody").innerHTML = SCHEDULES.map((s) => {
    const when = s.kind === "daily" ? "daily " + esc(s.at_time) : "every " + dur(s.every_s);
    return `<tr>
      <td>${esc(rname(s.runner_id))}</td><td>${esc(s.label)}</td><td>${when}</td>
      <td class="mono">${ts(s.next_run)}</td>
      <td class="muted">${s.last_run ? ts(s.last_run) : "—"}</td>
      <td><button class="danger" data-sched-del="${s.id}">delete</button></td></tr>`;
  }).join("") || `<tr><td colspan="6" class="muted" style="padding:16px">No schedules.</td></tr>`;
}

function newSchedule() {
  const opts = STATE.runners.map((r) => `<option value="${r.id}">${esc(r.label || r.name)}</option>`).join("");
  openModal(`<h3>New schedule</h3>
    <div class="formrow"><label>runner</label><select id="scRunner" style="flex:1">${opts}</select></div>
    <div class="formrow"><label>label</label><input id="scLabel" style="flex:1" value="scheduled run"></div>
    <div class="formrow"><label>repeat</label>
      <select id="scKind"><option value="every">every…</option><option value="daily">daily at…</option></select>
      <input id="scEvery" type="number" value="3600" style="width:90px"> seconds
      <input id="scAt" type="time" value="03:00" style="display:none"></div>
    <div class="formrow"><label>preset</label>
      <select id="scPreset">${Object.keys(PRESETS).map((k) => `<option>${esc(k)}</option>`).join("")}</select></div>
    <div class="formrow" style="justify-content:flex-end">
      <button class="ghost" onclick="closeModal()">cancel</button>
      <button class="accent" id="scGo">create</button></div>`);
  $("#scKind").onchange = () => {
    const daily = $("#scKind").value === "daily";
    $("#scAt").style.display = daily ? "" : "none";
    $("#scEvery").style.display = daily ? "none" : "";
  };
  $("#scGo").onclick = async () => {
    const p = PRESETS[$("#scPreset").value] || {};
    const profile = { label: $("#scPreset").value,
      skip: p.groups ? GROUPS.filter((g) => !p.groups[g]) : [] };
    if (p.spin) Object.assign(profile, { gpu_soak: p.spin.gpu, combined: p.spin.combined,
      fullsoak: p.spin.fullsoak, gfx: p.spin.gfx });
    await api("/schedules", "POST", {
      runner_id: $("#scRunner").value, label: $("#scLabel").value, profile,
      kind: $("#scKind").value, every_s: +$("#scEvery").value, at_time: $("#scAt").value });
    closeModal(); renderSchedules();
  };
}
async function deleteSchedule(id) { await api("/schedules/" + id, "DELETE"); renderSchedules(); }

/* -------------------------------------------------------------- modal --- */
function openModal(html) {
  $("#modalbox").innerHTML = html;
  $("#modal").classList.add("show");
  const f = $("#modalbox input, #modalbox select");
  if (f) setTimeout(() => { try { f.focus(); } catch (e) { /* ignore */ } }, 0);
}
function closeModal() { $("#modal").classList.remove("show"); }
window.closeModal = closeModal;
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") { if ($("#modal").classList.contains("show")) closeModal(); return; }
  if (e.key === "/" && !/^(INPUT|SELECT|TEXTAREA)$/.test(document.activeElement.tagName)) {
    e.preventDefault();
    $$("#tabs button").find((b) => b.dataset.tab === "history")?.click();
    const s = $("#histSearch"); if (s) s.focus();
  }
});
$("#modal").addEventListener("click", (e) => { if (e.target.id === "modal") closeModal(); });

/* -------------------------------------------------------------- wiring -- */
function fillSelect(sel, values, placeholder) {
  const el = $(sel);
  const cur = el.value;
  el.innerHTML = `<option value="">${esc(placeholder)}</option>` + values.map((v) => `<option value="${esc(v)}">${esc(v)}</option>`).join("");
  if (values.includes(cur)) el.value = cur;
}

document.addEventListener("click", (e) => {
  const th = e.target.closest("th.sortable");
  if (th) {
    const k = th.dataset.sort;
    const numeric = ["started", "dur", "gpu", "cpu", "ram", "peak"].includes(k);
    SORT = { key: k, dir: SORT.key === k ? -SORT.dir : (numeric ? -1 : 1) };
    renderHistory();
    return;
  }
  const t = e.target.closest("[data-tab],[data-start],[data-stop],[data-update],[data-fan],[data-detail],[data-sched-del],[data-toggle-off],[data-forget],[data-power]");
  if (!t) return;
  if (t.dataset.toggleOff !== undefined) {
    SHOW_OFFLINE = !SHOW_OFFLINE;
    localStorage.setItem("benchd_show_offline", SHOW_OFFLINE ? "1" : "0");
    renderTop(); renderRunners();
    return;
  }
  if (t.dataset.tab) {
    $$("#tabs button").forEach((b) => b.classList.toggle("active", b === t));
    $$(".tabpane").forEach((p) => p.classList.toggle("active", p.id === "tab-" + t.dataset.tab));
    if (t.dataset.tab === "history") renderHistory();
    if (t.dataset.tab === "trends") renderTrends();
    if (t.dataset.tab === "schedules") renderSchedules();
    return;
  }
  if (t.dataset.start) return startRun(t.dataset.start);
  if (t.dataset.stop) return cmd(t.dataset.stop, "stop");
  if (t.dataset.update) return cmd(t.dataset.update, "update");
  if (t.dataset.fan) return cmd(t.dataset.fan, "fan_curve", { on: true });
  if (t.dataset.detail) return showDetail(t.dataset.detail);
  if (t.dataset.schedDel) return deleteSchedule(t.dataset.schedDel);
  if (t.dataset.forget) return forgetRunner(t.dataset.forget);
  if (t.dataset.power) return powerRunner(t.dataset.power, t.dataset.powerkind);
});

$("#tokenBtn").onclick = askToken;
$("#schedNew").onclick = newSchedule;
$("#histRefresh").onclick = renderHistory;
$("#histHost").onchange = renderHistory;
$("#histStatus").onchange = renderHistory;
$("#histSearch").oninput = () => { clearTimeout(window._hs); window._hs = setTimeout(renderHistory, 200); };
$("#trendHost").onchange = renderTrends;
$("#trendMetric").onchange = renderTrends;
$("#trendMetric2").onchange = renderTrends;
$("#cmpRun").onclick = runCompare;
document.addEventListener("change", (e) => {
  const id = e.target && e.target.dataset && e.target.dataset.chart;
  if (!id) return;
  CHART[id] = e.target.value;
  const card = document.querySelector(`[data-active="${id}"]`);
  if (card) drawRunChart(card, id);
});
document.addEventListener("submit", async (e) => {
  const form = e.target.closest && e.target.closest(".term");
  if (!form) return;
  e.preventDefault();
  const input = form.querySelector("input");
  const cmd = (input.value || "").trim();
  if (!cmd) return;
  input.value = "";
  try {
    await api("/runners/" + form.dataset.exec + "/commands", "POST",
              { kind: "exec", payload: { cmd } });
    toast("sent to runner: " + cmd);
  } catch (err) { toast("exec failed: " + err, "err"); }
});
$("#modal").addEventListener("click", (e) => { if (e.target.id === "modal") closeModal(); });
window.addEventListener("resize", () => { renderActive(); });

/* ---------------------------------------------------------------- boot -- */
if (!TOKEN) askToken(); else { connect(); refresh(); }
setInterval(() => {
  if (STATE.active.length) { const st = _scrollState(); renderActive(); _restoreScroll(st); }
}, 5000);
