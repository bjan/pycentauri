// pycentauri web console
// Consumes /api/info, /status, /events/status (SSE), /attributes.
// Posts to /print/{pause,resume,stop} and /print/{speed,fan,temperature}
// when --enable-control is on.

const $ = (id) => document.getElementById(id);

// PrintInfo.Status → display label + semantic class.
// Table is authoritative, lifted from Elegoo's own elegoo-link SDK
// (src/lan/adapters/elegoo_fdm_cc/elegoo_fdm_cc_message_adapter.cpp:33–62).
// Codes 2/3/4 and 23–26 are resin-printer / LCD-specific — kept here for
// completeness in case a future firmware surfaces them on the Carbon.
const PRINT_STATUS = {
  0:  { label: "IDLE",         cls: "state-idle" },
  1:  { label: "HOMING",       cls: "state-printing" },
  2:  { label: "DROPPING",     cls: "state-printing" },
  3:  { label: "EXPOSING",     cls: "state-printing" },
  4:  { label: "LIFTING",      cls: "state-printing" },
  5:  { label: "PAUSING",      cls: "state-paused" },
  6:  { label: "PAUSED",       cls: "state-paused" },
  7:  { label: "STOPPING",     cls: "state-stopped" },
  8:  { label: "STOPPED",      cls: "state-stopped" },
  9:  { label: "COMPLETED",    cls: "state-completed" },
  10: { label: "FILE CHECK",   cls: "state-printing" },
  11: { label: "SELF CHECK",   cls: "state-printing" },
  12: { label: "RESUMING",     cls: "state-printing" },
  13: { label: "PRINTING",     cls: "state-printing" },
  14: { label: "ERROR",        cls: "state-err" },
  15: { label: "LEVELING",     cls: "state-printing" },
  16: { label: "PREHEATING",   cls: "state-printing" },
  17: { label: "RESONANCE",    cls: "state-printing" },
  18: { label: "PRINT START",  cls: "state-printing" },
  19: { label: "LEVEL DONE",   cls: "state-printing" },
  20: { label: "PREHEAT DONE", cls: "state-printing" },
  21: { label: "HOMING DONE",  cls: "state-printing" },
  22: { label: "RESONANCE OK", cls: "state-printing" },
  23: { label: "AUTO FEED",    cls: "state-printing" },
  24: { label: "UNLOADING",    cls: "state-printing" },
  25: { label: "UNLOAD ERR",   cls: "state-err" },
  26: { label: "UNLOAD PAUSE", cls: "state-paused" },
  // CC2-only codes (mapped from machine_status + sub_status in cc2.py)
  27: { label: "SWITCHING FILAMENT", cls: "state-printing" },
  28: { label: "FILAMENT LOADED",    cls: "state-printing" },
  29: { label: "UNLOADING FILAMENT", cls: "state-printing" },
};

function fmtSeconds(s) {
  if (s == null || Number.isNaN(+s)) return "—";
  s = Math.max(0, Math.round(+s));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  if (h > 0) return `${h}h ${m.toString().padStart(2, "0")}m`;
  if (m > 0) return `${m}m ${sec.toString().padStart(2, "0")}s`;
  return `${sec}s`;
}

function fmtTemp(v, padTo = "---.-") {
  if (v == null || Number.isNaN(+v)) return padTo;
  return (+v).toFixed(1);
}

function fmtCoord(v) {
  if (v == null || Number.isNaN(+v)) return "---.--";
  return (+v).toFixed(2).padStart(6, " ");
}

function tempPct(actual, target) {
  if (actual == null || target == null || target <= 0) return 0;
  return Math.min(100, Math.max(0, (actual / target) * 100));
}

function setStatusPill(kind, label) {
  const pill = $("status-pill");
  pill.classList.remove("warn", "err");
  if (kind === "warn") pill.classList.add("warn");
  if (kind === "err")  pill.classList.add("err");
  $("status-text").textContent = label;
}

// ---------------------------------------------------------------------------

