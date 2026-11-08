# Wax Digger

A tiny local audio downloader built on **yt-dlp**, with a matching browser
extension. Two pieces:

- **`ytdlp-gui/`** — a local Flask server (`127.0.0.1:7337`) shown in a native
  frameless window (pywebview), not a browser tab. Shipped as a self-contained
  desktop app: **`WaxDigger.dmg`** (macOS) and **`WaxDigger.exe`** (Windows
  portable), each bundling `yt-dlp` + `ffmpeg`.
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
Run it — the WAX window opens on your desktop. The extension popup and the ↓
button on badger.greenlight.hr talk to the same local server.

Spotify track, album and public playlist links use the bundled GoFi downloader
to find matching tracks on Deezer. A Deezer session (ARL) is required. Wax
accepts an ARL in the app for the current launch; it is not saved by Wax. In a
source checkout, Wax can also use the GoFi config in the local `deezload/`
folder. For an installed build, `gofi_config_path` in
`~/.wax-digger_config.json` can point to an existing GoFi config; Wax stores
only the path there. Keep this config private.

Spotify downloads offer MP3 128, MP3 320 and FLAC. Availability depends on the
Deezer account and catalog. Wax does not silently lower quality, and reports
partial results when tracks cannot be matched or saved. Existing target files
are left untouched. Spotify's services and catalog matching can change, and a
playlist may contain tracks unavailable on Deezer.

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
# desktop app (current OS) — needs Go 1.25+ and: pip install pyinstaller pillow flask pywebview
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
- `vendor/gofi` contains the MIT-licensed GoFi source at upstream commit
  `fa56122`; `cmd/wax-gofi` is Wax's adapter. The bundled app includes its license.
