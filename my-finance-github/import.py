"""Імпортує обидві виписки; не змінює data/ та ручні операції."""
from finance import locked, sync


def main():
    with locked():
        sync()


if __name__ == '__main__':
    main()