async function loadInfo() {
  try {
    const r = await fetch("/api/info");
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const info = await r.json();

    $("version").textContent = info.version || "—";
    $("host").textContent = info.printer_host || "—";
    $("mainboard").textContent = info.mainboard_id || "—";
    $("mainboard").title = info.mainboard_id || "";

    if (info.enable_control) {
      $("controls").hidden = false;
      $("adjust").hidden = false;
      canvasControlEnabled = true;
    }

    try {
      const attrs = await (await fetch("/attributes")).json();
      $("printer-name").textContent = (attrs.machine_name || attrs.name || "Centauri Carbon").toUpperCase();
      $("firmware").textContent = attrs.firmware_version ? `FW ${attrs.firmware_version}` : "";
    } catch (_) { /* firmware may not push in idle */ }

    if (info.connected) {
      setStatusPill("ok", "LINK OK");
    } else {
      setStatusPill("warn", "LINK DOWN");
    }
  } catch (e) {
    setStatusPill("err", "SRV OFFLINE");
    console.warn("loadInfo failed:", e);
  }
}

function renderStatus(raw) {
  const pi = raw.PrintInfo || {};
  const pstatus = pi.Status;
  const filename = pi.Filename;

  // Job name
  $("filename").textContent = filename || "— · AWAITING ·";

  // Progress
  const progress = pi.Progress;
  const pct = (progress != null) ? Math.max(0, Math.min(100, +progress)) : 0;
  $("progress-bar").style.width = pct + "%";
  $("progress-label").textContent = (progress != null) ? `${pct}%` : "—";

  // State word + code
  const info = PRINT_STATUS[pstatus];
  const sw = $("print-status");
  const sc = $("print-status-code");
  sw.classList.remove(
    "state-idle", "state-printing", "state-paused",
    "state-stopped", "state-completed", "state-err",
  );
  if (info) {
    sw.textContent = info.label;
    sw.classList.add(info.cls);
  } else if (pstatus != null) {
    sw.textContent = `CODE·${pstatus}`;
  } else {
    sw.textContent = "—";
  }
  sc.textContent = (pstatus != null) ? String(pstatus).padStart(2, "0") : "--";

  // Layer
  $("layer").textContent = (pi.CurrentLayer != null && pi.TotalLayer)
    ? `${pi.CurrentLayer} / ${pi.TotalLayer}`
    : "—";

  // Times (CurrentTicks / TotalTicks are seconds on CC firmware)
  const elapsed = pi.CurrentTicks;
  const total = pi.TotalTicks;
  $("elapsed").textContent = fmtSeconds(elapsed);
  $("remaining").textContent = (total != null && elapsed != null)
    ? fmtSeconds(total - elapsed) : "—";

  // Speed %
  $("speed").textContent = (pi.PrintSpeedPct != null) ? `${pi.PrintSpeedPct}%` : "—";
  reflectSpeedMode(pi.PrintSpeedPct);

  // Hydrate ADJUST controls from live values + retune poll cadence.
  hydrateAdjust(raw);
  noteStatusForPoll(pstatus);

  // Temperatures
  const noz  = raw.TempOfNozzle, nozT = raw.TempTargetNozzle;
  const bed  = raw.TempOfHotbed, bedT = raw.TempTargetHotbed;
  const cham = raw.TempOfBox,    chamT = raw.TempTargetBox;
  $("t-nozzle").textContent        = fmtTemp(noz);
  $("t-nozzle-target").textContent = (nozT != null) ? String(Math.round(+nozT)) : "---";
  $("t-nozzle-bar").style.width    = tempPct(noz, nozT) + "%";
  $("t-bed").textContent           = fmtTemp(bed);
  $("t-bed-target").textContent    = (bedT != null) ? String(Math.round(+bedT)) : "---";
  $("t-bed-bar").style.width       = tempPct(bed, bedT) + "%";
  $("t-chamber").textContent       = fmtTemp(cham);
  $("t-chamber-target").textContent = (chamT != null) ? String(Math.round(+chamT)) : "---";
  // Chamber often targets 0; show the actual vs a reference of 50°C so the bar conveys warmth.
  $("t-chamber-bar").style.width   = Math.min(100, Math.max(0, (+(cham ?? 0) / 50) * 100)) + "%";

  // Coords "x,y,z"
  if (typeof raw.CurrenCoord === "string") {
    const [x, y, z] = raw.CurrenCoord.split(",");
    $("pos-x").textContent = fmtCoord(x);
    $("pos-y").textContent = fmtCoord(y);
    $("pos-z").textContent = fmtCoord(z);
  }

  // Z offset
  if (raw.ZOffset != null) $("z-offset").textContent = (+raw.ZOffset).toFixed(3);

  // Fans
  const fans = raw.CurrentFanSpeed || {};
  const setFan = (id, key, rowId) => {
    const v = fans[key];
    $(id).textContent = (v != null) ? `${v}%` : "—";
    if (rowId && v != null) $(rowId).hidden = false;
  };
  setFan("fan-model", "ModelFan");
  setFan("fan-aux",   "AuxiliaryFan");
  setFan("fan-box",   "BoxFan");
  setFan("fan-controller", "ControllerFan", "fan-controller-row");
  setFan("fan-heater",     "HeaterFan",     "fan-heater-row");

  // CC2-only: live head speed from gcode_move.speed (mm/min)
  const cc2 = raw._cc2;
  if (cc2 && cc2.gcode_move_speed != null) {
    // gcode_move.speed is the commanded speed of the current move in
    // mm/min; ÷60 gives the mm/s figure the printer's own screen shows
    // (verified against the screen 2026-07-05). Travels spike it briefly.
    $("head-speed-row").hidden = false;
    $("head-speed").textContent = `${Math.round(cc2.gcode_move_speed / 60)} mm/s`;
  }

  setStatusPill("ok", "LINK OK");
}

