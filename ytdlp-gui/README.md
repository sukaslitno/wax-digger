# Wax Digger GUI

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

Дважды кликнуть `Wax Digger.app` (или перетащить в /Applications).
При первом запуске: **правый клик → Открыть** (обход Gatekeeper).

`Wax Digger.app` и папка `ytdlp-gui/` должны лежать рядом в одной директории.

## Конфигурация

Выбранная папка сохранения хранится в `~/.wax-digger_config.json`.

## Нативные сборки (DMG / EXE)

`app.py` кросс-платформенный (macOS / Windows / Linux): сам находит
bundled `yt-dlp`/`ffmpeg`, открывает браузер при старте и использует нативный
выбор папки для каждой ОС. Готовые self-contained сборки собираются из корня
репозитория:

```bash
bash packaging/build_macos.sh      # → dist/WaxDigger.dmg
pwsh packaging/build_windows.ps1   # → dist/WaxDigger.exe
```

Сборки бандлят `yt-dlp` и `ffmpeg` внутрь — пользователю ничего ставить не надо.
Автоматически всё это собирает GitHub Actions по тегу `v*` (см. корневой README).
