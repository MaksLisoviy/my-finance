"""Пароль вводиться приховано; у виводі лише хеш для змінної середовища."""
from getpass import getpass
from werkzeug.security import generate_password_hash

if __name__ == '__main__':
    first = getpass('Пароль (12+ символів): ')
    if len(first) < 12 or first != getpass('Повторіть пароль: '):
        raise SystemExit('Паролі не збігаються або надто короткі')
    print(generate_password_hash(first, method='scrypt'))
