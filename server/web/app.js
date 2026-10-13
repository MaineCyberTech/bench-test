"use strict";
/* bench-test control plane — single pane of glass (vanilla JS, no build step) */

const GROUPS = ["gpu", "cpu", "ram", "disk", "combined", "fullsoak", "net", "nvenc", "llm", "fan", "sys", "gfx"];
const ALL_GROUPS = ["gpu", "cpu", "ram", "disk", "combined", "fullsoak", "net", "nvenc", "llm", "fan", "sys", "gfx"];
const gset = (...on) => Object.fromEntries(ALL_GROUPS.map((g) => [g, on.includes(g) ? 1 : 0]));
const DEF_SPIN = { gpu: 600, combined: 300, fullsoak: 300, gfx: 60 };
// Tuning knobs the runner accepts but the four duration boxes do not cover. Left
// at these defaults a run inherits the runner's long values (cpu 120s, ram 120s,
// gpu matrix 12288, steady 10GB); a preset overrides them via `tune`.
const TUNE_DEFAULT = { cpu_soak: 120, ram_soak: 120, disk_seconds: 15, disk_size_mb: 2048,
  gpu_size: 12288, cpu_size: 4096, ram_size_mb: 1024, passes: 2, steady_gb: 10, stable: 0,
  stable_hold: 60, gfx_engine: "furmark" };
const PRESETS = {
  "Quick check (short soaks)": { groups: gset("gpu", "cpu", "ram", "disk", "combined", "fullsoak", "net", "nvenc", "sys", "gfx"), spin: { gpu: 60, combined: 60, fullsoak: 60, gfx: 30 }, tune: { ...TUNE_DEFAULT, cpu_soak: 60, ram_soak: 30, disk_seconds: 15, disk_size_mb: 256, gpu_size: 4096, cpu_size: 2048, ram_size_mb: 512, passes: 1, steady_gb: 2 } },
  "Stabilized full soak (15 min)": { groups: gset("gpu", "cpu", "ram", "disk", "combined", "fullsoak", "net", "nvenc", "fan", "sys", "gfx"), spin: { gpu: 180, combined: 45, fullsoak: 90, gfx: 30 }, tune: { ...TUNE_DEFAULT, cpu_soak: 45, ram_soak: 30, disk_seconds: 30, disk_size_mb: 512, gpu_size: 4096, cpu_size: 2048, ram_size_mb: 512, passes: 1, steady_gb: 2, stable: 1, stable_hold: 60, gfx_engine: "furmark" } },
  "System info + 60s soak (all)": { groups: gset("sys", "fullsoak"), spin: { ...DEF_SPIN, fullsoak: 60 } },
  "Full run (defaults)": { groups: gset(...ALL_GROUPS), spin: { ...DEF_SPIN } },
  "GPU compute": { groups: gset("gpu", "sys"), spin: { ...DEF_SPIN, gpu: 60 } },
  "CPU + RAM + disk": { groups: gset("cpu", "ram", "disk", "sys"), spin: { ...DEF_SPIN } },
  "Storage + network": { groups: gset("disk", "net", "sys"), spin: { ...DEF_SPIN } },
  "Video / graphics": { groups: gset("nvenc", "gfx", "sys"), spin: { ...DEF_SPIN } },
  "Graphics only (FurMark/Valley)": { groups: gset("gfx", "sys"), spin: { ...DEF_SPIN, gfx: 30 }, tune: { ...TUNE_DEFAULT, gfx_engine: "furmark" } },
  "LLM inference (Ollama)": { groups: gset("llm", "sys"), spin: { ...DEF_SPIN } },
  "Fan response": { groups: gset("fan", "sys"), spin: { ...DEF_SPIN } },
  "Soak only (long)": { groups: gset("gpu", "fullsoak", "sys"), spin: { ...DEF_SPIN, gpu: 1800, fullsoak: 1800 } },
  "Full-system soak (long)": { groups: gset("fullsoak", "sys"), spin: { ...DEF_SPIN, fullsoak: 1800 } },
};
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
  gpu_3d_pct: ["3D engine %", "#68d391"], gpu_copy_pct: ["copy engine %", "#f6ad55"],
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
  "3D · copy engines": ["gpu_util", "gpu_3d_pct", "gpu_copy_pct"],
};
const GAUGES = [["temp_c", "temp °C"], ["power_w", "power W"], ["gpu_util", "GPU %"],
                ["fan_pct", "fan %"], ["mem_util", "VRAM %"], ["cpu_temp_c", "CPU °C"]];
// telemetry tables are easier to scan with the load/thermal metrics first
const STAT_ORDER = ["temp_c", "temp_limit_c", "throttle", "throttle_bits", "power_w",
  "power_limit_w", "gpu_util", "sm_mhz", "mem_mhz", "gr_mhz", "vram_used_mib", "mem_util",
  "fan_pct", "gpu_fan_rpm", "temp_mem_c", "cpu_util", "cpu_temp_c", "cpu_max_core_c",
  "cpu_pkg_w", "cpu_mhz", "ram_used", "swap_used_gib", "load1", "nvme_c", "disk_read_mbs",
  "disk_write_mbs", "net_rx_mbs", "net_tx_mbs", "pcie_gen", "pcie_width"];
function orderedStatKeys(keys) {
  const rank = (k) => { const i = STAT_ORDER.indexOf(k); return i < 0 ? 999 : i; };
  return keys.slice().sort((a, b) => rank(a) - rank(b) || a.localeCompare(b));
}
// bars in the live focus view (mirrors the TUI telemetry panel, wider); the 4th
// entry is the full-scale value used for the bar width. Labels carry their unit
// so "cpu %" / "cpu °C" / "cpu W" are not three identical "cpu" rows.
const FOCUS_BARS = [
  ["temp_c", "gpu °C", "°C", 100], ["fan_pct", "gpu fan", "%", 100],
  ["power_w", "gpu W", "W", 400], ["gpu_util", "gpu %", "%", 100],
  ["mem_util", "vram %", "%", 100], ["vram_used_mib", "vram", "MiB", 16384],
  ["sm_mhz", "sm clk", "MHz", 3000], ["mem_mhz", "mem clk", "MHz", 12000],
  ["cpu_util", "cpu %", "%", 100], ["cpu_temp_c", "cpu °C", "°C", 100],
  ["cpu_pkg_w", "cpu W", "W", 200], ["ram_used", "ram", "GiB", 64],
  ["load1", "load", "", 16], ["nvme_c", "nvme °C", "°C", 100],
  ["disk_write_mbs", "disk W", "MB/s", 2000], ["net_rx_mbs", "net rx", "MB/s", 1250],
  // the rest of DASH_FIELDS, so every metric the TUI can show has a bar here
  ["disk_read_mbs", "disk R", "MB/s", 2000], ["net_tx_mbs", "net tx", "MB/s", 1250],
  ["gpu_fan_rpm", "gpu rpm", "rpm", 3000], ["cpu_fan_rpm", "cpu fan", "rpm", 3000],
  ["gpu_enc_pct", "enc %", "%", 100], ["gpu_dec_pct", "dec %", "%", 100],
  ["temp_mem_c", "vram °C", "°C", 110], ["temp_limit_c", "gpu limit", "°C", 110],
  ["cpu_max_core_c", "core max", "°C", 100], ["psi_io", "io stall", "%", 100],
  ["retired", "retired", "", 1],
];
const HOT_BARS = ["temp_c", "cpu_temp_c", "nvme_c", "temp_mem_c"];
// small sparkline panel under the main chart, filling the telemetry column
const FOCUS_MINI = [
  ["temp_c", "temp °C"], ["power_w", "power W"], ["gpu_util", "GPU %"],
  ["sm_mhz", "SM MHz"], ["mem_util", "VRAM %"], ["fan_pct", "fan %"],
  ["cpu_util", "CPU %"], ["cpu_temp_c", "CPU °C"], ["cpu_pkg_w", "CPU W"],
  ["ram_used", "RAM GiB"], ["disk_write_mbs", "disk W MB/s"], ["net_rx_mbs", "net rx MB/s"],
];

let SHOW_OFFLINE = localStorage.getItem("benchd_show_offline") === "1";
let SORT = { key: "started", dir: -1 };

let TOKEN = new URLSearchParams(location.search).get("token") || localStorage.getItem("benchd_token") || "";
let STATE = { runners: [], active: [], recent: [], metrics: [] };
let TEL = {};               // run_id -> [sample dicts]
let LOGS = {};              // run_id -> [line strings]
let CHART = {};             // run_id -> selected chart preset
let FOCUS = null;           // run_id shown in the full-pane live view
let HOSTS = [];
let SCHEDULES = [];
let es = null;

/* ------------------------------------------------------------------ dom -- */
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = (v, d = 1) => (v == null || isNaN(v)) ? "—" : (+v).toFixed(d);
const dur = (s) => s == null ? "-" : (s < 60 ? s.toFixed(0) + "s"
  : s < 3600 ? Math.floor(s / 60) + "m" + String(Math.round(s % 60)).padStart(2, "0") + "s"
  : Math.floor(s / 3600) + "h" + String(Math.round((s % 3600) / 60)).padStart(2, "0") + "m");
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
const numOrNull = (v) => (v == null || v === "" || isNaN(v)) ? null : +v;
function median(vals) {
  const v = vals.filter((x) => x != null).map(Number).sort((a, b) => a - b);
  if (!v.length) return null;
  const m = v.length >> 1;
  return v.length % 2 ? v[m] : (v[m - 1] + v[m]) / 2;
}
function gpuOf(r) { return ((r.system && r.system.gpu) || r.gpu || "?").replace(/NVIDIA GeForce\s*/i, ""); }
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
  if (FOCUS === d.run_id) { const f = $("#focus"); if (f) paintConsole(f, d.run_id); }
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
    HOSTS = (s.hosts && s.hosts.length) ? s.hosts
      : [...new Set(STATE.recent.map((r) => r.host).filter(Boolean))].sort();
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
  api("/runs/" + id + "/telemetry?max=4000").then((d) => {
    TEL[id] = (d.rows || []).filter((x) => x.elapsed != null);
    renderActive();
  }).catch(() => {});
}

