---
name: hermes-mods-cli
description: Справочник оператора по CLI движка модов Hermes runner.py: синтаксис и флаги apply, verify, test, update, doctor, diff, info, commands, create, unpatch, purge, config, snapshot, watchdog, --lang и --json.
category: devops
---

# Hermes Mods CLI Reference

Используй этот скилл как полный справочник по CLI-интерфейсу движка модов Hermes (`~/.hermes/mods/runner.py`).

## Архитектурные принципы CLI и движка

- **Роль `runner.py` и `.engine/`:**
  `runner.py` — это CLI-фасад микро-ядра движка (`.engine/`). В самом коде движка **нет прямых правок или модификаций функционала Hermes Agent** (`/usr/local/lib/hermes-agent`). Движок служит исключительно инфраструктурным загрузчиком, валидатором, раннером и супервизором.
- **Cache-Driven архитектура:**
  Ядро Hermes общается с модами ТОЛЬКО через JSON-кэши (`.commands_cache.json`, `.locales_cache.json`, `.mcp_cache.json`). В коде ядра категорически запрещены `import engine`, `from engine...` и манипуляции с `sys.path.insert`.
- **Строгий запрет рестартов шлюза (Zero Downtime Policy):**
  **КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО** выполнять `systemctl restart hermes-gateway`, `systemctl reload`, `hermes gateway restart` и любые другие перезапуски шлюза или системных процессов! Любые обновления применяются, верифицируются и тестируются на лету через `runner.py apply / verify / test`. Ядро считывает плоские JSON-кэши по `mtime` без простоя и без потери активных сессий.
- **Обязательная мультиязычность i18n на 8 языках:**
  При любом изменении текстовых литералов, сообщений команд или меток агент **ОБЯЗАН** обновлять переводы на **ВСЕХ 8 официальных языках** (`en`, `ru`, `es`, `fr`, `de`, `zh`, `ar`, `pt`). Полный запрет захардкоженных русских или английских строк в Python-коде — все тексты извлекаются через `t("key", lang=...)`.
- **Сквозная синхронизация интерфейсов интроспекции:**
  При изменении формул токенов, сжатий или процента кэша (`cache_pct`) агент обязан синхронизировать логику расчета во ВСЕХ командах интроспекции (`/context`, `/usage`, `/status`, `/insights`, CLI, TUI).
- **Неблокируемость команд интроспекции (Zero-Blocking SLA):**
  Команды статуса, использования, контекста и инсайтов никогда не должны падать или блокироваться сообщением «агент занят/работает, подождите завершения». Они обязаны мгновенно отдавать актуальные данные из RAM или `state.db` (WAL read-only).
- **Форматирование вывода и запрет псевдобуллетов (`•`):**
  В структурированных блоках метрик команд категорически запрещено использовать псевдобуллеты (`•`). Вывод должен быть компактным, строгим и единообразным.
- **Развитие движка:** Функционал самого движка (CLI, парсеры, кэширование, раннеры тестов, экспортеры, мониторы, снапшоты, circuit breaker) внутри `.engine/` можно и нужно развивать и расширять.
- **Изоляция изменений Hermes:** Все изменения функционала ядра Hermes выполняются исключительно через применение системных модов (Tier 0, `system/`) и кастомных модов (`custom/`).
- **Топологический порядок графа (DAG):**
  Системные моды (Tier 0) гарантированно применяются первыми на уровне архитектуры (`_mod_tier_key`), затем применяются кастомные моды (Tier 1). Системные моды могут зависеть от других системных модов (`dependencies: [mod-command-registry]`), а кастомные моды изолированы и не должны зависеть от системных в графе.

## Базовый синтаксис

```bash
python3 ~/.hermes/mods/runner.py <действие> [цель] [флаги]
```

- Любая команда принимает как точный `mod_id` (например, `command-close`), так и понятное имя из манифеста (`"Command Close"`).
- Все пути и окружение автоматически определяются движком динамически без хардкода `/root/`.

---

## Каталог команд от А до Я

