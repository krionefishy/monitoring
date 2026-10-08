# Monitoring v2

Мониторинг HTTP-трафика приложения по JSON access-логам nginx: метрики, инциденты и доступность сервисов в веб-панели. Текущая версия API и интерфейса — `2.1.0`.

## Возможности

- Количество запросов, запросы в минуту, распределение HTTP-кодов и доля ошибок.
- Задержки p50, p75 и p95 для всего выбранного трафика и отдельных маршрутов. Значения рассчитываются по гистограмме в миллисекундах и являются приближёнными.
- Фильтры по сервису, группе путей, маршруту, HTTP-методу и периоду.
- Инциденты по ответам 4xx/5xx с примерами request ID и состояниями `open`, `acknowledged`, `resolved`. Новая ошибка после закрытого временного интервала снова открывает инцидент.
- HTTP health-проверки сервисов, история проверок и диагностика сборщика и публикации данных.
- Локальные учётные записи с ролями `admin` и `viewer`, cookie-сессии и CSRF-защита изменений.
- Топологии `direct` для одного приложения и `gateway` для нескольких сервисов за общим входом.

Один экземпляр мониторинга обслуживает одну пару `application` / `environment`.

## Архитектура

```text
nginx → JSON access log → collector → primary PostgreSQL
                             ↓                 ↓
                       SQLite spool          outbox
                                               ↓
                                            worker → read PostgreSQL
                                               ↓              ↓
                                       health-проверки    API + веб-панель
```

| Компонент Compose | Назначение |
| --- | --- |
| `primary` | Основная PostgreSQL: агрегаты, инциденты, пользователи, сессии, health и очередь публикации |
| `read-db` | Отдельная PostgreSQL с копией данных для аналитических запросов; обновляется приложением, а не репликацией PostgreSQL |
| `redis` | Кеш метрик и ограничение попыток входа |
| `init` | Одноразовое применение миграций v2 к обеим базам и привязка к экземпляру |
| `api` | FastAPI и собранный React-интерфейс |
| `collector` | Чтение nginx-логов, агрегация и доставка в основную базу |
| `worker` | Публикация в read-db, очистка по сроку хранения и health-проверки |

Сборщик обычно читает логи каждые 30 секунд; при отставании ускоряет обработку ограниченными порциями. Курсоры чтения и ожидающие отправки пакеты сохраняются в SQLite. Повторная доставка пакета не увеличивает счётчики повторно.

Health-проверки выполняются примерно каждые 30 секунд, тайм-аут одной проверки — 3 секунды. Первая неудача даёт `degraded`, повторная — `down`, успешная проверка — `healthy`. Отсутствующие или устаревшие проверки отображаются как `unknown`.

По умолчанию аналитические данные публикуются раз в **300 секунд** и хранятся **30 дней**. Панель показывает время опубликованного среза: появление новых запросов на графике не мгновенное.

## Развёртывание через Docker Compose

### 1. Подготовить окружение

Нужны Docker с плагином Compose, nginx перед наблюдаемым приложением и доступ к его логам с Docker-хоста. Для рабочей установки подготовьте отдельный HTTPS-домен мониторинга.

Все дальнейшие команды Compose выполняются из каталога:

```bash
cd monitor-service/deploy
```

Образ собирается из корня репозитория и включает frontend. Устанавливать Python и Node.js на сервер для этого не требуется. PostgreSQL и Redis не публикуют порты на хост; API доступен через `127.0.0.1:8088`.

### 2. Настроить приложение

Возьмите за основу [application.direct.json](monitor-service/deploy/application.direct.json) либо [application.gateway.json](monitor-service/deploy/application.gateway.json).

Основные поля JSON:

| Поле | Что указать |
| --- | --- |
| `application`, `environment` | Идентификаторы приложения и окружения; должны совпадать со значениями в nginx-логе |
| `topology` | `direct` или `gateway` |
| `base_path` | Общий префикс наблюдаемого API, например `/api/v1`; запросы вне него исключаются |
| `log_path`, `rotated_glob` | Пути к текущему и ротированным логам **внутри контейнера** |
| `exclude_paths` | Точные исходные пути без query string, которые не нужно учитывать, включая полный `base_path` |
| `route_rules` | Regex-правила нормализации маршрутов, например `/users/[0-9]+` → `/users/{id}` |
| `max_routes` | Лимит комбинаций сервис/группа/метод/маршрут; по умолчанию 2000 |
| `services` | Сервисы с уникальными `id`, названием `name`, префиксом `prefix` и необязательным `health_url` |