/* Re-read a run's telemetry while the live view is open: the SSE stream is the
   fast path, this is the authoritative one (a batch can be missed). */
function reloadTel(id) {
  api("/runs/" + id + "/telemetry?max=4000").then((d) => {
    const rows = (d.rows || []).filter((x) => x.elapsed != null);
    if (rows.length) TEL[id] = rows;
    if (FOCUS === id) {
      const f = $("#focus");
      if (f) { paintFocusStats(f, id); drawRunChart(f, id); }
    }
  }).catch(() => {});
}

function onTelemetry(d) {
  const buf = TEL[d.run_id] || (TEL[d.run_id] = []);
  // the server publishes the whole posted batch ("samples"); fall back to a
  // single "sample" for compatibility
  const batch = (d.samples && d.samples.length) ? d.samples : (d.sample ? [d.sample] : []);
  if (!batch.length) return;
  for (const s of batch) if (s && typeof s === "object") buf.push(s);
  if (buf.length > 900) buf.splice(0, buf.length - 900);
  const last = batch[batch.length - 1];
  const card = document.querySelector(`[data-active="${d.run_id}"]`);
  if (card) { paintGauges(card, last); drawRunChart(card, d.run_id); }
  if (FOCUS === d.run_id) {
    const f = $("#focus");
    if (f) { paintGauges(f, last); paintFocusBars(f, last); paintFocusStats(f, d.run_id); drawRunChart(f, d.run_id); }
  }
}

function onPhase(p) {
  const card = document.querySelector(`[data-active="${p.run_id}"]`);
  if (card) queueRefresh(300);
}

/* -------------------------------------------------------------- render -- */
function renderAll() {
  renderTop(); renderRunners(); renderActive(); renderHistory(); renderTrends();
  renderCompareOptions(); renderSchedules(); renderFocus();
  const fp = $("#tab-fleet");
  if (fp && fp.classList.contains("active")) renderFleet();
}

function machineKey(r) { return r.machine_id || ("name:" + (r.name || "")); }

function renderTop() {
  const online = STATE.runners.filter((r) => r.online).length;
  const machines = new Set(STATE.runners.map(machineKey)).size;
  const stale = STATE.runners.filter((r) => r.update_available).length;
  $("#topstats").innerHTML = [
    ["machines", machines], ["runners", STATE.runners.length], ["online", online],
    ["active runs", STATE.active.length], ["history", STATE.recent.length],
  ].concat(stale ? [["stale", stale]] : [])
    .map(([k, v]) => `<div class="tstat"><div class="v">${v}</div><div class="k">${k}</div></div>`).join("");
  document.title = STATE.active.length ? `(${STATE.active.length}) bench-test` : "bench-test control plane";
  const sub = document.querySelector(".brand .sub");
  if (sub) sub.textContent = "control plane · v" + (STATE.version || "?") + " · single pane of glass";
  const off = STATE.runners.length - online;
  $("#runnerCount").innerHTML = `${online} online · ${machines} machine${machines === 1 ? "" : "s"}` +
    (off ? ` · <button class="badge toggle" data-toggle-off>${SHOW_OFFLINE ? "hide" : "show"} ${off} offline</button>` : "");
  $("#activeCount").textContent = `${STATE.active.length} running`;
}

function lastRunOf(rid) {
  // a finished run: the running one is the "now" row, and calling it the last run
  // reads as if the machine had already stopped
  return STATE.recent.filter((a) => a.runner_id === rid && a.status !== "running")
    .sort((a, b) => (b.started || 0) - (a.started || 0))[0] || null;
}

/* The at-a-glance block on a runner panel: what it is doing right now, and what its
   last run achieved. Both come from data the page already has, so nothing extra is
   fetched to render the fleet. */
function runnerInfo(r) {
  const rows = [];
  const live = STATE.active.find((a) => a.id === r.current_run);
  if (live) {
    const v = live.latest || {};
    const now = [v.temp_c != null ? fmt(v.temp_c, 0) + " °C" : null,
                 v.gpu_util != null ? fmt(v.gpu_util, 0) + "% gpu" : null,
                 v.power_w != null ? fmt(v.power_w, 0) + " W" : null,
                 v.cpu_util != null ? fmt(v.cpu_util, 0) + "% cpu" : null]
      .filter(Boolean).join(" · ");
    if (now) {
      rows.push(["current run", `${esc(live.label || "run")} · ${dur(Date.now() / 1000 - (live.started || 0))} elapsed`
        + `<div class="muted mono">${esc(now)}</div>`]);
    }
  }
  const last = lastRunOf(r.id);
  if (last) {
    const s = last.summary || {};
    const bits = [];
    if (s.gpu_soak_tflops != null) bits.push(fmt(s.gpu_soak_tflops, 1) + " TFLOPS soak");
    if (s.cpu_soak_gflops != null) bits.push(fmt(s.cpu_soak_gflops, 0) + " GFLOPS cpu");
    if (s.gfx && s.gfx.score != null) bits.push("gfx " + fmt(s.gfx.score, 0));
    if (s.ram_soak_gbps != null) bits.push(fmt(s.ram_soak_gbps, 1) + " GB/s ram");
    rows.push(["last run", `${esc(last.label || "run")} · ${esc(last.status || "")} · `
      + `${ago(last.ended || last.started)} · ${runDur(last)}${bits.length ? `<div class="muted mono">${bits.join(" · ")}</div>` : ""}`]);
  } else {
    rows.push(["last run", "none yet"]);
  }
  return `<div class="rinfo">${rows.map(([k, v]) =>
    `<div class="rrow"><span class="rk">${esc(k)}</span><span class="rv">${v}</span></div>`).join("")}</div>`;
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
    (caps.groups && caps.groups.length && caps.groups.length < ALL_GROUPS.length)
      ? `<span class="badge" title="groups this runner implements">${caps.groups.length}/${ALL_GROUPS.length} groups</span>`
      : "",
    (caps.toolkit && caps.toolkit.version)
      ? `<span class="badge" title="toolkit release + build">v${esc(caps.toolkit.version)}${(caps.toolkit.build || caps.toolkit.commit) ? "+" + esc(caps.toolkit.build || caps.toolkit.commit) : ""}</span>` : "",
    (caps.toolkit && caps.toolkit.version && !(caps.toolkit.build || caps.toolkit.commit))
      ? `<span class="badge stale" title="this copy does not record the commit it was fetched at">build unknown</span>` : "",
    (caps.toolkit && STATE.protocol != null && caps.toolkit.protocol !== STATE.protocol)
      ? `<span class="badge stale" title="runner speaks protocol ${caps.toolkit.protocol == null ? "unknown (older toolkit)" : esc(String(caps.toolkit.protocol))}, control plane speaks ${esc(String(STATE.protocol))} - update it">protocol mismatch</span>` : "",
    r.update_available
      ? `<span class="badge stale" title="${esc(r.toolkit_status || "behind")}: toolkit ${esc(r.toolkit_commit || "?")} vs latest ${esc(r.latest_commit || STATE.latest_git || "?")}">update available</span>`
      : (r.toolkit_commit
        ? `<span class="badge" title="toolkit ${esc(r.toolkit_commit)} (${esc(r.toolkit_status || "unknown")} vs latest ${esc(r.latest_commit || STATE.latest_git || "?")})">${esc(r.toolkit_commit)}</span>` : ""),
  ].join("");
  const dev = [caps.gpu || "no GPU", caps.cpu, caps.ram_gib ? caps.ram_gib + " GiB" : "",
               caps.driver ? "driver " + caps.driver : ""].filter(Boolean).join(" · ");
  return `<div class="runner ${r.online ? "" : "off"} ${r.status === "busy" ? "busy" : ""}" data-runner="${esc(r.id)}">
      <div class="rhead">
        <span class="statusp ${cls}"></span>
        <div style="flex:1;min-width:0"><div class="rname">${esc(r.label || r.name)}</div></div>
      </div>
      <div class="rmeta row">${badges}</div>
      <div class="rmeta">${esc(dev || "no hardware detail")}</div>
      ${phase ? `<div class="phasechip"><span class="d"></span>${esc(phase.grp)} · ${esc(phase.label || "")}</div>` : ""}
      ${runnerInfo(r)}
      <div class="ract" style="margin-top:12px">
        <button class="accent" data-rtab="${esc(r.id)}" title="Open the live view for this runner">live view</button>
        <button class="accent" data-start="${esc(r.id)}" ${disabled}>Start run</button>
        <button data-stop="${esc(r.id)}" ${run ? "" : "disabled"}>Stop</button>
        <button class="ghost" data-update="${esc(r.id)}" ${disabled}>Update</button>
        <button class="ghost" data-fan="${esc(r.id)}" ${disabled}>Fan curve</button>
        <button class="ghost power" data-iso="${esc(r.id)}" ${disabled}
                title="Update the toolkit / fetch + write a new ISO">update…</button>
        <button class="ghost power" data-power="${esc(r.id)}" data-powerkind="reboot" ${disabled}
                title="Reboot this machine">reboot</button>
        <button class="ghost power" data-power="${esc(r.id)}" data-powerkind="shutdown" ${disabled}
                title="Power off this machine">shut down</button>
        <button class="ghost forget" data-forget="${esc(r.id)}" title="Remove this runner from the fleet">forget</button>
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
  return `<div class="runbox" data-active="${esc(a.id)}">
      <div class="rh">
        <span class="statusp busy"></span>
        <div class="h">${esc(a.host)} · ${esc(a.label || "run")}</div>
        <span class="muted mono elapsed"></span>
        <span class="muted mono sysline">${esc(sysLine(a))}</span>
        <div style="margin-left:auto" class="ract">
          <button class="ghost" data-focus="${esc(a.id)}">live view</button>
          <button class="danger" data-stop="${esc(a.runner_id)}">Stop</button>
          <button class="ghost" data-detail="${esc(a.id)}">details</button>
        </div>
      </div>
      <div class="gauges">
        ${GAUGES.map(([k, label]) => `<div class="gauge"><div class="gv" data-g="${k}">—</div><div class="gk">${esc(label)}</div></div>`).join("")}
      </div>
      <div class="fhealth" data-h="health"></div>
      <div class="chartrow">
        <canvas class="runchart" height="180"></canvas>
        <select class="csel" data-chart="${a.id}">${Object.keys(CHART_PRESETS).map((k) => `<option${k === preset ? " selected" : ""}>${esc(k)}</option>`).join("")}</select>
      </div>
      <div class="chartlegend"></div>
      <div class="phases"></div>
      <div class="consolewrap">
        <div class="chead"><span class="muted">live console</span>
          <form class="term" data-exec="${esc(a.runner_id)}">
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
    paintHealth(card.querySelector("[data-h=health]"), a.latest || {});
    paintHeader(card, a);
    updatePhases(card, a.phases || []);
    paintConsole(card, a.id);
    drawRunChart(card, a.id);
  });
}