### 1. `apply` — Транзакционное наложение модов
Применяет декларативные патчи к кодовой базе Hermes Agent (`/usr/local/lib/hermes-agent`).
```bash
# Применить все активные моды в топологическом порядке
python3 ~/.hermes/mods/runner.py apply

# Применить конкретный мод (с авторазрешением зависимостей)
python3 ~/.hermes/mods/runner.py apply <mod_id>

# Принудительное наложение без использования кэша
python3 ~/.hermes/mods/runner.py apply <mod_id> --force

# Наложение с автоматическим запуском watchdog-контроля шлюза
python3 ~/.hermes/mods/runner.py apply --watchdog [--watchdog-window 30]
```
- **Очередность DAG:** Сначала атомарно накладываются базовые системные моды (Tier 0 из `system/`), затем кастомные моды из `custom/`.
- Системные моды с зависимостями (например, `mod-manager` с `dependencies: [mod-command-registry]`) накладываются в строгом топологическом порядке.
- Перед наложением автоматически создается дельта-снапшот ядра (`trigger: pre_apply`).
- Атомарно создает резервную копию изменяемых файлов ядра.
- При ошибке в любом таргете автоматически откатывает транзакцию.
- Повторный вызов безопасен и сообщает `already_applied`.
- **Zero Downtime:** Наложение выполняется на лету без вызова `systemctl restart/reload` (`systemctl restart hermes-gateway` строго запрещен). Ядро моментально считывает обновленные кэши по `mtime`.

---

### 2. `verify` — Инкрементальная верификация целостности
Проверяет статус применения модов без изменения файлов на диске.
```bash
# Проверить все моды
python3 ~/.hermes/mods/runner.py verify

# Проверить конкретный мод
python3 ~/.hermes/mods/runner.py verify <mod_id>

# Проверка в обход кэша
python3 ~/.hermes/mods/runner.py verify <mod_id> --no-cache
```
- Использует хэши содержимого файлов и возвращает результат мгновенно (`cached: true`).
- Возможные статусы: `already_applied` (патчи наложены корректно), `not_applied` (код чистый), `diverged` (расхождение кода с патчем).

---

### 3. `test` — Запуск тестов модов
Запускает тесты модов (как словарные `tests:` из однофайловых модов, так и pytest из `tests/test_*.py`).
```bash
# Запустить тесты всех модов
python3 ~/.hermes/mods/runner.py test

# Запустить тесты конкретного мода
python3 ~/.hermes/mods/runner.py test <mod_id>

# Принудительный холодный перезапуск тестов
python3 ~/.hermes/mods/runner.py test <mod_id> --no-cache

# Остановка на первой ошибке (fail-fast)
python3 ~/.hermes/mods/runner.py test -x

# Фильтрация тестов по паттерну имени
python3 ~/.hermes/mods/runner.py test -k "token or context"
```
- Поддерживает параллелизацию через `ThreadPoolExecutor`.
- Кэширует успешные результаты прогонов по хэшам зависимостей и кода в `.test_cache.json`.

---

### 4. `doctor` — Комплексная диагностика экосистемы
Проводит всесторонний аудит здоровья окружения, интерпретатора и файловых дескрипторов.
```bash
python3 ~/.hermes/mods/runner.py doctor [--no-color|--color]
```
Проверяемые инварианты:
- **Python Environment:** Версия интерпретатора и путь к venv Hermes (`/usr/local/lib/hermes-agent/venv`).
- **Git CLI:** Наличие и корректность репозитория модов.
- **Hermes Core:** Доступность и права на запись в ядро Hermes Agent.
- **Secrets Storage:** Права доступа на `~/.hermes/secrets.yaml` (строго `0600`).
- **Engine Integrity:** Валидность всех модулей `.engine/` (загрузчики, валидаторы, раннеры).
- **Mod Manifests & Dependencies:** Отсутствие циклических зависимостей в графе (DAG clean), корректность связей между системными модами (`system/`), изоляция кастомных модов (`custom/`) от избыточных системных зависимостей.
- **Commands Cache & Locks:** Отсутствие зависших процессов и блокировок `.lock`.

---

### 5. `diff` — Просмотр изменений ядра
Отображает цветной унифицированный diff между чистым кодом Hermes и модифицированным состоянием.
```bash
# Дифф по всем примененным модам
python3 ~/.hermes/mods/runner.py diff

# Дифф по конкретному моду
python3 ~/.hermes/mods/runner.py diff <mod_id>
```

---

