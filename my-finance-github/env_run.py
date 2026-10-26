"""Передає секрети з приватного JSON у середовище команди, не друкуючи їх."""
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

if __name__ == '__main__':
    if len(sys.argv) < 3:
        raise SystemExit('env_run.py /private/env.json python verify.py --baseline')
    path = Path(sys.argv[1])
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise SystemExit('Приватний файл має бути доступний лише власнику (chmod 600)')
    values = json.loads(path.read_text())
    if not isinstance(values, dict) or not all(isinstance(v, str) for v in values.values()):
        raise SystemExit('Потрібен JSON-об’єкт зі строковими значеннями')
    environment = os.environ | values
    command = sys.argv[2:]
    if command[0] == 'python':
        command[0] = sys.executable
    raise SystemExit(subprocess.call(command, env=environment))
