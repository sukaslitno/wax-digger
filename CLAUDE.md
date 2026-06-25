# Wax Digger — инструкция для агента

## Структура проекта

```
wax-digger/
├── ytdlp-gui/              # Flask-сервер + веб-UI (app.py, templates/)
├── ytdlp-extension/        # Браузерное расширение Chrome и Firefox
│   ├── build.js            # Сборщик — генерирует dist/
│   ├── manifest.json       # Manifest V3 (Chrome) + патч для Firefox
│   ├── background.js       # Service worker
│   ├── content.js          # Инжектируемый скрипт (кнопка на страницах)
│   ├── popup.html          # UI попапа расширения
│   └── scripts/            # Вспомогательные скрипты
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
3. Сделать изменения, коммитить с понятными сообщениями.
4. Запушить ветку:
   ```bash
   git push origin feature/<название>
   ```
5. Сообщить пользователю — готово к PR в `dev`.

## Формат коммит-сообщений

```
feat: короткое описание       # новая функциональность
fix: короткое описание        # исправление бага
chore: короткое описание      # техническая работа, зависимости
docs: короткое описание       # документация
refactor: короткое описание   # рефакторинг без изменения поведения
```

Примеры:
```
feat: add download progress bar to web UI
fix: correct SSE stream closing on error
chore: update yt-dlp to 2025.06.01
```

## Запрещено

- `git push origin main` — пушить в `main` напрямую
- `git push --force` — принудительный push
- `git branch -D` — принудительное удаление веток
- Коммитить в `main` напрямую, минуя PR
- Смешивать изменения разных компонентов в одной feature-ветке
- Добавлять в репозиторий любые файлы агента, AI-инструментов или сессий

## Компоненты и где что менять

| Компонент | Путь | Описание |
|---|---|---|
| GUI-сервер | `ytdlp-gui/app.py` | Flask backend, логика загрузки через yt-dlp |
| Веб-UI | `ytdlp-gui/templates/index.html` | Интерфейс Flask-приложения |
| Расширение | `ytdlp-extension/` | Chrome/Firefox extension |
| Сборка ext | `ytdlp-extension/build.js` | Генерирует `dist/` zip-пакеты |
| macOS-билд | `packaging/build_macos.sh` | PyInstaller + DMG |
| Windows-билд | `packaging/build_windows.ps1` | PyInstaller → portable EXE |
| CI/CD | `.github/workflows/release.yml` | GitHub Actions → GitHub Release |

## Платформы macOS / Windows

Разделения по веткам нет. Оба билда собираются одним `release.yml` при push тега.

Локальная сборка:
```bash
bash packaging/build_macos.sh       # macOS → dist/WaxDigger.dmg
pwsh packaging/build_windows.ps1    # Windows → dist/WaxDigger.exe
cd ytdlp-extension && node build.js # → dist/ zip-пакеты
```

## Как делать релиз

1. Убедиться что `dev` стабилен — проверить локальную сборку на нужной платформе.
2. Смержить `dev` в `main` через PR на GitHub — не напрямую.
3. После merge поставить тег:
   ```bash
   git checkout main && git pull origin main
   git tag vX.X.X
   git push origin vX.X.X
   ```
4. GitHub Actions автоматически соберёт DMG, EXE, zip расширений и создаст Release.

## Безопасность

- Секреты хранятся только в GitHub Actions Secrets, не в коде.
- `AMO_JWT_ISSUER` / `AMO_JWT_SECRET` — для подписи Firefox через AMO API.
- `.env` и `.wax-digger_config.json` в `.gitignore` — никогда не коммитить.
- **Остаточный риск:** actions в `release.yml` закреплены по version tag (`@v4`, `@v2`), не по SHA. При следующем рефакторинге CI — закрепить по SHA.
