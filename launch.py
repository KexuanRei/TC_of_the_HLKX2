"""Find a compatible interpreter, then launch the desktop without a console."""
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
CHECK = 'import tkinter, openpyxl, lxml, playwright, win32com.client'


def main():
    candidates = [ROOT / '.venv' / 'Scripts' / 'python.exe', Path(sys.executable),
                  Path('C:/Users/Kexuan/Documents/ChatGPT/FGO2.0/wps_mysql_sync/.venv/Scripts/python.exe')]
    for candidate in candidates:
        if not candidate.exists():
            continue
        try:
            result = subprocess.run([str(candidate), '-c', CHECK], capture_output=True, timeout=25,
                                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except (OSError, subprocess.TimeoutExpired):
            continue
        if result.returncode == 0:
            pythonw = candidate.with_name('pythonw.exe')
            (ROOT / 'logs').mkdir(exist_ok=True)
            with (ROOT / 'logs' / 'desktop.log').open('a', encoding='utf-8') as log:
                subprocess.Popen([str(pythonw if pythonw.exists() else candidate), str(ROOT / 'app.py')],
                                 cwd=ROOT, stdout=log, stderr=log,
                                 creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            return 0
    print('没有找到依赖完整的 Python 环境，请运行 install_dependencies.bat 后重试。')
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
