// pycentauri web UI
// Consumes /api/info, /status (one-shot), /events/status (SSE), and POSTs to
// /print/{pause,resume,stop} when control is enabled.

const $ = (id) => document.getElementById(id);

const PRINT_STATUS = {
  0:  { label: "idle",       cls: "state-idle" },
  1:  { label: "homing",     cls: "state-printing" },
  2:  { label: "dropping",   cls: "state-printing" },
  3:  { label: "exposing",   cls: "state-printing" },
  4:  { label: "lifting",    cls: "state-printing" },
  5:  { label: "pausing",    cls: "state-paused" },
  6:  { label: "paused",     cls: "state-paused" },
  7:  { label: "stopping",   cls: "state-stopped" },
  8:  { label: "stopped",    cls: "state-stopped" },
  9:  { label: "completed",  cls: "state-idle" },
  10: { label: "checking",   cls: "state-printing" },
  12: { label: "preparing",  cls: "state-printing" },
  13: { label: "printing",   cls: "state-printing" },
  18: { label: "resumed",    cls: "state-printing" },
};

function fmtSeconds(s) {
  if (s == null || Number.isNaN(s)) return "—";
  s = Math.max(0, Math.round(s));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  if (h > 0) return `${h}h ${m.toString().padStart(2, "0")}m`;
  if (m > 0) return `${m}m ${sec.toString().padStart(2, "0")}s`;
  return `${sec}s`;
}

function fmtTemp(v) {
  return (v == null || Number.isNaN(v)) ? "—" : v.toFixed(1);
}

async function loadInfo() {
  try {
    const r = await fetch("/api/info");
    if (!r.ok) return;
    const info = await r.json();
    $("version").textContent = info.version || "—";
    $("host").textContent = info.printer_host || "—";
    $("mainboard").textContent = info.mainboard_id || "—";
    if (info.enable_control) $("controls").hidden = false;

    try {
      const attrs = await (await fetch("/attributes")).json();
      $("printer-name").textContent = attrs.machine_name || attrs.name || "Centauri Carbon";
      $("firmware").textContent = attrs.firmware_version ? `· ${attrs.firmware_version}` : "";
    } catch (_) { /* printer may still be warming up */ }
  } catch (e) {
    console.warn("loadInfo failed:", e);
  }
}

function renderStatus(raw) {
  // raw is the printer's Status dict (SDCP payload).
  const pi = raw.PrintInfo || {};
  const pstatus = pi.Status;
  const filename = pi.Filename || "—";

  $("filename").textContent = filename;

  const progress = pi.Progress;
  $("progress-bar").style.width = (progress != null ? progress : 0) + "%";
  const layerStr = pi.CurrentLayer != null && pi.TotalLayer
    ? ` · layer ${pi.CurrentLayer}/${pi.TotalLayer}`
    : "";
  $("progress-label").textContent = (progress != null ? progress + "%" : "—") + layerStr;

  const info = PRINT_STATUS[pstatus];
  const ps = $("print-status");
  ps.textContent = info ? info.label : (pstatus != null ? `code ${pstatus}` : "—");
  ps.className = info ? info.cls : "";

  $("layer").textContent = (pi.CurrentLayer != null && pi.TotalLayer)
    ? `${pi.CurrentLayer}/${pi.TotalLayer}` : "—";

  // SDCP Ticks are reported in seconds on this firmware.
  const elapsed = pi.CurrentTicks;
  const total = pi.TotalTicks;
  $("elapsed").textContent = fmtSeconds(elapsed);
  $("remaining").textContent = (total != null && elapsed != null)
    ? fmtSeconds(total - elapsed) : "—";

  // Temperatures — firmware gives scalars for actuals and TempTarget* for targets.
  const noz    = raw.TempOfNozzle,   nozT = raw.TempTargetNozzle;
  const bed    = raw.TempOfHotbed,   bedT = raw.TempTargetHotbed;
  const cham   = raw.TempOfBox,      chamT = raw.TempTargetBox;
  $("t-nozzle").textContent        = fmtTemp(noz);
  $("t-nozzle-target").textContent = fmtTemp(nozT);
  $("t-bed").textContent           = fmtTemp(bed);
  $("t-bed-target").textContent    = fmtTemp(bedT);
  $("t-chamber").textContent       = fmtTemp(cham);
  $("t-chamber-target").textContent = fmtTemp(chamT);

  if (raw.CurrenCoord) $("position").textContent = raw.CurrenCoord;
  if (raw.ZOffset != null) $("z-offset").textContent = raw.ZOffset.toFixed(3);
  if (raw.CurrentFanSpeed) {
    $("fans").textContent = Object.entries(raw.CurrentFanSpeed)
      .map(([k, v]) => `${k}=${v}%`).join("  ");
  }

  $("status-dot").className = "dot ok";
}

async function pollOnce() {
  try {
    const r = await fetch("/status");
    if (!r.ok) throw new Error("HTTP " + r.status);
    const body = await r.json();
    if (body.raw) renderStatus(body.raw);
  } catch (e) {
    $("status-dot").className = "dot err";
    console.warn("poll failed:", e);
  }
}

function connectSSE() {
  let src;
  try {
    src = new EventSource("/events/status");
  } catch (e) {
    $("status-dot").className = "dot warn";
    setInterval(pollOnce, 3000);
    return;
  }
  src.addEventListener("status", (ev) => {
    try { renderStatus(JSON.parse(ev.data)); } catch (_) {}
  });
  src.onerror = () => {
    $("status-dot").className = "dot warn";
    // EventSource auto-reconnects; keep a slow poll as a safety net.
    setTimeout(pollOnce, 2000);
  };
}

// --- Controls (only wired when visible) -----------------------------------

async function doAction(name, path) {
  const msg = $("ctrl-msg");
  msg.textContent = `${name}…`;
  for (const b of document.querySelectorAll(".controls button")) b.disabled = true;
  try {
    const r = await fetch(path, { method: "POST" });
    if (!r.ok) throw new Error(`HTTP ${r.status}: ${await r.text()}`);
    msg.textContent = `${name} ✓`;
  } catch (e) {
    msg.textContent = `${name} failed — ${e.message}`;
  } finally {
    setTimeout(() => {
      for (const b of document.querySelectorAll(".controls button")) b.disabled = false;
    }, 800);
  }
}

function wireControls() {
  $("btn-pause").addEventListener("click", () => {
    if (confirm("Pause the current print?")) doAction("pause", "/print/pause");
  });
  $("btn-resume").addEventListener("click", () => doAction("resume", "/print/resume"));
  $("btn-stop").addEventListener("click", () => {
    if (confirm("Stop the current print? This cannot be undone.")) doAction("stop", "/print/stop");
  });
}

// --- Boot ------------------------------------------------------------------

(async function main() {
  await loadInfo();
  wireControls();
  await pollOnce();
  connectSSE();
  // Safety net: refresh info every 60s in case the server was restarted.
  setInterval(loadInfo, 60000);
})();