Сначала из пути удаляется `base_path`, затем выбирается сервис по самому длинному совпавшему `prefix` и применяется первое полностью совпавшее правило `route_rules`. Группа — первый сегмент пути после удаления `base_path`. Без правила маршрут сохраняется как путь; числовые ID автоматически не заменяются. При превышении `max_routes` новые маршруты объединяются в `/[cardinality-limit]`.

У health-проверки можно задать `expected_status` (по умолчанию `200`) и `health_body`, например `{"status": "ok"}`: указанные поля должны совпасть с JSON-ответом. Редиректы не выполняются. Без `health_url` сервис не проверяется.

В direct-примере health доступен по `http://host.docker.internal:8080/health`. Приложение должно слушать адрес, доступный из контейнера `worker`. Для сервисов в другой Docker-сети используйте вариант gateway ниже.

### 3. Подключить nginx-логи

Шаблоны находятся в [monitor-service/deploy/nginx](monitor-service/deploy/nginx):

1. Подключите `log-format.conf` внутри блока `http {}`. Замените значения `application` и `environment` на значения из JSON-конфига.
2. Добавьте `access_log` из `application-location.conf` в `server {}` наблюдаемого приложения, сохранив его текущую маршрутизацию.
3. Директивы `proxy_set_header X-Request-ID $request_id` и `add_header X-Request-ID $request_id always` добавьте в каждый проксируемый `location`: внутренние `proxy_set_header` переопределяют наследование.
4. Настройте ротацию по `logrotate.conf`: переименование файла и `nginx -s reopen`, без сжатия. Адаптируйте пользователя и группу под сервер. Стандартный `rotated_glob` читает суффиксы `.1`–`.9`; `.gz` сборщик не читает.
5. Проверьте конфигурацию через `sudo nginx -t` и примените её через `sudo nginx -s reload`.

`NGINX_LOG_DIRECTORY` монтируется в collector как `/var/log/nginx` только для чтения. Контейнер работает с UID/GID `10001:10001`; задайте `NGINX_LOG_GID` равным числовому GID группы, имеющей доступ к каталогу и файлам логов, в том числе после ротации.

Контракт лога использует `schema_version: "1"`. Обязательны `application`, `environment`, `msec`, `method`, `path`, `status`; `request_time` нужен для задержек, `request_id` — для примеров инцидентов. Готовый формат не записывает query string, IP, заголовки или тела запросов. Нормализуйте чувствительные сегменты путей через `route_rules`.

### 4. Создать `.env`

Сгенерируйте три независимых значения — для ключа экземпляра и двух паролей PostgreSQL:

```bash
openssl rand -hex 48
openssl rand -hex 24
openssl rand -hex 24
```

Создайте `monitor-service/deploy/.env`, подставив значения вместо `<...>`:

```dotenv
MONITOR_INSTANCE_ID=application-production
MONITOR_SECRET_KEY=<первое сгенерированное значение>
POSTGRES_PASSWORD=<второе сгенерированное значение>
POSTGRES_READ_PASSWORD=<третье сгенерированное значение>

MONITOR_CONFIG_FILE=./application.direct.json
MONITOR_PUBLIC_ORIGIN=https://monitor.example.com
MONITOR_COOKIE_SECURE=true
MONITOR_TRUSTED_PROXIES=<IP или CIDR nginx, видимый контейнеру API>
MONITOR_PORT=8088

NGINX_LOG_DIRECTORY=/var/log/nginx
NGINX_LOG_GID=<числовой GID группы чтения логов>
MONITOR_PUBLISH_SECONDS=300
MONITOR_RETENTION_DAYS=30
```

```bash
chmod 600 .env
```

`MONITOR_INSTANCE_ID` допускает латинские буквы, цифры, `_` и `-` и должен быть уникальным для установки. Сохраняйте его и `MONITOR_SECRET_KEY` вместе с резервной копией: базы привязаны к ID и отпечатку ключа, произвольная замена блокирует запуск. Пароли БД должны быть URL-safe, поскольку Compose подставляет их в URL подключения; hex из примера подходит.

`MONITOR_TRUSTED_PROXIES` передаётся Uvicorn как `FORWARDED_ALLOW_IPS`. Укажите адрес непосредственного nginx-прокси, видимый из контейнера, с учётом Docker-сети; не используйте `*`. Для nginx на хосте это может быть адрес шлюза Docker-сети.

