/*
 * content.js — injects a ↓ download button into album cards on
 * badger.greenlight.hr.
 *
 * For every card that contains a bandcamp.com link we drop a small ↓ button.
 * Clicking it opens a mini-popup with a format picker; picking a format sends
 * the bandcamp URL to the local YTDLP server (via background.js). If the server
 * isn't running, the mini-popup shows a "Launch YTDLP GUI" action instead.
 *
 * The page is a SPA, so cards arrive/refresh continuously — a MutationObserver
 * keeps re-scanning.
 */

(() => {
  const FORMATS = [
    ["mp3_128", "128k"],
    ["mp3_320", "320k"],
    ["flac", "FLAC"],
    ["wav", "WAV"],
    ["aac", "AAC"],
  ];

  const processed = new WeakSet();
  let openPopup = null;

  // ---------- background bridge ----------
  const ask = (type, extra = {}) =>
    new Promise((resolve) => {
      chrome.runtime.sendMessage({ type, ...extra }, (resp) => resolve(resp || {}));
    });

  // ---------- card discovery ----------
  function findBandcampUrl(link) {
    const href = link.getAttribute("href") || link.href || "";
    return href.includes("bandcamp.com") ? href : null;
  }

  function findCard(link) {
    return (
      link.closest(
        '[class*="card" i], [class*="album" i], [class*="release" i], [class*="item" i], article, li'
      ) || link.parentElement
    );
  }

  function scan(root) {
    const links = (root.querySelectorAll
      ? root.querySelectorAll('a[href*="bandcamp.com"]')
      : []);
    links.forEach((link) => {
      const url = findBandcampUrl(link);
      if (!url) return;
      const card = findCard(link);
      if (!card || processed.has(card)) return;
      processed.add(card);
      injectButton(card, url);
    });
  }

  // ---------- button injection ----------
  function injectButton(card, url) {
    if (getComputedStyle(card).position === "static") {
      card.style.position = "relative";
    }

    const btn = document.createElement("button");
    btn.className = "wd-btn";
    btn.type = "button";
    btn.title = "Download with Wax Digger";
    btn.textContent = "↓";
    btn.dataset.wdUrl = url;

    btn.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      togglePopup(btn, url);
    });

    card.appendChild(btn);
  }

  // ---------- mini-popup ----------
  function closePopup() {
    if (openPopup) {
      openPopup.remove();
      openPopup = null;
      document.removeEventListener("click", onDocClick, true);
      window.removeEventListener("scroll", closePopup, true);
      window.removeEventListener("resize", closePopup, true);
    }
  }

  function onDocClick(e) {
    if (openPopup && !openPopup.contains(e.target)) closePopup();
  }

  async function togglePopup(btn, url) {
    if (openPopup && openPopup.dataset.owner === url && openPopup.isConnected) {
      closePopup();
      return;
    }
    closePopup();

    const pop = document.createElement("div");
    pop.className = "wd-pop";
    pop.dataset.owner = url;
    pop.innerHTML = '<div class="wd-pop-title">Checking server…</div>';
    document.body.appendChild(pop);
    openPopup = pop;

    positionPopup(pop, btn);

    document.addEventListener("click", onDocClick, true);
    window.addEventListener("scroll", closePopup, true);
    window.addEventListener("resize", closePopup, true);

    const status = await ask("status");
    if (!openPopup || openPopup !== pop) return; // closed meanwhile

    if (!status.running) {
      renderOffline(pop);
    } else {
      renderFormats(pop, btn, url);
    }
    positionPopup(pop, btn);
  }

  function positionPopup(pop, btn) {
    const r = btn.getBoundingClientRect();
    const pw = pop.offsetWidth || 150;
    let left = r.right - pw;
    if (left < 8) left = 8;
    if (left + pw > window.innerWidth - 8) left = window.innerWidth - 8 - pw;
    let top = r.bottom + 6;
    if (top + pop.offsetHeight > window.innerHeight - 8) {
      top = r.top - pop.offsetHeight - 6;
    }
    pop.style.left = left + "px";
    pop.style.top = Math.max(8, top) + "px";
  }

  function renderOffline(pop) {
    pop.innerHTML = '<div class="wd-pop-title">Server offline</div>';
    const launch = document.createElement("button");
    launch.className = "wd-launch";
    launch.type = "button";
    launch.textContent = "Launch YTDLP GUI";
    launch.addEventListener("click", async (e) => {
      e.stopPropagation();
      await ask("openGui");
      pop.querySelector(".wd-pop-title").textContent = "Starting…";
    });
    pop.appendChild(launch);
  }

  function renderFormats(pop, btn, url) {
    pop.innerHTML = '<div class="wd-pop-title">Format</div>';
    const grid = document.createElement("div");
    grid.className = "wd-fmts";
    FORMATS.forEach(([value, label]) => {
      const f = document.createElement("button");
      f.className = "wd-fmt";
      f.type = "button";
      f.textContent = label;
      f.addEventListener("click", (e) => {
        e.stopPropagation();
        closePopup();
        startDownload(btn, url, value);
      });
      grid.appendChild(f);
    });
    pop.appendChild(grid);
  }

  // ---------- download + button feedback ----------
  function startDownload(btn, url, format) {
    btn.classList.add("wd-busy");
    btn.classList.remove("wd-done", "wd-error");
    btn.textContent = "0%";

    const port = chrome.runtime.connect({ name: "download" });
    port.postMessage({ type: "start", url, format });

    let settled = false;
    const reset = (ms) =>
      setTimeout(() => {
        btn.classList.remove("wd-busy", "wd-done", "wd-error");
        btn.textContent = "↓";
      }, ms);

    port.onMessage.addListener((m) => {
      if (m.type === "progress") {
        btn.textContent = m.state === "Converting" ? "···" : Math.round(m.pct) + "%";
      } else if (m.type === "done") {
        settled = true;
        btn.classList.remove("wd-busy");
        btn.classList.add("wd-done");
        btn.textContent = "✓";
        reset(2500);
      } else if (m.type === "error" || m.type === "offline") {
        settled = true;
        btn.classList.remove("wd-busy");
        btn.classList.add("wd-error");
        btn.textContent = "✗";
        reset(3000);
      }
    });

    port.onDisconnect.addListener(() => {
      if (!settled) {
        btn.classList.remove("wd-busy");
        btn.textContent = "↓";
      }
    });
  }

  // ---------- observe SPA changes ----------
  let pending = false;
  const observer = new MutationObserver(() => {
    if (pending) return;
    pending = true;
    requestAnimationFrame(() => {
      pending = false;
      scan(document);
    });
  });

  observer.observe(document.documentElement, { childList: true, subtree: true });
  scan(document);
})();
