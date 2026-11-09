"""Keep yt-dlp current without re-releasing the app.

The yt-dlp shipped inside the app is read-only (macOS .app bundle, or the
temp dir a Windows onefile exe unpacks to on every launch) and its site
extractors go stale within weeks. So Wax keeps its own copy in a per-user data
dir, seeded from the bundled binary, and keeps that copy current with yt-dlp's
own updater (`yt-dlp -U`, which checks the release's SHA2-256SUMS and knows
which asset fits this OS, including the legacy builds for old systems).

The updater never touches the copy downloads use: it works on a staging copy,
checks that the result still runs, and only then swaps it in. Each download
uses whichever working binary is newer, the user copy or the bundled one, so a
fresh app release with a newer bundled yt-dlp wins over a stale user copy.
"""
import os
import re
import shutil
import stat
import subprocess
import sys
import threading
import time

IS_WINDOWS = os.name == "nt"
EXE = ".exe" if IS_WINDOWS else ""

# A regular check runs at most this often (stamped in the config file) ...
CHECK_INTERVAL = 24 * 3600
# ... a failed one (offline, GitHub down) is retried after this long ...
RETRY_INTERVAL = 3600
# ... and one forced by a download that looked like a stale extractor at most
# this often, so a link that is simply broken can't hammer GitHub.
FORCED_INTERVAL = 30 * 60
# how often the background thread looks whether a check is due
TICK = 10 * 60
CONFIG_KEY = "ytdlp_last_check"

VERSION_TIMEOUT = 60      # the macOS onefile binary unpacks itself first
UPDATE_TIMEOUT = 300


def data_dir():
    """Per-user, writable app data dir. WAX_DIGGER_DATA_DIR overrides it."""
    override = os.environ.get("WAX_DIGGER_DATA_DIR")
    if override:
        return override
    home = os.path.expanduser("~")
    if sys.platform == "darwin":
        return os.path.join(home, "Library", "Application Support", "Wax Digger")
    if IS_WINDOWS:
        base = os.environ.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
        return os.path.join(base, "Wax Digger")
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(home, ".local", "share")
    return os.path.join(base, "wax-digger")


def bin_dir():
    return os.path.join(data_dir(), "bin")


def user_binary():
    return os.path.join(bin_dir(), "yt-dlp" + EXE)


_VERSION_RE = re.compile(r"^(\d{4})\.(\d{1,2})\.(\d{1,2})(?:\.(\d+))?$")


def parse_version(text):
    """'2026.10.26' -> (2026, 10, 26, 0); nightly '2026.10.26.123456' keeps the
    build number. Anything else -> None."""
    lines = (text or "").strip().splitlines()
    m = _VERSION_RE.match(lines[0].strip()) if lines else None
    if not m:
        return None
    return tuple(int(g or 0) for g in m.groups())


def format_version(version):
    if not version:
        return None
    text = "%04d.%02d.%02d" % version[:3]
    return text + (".%d" % version[3] if version[3] else "")


def is_newer(a, b):
    """True if version a is strictly newer than b (None = unknown/broken)."""
    if a is None:
        return False
    return b is None or a > b


def child_kwargs():
    # never flash a console window for a helper process on Windows
    return {"creationflags": 0x08000000} if IS_WINDOWS else {}  # CREATE_NO_WINDOW


def run_version(cmd):
    """Run `<cmd> --version`; the parsed version, or None if it doesn't run."""
    try:
        r = subprocess.run(list(cmd) + ["--ignore-config", "--version"], capture_output=True,
                           text=True, errors="replace", timeout=VERSION_TIMEOUT, **child_kwargs())
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return parse_version(r.stdout) if r.returncode == 0 else None