function phaseRow(p) {
  const skipped = /\bskip(ped)?\b/i.test(p.label || "");
  const bad = p.status === "failed" || p.status === "error";
  const ic = skipped ? "–" : p.status === "running" ? "▶" : p.status === "warn" ? "!" :
    bad ? "✕" : "✓";
  const cls = skipped ? "skip" : p.status === "running" ? "run" :
    p.status === "warn" ? "warn" : bad ? "bad" : "ok";
  const t = skipped ? "" : ((p.t0 && p.t1) ? dur(p.t1 - p.t0) : (p.t0 ? dur(Date.now() / 1000 - p.t0) : ""));
  return `<div class="phase ${cls}">
    <span class="ic">${ic}</span><span class="g">${esc(p.grp)}</span>
    <span>${esc(p.label || "")}</span><span class="muted">${t}</span></div>`;
}

function paintGauges(card, v) {
  GAUGES.forEach(([k]) => {
    const e = card.querySelector(`[data-g="${k}"]`);
    if (e) e.textContent = fmt(v[k], 0);
  });
}

/* ---------------------------------------------------------- live focus -- */
/* A full-pane view of one active run: the TUI's panels (box status/specs, live
   telemetry bars, charts, phases, console) laid out wide and updated in place. */
function focusRun(id) {
  FOCUS = id;
  const el = $("#focus");
  if (el) { el.dataset.run = ""; el.classList.add("show"); }
  renderFocus();
}

function closeFocus() {
  FOCUS = null;
  const el = $("#focus");
  if (el) { el.classList.remove("show"); el.innerHTML = ""; el.dataset.run = ""; }
}

/* Which run a runner tab should show: its live run when it has one, otherwise its
   most recent, so the pane stays useful for a machine that has just finished. */
function runnerRun(rid) {
  const live = STATE.active.find((a) => a.runner_id === rid);
  if (live) return { run: live, live: true };
  const past = STATE.recent.filter((a) => a.runner_id === rid)
    .sort((a, b) => (b.started || 0) - (a.started || 0))[0];
  return past ? { run: past, live: false } : null;
}

/* The runner tabs: switch the live view between machines without going back to the
   fleet table. Rebuilt on every paint so a run finishing/starting updates the dots. */
function focusTabsInner(cur) {
  const order = [...STATE.runners].sort((a, b) =>
    (b.online ? 1 : 0) - (a.online ? 1 : 0) || String(a.name).localeCompare(String(b.name)));
  return order.map((rr) => {
    const found = runnerRun(rr.id);
    const on = found && cur && found.run.id === cur.id;
    const title = found
      ? `${rr.name} — ${found.live ? "live run" : "last run"}: ${found.run.label || ""}`
      : `${rr.name} — no run yet`;
    return `<button class="rtab${on ? " on" : ""}${found && found.live ? " live" : ""}"
      data-rtab="${esc(rr.id)}" title="${esc(title)}"${found ? "" : " disabled"}>
      <span class="dot ${rr.online ? "up" : "down"}"></span>${esc(rr.name)}
      ${found && !found.live ? `<span class="muted">last</span>` : ""}</button>`;
  }).join("");
}

function focusShell(r, live) {
  const preset = CHART[r.id] || Object.keys(CHART_PRESETS)[0];
  const sel = (id) => Object.keys(CHART_PRESETS).map((k) =>
    `<option${k === (CHART[id] || Object.keys(CHART_PRESETS)[0]) ? " selected" : ""}>${esc(k)}</option>`).join("");
  return `<div class="rtabs" data-f="rtabs">${focusTabsInner(r)}</div>
    <div class="focushead">
      <span class="statusp ${live ? "busy" : "idle"}"></span>
      <div class="h">${esc(r.host)} · ${esc(r.label || "run")}</div>
      <span class="pill ${esc(r.status || "")}">${esc(r.status || "")}</span>
      <span class="muted mono" data-f="elapsed"></span>
      <span class="muted mono" data-f="phase"></span>
      <span class="muted mono">${esc(sysLine(r))}</span>
      <span class="muted mono" data-f="runner"></span>
      <div style="margin-left:auto" class="ract">
        ${live ? `<button class="ghost" data-f="update">update</button>
        <button class="danger" data-stop="${esc(r.runner_id)}">Stop</button>` : ""}
        <button class="ghost" data-f="detail">details</button>
        <button class="ghost" data-f="close">close ✕</button>
      </div>
    </div>
    <div class="focusbody">
      <div class="fgrid">
        <div class="fcard"><h4>Box status &amp; specs</h4><div class="fk" data-f="specs"></div></div>
        <div class="fcard"><h4>Live</h4><div data-f="bars"></div>
          <h4>Gauges</h4>
          <div class="gauges" data-f="gauges">${GAUGES.map(([k, label]) =>
            `<div class="gauge"><div class="gv" data-g="${k}">—</div><div class="gk">${esc(label)}</div></div>`).join("")}</div>
          <h4>Health</h4><div class="fhealth" data-f="health"></div>
          <h4>Temps</h4><div class="fhealth" data-f="temps"></div>
        </div>
        <div class="fcard"><h4>Telemetry</h4>
          <div class="chartrow"><canvas class="runchart" height="200"></canvas>
            <select class="csel" data-chart="${esc(r.id)}">${sel(r.id)}</select></div>
          <div class="chartlegend"></div>
          <div class="minigrid" data-f="minis">${FOCUS_MINI.map(([k, lbl]) =>
            `<div class="mini"><div class="ml"><span>${esc(lbl)}</span>
             <span class="mv" data-m="${k}">—</span></div>
             <canvas data-mini="${k}" height="56"></canvas></div>`).join("")}</div>
        </div>
      </div>
      <div class="fcard" style="margin-top:14px"><h4>Telemetry stats (min / avg / max · n)</h4>
        <div class="statcols" data-f="stats"></div>
      </div>
      <div class="fgrid two">
        <div class="fcard"><h4>Phases</h4><div class="phases" data-f="phases"></div></div>
        <div class="fcard"><h4>Console</h4>
          ${live ? `<form class="term" data-exec="${esc(r.runner_id)}">
            <input placeholder="run a command on this runner…" autocomplete="off">
            <button class="accent" type="submit">run</button></form>` : ""}
          <pre class="console mono" data-f="console"></pre>
        </div>
      </div>
    </div>`;
}

function renderFocus() {
  const el = $("#focus");
  if (!el) return;
  if (!FOCUS) { el.classList.remove("show"); return; }
  const live = STATE.active.find((x) => x.id === FOCUS);
  const r = live || STATE.recent.find((x) => x.id === FOCUS);
  if (!r) { el.classList.remove("show"); return; }
  if (el.dataset.run !== FOCUS) {
    el.dataset.run = FOCUS;
    el.innerHTML = focusShell(r, !!live);
    el.classList.add("show");
  }
  paintFocus(el, r, !!live);
}

