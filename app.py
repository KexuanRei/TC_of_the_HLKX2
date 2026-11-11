"""Entry point: HTML front-end host (local JSON API + dedicated Edge app window).

The interface is rendered with HTML/CSS in a Chromium app-mode window; all
business logic stays in the unchanged queue/backend/engine modules.
"""
import json
import os
import tkinter as tk
from pathlib import Path
from tkinter import messagebox

from catalog import ROOT
from instance_lock import acquire_instance_lock
from ui_web import run_workbench, restore_window, activate_window


def startup_message(title, message, error=False):
    root = tk.Tk()
    root.withdraw()
    try:
        (messagebox.showerror if error else messagebox.showinfo)(title, message, parent=root)
    finally:
        root.destroy()


def main():
    if os.name == 'nt':
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('Local.UnifiedTaskWorkbench')
    try:
        lockfile = acquire_instance_lock(ROOT / 'state' / 'desktop.lock')
    except OSError as exc:
        startup_message('无法启动识质存在',
                        '无法访问程序的状态文件，请确认已完整解压到有写入权限的文件夹。\n\n' + str(exc), error=True)
        return
    if lockfile is None:
        if os.name == 'nt':
            try:
                if restore_window() or activate_window():
                    return
            except OSError:
                pass
        startup_message('识质存在已在运行',
                        '同一文件夹中的程序已在运行。请使用已有窗口；如果正在启动或关闭，请稍后重试。')
        return
    # Closing the handle releases ownership even if run_workbench raises.
    with lockfile:
        run_workbench()


if __name__ == '__main__':
    import sys
    if len(sys.argv) >= 3 and sys.argv[1] == '--check-ui':
        try:
            from runtime_check import check_ui
            check_ui(sys.argv[2], preview='--preview' in sys.argv[3:])
        except Exception:
            import traceback
            Path(sys.argv[2]).write_text(json.dumps({'ok': False, 'error': traceback.format_exc()},
                                                    ensure_ascii=False, indent=2), encoding='utf-8')
            raise SystemExit(1)
    else:
        main()
