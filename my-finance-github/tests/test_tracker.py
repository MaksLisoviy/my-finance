import csv
import importlib.util
import json
import os
import re
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from werkzeug.security import generate_password_hash

import finance
from app import create_app
from verify import actual_values, compare


class TrackerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.hash = generate_password_hash('test-only-long-password')

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {'FINANCE_STORAGE': self.tmp.name})
        self.env.start()
        root = Path(self.tmp.name) / 'project'
        (root / 'data').mkdir(parents=True)
        for name in ('main.beancount', 'rules.csv'):
            (root / name).write_bytes((finance.ROOT / name).read_bytes())
        for name in ('statement.csv', 'marko-statement.csv'):
            (root / 'data' / name).write_bytes((finance.ROOT / 'data' / name).read_bytes())
        (root / 'checks').mkdir()
        (root / 'checks/baseline.json').write_bytes((finance.ROOT / 'checks/baseline.json').read_bytes())
        self.root_patch = patch.object(finance, 'ROOT', root)
        self.root_patch.start()
        self.app = create_app({'TESTING': True, 'SECRET_KEY': 'test-only-secret-' * 4,
                               'USERS': {'ola': self.hash, 'marko': self.hash},
                               'LOCAL_HTTP': True, 'SESSION_COOKIE_SECURE': False})
        self.client = self.app.test_client()

    def tearDown(self):
        self.root_patch.stop()
        self.env.stop()
        self.tmp.cleanup()

    def token(self, url):
        response = self.client.get(url)
        return re.search(rb'name="csrf" value="([^"]+)"', response.data)[1].decode()

    def login(self, name='ola'):
        token = self.token('/login')
        return self.client.post('/login', data={'csrf': token, 'username': name, 'password': 'test-only-long-password'})

    def test_baseline_and_zero_income(self):
        expected = json.loads((finance.ROOT / 'checks/baseline.json').read_text())
        compare(expected, {owner: actual_values(owner) for owner in finance.OWNERS})
        self.assertIsNone(actual_values('marko')['2026-10']['savings_percent'])
        self.assertEqual(actual_values('marko')['2026-10']['saved'], Decimal('-1640.35'))

    def test_confirmed_fixed_costs_and_ukrainian_labels(self):
        # Переліки власників різні навіть для того самого продавця.
        for description in ('ORANGE POLSKA S.A. ABONAMENT GSM',
                            'ORANGE POLSKA S.A. ŚWIATŁOWÓD', 'PGE OBRÓT',
                            'ZDROFIT', 'YOUTUBE PREMIUM'):
            self.assertTrue(finance.classify(description, Decimal('-10'), 'marko')[1])
            self.assertFalse(finance.classify(description, Decimal('-10'), 'ola')[1])
        for description in ('Rent', 'ZTM', 'Netflix', 'Spotify', 'Claude.ai', 'Play'):
            self.assertFalse(finance.classify(description, Decimal('-10'), 'marko')[1])
            self.assertTrue(finance.classify(description, Decimal('-10'), 'ola')[1])
        self.login()
        page = self.client.get('/?owner=marko').get_data(as_text=True)
        self.assertIn('Фіксовані витрати', page)
        self.assertIn('Змінні витрати', page)
        self.assertNotIn('Fixed Expenses', page)
        self.assertNotIn('Variable Expenses', page)
        self.assertNotIn('правила фіксованих витрат ще не підтверджені', page)

    def test_private_routes_and_files(self):
        for url in ('/', '/?owner=marko', '/expenses/new', '/data/statement.csv', '/instance/manual.sqlite3', '/report.html', '/main.beancount', '/.env', '/api/reports', '/static/../data/statement.csv'):
            response = self.client.get(url)
            self.assertIn(response.status_code, (302, 404), url)
            self.assertNotIn(b'9800', response.data)
        self.assertEqual(self.client.post('/expenses/new').status_code, 400)

    def test_login_logout_csrf_and_shared_access(self):
        self.assertEqual(self.client.post('/login', data={}).status_code, 400)
        token = self.token('/login')
        self.assertEqual(self.client.post('/login', data={'csrf':token,'username':'ola','password':'wrong'}).status_code, 200)
        self.assertEqual(self.login().status_code, 302)
        for owner in finance.OWNERS:
            response = self.client.get('/?owner=' + owner + '&month=2026-10')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertEqual(self.client.get('/?owner=attacker').status_code, 400)
        token = self.token('/expenses/new')
        self.assertEqual(self.client.post('/logout', data={'csrf':token}).status_code, 302)
        self.assertEqual(self.client.get('/').status_code, 302)

    def test_cash_survives_import_and_restart_without_bank_change(self):
        self.login()
        response = self.client.get('/expenses/new')
        token = re.search(rb'name="csrf" value="([^"]+)"', response.data)[1].decode()
        identifier = re.search(rb'name="identifier" value="([^"]+)"', response.data)[1].decode()
        payload = dict(csrf=token,identifier=identifier,owner='marko',day='2026-09-12',amount='25,50',
                       category='Expenses:Продукти',description='<script>alert(1)</script>')
        before = actual_values('marko')['2026-09']['closing']
        for _ in range(2):
            self.assertEqual(self.client.post('/expenses/new', data=payload).status_code, 302)
        with finance.locked():
            finance.sync()
        with finance.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM expenses').fetchone()[0], 1)
        data = finance.results('marko')[1]
        september = next(r for r in data if r['month'].month == 9)
        self.assertEqual(september['expenses'], Decimal('2916.22'))
        self.assertEqual(actual_values('marko')['2026-09']['closing'], before)
        self.assertEqual(actual_values('ola')['2026-09']['expenses'], Decimal('6090.91'))
        finance.bean_check(finance.private_dir() / 'marko.beancount')
        # Відновлення з SQLite після втрати похідного файлу.
        (finance.private_dir() / 'marko.beancount').unlink()
        with finance.locked():
            finance.sync()
        page = self.client.get('/?owner=marko&month=2026-09')
        self.assertIn(b'&lt;script&gt;', page.data)
        self.assertNotIn(b'<script>alert', page.data)

    def test_invalid_cash_is_not_saved(self):
        for amount in ('-1','0','1.001','NaN','Infinity','1000001'):
            with self.assertRaises(ValueError):
                finance.add_cash('ola','2026-09-01',amount,'Expenses:Продукти','Test','ola','id')
        with self.assertRaises(ValueError):
            finance.add_cash('ola','2026-09-01','1','Assets:Банк','Test','ola','id')
        with finance.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM expenses').fetchone()[0], 0)

    def test_bad_bank_data_does_not_replace_snapshot(self):
        destination = finance.private_dir() / 'ola.beancount'
        previous = destination.read_bytes()
        original = finance.read_rows
        def corrupt(owner):
            rows = [dict(r) for r in original(owner)]
            if owner == 'marko':
                rows[0]['Balance'] = '999'
            return rows
        with patch.object(finance, 'read_rows', side_effect=corrupt):
            with self.assertRaises(ValueError):
                with finance.locked():
                    finance.sync()
        self.assertEqual(destination.read_bytes(), previous)

    def test_duplicate_files_and_owner_isolation(self):
        original = finance.ROOT
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'data').mkdir()
            for name in ('statement.csv','ola-duplicate.csv'):
                (root / 'data' / name).write_bytes((original / 'data/statement.csv').read_bytes())
            (root / 'data/marko-statement.csv').write_bytes((original / 'data/marko-statement.csv').read_bytes())
            with patch.object(finance, 'ROOT', root):
                self.assertEqual(len(finance.read_rows('ola')), 142)
                self.assertEqual(len(finance.read_rows('marko')), 109)

    def test_wsgi_uses_project_checker(self):
        command = finance.ROOT / '.venv/bin/bean-check'
        command.parent.mkdir(parents=True)
        command.touch()
        from subprocess import CompletedProcess
        with patch.object(finance.sys, 'executable', '/usr/local/bin/python'), patch.object(
                finance.subprocess, 'run', return_value=CompletedProcess([], 0, '', '')) as run:
            finance.bean_check('candidate.beancount')
        self.assertEqual(run.call_args.args[0], [str(command), 'candidate.beancount'])

    def test_rejected_candidate_does_not_commit(self):
        with patch.object(finance, 'bean_check', side_effect=ValueError('invalid candidate')):
            with self.assertRaises(ValueError):
                finance.add_cash('ola','2026-09-01','12.34','Expenses:Продукти','Test','ola','reject')
        with finance.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM expenses').fetchone()[0], 0)

    def test_committed_cash_recovers_after_publication_failure(self):
        with patch.object(finance, 'sync', side_effect=OSError('simulated disk failure')):
            with self.assertRaises(OSError):
                finance.add_cash('ola','2026-09-01','12.34','Expenses:Продукти','Test','ola','recover')
        with finance.locked():
            finance.sync()
        finance.add_cash('ola','2026-09-01','12.34','Expenses:Продукти','Test','ola','recover')
        with finance.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM expenses').fetchone()[0], 1)
        september = next(r for r in finance.results('ola')[1] if r['month'].month == 9)
        self.assertEqual(september['expenses'], Decimal('6103.25'))

    def test_https_secure_cookies_and_limit(self):
        self.app.config.update(LOCAL_HTTP=False, SESSION_COOKIE_SECURE=True)
        self.assertEqual(self.client.get('/login').status_code, 308)
        page = self.client.get('/login', base_url='https://localhost')
        cookie = page.headers['Set-Cookie']
        for flag in ('Secure','HttpOnly','SameSite=Lax'):
            self.assertIn(flag,cookie)
        self.assertIn('max-age',page.headers['Strict-Transport-Security'])
        self.app.config['LOCAL_HTTP'] = True
        self.app.config['SESSION_COOKIE_SECURE'] = False
        token = self.token('/login')
        for _ in range(10):
            self.client.post('/login',data={'csrf':token,'username':'x','password':'x'})
        self.assertEqual(self.client.post('/login',data={'csrf':token,'username':'x','password':'x'}).status_code,429)


if __name__ == '__main__':
    unittest.main()