### 6. `info` — Каталог и карточки модов
```bash
# Компактный каталог пользовательских модов (служебные system/ скрыты)
python3 ~/.hermes/mods/runner.py info

# Детальная карточка мода
python3 ~/.hermes/mods/runner.py info <mod_id>

# Карточка мода на любом из языков BCP 47 / официальных 8 языков
python3 ~/.hermes/mods/runner.py info <mod_id> --lang ru
python3 ~/.hermes/mods/runner.py info <mod_id> --lang es
python3 ~/.hermes/mods/runner.py info <mod_id> --lang de

# Машиночитаемый вывод карточки в JSON
python3 ~/.hermes/mods/runner.py info <mod_id> --format json
```
- Поддерживает 8 языков международного стандарта (`en`, `es`, `fr`, `zh`, `ar`, `ru`, `de`, `pt`) и любые другие коды BCP 47 / User Display Language.
- Принцип English-by-default: при отсутствии перевода в `i18n/` всегда отображается базовый английский текст из манифеста.
- Карточка содержит: версию, автора, описание, статус, список таргетов, параметры конфигурации, слэш-команды, мониторы и скиллы.

---

### 7. `commands` — Сводная таблица слэш-команд
Выводит интерактивную сводку всех слэш-команд, зарегистрированных активными модами, и актуализирует кэши `.commands_cache.json`, `.locales_cache.json` и `.mcp_cache.json`:
```bash
python3 ~/.hermes/mods/runner.py commands [--lang ru] [--json]
```
- Флаг `--lang` поддерживает любой язык BCP 47 / 8 официальных языков (`en`, `ru`, `es`, `fr`, `de`, `zh`, `ar`, `pt`, `auto`).
- **Обязательная мультиязычность:** Все команды модов обязаны иметь описания на всей восьмерке официальных языков без пропусков и строкового хардкода в коде.
- **Чистота вывода:** В структурированных блоках метрик команд категорически запрещено использовать псевдобуллеты (`•`).
- **Неблокируемость:** Интроспекционные команды работают асинхронно, считывая данные из RAM/`state.db` без блокировки очереди ходов агента.
- Колонки таблицы:
  - Команда (`/{name}`).
  - Мод-источник.
  - Категория.
  - Режим (`replace` или `extend`).
  - Аргументы (`args_hint`).
  - Описание (с учетом языка `--lang`, колоцированной локализации и оверлеев системных строк).

---

### 8. `create` — Скаффолдинг нового мода
Генерирует шаблон мода в папке `custom/` (по умолчанию с английскими манифестами):
```bash
# Создание компактного однофайлового мода
python3 ~/.hermes/mods/runner.py create <mod_id> --type single

# Создание папочного мода со строгой структурой (Strict Layout)
python3 ~/.hermes/mods/runner.py create <mod_id> --type folder
```

---

### 9. `unpatch` (алиас: `rollback`) — Мягкий откат
Снимает патчи кодовой базы Hermes.
```bash
# Мягкий откат: снимает ТОЛЬКО пользовательские моды из custom/
python3 ~/.hermes/mods/runner.py unpatch

# Откат одного конкретного мода
python3 ~/.hermes/mods/runner.py unpatch <mod_id>

# Откат ВСЕХ модов, включая системные компоненты ядра
python3 ~/.hermes/mods/runner.py unpatch --all
```
*По умолчанию системные моды ядра (`system/`) остаются активными*, что сохраняет работоспособность самого движка и шлюза.

---

### 10. `purge` (алиас: `uninstall-all`) — Полная зачистка под ваниль
Полный снос всех модов и возврат ядра в 100% чистый upstream:
```bash
# Требует обязательного флага подтверждения
python3 ~/.hermes/mods/runner.py purge --confirm
# или
python3 ~/.hermes/mods/runner.py purge --yes
```
- Снимает абсолютно все моды (`custom/` и `system/`).
- Очищает все файлы кэшей (`.commands_cache.json`, `.locales_cache.json`, `test_cache.json`, кэши скиллов и MCP).
- Ядро `/usr/local/lib/hermes-agent` возвращается к оригинальному состоянию.

---

### 11. `config` — Управление параметрами и секретами
```bash
# Просмотр всех параметров мода (секреты маскируются ||********||)
python3 ~/.hermes/mods/runner.py config <mod_id>

# Установка публичного параметра (пишется в config.yaml)
python3 ~/.hermes/mods/runner.py config <mod_id> --set <key>=<value>

# Установка чувствительного секрета (пишется строго в secrets.yaml с правами 0600)
python3 ~/.hermes/mods/runner.py config <mod_id> --set-secret <key>=<value>

# Заполнение значений по умолчанию в config.yaml и secrets.yaml
python3 ~/.hermes/mods/runner.py config <mod_id> --populate
```

