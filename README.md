# Wax Digger

A tiny local audio downloader built on **yt-dlp**, with a matching browser
extension. Two pieces:

- **`ytdlp-gui/`** — a local Flask server + web UI (`127.0.0.1:7337`). Shipped as
  a self-contained desktop app: **`WaxDigger.dmg`** (macOS) and
  **`WaxDigger.exe`** (Windows portable), each bundling `yt-dlp` + `ffmpeg`.
- **`ytdlp-extension/`** — a cross-browser extension: a popup, plus a ↓ button
  injected into Bandcamp-linked album cards on `badger.greenlight.hr`.

## Install (users)

Grab the files for your platform from the **[Releases](../../releases)** page:

| You have | Download | How |
| --- | --- | --- |
| macOS | `WaxDigger.dmg` | Open → drag **Wax Digger** to Applications. First launch: right-click → **Open** (unsigned app). |
| Windows | `WaxDigger.exe` | Double-click. SmartScreen → **More info → Run anyway** (unsigned). |
| Chrome/Edge/Brave/Opera/Vivaldi/Arc | `wax-digger-chrome.zip` | Unzip → `chrome://extensions` → Developer mode → **Load unpacked**. |
| Firefox | `.xpi` / `wax-digger-firefox.zip` | `.xpi` = permanent; zip = temporary via `about:debugging`. |

The desktop app bundles everything — no Python, yt-dlp, or ffmpeg to install.
Run it, then use the web UI, the extension popup, or the ↓ button on
badger.greenlight.hr.

## Build / release (maintainers)

Everything is automated by `.github/workflows/release.yml`. Push a version tag:

```bash
git tag v1.0.0
git push origin v1.0.0
```

GitHub Actions then builds and attaches to a single Release:
- the extension packages (Chrome zip + Firefox zip, or signed `.xpi` if AMO secrets are set),
- `WaxDigger.dmg` (built on a macOS runner),
- `WaxDigger.exe` (built on a Windows runner).

### Local builds

```bash
# desktop app (current OS) — needs: pip install pyinstaller pillow flask
bash packaging/build_macos.sh        # macOS  → dist/WaxDigger.dmg
pwsh packaging/build_windows.ps1     # Windows → dist/WaxDigger.exe

# extension
cd ytdlp-extension && node build.js   # → dist/*.zip
```

See [`ytdlp-extension/README.md`](ytdlp-extension/README.md) for extension
details and [`ytdlp-gui/README.md`](ytdlp-gui/README.md) for the server.

## Notes

- Apps are **unsigned** — no Apple/Microsoft developer certs. That's why the
  first-launch override is needed. Code-signing can be added later.
- `packaging/fetch_binaries.py` downloads the right `yt-dlp`/`ffmpeg` for the
  current OS into `packaging/bin/`; `build_*` scripts call it automatically.
