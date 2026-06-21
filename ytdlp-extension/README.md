# Wax Digger — Browser Extension

Companion extension for the local **YTDLP GUI** server (`127.0.0.1:7337`). It
mirrors the app's paper/VFD look (JetBrains Mono, cream + ink palette, sharp
corners) and adds:

1. **A popup** — server status, current download folder, URL + format + Download.
2. **A ↓ button on [badger.greenlight.hr](https://badger.greenlight.hr)** —
   injected into every album card that links to Bandcamp; one click picks a
   format and sends the album to the local server.

Works in any browser: **Chrome, Edge, Brave, Opera, Vivaldi, Arc, Firefox, and
Safari** (one codebase; per-browser packages are built automatically).

---

## Install (for users)

> ⚠️ The extension only *controls* the local server — the **YTDLP GUI** app must
> be running for downloads to work. If it's off, the extension shows a
> **Launch YTDLP GUI** button.

### Easiest: from the GitHub Releases page

Go to the repo's **Releases** and download the package for your browser:

#### Chrome · Edge · Brave · Opera · Vivaldi · Arc
1. Download **`wax-digger-chrome.zip`** and unzip it.
2. Open `chrome://extensions` (Edge: `edge://extensions`, etc.).
3. Toggle **Developer mode** (top-right).
4. Click **Load unpacked** and select the unzipped folder.
5. Pin the icon — done. It stays installed across restarts.

#### Firefox
- If a signed **`.xpi`** is attached to the release: open it in Firefox → install → permanent.
- Otherwise: `about:debugging#/runtime/this-firefox` → **Load Temporary Add-on** → pick `manifest.json` from the unzipped firefox build (clears on restart).
- On first use, open `about:addons` → Wax Digger → **Permissions** and allow access to `127.0.0.1` if prompted.

#### Safari (macOS)
Safari needs a one-time local build (see [Safari](#safari-1) below).

### Alternative: clone the repo (Chromium)
```bash
git clone <repo-url>
# chrome://extensions → Developer mode → Load unpacked → select ytdlp-extension/
```
The `ytdlp-extension/` folder is a ready-to-load Chromium extension as-is (icons
are committed), so no build step is needed for Chromium browsers.

---

## Build & release (for maintainers)

```bash
cd ytdlp-extension
node generate-icons.js   # (only if icons are missing)
node build.js            # → dist/wax-digger-chrome.zip, dist/wax-digger-firefox.zip
```

`build.js` has **zero dependencies**. It produces:

| Output | For |
| --- | --- |
| `dist/chrome/` + `wax-digger-chrome.zip` | Chrome/Chromium + Chrome Web Store |
| `dist/firefox/` + `wax-digger-firefox.zip` | Firefox + AMO (manifest patched for Gecko) |

### Cutting a release (GitHub Actions)

Tagging a version builds the packages and attaches them to a GitHub Release
automatically (`.github/workflows/release.yml`):

```bash
git tag v1.0.0
git push origin v1.0.0
```

To get a **signed, one-click, permanent Firefox `.xpi`**, add two repo secrets
(Settings → Secrets and variables → Actions), obtained free from
<https://addons.mozilla.org/developers/addon/api/key/>:

- `AMO_JWT_ISSUER`
- `AMO_JWT_SECRET`

Without them the workflow attaches an unsigned Firefox zip instead (temporary
install only).

### Safari

Safari extensions must be wrapped in an Xcode app. On macOS with Xcode:

```bash
cd ytdlp-extension
./scripts/make-safari.sh      # → dist/safari/ Xcode project
```

Open the generated project, set a signing team, and Run. For unsigned local use,
enable Safari → Develop → **Allow Unsigned Extensions**.

---

## Architecture

| File | Role |
| --- | --- |
| `manifest.json` | Manifest V3 (canonical, Chromium). Host permissions: `localhost:7337`, `127.0.0.1:7337`, `badger.greenlight.hr`. |
| `background.js` | Service worker / API client. All network traffic flows through here, so it bypasses page CORS. Handles `/status`, `/get-folder`, `/download`, and streams `/progress` (SSE) over a runtime Port. |
| `popup.html/.css/.js` | The toolbar popup. |
| `content.js` / `content.css` | Injected into badger.greenlight.hr — MutationObserver finds Bandcamp links and drops the ↓ button + mini format popup. |
| `generate-icons.js` | Generates `icons/icon{16,32,48,128}.png` (zero deps, hand-rolled 4×-supersampled PNG encoder). |
| `build.js` | Builds per-browser packages into `dist/`. |
| `scripts/make-safari.sh` | Converts the build into a Safari Web Extension Xcode project. |

The code uses the `chrome.*` API namespace, which Firefox also supports — so a
single source runs everywhere; only the manifest is tweaked per browser at build
time.

## Server requirements

Already present in `ytdlp-gui/app.py`:

- `GET /status` → `{ "status": "running", "output_path": "…" }`
- CORS headers that echo back `chrome-extension://…` / `moz-extension://…` origins.

## Why not a single "install everywhere" button?

There isn't one — each browser engine controls installs differently. The closest
to it: publish to the Chrome Web Store + Firefox AMO for true one-click installs.
This repo instead ships packaged builds on the Releases page, which keeps
distribution free and self-hosted while staying simple for each browser.
