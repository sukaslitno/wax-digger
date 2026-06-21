# YTDLP GUI

Веб-интерфейс для загрузки аудио через yt-dlp. Работает локально на `localhost:7337`.

## Зависимости

- Python 3.8+
- [yt-dlp](https://github.com/yt-dlp/yt-dlp) — установить через `brew install yt-dlp` или `pip3 install yt-dlp`
- ffmpeg — `brew install ffmpeg`

## Разработка

```bash
pip3 install flask
python3 app.py
# открыть http://localhost:7337
```

## macOS App Bundle

Дважды кликнуть `YTDLPGui.app` (или перетащить в /Applications).  
При первом запуске: **правый клик → Открыть** (обход Gatekeeper).

`YTDLPGui.app` и папка `ytdlp-gui/` должны лежать рядом в одной директории.

## Конфигурация

Выбранная папка сохранения хранится в `~/.ytdlpgui_config.json`.
