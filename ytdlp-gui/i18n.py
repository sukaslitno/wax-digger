"""Server-side copy in English and Russian.

Every message the server shows (errors, status lines, dialogs, menu) is a key
here; the page has its own dictionary for its own text (templates/index.html).
A value with a count in front of a noun is a list of plural forms: English
has two (one, other), Russian three (one, few, many).
"""
import locale
import os
import subprocess
import sys

LANGUAGES = ("en", "ru")
DEFAULT_LANGUAGE = "en"


def plural_index(lang, n):
    """Which plural form a number takes: 0 = one, 1 = few (ru) / other (en), 2 = many (ru)."""
    n = abs(int(n))
    if lang == "ru":
        if n % 10 == 1 and n % 100 != 11:
            return 0
        if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
            return 1
        return 2
    return 0 if n == 1 else 1


STRINGS = {
    "en": {
        # yt-dlp errors, shown on the status line
        "err.drm": "this one is drm-protected",
        "err.rate": "too many requests, try again later",
        "err.ffmpeg": "ffmpeg is missing",
        "err.private": "this one is private",
        "err.age": "this one is age-restricted",
        "err.bot": "youtube wants a sign-in, try again later",
        "err.country": "not available in your country",
        "err.signin": "this one needs a sign-in",
        "err.not_started": "this stream hasn't started yet",
        "err.unsupported": "this link isn't supported",
        "err.gone": "this one is gone or unavailable",
        "err.forbidden": "the site refused the download, try again later",
        "err.network": "no connection, check your internet",
        "err.disk": "the disk is full",
        "err.no_write": "can't write to that folder",
        "err.generic": "couldn't grab this one",
        "err.updated": "updated yt-dlp — try again",
        # progress / status lines
        "st.updating": "updating yt-dlp…",
        "st.track": "track {n} of {m}",
        "st.converting_n": "converting {n} of {m}",
        "st.converting": "converting…",
        "st.downloading": "downloading…",
        "st.video": "downloading video…",
        "st.audio": "downloading audio…",
        "st.merging": "merging…",
        "st.connecting": "connecting to deezer…",
        "st.finding": "finding tracks on deezer…",
        "st.finding_n": "finding track {n} of {m} on deezer…",
        # what wax-gofi says when the whole link fails
        "go.bad_request": "invalid download request",
        "go.bad_quality": "unsupported deezer quality",
        "go.use_spotify": "use a spotify track, album, or playlist link",
        "go.connect": "could not connect to deezer. check your arl and connection.",
        "go.link_not_found": "could not find this spotify link on deezer: {detail}",
        "go.album": "could not check spotify album: {detail}",
        "go.no_matches": "no matching tracks found on deezer",
        # why one track was skipped or failed (details screen)
        "why.already_there": "already there",
        "why.not_found": "not found on deezer",
        "why.no_details": "spotify gave no details for this track",
        "why.no_file": "deezer has no file for this track",
        "why.session": "deezer session expired, reconnect deezer",
        "why.network": "connection lost while downloading",
        "why.refused": "deezer refused the download",
        "why.tagging": "couldn't write the tags",
        "why.cancelled": "cancelled",
        "why.generic": "download failed",
        "why.license": "not available in {label} for this account",
        "log.track_n": "track {n}",
        # answers of /download, /deezer-session
        "api.url_required": "url is required",
        "api.unknown_format": "unknown format",
        "api.spotify_formats": "deezer supports mp3 128k, mp3 320k and flac",
        "api.no_gofi": "deezer downloader is not installed in this build",
        "api.connect_first": "connect deezer first",
        "api.youtube_only": "video is for youtube links only",
        "api.arl_length": "arl must be 192 characters",
        # native bits
        "quit.title": "quit wax?",
        "quit.message": "a download is still running. quitting will stop it.",
        "menu.open": "Open Wax",
        "menu.hide": "Hide Wax",
        "menu.quit": "Quit Wax",
    },
    "ru": {
        "err.drm": "тут drm, это не скачать",
        "err.rate": "слишком много запросов, подожди немного",
        "err.ffmpeg": "ffmpeg не найден",
        "err.private": "это приватное",
        "err.age": "тут возрастное ограничение",
        "err.bot": "youtube просит войти, попробуй позже",
        "err.country": "недоступно в твоей стране",
        "err.signin": "нужно войти в аккаунт",
        "err.not_started": "эфир ещё не начался",
        "err.unsupported": "эта ссылка не поддерживается",
        "err.gone": "удалено или недоступно",
        "err.forbidden": "сайт не отдаёт файл, попробуй позже",
        "err.network": "нет связи, проверь интернет",
        "err.disk": "диск забит",
        "err.no_write": "в эту папку не записать",
        "err.generic": "не вышло скачать",
        "err.updated": "обновил yt-dlp — попробуй ещё раз",
        "st.updating": "обновляю yt-dlp…",
        "st.track": "трек {n} из {m}",
        "st.converting_n": "конвертирую {n} из {m}",
        "st.converting": "конвертирую…",
        "st.downloading": "качаю…",
        "st.video": "качаю видео…",
        "st.audio": "качаю звук…",
        "st.merging": "склеиваю…",
        "st.connecting": "стучусь в deezer…",
        "st.finding": "ищу треки на deezer…",
        "st.finding_n": "ищу на deezer: {n} из {m}…",
        "go.bad_request": "не удалось разобрать запрос",
        "go.bad_quality": "deezer не поддерживает это качество",
        "go.use_spotify": "нужна ссылка на трек, альбом или плейлист spotify",
        "go.connect": "не достучался до deezer — проверь arl и интернет",
        "go.link_not_found": "не нашёл эту ссылку spotify на deezer: {detail}",
        "go.album": "не вышло проверить альбом spotify: {detail}",
        "go.no_matches": "на deezer ничего не нашлось",
        "why.already_there": "уже есть",
        "why.not_found": "нет на deezer",
        "why.no_details": "spotify не отдал данные трека",
        "why.no_file": "у deezer нет файла для этого трека",
        "why.session": "сессия deezer протухла — подключи заново",
        "why.network": "связь отвалилась во время загрузки",
        "why.refused": "deezer отказал в загрузке",
        "why.tagging": "не вышло записать теги",
        "why.cancelled": "отменено",
        "why.generic": "не скачалось",
        "why.license": "недоступно в {label} для этого аккаунта",
        "log.track_n": "трек {n}",
        "api.url_required": "нужна ссылка",
        "api.unknown_format": "неизвестный формат",
        "api.spotify_formats": "deezer умеет mp3 128k, mp3 320k и flac",
        "api.no_gofi": "в этой сборке нет загрузчика deezer",
        "api.connect_first": "сначала подключи deezer",
        "api.youtube_only": "видео — только для ссылок youtube",
        "api.arl_length": "в arl должно быть 192 символа",
        "quit.title": "выйти из wax?",
        "quit.message": "загрузка ещё идёт. выйдешь — она остановится.",
        "menu.open": "Открыть Wax",
        "menu.hide": "Скрыть Wax",
        "menu.quit": "Выйти из Wax",
    },
}