def run_self_update(path):
    """Run yt-dlp's own updater on `path` (in place). True if it exited cleanly.

    Exit codes: 0 = updated or already current, non-zero (100) = failed. The
    result is judged by `--version` before/after, not by parsing this output.
    """
    try:
        r = subprocess.run([path, "--ignore-config", "-U"], capture_output=True, text=True,
                           errors="replace", timeout=UPDATE_TIMEOUT, **child_kwargs())
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        log(f"yt-dlp update failed to run: {e}")
        return False
    if r.returncode != 0:
        tail = (r.stdout + r.stderr).strip().splitlines()[-3:]
        log(f"yt-dlp update failed ({r.returncode}): {' | '.join(tail)}")
        return False
    return True


def make_executable(path):
    if not IS_WINDOWS:
        mode = os.stat(path).st_mode
        os.chmod(path, mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def log(message):
    print(f"[wax] {message}", file=sys.stderr, flush=True)


class YtdlpManager:
    """Which yt-dlp a download runs, and keeping the user copy current.

    base():     the yt-dlp Wax would use without a user copy (bundled, or a
                system one in a source checkout) as a command list
    bundled():  path of the bundled standalone binary, or None; the only thing
                a user copy is ever seeded from
    """

    def __init__(self, base, bundled, load_config, save_config, now=time.time):
        self.base = base
        self.bundled = bundled
        self.load_config = load_config
        self.save_config = save_config
        self.now = now
        self._selected = None          # (cmd, version) or None = not resolved yet
        self._select_lock = threading.Lock()
        self._update_lock = threading.Lock()
        self._busy_lock = threading.Lock()
        self._downloads = 0
        self._last_attempt = 0.0       # any check, in memory
        self._last_forced = 0.0
        self._version_cache = {}       # (cmd, mtime, size) -> version

    # -- which binary ------------------------------------------------------
    def version_of(self, cmd):
        key = tuple(cmd)
        try:
            st = os.stat(cmd[0])
            key += (st.st_mtime_ns, st.st_size)
        except (OSError, IndexError):
            pass
        if key not in self._version_cache:
            self._version_cache[key] = run_version(cmd)
        return self._version_cache[key]

    def _resolve(self):
        base = list(self.base())
        base_version = self.version_of(base)
        user = user_binary()
        if os.path.isfile(user):
            user_version = self.version_of([user])
            # a broken user copy (None) never wins; on a tie the bundled one does
            if is_newer(user_version, base_version):
                return [user], user_version
        return base, base_version

    def current(self):
        """Command list for the next download. Resolved once, again after an update."""
        with self._select_lock:
            if self._selected is None:
                self._selected = self._resolve()
            return list(self._selected[0])

    def known_version(self):
        """Version string of the yt-dlp in use, without running anything (None if not resolved yet)."""
        selected = self._selected
        return format_version(selected[1]) if selected else None

    def refresh(self):
        with self._select_lock:
            self._selected = None
        self.current()

    # -- downloads ---------------------------------------------------------
    def begin_download(self):
        with self._busy_lock:
            self._downloads += 1

    def end_download(self):
        with self._busy_lock:
            self._downloads = max(0, self._downloads - 1)

    def busy(self):
        return self._downloads > 0

    # -- updating ----------------------------------------------------------
    def force_due(self):
        """Would check(force=True) actually run now?"""
        return (not self.busy() and not self._update_lock.locked()
                and self.now() - self._last_forced >= FORCED_INTERVAL)

    def check(self, force=False):
        """Update the user copy if a check is due and no download is running.

        Returns "updated" (a newer yt-dlp is now in use), "current", "failed",
        or "skipped" (throttled, busy, another check running, nothing to seed from).
        """
        if not self._update_lock.acquire(blocking=False):
            return "skipped"
        try:
            now = self.now()
            if force:
                if now - self._last_forced < FORCED_INTERVAL:
                    return "skipped"
            else:
                last = self.load_config().get(CONFIG_KEY, 0)
                if not isinstance(last, (int, float)):
                    last = 0
                if now - last < CHECK_INTERVAL or now - self._last_attempt < RETRY_INTERVAL:
                    return "skipped"
            if self.busy():
                return "skipped"
            self._last_attempt = now
            if force:
                self._last_forced = now
            result = self._update()
            if result in ("updated", "current"):
                cfg = self.load_config()
                cfg[CONFIG_KEY] = now
                self.save_config(cfg)
            return result
        except Exception as e:  # an update must never take the app down
            log(f"yt-dlp update error: {e}")
            return "failed"
        finally:
            self._update_lock.release()

    def _update(self):
        user = user_binary()
        user_version = self.version_of([user]) if os.path.isfile(user) else None
        bundled = self.bundled()
        bundled_version = self.version_of([bundled]) if bundled else None
        # start from the newer working binary, so a stale user copy is reseeded
        if is_newer(bundled_version, user_version):
            source, source_version = bundled, bundled_version
        elif user_version is not None:
            source, source_version = user, user_version
        else:
            return "skipped"  # nothing to seed from (source checkout without a bundled binary)
        in_use = self._selected[1] if self._selected else None

        os.makedirs(bin_dir(), exist_ok=True)
        self._sweep()
        staging = os.path.join(bin_dir(), "yt-dlp.staging" + EXE)
        shutil.copyfile(source, staging)
        make_executable(staging)
        try:
            if not run_self_update(staging):
                return "failed"
            new_version = run_version([staging])
            if new_version is None:
                log("updated yt-dlp does not run, keeping the previous one")
                return "failed"
            if not is_newer(new_version, user_version):
                return "current"  # the user copy is already this version
            if not self._install(staging, user):
                return "failed"
            log(f"yt-dlp {format_version(source_version)} -> {format_version(new_version)}")
        finally:
            self._remove(staging)
        self.refresh()
        return "updated" if is_newer(self._selected[1], in_use) else "current"

    def _install(self, staging, user):
        # The swap waits for downloads (one may still run the old copy, and
        # Windows can't replace a running exe); a download starting meanwhile
        # waits a moment on the lock.
        with self._busy_lock:
            if self._downloads:
                log("a download started, the yt-dlp update waits for the next check")
                return False
            for attempt in range(5):
                try:
                    os.replace(staging, user)
                    return True
                except PermissionError:
                    # Windows: still held by an exiting process or an AV scan.
                    # Move it aside (allowed for a running exe), then retry.
                    try:
                        os.replace(user, user + ".old")
                    except OSError:
                        pass
                    time.sleep(0.5 * (attempt + 1))
                except OSError as e:
                    log(f"couldn't install the yt-dlp update: {e}")
                    break
            # put the previous copy back if it was moved aside
            if not os.path.exists(user) and os.path.exists(user + ".old"):
                try:
                    os.replace(user + ".old", user)
                except OSError:
                    pass  # no user copy then: the bundled one is used, the next check reseeds
            return False

    def _sweep(self):
        """Leftovers: an old staging copy, yt-dlp's own *.old on Windows."""
        try:
            names = os.listdir(bin_dir())
        except OSError:
            return
        for name in names:
            if name.endswith(".old") or name.startswith("yt-dlp.staging"):
                self._remove(os.path.join(bin_dir(), name))

    @staticmethod
    def _remove(path):
        try:
            os.remove(path)
        except OSError:
            pass

    def start(self):
        """Background thread: resolve the binary, then check whenever one is due."""
        def loop():
            self.current()
            while True:
                self.check()
                time.sleep(TICK)
        threading.Thread(target=loop, daemon=True, name="ytdlp-update").start()


# Errors that usually mean the site changed and yt-dlp's extractor is behind.
# Deliberately not: sign-in / bot checks, private, geo, rate limits, network.
OUTDATED_HINTS = (
    "unable to extract", "please report this issue", "yt-dlp -u", "failed to extract",
    "http error 403",  # on YouTube the classic symptom of stale signature code
)
# These say so on their own, whatever else the line mentions.
OUTDATED_SURE = (
    "signature extraction failed", "nsig extraction failed",
    "no video formats found", "requested format is not available",
)
