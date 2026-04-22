// pycentauri web console
// Consumes /api/info, /status, /events/status (SSE), /attributes.
// Posts to /print/{pause,resume,stop} when --enable-control is on.

const $ = (id) => document.getElementById(id);

// PrintInfo.Status → display label + semantic class.
// Codes decoded from CentauriLink + official elegoo-link + live observation.
const PRINT_STATUS = {
  0:  { label: "IDLE",       cls: "state-idle" },
  1:  { label: "HOMING",     cls: "state-printing" },
  2:  { label: "DROPPING",   cls: "state-printing" },
  3:  { label: "EXPOSING",   cls: "state-printing" },
  4:  { label: "LIFTING",    cls: "state-printing" },
  5:  { label: "PAUSING",    cls: "state-paused" },
  6:  { label: "PAUSED",     cls: "state-paused" },
  7:  { label: "STOPPING",   cls: "state-stopped" },
  8:  { label: "STOPPED",    cls: "state-stopped" },
  9:  { label: "COMPLETED",  cls: "state-completed" },
  10: { label: "CHECKING",   cls: "state-printing" },
  12: { label: "PREPARING",  cls: "state-printing" },
  13: { label: "PRINTING",   cls: "state-printing" },
  18: { label: "RESUMED",    cls: "state-printing" },
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

    if (info.enable_control) $("controls").hidden = false;

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
  sw.classList.remove("state-idle", "state-printing", "state-paused", "state-stopped", "state-completed");
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
  const setFan = (id, key) => {
    const v = fans[key];
    $(id).textContent = (v != null) ? `${v}%` : "—";
  };
  setFan("fan-model", "ModelFan");
  setFan("fan-aux",   "AuxiliaryFan");
  setFan("fan-box",   "BoxFan");

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
    setInterval(pollOnce, 3000);
    return;
  }
  src.addEventListener("status", (ev) => {
    try { renderStatus(JSON.parse(ev.data)); } catch (_) {}
  });
  src.onerror = () => {
    setStatusPill("warn", "LINK WAIT");
    setTimeout(pollOnce, 2000);
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
// Webcam resilience — if the MJPEG stream stalls, reload it.

function wireWebcamKeepalive() {
  const img = $("webcam");
  let lastChange = Date.now();
  let loads = 0;
  img.addEventListener("load", () => { lastChange = Date.now(); loads++; });
  img.addEventListener("error", () => {
    setTimeout(() => { img.src = `/stream?t=${Date.now()}`; }, 2000);
  });
  setInterval(() => {
    if (Date.now() - lastChange > 15000) {
      img.src = `/stream?t=${Date.now()}`;
      lastChange = Date.now();
    }
  }, 5000);
}

// ---------------------------------------------------------------------------

(async function main() {
  wireControls();
  wireRtsp();
  wireWebcamKeepalive();
  await loadInfo();
  await loadRtsp();
  await pollOnce();
  connectSSE();
  setInterval(loadInfo, 60000);
  // RTSP state changes are external, poll occasionally.
  setInterval(loadRtsp, 8000);
})();