function paintFocus(el, r, live) {
  const now = Date.now() / 1000;
  const e = el.querySelector("[data-f=elapsed]");
  if (e) e.textContent = r.started ? dur((r.ended || now) - r.started) + (live ? " elapsed" : " total") : "";
  const pill = el.querySelector(".pill");
  if (pill) { pill.textContent = r.status || ""; pill.className = "pill " + (r.status || ""); }
  const run = el.querySelector("[data-f=runner]");
  if (run) {
    const rr = STATE.runners.find((x) => x.id === r.runner_id) || {};
    run.textContent = [rr.addr, rr.kind, rr.toolkit_commit ? "toolkit " + rr.toolkit_commit : null,
                       (rr.capabilities && rr.capabilities.toolkit && rr.capabilities.toolkit.version)
                         ? "v" + rr.capabilities.toolkit.version : null]
      .filter(Boolean).join(" · ");
  }
  const tabs = el.querySelector("[data-f=rtabs]");
  if (tabs) tabs.innerHTML = focusTabsInner(r);
  const ph = el.querySelector("[data-f=phase]");
  if (ph) {
    const ps = r.phases || [];
    const cur = ps.filter((p) => p.status === "running").slice(-1)[0] ||
                ps.slice(-1)[0];
    ph.textContent = cur ? `${cur.grp} · ${cur.label || ""}` : "";
  }
  const v = r.latest || {};
  paintFocusSpecs(el, r);
  paintGauges(el, v);
  paintFocusBars(el, v);
  paintFocusHealth(el, v);
  paintFocusStats(el, r.id);
  drawRunChart(el, r.id);
  drawMiniCharts(el, r.id);
  updatePhases(el, r.phases || []);
  paintConsole(el, r.id);
}

function paintFocusSpecs(el, r) {
  const sy = r.system || {};
  const rr = STATE.runners.find((x) => x.id === r.runner_id) || {};
  const rows = [
    ["host", r.host], ["platform", sy.platform || r.platform], ["kernel", sy.kernel],
    ["cpu", sy.cpu], ["cores", [sy.cores, sy.threads].filter(Boolean).join(" / ")],
    ["ram", sy.ram_gib ? sy.ram_gib + " GiB" : null],
    ["gpu", sy.gpu || r.gpu], ["driver", sy.driver || r.driver], ["vram", sy.vram],
    ["board", sy.board], ["bios", sy.bios && (sy.bios.version + " " + (sy.bios.date || ""))],
    ["torch", r.torch], ["git", r.git],
    ["preset", r.preset], ["label", r.label],
    ["runner", rr.label || rr.name], ["addr", rr.addr], ["kind", rr.kind],
    ["version", rr.version],
    ["toolkit", rr.toolkit_commit ? rr.toolkit_commit + " (" + (rr.toolkit_mode || "?") + ")" : null],
    ["tk ver", (rr.capabilities && rr.capabilities.toolkit && rr.capabilities.toolkit.version) || null],
    ["started", r.started ? ts(r.started) : null],
    ["peak", (() => {
      // the true peak of this run's samples, not the newest one: r.latest is a
      // single row, so calling it "peak" disagreed with the details panel
      const rows = TEL[r.id] || [];
      let mx = null;
      for (const x of rows) if (x.temp_c != null && (mx == null || x.temp_c > mx)) mx = x.temp_c;
      return mx != null ? fmt(mx, 0) + " °C" : null;
    })()],
  ].filter((x) => x[1] != null && x[1] !== "");
  const tb = el.querySelector("[data-f=specs]");
  if (tb) tb.innerHTML = rows.map(([k, val]) =>
    `<div class="fkr"><span class="fkk">${esc(k)}</span><span class="fkv">${esc(String(val))}</span></div>`).join("");
}

function paintFocusBars(el, v) {
  const host = el.querySelector("[data-f=bars]");
  if (!host) return;
  host.innerHTML = FOCUS_BARS.map(([k, name, unit, full]) => {
    const val = numOrNull(v[k]);
    const pct = val == null ? 0 : Math.max(0, Math.min(100, (val / full) * 100));
    const col = HOT_BARS.includes(k) ? "var(--bad)"
      : k.indexOf("power") === 0 ? "var(--accent)" : "var(--good)";
    return `<div class="fbar"><span class="bn">${esc(name)}</span>
      <span class="bt"><span class="bf" style="width:${pct}%;background:${col}"></span></span>
      <span class="bv">${val == null ? "—" : fmt(val, 0) + (unit ? " " + esc(unit) : "")}</span></div>`;
  }).join("");
}

/* One line summarising the things that indicate trouble, shared by the live card
 * and the focus view (the TUI's "health" line). */
function healthBits(v) {
  const hot = !!numOrNull(v.throttle);
  const bits = [hot ? "THROTTLED" : "no throttle"];
  if (v.temp_limit_c != null) bits.push("limit " + fmt(v.temp_limit_c, 0) + "°C");
  if (v.power_limit_w != null) bits.push("cap " + fmt(v.power_limit_w, 0) + "W");
  if (v.cpu_pkg_w != null) bits.push("cpu " + fmt(v.cpu_pkg_w, 0) + "W");
  if (v.pcie_gen != null && v.pcie_width != null) {
    bits.push("pcie " + fmt(v.pcie_gen, 0) + "×" + fmt(v.pcie_width, 0));
  }
  if (v.video_mhz != null) bits.push("nvenc " + fmt(v.video_mhz, 0));
  if (v.pstate) bits.push(String(v.pstate));
  if (hot && v.throttle_bits != null) bits.push("bits " + fmt(v.throttle_bits, 0));
  return { text: bits.join(" · "), hot };
}

function paintHealth(node, v) {
  if (!node) return;
  const h = healthBits(v);
  node.textContent = h.text;
  node.className = "fhealth" + (h.hot ? " hot" : "");
}

function paintFocusHealth(el, v) {
  paintHealth(el.querySelector("[data-f=health]"), v);
  const temps = el.querySelector("[data-f=temps]");
  if (!temps) return;
  const t = [];
  if (v.temp_c != null) t.push("gpu " + fmt(v.temp_c, 0) + "°");
  if (v.cpu_temp_c != null) t.push("cpu " + fmt(v.cpu_temp_c, 0) + "°");
  if (v.cpu_max_core_c != null) t.push("core-max " + fmt(v.cpu_max_core_c, 0) + "°");
  if (v.temp_mem_c != null) t.push("vram " + fmt(v.temp_mem_c, 0) + "°");
  if (v.nvme_c != null) t.push("nvme " + fmt(v.nvme_c, 0) + "°");
  if (v.gpu_fan_rpm != null) t.push("fan " + fmt(v.gpu_fan_rpm, 0) + "rpm");
  temps.textContent = t.join(" · ") || "—";
}

/* Sparkline panel: one small chart per metric, so the whole picture is visible
   at once instead of switching the main chart's preset. */
function drawMiniCharts(el, runId) {
  const buf = (TEL[runId] || []).slice(-2000);
  const base = teleBase(buf);
  const win = runWindow(runId);
  FOCUS_MINI.forEach(([k]) => {
    const cv = el.querySelector(`canvas[data-mini="${k}"]`);
    if (!cv) return;
    const meta = SERIES[k] || [k, "#4c9aff"];
    const pts = seriesPoints(buf, k, base, win.start);
    const out = el.querySelector(`[data-m="${k}"]`);
    if (out) out.textContent = pts.length ? fmt(pts[pts.length - 1].y, 1) : "—";
    drawChart(cv, [{ name: meta[0], color: meta[1], points: pts }],
              { normalize: true, compact: true });
  });
}

function paintFocusStats(el, id) {
  const host = el.querySelector("[data-f=stats]");
  if (!host) return;
  const buf = TEL[id] || [];
  const keys = orderedStatKeys(Object.keys(SERIES).filter((k) => buf.some((b) => numOrNull(b[k]) != null)));
  if (!keys.length) { host.innerHTML = `<div class="muted">no telemetry yet</div>`; return; }
  const row = (k) => {
    const vals = buf.map((b) => numOrNull(b[k])).filter((x) => x != null);
    const mn = Math.min.apply(null, vals), mx = Math.max.apply(null, vals);
    const avg = vals.reduce((a, b) => a + b, 0) / vals.length;
    return `<tr><th>${esc(SERIES[k][0])}</th><td class="num">${fmt(mn)}</td>
      <td class="num">${fmt(avg)}</td><td class="num">${fmt(mx)}</td>
      <td class="num muted">${vals.length}</td></tr>`;
  };
  const head = `<thead><tr><th>metric</th><th>min</th><th>avg</th><th>max</th><th>n</th></tr></thead>`;
  // two side-by-side tables: 27 metrics in one narrow column is unreadable
  const half = Math.ceil(keys.length / 2);
  const table = (part) => `<table class="statstable">${head}<tbody>${part.map(row).join("")}</tbody></table>`;
  host.innerHTML = keys.length > 8
    ? table(keys.slice(0, half)) + table(keys.slice(half))
    : table(keys);
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
function nvFps(nv) {
  if (!nv || !nv.results) return null;
  const v = Object.values(nv.results).map((x) => +x.fps).filter((x) => !isNaN(x));
  return v.length ? Math.max(...v) : null;
}
function nvSsim(nv) {
  if (!nv || !nv.results) return null;
  const v = Object.values(nv.results).map((x) => +x.ssim).filter((x) => !isNaN(x));
  return v.length ? Math.min(...v) : null;
}
function llmTok(llm) {
  if (!llm || !Array.isArray(llm.results)) return null;
  const v = llm.results.map((x) => +x.tok_per_s).filter((x) => !isNaN(x) && x);
  return v.length ? Math.max(...v) : null;
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
    // pair each run with the previous one for the same host + preset (chronological)
    const seen = {};
    d.runs.slice().sort((a, b) => (a.started || 0) - (b.started || 0)).forEach((r) => {
      if (!r.preset) { r.__prev = null; return; }   // only compare like-for-like runs
      const k = (r.host || "") + "|" + r.preset;
      r.__prev = seen[k] || null;
      seen[k] = r;
    });
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
      // ▲/▼ vs the previous run of the same preset on the same host (>=3%)
      let vsBadge = "";
      const pg = numOrNull(r.__prev && r.__prev.summary && r.__prev.summary.gpu_soak_tflops);
      const cg = numOrNull(s.gpu_soak_tflops);
      if (pg && cg) {
        const dd = (cg - pg) / Math.abs(pg) * 100;
        if (Math.abs(dd) >= 3) {
          vsBadge = ` <span class="delta ${dd > 0 ? "up" : "down"}" title="vs previous run on this host">`
            + `${dd > 0 ? "▲" : "▼"}${Math.abs(dd).toFixed(0)}%</span>`;
        }
      }
      return `<tr data-detail="${esc(r.id)}">
        <td>${esc(r.host)}</td>
        <td class="syscol" title="${esc(sysLine(r))}">${esc(sysLine(r))}</td>
        <td>${esc(r.preset || "—")}</td>
        <td>${esc(r.label || "")}</td>
        <td class="muted" title="${ts(r.started)}">${ago(r.started)}</td>
        <td class="num">${runDur(r)}</td>
        <td><span class="pill ${esc(r.status)}">${esc(r.status)}</span></td>
        <td class="num">${fmt(s.gpu_soak_tflops)}${vsBadge}</td>
        <td class="num">${fmt(s.cpu_soak_gflops, 0)}</td>
        <td class="num">${fmt(s.ram_soak_gbps)}</td>
        <td class="num">${fmt(peak, 0)}</td></tr>`;
    }).join("") || `<tr><td colspan="11" class="empty">No matching runs.</td></tr>`;
    fillSelect("#histHost", HOSTS, "all hosts");
  } catch (e) { /* ignore */ }
}