Настройте отдельный HTTPS virtual host по [monitoring-location.conf](monitor-service/deploy/nginx/monitoring-location.conf), указав домен и сертификат в окружающем `server {}`. Если изменили `MONITOR_PORT`, измените и `proxy_pass`. Шаблон рассчитан на nginx на том же хосте. Логи самого мониторинга отключены, чтобы его запросы не попадали в аналитику приложения.

Для локальной проверки по HTTP задайте `MONITOR_COOKIE_SECURE=false`, `MONITOR_PUBLIC_ORIGIN=http://127.0.0.1:8088` и `MONITOR_TRUSTED_PROXIES=127.0.0.1,::1`, затем открывайте именно этот адрес. Для HTTPS оставьте secure cookies включёнными.

### 5. Запустить и создать пользователя

```bash
docker compose --env-file .env config --quiet
docker compose --env-file .env up --build -d
docker compose ps -a
docker compose logs --tail=100 init api collector worker
curl --fail http://127.0.0.1:8088/healthz
```

`init` должен завершиться с кодом `0`; это нормальное состояние одноразового контейнера. `/healthz` возвращает `{"status":"ok"}` и проверяет доступность API, но не весь поток сбора и публикации.

Пользователи автоматически не создаются. После старта API выполните:

```bash
docker compose exec api python -m app.v2.cli create-user admin --role admin
```

Введите пароль дважды; допустимая длина — 12–256 символов. Откройте `MONITOR_PUBLIC_ORIGIN` и войдите. `admin` может менять состояния инцидентов, `viewer` — просматривать данные.

Сделайте несколько запросов к наблюдаемому приложению и дождитесь сбора и очередной публикации. В панели проверьте счётчики, время среза и состояние сервисов. Пустая аналитика сразу после запуска при стандартном интервале публикации ожидаема.

### Gateway и отдельная Docker-сеть приложения

В `.env` выберите gateway-конфиг и существующую сеть приложения:

```dotenv
MONITOR_CONFIG_FILE=./application.gateway.json
MONITOR_APP_NETWORK=my-application-network
```

Замените демонстрационные имена `gateway`, `users`, `orders`, пути и health URL в JSON на реальные. Запускайте Compose с дополнительным файлом, который подключает `worker` к этой сети:

```bash
docker compose --env-file .env \
  -f docker-compose.yaml -f compose.application-network.yaml up --build -d
```

Используйте оба `-f` и для следующих команд Compose этой установки. Сам collector по-прежнему получает трафик из общего nginx-лога; health URL вызываются из `worker` напрямую.

## Эксплуатация

Команды ниже выполняются из `monitor-service/deploy`:

```bash
# Логи и состояние контейнеров
docker compose ps -a
docker compose logs -f --tail=100 api collector worker

# Управление пользователями
docker compose exec api python -m app.v2.cli create-user observer --role viewer
docker compose exec api python -m app.v2.cli set-password admin
docker compose exec api python -m app.v2.cli disable-user observer
docker compose exec api python -m app.v2.cli enable-user observer

# Обновление после получения новой версии исходников
docker compose --env-file .env up --build -d

# Восстановление аналитической базы из агрегатов primary
docker compose exec api python -m app.v2.cli rebuild-read

# Остановка с сохранением данных
docker compose down
```

Смена пароля, отключение и повторное включение пользователя удаляют его текущие сессии. Длительность сессии по умолчанию — 12 часов.

Данные находятся в volumes `primary_v2`, `read_v2` и `collector_v2` (Compose добавляет префикс проекта). Не используйте `down -v`, если хотите сохранить установку. Для восстановления нужны резервная копия основной PostgreSQL, `.env`, конфиг приложения и SQLite spool сборщика; read-db можно перестроить командой `rebuild-read`.

Перед изменением конфига collector дождитесь доставки ожидающих пакетов (`pending_bytes=0` в диагностике `/api/services-status`), затем перезапустите `api`, `collector` и `worker`. Если в spool остались пакеты со старым конфигом, сборщик откажется стартовать с новым: временно верните прежний конфиг и дайте очереди обработаться.

### Если данные не появляются

