"""Захищений вебінтерфейс. Запуск: uv run flask --app app:create_app run -p 5050."""
import hmac
import os
import secrets
import sqlite3
import time
from datetime import date, timedelta
from decimal import Decimal

from flask import Flask, abort, flash, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from finance import OWNERS, add_cash, categories, connect, locked, results, sync
from report import money, percent


def create_app(test_config=None):
    app = Flask(__name__)
    local = os.environ.get('FINANCE_LOCAL_HTTP') == '1'
    app.config.update(
        SECRET_KEY=os.environ.get('SECRET_KEY'),
        USERS={name: os.environ.get(name.upper() + '_PASSWORD_HASH') for name in OWNERS},
        SESSION_COOKIE_SECURE=not local, SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE='Lax', PERMANENT_SESSION_LIFETIME=timedelta(hours=2),
        MAX_CONTENT_LENGTH=16 * 1024, LOCAL_HTTP=local,
        TRUSTED_HOSTS=os.environ.get('FINANCE_HOSTS', 'localhost,127.0.0.1').split(','),
    )
    if test_config:
        app.config.update(test_config)
    if not app.secret_key or len(app.secret_key) < 32 or not all(app.config['USERS'].values()):
        raise RuntimeError('Потрібні SECRET_KEY (32+ символи), OLA_PASSWORD_HASH і MARKO_PASSWORD_HASH')
    with locked():
        sync()
    dummy_hash = generate_password_hash(secrets.token_urlsafe(32))
    app.jinja_env.filters['money'] = money
    app.jinja_env.filters['cash_money'] = lambda cents: money(Decimal(cents) / 100)
    app.jinja_env.filters['percent'] = percent

    def csrf():
        if 'csrf' not in session:
            session['csrf'] = secrets.token_urlsafe(32)
        return session['csrf']

    app.jinja_env.globals['csrf_token'] = csrf

    @app.before_request
    def guard():
        if not app.config['LOCAL_HTTP'] and not request.is_secure:
            # PythonAnywhere встановлює wsgi.url_scheme; довільним forwarded-заголовкам не довіряємо.
            return redirect(request.url.replace('http://', 'https://', 1), code=308)
        if request.method == 'POST':
            token = request.form.get('csrf', '')
            if not session.get('csrf') or not hmac.compare_digest(token, session['csrf']):
                abort(400)
        if request.endpoint not in ('login', 'static') and session.get('user') not in app.config['USERS']:
            return redirect(url_for('login'))

    @app.after_request
    def headers(response):
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Content-Security-Policy'] = "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        if not app.config['LOCAL_HTTP']:
            response.headers['Strict-Transport-Security'] = 'max-age=31536000'
        return response

    def limited():
        # Постійний ліміт за IP та загальний ліміт; працює й після перезапуску worker.
        now = int(time.time())
        with connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS login_attempts (ip TEXT, moment INTEGER)')
            db.execute('DELETE FROM login_attempts WHERE moment < ?', (now - 900,))
            ip = request.remote_addr or 'unknown'
            individual = db.execute('SELECT count(*) FROM login_attempts WHERE ip=?', (ip,)).fetchone()[0]
            total = db.execute('SELECT count(*) FROM login_attempts').fetchone()[0]
            if individual >= 10 or total >= 100:
                return True
            db.execute('INSERT INTO login_attempts VALUES (?,?)', (ip, now))
        return False

    @app.route('/login', methods=['GET', 'POST'])
    def login():
        error = None
        if request.method == 'POST':
            if limited():
                return render_template('login.html', error='Забагато спроб. Спробуйте через 15 хвилин.'), 429
            name = request.form.get('username', '')
            stored = app.config['USERS'].get(name, dummy_hash)
            valid = check_password_hash(stored, request.form.get('password', ''))
            if valid and name in app.config['USERS']:
                session.clear()
                session['user'] = name
                session.permanent = True
                return redirect(url_for('dashboard'))
            error = 'Неправильний логін або пароль.'
        return render_template('login.html', error=error)

    @app.post('/logout')
    def logout():
        session.clear()
        return redirect(url_for('login'))

    @app.get('/')
    def dashboard():
        owner = request.args.get('owner', session.get('user', 'ola'))
        if owner not in OWNERS:
            abort(400)
        # Блокування також захищає читання snapshot під час імпорту/збереження.
        with locked():
            sync()
            data = results(owner)
            baseline = results(owner, baseline=True)
            with connect() as db:
                cash = db.execute('SELECT * FROM expenses WHERE owner=? ORDER BY day DESC,id', (owner,)).fetchall()
        months = sorted({r['month'].strftime('%Y-%m') for r in data[1]})
        month = request.args.get('month', months[-1])
        if month not in months:
            abort(400)
        selected = lambda rows: next(r for r in rows if r['month'].strftime('%Y-%m') == month)
        summary, breakdown = selected(data[1]), selected(data[2])
        controls = selected(baseline[1])
        items = [r for r in data[0] if r['month'].strftime('%Y-%m') == month]
        cash = [r for r in cash if r['day'].startswith(month)]
        coverage = ('Банківські дані до 15 жовтня 2026 року' if month == '2026-10'
                    else 'Повний банківський місяць' if month in ('2026-08', '2026-09')
                    else 'Банківської виписки за цей місяць немає')
        return render_template('dashboard.html', owners=OWNERS, owner=owner, months=months, month=month,
                               summary=summary, breakdown=breakdown, controls=controls, items=items,
                               coverage=coverage, cash=cash, pending=owner == 'marko')

    @app.route('/expenses/new', methods=['GET', 'POST'])
    def expense():
        error = None
        if request.method == 'POST':
            try:
                add_cash(request.form.get('owner'), request.form.get('day', ''),
                         request.form.get('amount', ''), request.form.get('category'),
                         request.form.get('description', ''), session['user'],
                         request.form.get('identifier', ''))
            except (ValueError, TypeError) as exc:
                error = str(exc)
            else:
                flash('Готівкову витрату збережено.')
                return redirect(url_for('dashboard', owner=request.form['owner'], month=request.form['day'][:7]))
        return render_template('expense.html', owners=OWNERS, categories=categories(), error=error,
                               identifier=request.form.get('identifier') or secrets.token_urlsafe(24),
                               today=date.today().isoformat()), 400 if error else 200

    @app.errorhandler(500)
    def failed(error):
        return render_template('error.html'), 500

    return app
