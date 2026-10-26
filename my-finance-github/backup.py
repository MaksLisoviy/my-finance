"""uv run python backup.py /private/path/backup.sqlite3 — узгоджена копія SQLite."""
import argparse
import os
import sqlite3
from pathlib import Path
from finance import connect, locked


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination', type=Path)
    args = parser.parse_args()
    # Не перезаписувати наявну копію та створювати з приватними правами від початку.
    fd = os.open(args.destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    with locked(), connect() as source, sqlite3.connect(args.destination) as target:
        source.backup(target)
    print('Резервну копію створено. Зберігайте її приватно.')


if __name__ == '__main__':
    main()
