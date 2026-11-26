# EntryX Platform

Бот процесса поставки дверей ENTRYX (карта v4): сделка в CRM, агент на каждую роль, бот сам передаёт карточку следующему, когда задачи этапа закрыты.

Всё в одном репозитории, ставится одной командой:

| Часть | Папка | Адрес после установки |
|---|---|---|
| Чат Entryx (веб, кнопки, уведомления) | `bridge/static` | http://localhost:8787/app |
| Мост: процесс, задачи, переходы этапов | `bridge/` | http://localhost:8787 |
| Twenty CRM v2.57.1 | `crm/` | http://localhost:3000 |
| Dify 1.20.0 (агенты ролей) | `dify/` | http://localhost |
| Процесс: этапы, роли, шаги, варианты ответа | `process/stages.yaml` | — |
| 7 агентов в формате Dify DSL | `dify-apps/*.yml` | — |

```
Twenty CRM (сделка, этап, задачи) ⇄ мост (движок процесса) ⇄ Dify (агент роли) ⇄ чат Entryx
```

## Что нужно

- **Docker** — Docker Desktop на Windows / macOS или Docker Engine с Compose v2.35+ на Linux. Свободно ~10 ГБ, памяти 8 ГБ и больше.
- **Python 3.10+** — только для скриптов настройки.
- **Git**.
- Свободные порты 80, 3000, 8787.

## Установка

```powershell
git clone https://github.com/RakhatLukum646/EntryXPlatfrom.git
cd EntryXPlatfrom
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

Linux / macOS:

```bash
git clone https://github.com/RakhatLukum646/EntryXPlatfrom.git
cd EntryXPlatfrom
./install.sh
```

Первый запуск скачивает образы Docker: 5–15 минут. Повторный запуск безопасен — готовые шаги пропускаются, пароли и ключи не перезаписываются.

### Что делает скрипт

1. Создаёт `.env` для Dify, Twenty и моста со случайными паролями и ключами, пароль администратора Dify — в `.secrets/local-accounts.env`.
2. Ставит Python-зависимости в `.venv`.
3. Поднимает Dify и Twenty в Docker.
4. Dify: создаёт администратора, ставит плагин Gemini, импортирует и публикует 7 агентов, пишет их ключи в `bridge/.env`.
5. Twenty: **один раз вручную** — регистрация и API-ключ (скрипт откроет браузер и попросит вставить ключ). Потом сам настраивает воронку из 13 этапов, флаги и поля сделки.
6. Собирает и запускает мост.

### После установки — один шаг руками

Ключ модели: Dify → аватар → **Настройки → Поставщик моделей → Gemini** → ключ Google AI Studio.
Без ключа задачи, кнопки и переходы работают, а ответы на свободные вопросы в чате — нет.

### Проверка

В Twenty создайте сделку на этапе «Договор с заказчиком». Через несколько секунд откройте http://localhost:8787/app, роль «Данил»: в «Что на мне сегодня» появится задача с кнопками. Ответ кнопкой закрывает задачу, бот передаёт карточку следующему.

### Linux-сервер

Чтобы Dify видел мост, на Linux нужен порт на адресе Docker-сети. `install.sh` делает это сам (`tools/linux_bridge_port.sh`). Если кнопки в чате отвечают «Мост Entryx недоступен», на уже установленном сервере выполните из корня репозитория:

```bash
bash tools/linux_bridge_port.sh && (cd bridge && docker compose up -d --build)
cd dify && docker compose exec api curl -sS -m 5 http://host.docker.internal:8787/health   # должно быть {"ok":true}
```

Если проверка не проходит, посмотрите файрвол (`ufw`). Не открывайте мост на `0.0.0.0`: у него нет входа по паролю.

## Где пароли и ключи

В git не попадает ничего секретного: все `.env` и `.secrets/` в `.gitignore`, в репозитории только `*.env.example`.

| Файл | Что внутри |
|---|---|
| `.secrets/local-accounts.env` | Вход в Dify (почта и пароль администратора). Туда же можно записать вход в Twenty. |
| `bridge/.env` | API-ключ Twenty и ключи 7 агентов Dify. |
| `dify/.env`, `crm/.env` | Служебные ключи и пароли баз. |

## Повседневное

| Задача | Команда |
|---|---|
| Остановить всё | `docker compose down` в папках `bridge`, `crm`, `dify` |
| Запустить снова | `docker compose up -d` в папках `dify`, `crm`, `bridge` (или ещё раз `install.ps1`) |
| Поправили `process/stages.yaml` | `docker compose restart` в `bridge` |
| Поменялись шаги или правила агентов | `.venv\Scripts\python tools\gen_dify_apps.py`, затем `.venv\Scripts\python tools\dify_setup.py --overwrite`, затем `docker compose up -d --force-recreate` в `bridge` |
| Поправили страницы чата | ничего: `bridge/static` подключён к контейнеру напрямую, обновите страницу |
| Журнал моста | `docker compose logs -f` в `bridge` |

Данные (сделки, задачи, разговоры) живут в Docker-томах и папках `dify/volumes`, `bridge/data` и переживают перезапуск.

## Подробнее

- [Как устроен процесс](docs/process.md): этапы, волны задач, варианты ответа, проблемные ветки, уведомления, веб-чат.
- `dify/` — docker-часть Dify 1.20.0 без изменений (лицензия Dify — `dify/LICENSE`) плюс `docker-compose.override.yaml` от Entryx: агенты ходят к мосту на `host.docker.internal`.
