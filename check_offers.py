#!/usr/bin/env python3
"""
Script to check db.json on landing domains against offer lists from CSV spreadsheets.
"""

import argparse
import asyncio
import csv
import json
import os
import sys
from datetime import datetime
from difflib import SequenceMatcher
from urllib.parse import urljoin

import aiohttp

try:
    from termcolor import colored
except ImportError:
    def colored(text, color=None, on_color=None, attrs=None):
        return text


DEFAULT_CSV_DIR = 'spreadsheets/'
DEFAULT_DOMAIN_DIR = 'domain-list/'
DEFAULT_CATEGORIES_FILE = 'categories.json'
DEFAULT_MAPPING_FILE = 'name_mappings.json'
DEFAULT_CONCURRENCY = 20

EXIT_OK = 0
EXIT_CONTENT_ISSUES = 1
EXIT_VALIDATION_OR_USAGE = 2


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def load_category_config(categories_file_path):
    """Load CSV category name -> db.json field mapping."""
    try:
        with open(categories_file_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except FileNotFoundError:
        print(colored(f"Файл категорий не найден: {categories_file_path}", "red"))
        return None
    except json.JSONDecodeError as e:
        print(colored(f"Ошибка JSON в {categories_file_path}: {e}", "red"))
        return None

    if not isinstance(data, dict) or not data:
        print(colored(f"Файл категорий должен быть непустым объектом: {categories_file_path}", "red"))
        return None

    return data


def map_category_to_db_field(category_name, category_mapping):
    return category_mapping.get(category_name, category_name)


# ---------------------------------------------------------------------------
# Input files
# ---------------------------------------------------------------------------

def read_csv_offers(csv_file_path, category_names):
    """
    Read the CSV file and extract offer categories.
    Category headers must match names from categories.json.
    """
    categories = {}
    current_category = None
    unknown_headers = []

    try:
        with open(csv_file_path, 'r', encoding='utf-8-sig') as f:
            reader = csv.reader(f)
            rows = list(reader)

        for row in rows:
            if not any(field.strip() for field in row):
                continue

            first_col = row[0].strip().strip('"') if row else ""

            if first_col in category_names:
                current_category = first_col
                categories.setdefault(current_category, [])
            elif first_col and first_col not in category_names and not current_category:
                if first_col not in unknown_headers:
                    unknown_headers.append(first_col)
            elif current_category and first_col:
                categories[current_category].append(first_col.strip('"'))

        if not categories:
            with open(csv_file_path, 'r', encoding='utf-8-sig') as f:
                lines = f.readlines()

            for line in lines:
                line = line.strip().strip('"')
                if not line:
                    continue

                if line in category_names:
                    current_category = line
                    categories.setdefault(current_category, [])
                elif current_category:
                    categories[current_category].append(line)
                elif line not in unknown_headers:
                    unknown_headers.append(line)

    except OSError as e:
        print(colored(f"Ошибка чтения CSV: {e}", "red"))
        return {}, unknown_headers

    return categories, unknown_headers


def read_domain_list(domain_file_path):
    domains = []
    with open(domain_file_path, 'r', encoding='utf-8-sig') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue

            parts = line.split(' - ')
            domain_part = parts[0] if parts else line
            id_label = parts[1] if len(parts) > 1 else None

            if not domain_part.startswith(('http://', 'https://')):
                domain_part = 'https://' + domain_part

            domains.append((domain_part, id_label))

    return domains


def list_available_files(directory, extension):
    if not os.path.isdir(directory):
        return []
    files = []
    for filename in os.listdir(directory):
        if extension == "" or filename.endswith(extension):
            files.append(filename)
    return sorted(files)


def select_file(files, directory, file_type, allow_all=False):
    if not files:
        print(colored(f"Нет файлов ({file_type}) в {directory}", "red"))
        return None

    print(colored(f"\nДоступные файлы ({file_type}):", "blue"))
    for i, filename in enumerate(files, 1):
        print(colored(f"{i}. {filename}", "cyan"))

    if allow_all:
        print(colored(f"{len(files) + 1}. ALL", "cyan"))
        prompt_range = f"1-{len(files) + 1}"
    else:
        prompt_range = f"1-{len(files)}"

    while True:
        try:
            choice = int(input(colored(f"\nВыберите файл ({prompt_range}): ", "yellow")))
            if 1 <= choice <= len(files):
                return os.path.join(directory, files[choice - 1])
            if allow_all and choice == len(files) + 1:
                return "ALL"
            print(colored(f"Введите число от {prompt_range}", "red"))
        except (ValueError, KeyboardInterrupt, EOFError):
            print(colored("Некорректный ввод.", "red"))
            return None


def build_file_pairs(csv_dir, domain_dir, csv_files):
    domain_files_list = list_available_files(domain_dir, '')
    pairs = []
    unmatched_csv = []

    for csv_file in csv_files:
        base_name = os.path.splitext(csv_file)[0]
        matching_domain_file = None

        if base_name in domain_files_list:
            matching_domain_file = base_name
        elif f"{base_name}_mimic" in domain_files_list:
            matching_domain_file = f"{base_name}_mimic"

        if matching_domain_file:
            pairs.append({
                "csv": os.path.join(csv_dir, csv_file),
                "domain": os.path.join(domain_dir, matching_domain_file),
                "pair_name": f"{csv_file} -> {matching_domain_file}",
            })
        else:
            unmatched_csv.append(csv_file)

    return pairs, unmatched_csv


# ---------------------------------------------------------------------------
# Validation (before HTTP)
# ---------------------------------------------------------------------------

def _normalize_url(url):
    return url.rstrip('/').lower()


def _offer_has_mapping(offer_name, name_mappings):
    if offer_name in name_mappings or offer_name in name_mappings.values():
        return True
    return False


def _offer_looks_suspicious(offer_name):
    stripped = offer_name.strip()
    if not stripped:
        return True
    if stripped != offer_name:
        return True
    if '"' in offer_name or stripped.startswith('"') or stripped.endswith('"'):
        return True
    return False


def validate_file_pair(pair, category_names, name_mappings):
    """
    Validate a single csv/domain pair before HTTP requests.
    Returns (is_runnable, warnings, errors).
    """
    warnings = []
    errors = []
    pair_label = pair.get('pair_name') or pair['csv']

    if not os.path.isfile(pair['csv']):
        errors.append(f"{pair_label}: CSV не найден — {pair['csv']}")
        return False, warnings, errors

    if not os.path.isfile(pair['domain']):
        errors.append(f"{pair_label}: список доменов не найден — {pair['domain']}")
        return False, warnings, errors

    categories, unknown_headers = read_csv_offers(pair['csv'], category_names)
    if unknown_headers:
        for header in unknown_headers:
            warnings.append(
                f"{pair_label}: неизвестный заголовок «{header}» (нет в categories.json)"
            )

    if not categories:
        errors.append(f"{pair_label}: в CSV не найдено ни одной категории")
        return False, warnings, errors

    for category_name, offers in categories.items():
        if not offers:
            errors.append(f"{pair_label}: категория «{category_name}» пуста")

    domains = read_domain_list(pair['domain'])
    if not domains:
        errors.append(f"{pair_label}: список доменов пуст")
        return False, warnings, errors

    seen_urls = {}
    for url, app_id in domains:
        key = _normalize_url(url)
        if key in seen_urls:
            prev_id = seen_urls[key]
            label = f" [{app_id}]" if app_id else ""
            prev_label = f" [{prev_id}]" if prev_id else ""
            warnings.append(
                f"{pair_label}: дубликат URL {url}{label} "
                f"(уже есть{prev_label})"
            )
        else:
            seen_urls[key] = app_id

    mapping_values = set(name_mappings.values())
    for category_name, offers in categories.items():
        for offer in offers:
            if _offer_looks_suspicious(offer):
                warnings.append(
                    f"{pair_label}: подозрительное имя оффера «{offer}» "
                    f"(пробелы/кавычки) в «{category_name}»"
                )
            elif not _offer_has_mapping(offer, name_mappings):
                best_score = 0.0
                for candidate in list(name_mappings.keys()) + list(mapping_values):
                    score = SequenceMatcher(
                        None, normalize_name(offer), normalize_name(candidate)
                    ).ratio()
                    best_score = max(best_score, score)
                if best_score < 0.6:
                    warnings.append(
                        f"{pair_label}: «{offer}» без маппинга и слабое fuzzy-совпадение "
                        f"(лучший score {best_score:.2f})"
                    )

    is_runnable = not errors
    return is_runnable, warnings, errors


def validate_before_run(file_pairs, category_names, name_mappings):
    all_warnings = []
    all_errors = []
    runnable_pairs = []

    for pair in file_pairs:
        is_runnable, warnings, errors = validate_file_pair(
            pair, category_names, name_mappings
        )
        all_warnings.extend(warnings)
        all_errors.extend(errors)
        if is_runnable:
            runnable_pairs.append(pair)

    return runnable_pairs, all_warnings, all_errors


def print_validation_messages(warnings, errors):
    for message in warnings:
        print(colored(f"Предупреждение: {message}", "yellow"))
    for message in errors:
        print(colored(f"Ошибка: {message}", "red"))


# ---------------------------------------------------------------------------
# Comparison logic
# ---------------------------------------------------------------------------

def normalize_name(name):
    normalized = name.lower().strip()
    return normalized.replace('-', '').replace('_', '').replace(' ', '')


def fuzzy_match(str1, str2, name_mappings=None, threshold=0.8):
    if name_mappings:
        if str1 in name_mappings and name_mappings[str1] == str2:
            return True
        if str2 in name_mappings and name_mappings[str2] == str1:
            return True

    similarity = SequenceMatcher(None, normalize_name(str1), normalize_name(str2)).ratio()
    return similarity >= threshold


def _offer_at_index_matches(expected, actual_offer, name_mappings):
    name = actual_offer.get('name', '')
    description = actual_offer.get('description', '')
    if fuzzy_match(expected, name, name_mappings):
        return True
    return fuzzy_match(expected, description, name_mappings)


def compare_offers(expected_offers, actual_offers, name_mappings=None):
    if name_mappings is None:
        name_mappings = {}

    actual_names = []
    for offer in actual_offers:
        name = offer.get('name', '')
        description = offer.get('description', '')
        actual_names.append({'name': name, 'description': description})

    actual_name_list = [item['name'] for item in actual_names]

    missing = []
    for expected in expected_offers:
        matched = False
        for actual in actual_name_list:
            if fuzzy_match(expected, actual, name_mappings):
                matched = True
                break

        if not matched:
            for actual_obj in actual_names:
                desc = actual_obj['description']
                if fuzzy_match(expected, actual_obj['name'], name_mappings) or fuzzy_match(
                    expected, desc, name_mappings
                ):
                    matched = True
                    break

        if not matched:
            missing.append(expected)

    extra = []
    for actual in actual_name_list:
        matched = any(
            fuzzy_match(actual, expected, name_mappings) for expected in expected_offers
        )
        if not matched:
            extra.append(actual)

    ordered_correctly = True
    order_mismatches = []

    for i, expected in enumerate(expected_offers):
        if i >= len(actual_offers):
            ordered_correctly = False
            order_mismatches.append({
                'position': i + 1,
                'expected': expected,
                'actual': '(нет оффера на этой позиции)',
            })
            continue

        if not _offer_at_index_matches(expected, actual_offers[i], name_mappings):
            ordered_correctly = False
            actual_offer = actual_offers[i]
            actual_label = (
                actual_offer.get('name', '')
                or actual_offer.get('description', '')
                or '(пусто)'
            )
            order_mismatches.append({
                'position': i + 1,
                'expected': expected,
                'actual': actual_label,
            })

    return {
        'missing': missing,
        'extra': extra,
        'ordered_correctly': ordered_correctly,
        'order_mismatches': order_mismatches,
        'expected_count': len(expected_offers),
        'actual_count': len(actual_offers),
    }


def load_mapping_file(mapping_file_path):
    try:
        with open(mapping_file_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_mapping_file(mapping, mapping_file_path):
    with open(mapping_file_path, 'w', encoding='utf-8') as f:
        json.dump(mapping, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# HTTP + domain processing
# ---------------------------------------------------------------------------

async def get_remote_db_json(session, base_url, retries=1):
    if not base_url.endswith('/'):
        base_url += '/'
    db_url = urljoin(base_url, 'db.json')

    headers = {
        'User-Agent': (
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
            'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        )
    }

    timeout = aiohttp.ClientTimeout(total=20, connect=10)
    last_error = None

    for attempt in range(retries + 1):
        try:
            async with session.get(
                db_url, timeout=timeout, ssl=False, headers=headers
            ) as response:
                response.raise_for_status()
                text_content = await response.text(encoding='utf-8-sig')

            try:
                data = json.loads(text_content)
            except json.JSONDecodeError as e:
                print(colored(f"  JSON decode error for {base_url}: {e}", "red"))
                return None

            if not isinstance(data, dict):
                print(colored(
                    f"  Unexpected JSON type from {base_url} "
                    f"(expected dict, got {type(data).__name__})",
                    "red",
                ))
                return None

            return data

        except aiohttp.ClientConnectorError as e:
            last_error = f"Connection error: {e}"
        except asyncio.TimeoutError:
            last_error = "Timeout"
        except aiohttp.ClientResponseError as e:
            last_error = f"HTTP error {e.status}"
            if 400 <= e.status < 500:
                break
        except OSError as e:
            last_error = f"Network error: {e}"

        if attempt < retries:
            await asyncio.sleep(2)

    print(colored(f"  {last_error} for {base_url}", "red"))
    return None


async def process_domain_async(
    session,
    domain_info,
    categories,
    name_mappings,
    category_mapping,
    domain_semaphore,
):
    domain, app_id = domain_info
    domain_result = {
        'domain': domain,
        'app_id': app_id,
        'status': 'OK',
        'error_message': None,
        'category_results': [],
    }

    async with domain_semaphore:
        db_data = await get_remote_db_json(session, domain, retries=1)

    if not db_data:
        domain_result['status'] = 'Error'
        domain_result['error_message'] = "Could not fetch or parse db.json"
        print(f"  {colored('Error', 'red')} - {domain}")
        return domain_result

    domain_has_issues = False
    for category_name, expected_offers in categories.items():
        db_field = map_category_to_db_field(category_name, category_mapping)
        if db_field not in db_data:
            category_result = {
                'name': category_name,
                'has_issues': True,
                'error': f"Field '{db_field}' not in db.json",
            }
            domain_result['category_results'].append(category_result)
            domain_has_issues = True
            continue

        actual_offers = db_data.get(db_field, []) or []
        comparison_result = compare_offers(expected_offers, actual_offers, name_mappings)
        has_content_issues = bool(comparison_result['missing'] or comparison_result['extra'])

        if has_content_issues:
            domain_has_issues = True

        domain_result['category_results'].append({
            'name': category_name,
            'has_issues': has_content_issues,
            **comparison_result,
        })

    if domain_has_issues:
        domain_result['status'] = 'Issues'

    status_color = {"OK": "green", "Issues": "yellow", "Error": "red"}
    print(f"  {colored(domain_result['status'], status_color[domain_result['status']])} - {domain}")

    return domain_result


async def run_checks(file_pairs, category_names, category_mapping, name_mappings, concurrency):
    all_results = []
    domain_semaphore = asyncio.Semaphore(concurrency)

    async with aiohttp.ClientSession() as session:
        for pair in file_pairs:
            print(colored(f"\nПроверка: {pair['pair_name']}", "magenta"))
            categories, _ = read_csv_offers(pair['csv'], category_names)
            domains = read_domain_list(pair['domain'])

            print(colored(f"-> {len(domains)} доменов (concurrency={concurrency})...", "blue"))
            tasks = [
                process_domain_async(
                    session,
                    domain_info,
                    categories,
                    name_mappings,
                    category_mapping,
                    domain_semaphore,
                )
                for domain_info in domains
            ]
            results = await asyncio.gather(*tasks)
            all_results.extend(results)

    return all_results


# ---------------------------------------------------------------------------
# Mapping suggestions (interactive)
# ---------------------------------------------------------------------------

def suggest_mappings(all_results, name_mappings, mapping_file_path):
    mappings_updated = False
    original_mappings = list(name_mappings.keys()) + list(name_mappings.values())

    for res in all_results:
        if res['status'] != 'Issues':
            continue
        for cat_res in res['category_results']:
            missing = cat_res.get('missing', [])
            extra = cat_res.get('extra', [])
            if len(missing) != 1 or len(extra) != 1:
                continue

            if missing[0] in original_mappings or extra[0] in original_mappings:
                continue

            similarity = SequenceMatcher(
                None, normalize_name(missing[0]), normalize_name(extra[0])
            ).ratio()
            if not (0.6 < similarity < 0.95):
                continue

            try:
                prompt = colored(
                    f"\nДобавить маппинг «{missing[0]}» -> «{extra[0]}»? (y/n): ",
                    "cyan",
                )
                user_input = input(prompt)
                if user_input.lower() == 'y':
                    name_mappings[missing[0]] = extra[0]
                    original_mappings.extend([missing[0], extra[0]])
                    mappings_updated = True
                    print(colored("-> Маппинг добавлен. Перезапустите проверку.", "green"))
            except (KeyboardInterrupt, EOFError):
                print(colored("\nПодсказки пропущены.", "yellow"))
                return mappings_updated

    if mappings_updated:
        save_mapping_file(name_mappings, mapping_file_path)
        print(colored("\nname_mappings.json обновлён.", "green"))

    return mappings_updated


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

def _category_has_order_warning(cat_res):
    return not cat_res.get('ordered_correctly', True) and not cat_res.get('error')


def _domain_has_order_warning(domain_res):
    if domain_res['status'] != 'OK':
        return False
    return any(
        _category_has_order_warning(cat)
        for cat in domain_res.get('category_results', [])
    )


def _format_order_mismatches_html(order_mismatches, informational=False):
    if not order_mismatches:
        css_class = 'ok-issue' if informational else ''
        return f"<li class='{css_class}'>Порядок не совпадает с CSV</li>"

    items = []
    for mismatch in order_mismatches:
        css_class = 'ok-issue' if informational else ''
        items.append(
            f"<li class='{css_class}'>Поз. {mismatch['position']}: "
            f"ожидался «{mismatch['expected']}», на сайте «{mismatch['actual']}»</li>"
        )
    return ''.join(items)


def generate_html_report(results, report_filename):
    total_count = len(results)
    content_issues_count = sum(1 for r in results if r['status'] != 'OK')
    order_warning_count = sum(1 for r in results if _domain_has_order_warning(r))
    fully_ok_count = total_count - content_issues_count - order_warning_count

    html_style = """
    <style>
        body { font-family: sans-serif; margin: 2em; background-color: #f9f9f9; }
        h1, h2, h3 { color: #333; }
        .summary { background-color: #eee; padding: 1em; border-radius: 8px; margin-bottom: 1em; }
        .summary ul { margin: 0.5em 0 0 1.2em; }
        .domain-card { border: 1px solid #ddd; border-radius: 8px; margin-bottom: 1.5em; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }
        .domain-header { padding: 0.8em 1.2em; border-bottom: 1px solid #ddd; }
        .domain-header.status-OK { background-color: #d4edda; color: #155724; }
        .domain-header.status-Issues { background-color: #fff3cd; color: #856404; }
        .domain-header.status-Error { background-color: #f8d7da; color: #721c24; }
        .domain-content { padding: 1.2em; }
        table { border-collapse: collapse; width: 100%; margin-top: 1em; }
        th, td { border: 1px solid #ddd; padding: 0.6em; text-align: left; }
        th { background-color: #f2f2f2; }
        .missing { color: #dc3545; }
        .extra { color: #ffc107; }
        .error { font-weight: bold; color: #dc3545; }
        .order-info { color: #155724; }
        .ok-issue { color: #28a745; }
        a { color: #007bff; text-decoration: none; }
        a:hover { text-decoration: underline; }
        .ok-card.hidden { display: none; }
        .toolbar { margin-bottom: 1em; }
        .btn { display: inline-block; padding: 0.4em 0.8em; border-radius: 4px; background: #007bff; color: white; text-decoration: none; cursor: pointer; }
        .btn.secondary { background: #6c757d; }
    </style>
    """

    domain_cards_html = []
    for res in results:
        is_fully_ok = (res['status'] == 'OK') and (not _domain_has_order_warning(res))

        domain_display = res['domain'] + (f" [{res['app_id']}]" if res['app_id'] else "")
        card_content = ''

        if res['status'] == 'Error':
            card_content = f"<p class='error'>Ошибка: {res['error_message']}</p>"
        else:
            categories_html = []
            for cat_res in res['category_results']:
                has_content_issues = cat_res.get('has_issues', False) or bool(cat_res.get('error'))
                has_order_warning = _category_has_order_warning(cat_res)
                # show only categories with issues or informative order warnings
                if not has_content_issues and not has_order_warning:
                    continue

                cat_name = cat_res['name']
                details_html = ''

                if cat_res.get('error'):
                    details_html = f"<tr><td colspan='2' class='error'>{cat_res['error']}</td></tr>"
                else:
                    missing = cat_res.get('missing', [])
                    extra = cat_res.get('extra', [])
                    order_mismatches = cat_res.get('order_mismatches', [])
                    informational_order = res['status'] == 'OK'

                    missing_html = ''.join(f"<li>{m}</li>" for m in missing)
                    extra_html = ''.join(f"<li>{e}</li>" for e in extra)
                    order_html = _format_order_mismatches_html(
                        order_mismatches,
                        informational=informational_order,
                    )

                    missing_row = (
                        f"<tr><td>Отсутствуют ({len(missing)})</td>"
                        f"<td class='missing'><ul>{missing_html}</ul></td></tr>"
                        if missing else ""
                    )
                    extra_row = (
                        f"<tr><td>Лишние ({len(extra)})</td>"
                        f"<td class='extra'><ul>{extra_html}</ul></td></tr>"
                        if extra else ""
                    )
                    order_row = (
                        f"<tr><td>Порядок</td>"
                        f"<td class='order-info'><ul>{order_html}</ul>"
                        f"{' <em>(авторанжирование, OK)</em>' if informational_order else ''}"
                        f"</td></tr>"
                        if has_order_warning else ""
                    )
                    count_row = (
                        f"<tr><td>Количество</td>"
                        f"<td>Ожидалось: {cat_res.get('expected_count', 'N/A')}, "
                        f"на сайте: {cat_res.get('actual_count', 'N/A')}</td></tr>"
                    )
                    details_html = f"{missing_row}{extra_row}{order_row}{count_row}"

                categories_html.append(f"""
                    <h3>Категория: {cat_name}</h3>
                    <table>{details_html}</table>
                """)

            card_content = (
                ''.join(categories_html) if categories_html else "<p>Нет данных для отображения.</p>"
            )

        card_classes = "domain-card"
        if is_fully_ok:
            # collapse fully OK cards by default
            card_classes += " ok-card hidden"

        domain_cards_html.append(f"""
        <div class="{card_classes}">
            <div class="domain-header status-{res['status']}">
                <h2><a href="{res['domain']}" target="_blank">{domain_display}</a> — {res['status']}</h2>
            </div>
            <div class="domain-content">{card_content}</div>
        </div>
        """)

    # If there are no cards at all (no issues and nothing to show), show a friendly message
    if not domain_cards_html:
        domain_cards_html.append(
            "<p>Все домены совпадают с CSV по составу и порядку офферов.</p>"
        )

    toggle_button_html = ''
    if fully_ok_count > 0:
        toggle_button_html = f'<div class="toolbar"><button id="toggle-ok-btn" class="btn" onclick="toggleOk()">Показать полностью OK ({fully_ok_count})</button></div>'

    html_content = (
        "<!DOCTYPE html>\n<html lang=\"ru\">\n<head>\n    <meta charset=\"UTF-8\">\n    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\">\n    <title>Отчёт проверки офферов</title>\n"
        + html_style
        + "\n</head>\n<body>\n"
        "    <h1>Отчёт проверки офферов</h1>\n"
        "    <div class=\"summary\">\n        <h2>Сводка</h2>\n        <ul>\n"
        + f"            <li>Проверено доменов: {total_count}</li>\n"
        + f"            <li>Полностью совпадают: {fully_ok_count}</li>\n"
        + f"            <li>Расхождения по составу (missing/extra): {content_issues_count}</li>\n"
        + f"            <li>Отличается порядок (информационно, статус OK): {order_warning_count}</li>\n"
        + "        </ul>\n    </div>\n"
        + toggle_button_html
        + "\n"
        + ''.join(domain_cards_html)
        + "\n"
        + "    <script>\n"
        + "    function toggleOk() {\n"
        + "        var okCards = document.querySelectorAll('.ok-card');\n"
        + "        okCards.forEach(function(c) { c.classList.toggle('hidden'); });\n"
        + "        var btn = document.getElementById('toggle-ok-btn');\n"
        + "        if (btn) {\n"
        + "            if (btn.textContent.indexOf('Показать') !== -1) {\n"
        + f"                btn.textContent = 'Скрыть полностью OK ({fully_ok_count})';\n"
        + "            } else {\n"
        + f"                btn.textContent = 'Показать полностью OK ({fully_ok_count})';\n"
        + "            }\n"
        + "        }\n"
        + "    }\n"
        + "    </script>\n</body>\n</html>"
    )

    with open(report_filename, 'w', encoding='utf-8') as f:
        f.write(html_content)


def build_report_paths(args):
    timestamp = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
    reports_dir = 'reports'
    try:
        if not os.path.isdir(reports_dir):
            os.makedirs(reports_dir, exist_ok=True)
    except OSError:
        # fallback: continue and use cwd if cannot create directory
        reports_dir = '.'

    html_path = args.output or os.path.join(reports_dir, f"report_{timestamp}.html")
    json_path = args.json or os.path.join(reports_dir, f"report_{timestamp}.json")
    csv_path = args.summary_csv or os.path.join(reports_dir, f"summary_{timestamp}.csv")
    return html_path, json_path, csv_path


def generate_json_report(results, json_filename):
    content_issues_count = sum(1 for r in results if r['status'] != 'OK')
    order_warning_count = sum(1 for r in results if _domain_has_order_warning(r))

    payload = {
        'generated_at': datetime.now().isoformat(timespec='seconds'),
        'summary': {
            'total': len(results),
            'fully_ok': len(results) - content_issues_count - order_warning_count,
            'content_issues': content_issues_count,
            'order_warnings': order_warning_count,
        },
        'domains': results,
    }

    with open(json_filename, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def generate_csv_summary(results, csv_filename):
    fieldnames = [
        'domain',
        'app_id',
        'status',
        'category',
        'missing_count',
        'extra_count',
        'order_ok',
        'missing',
        'extra',
        'error',
    ]

    with open(csv_filename, 'w', encoding='utf-8', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()

        for res in results:
            base = {
                'domain': res['domain'],
                'app_id': res.get('app_id') or '',
                'status': res['status'],
            }

            if res['status'] == 'Error':
                writer.writerow({
                    **base,
                    'category': '',
                    'missing_count': '',
                    'extra_count': '',
                    'order_ok': '',
                    'missing': '',
                    'extra': '',
                    'error': res.get('error_message', ''),
                })
                continue

            for cat_res in res.get('category_results', []):
                writer.writerow({
                    **base,
                    'category': cat_res.get('name', ''),
                    'missing_count': len(cat_res.get('missing', [])),
                    'extra_count': len(cat_res.get('extra', [])),
                    'order_ok': cat_res.get('ordered_correctly', True),
                    'missing': '; '.join(cat_res.get('missing', [])),
                    'extra': '; '.join(cat_res.get('extra', [])),
                    'error': cat_res.get('error', '') or '',
                })


def compute_exit_code(results):
    if any(r['status'] in ('Issues', 'Error') for r in results):
        return EXIT_CONTENT_ISSUES
    return EXIT_OK


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description='Проверка офферов на лендингах (db.json) по эталону из CSV.',
    )
    parser.add_argument(
        '--csv',
        metavar='PATH',
        help='Путь к CSV с офферами (одиночный режим)',
    )
    parser.add_argument(
        '--domains',
        metavar='PATH',
        help='Путь к файлу со списком доменов (одиночный режим)',
    )
    parser.add_argument(
        '--all',
        action='store_true',
        help='Проверить все пары spreadsheets/*.csv + domain-list/*',
    )
    parser.add_argument(
        '--concurrency',
        type=int,
        default=DEFAULT_CONCURRENCY,
        metavar='N',
        help=f'Параллельных HTTP-запросов (по умолчанию {DEFAULT_CONCURRENCY})',
    )
    parser.add_argument(
        '--no-suggestions',
        action='store_true',
        help='Не предлагать новые записи в name_mappings.json',
    )
    parser.add_argument(
        '-o', '--output',
        metavar='PATH',
        help='Путь к HTML-отчёту (по умолчанию report_YYYY-MM-DD_HH-MM-SS.html)',
    )
    parser.add_argument(
        '--json',
        metavar='PATH',
        help='Путь к JSON-отчёту (по умолчанию report_*.json)',
    )
    parser.add_argument(
        '--summary-csv',
        metavar='PATH',
        help='Путь к CSV-сводке (по умолчанию summary_*.csv)',
    )
    parser.add_argument(
        '--categories',
        default=DEFAULT_CATEGORIES_FILE,
        metavar='PATH',
        help=f'Файл категорий (по умолчанию {DEFAULT_CATEGORIES_FILE})',
    )
    parser.add_argument(
        '--mapping',
        default=DEFAULT_MAPPING_FILE,
        metavar='PATH',
        help=f'Файл маппингов имён (по умолчанию {DEFAULT_MAPPING_FILE})',
    )
    parser.add_argument(
        '--csv-dir',
        default=DEFAULT_CSV_DIR,
        metavar='DIR',
        help=f'Каталог CSV (по умолчанию {DEFAULT_CSV_DIR})',
    )
    parser.add_argument(
        '--domain-dir',
        default=DEFAULT_DOMAIN_DIR,
        metavar='DIR',
        help=f'Каталог списков доменов (по умолчанию {DEFAULT_DOMAIN_DIR})',
    )
    parser.add_argument(
        '--strict-validation',
        action='store_true',
        help='Остановиться при ошибках валидации (не только предупреждения)',
    )

    args = parser.parse_args(argv)

    if args.concurrency < 1:
        parser.error('--concurrency должен быть >= 1')

    if args.all and (args.csv or args.domains):
        parser.error('Нельзя совмещать --all с --csv/--domains')

    if (args.csv or args.domains) and not (args.csv and args.domains) and not args.all:
        parser.error('В одиночном режиме укажите оба: --csv и --domains')

    return args


def resolve_file_pairs(args):
    csv_dir = args.csv_dir
    domain_dir = args.domain_dir

    if args.all:
        csv_files = list_available_files(csv_dir, '.csv')
        if not csv_files:
            print(colored(f"Нет CSV в {csv_dir}", "red"))
            return None, []
        pairs, unmatched = build_file_pairs(csv_dir, domain_dir, csv_files)
        for csv_file in unmatched:
            print(colored(
                f"Предупреждение: нет пары domain-list для {csv_file}",
                "yellow",
            ))
        return pairs, unmatched

    if args.csv and args.domains:
        pair_name = f"{os.path.basename(args.csv)} -> {os.path.basename(args.domains)}"
        return [{
            'csv': args.csv,
            'domain': args.domains,
            'pair_name': pair_name,
        }], []

    csv_files = list_available_files(csv_dir, '.csv')
    csv_selection = select_file(csv_files, csv_dir, "CSV", allow_all=True)
    if not csv_selection:
        return None, []

    if csv_selection == "ALL":
        pairs, unmatched = build_file_pairs(csv_dir, domain_dir, csv_files)
        for csv_file in unmatched:
            print(colored(
                f"Предупреждение: нет пары domain-list для {csv_file}",
                "yellow",
            ))
        return pairs, unmatched

    domain_file_path = select_file(
        list_available_files(domain_dir, ''),
        domain_dir,
        "domain list",
    )
    if not domain_file_path:
        return None, []

    return [{
        'csv': csv_selection,
        'domain': domain_file_path,
        'pair_name': (
            f"{os.path.basename(csv_selection)} -> "
            f"{os.path.basename(domain_file_path)}"
        ),
    }], []


async def async_main(argv=None):
    args = parse_args(argv)

    category_mapping = load_category_config(args.categories)
    if category_mapping is None:
        return EXIT_VALIDATION_OR_USAGE

    category_names = set(category_mapping.keys())
    name_mappings = load_mapping_file(args.mapping)

    file_pairs, _ = resolve_file_pairs(args)
    if file_pairs is None:
        return EXIT_VALIDATION_OR_USAGE
    if not file_pairs:
        print(colored("Нет пар CSV/domain для проверки.", "red"))
        return EXIT_VALIDATION_OR_USAGE

    runnable_pairs, warnings, errors = validate_before_run(
        file_pairs, category_names, name_mappings
    )
    print_validation_messages(warnings, errors)

    if args.strict_validation and errors:
        print(colored("Остановка: --strict-validation и есть ошибки.", "red"))
        return EXIT_VALIDATION_OR_USAGE

    if not runnable_pairs:
        print(colored("Нет валидных пар для запуска HTTP-проверки.", "red"))
        return EXIT_VALIDATION_OR_USAGE

    if len(runnable_pairs) < len(file_pairs):
        print(colored(
            f"К проверке допущено {len(runnable_pairs)} из {len(file_pairs)} пар.",
            "yellow",
        ))

    all_results = await run_checks(
        runnable_pairs,
        category_names,
        category_mapping,
        name_mappings,
        args.concurrency,
    )

    if not args.no_suggestions:
        suggest_mappings(all_results, name_mappings, args.mapping)

    html_path, json_path, csv_path = build_report_paths(args)

    generate_html_report(all_results, html_path)
    print(colored(f"HTML: {html_path}", "blue"))

    generate_json_report(all_results, json_path)
    print(colored(f"JSON: {json_path}", "blue"))

    generate_csv_summary(all_results, csv_path)
    print(colored(f"CSV:  {csv_path}", "blue"))

    exit_code = compute_exit_code(all_results)
    if exit_code == EXIT_OK:
        print(colored("\nПроверка завершена без расхождений по составу.", "green"))
    else:
        print(colored("\nЕсть домены со статусом Issues или Error.", "yellow"))

    return exit_code


def main():
    return asyncio.run(async_main())


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (KeyboardInterrupt, EOFError):
        print(colored("\nПрервано пользователем.", "yellow"))
        raise SystemExit(130) from None