async function pollOnce() {
  try {
    const r = await fetch("/status");
    if (!r.ok) throw new Error("HTTP " + r.status);
    const body = await r.json();
    if (body.raw) renderStatus(body.raw);
  } catch (e) {
    setStatusPill("warn", "LINK WAIT");
    console.warn("poll failed:", e);
  }
}

function connectSSE() {
  let src;
  try { src = new EventSource("/events/status"); }
  catch (_) {
    setStatusPill("warn", "SSE N/A");
    return;
  }
  src.addEventListener("status", (ev) => {
    try { renderStatus(JSON.parse(ev.data)); } catch (_) {}
  });
  // SSE in Firefox is prone to silent stalls — the connection appears
  // open but no events arrive. `onerror` fires once and the browser's
  // auto-reconnect doesn't always kick in. The pollOnce safety net in
  // main() compensates regardless; here we just surface the wait state.
  src.onerror = () => {
    setStatusPill("warn", "LINK WAIT");
  };
}

// ---------------------------------------------------------------------------
// Controls

function setMsg(text, kind) {
  const el = $("ctrl-msg");
  el.classList.remove("ok", "warn", "err");
  if (kind) el.classList.add(kind);
  el.textContent = text;
}

async function doAction(label, path, confirmMsg) {
  if (confirmMsg && !confirm(confirmMsg)) return;
  setMsg(`» ${label}…`);
  const btns = document.querySelectorAll(".controls button");
  for (const b of btns) b.disabled = true;
  try {
    const r = await fetch(path, { method: "POST" });
    if (r.status === 504) {
      // Command reached the printer but confirmation didn't arrive in
      // time (CC2 lifecycle commands respond only after the mechanical
      // sequence finishes). Almost always the action still completes.
      setMsg(`⧗ ${label} sent — no confirmation yet; watch the status`, "warn");
      return;
    }
    if (!r.ok) throw new Error(`HTTP ${r.status} — ${await r.text()}`);
    setMsg(`✓ ${label} acknowledged`, "ok");
  } catch (e) {
    setMsg(`✗ ${label}: ${e.message}`, "err");
  } finally {
    setTimeout(() => {
      for (const b of btns) b.disabled = false;
    }, 800);
  }
}

function wireControls() {
  $("btn-pause")?.addEventListener("click", () =>
    doAction("PAUSE", "/print/pause", "Pause the current print?")
  );
  $("btn-resume")?.addEventListener("click", () =>
    doAction("RESUME", "/print/resume")
  );
  $("btn-stop")?.addEventListener("click", () =>
    doAction("STOP", "/print/stop",
      "Stop the current print?\n\nThis cannot be undone.")
  );

  // Keyboard shortcuts when controls are visible.
  document.addEventListener("keydown", (ev) => {
    if ($("controls").hidden) return;
    if (ev.target && /^(INPUT|TEXTAREA|SELECT)$/.test(ev.target.tagName)) return;
    if (ev.key === "F1") { ev.preventDefault(); $("btn-pause").click(); }
    if (ev.key === "F2") { ev.preventDefault(); $("btn-resume").click(); }
    if (ev.key === "F3") { ev.preventDefault(); $("btn-stop").click(); }
  });
}

