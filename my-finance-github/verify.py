"""uv run python verify.py [--baseline | --expected /private/assignment.json]."""
import argparse
import json
import tempfile
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from finance import (OWNERS, ROOT, bank_text, bean_check, locked, read_rows,
                     results, snapshot, sync, private_dir)


def compare(expected, actual):
    for owner in OWNERS:
        if owner not in expected['owners']:
            raise ValueError(f'Немає еталона для {owner}')
        months = expected['owners'][owner]
        if set(months) != set(actual[owner]):
            raise ValueError(f'{owner}: різні набори місяців')
        for month, fields in months.items():
            for field, target in fields.items():
                value = actual[owner][month].get(field)
                target = None if target is None else Decimal(str(target))
                print(f'{OWNERS[owner]} {month} {field}: факт={value}; очікується={target}')
                if value != target:
                    raise ValueError(f'Розбіжність: {owner} {month} {field}')


def actual_values(owner):
    data = results(owner, baseline=True)
    fixed = {r['month']: r for r in data[2]}
    months = {}
    for row in data[1]:
        key = row['month'].strftime('%Y-%m')
        months[key] = {k: row[k] for k in ('income','expenses','saved','savings_percent')}
        months[key].update({k: fixed[row['month']][k] for k in ('fixed','variable')})
    counts = {}
    for row in read_rows(owner):
        key = datetime.fromisoformat(row['Completed Date']).strftime('%Y-%m')
        counts[key] = counts.get(key, 0) + 1
        months[key]['closing'] = Decimal(row['Balance'])
        months[key]['count'] = Decimal(counts[key])
    return months


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', action='store_true', help='Лише наявна база CSV, без підтвердження ДЗ')
    parser.add_argument('--expected', type=Path, default=ROOT / 'checks' / 'assignment.json')
    args = parser.parse_args()
    try:
        with locked():
            sync()
            bean_check(ROOT / 'main.beancount')
            actual = {}
            for owner in OWNERS:
                first, _ = bank_text(owner)
                second, _ = bank_text(owner)
                if first != second:
                    raise ValueError('Повторний імпорт відрізняється')
                with tempfile.NamedTemporaryFile(mode='w', suffix='.beancount') as f:
                    f.write(snapshot(owner, first, []))
                    f.flush()
                    bean_check(f.name)
                bean_check(private_dir() / f'{owner}.beancount')
                results(owner)
                actual[owner] = actual_values(owner)
            compare(json.loads((ROOT / 'checks' / 'baseline.json').read_text()), actual)
            print('bean-check: без помилок і повідомлень. Базові цифри CSV збігаються.')
            if not args.baseline:
                if not args.expected.is_file():
                    raise ValueError('Контрольні дані умови ще не надані. Для перевірки бази CSV: --baseline')
                expected = json.loads(args.expected.read_text())
                if expected.get('confirmed') is not True or not expected.get('source'):
                    raise ValueError('Еталон умови не підтверджено')
                required = {'income','expenses','saved','savings_percent','fixed','variable','closing','count','categories'}
                for owner in OWNERS:
                    for fields in expected['owners'][owner].values():
                        if not required <= fields.keys():
                            raise ValueError('Еталон має містити всі суми та categories')
                # Категорії звіряємо окремо: назва рахунку -> сума PLN.
                for owner in OWNERS:
                    category_rows = results(owner, baseline=True)[0]
                    for month, fields in expected['owners'][owner].items():
                        found = {r['category']: r['expenses'] for r in category_rows if r['month'].strftime('%Y-%m') == month}
                        target = {k: Decimal(v) for k,v in fields['categories'].items()}
                        if found != target:
                            raise ValueError(f'{owner} {month}: категорії не збігаються')
                        fields.pop('categories')
                compare(expected, actual)
                print('Контрольні дані умови підтверджені.')
        return 0
    except (ValueError, OSError, KeyError) as exc:
        print(f'ПЕРЕВІРКУ НЕ ПРОЙДЕНО: {exc}')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
