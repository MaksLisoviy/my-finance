"""Спільні функції обліку. Приватні дані — поза static/."""
import csv
import fcntl
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from beancount import loader
from beancount.core.data import Query
from beanquery.query import run_query

ROOT = Path(__file__).resolve().parent
OWNERS = {'ola': 'Оля', 'marko': 'Марко'}
QUERIES = ('Витрати за категоріями', 'Норма заощаджень', 'Фіксовані і змінні')
OPENING = {'ola': Decimal('4500'), 'marko': Decimal('2860.40')}


def private_dir():
    path = Path(os.environ.get('FINANCE_STORAGE', str(ROOT / 'instance'))).resolve()
    if any(path.is_relative_to((ROOT / name).resolve()) for name in ('static', 'data')):
        raise ValueError('FINANCE_STORAGE має бути поза static/ та data/')
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


@contextmanager
def locked():
    with (private_dir() / '.lock').open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def connect():
    db = sqlite3.connect(private_dir() / 'manual.sqlite3', timeout=30)
    db.row_factory = sqlite3.Row
    db.execute('''CREATE TABLE IF NOT EXISTS expenses (
        id TEXT PRIMARY KEY, owner TEXT NOT NULL, day TEXT NOT NULL,
        cents INTEGER NOT NULL CHECK(cents > 0), category TEXT NOT NULL,
        description TEXT NOT NULL, author TEXT NOT NULL,
        created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)''')
    return db


def rules():
    with (ROOT / 'rules.csv').open(encoding='utf-8-sig', newline='') as source:
        return list(csv.DictReader(source))


def categories():
    return sorted({r['рахунок'] for r in rules() if r['рахунок'].startswith('Expenses:')})


def classify(description, amount, owner="ola"):
    account = 'Income:Інше' if amount > 0 else 'Expenses:Без-Категорії'
    fixed = False
    for rule in rules():
        if rule['шаблон'].casefold() in description.casefold():
            if amount <= 0 or rule['рахунок'].startswith('Income:'):
                account = rule['рахунок']
                fixed = (rule['фіксована'].strip().casefold() == 'так'
                         and rule.get('фіксована-для', owner) in ('', owner))
            break
    return account, fixed


def quoted(value):
    return json.dumps(value, ensure_ascii=False)


def read_rows(owner):
    if owner not in OWNERS:
        raise ValueError('Невідомий власник')
    paths = ([ROOT / 'data' / 'statement.csv', *sorted((ROOT / 'data').glob('ola-*.csv'))]
             if owner == 'ola' else sorted((ROOT / 'data').glob('marko-*.csv')))
    files = []
    for path in paths:
        with path.open(encoding='utf-8-sig', newline='') as source:
            if owner == 'ola':
                rows = list(csv.DictReader(source))
            else:
                lines = list(csv.reader(source, delimiter=';'))
                metadata = {r[0]: r[1] for r in lines[:6] if len(r) == 2}
                if metadata.get('Waluta') != 'PLN':
                    raise ValueError('Підтримується лише PLN')
                if path.name == 'marko-statement.csv' and pln(metadata['Saldo początkowe']) != OPENING[owner]:
                    raise ValueError('Змінився початковий залишок Марка')
                start = next(i for i, r in enumerate(lines) if r and r[0] == 'Data operacji')
                rows = []
                for r in reversed(lines[start + 1:]):
                    if not r:
                        continue
                    if len(r) != 6:
                        raise ValueError('Неправильний рядок виписки Марка')
                    rows.append({'Completed Date': datetime.strptime(r[1], '%d.%m.%Y').isoformat(),
                                 'Description': r[3], 'Amount': str(pln(r[4])),
                                 'Balance': str(pln(r[5])), 'Fee': '0', 'State': 'COMPLETED', 'Currency': 'PLN'})
        if rows:
            files.append((rows[0]['Completed Date'], path.name, rows))
    seen, result = set(), []
    for _, name, rows in sorted(files):
        for row in rows:
            if row['State'] != 'COMPLETED' or row['Currency'] != 'PLN':
                raise ValueError(f'{name}: потрібна завершена операція PLN')
            key = (row['Completed Date'], row['Description'], Decimal(row['Amount']), Decimal(row['Balance']))
            if key not in seen:
                seen.add(key)
                result.append(row)
    if not result:
        raise ValueError(f'Немає виписки: {owner}')
    return result


def pln(value):
    number = Decimal(value.replace(' ', '').replace('\u00a0', '').replace(',', '.'))
    if not number.is_finite() or number != number.quantize(Decimal('.01')):
        raise ValueError('Сума повинна мати не більше двох десяткових знаків')
    return number


def bank_text(owner):
    lines, months = [], {}
    balance = OPENING[owner]
    previous = date(2026, 7, 31)
    rows = read_rows(owner)
    for row in rows:
        day = datetime.fromisoformat(row['Completed Date']).date()
        amount, fee, expected = (pln(row[k]) for k in ('Amount', 'Fee', 'Balance'))
        if day <= date(2026, 7, 31) or day < previous or fee:
            raise ValueError('Неправильна послідовність дат або ненульова комісія')
        previous = day
        balance += amount
        if balance != expected:
            raise ValueError(f'{owner} {day}: залишок {balance}, банк {expected}')
        account, fixed = classify(row['Description'], amount, owner)
        lines += [f'{day} * {quoted(row["Description"])}' + (' #fixed' if fixed else ''),
                  f'  Assets:Банк  {amount:.2f} PLN', f'  {account}  {-amount:.2f} PLN', '']
        months[(day.year, day.month)] = day, balance
    for month, (last, balance) in sorted(months.items()):
        y, m = month
        day = last + timedelta(days=1) if month == max(months) else date(y + (m == 12), m % 12 + 1, 1)
        lines += [f'{day} balance Assets:Банк {balance:.2f} PLN', '']
    return '\n'.join(lines), len(rows)