// ---------------------------------------------------------------------------
// Live adjust (Cmd 403 family) — print speed, fan, temperature

function setAdjMsg(text, kind) {
  const el = $("adj-msg");
  el.classList.remove("ok", "warn", "err");
  if (kind) el.classList.add(kind);
  el.textContent = text || "";
}

// Two-way bind a slider/number pair sharing a base id (`${base}` for number,
// `${base}-slider` for slider). Returns a getter for the current value.
function bindPair(base) {
  const num = $(`adj-${base}`);
  const slider = $(`adj-${base}-slider`);
  if (!num || !slider) return () => 0;
  const clamp = (v) =>
    Math.max(+num.min, Math.min(+num.max, Number.isFinite(+v) ? +v : 0));
  slider.addEventListener("input", () => { num.value = String(clamp(slider.value)); });
  num.addEventListener("input", () => { slider.value = String(clamp(num.value)); });
  return () => clamp(num.value);
}

const ADJUST_TARGETS = {
  "fan-model": {
    label: "MODEL FAN",
    path: "/print/fan",
    build: (g) => ({ model: g("fan-model") }),
  },
  "fan-aux": {
    label: "AUX FAN",
    path: "/print/fan",
    build: (g) => ({ auxiliary: g("fan-aux") }),
  },
  "fan-chamber": {
    label: "CHAMBER FAN",
    path: "/print/fan",
    build: (g) => ({ chamber: g("fan-chamber") }),
  },
  "temp-nozzle": {
    label: "NOZZLE TEMP",
    path: "/print/temperature",
    build: (g) => ({ nozzle: g("temp-nozzle") }),
    confirm: (g) => g("temp-nozzle") > 240
      ? `Set nozzle target to ${g("temp-nozzle")}°C ? High-temp; check filament rating.`
      : null,
  },
  "temp-bed": {
    label: "BED TEMP",
    path: "/print/temperature",
    build: (g) => ({ bed: g("temp-bed") }),
    confirm: (g) => g("temp-bed") > 85
      ? `Set bed target to ${g("temp-bed")}°C ? High-temp; check bed adhesive.`
      : null,
  },
  "temp-chamber": {
    label: "CHAMBER TEMP",
    path: "/print/temperature",
    build: (g) => ({ chamber: g("temp-chamber") }),
  },
};

async function applyAdjust(target, getters) {
  const spec = ADJUST_TARGETS[target];
  if (!spec) return;
  const get = (k) => getters[k] ? getters[k]() : 0;
  const confirmMsg = spec.confirm ? spec.confirm(get) : null;
  if (confirmMsg && !confirm(confirmMsg)) return;

  setAdjMsg(`» ${spec.label}…`);
  const btns = document.querySelectorAll("#adjust .btn");
  for (const b of btns) b.disabled = true;

  try {
    const r = await fetch(spec.path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(spec.build(get)),
    });
    if (!r.ok) throw new Error(`HTTP ${r.status} — ${await r.text()}`);
    setAdjMsg(`✓ ${spec.label} acknowledged`, "ok");
  } catch (e) {
    setAdjMsg(`✗ ${spec.label}: ${e.message}`, "err");
  } finally {
    setTimeout(() => { for (const b of btns) b.disabled = false; }, 600);
  }
}

// Hydrate the ADJUST sliders/inputs from a status push. Skips any control
// that currently has focus (so a live SSE update doesn't yank a value out
// from under the user mid-drag or mid-type).
function hydrateAdjust(raw) {
  if (!$("adjust")) return;
  const fans = raw.CurrentFanSpeed || {};
  const targets = [
    ["fan-model",    fans.ModelFan],
    ["fan-aux",      fans.AuxiliaryFan],
    ["fan-chamber",  fans.BoxFan],
    ["temp-nozzle",  raw.TempTargetNozzle],
    ["temp-bed",     raw.TempTargetHotbed],
    ["temp-chamber", raw.TempTargetBox],
  ];
  for (const [base, v] of targets) {
    if (v == null) continue;
    const num = $(`adj-${base}`);
    const slider = $(`adj-${base}-slider`);
    if (!num || !slider) continue;
    if (document.activeElement === num || document.activeElement === slider) continue;
    const rounded = String(Math.round(+v));
    num.value = rounded;
    slider.value = rounded;
  }
}