---

### 12. `enable` и `disable` — Переключение активности модов
```bash
# Включить мод (enabled: true в манифесте)
python3 ~/.hermes/mods/runner.py enable <mod_id>

# Выключить мод (enabled: false в манифесте)
python3 ~/.hermes/mods/runner.py disable <mod_id>

# Отключение сбойного мода через Circuit Breaker (с немедленным откатом патчей из ядра)
python3 ~/.hermes/mods/runner.py disable <mod_id> --circuit-breaker [--reason "Причина"]
```

---

### 13. `circuit-breaker` — Аварийный размыкатель и изоляция сбоев
Управляет изоляцией сбойных модов, историей падений и сбросом состояния автоматической защиты:
```bash
# Просмотр статуса Circuit Breaker всех модов (CLOSED/OPEN, число сбоев, время трипа)
python3 ~/.hermes/mods/runner.py circuit-breaker status

# Принудительная изоляция (trip) сбойного мода с откатом его патчей
python3 ~/.hermes/mods/runner.py circuit-breaker trip <mod_id> [--reason "Причина сбоя"]

# Сброс состояния Circuit Breaker (перевод в CLOSED и повторное включение)
python3 ~/.hermes/mods/runner.py circuit-breaker reset <mod_id>

# Просмотр истории инцидентов и ошибок мода
python3 ~/.hermes/mods/runner.py circuit-breaker failures <mod_id>
```

---

### 14. `monitors` — Управление фоновыми процессами
```bash
python3 ~/.hermes/mods/runner.py monitors list
python3 ~/.hermes/mods/runner.py monitors start <mod_id> [monitor_id]
python3 ~/.hermes/mods/runner.py monitors stop <mod_id> [monitor_id]
python3 ~/.hermes/mods/runner.py monitors status
```

---

### 15. `mcp` — Супервизор декларативных MCP-серверов
```bash
# Список серверов, объявленных в модах
python3 ~/.hermes/mods/runner.py mcp list

# Принудительная синхронизация серверов в рантайм и актуализация .mcp_cache.json
python3 ~/.hermes/mods/runner.py mcp sync
```

---

### 16. `validate` — Статическая валидация спецификации
```bash
# Базовая валидация структуры, манифестов и графа зависимостей
python3 ~/.hermes/mods/runner.py validate

# Строгая валидация раскладки файлов (Strict Layout и декомпозиция targets)
python3 ~/.hermes/mods/runner.py validate --strict-layout
```

---

### 17. `export` (алиас: `pack`) — Дистрибуция и экспорт модов
Упаковывает чистый, переносимый архив модов без утечки локальных кэшей, секретов или логов:
```bash
# Экспорт конкретного мода
python3 ~/.hermes/mods/runner.py export <mod_id> [--out archive.tar.gz]

# Алиас pack
python3 ~/.hermes/mods/runner.py pack <mod_id> --out my-mod.tar.gz

# Экспорт базовых системных модов (Tier 0)
python3 ~/.hermes/mods/runner.py export --out hermes-mods.tar.gz

# Экспорт всех модов (system + custom)
python3 ~/.hermes/mods/runner.py export --include-custom --out all-mods.tar.gz

# Строгий аудит на отсутствие любых секретов
python3 ~/.hermes/mods/runner.py export <mod_id> --no-secrets
```
- Автоматически исключает: `__pycache__`, `.test_cache.json`, `.commands_cache.json`, `.locales_cache.json`, `.mcp_cache.json`, `.git`, временные файлы, `secrets.yaml`, `*.db`, `*.log`.
- Проверяет валидность манифестов перед сборкой архива (при ошибках сборка отменяется).
- Включает файл метаданных `mod_export.json` в корень архива.

---

### 18. `import` — Быстрая распаковка и валидация модов
Безопасно извлекает моды из архива на новой машине и проводит автоматическую валидацию манифестов:
```bash
# Импорт архива
python3 ~/.hermes/mods/runner.py import <archive.tar.gz>

# Принудительный импорт с перезаписью существующих файлов
python3 ~/.hermes/mods/runner.py import <archive.tar.gz> --force

# С выводом JSON
python3 ~/.hermes/mods/runner.py import <archive.tar.gz> --json
```
- Защита от TarBomb и Path Traversal.
- Фильтрует нежелательные файлы, если они случайно присутствовали в стороннем архиве.
- Автоматически валидирует импортированные моды через `ModValidator`.