def base_text(owner):
    text = '\n'.join(line for line in (ROOT / 'main.beancount').read_text().splitlines()
                     if not line.startswith('include '))
    if owner == 'marko':
        text = text.replace('4500.00', '2860.40').replace('трекер Олі', 'трекер Марка')
    return text


def manual_text(rows, owner):
    lines = ['; Готівкові списання; фактичний залишок готівки невідомий.', '']
    for r in rows:
        if r['owner'] == owner:
            amount = Decimal(r['cents']) / 100
            lines += [f'{r["day"]} * {quoted(r["description"])} #cash',
                      f'  id: {quoted(r["id"])}',
                      f'  Expenses:{r["category"].removeprefix("Expenses:")}  {amount:.2f} PLN',
                      f'  Assets:Готівка  {-amount:.2f} PLN', '']
    return '\n'.join(lines)


def checked(text):
    entries, errors, options = loader.load_string(text)
    if errors:
        raise ValueError(str(errors))
    return entries, options


def bean_check(path):
    command = ROOT / '.venv' / 'bin' / 'bean-check'
    if not command.is_file():
        command = Path(sys.executable).with_name('bean-check')
    result = subprocess.run([str(command), str(path)], capture_output=True, text=True)
    if result.returncode or result.stdout.strip() or result.stderr.strip():
        raise ValueError(result.stdout + result.stderr or 'bean-check завершився з помилкою')


def atomic_write(path, text):
    path = Path(path)
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False) as f:
        temporary = Path(f.name)
        f.write(text)
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def snapshot(owner, bank, rows):
    return base_text(owner) + '\n' + bank + '\n' + manual_text(rows, owner)


def sync(rows=None):
    """Викликати під locked(); SQLite відновлює похідні файли після збою."""
    if rows is None:
        with connect() as db:
            rows = db.execute('SELECT * FROM expenses ORDER BY day, id').fetchall()
    prepared = []
    for owner in OWNERS:
        bank, count = bank_text(owner)
        text = snapshot(owner, bank, rows)
        checked(text)
        with tempfile.NamedTemporaryFile(mode='w', suffix='.beancount', dir=private_dir()) as f:
            f.write(text)
            f.flush()
            bean_check(f.name)
        prepared.append((owner, bank, count, text))
    for owner, bank, count, text in prepared:
        atomic_write(private_dir() / f'{owner}.beancount', text)
        atomic_write(private_dir() / f'{owner}-manual.beancount', manual_text(rows, owner))
        if owner == 'ola':
            atomic_write(ROOT / 'transactions.beancount', bank)
            atomic_write(ROOT / 'ola-manual.beancount', manual_text(rows, owner))
        print(f'{OWNERS[owner]}: {count} банківських операцій; bean-check OK')


def results(owner, baseline=False):
    if baseline:
        bank, _ = bank_text(owner)
        entries, options = checked(snapshot(owner, bank, []))
    else:
        entries, errors, options = loader.load_file(str(private_dir() / f'{owner}.beancount'))
        if errors:
            raise ValueError(str(errors))
    queries = {e.name: e.query_string for e in entries if isinstance(e, Query)}
    output = []
    for name in QUERIES:
        columns, rows = run_query(entries, options, queries[name])
        output.append([dict(zip((c.name for c in columns), row)) for row in rows])
    for month in output[1]:
        key = month['month']
        total = sum((r['expenses'] for r in output[0] if r['month'] == key), Decimal(0))
        fixed = next(r for r in output[2] if r['month'] == key)
        if total != month['expenses'] or fixed['fixed'] + fixed['variable'] != total:
            raise ValueError('Не збігаються підсумки звіту')
    return output


def add_cash(owner, day, amount, category, description, author, identifier):
    if owner not in OWNERS or category not in categories():
        raise ValueError('Невідомий власник або категорія')
    parsed = date.fromisoformat(day)
    if parsed < date(2026, 8, 1) or parsed > date.today():
        raise ValueError('Дата: від 01.08.2026 до сьогодні')
    try:
        value = pln(amount)
    except (InvalidOperation, ValueError):
        raise ValueError('Вкажіть коректну суму PLN') from None
    if not 0 < value <= Decimal('1000000'):
        raise ValueError('Сума має бути більшою за нуль і не перевищувати 1 000 000 PLN')
    description = description.strip()
    if not description or len(description) > 300 or any(ord(c) < 32 for c in description):
        raise ValueError('Опис: 1–300 символів без керівних символів')
    if not identifier or len(identifier) > 100:
        raise ValueError('Неправильний ідентифікатор форми')
    record = dict(id=identifier, owner=owner, day=day, cents=int(value * 100), category=category,
                  description=description, author=author)
    with locked(), connect() as db:
        db.execute('BEGIN IMMEDIATE')
        existing = db.execute('SELECT * FROM expenses WHERE id=?', (identifier,)).fetchone()
        if existing:
            if any(existing[k] != v for k, v in record.items()):
                raise ValueError('Форму вже використано для іншої операції')
            sync()
            return
        rows = list(db.execute('SELECT * FROM expenses ORDER BY day, id')) + [record]
        for candidate_owner in OWNERS:
            bank, _ = bank_text(candidate_owner)
            text = snapshot(candidate_owner, bank, rows)
            checked(text)
            with tempfile.NamedTemporaryFile(mode='w', suffix='.beancount', dir=private_dir()) as f:
                f.write(text)
                f.flush()
                bean_check(f.name)
        db.execute('INSERT INTO expenses(id,owner,day,cents,category,description,author) VALUES (:id,:owner,:day,:cents,:category,:description,:author)', record)
        db.commit()
        sync(rows)
