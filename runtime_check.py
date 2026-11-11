"""Offline diagnostics for the portable build; never run a cloud operation."""
import importlib
import json
from pathlib import Path
import sys


def check_engine(engine):
    from catalog import ROOT
    root = ROOT / 'engines' / engine
    sys.path.insert(0, str(root))
    if engine == 'modern':
        modules = ['web_app', 'edge_copy', 'cloud_file_replace', 'values_workbook',
                   'sync_runner', 'table_c_download', 'full_upload', 'cell_sync', 'overwatch']
    else:
        modules = ['web_app', 'edge_copy', 'upload_runner', 'batch_copy',
                   'excel_clipboard', 'upload_format', 'format_reference']
    for name in modules:
        importlib.import_module(name)
    import pythoncom
    import win32clipboard
    import win32com.client
    pythoncom.CoInitialize()
    pythoncom.CoUninitialize()
    from io import BytesIO
    from openpyxl import Workbook, load_workbook
    book = Workbook()
    book.active['A1'] = '离线检查'
    data = BytesIO()
    book.save(data)
    data.seek(0)
    assert load_workbook(data).active['A1'].value == '离线检查'
    config = json.loads((root / 'config.json').read_text(encoding='utf-8-sig'))
    importlib.import_module('web_app').validate_config(config)
    from playwright.sync_api import sync_playwright
    import tempfile
    with tempfile.TemporaryDirectory() as profile, sync_playwright() as playwright:
        # Match the engines' persistent-profile startup without opening a cloud
        # document or reusing the user's logged-in browser profile.
        browser = playwright.chromium.launch_persistent_context(
            profile, channel='msedge', headless=True, accept_downloads=(engine == 'modern'))
        try:
            page = browser.new_page()
            page.set_content('<html><body><p>OFFLINE-PACKAGE-CHECK</p></body></html>')
            assert page.locator('p').inner_text() == 'OFFLINE-PACKAGE-CHECK'
        finally:
            browser.close()
    return dict(ok=True, engine=engine, frozen=bool(getattr(sys, 'frozen', False)),
                imported_modules=modules, excel_library=True, edge_offline=True,
                cloud_operations=0)