---

### 19. `snapshot` — Дельта-снапшотирование ядра и откат
Сохраняет точный дельта-снапшот затрагиваемых файлов ядра Hermes (`/usr/local/lib/hermes-agent`) перед наложением модов или вручную, а также обеспечивает мгновенный откат:
```bash
# Список всех существующих снапшотов
python3 ~/.hermes/mods/runner.py snapshot list

# Ручное создание дельта-снапшота (по умолчанию сохраняет все цели активных модов)
python3 ~/.hermes/mods/runner.py snapshot create [--desc "Описание"]

# Создание снапшота для конкретного мода или списка файлов
python3 ~/.hermes/mods/runner.py snapshot create <mod_id>
python3 ~/.hermes/mods/runner.py snapshot create --files file1.py file2.py

# Откат ядра к последнему валидному снапшоту (latest)
python3 ~/.hermes/mods/runner.py snapshot restore

# Откат ядра к конкретному снапшоту по ID
python3 ~/.hermes/mods/runner.py snapshot restore <snapshot_id>
```
- Перед любым `runner.py apply` автоматически создается предпусковой дельта-снапшот (`trigger: pre_apply`).
- Снапшот сохраняет хэши SHA-256, размеры и флаг `exists_before`.
- При откате удаляются файлы, которых не существовало до наложения мода, а исходные файлы восстанавливаются с проверкой целостности.

---

### 20. `watchdog` — Сторожевой таймер и автооткат шлюза
Проверяет стабильность запуска шлюза Hermes Agent (`hermes-gateway.service`) в течение первых 30 секунд:
```bash
# Ручной запуск мониторинга шлюза (окно по умолчанию 30с)
python3 ~/.hermes/mods/runner.py watchdog [--window 30] [--service hermes-gateway]

# Или через алиас подкоманды
python3 ~/.hermes/mods/runner.py snapshot watchdog

# Наложение модов с автоматическим запуском watchdog после apply
python3 ~/.hermes/mods/runner.py apply --watchdog [--watchdog-window 30]
```
- Если шлюз падает, завершается аварийно или входит в цикл перезапусков (смена PID) в первые 30 секунд, автоматически срабатывает откат к последнему снапшоту ядра.

---

### 21. `list` — Каталог модов и фильтрация по статусу
Выводит структурированный список модов с названиями, версиями и описаниями:
```bash
# Человекочитаемый список всех модов
python3 ~/.hermes/mods/runner.py list

# Фильтрация по статусу наложения
python3 ~/.hermes/mods/runner.py list --status applied
python3 ~/.hermes/mods/runner.py list --status disabled
python3 ~/.hermes/mods/runner.py list --status all

# Вывод в чистом формате JSON
python3 ~/.hermes/mods/runner.py list --format json
```

---

### 22. `status` — Сводка активности и здоровья экосистемы модов
Выводит сводный дашборд активности и здоровья рантайма модов:
```bash
# Текстовый дашборд со сводкой здоровья и активности
python3 ~/.hermes/mods/runner.py status

# В машинном формате JSON
python3 ~/.hermes/mods/runner.py status --format json
```
- Отображает: общий статус здоровья (HEALTHY/DEGRADED/DISABLED), состояние мастер-рубильника `mods.enabled`, счетчики примененных/отключенных модов, активность команд, мониторов, MCP и скиллов, проверку DAG и прав доступа к секретам (0600).
- **Сквозная синхронизация и чистота вывода:** Метрики статуса строго согласованы с данными слэш-команд (`/status`, `/usage`, `/context`, `/insights`), структурированные блоки форматируются без псевдобуллетов (`•`).
- **Неблокируемость:** Работает независимо от текущей нагрузки агента, опрашивая локальное состояние без задержек.

---

### 23. `logs` — Просмотр логов конкретного мода
Собирает и отображает последние записи логов и ошибок, относящихся к моду (из логов шлюза, агента, мониторов и journalctl):
```bash
# Последние 50 строк логов для мода
python3 ~/.hermes/mods/runner.py logs <mod_id>

# Указание количества строк
python3 ~/.hermes/mods/runner.py logs <mod_id> --lines 20
python3 ~/.hermes/mods/runner.py logs <mod_id> -n 20

# Вывод в формате JSON
python3 ~/.hermes/mods/runner.py logs <mod_id> --format json
```

