# Rusprofile Monitor — Telegram-бот мониторинга лавок

Приватный бот проверяет карточки компаний на Rusprofile (~200 лавок), 2 раза в день (настраивается), и шлёт алерты в Telegram.

## Что проверяется

По аналогии с вашими скринами — три зоны карточки:

| Сигнал | Действие бота |
|--------|----------------|
| **Недостоверность адреса** | Уведомление + тикет «В работе». После `/heal` или авто-снятия флага — «адрес восстановлен» |
| **Недостоверность ДЛ / руководителя** | Уведомление + тикет |
| **Недостоверность учредителя** | Уведомление (отдельный текст про согласование с бухами) |
| **Ликвидация / исключение** | Уведомление + тикет |

## Быстрый старт (Docker)

```bash
cp .env.example .env
# заполнить BOT_TOKEN и ADMIN_IDS

docker compose up -d --build
docker compose logs -f
```

## Локально без Docker

```bash
python -m venv .venv
.venv\Scripts\activate   # Windows
pip install -r requirements.txt
cp .env.example .env
python -m app.main
```

## Настройка `.env`

| Переменная | Описание |
|------------|----------|
| `BOT_TOKEN` | Токен от @BotFather |
| `ADMIN_IDS` | Telegram user id админа (через запятую) |
| `CHECK_CRON` | Cron, по умолчанию `0 10,18 * * *` — 10:00 и 18:00 |
| `REQUEST_DELAY_SEC` | Пауза между запросами к Rusprofile (3 сек) |
| `TIMEZONE` | `Europe/Moscow` |

## Команды бота

**Все пользователи:** `/start`, `/status`, `/tickets`

**Админ:**
- `/add_company ОГРН` — добавить лавку в мониторинг
- `/add_companies` + список ОГРН — пакетная загрузка
- `/import_file` + `.txt` — импорт ОГРН
- `/resolve_inn ИНН` — **только** ИНН→ОГРН (в мониторинг не кладёт)
- `/resolve_inns` + список ИНН — пакетный резолв
- `/remove_company ОГРН` — убрать из мониторинга (уведомление всем)
- `/list_companies` — список
- `/add_user ID` / `/remove_user ID` / `/list_users` — ACL
- `/heal TICKET_ID` — закрыть тикет как «Вылечена»
- `/check_now` — полная проверка сейчас
- `/check ОГРН` — одна компания

Первый `/start` от id из `ADMIN_IDS` автоматически создаёт админа в БД.

## Если есть список ИНН, а не ОГРН

Мониторинг работает **только по ОГРН** (карточка Rusprofile = `/id/{ОГРН}`).

Отдельный шаг резолва:

```bash
# на сервере / локально
python scripts/resolve_inn.py --file inns.txt --out ogrns.txt --csv mapping.csv
# потом в боте:
# /import_file  + ogrns.txt
```

Или в Telegram: `/resolve_inns` → скопировать ОГРН → `/add_companies`.

## Деплой на сервер

```bash
git clone <repo> rusprofile-monitor && cd rusprofile-monitor
cp .env.example .env && nano .env
docker compose up -d --build
```

Данные SQLite лежат в `./data/bot.db` (volume в compose).

## Тесты парсера

```bash
pip install pytest
pytest tests/ -q
```

Для отладки на живых страницах:

```bash
python scripts/fetch_sample.py 1237700215290 1227700761001
pytest tests/ -q
```

## Ограничения

- Rusprofile может менять вёрстку — парсер эвристический, при сбоях смотрите `last_error` у компании.
- Не ставьте `REQUEST_DELAY_SEC` ниже 2–3 сек на ~200 компаний — иначе риск блокировки.
- Для продакшена с высокой нагрузкой лучше вынести очередь проверок в отдельный worker (сейчас последовательный обход).

## Структура

```
app/
  bot/          — Telegram handlers
  db/           — SQLAlchemy models
  parser/       — парсер Rusprofile
  services/     — мониторинг, HTTP-клиент
  scheduler.py  — APScheduler
  main.py       — entrypoint
```
