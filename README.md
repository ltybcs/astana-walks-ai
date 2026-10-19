# Astana Walks

ИИ-помощник по прогулкам в Астане для задачи №1 НИЦ Smart City, Build with AI / GDG Astana.

Пользователь выбирает старт, интересы и запас времени. Прототип ищет подходящие POI и возвращает до трёх мест: почему они подходят, что там сделать и сколько примерно займёт прогулка с возвращением к старту. У каждого места есть источник; неизвестные свойства прямо отмечаются.

## Быстрый старт

Нужен Python 3.11 или новее. Сторонние Python- и JavaScript-пакеты не требуются.

```sh
python -m app.server
```

Откройте http://127.0.0.1:8000. Рабочий набор `data/poi.astana.json` содержит 738 POI, автоматически собранных из OpenStreetMap; сырой ответ и запрос сохранены в `data/snapshots/`. Данные получены из источника и не проверены на месте: отсутствие часов не означает, что место открыто. Учебный набор остаётся отдельным для тестов. Без настроенной LLM ответы формируются шаблонно; интерфейс явно показывает режим.

## Полный RAG без облачного API

Для Windows x64 предусмотрен локальный CPU-запуск Qwen3-1.7B Q8_0 и llama.cpp. Сборщик runtime и весов сверяет SHA-256; загрузка примерно 1,9 ГБ выполняется один раз, файлы не попадают в Git. Приложение и модель слушают только loopback.

```sh
python scripts/setup_local_llm.py --download
python scripts/run_demo.py --data data/poi.astana.json --local-llm
```

На Linux/macOS можно запустить собственную llama.cpp/Ollama модель с совместимым Chat Completions API и задать `LLM_BASE_URL` и `LLM_MODEL`. Для loopback ключ не требуется. Для облачного HTTPS endpoint задайте также `LLM_API_KEY`; ключи в репозитории не хранятся.

Конфигурация для указанного локального сервера: `LLM_BASE_URL=http://127.0.0.1:8081/v1`, `LLM_MODEL=astana-local`, `LLM_JSON_MODE=schema`, `LLM_TOKEN_PARAMETER=max_tokens`, `LLM_TIMEOUT_SECONDS=120`. Скрипт `run_demo.py --local-llm` устанавливает эти параметры сам и останавливает оба процесса по Ctrl+C.

```sh
python -m unittest discover -s tests -v
```

## Работа команды

[COORDINATION.md](COORDINATION.md) — общий файл задач, владельцев, API-контракта, проверок и передачи. [AGENTS.md](AGENTS.md) задаёт правила работы Codex, [docs/HANDOFF.md](docs/HANDOFF.md) содержит инструкцию для второго независимого Codex.

Codex A отвечает за интерфейс, локальную LLM и материалы сдачи; Scirtor — за данные, Chrome E2E и финальную интеграцию; Radkurzior выполняет небольшие приёмочные проверки в `qa/` с ревью. Локальный помощник Codex B разрабатывает backend и тесты в общем checkout A. Для отдельных машин/чатов используйте отдельные клоны и ветки `codex/<task>`, согласуя задачи через общий файл и PR. Локальные помощники не являются постоянно работающими облачными процессами.

## Данные и RAG

- `scripts/collect_osm.py` собирает POI через Overpass API/OpenStreetMap. Перед запуском посмотрите `python scripts/collect_osm.py --help`.
- `data/poi.astana.json` — рабочий OSM snapshot; `data/poi.sample.json` — отдельный учебный набор для тестов, не проверенный каталог Астаны.
- Retrieval использует текстовый поиск BM25, координаты, время и ограничения запроса. Кандидаты передаются генератору.
- Extractive-режим работает локально без ключа. В режиме `llm-grounded` LLM пишет объяснения и идеи действий по найденным POI. Приложение проверяет ids/цитаты/известные факты и защищённые утверждения; механическая проверка не доказывает семантическую достоверность каждой фразы.
- Данные OpenStreetMap требуют атрибуции и соблюдения ODbL: [OpenStreetMap contributors](https://www.openstreetmap.org/copyright). Лицензия данных не является лицензией кода проекта.

Сбор и запуск на реальных данных:

```sh
python scripts/collect_osm.py --output data/poi.osm.json
python -m app.server --data data/poi.osm.json
```

Сбор требует сетевого доступа к Overpass. Ошибка сети сохраняет существующий набор. Файл реальной выгрузки игнорируется Git по умолчанию: добавляйте его осознанно после проверки происхождения и качества.

Файлы `.env` автоматически не загружаются. Без настроенной модели, при сетевой ошибке или отклонённом ответе работает extractive fallback с видимым предупреждением. `LLM_JSON_MODE` принимает schema/json/none в зависимости от совместимости провайдера; по умолчанию используется JSON Schema. Полный пользовательский запрос и точные координаты в LLM не передаются.

## Оценка и контейнер

```sh
python -m app.evaluate --data data/poi.astana.json --output output/evaluation.json --run-tests
python -m app.evaluate --data data/poi.astana.json --output output/evaluation-live.json --llm
docker build -t astana-walks .
docker run --rm -p 127.0.0.1:8000:8000 astana-walks
```

Вторая команда требует работающей модели и соответствующего окружения. Отчёт содержит результаты сценариев, фактические режимы, число POI и SHA-256 набора. Контейнер запускает приложение без локальных весов; для полного локального RAG используйте `run_demo.py`. CI проверяет Python-тесты и запуск контейнера.

### Данные 2ГИС

Доступен отдельный сборщик официального Places API. Получите собственный
[демо-ключ 2ГИС](https://docs.2gis.com/platform-manager/subscription/keys) и задайте
его в окружении `DGIS_API_KEY`; `.env` автоматически не читается.

```sh
python scripts/collect_2gis.py --output data/poi.2gis.json
python -m app.server --data data/poi.2gis.json
```

Для одного сценария можно сузить поиск:

```sh
python scripts/collect_2gis.py --query Кофейни --pages 2 --output data/poi.2gis.json
```

Сохранённый ответ официального API можно нормализовать без сети и ключа:

```sh
python scripts/collect_2gis.py --input tmp/2gis-response.json --output data/poi.2gis.json
```

Выгрузки игнорируются Git. Сборщик сохраняет происхождение, проверяет координаты
и не выводит ключ в ошибках. Неизвестные свойства остаются неизвестными.
Тесты используют синтетические ответы; реальная выгрузка 2ГИС пока не получена.
Подробности и статус проверки: [источники данных](data/SOURCES.md).

Подробности и ограничения: [концепция и RAG](docs/CONCEPT.md), [сценарии проверки](docs/SCENARIOS.md), [приватность](docs/PRIVACY.md), [план выступления](docs/PITCH.md).

## Структура

```text
app/                  HTTP API, retrieval, grounded generation
web/                  Русский интерфейс, схема POI, рекомендации
scripts/collect_osm.py Автоматизированный сбор POI
data/                 Реальные POI, сырой snapshot и отдельный учебный набор
tests/                Проверки ограничений и корректности
docs/                 Концепция, сценарии, ограничения, передача
COORDINATION.md       Общие задачи, владельцы, проверки и передача трём Codex
```

## Статус хакатонной сдачи

Актуальные выполненные проверки, материалы и оставшиеся задачи записаны в `COORDINATION.md` и [docs/SUBMISSION.md](docs/SUBMISSION.md). Отчёты не подменяют полевую проверку POI. Окончательная готовность сдачи определяется наличием проверенных материалов и видео по ссылке.