---

### 24. `update` — Синхронизация и обновление экосистемы модов из Git
Служит для проверки, загрузки и атомарного наложения обновлений репозитория модов:
```bash
# Проверка наличия обновлений в удаленном репозитории без применения изменений
python3 ~/.hermes/mods/runner.py update --check [--remote origin] [--branch main]

# Симуляция обновления (dry-run): виртуальное наложение патчей в памяти без изменения файлов
python3 ~/.hermes/mods/runner.py update --dry-run

# Подтягивание изменений из Git в репозиторий модов без наложения патчей на Hermes Agent
python3 ~/.hermes/mods/runner.py update --no-apply

# Полный цикл обновления: git pull + симуляция + атомарное наложение патчей на ядро
python3 ~/.hermes/mods/runner.py update [--remote origin] [--branch main]

# Принудительное обновление с git reset --hard (с сохранением локальных модов) и перезаписью кэшей
python3 ~/.hermes/mods/runner.py update --force
```
- **Защита локальных пользовательских модов:** Автоматически защищает кастомные моды пользователя от перезаписи при `git pull` через `.gitignore`.
- **Pre-flight симуляция:** Перед записью на диск выполняет виртуальную симуляцию наложения всех обновленных таргетов (`simulate_composite_apply`).
- **Zero Downtime:** Обновление и наложение производятся на лету без перезапуска шлюза (`systemctl restart hermes-gateway` строго запрещен).
- **Машиночитаемый формат:** Поддерживает флаг `--json` для автоматизированных сценариев и интеграций.
- **Актуализация кэшей:** После успешного обновления автоматически регенерирует `.commands_cache.json`, `.locales_cache.json` и `.mcp_cache.json`.

---

### 25. `push` — Автоматизированная выгрузка в GitHub (Zero-Friction Push)
Обеспечивает безопасную выгрузку изменений, автокоммит по стандартам Conventional Commits и отправку в репозиторий через настроенный credential helper:
```bash
# Симуляция выгрузки (dry-run): показывает файлы, авто-сообщение коммита и статус без изменений
python3 ~/.hermes/mods/runner.py push --dry-run

# Автоматическая выгрузка с генерацией Conventional Commit (feat/fix/chore/docs)
python3 ~/.hermes/mods/runner.py push

# Выгрузка с явным пользовательским сообщением коммита
python3 ~/.hermes/mods/runner.py push -m "feat(mod-manager): add telegram button actions"

# Установка git-хука post-commit для перманентного авто-пуша при любых git commit
python3 ~/.hermes/mods/runner.py push --install-hook

# Машиночитаемый JSON-вывод для скриптов и агентов
python3 ~/.hermes/mods/runner.py push --dry-run --json
```
- **Аутентификация Zero-Prompt:** использует скрипт `scripts/git-askpass.sh` и настроенный `GITHUB_TOKEN` из `~/.hermes/.env` без интерактивных запросов логина/пароля.
- **Интеллектуальный Conventional Commit:** если `--message` не передан, движок автоматически анализирует измененные файлы (по категориям `custom/`, `system/`, `.engine/`, `runner.py`, `docs/`, `tests/`) и формирует валидное семантическое сообщение.
- **Git Hook `.git/hooks/post-commit`:** флаг `--install-hook` активирует авто-пуш при любых локальных коммитах через стандартный CLI `git commit`.
- **Защита от зацикливания:** переменная окружения `HERMES_DISABLE_AUTOPUSH=1` предотвращает повторный вызов хука внутри `runner.py push`.

---

