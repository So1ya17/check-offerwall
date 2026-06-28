# check-offerwall

Скрипт для массовой проверки офферов на лендингах: сравнивает `db.json` на сайте с эталоном из CSV.

## Установка

```bash
python -m venv .offerwall
source .offerwall/bin/activate
pip install -r requirements.txt
```

## Структура

| Путь | Назначение |
|------|------------|
| `spreadsheets/*.csv` | Эталонные списки офферов по категориям |
| `domain-list/*` | URL лендингов и метки (ID) |
| `categories.json` | Маппинг заголовков CSV → поля `db.json` |
| `name_mappings.json` | Ручные алиасы имён офферов |
| `check_offers.py` | Основной скрипт |

Файлы paired по именам: `example.csv` ↔ `domain-list/example.txt` (или точное имя без суффикса).

## Запуск

### Интерактивно

```bash
python check_offers.py
```

### Один CSV и один список доменов

```bash
python check_offers.py \
  --csv spreadsheets/example.csv \
  --domains domain-list/example.txt \
  --no-suggestions
```

### Все пары

```bash
python check_offers.py --all --concurrency 20 --no-suggestions
```

## Флаги

| Флаг | Описание |
|------|----------|
| `--csv`, `--domains` | Одиночная пара файлов |
| `--all` | Все совпавшие пары из каталогов |
| `--concurrency N` | Параллельных HTTP-запросов (по умолчанию 20) |
| `--no-suggestions` | Не спрашивать про новые маппинги в терминале |
| `-o PATH` | HTML-отчёт |
| `--json PATH` | JSON-отчёт (по умолчанию `report_*.json`) |
| `--summary-csv PATH` | CSV-сводка (по умолчанию `summary_*.csv`) |
| `--categories PATH` | Файл категорий (по умолчанию `categories.json`) |
| `--strict-validation` | Остановиться при ошибках валидации входных данных |

## Статусы

- **OK** — состав офферов совпадает (расхождение только в порядке тоже OK).
- **Issues** — есть missing/extra или отсутствует поле в `db.json`.
- **Error** — не удалось скачать или разобрать `db.json`.

Порядок сверяется строго по индексу: i-й оффер в CSV = i-й элемент массива в `db.json`. Отличие порядка пишется в отчёт, но статус остаётся **OK** (авторанжирование на сайте).

## Коды выхода

| Код | Значение |
|-----|----------|
| `0` | Нет доменов со статусом Issues/Error |
| `1` | Есть Issues или Error |
| `2` | Ошибка аргументов или валидации |

Пример для cron:

```bash
python check_offers.py --all --no-suggestions || echo "Есть расхождения"
```

## Категории

Редактируйте `categories.json`:

```json
{
  "Займы": "loans",
  "кредиты": "credits"
}
```

Заголовки в CSV должны совпадать с ключами этого файла.

## Валидация до HTTP

Перед запросами к доменам скрипт проверяет:

- дубликаты URL в списке доменов;
- пустые категории в CSV;
- отсутствие пары `csv` ↔ `domain-list` (режим `--all`);
- подозрительные имена офферов (пробелы, кавычки, слабое fuzzy без маппинга).

С `--strict-validation` при ошибках валидации проверка не запускается.