def check_ui(report, preview=False):
    import copy
    import tkinter as tk
    import tempfile
    import threading
    import time
    from backend import Backend
    from catalog import ROOT
    from local_upload import default_config
    from instance_lock import acquire_instance_lock
    from ui_web import Workbench, WorkbenchServer

    # File picker and duplicate-instance message still use Tk in the HTML host.
    dialog_root = tk.Tk()
    dialog_root.withdraw()
    dialog_root.update_idletasks()
    dialog_root.destroy()

    class Fake:
        def __init__(self):
            self.calls = []

        def validate(self, engine, config, action=None):
            return copy.deepcopy(config)

        def execute(self, task, stop, progress):
            self.calls.append(task['action'])
            return '离线界面检查完成'

        def close(self):
            pass

    with tempfile.TemporaryDirectory() as temp:
        lock_path = Path(temp) / 'desktop.lock'
        with acquire_instance_lock(lock_path):
            assert acquire_instance_lock(lock_path) is None
        with acquire_instance_lock(lock_path) as handle:
            assert not handle.closed
        backend = Fake()
        workbench = Workbench(backend, Path(temp) / 'queue.json')
        server = WorkbenchServer(workbench)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = server.url
        page_errors = []
        try:
            # Exercise the exact frozen GUI -> frozen worker command path.
            real = Backend()
            try:
                for engine in ('modern', 'legacy'):
                    real.validate(engine, workbench.templates[engine])
                    state = real.engine(engine).call('state')
                    assert not state['busy'] and isinstance(state['message'], str)
                local = default_config()
                local['source']['local_file'] = str(Path(temp) / '表A1.xlsx')
                local['upload'] = {'share_url': 'https://www.kdocs.cn/l/abc123', 'cloud_filename': '目标.xlsx'}
                real.validate('modern', local, action='local_upload')
                download = {'source': {'share_url': 'https://www.kdocs.cn/l/source123', 'sheet_name': 'Sheet1'},
                            'monitor': {'enabled': False}}
                assert real.validate('modern', download, action='download_c') == download
            finally:
                real.close()

            # Bridge asserts (equivalent of the former Tk dialog asserts).
            descriptor = workbench.config_payload('modern', 'local_upload')
            fields = [field for section in descriptor['sections'] for field in section['fields']]
            assert len(fields) == 4, fields
            assert descriptor['commit_label'] == '创建任务 → 加入队列'
            assert workbench.controller.settings['interval'] == 15
            assert workbench.controller.settings['cycles'] == 0

            from playwright.sync_api import sync_playwright
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(channel='msedge', headless=True)
                try:
                    page = browser.new_page(viewport={'width': 1240, 'height': 850})
                    page.on('pageerror', lambda exc: page_errors.append(str(exc)))
                    page.goto(url, wait_until='load', timeout=60000)
                    assert page.title() == '识质存在'
                    page.wait_for_selector('.fn-btn', timeout=30000)
                    assert page.locator('.fn-btn').count() == 6
                    assert page.locator('#brand').inner_text() == '表格同步工作台'
                    assert page.locator('text=排队窗口').count() >= 1
                    assert '00 项任务' in page.locator('#count').inner_text()
                    page.get_by_role('button', name='下载表C', exact=False).click()
                    page.wait_for_selector('#cfg-modal')
                    assert page.locator('#cfg-sections .sec-tab').count() == 2
                    page.fill('#cfg-f-share_url', download['source']['share_url'])
                    page.fill('#cfg-f-sheet_name', download['source']['sheet_name'])
                    page.click('#cfg-commit')
                    page.wait_for_selector('#cfg-modal', state='hidden')
                    assert 'upload' not in workbench.controller.snapshot()[0]['config']
                    # Create through the real form, including edits across pages.
                    page.click('.fn-btn.accent')
                    page.wait_for_selector('#cfg-title')
                    assert '配置功能' in page.locator('#cfg-title').inner_text()
                    page.fill('#cfg-f-local_file', local['source']['local_file'])
                    page.locator('#cfg-sections').get_by_role('button', name='上传设置', exact=True).click()
                    page.fill('#cfg-f-share_url', local['upload']['share_url'])
                    page.fill('#cfg-f-cloud_filename', local['upload']['cloud_filename'])
                    page.click('#cfg-commit')
                    page.wait_for_selector('#cfg-modal', state='hidden')
                    assert workbench.controller.snapshot()[1]['config']['source'] == local['source']
                    page.get_by_role('button', name='表A → 表A1 复制', exact=False).click()
                    page.wait_for_selector('#cfg-modal')
                    page.click('#cfg-commit')
                    page.wait_for_selector('#cfg-modal', state='hidden')
                    page.wait_for_function("document.getElementById('count').innerText.includes('03 项任务')")
                    # Queue tab and start dialog through the real UI path.
                    page.click('#tab-queue')
                    page.click('#btn-start')
                    page.wait_for_selector('#start-modal')
                    assert page.locator('#sm-inf').is_checked()
                    page.fill('#sm-interval', '0.1')
                    page.uncheck('#sm-inf')
                    page.click('#sm-confirm')
                    deadline = time.monotonic() + 30
                    while (backend.calls != ['download_c', 'local_upload', 'copy'] or workbench.controller.running) \
                            and time.monotonic() < deadline:
                        time.sleep(.2)
                    assert backend.calls == ['download_c', 'local_upload', 'copy'], backend.calls
                    assert not workbench.controller.running
                    page.wait_for_selector('#log')
                    page.wait_for_function(
                        "document.getElementById('log').innerText.indexOf('离线界面检查完成') >= 0",
                        timeout=10000)
                    assert not page_errors, page_errors
                finally:
                    browser.close()

            Path(report).write_text(json.dumps(dict(ok=True, frozen=bool(getattr(sys, 'frozen', False)),
                                                    gui_worker_bridge=True, dialogs=True, fifo=True,
                                                    instance_lock=True,
                                                    cloud_operations=0), indent=2), encoding='utf-8')
            if preview:
                workbench.status = '界面预览 · 示例任务，未执行任何云端操作'
                workbench.launch_edge(url)
                deadline = time.monotonic() + 600
                while not workbench.done and time.monotonic() < deadline:
                    time.sleep(.5)
                if not workbench.done:
                    workbench.shutdown()
                    time.sleep(3)
        finally:
            if preview:
                pass
            server.shutdown()
            server.server_close()