async function applySpeedMode(mode) {
  setAdjMsg(`» SPEED MODE → ${mode.toUpperCase()}…`);
  const btns = document.querySelectorAll("#adjust .btn");
  for (const b of btns) b.disabled = true;
  try {
    const r = await fetch("/print/speed", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode }),
    });
    if (!r.ok) throw new Error(`HTTP ${r.status} — ${await r.text()}`);
    setAdjMsg(`✓ SPEED MODE → ${mode.toUpperCase()} acknowledged · only effective mid-print`, "ok");
  } catch (e) {
    setAdjMsg(`✗ SPEED MODE: ${e.message}`, "err");
  } finally {
    setTimeout(() => { for (const b of btns) b.disabled = false; }, 600);
  }
}

// Visually highlight the mode that matches the live PrintSpeedPct.
function reflectSpeedMode(pct) {
  if (pct == null) return;
  for (const btn of document.querySelectorAll("#adj-speed-modes .adj-mode")) {
    const v = { silent: 50, balanced: 100, sport: 130, ludicrous: 160 }[btn.dataset.mode];
    btn.classList.toggle("active", +pct === v);
  }
}

function wireAdjust() {
  if (!$("adjust")) return;
  const getters = {
    "fan-model":     bindPair("fan-model"),
    "fan-aux":       bindPair("fan-aux"),
    "fan-chamber":   bindPair("fan-chamber"),
    "temp-nozzle":   bindPair("temp-nozzle"),
    "temp-bed":      bindPair("temp-bed"),
    "temp-chamber":  bindPair("temp-chamber"),
  };
  for (const btn of document.querySelectorAll("#adjust .adj-apply")) {
    btn.addEventListener("click", () => applyAdjust(btn.dataset.target, getters));
  }
  for (const btn of document.querySelectorAll("#adj-speed-modes .adj-mode")) {
    btn.addEventListener("click", () => applySpeedMode(btn.dataset.mode));
  }
}

// ---------------------------------------------------------------------------
// RTSP bridge

function setRtspMsg(text, kind) {
  const el = $("rtsp-msg");
  el.classList.remove("ok", "err", "warn");
  if (kind) el.classList.add(kind);
  el.textContent = text || "";
}

function renderRtsp(info) {
  const panel = $("rtsp-panel");
  if (!info || !info.enabled) {
    panel.hidden = true;
    return;
  }
  panel.hidden = false;

  const state = $("rtsp-state");
  const btn   = $("btn-rtsp-toggle");
  const lbl   = $("btn-rtsp-label");
  const url   = $("rtsp-url");
  const meta  = $("rtsp-meta");

  // URL — prefer the advertised URL that uses the hostname the user hit us with.
  const shown = (info.advertised_urls && info.advertised_urls[0]) ||
                (info.urls && info.urls[0]) ||
                "rtsp://—";
  url.textContent = shown;
  url.classList.toggle("dim", !info.running);

  // Meta: port, path, fps, bitrate
  const parts = [];
  if (info.port)    parts.push(`PORT ${info.port}`);
  if (info.path)    parts.push(`PATH /${info.path}`);
  if (info.fps)     parts.push(`FPS ${info.fps}`);
  if (info.bitrate) parts.push(`BR ${info.bitrate}`);
  meta.textContent = parts.join("   ·   ");

  // State + button
  state.classList.remove("on", "off", "err", "na");
  if (!info.available) {
    state.textContent = "N/A";
    state.classList.add("na");
    lbl.textContent = "N/A";
    btn.disabled = true;
    setRtspMsg(info.reason || "MediaMTX or ffmpeg not available.", "err");
    return;
  }
  if (info.running) {
    state.textContent = "ONLINE";
    state.classList.add("on");
    lbl.textContent = "STOP";
  } else {
    state.textContent = "OFFLINE";
    state.classList.add("off");
    lbl.textContent = "START";
  }
  btn.disabled = false;

  if (info.reason && !info.running) {
    setRtspMsg(info.reason, "warn");
  } else if (info.running) {
    setRtspMsg("MediaMTX running · ffmpeg transcode is on-demand", "ok");
  } else {
    setRtspMsg("", null);
  }
}