### 26. `cron` — Планировщик и статус ночного автообновления
Управляет регулярным фоновым трёхфазным обновлением движка и ядра через crontab пользователя:
```bash
# Просмотр текущего статуса задачи автообновления и последних строк лога
python3 ~/.hermes/mods/runner.py cron status

# Машиночитаемый вывод статуса в JSON
python3 ~/.hermes/mods/runner.py cron status --format json

# Установка регулярного ночного автообновления (по умолчанию ежедневно в 04:00)
python3 ~/.hermes/mods/runner.py cron install

# Установка с кастомным cron-расписанием (например, каждый день в 03:00)
python3 ~/.hermes/mods/runner.py cron install --schedule "0 3 * * *"

# Безопасное удаление задачи автообновления из crontab
python3 ~/.hermes/mods/runner.py cron uninstall
```
- **Трёхфазный конвейер:** запускает команду `runner.py update --all`, которая выполняет Git pull движка, обновление ядра Hermes Agent и повторное наложение активных модов с валидацией.
- **Идемпотентность и защита от дубликатов:** при повторном вызове `cron install` проверяет существующие записи crontab и обновляет расписание без дублирования строк.
- **Безопасность сторонних задач:** операции `install` и `uninstall` никогда не затрагивают другие существующие задачи пользователя в crontab.
- **Логирование:** весь вывод пишется в `~/.hermes/logs/update.log`.

---

## Глобальные флаги и опции

| Флаг | Описание |
|---|---|
| `--format <text\|json>` | Формат вывода данных: человекочитаемый текст или структурированный JSON |
| `--status <applied\|disabled\|all>` | Фильтрация модов по статусу наложения в команде `list` |
| `--lines <N>`, `-n <N>` | Количество последних строк логов для команды `logs` (по умолчанию: 50) |
| `--lang <код>` | Выбор языка для вывода описаний модов и слэш-команд. Поддерживает любые коды BCP 47 / 8 базовых языков (`en`, `es`, `fr`, `zh`, `ar`, `ru`, `de`, `pt`) или `auto` |
| `--json` | Машиночитаемый JSON-вывод (для агентов и скриптов) |
| `--check` | Проверка доступности обновлений без применения (в `update`) |
| `--dry-run` | Симуляция обновления и наложения без изменения файлов на диске (в `update`) |
| `--no-apply` | Подтягивание изменений Git в репозиторий модов без наложения патчей на ядро (в `update`) |
| `--remote <имя>` | Имя удаленного Git-репозитория для `update` (по умолчанию: `origin`) |
| `--branch <имя>` | Имя Git-ветки для `update` (по умолчанию: `main`) |
| `--no-cache` / `--force` | Принудительный перезапуск без использования инкрементального кэша / жесткий сброс при обновлении |
| `--populate` | Заполнение значений параметров по умолчанию в `config.yaml` и `secrets.yaml` (в `config`) |
| `--set <ключ>=<значение>` | Установка значения публичного параметра мода в `config.yaml` |
| `--set-secret <ключ>=<значение>` | Установка конфиденциального секрета мода в `secrets.yaml` с правами `0600` |
| `-k <фильтр>`, `--filter <фильтр>` | Фильтрация запускаемых тестов по шаблону/выражению имени (в `test`) |
| `-x`, `--fail-fast` | Немедленная остановка прогона тестов при первом сбое (в `test`) |
| `-v`, `--verbose` | Подробный вывод выполнения команд и тестов |
| `--desc <текст>`, `--description <текст>` | Пользовательское описание снапшота ядра (в `snapshot create`) |
| `--files <файлы...>` | Явный список путей файлов ядра для включения в дельта-снапшот |
| `--no-color` | Отключение ANSI-цветов в терминале |
| `--color` | Принудительное включение цветов |
| `--confirm` / `--yes` | Подтверждение деструктивных действий (для `purge`) |
| `--all` / `--system` | Распространение операции отката на системные компоненты ядра |
| `--circuit-breaker` | Отключение мода с немедленным изолирующим откатом патчей |
| `--reason <текст>` | Обоснование изоляции мода для аудита |
| `--watchdog` | Запуск watchdog healthcheck после `apply` с автооткатом при падении |
| `--watchdog-window <секунды>` | Окно наблюдения сторожевого таймера после `apply` (по умолчанию: 30с) |
| `--window <секунды>` | Окно наблюдения сторожевого таймера в команде `watchdog` (по умолчанию: 30с) |
| `--schedule <выражение>` | Cron-выражение расписания автообновления (по умолчанию: `"0 4 * * *"`) |
| `--out <путь>` | Путь сохранения архива для `export` / `pack` |
| `--include-custom` | Включение пользовательских кастомных модов при экспорте |
| `--no-secrets` | Строгая проверка исключения приватных ключей и токенов при экспорте |
| `--strict-layout` | Принудительная проверка модульной раскладки папочных модов |
