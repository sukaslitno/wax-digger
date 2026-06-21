/* popup.js — talks to background.js, which proxies the local YTDLP server. */

const $ = (id) => document.getElementById(id);

const statusDot = $("statusDot");
const statusText = $("statusText");
const launchBtn = $("launchBtn");
const folderPath = $("folderPath");
const urlInput = $("url");
const dlBtn = $("dlBtn");
const progressSection = $("progressSection");
const progressFill = $("progressFill");
const progressNum = $("progressNum");
const progressState = $("progressState");

let serverOnline = false;
let downloading = false;

function send(type, extra = {}) {
  return new Promise((resolve) => {
    chrome.runtime.sendMessage({ type, ...extra }, (resp) => resolve(resp || {}));
  });
}

async function refreshStatus() {
  const s = await send("status");
  serverOnline = !!s.running;

  statusDot.classList.toggle("online", serverOnline);
  statusDot.classList.toggle("offline", !serverOnline);
  statusText.textContent = serverOnline ? "Running" : "Offline";
  launchBtn.hidden = serverOnline;

  if (serverOnline && s.output_path) {
    folderPath.textContent = s.output_path;
  } else if (serverOnline) {
    const f = await send("getFolder");
    folderPath.textContent = f.ok ? f.path : "—";
  } else {
    folderPath.textContent = "—";
  }

  updateButton();
}

function updateButton() {
  dlBtn.disabled = downloading || !serverOnline || !urlInput.value.trim();
}

function setProgress(pct, state, cls) {
  progressSection.hidden = false;
  const w = cls === "done" || cls === "error" ? 100 : Math.max(0, Math.min(100, pct));
  progressFill.style.width = w + "%";
  progressFill.classList.toggle("done", cls === "done");
  progressFill.classList.toggle("error", cls === "error");
  progressNum.textContent = cls === "error" ? "ERR" : Math.round(w) + "%";
  progressState.textContent = state;
  progressState.className = "progress-state" + (cls ? " " + cls : "");
}

function startDownload() {
  const url = urlInput.value.trim();
  if (!url || !serverOnline || downloading) return;
  const format = document.querySelector('input[name="fmt"]:checked').value;

  downloading = true;
  updateButton();
  setProgress(0, "Queued", "");

  const port = chrome.runtime.connect({ name: "download" });
  port.postMessage({ type: "start", url, format });

  port.onMessage.addListener((m) => {
    if (m.type === "progress") setProgress(m.pct, m.state, "");
    else if (m.type === "queued") setProgress(0, "Queued", "");
    else if (m.type === "done") {
      setProgress(100, "Done", "done");
      finish();
    } else if (m.type === "error") {
      setProgress(0, m.message || "Error", "error");
      finish();
    } else if (m.type === "offline") {
      setProgress(0, "Server offline", "error");
      refreshStatus();
      finish();
    }
  });

  port.onDisconnect.addListener(finish);

  function finish() {
    if (!downloading) return;
    downloading = false;
    updateButton();
  }
}

launchBtn.addEventListener("click", async () => {
  await send("openGui");
  statusText.textContent = "Starting…";
  // give the GUI a moment to boot, then re-check
  setTimeout(refreshStatus, 2500);
});

dlBtn.addEventListener("click", startDownload);
urlInput.addEventListener("input", updateButton);
urlInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter") startDownload();
});

refreshStatus();