async function loadRtsp() {
  try {
    const r = await fetch("/api/rtsp");
    if (!r.ok) { $("rtsp-panel").hidden = true; return; }
    renderRtsp(await r.json());
  } catch (e) {
    $("rtsp-panel").hidden = true;
  }
}

async function toggleRtsp() {
  const btn = $("btn-rtsp-toggle");
  const isRunning = $("rtsp-state").classList.contains("on");
  const path = isRunning ? "/api/rtsp/stop" : "/api/rtsp/start";
  btn.disabled = true;
  setRtspMsg(isRunning ? "stopping MediaMTX…" : "starting MediaMTX…");
  try {
    const r = await fetch(path, { method: "POST" });
    if (!r.ok) throw new Error(`HTTP ${r.status}: ${await r.text()}`);
    renderRtsp(await r.json());
  } catch (e) {
    setRtspMsg(`${isRunning ? "stop" : "start"} failed — ${e.message}`, "err");
    btn.disabled = false;
  }
}

function wireRtsp() {
  $("btn-rtsp-toggle")?.addEventListener("click", toggleRtsp);
  $("btn-rtsp-copy")?.addEventListener("click", async () => {
    const url = $("rtsp-url").textContent;
    const btn = $("btn-rtsp-copy");
    try {
      await navigator.clipboard.writeText(url);
      btn.classList.add("done");
      btn.textContent = "OK";
      setTimeout(() => {
        btn.classList.remove("done");
        btn.textContent = "COPY";
      }, 1200);
    } catch (_) {
      setRtspMsg("clipboard blocked — select the URL and copy manually", "warn");
    }
  });
}

// ---------------------------------------------------------------------------
// Canvas multi-filament (CC2 only)

let canvasControlEnabled = false;

function setCanvasMsg(text, kind) {
  const el = $("canvas-msg");
  el.classList.remove("ok", "warn", "err");
  if (kind) el.classList.add(kind);
  el.textContent = text || "";
}

function renderCanvas(data) {
  const panel = $("canvas-panel");
  // Firmware 01.03.02.51 wraps the payload in canvas_info; tolerate a
  // flat shape too (CanvasStatus.from_payload accepts both).
  const ci = data && (data.canvas_info || (data.canvas_list ? data : null));
  if (!ci) { panel.hidden = true; return; }
  panel.hidden = false;
  const traysEl = $("canvas-trays");
  traysEl.innerHTML = "";

  for (const unit of (ci.canvas_list || [])) {
    for (const t of (unit.tray_list || [])) {
      const loaded = t.status === 1;
      const active = t.tray_id === ci.active_tray_id;
      const row = document.createElement("div");
      row.className = "canvas-tray" + (active ? " active" : "") + (loaded ? "" : " empty");
      const swatch = document.createElement("span");
      swatch.className = "canvas-swatch";
      swatch.style.background = t.filament_color;
      const name = document.createElement("span");
      name.className = "canvas-tray-name";
      name.textContent = t.filament_name;
      const type = document.createElement("span");
      type.className = "canvas-tray-type";
      type.textContent = t.filament_type;
      const temp = document.createElement("span");
      temp.className = "canvas-tray-temp";
      temp.textContent = `${t.min_nozzle_temp}-${t.max_nozzle_temp}°`;
      const stat = document.createElement("span");
      stat.className = "canvas-tray-status";
      stat.textContent = loaded ? "●" : "○";
      row.append(swatch, name, type, temp, stat);
      traysEl.appendChild(row);
    }
  }

  $("canvas-refill-state").textContent = ci.auto_refill ? "ON" : "OFF";
  $("canvas-refill-state").classList.toggle("on", ci.auto_refill);

  const toggleBtn = $("canvas-refill-toggle");
  if (canvasControlEnabled) {
    toggleBtn.hidden = false;
    $("canvas-refill-btn-label").textContent = ci.auto_refill ? "DISABLE" : "ENABLE";
  }
}

let canvasSupported = true;
let canvasRetryTimer = null;

