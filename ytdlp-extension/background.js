/*
 * background.js — service worker / API client for the local YTDLP GUI server.
 *
 * All network traffic goes through here. Because the server hosts are listed in
 * `host_permissions`, fetches from this worker bypass page CORS entirely, so the
 * injected content script never hits a cross-origin wall (it talks to us via
 * messages, we talk to localhost).
 *
 * Two channels:
 *   - one-shot requests (status / folder) via chrome.runtime.onMessage
 *   - the long-running download (POST + SSE progress) via a runtime Port
 */

const SERVER = "http://127.0.0.1:7337";

// ---------- one-shot helpers ----------

async function getStatus() {
  try {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), 2500);
    const r = await fetch(`${SERVER}/status`, { signal: ctrl.signal });
    clearTimeout(timer);
    if (!r.ok) return { running: false };
    const data = await r.json();
    return { running: true, output_path: data.output_path };
  } catch (e) {
    return { running: false };
  }
}

async function getFolder() {
  try {
    const r = await fetch(`${SERVER}/get-folder`);
    if (!r.ok) return { ok: false };
    const data = await r.json();
    return { ok: true, path: data.path };
  } catch (e) {
    return { ok: false };
  }
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg && msg.type === "status") {
    getStatus().then(sendResponse);
    return true; // async
  }
  if (msg && msg.type === "getFolder") {
    getFolder().then(sendResponse);
    return true;
  }
  if (msg && msg.type === "openGui") {
    chrome.tabs.create({ url: `${SERVER}/` });
    sendResponse({ ok: true });
    return false;
  }
});

// ---------- download with live progress (Port) ----------

chrome.runtime.onConnect.addListener((port) => {
  if (port.name !== "download") return;

  port.onMessage.addListener((msg) => {
    if (msg && msg.type === "start") {
      runDownload(port, msg.url, msg.format);
    }
  });
});

async function runDownload(port, url, format) {
  const post = (m) => {
    try {
      port.postMessage(m);
    } catch (e) {
      /* port closed */
    }
  };

  let stream_id;
  try {
    const r = await fetch(`${SERVER}/download`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url, format }),
    });
    const data = await r.json();
    if (data.error) {
      post({ type: "error", message: data.error });
      return;
    }
    stream_id = data.stream_id;
    post({ type: "queued", stream_id });
  } catch (e) {
    post({ type: "offline" });
    return;
  }

  // Stream SSE progress. EventSource isn't available in a service worker, so we
  // read the response body manually. Handles both the dev server's "__DONE__"
  // and the app bundle's "__DONE__:done" / "__DONE__:error" terminators.
  try {
    const r = await fetch(`${SERVER}/progress/${stream_id}`);
    const reader = r.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    let pct = 0;

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });

      const events = buf.split("\n\n");
      buf = events.pop(); // keep trailing partial

      for (const ev of events) {
        const line = ev.replace(/^data:\s?/, "");
        if (!line) continue;

        if (line.startsWith("__DONE__")) {
          const status = line.includes(":") ? line.split(":")[1] : "done";
          if (status === "error") post({ type: "error", message: "Download failed" });
          else post({ type: "done" });
          return;
        }

        if (/^ERROR/i.test(line)) {
          post({ type: "error", message: line });
          continue;
        }

        const m = line.match(/\[download\]\s+([\d.]+)%/);
        if (m) {
          pct = parseFloat(m[1]);
          post({ type: "progress", pct, state: "Downloading" });
          continue;
        }
        if (/\[ExtractAudio\]|Extracting audio|\[ffmpeg\]|Merger|Post-process/i.test(line)) {
          post({ type: "progress", pct, state: "Converting" });
        }
      }
    }
    // stream ended without explicit terminator
    post({ type: "done" });
  } catch (e) {
    post({ type: "error", message: "Lost connection to server" });
  }
}