/* -------------------------------------------------------------- fleet --- */
async function renderFleet() {
  let runs = [];
  try { runs = (await api("/runs?limit=500")).runs; } catch (e) { return; }
  const done = runs.filter((r) => r.status !== "running");
  const byHost = {}, byGpu = {};
  for (const r of done) {
    (byHost[r.host || "?"] = byHost[r.host || "?"] || []).push(r);
    (byGpu[gpuOf(r)] = byGpu[gpuOf(r)] || []).push(r);
  }
  const agg = (rs) => ({
    gpu: median(rs.map((r) => numOrNull(r.summary && r.summary.gpu_soak_tflops))),
    cpu: median(rs.map((r) => numOrNull(r.summary && r.summary.cpu_soak_gflops))),
    ram: median(rs.map((r) => numOrNull(r.summary && r.summary.ram_soak_gbps))),
    peak: median(rs.map(peakTemp)),
  });
  const latest = (rs) => rs.slice().sort((a, b) => (b.started || 0) - (a.started || 0))[0];
  const tkState = (rs) => {
    let seen = "";
    for (const x of rs) {
      const rr = STATE.runners.find((y) => y.id === x.runner_id);
      if (rr && rr.update_available) return "update available";
      if (rr && rr.toolkit_commit) seen = (rr.toolkit_mode === "git" ? "git " : "") + rr.toolkit_commit;
    }
    return seen || "—";
  };
  $("#fleetCount").textContent = `${Object.keys(byHost).length} machines · ${done.length} finished runs` +
    (STATE.server_git ? ` · control plane ${STATE.server_git}` : "");
  $("#fleetHosts tbody").innerHTML = Object.entries(byHost).sort().map(([h, rs]) => {
    const a = agg(rs), L = latest(rs), tk = tkState(rs);
    const stale = tk === "update available";
    return `<tr data-detail="${esc(L.id)}">
      <td>${esc(h)}</td><td class="num">${rs.length}</td>
      <td class="muted">${ago(L.started)}</td>
      <td class="num">${fmt(a.gpu)}</td><td class="num">${fmt(a.cpu, 0)}</td>
      <td class="num">${fmt(a.ram)}</td><td class="num">${fmt(a.peak, 0)}</td>
      <td class="${stale ? "delta down" : "muted"}">${esc(tk)}</td>
      <td><span class="pill ${esc(L.status)}">${esc(L.status)}</span></td></tr>`;
  }).join("") || `<tr><td colspan="9" class="empty">No finished runs yet.</td></tr>`;
  $("#fleetGpus tbody").innerHTML = Object.entries(byGpu)
    .sort((x, y) => y[1].length - x[1].length).map(([g, rs]) => {
      const a = agg(rs);
      const hosts = new Set(rs.map((r) => r.host)).size;
      return `<tr><td>${esc(g)}</td><td class="num">${rs.length}</td><td class="num">${hosts}</td>
        <td class="num">${fmt(a.gpu)}</td><td class="num">${fmt(a.peak, 0)}</td></tr>`;
    }).join("") || `<tr><td colspan="5" class="empty">No GPU data.</td></tr>`;
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
    const tstats = d.stats || {};
    $("#trendLegend").innerHTML = series.map((s, i) => {
      const q = tstats[metrics[i]] || {};
      const extra = q.n ? ` · med ${fmt(q.p50, 1)} · p95 ${fmt(q.p95, 1)} · max ${fmt(q.max, 1)}` : "";
      return `<span><i class="sw" style="background:${s.color}"></i>${esc(s.name)} · ${s.points.length} pts${extra}</span>`;
    }).join("");
  } catch (e) { /* ignore */ }
}
function labelFor(m) { const f = STATE.metrics.find((x) => x[0] === m); return f ? f[1] : m; }