async function loadCanvas() {
  // 501 means "this printer has no Canvas" (CC1) — stop asking. Anything
  // else (504 timeout, 503 reconnecting, network blip) is transient: keep
  // whatever is on screen and retry, so a CC2 that was mid-cooldown at
  // page load doesn't lose its Canvas panel until a manual reload.
  if (!canvasSupported) return;
  if (canvasRetryTimer) { clearTimeout(canvasRetryTimer); canvasRetryTimer = null; }
  try {
    const r = await fetch("/canvas");
    if (r.status === 501) {
      canvasSupported = false;
      $("canvas-panel").hidden = true;
      return;
    }
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    renderCanvas(await r.json());
  } catch (_) {
    canvasRetryTimer = setTimeout(loadCanvas, 30000);
  }
}

async function toggleRefill() {
  const btn = $("canvas-refill-toggle");
  const isOn = $("canvas-refill-state").textContent === "ON";
  btn.disabled = true;
  setCanvasMsg(isOn ? "disabling auto-refill…" : "enabling auto-refill…");
  try {
    const r = await fetch("/canvas/refill", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled: !isOn }),
    });
    if (!r.ok) throw new Error(`HTTP ${r.status}: ${await r.text()}`);
    setCanvasMsg(`auto-refill ${isOn ? "disabled" : "enabled"}`, "ok");
    setTimeout(loadCanvas, 2000);
  } catch (e) {
    setCanvasMsg(`toggle failed — ${e.message}`, "err");
  } finally {
    setTimeout(() => { btn.disabled = false; }, 1500);
  }
}

function wireCanvas() {
  $("canvas-refresh")?.addEventListener("click", () => {
    setCanvasMsg("refreshing…");
    loadCanvas().then(() => setCanvasMsg(""));
  });
  $("canvas-refill-toggle")?.addEventListener("click", toggleRefill);
}

// ---------------------------------------------------------------------------
// Webcam resilience — if the MJPEG stream stalls, reload it.

function wireWebcamKeepalive() {
  const img = $("webcam");
  let lastChange = Date.now();
  // Some browsers fire `load` per MJPEG frame (Firefox), others once or
  // never (Chrome). The staleness reload is therefore a last resort with
  // a long fuse — on a browser that never re-fires `load`, a short fuse
  // would tear down a perfectly healthy stream on every tick, and each
  // reload costs a fresh upstream camera connection.
  img.addEventListener("load", () => { lastChange = Date.now(); });
  img.addEventListener("error", () => {
    setTimeout(() => { img.src = `/stream?t=${Date.now()}`; }, 2000);
  });
  setInterval(() => {
    if (Date.now() - lastChange > 60000) {
      img.src = `/stream?t=${Date.now()}`;
      lastChange = Date.now();
    }
  }, 10000);
}

// ---------------------------------------------------------------------------

// Adaptive backup poll. SSE is the primary update path; this poll keeps
// the UI alive when SSE silently stalls (Firefox in particular). Rate
// follows the printer's state: fast while actively printing, slow when
// idle/paused/done/errored, so we're not hammering the firmware at
// 2 Hz when nothing is happening.
const IDLE_STATUSES = new Set([0, 6, 8, 9, 14]); // idle, paused, stopped, completed, error
const POLL_FAST_MS = 2000;
const POLL_SLOW_MS = 10000;
let pollTimer = null;
let lastPrintStatus = null;

function pollIntervalMs() {
  return (lastPrintStatus != null && !IDLE_STATUSES.has(lastPrintStatus))
    ? POLL_FAST_MS : POLL_SLOW_MS;
}

function schedulePoll() {
  if (pollTimer) clearTimeout(pollTimer);
  pollTimer = setTimeout(async () => {
    await pollOnce();
    schedulePoll();
  }, pollIntervalMs());
}

function noteStatusForPoll(pstatus) {
  const prev = lastPrintStatus;
  lastPrintStatus = pstatus;
  const wasActive = prev != null && !IDLE_STATUSES.has(prev);
  const nowActive = pstatus != null && !IDLE_STATUSES.has(pstatus);
  if (wasActive !== nowActive) schedulePoll(); // retune immediately
}

(async function main() {
  wireControls();
  wireAdjust();
  wireCanvas();
  wireRtsp();
  wireWebcamKeepalive();
  await loadInfo();
  await loadCanvas();
  await loadRtsp();
  await pollOnce();
  connectSSE();
  setInterval(loadInfo, 60000);
  // RTSP state changes are external, poll occasionally.
  setInterval(loadRtsp, 8000);
  schedulePoll();
})();