_language = DEFAULT_LANGUAGE


def normalize(value):
    """'ru', 'ru-RU', 'ru_RU.UTF-8' -> 'ru'; anything unsupported -> None."""
    code = str(value or "").strip().lower().replace("_", "-").split("-")[0].split(".")[0]
    return code if code in LANGUAGES else None


def system_language():
    """Russian if the OS interface language is Russian, else English."""
    try:
        if sys.platform == "darwin":
            out = subprocess.run(["defaults", "read", "-g", "AppleLanguages"],
                                 capture_output=True, text=True, timeout=2).stdout
            first = next((ln.strip().strip('",()') for ln in out.splitlines()
                          if ln.strip().strip('",()')), "")
            return normalize(first) or DEFAULT_LANGUAGE
        if os.name == "nt":
            import ctypes
            lang_id = ctypes.windll.kernel32.GetUserDefaultUILanguage()
            return "ru" if lang_id & 0x3FF == 0x19 else DEFAULT_LANGUAGE
    except Exception:
        pass
    for var in ("LC_ALL", "LC_MESSAGES", "LANG"):
        if os.environ.get(var):
            return normalize(os.environ[var]) or DEFAULT_LANGUAGE
    try:
        return normalize(locale.getlocale()[0]) or DEFAULT_LANGUAGE
    except Exception:
        return DEFAULT_LANGUAGE


def get_language():
    return _language


def set_language(value):
    """Switch the language of everything the server says from now on."""
    global _language
    code = normalize(value)
    if code is None:
        return False
    _language = code
    return True


def tr(key, lang=None, **kw):
    """The text for a key in the current (or given) language; a list value is
    plural forms, picked by the count `n`. Unknown keys fall back to English."""
    lang = lang or _language
    text = STRINGS.get(lang, {}).get(key)
    if text is None:
        text = STRINGS["en"][key]
    if isinstance(text, (list, tuple)):
        text = text[min(plural_index(lang, kw.get("n", 1)), len(text) - 1)]
    return text.format(**kw) if kw else text