/* ------------------------------------------------------------ compare --- */
function renderCompareOptions() {
  const finished = STATE.recent.filter((r) => r.status !== "running");
  const opts = finished.slice(0, 60).map((r) =>
    `<option value="${esc(r.id)}">${esc(r.host)} · ${esc(r.label || "")} · ${ts(r.started)}</option>`).join("");
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
/* Plot a set of series.
 *
 * opt.normalize -> each series is scaled to its own band. Telemetry charts mix
 *   units (degC, W, %), and a single shared axis made everything but the largest
 *   series look like a flat line at the bottom - the legend carries the numbers.
 * opt.xTime    -> x is unix seconds (clock labels)
 * opt.xElapsed -> x is seconds into the run (m:ss labels)
 */
function drawChart(cv, series, opt = {}) {
  const dpr = window.devicePixelRatio || 1;
  const h = +cv.getAttribute("height") || 180;   // fixed CSS height (never compound)
  const w = Math.max(140, cv.clientWidth || 300);
  cv.style.height = h + "px";
  cv.width = Math.max(1, Math.round(w * dpr));
  cv.height = Math.max(1, Math.round(h * dpr));
  const g = cv.getContext("2d");
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.clearRect(0, 0, w, h);

  const norm = !!opt.normalize;
  const compact = !!opt.compact;
  const use = (series || []).filter((s) => s && s.points && s.points.length >= 2);
  use.forEach((s) => {
    const ys = s.points.map((p) => p.y);
    s.min = Math.min.apply(null, ys);
    s.max = Math.max.apply(null, ys);
    s.last = ys[ys.length - 1];
    let lo = s.min, hi = s.max;
    if (hi - lo < 1e-9) { hi = lo + 1; }
    const pad = (hi - lo) * 0.1;
    s.lo = lo - pad; s.hi = hi + pad;
  });

  const pl = compact ? 3 : (norm ? 32 : 46), pr = compact ? 3 : 12;
  const pt = compact ? 4 : 10, pb = compact ? 4 : 24;
  const x0 = pl, y0 = h - pb, x1 = w - pr, y1 = pt;

  let lo = 0, hi = 1, ticks;
  if (norm) {
    ticks = [0, 1, 2, 3, 4];
  } else {
    hi = 0;
    use.forEach((s) => { lo = Math.min(lo, s.min); hi = Math.max(hi, s.max); });
    if (hi - lo < 1e-9) { hi = lo + 1; }
    hi = niceMax(hi);
    ticks = [0, 1, 2, 3, 4];
  }

  g.font = "10px monospace";
  ticks.forEach((i) => {
    const y = Math.round(y0 - (y0 - y1) * i / 4) + 0.5;
    if (!compact) {
      g.strokeStyle = "#1c222c"; g.lineWidth = 1;
      g.beginPath(); g.moveTo(x0, y); g.lineTo(x1, y); g.stroke();
      g.fillStyle = "#67717f";
      g.fillText(norm ? (i * 25) + "%" : shortNum(lo + (hi - lo) * i / 4), 3, y + 3);
    }
  });
  if (!compact) {
    g.strokeStyle = "#2b3442";
    g.beginPath(); g.moveTo(x0 + 0.5, y1); g.lineTo(x0 + 0.5, y0); g.stroke();
  }

  if (!use.length) {
    g.fillStyle = "#67717f";
    if (!compact) g.fillText("no data yet", x0 + 8, (y0 + y1) / 2 + 4);
    cv._chart = { series: [], sx: () => x0, xmin: 0, xmax: 1, opt };
    return;
  }

  const xs = [].concat.apply([], use.map((s) => s.points.map((p) => p.x)));
  const xmin = Math.min.apply(null, xs), xmax = Math.max.apply(null, xs);
  const sx = (x) => x0 + (x1 - x0) * (xmax === xmin ? 0.5 : (x - xmin) / (xmax - xmin));
  const sy = (s, y) => {
    const frac = norm ? (y - s.lo) / (s.hi - s.lo) : (y - lo) / (hi - lo);
    return y0 - (y0 - y1) * Math.max(0, Math.min(1, frac));
  };

  use.forEach((s) => {
    g.strokeStyle = s.color; g.lineWidth = 1.7; g.lineJoin = "round";
    g.beginPath();
    s.points.forEach((p, i) => {
      const X = sx(p.x), Y = sy(s, p.y);
      i ? g.lineTo(X, Y) : g.moveTo(X, Y);
    });
    g.stroke();
    // mark the newest point so "now" is obvious
    const last = s.points[s.points.length - 1];
    g.fillStyle = s.color;
    g.beginPath(); g.arc(sx(last.x), sy(s, last.y), 2.3, 0, 6.2832); g.fill();
  });

  g.fillStyle = "#67717f";
  if (!compact) {
    const lab = (x) => opt.xTime
      ? new Date(x * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
      : fmtElapsed(x);
    [[xmin, 0], [(xmin + xmax) / 2, 0.5], [xmax, 1]].forEach(([x, f]) => {
      const t = lab(x), tw = g.measureText(t).width;
      const tx = Math.max(x0, Math.min(x0 + (x1 - x0) * f - tw * f, x1 - tw));
      g.fillText(t, tx, h - 7);
    });
  }

  cv._chart = { series: use, sx, xmin, xmax, opt };
  attachHover(cv);
}

/* Legend for a telemetry chart: swatch, name, latest value, and the observed
   range - the real numbers behind the normalised plot. A series this runner
   never emitted is listed too, so a missing line is stated rather than implied. */
function chartLegendHTML(series) {
  const use = (series || []).filter((s) => s && s.points && s.points.length >= 2);
  const empty = (series || []).filter((s) => s && (!s.points || s.points.length < 2));
  const draw = use.map((s) => {
    const ys = s.points.map((p) => p.y);
    const mn = Math.min.apply(null, ys), mx = Math.max.apply(null, ys);
    return `<span class="cl"><i style="background:${s.color}"></i>${esc(s.name)}
      <b>${fmt(ys[ys.length - 1], 1)}</b><span class="muted">${fmt(mn, 0)}–${fmt(mx, 0)}</span></span>`;
  }).join("");
  const missing = empty.map((s) => `<span class="cl nodata" title="this runner did not report it">
      <i style="background:${s.color}"></i>${esc(s.name)} <b>—</b>
      <span class="muted">no data on this runner</span></span>`).join("");
  return draw + missing;
}

function fmtElapsed(s) {
  s = Math.max(0, Math.round(s || 0));
  return s < 60 ? s + "s" : Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0");
}

function shortNum(v) {
  const n = Math.abs(v || 0);
  if (n >= 1e6) return (v / 1e6).toFixed(1) + "M";
  if (n >= 1e4) return Math.round(v / 1e3) + "k";
  if (n >= 100) return String(Math.round(v));
  if (n >= 10) return String(Math.round(v * 10) / 10);
  return String(Math.round(v * 100) / 100);
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

/* x for a telemetry sample, relative to the start of the run's data.
 *
 * `elapsed` comes from the sampler's own clock, which can predate this run (the
 * dashboard tails whichever CSV is newest, so early samples of a run can carry a
 * previous run's clock) - rebase it to the first sample we hold. `ts` is aligned
 * to the run but stored at ~1s granularity, so it is only the fallback. */
function teleBase(buf) {
  let base = Infinity;
  for (const b of buf || []) {
    if (b.elapsed != null && b.elapsed < base) base = b.elapsed;
  }
  return isFinite(base) ? base : 0;
}

function seriesPoints(buf, key, base, runStart) {
  const rows = (buf || []).filter((b) => b[key] != null);
  const out = [];
  for (const b of rows) {
    let x = b.elapsed != null ? b.elapsed - base : null;
    if (x == null && b.ts != null && runStart != null) x = b.ts - runStart;
    if (x == null || x < 0) continue;
    out.push({ x, y: b[key] });
  }
  out.sort((a, b) => a.x - b.x);
  // collapse same-instant duplicates (coarse ts) to the newest value, so no
  // vertical line is drawn between two samples that share an x
  const clean = [];
  for (const p of out) {
    const prev = clean[clean.length - 1];
    if (prev && p.x - prev.x < 0.05) { prev.y = p.y; continue; }
    clean.push(p);
  }
  return clean;
}

function runWindow(runId) {
  const r = STATE.active.find((x) => x.id === runId) || STATE.recent.find((x) => x.id === runId);
  return { start: (r && r.started) || null, end: (r && r.ended) || null };
}

function drawRunChart(card, runId) {
  const cv = card.querySelector(".runchart");
  if (!cv) return;
  const buf = (TEL[runId] || []).slice(-2000);
  const base = teleBase(buf);
  const win = runWindow(runId);
  const preset = CHART[runId] || Object.keys(CHART_PRESETS)[0];
  const keys = CHART_PRESETS[preset] || [];
  const series = keys.map((k) => {
    const meta = SERIES[k] || [k, "#4c9aff"];
    return { name: meta[0], color: meta[1], points: seriesPoints(buf, k, base, win.start) };
  });
  drawChart(cv, series, { normalize: true, xElapsed: true });
  const lg = card.querySelector(".chartlegend");
  if (lg) lg.innerHTML = chartLegendHTML(series);
}

/* ------------------------------------------------------------ actions --- */
async function startRun(runnerId) {
  const runner = STATE.runners.find((r) => r.id === runnerId) || {};
  const sup = (runner.capabilities && runner.capabilities.groups) || GROUPS;
  const presetKeys = Object.keys(PRESETS);
  const usable = (k) => {
    const p = PRESETS[k];
    if (!p || !p.groups) return true;
    // a preset with nothing beyond `sys` this runner can do is not useful
    return GROUPS.some((g) => p.groups[g] && g !== "sys" && sup.includes(g));
  };
  openModal(`<h3>Start run</h3>
    <div class="formrow"><label>Preset</label>
      <select id="mPreset">${presetKeys.map((k) =>
        `<option${usable(k) ? "" : " disabled"}>${esc(k)}</option>`).join("")}</select>
      <span class="muted" id="mPresetInfo"></span></div>
    <div class="formrow"><label>Groups</label><div class="chk" id="mGroups">
      ${GROUPS.map((g) => {
        const ok = sup.includes(g);
        return `<label${ok ? "" : ' class="muted" title="not implemented on this runner"'}>`
          + `<input type="checkbox" value="${g}"${ok ? "" : " disabled"}>${esc(g)}</label>`;
      }).join("")}</div></div>
    <div class="formrow"><label>Durations (s)</label>
      GPU soak <input id="mGpu" type="number" value="600" style="width:80px">
      combined <input id="mComb" type="number" value="300" style="width:80px">
      full soak <input id="mFull" type="number" value="300" style="width:80px">
      graphics <input id="mGfx" type="number" value="60" style="width:80px"></div>
    <div class="formrow"><label>Tuning</label>
      GPU size <input id="mGpuSize" type="number" value="12288" style="width:80px">
      CPU size <input id="mCpuSize" type="number" value="4096" style="width:80px">
      RAM MB <input id="mRamSize" type="number" value="1024" style="width:80px">
      CPU soak <input id="mCpuSoak" type="number" value="120" style="width:80px">
      RAM soak <input id="mRamSoak" type="number" value="120" style="width:80px">
      disk s <input id="mDiskSec" type="number" value="15" style="width:80px">
      disk MB <input id="mDiskSize" type="number" value="2048" style="width:80px">
      steady GB <input id="mSteady" type="number" value="10" style="width:80px">
      passes <input id="mPasses" type="number" value="2" style="width:80px">
      <label><input type="checkbox" id="mStable"> soak until stable, hold</label>
      <input id="mStableHold" type="number" value="60" style="width:70px"> s after plateau
      <label>gfx engine</label>
      <select id="mGfxEngine"><option>furmark</option><option>valley</option></select></div>
    <div class="formrow" style="justify-content:flex-end">
      <button class="ghost" id="mCancel">cancel</button>
      <button class="accent" id="mGo">start</button></div>`);
  const applyPreset = () => {
    const p = PRESETS[$("#mPreset").value] || {};
    const groups = p.groups || gset(...ALL_GROUPS);
    $$("#mGroups input").forEach((i) => { i.checked = !i.disabled && !!groups[i.value]; });
    const spin = p.spin || DEF_SPIN;
    $("#mGpu").value = spin.gpu; $("#mComb").value = spin.combined;
    $("#mFull").value = spin.fullsoak; $("#mGfx").value = spin.gfx;
    const tune = { ...TUNE_DEFAULT, ...(p.tune || {}) };
    $("#mGpuSize").value = tune.gpu_size; $("#mCpuSize").value = tune.cpu_size;
    $("#mRamSize").value = tune.ram_size_mb; $("#mCpuSoak").value = tune.cpu_soak;
    $("#mRamSoak").value = tune.ram_soak; $("#mDiskSec").value = tune.disk_seconds;
    $("#mDiskSize").value = tune.disk_size_mb;
    $("#mSteady").value = tune.steady_gb; $("#mPasses").value = tune.passes;
    $("#mStable").checked = !!tune.stable; $("#mStableHold").value = tune.stable_hold;
    $("#mGfxEngine").value = tune.gfx_engine || "furmark";
    const want = GROUPS.filter((g) => groups[g]);
    const un = want.filter((g) => !sup.includes(g));
    const info = $("#mPresetInfo");
    if (info) {
      info.textContent = "runs " + (want.filter((g) => sup.includes(g)).join(", ") || "nothing") +
        (un.length ? " · not on this runner: " + un.join(", ") : "");
    }
  };
  $("#mPreset").onchange = applyPreset;
  applyPreset();
  $("#mCancel").onclick = closeModal;
  $("#mGo").onclick = async () => {
    const skip = $$("#mGroups input:not(:disabled)").filter((i) => !i.checked).map((i) => i.value);
    // send the COMPLETE group map too: a runner may already have a different
    // preset selected locally, and `skip` alone would be applied relative to it
    const groups = {};
    $$("#mGroups input").forEach((i) => { groups[i.value] = (i.disabled || !i.checked) ? 0 : 1; });
    const payload = {
      label: $("#mPreset").value, preset: $("#mPreset").value, skip, groups,
      gpu_soak: +$("#mGpu").value, combined: +$("#mComb").value,
      fullsoak: +$("#mFull").value, gfx: +$("#mGfx").value,
      cpu_soak: +$("#mCpuSoak").value, ram_soak: +$("#mRamSoak").value,
      disk_seconds: +$("#mDiskSec").value, gpu_size: +$("#mGpuSize").value,
      disk_size_mb: +$("#mDiskSize").value,
      cpu_size: +$("#mCpuSize").value, ram_size_mb: +$("#mRamSize").value,
      passes: +$("#mPasses").value, steady_gb: +$("#mSteady").value,
      // stable_hold alone means "soak until stable" to extra_runner_args, and
      // TUNE_DEFAULT carries one - so sending it unconditionally made every
      // web-started run plateau-soak, unlike the same preset in the GUI/TUI
      ...($("#mStable").checked
        ? { stable: 1, stable_hold: +$("#mStableHold").value }
        : { stable: 0 }),
      gfx_engine: $("#mGfxEngine").value,
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
    await api("/runners/" + id + "/commands", "POST", { kind, payload: { confirm: true } });
    toast(verb.toLowerCase() + " sent to " + name);
    refresh();
  } catch (e) { toast(kind + " failed: " + e.message, "err"); }
}

/* update a runner in place (live USB) instead of walking over to reflash it */
function runnerUpdate(runnerId) {
  const r = STATE.runners.find((x) => x.id === runnerId) || {};
  const name = r.label || r.name || runnerId;
  const caps = r.capabilities || {};
  const live = !!caps.live;
  const latest = r.latest_commit || STATE.latest_git || "?";
  openModal(`<h3>Update / reflash — ${esc(name)}</h3>
    <p class="muted">toolkit <span class="mono">${esc(r.toolkit_commit || "unknown")}</span>
      · latest <span class="mono">${esc(latest)}</span>
      ${r.update_available ? '· <span class="delta down">update available</span>' : ""}
      ${live ? "· live USB" : "· checkout"}</p>
    <div class="sechead">Toolkit — scripts, phases, dashboards</div>
    ${live ? `<p class="muted">Fetch onto the stick (BENCHDATA); it switches now and again at boot.</p>` :
      `<p class="muted">This runner runs a <b>git checkout</b>: use <b>update (git pull)</b>, then
        <b>restart</b> if the badge persists — a process started before an update keeps running the
        old code until it is restarted (a build from recent main restarts itself).</p>`}
    <div class="formrow"><label>ref</label>
      <input id="uRef" value="main" style="width:190px" placeholder="main | tag | sha" ${live ? "" : "disabled"}>
      <input id="uSha" placeholder="(optional) expected tarball sha256" style="flex:1" ${live ? "" : "disabled"}></div>
    <div class="formrow" style="justify-content:flex-end">
      <button class="accent" id="uGo" ${live ? "" : "disabled"}>update toolkit</button>
      <button id="uGit">update (git pull) + restart</button>
      <button id="uRestart" title="re-exec the runner so it loads the code on disk">restart runner</button></div>
    <div class="sechead">ISO — kernel, drivers, packages</div>
    <div class="formrow"><label>release</label>
      <input id="uTag" placeholder="iso-nvidia-legacy-50" style="width:190px">
      <select id="uGpu"><option>nvidia-legacy</option><option>nvidia</option><option>amd</option></select>
      <button id="uFetch">fetch + verify</button></div>
    <div class="formrow"><label>iso path</label>
      <input id="uIso" placeholder="/mnt/benchdata/iso/bench-live-*.iso" style="flex:1"></div>
    <div class="formrow"><label>device</label>
      <input id="uDev" placeholder="/dev/sdX" style="width:120px">
      <label class="chk"><input type="checkbox" id="uOk"> confirm destructive write</label>
      <button class="danger" id="uWrite">write ISO</button>
      <button class="accent" id="uSlot" title="A/B: write the ISO into the inactive slot and flip the boot flag">write to inactive slot</button></div>
    <div class="formrow" style="justify-content:flex-end">
      <button class="ghost" onclick="closeModal()">close</button></div>`);
  const send = async (kind, payload) => {
    try {
      await api("/runners/" + runnerId + "/commands", "POST", { kind, payload });
      toast(kind + " queued for " + name + " (watch the runner's console)");
      refresh();
    } catch (e) { toast(kind + " failed: " + e.message, "err"); }
  };
  $("#uGo").onclick = () => send("update_toolkit", {
    ref: $("#uRef").value.trim() || "main", sha256: $("#uSha").value.trim() || null,
    restart: true });
  $("#uGit").onclick = () => send("update", { restart: true });
  $("#uRestart").onclick = () => send("restart", {});
  $("#uFetch").onclick = () => send("iso_fetch", {
    tag: $("#uTag").value.trim(), gpu: $("#uGpu").value });
  const needIso = () => {
    const iso = $("#uIso").value.trim();
    if (!iso) toast("give the iso path first", "err");
    return iso;
  };
  $("#uWrite").onclick = () => {
    if (!$("#uOk").checked) return toast("tick the confirm box first", "err");
    const iso = needIso();
    if (iso) send("iso_write", { iso, device: $("#uDev").value.trim(), confirm: true });
  };
  $("#uSlot").onclick = () => {
    if (!$("#uOk").checked) return toast("tick the confirm box first", "err");
    const iso = needIso();
    if (iso) send("iso_write_slot", { iso, confirm: true });
  };
}

async function showDetail(runId) {
  const r = await api("/runs/" + runId);
  const s = r.summary || {};
  const sy = r.system || {};
  const tel = await api("/runs/" + runId + "/telemetry?max=4000");
  const telBuf = tel.rows || [];
  const telBase0 = teleBase(telBuf);
  const pts = (n) => seriesPoints(telBuf.filter((x) => x[n] != null), n, telBase0, r.started);
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
    ["NVENC fps", fmt(nvFps(s.nvenc), 0) + " fps", ""],
    ["NVENC ssim", fmt(nvSsim(s.nvenc), 3), ""],
    ["LLM tok/s", fmt(llmTok(s.llm), 1), ""],
    ["PCIe H2D", s.pcie && s.pcie.h2d_gbps != null ? fmt(s.pcie.h2d_gbps, 1) + " GB/s" : "—", ""],
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
  const statKeys = orderedStatKeys(Object.keys(st).filter((k) => k !== "elapsed" && k !== "ts"));
  const statRows = statKeys.map((k) => {
    const meta = SERIES[k] || [k, "#8592a6"];
    const a = st[k];
    return `<tr><td>${esc(meta[0])}</td><td class="num">${fmt(a.min, 1)}</td>
      <td class="num">${fmt(a.avg, 1)}</td><td class="num">${fmt(a.max, 1)}</td>
      <td class="num muted">${fmt(a.std, 2)}</td><td class="num">${fmt(a.p95, 1)}</td>
      <td class="num">${fmt(a.p99, 1)}</td><td class="muted mono">${a.n}</td></tr>`;
  }).join("");
  const preset = PRESETS[r.preset || ""];
  let conformance = "";
  if (preset) {
    const expected = GROUPS.filter((g) => preset.groups && preset.groups[g]);
    const skipped = new Set((r.phases || [])
      .filter((p) => /\bskip(ped)?\b/i.test(p.label || "")).map((p) => p.grp));
    const ran = expected.filter((g) => !skipped.has(g));
    const missed = expected.filter((g) => skipped.has(g));
    const level = missed.length ? "warn" : "good";
    conformance = `<div class="v-${level}"><b>${missed.length ? "!" : "✓"}</b> preset “${esc(r.preset)}”
      ran ${ran.length}/${expected.length} groups${missed.length ? " — skipped: " + esc(missed.join(", ")) : ""}</div>`;
  }
  openModal(`<h3>${esc(r.host)} · ${esc(r.label || "run")} <span class="pill ${esc(r.status)}">${esc(r.status)}</span></h3>
    <div class="muted">${ts(r.started)} · ${runDur(r)} · ${esc(sy.gpu || r.gpu || "")} ${sy.driver || r.driver ? "· driver " + esc(sy.driver || r.driver) : ""}${prev ? ` · <span class="delta flat">vs ${ts(prev.started)}</span>` : ""}</div>
    ${conformance ? `<div class="remarks verdicts">${conformance}</div>` : ""}
    ${(s.verdicts || []).length ? `<div class="remarks verdicts">${s.verdicts.map((x) =>
      `<div class="v-${esc(x.level)}"><b>${x.level === "good" ? "✓" : x.level === "warn" ? "!" : x.level === "info" ? "i" : "✕"}</b> ${esc(x.text)}</div>`).join("")}</div>` : ""}
    ${remarks.length ? `<div class="remarks">${remarks.map((x) => `<div>${esc(x)}</div>`).join("")}</div>` : ""}
    <div class="kv" style="margin:14px 0">
      ${tiles.map(([k, val, d]) => `<div class="kvitem"><div class="k">${esc(k)}</div><div class="v">${esc(val)}${d || ""}</div></div>`).join("")}
    </div>
    <div class="kv" style="margin:0 0 14px">
      ${derived.map(([k, val]) => `<div class="kvitem"><div class="k">${esc(k)}</div><div class="v">${esc(val)}</div></div>`).join("")}
    </div>
    <div class="chartrow"><canvas id="dChart" height="220"></canvas>
      <select class="csel" id="dChartSel">${Object.keys(CHART_PRESETS).map((k) => `<option>${esc(k)}</option>`).join("")}</select></div>
    <div class="chartlegend" id="dChartLegend"></div>
    <h4 class="sechead">Results</h4>
    ${(() => {
      const secs = resultSections(s);
      if (!secs.length) return `<div class="muted">no structured results</div>`;
      return secs.map(([group, rows]) => `<h5 class="subhead">${esc(humanKey(group))}</h5>
        <table class="kvtable">${rows.map(([k, val]) =>
          `<tr><th>${esc(k)}</th><td>${esc(String(val))}</td></tr>`).join("")}</table>`).join("");
    })()}
    <h4 class="sechead">System</h4>
    <table class="kvtable">${sysrows.map(([k, val]) => `<tr><th>${esc(k)}</th><td>${esc(String(val))}</td></tr>`).join("")}</table>
    <h4 class="sechead">Profile</h4>
    <table class="kvtable">${(() => {
      const prof = r.profile || {};
      const rows = [["preset", prof.preset || r.preset],
        ["gpu soak (s)", prof.gpu_soak], ["combined (s)", prof.combined],
        ["full soak (s)", prof.fullsoak], ["graphics (s)", prof.gfx],
        ["cpu soak (s)", prof.cpu_soak], ["ram soak (s)", prof.ram_soak],
        ["disk (s)", prof.disk_seconds], ["disk size (MB)", prof.disk_size_mb],
    ["steady write (GB)", prof.steady_gb],
        ["passes", prof.passes], ["gpu matrix", prof.gpu_size],
        ["cpu size", prof.cpu_size], ["ram size (MB)", prof.ram_size_mb],
        ["soak until stable", prof.stable == null ? null : (prof.stable ? "yes" : "no")],
        ["stable hold (s)", prof.stable_hold],
      ].filter((x) => x[1] != null && x[1] !== "");
      return rows.map(([k, val]) => `<tr><th>${esc(k)}</th><td>${esc(String(val))}</td></tr>`).join("")
        || `<tr><td class="muted">no profile recorded</td></tr>`;
    })()}</table>
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
    const series = (CHART_PRESETS[dpreset] || []).map((k) => {
      const meta = SERIES[k] || [k, "#4c9aff"];
      return { name: meta[0], color: meta[1], points: pts(k) };
    });
    drawChart($("#dChart"), series, { normalize: true, xElapsed: true });
    const lg = document.querySelector("#dChartLegend");
    if (lg) lg.innerHTML = chartLegendHTML(series);
  };
  drawDetail();
  $("#dChartSel").onchange = (e) => { dpreset = e.target.value; drawDetail(); };
}

/* Turn a run summary into readable, grouped rows. The raw summary is a flat dump
 * of dotted keys (gpu_matmul_tflops.fp16, net.download_mbps ...) which is hard to
 * scan: label each value, group by the first path segment, and format numbers. */
function humanKey(k) {
  const known = (STATE.metrics || []).find((m) => m[0] === k);
  if (known) return known[1];
  return String(k).split(".").map((part) => part.replace(/_/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase()).replace(/\bTflops\b/, "TFLOPS")
    .replace(/\bGflops\b/, "GFLOPS").replace(/\bGbps\b/, "GB/s")
    .replace(/\bMbps\b/, "MB/s").replace(/\bC\b/, "°C")).join(" · ");
}

function humanVal(v) {
  if (typeof v === "boolean") return v ? "yes" : "no";
  if (typeof v === "number") return shortNum(v);
  return String(v);
}

function resultSections(summary) {
  const skip = new Set(["host", "timestamp", "telemetry", "samples", "gpu_soak_telemetry"]);
  const groups = new Map();
  const walk = (path, obj) => {
    for (const [k, v] of Object.entries(obj || {})) {
      if (skip.has(k)) continue;
      const key = path ? path + "." + k : k;
      if (v && typeof v === "object" && !Array.isArray(v)) { walk(key, v); continue; }
      if (v === null || v === undefined || v === "") continue;
      const group = key.split(".")[0];
      if (!groups.has(group)) groups.set(group, []);
      groups.get(group).push([humanKey(key), Array.isArray(v) ? v.join(" / ") : humanVal(v)]);
    }
  };
  walk("", summary);
  return Array.from(groups.entries());
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
      <td><button class="danger" data-sched-del="${esc(s.id)}">delete</button></td></tr>`;
  }).join("") || `<tr><td colspan="6" class="muted" style="padding:16px">No schedules.</td></tr>`;
}

function newSchedule() {
  const opts = STATE.runners.map((r) => `<option value="${esc(r.id)}">${esc(r.label || r.name)}</option>`).join("");
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
    try {
      const p = PRESETS[$("#scPreset").value] || {};
      const profile = { label: $("#scPreset").value,
        groups: p.groups || gset(...ALL_GROUPS),
        skip: p.groups ? GROUPS.filter((g) => !p.groups[g]) : [] };
      if (p.spin) Object.assign(profile, { gpu_soak: p.spin.gpu, combined: p.spin.combined,
        fullsoak: p.spin.fullsoak, gfx: p.spin.gfx });
      if (p.tune) Object.assign(profile, p.tune);
      await api("/schedules", "POST", {
        runner_id: $("#scRunner").value, label: $("#scLabel").value, profile,
        kind: $("#scKind").value, every_s: +$("#scEvery").value, at_time: $("#scAt").value });
      closeModal(); renderSchedules();
    } catch (e) { toast("schedule failed: " + e.message, "err"); }
  };
}
async function deleteSchedule(id) {
  try { await api("/schedules/" + id, "DELETE"); renderSchedules(); }
  catch (e) { toast("delete failed: " + e.message, "err"); }
}

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
  const t = e.target.closest("[data-tab],[data-start],[data-stop],[data-update],[data-fan],[data-detail],[data-sched-del],[data-toggle-off],[data-forget],[data-power],[data-iso],[data-focus],[data-rtab],[data-runner],[data-f]");
  if (!t) return;
  if (t.dataset.rtab) {
    const found = runnerRun(t.dataset.rtab);
    if (!found) return toast("that runner has no runs yet");
    if (FOCUS !== found.run.id) focusRun(found.run.id);
    return;
  }
  // clicking a runner panel opens its live view; a control inside the panel still
  // does its own thing (closest() finds the nearer match first)
  if (t.dataset.runner && !e.target.closest("button,input,select,textarea,a,form,label")) {
    const found = runnerRun(t.dataset.runner);
    if (!found) return toast("that runner has no runs yet");
    if (FOCUS !== found.run.id) focusRun(found.run.id);
    return;
  }
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
    if (t.dataset.tab === "fleet") renderFleet();
    if (t.dataset.tab === "schedules") renderSchedules();
    return;
  }
  if (t.dataset.start) return startRun(t.dataset.start);
  if (t.dataset.stop) return cmd(t.dataset.stop, "stop");
  if (t.dataset.update) return cmd(t.dataset.update, "update");
  if (t.dataset.fan) return cmd(t.dataset.fan, "fan_curve", { on: true });
  if (t.dataset.detail) return showDetail(t.dataset.detail).catch((e) => toast("details failed: " + e.message, "err"));
  if (t.dataset.schedDel) return deleteSchedule(t.dataset.schedDel);
  if (t.dataset.forget) return forgetRunner(t.dataset.forget);
  if (t.dataset.power) return powerRunner(t.dataset.power, t.dataset.powerkind);
  if (t.dataset.iso) return runnerUpdate(t.dataset.iso);
  if (t.dataset.focus) return focusRun(t.dataset.focus);
  if (t.dataset.f === "close") return closeFocus();
  if (t.dataset.f === "detail") return showDetail(FOCUS).catch((e) => toast("details failed: " + e.message, "err"));
  if (t.dataset.f === "update") {
    const a = STATE.active.find((x) => x.id === FOCUS) || STATE.recent.find((x) => x.id === FOCUS);
    if (a) return cmd(a.runner_id, "update");
  }
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
$("#cmpRun").onclick = () => runCompare().catch((e) => toast("compare failed: " + e.message, "err"));
document.addEventListener("change", (e) => {
  const id = e.target && e.target.dataset && e.target.dataset.chart;
  if (!id) return;
  CHART[id] = e.target.value;
  const card = document.querySelector(`[data-active="${id}"]`);
  if (card) drawRunChart(card, id);
  if (FOCUS === id) { const f = $("#focus"); if (f) drawRunChart(f, id); }
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
window.addEventListener("resize", () => { renderActive(); renderFocus(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && FOCUS) closeFocus(); });

/* ---------------------------------------------------------------- boot -- */
/* Connect first and only prompt on a real 401: benchd can run with no token at
   all (open mode), and the old code refused to load until one was saved - leaving
   a blank page. The modal is also guarded so a wrong token cannot rebuild it while
   the operator is typing. */
connect();
refresh();
setInterval(() => {
  if (STATE.active.length) { const st = _scrollState(); renderActive(); _restoreScroll(st); }
  if (FOCUS) renderFocus();
}, 5000);
setInterval(() => { if (FOCUS) reloadTel(FOCUS); }, 15000);
