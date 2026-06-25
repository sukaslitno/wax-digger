# Wax Digger — инструкция для агента

## Структура проекта

```
wax-digger/
├── ytdlp-gui/              # Flask-сервер + веб-UI (app.py, templates/)
├── ytdlp-extension/        # Браузерное расширение Chrome и Firefox
│   ├── build.js            # Сборщик — генерирует dist/
│   ├── manifest.json       # Manifest V3 (Chrome) + патч для Firefox
│   ├── background.js       # Service worker / background page
│   ├── content.js          # Инжектируемый скрипт (кнопка на страницах)
│   ├── popup.html          # UI попапа расширения
│   └── scripts/            # make-safari.sh и вспомогательные скрипты
├── packaging/
│   ├── build_macos.sh      # PyInstaller → WaxDigger.dmg
│   └── build_windows.ps1   # PyInstaller → WaxDigger.exe
├── .github/workflows/
│   └── release.yml         # CI/CD: триггер на тег v*, собирает все артефакты
└── README.md
```

## Ветковая модель

- **main** — только стабильные релизы с тегами `vX.X.X`. Пушить напрямую ЗАПРЕЩЕНО.
- **dev** — активная разработка. Все изменения попадают сюда сначала.
- **feature/\<название\>** — одна ветка на одну задачу. Создаётся от `dev`, мержится в `dev` через PR.

## Как вносить изменения (обязательный порядок)

1. Убедиться что ты на `dev` и он актуален:
   ```bash
   git checkout dev && git pull origin dev
   ```
2. Создать ветку для задачи:
   ```bash
   git checkout -b feature/<краткое-название-задачи>
   ```
3. Сделать изменения, коммитить с понятными сообщениями на английском.
4. Запушить ветку:
   ```bash
   git push origin feature/<название>
   ```
5. Сообщить пользователю — готово к PR в `dev`.

## Запрещено

- `git push origin main` — пушить в main напрямую
- `git push --force` — принудительный push
- `git branch -D` — принудительное удаление веток
- Коммитить в `main` напрямую, минуя PR
- Смешивать изменения разных компонентов в одной feature-ветке

## Компоненты и где что менять

| Компонент | Путь | Описание |
|---|---|---|
| GUI-сервер | `ytdlp-gui/app.py` | Flask backend, логика загрузки через yt-dlp |
| Веб-UI | `ytdlp-gui/templates/index.html` | Интерфейс Flask-приложения |
| Расширение | `ytdlp-extension/` | Chrome/Firefox extension — content script, popup, background |
| Сборка расширения | `ytdlp-extension/build.js` | Генерирует `dist/wax-digger-chrome.zip` и Firefox-пакет |
| macOS-билд | `packaging/build_macos.sh` | PyInstaller + создание DMG |
| Windows-билд | `packaging/build_windows.ps1` | PyInstaller → portable EXE |
| CI/CD | `.github/workflows/release.yml` | GitHub Actions: extension + DMG + EXE → GitHub Release |

## Платформы macOS / Windows

Разделения по веткам нет. Оба билда собираются одним `release.yml` при push тега.

Локальная сборка:
```bash
bash packaging/build_macos.sh       # macOS → dist/WaxDigger.dmg
pwsh packaging/build_windows.ps1    # Windows → dist/WaxDigger.exe
```

Расширение:
```bash
cd ytdlp-extension && node build.js  # → dist/wax-digger-chrome.zip + firefox-пакет
```

## Как делать релиз

1. Убедиться что `dev` стабилен (все тесты проходят, артефакты собираются локально).
2. Смержить `dev` в `main` через PR на GitHub — **не напрямую**.
3. После merge поставить тег и запушить его:
   ```bash
   git checkout main && git pull origin main
   git tag vX.X.X
   git push origin vX.X.X
   ```
4. GitHub Actions автоматически соберёт DMG, EXE, zip расширений и создаст GitHub Release.

## Безопасность

- Секреты хранятся **только** в GitHub Actions Secrets, не в коде репозитория.
- `AMO_JWT_ISSUER` / `AMO_JWT_SECRET` — для подписи Firefox-расширения через AMO API.
- `.env` файлы и `.wax-digger_config.json` перечислены в `.gitignore` — никогда не коммитить.
- **Остаточный риск:** actions в `release.yml` закреплены по version tag (`@v4`, `@v2`), не по commit SHA. При следующем рефакторинге CI рекомендуется закрепить по SHA для защиты от supply chain атак.