| Симптом | Что проверить |
| --- | --- |
| `missing_log` | Путь `NGINX_LOG_DIRECTORY` на хосте, `log_path` в контейнере и создание файла nginx |
| Ошибка доступа к логу | GID и права чтения файлов/прохода по каталогам, включая ротированные файлы |
| `invalid_lines` растёт | JSON-формат, `schema_version`, совпадение `application` / `environment`, корректность времени |
| `excluded_lines` растёт | `base_path` и `exclude_paths` |
| `backpressure` или растущая очередь | Доступность primary; сборщик приостанавливает чтение после заполнения лимита ожидающих пакетов (по умолчанию 512 МиБ) |
| Устаревший срез или heartbeat | Логи `worker` / `collector`, доступность обеих баз и интервал публикации |
| Health показывает `down` | Доступность `health_url` из `worker`, ожидаемый код и JSON-ответ |
| Вход не работает | Совпадение адреса браузера с `MONITOR_PUBLIC_ORIGIN`, secure cookies, Redis и доверенный nginx-прокси |

Redis нужен для входа: при его недоступности login возвращает `503`. Ограничение — до 10 попыток за 5 минут на имя пользователя и IP; при превышении возвращается `429`.

## API

Swagger/ReDoc и OpenAPI в текущем приложении отключены. Основные маршруты:

| Метод и путь | Назначение |
| --- | --- |
| `GET /healthz` | Liveness без авторизации |
| `POST /api/auth/login` | Вход с JSON `username` / `password`, установка cookie |
| `GET /api/auth/me` | Текущий пользователь, роль и CSRF-токен |
| `POST /api/auth/logout` | Выход |
| `GET /api/config` | Конфигурация интерфейса |
| `GET /api/metrics` | Метрики; параметры `hours`, `end` (Unix timestamp), `service`, `group`, `route`, `method` |
| `GET /api/incidents` | Инциденты; фильтры `service`, `group`, `route`, `method`, `state` и пагинация `offset` по 100 записей |
| `GET /api/incidents/{id}` | Детали инцидента |
| `PATCH /api/incidents/{id}` | Изменение состояния: JSON `state` и текущая `version`; только `admin` |
| `GET /api/services-status` | Health сервисов и диагностика фоновых процессов |

Все маршруты данных требуют сессию. Logout и изменение инцидента также требуют заголовок `X-CSRF-Token`. При конфликте версии инцидента API возвращает `409` — перечитайте состояние перед повтором.

## Разработка и проверки

Backend использует Python 3.13; frontend — Node.js 22, React 19, TypeScript и Vite. Команды из корня репозитория:

```bash
python3.13 -m venv venv
source venv/bin/activate
pip install -r monitor-service/requirements-dev.txt
ruff check --config monitor-service/pyproject.toml monitor-service/app/v2 monitor-service/tests
pytest monitor-service/tests -q

cd frontend
npm ci
npm run build
npm test
```

Без `MONITOR_TEST_DATABASES=1` тесты, требующие PostgreSQL, пропускаются. Интеграционные тесты **удаляют схему `public`**: используйте только отдельные временные базы с именем `monitoring_v2_test`. Переменные `TEST_DATABASE_URL`, `TEST_READ_DATABASE_URL` и `TEST_REDIS_URL` переопределяют подключения; стандартные локальные порты — `56432`, `56433` и `56479`. Пример инфраструктуры и запуска есть в [.github/workflows/checks.yml](.github/workflows/checks.yml).

Полная проверка поставляемого Compose из корня репозитория:

```bash
python3.13 monitor-service/tests/smoke_deploy.py
```

Smoke-тест требует Docker, собирает образ, создаёт отдельный Compose-проект на порту `18189`, проверяет вход, сбор логов, метрики, инциденты и health, затем удаляет свои контейнеры и volumes.

Для разработки UI `npm run dev` запускает Vite с проксированием `/api` на `127.0.0.1:8000`. Этот адрес должен обслуживаться backend; при использовании Compose задайте `MONITOR_PORT=8000`. Для входа через Vite установите `MONITOR_PUBLIC_ORIGIN` равным origin dev-сервера и отключите secure cookies для локального HTTP.

## Переход с v1

v2 получает HTTP-телеметрию из nginx. `POST /api/errors` возвращает **410 Gone**; старый Python SDK с v2 не работает. Telegram-алерты и база ошибок SQLite из v1 в новом runtime не используются. Автоматического импорта старой базы в v2 нет.

Каталог [sdk](sdk), старые модули backend и миграции v1 остаются в репозитории как legacy-код. Для v2 запускайте `main:app` и `python -m app.v2.cli init`; Compose делает это сам. Старую инструкцию с `MONITOR_TG_*`, `MONITOR_DB_PATH` и `alembic upgrade head` из корня monitor-service к v2 применять не нужно.

## Лицензия

[AGPL-3.0-or-later](LICENSE). [RELEASE.md](RELEASE.md) описывает правила версий и релизов legacy SDK.
