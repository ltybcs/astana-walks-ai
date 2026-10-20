# Запуск основного единого помощника и откат

Целевая конфигурация: единый OSM+Google+2ГИС интерфейс на loopback8000 за существующим HTTPS-туннелем;8003 — только локальный предпросмотр. **Фактическое переключение пока ожидает подтверждения Codex A** в source-bound протоколе. Эта инструкция не утверждает, что публичная проверка уже выполнена.

Перед изменением на GitHub сохранён tag `checkpoint/before-unified-2026-10-20`, commit `a20dc81`: прежний сайт и материалы. По записи интегратора отличие от прежнего развёрнутого `a46b` — только COORDINATION. Модель8081 и сам туннель при переключении не перезапускаются.

## Настройки и основной запуск8000

Команды выполняются из checkout, содержащего принятую единую версию. Замените четыре значения в угловых скобках **абсолютными существующими путями**; это пути к приватным файлам, не содержимое ключей. Не создавайте новые ledger вместо действующих.

```powershell
$taskCheckout = '<absolute-unified-checkout>'
$taskPrimary = '<absolute-primary-workspace>'
$taskGoogleConfig = '<absolute-existing-google-config.json>'
$taskEmbedConfig = '<absolute-existing-embed-config.json>'
Set-Location -LiteralPath $taskCheckout
$env:LLM_BASE_URL = 'http://127.0.0.1:8081/v1'
$env:LLM_MODEL = 'astana-local'
$env:LLM_JSON_MODE = 'schema'
$env:LLM_TOKEN_PARAMETER = 'max_tokens'
$env:LLM_TIMEOUT_SECONDS = '120'
python -B -m app.google_lab --host 127.0.0.1 --port 8000 --llm --google --google-config $taskGoogleConfig --dgis-root $taskPrimary --map-config $taskEmbedConfig --public-origin https://entire-burn-cricket-ashley.trycloudflare.com
```

Перед запуском завершите только прежний процесс приложения на8000. Порт8081 должен уже обслуживать закреплённую локальную модель. Не запускайте `run_demo.py`: он относится к старому приложению и может создать второй процесс модели. Туннель продолжает направлять тот же публичный адрес на loopback8000.

Google config содержит `key_file` и **тот же** `ledger_file` существующего500-попыточного пилота. Optional `cache_dir` относится лишь к историческим отдельным endpoints; смешанные ответы никогда не архивируются. `$taskPrimary` содержит существующие `.local/2gis/api-key`, `usage.json`, `usage.lock`: это тот же20/24h лимит, который использовало старое приложение. Пути/ключи не коммитятся, значения ключей не передаются в CLI.

Embed config содержит `{"embed_key_file":"<absolute-dedicated-browser-key>"}`. Отдельный ключ намеренно доступен браузеру, ограничен **только Maps Embed API** и пятью referrers:

```text
http://127.0.0.1:8000/*
http://localhost:8000/*
http://127.0.0.1:8003/*
http://localhost:8003/*
https://entire-burn-cricket-ashley.trycloudflare.com/*
```

Backend Places/GML key в iframe запрещён. `--public-origin` разрешает только указанный HTTPS origin в проверках Host/Origin; это не wildcard для Cloudflare и не отключение проверки POST. Слушатель остаётся loopback. Iframe передаёт только origin страницы как referrer; пользовательский запрос и GPS в referrer не включаются.

## Локальный предпросмотр8003

Используйте те же конфигурации, ledger и8081. Отдельный процесс увеличивает нагрузку и расходует **те же**, а не новые лимиты:

```powershell
python -B -m app.google_lab --host 127.0.0.1 --port 8003 --llm --google --google-config $taskGoogleConfig --dgis-root $taskPrimary --map-config $taskEmbedConfig
```

Не добавляйте `--public-origin` к локальному предпросмотру без необходимости. Не запускайте одновременно конкурирующие live QA на обоих портах. GET `/api/unified/status` показывает локальную безопасную диагностику без нового провайдерского запроса.

## Проверка после переключения

Codex A фиксирует deployed commit/source SHA и результат по фактическому публичному адресу. Нужно различать загрузку оболочки, поиск и модель: manifest/icons/service worker могут работать при недоступном внешнем источнике.

- Первое открытие: одно поле, одна кнопка, без провайдерских переключателей и начальной карты.
- Неизвестный старт/отказ GPS: явная ошибка, ноль поиска и LLM.
- Разрешённый публичный сценарий: отдельные receipts трёх источников, один полный ответ или честно обозначенный fallback, все Google previews.
- Построенный путь: единственная Google-карта с остановками и возвращением; другой ready-вариант не запускает новый поиск/LLM.
- Установка: прежние manifest id/start_url/scope; новый `astana-walks-shell-v5-unified` удаляет только известные shell caches. Нет автоматической перезагрузки посреди запроса. Кэшируется только allowlisted оболочка; API, query strings, ответы, координаты, cross-origin карты не кэшируются.

Проверка телефона прежней версии не считается проверкой новой установки. Исторические receipts сохраняются; тексты и сырые данные провайдеров при новой QA на диск не пишутся.

## Откат без reset рабочей копии

Сначала завершите только единый процесс8000. Из Git checkout с доступом к origin подготовьте **новую отсутствующую папку**. Эти команды не меняют текущие файлы и ветку:

```powershell
$taskRollback = 'C:\Users\ltybcs\Documents\ChatGPT\astana-walks-before-unified-2026-10-20'
if (Test-Path -LiteralPath $taskRollback) { throw 'Выберите новую отсутствующую папку отката.' }
git fetch origin tag checkpoint/before-unified-2026-10-20
git worktree add --detach $taskRollback checkpoint/before-unified-2026-10-20
Set-Location -LiteralPath $taskRollback
Remove-Item Env:DGIS_API_KEY -ErrorAction SilentlyContinue
python -B -m app.server --host 127.0.0.1 --port 8000 --data "$taskPrimary/data/poi.astana.json" --refresh-poi
```

Сохраните LLM env из раздела запуска: старый `app.server` читает его автоматически и **не имеет флага `--llm`**. В новой чистой worktree нет приватного runtime2ГИС, поэтому отдельная панель2ГИС при таком откате будет недоступна. Команда удаляет унаследованный DGIS_API_KEY только из текущего процесса PowerShell, чтобы не открыть новый счётчик в новой папке. Не копируйте ключ или ledger и не создавайте новый бюджет. Для полного восстановления старого2ГИС интегратор отдельно возвращает прежний primary-процесс с его существующим runtime и проверенными исходниками.

Модель8081 и HTTPS-туннель остаются прежними. Проверьте публичную загрузку старого интерфейса и его service worker после отката; не считайте браузерный кэш доказательством версии. Не используйте `git reset --hard`, force push или перенос поверх существующей рабочей папки.

[Основной контракт и ограничения](UNIFIED_LAB.md) · [README](../README.md) · [метаданные QA](../qa/unified-search-check.json).
