"""HTML front-end host: local JSON API + a dedicated Edge app-mode window.

The whole UI is rendered by HTML/CSS in a Chromium window; the business layer
(QueueController, Backend, engine workers) is unchanged. The frontend talks to
this module only through the HTTP API described in assets/webui/app.js.
"""
import collections
import copy
import json
import os
import queue as queue_module
import secrets
import shutil
import subprocess
import threading
import time
import tkinter as tk
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tkinter import filedialog
from urllib.parse import parse_qs, urlparse

from backend import Backend
from catalog import ACTIONS, CHOICES, ENGINES, LABELS, OPEN_ACTIONS, ROOT, SECTIONS
from local_upload import LOCAL_ACTION, LOCAL_OPEN, default_config as local_defaults
from table_download import DOWNLOAD_ACTION, DOWNLOAD_OPEN, download_config
from queue_core import QueueController, atomic_json

WEBUI = ROOT / 'assets' / 'webui'
STATIC = {'/': 'index.html', '/index.html': 'index.html', '/app.css': 'app.css', '/app.js': 'app.js',
          '/FGO.ico': 'FGO.ico'}
WINDOW_TITLE = '识质存在'


def _find_window_by_title(title):
    """Top-level window handle whose exact title matches (None if absent)."""
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    result = {}

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def callback(hwnd, _):
        buffer = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, buffer, 256)
        if buffer.value == title:
            result['hwnd'] = hwnd
            return False
        return True

    user32.EnumWindows(callback, 0)
    return result.get('hwnd')


def activate_window(title=WINDOW_TITLE):
    import ctypes
    user32 = ctypes.windll.user32
    hwnd = _find_window_by_title(title)
    if not hwnd:
        return False
    if not user32.SetForegroundWindow(hwnd):
        user32.keybd_event(0x12, 0, 0, 0)  # ALT down/up: release the foreground lock
        user32.keybd_event(0x12, 0, 2, 0)
        user32.SetForegroundWindow(hwnd)
    return True


def restore_window(title=WINDOW_TITLE):
    """Restore only on an explicit duplicate launch; never run periodically."""
    import ctypes
    user32 = ctypes.windll.user32
    hwnd = _find_window_by_title(title)
    if not hwnd or not user32.IsIconic(hwnd):
        return False
    user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    activate_window(title)
    return True


class EventHub:
    """Single-consumer buffered event queue for the page's long-poll loop."""

    def __init__(self):
        self.condition = threading.Condition()
        self.events = collections.deque()

    def publish(self, event):
        with self.condition:
            self.events.append(event)
            self.condition.notify_all()

    def wait(self, timeout):
        with self.condition:
            if not self.events:
                self.condition.wait(timeout)
            if not self.events:
                return []
            result = list(self.events)
            self.events.clear()
            return result


def find_edge():
    found = shutil.which('msedge') or shutil.which('msedge.exe')
    if found:
        return found
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                             r'SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe')
        found = winreg.QueryValue(key, None)
    except OSError:
        found = ''
    if found and Path(found).is_file():
        return found
    for base in (os.environ.get('ProgramFiles(x86)', ''), os.environ.get('ProgramFiles', '')):
        candidate = Path(base) / 'Microsoft' / 'Edge' / 'Application' / 'msedge.exe'
        if candidate.is_file():
            return str(candidate)
    raise RuntimeError('未找到 Microsoft Edge，请先安装 Edge 后再运行工作台。')


def ask_open_filename():
    """Native file dialog; Tk is confined to its own thread and interpreter."""
    result = {}

    def run():
        root = tk.Tk()
        root.withdraw()
        try:
            result['path'] = filedialog.askopenfilename(
                parent=root, title='选择 Excel 文件',
                filetypes=[('Excel 工作簿', '*.xlsx'), ('所有文件', '*.*')])
        finally:
            try:
                root.destroy()
            except tk.TclError:
                pass

    thread = threading.Thread(target=run)
    thread.start()
    thread.join()
    return result.get('path')


class Workbench:
    def __init__(self, backend=None, state_path=None):
        self.backend = backend or Backend()
        self.validating = False
        self.closing = False
        self.done = False
        self.edge_process = None
        self.server = None
        self.lock = threading.RLock()
        self.events = queue_module.Queue()
        self.hub = EventHub()
        self.controller = QueueController(state_path or ROOT / 'state' / 'queue.json',
                                          self.backend, self.events.put)
        self.templates = {engine: json.loads(
            (ROOT / 'engines' / engine / 'config.json').read_text(encoding='utf-8-sig'))
            for engine in ENGINES}
        self.template_error = ''
        self.local_template = local_defaults()
        local_template_path = ROOT / 'state' / 'local_upload_template.json'
        if local_template_path.exists():
            try:
                data = json.loads(local_template_path.read_text(encoding='utf-8'))
                if set(data) != set(self.local_template) or any(
                        not isinstance(data[key], dict) or set(data[key]) != set(value)
                        for key, value in self.local_template.items()):
                    raise ValueError('配置格式无效')
                self.local_template = data
            except Exception as exc:
                self.template_error = f'本地Excel覆盖默认配置读取失败：{exc}'
        if (ROOT / 'state' / 'templates.json').exists():
            try:
                data = json.loads((ROOT / 'state' / 'templates.json').read_text(encoding='utf-8'))
                if set(data) != set(ENGINES) or any(not isinstance(c, dict) for c in data.values()):
                    raise ValueError('配置格式无效')
                self.templates = data
            except Exception as exc:
                self.template_error = f'默认配置读取失败，已使用导入配置：{exc}'
        self.download_template = download_config(self.templates['modern'])
        download_template_path = ROOT / 'state' / 'download_template.json'
        if download_template_path.exists():
            try:
                self.download_template = download_config(json.loads(download_template_path.read_text(encoding='utf-8')))
            except Exception as exc:
                self.template_error = f'下载表C默认配置读取失败：{exc}'
        self.status = '准备就绪 · 创建任务后，前往任务队列开始执行'
        self.detail = '默认间隔 15 秒 · 无限循环 · 严格串行'
        self.log_lines = ['工作台已就绪。首次使用，请在功能配置窗口打开两个项目各自的专用 Edge 并登录。']
        threading.Thread(target=self._pump, daemon=True).start()
        for error in (self.controller.load_error, self.template_error):
            if error:
                self.events.put({'type': 'error', 'message': error})

    # ---------- state ----------
    def busy(self):
        return {'running': self.controller.running, 'validating': self.validating,
                'closing': self.closing}

    def append_log(self, message):
        self.log_lines.append(datetime.now().strftime('%H:%M:%S') + '  ' + str(message))
        if len(self.log_lines) > 500:
            del self.log_lines[:100]
        # Deliberately keep the central log free of configuration contents.
        try:
            folder = ROOT / 'logs'
            folder.mkdir(exist_ok=True)
            with (folder / (datetime.now().strftime('%Y-%m-%d') + '.log')).open('a', encoding='utf-8') as stream:
                stream.write(datetime.now().isoformat(timespec='seconds') + ' ' + str(message) + '\n')
        except OSError:
            pass

    def _pump(self):
        """Forward controller events to the page and keep the log file up to date."""
        while not self.done:
            try:
                event = self.events.get(timeout=0.2)
            except queue_module.Empty:
                continue
            kind = event['type']
            if kind == 'callback':
                event['callback']()
                continue
            message = event.get('message', '')
            if kind == 'detail':
                self.detail = message
            elif kind in ('progress', 'waiting', 'stopping', 'finished'):
                self.status = message
            elif kind == 'error':
                self.detail = message
            if kind != 'waiting' and message:
                self.append_log(message)
            # Busy flags ride on every event at the top level (frontend contract).
            self.hub.publish(dict(event, **self.busy()))

    def tasks_payload(self):
        payload = []
        for index, task in enumerate(self.controller.snapshot(), 1):
            status = task['status']
            tag = ('failed' if status == '失败' else 'active' if '执行中' in status
                   else 'success' if '成功' in status else ('even' if index % 2 == 0 else 'odd'))
            payload.append({'id': task['id'], 'order': index, 'name': task['name'],
                            'engine': task['engine'], 'action': task['action'],
                            'engine_name': ENGINES[task['engine']], 'status': status, 'tag': tag})
        return payload

    def state_payload(self):
        return {
            'ok': True,
            'status': self.status,
            'detail': self.detail,
            'settings': dict(self.controller.settings),
            'busy': self.busy(),
            'tasks': self.tasks_payload(),
            'count_text': f'排队窗口  ·  {len(self.tasks_payload()):02d} 项任务',
        }

    def init_payload(self):
        payload = self.state_payload()
        payload['brand'] = '表格同步工作台'
        payload['badge'] = 'WPS SYNC  /  OLD'
        payload['log_lines'] = list(self.log_lines)
        payload['engines'] = [
            {'id': engine, 'number': '01' if index == 0 else '02', 'title': ENGINES[engine],
             'actions': [{'id': action, 'name': name, 'description': description,
                          'accent': action == LOCAL_ACTION}
                         for action, (name, description) in ACTIONS[engine].items()]}
            for index, engine in enumerate(ENGINES)]
        return payload

    # ---------- config descriptor (mirrors the former ConfigDialog) ----------
    def config_payload(self, engine=None, action=None, task_id=None):
        task = next((t for t in self.controller.snapshot() if task_id and t['id'] == task_id), None) \
            if task_id else None
        engine = engine or (task['engine'] if task else '')
        action = action or (task['action'] if task else '')
        if engine not in ACTIONS or action not in ACTIONS[engine]:
            raise ValueError('任务功能无效')
        is_local = engine == 'modern' and action == LOCAL_ACTION
        is_download = engine == 'modern' and action == DOWNLOAD_ACTION
        data = copy.deepcopy(task['config'] if task else
                             (self.local_template if is_local else
                              self.download_template if is_download else self.templates[engine]))
        if is_download:
            data = download_config(data)
        notes = ['任务保留独立配置快照；修改其他任务不会影响本任务。']
        notes.append('覆盖整个云文件及全部工作表，保留本地格式；公式使用已保存的计算结果转为值。'
                     if is_local else '调度统一由任务队列控制；旧版定时及即时同步不会在队列外自行执行。')
        if is_local:
            notes.append('目标须位于“我的云文档”根目录。执行时读取最新已保存文件，可接在生成表A1的任务后。')
            notes.append('如需上传已下载的表C，请选择：' + str(ROOT / 'engines' / 'modern' / 'DATA' / '表C.xlsx'))
        elif is_download:
            notes.append('本任务只下载完整Excel文件，不上传、不覆盖云端，也不生成表A1镜像。')
            notes.append('保存位置：' + str(ROOT / 'engines' / 'modern' / 'DATA' / '表C.xlsx') + '；再次下载会替换该本地文件。')
        elif engine == 'modern':
            notes.append('新版主流程为完整云文件覆盖，扫描范围及旧筛选等选项的作用沿用原项目。')
        sections = []
        for section, values in data.items():
            fields = []
            for key, value in values.items():
                label = LABELS.get(key, key)
                if is_local and key == 'local_file':
                    label = '待上传的本地Excel（完整路径）'
                elif is_local and key == 'share_url':
                    label = '目标云文件分享链接'
                elif key == 'share_url':
                    table = ('C' if section == 'source' else 'A') if engine == 'modern' \
                        else ('A' if section == 'source' else 'D')
                    label = '表' + table + '分享链接'
                if is_download and key == 'sheet_name':
                    label = '表C工作表名称（用于校验，仍下载完整文件）'
                field = {'key': key, 'label': label, 'value': value,
                         'type': 'select' if key in CHOICES
                         else 'checkbox' if isinstance(value, bool)
                         else 'file' if key in ('local_file', 'format_file')
                         else 'text'}
                if key in CHOICES:
                    field['options'] = [{'value': option, 'label': text}
                                        for option, text in CHOICES[key].items()]
                    field['display'] = CHOICES[key].get(value, value)
                field['disabled'] = section == 'schedule' or (
                    section == 'sync' and key in ('enabled', 'wait_seconds', 'watch_enabled', 'watch_debounce'))
                fields.append(field)
            sections.append({'id': section, 'title': SECTIONS.get(section, section), 'fields': fields})
        open_actions = ([{'id': LOCAL_OPEN, 'label': '打开目标 / 登录'}] if is_local
                        else [{'id': DOWNLOAD_OPEN, 'label': '打开表C / 登录'}] if is_download
                        else [{'id': operation, 'label': label}
                              for operation, label in OPEN_ACTIONS[engine]])
        return {'ok': True, 'title': ('编辑任务 · ' if task else '配置功能 · ') + ACTIONS[engine][action][0],
                'name': task['name'] if task else ACTIONS[engine][action][0],
                'engine': engine, 'action': action,
                'notes': notes, 'sections': sections, 'open_actions': open_actions,
                'commit_label': '保存任务' if task else '创建任务 → 加入队列',
                'template_label': '保存为功能默认配置', 'is_local_upload': is_local}

    def _convert(self, engine, action, payload_config, base):
        result = copy.deepcopy(base)
        for section, fields in result.items():
            for key, original in fields.items():
                value = payload_config.get(section, {}).get(key, original)
                try:
                    if key in CHOICES:
                        if value not in CHOICES[key]:
                            raise ValueError
                    elif isinstance(original, bool):
                        value = bool(value)
                    elif isinstance(original, int):
                        value = int(value)
                    elif isinstance(original, float):
                        value = float(value)
                except (ValueError, TypeError):
                    raise ValueError(LABELS.get(key, key) + '：输入格式不正确')
                result[section][key] = value
        return result

    # ---------- actions (mirror the former Tk handlers) ----------
    def _guard_editable(self):
        if self.controller.running:
            raise ValueError('请先停止任务，等待当前操作结束后再编辑队列')
        if self.controller.load_error:
            raise ValueError(self.controller.load_error + '\n请修复或移走 state/queue.json 后重新打开程序。')
        if self.validating or self.closing:
            raise ValueError('请等待当前操作完成后再继续')

    def commit(self, payload):
        engine = payload.get('engine')
        action = payload.get('action')
        mode = payload.get('mode')
        task_id = payload.get('task_id')
        name = payload.get('name', '')
        task = next((t for t in self.controller.snapshot() if task_id and t['id'] == task_id), None) \
            if task_id else None
        engine = engine or (task['engine'] if task else '')
        action = action or (task['action'] if task else '')
        if engine not in ACTIONS or action not in ACTIONS[engine]:
            raise ValueError('任务功能无效')
        if mode == 'task' and task_id and not task:
            raise ValueError('任务不存在，队列可能已被修改，请刷新后重试')
        self._guard_editable()
        is_local = engine == 'modern' and action == LOCAL_ACTION
        is_download = engine == 'modern' and action == DOWNLOAD_ACTION
        base = copy.deepcopy(task['config'] if task else
                             (self.local_template if is_local else
                              self.download_template if is_download else self.templates[engine]))
        if is_download:
            base = download_config(base)
        config = self._convert(engine, action, payload.get('config') or {}, base)
        self.validating = True
        self.hub.publish(dict(type='busy', **self.busy()))
        try:
            validated = self.backend.validate(engine, config, action=action)
            if mode == 'template':
                with self.lock:
                    if is_local:
                        atomic_json(ROOT / 'state' / 'local_upload_template.json', validated)
                        self.local_template = validated
                    elif is_download:
                        atomic_json(ROOT / 'state' / 'download_template.json', validated)
                        self.download_template = validated
                    else:
                        templates = copy.deepcopy(self.templates)
                        templates[engine] = validated
                        atomic_json(ROOT / 'state' / 'templates.json', templates)
                        self.templates = templates
                message = '默认配置已保存。已有任务保持各自的配置。'
            elif mode == 'task':
                if task:
                    self.controller.update(task['id'], validated, name)
                else:
                    self.controller.add(engine, action, validated, name)
                message = None
            else:
                self.controller.auxiliary(engine, mode, validated)
                message = '正在打开专用 Edge，请等待浏览器窗口并确认登录。'
            return {'ok': True, 'message': message}
        finally:
            self.validating = False
            self.hub.publish(dict(type='busy', **self.busy()))

    def duplicate(self, payload):
        task = next((t for t in self.controller.snapshot() if t['id'] == payload.get('id')), None)
        if not task:
            raise ValueError('请先选择要复制的任务')
        self._guard_editable()
        self.controller.add(task['engine'], task['action'], task['config'], task['name'] + '（副本）')
        return {'ok': True}

    def remove(self, payload):
        task = next((t for t in self.controller.snapshot() if t['id'] == payload.get('id')), None)
        if not task:
            raise ValueError('请先选择要删除的任务')
        self._guard_editable()
        self.controller.remove(task['id'])
        return {'ok': True}

    def start(self, payload):
        self._guard_editable()
        try:
            interval = float(payload.get('interval'))
            cycles = int(payload.get('cycles'))
        except (TypeError, ValueError):
            raise ValueError('任务间隔或循环次数格式不正确')
        if cycles < 0:
            raise ValueError('有限循环次数至少为 1')
        self.controller.start(interval, cycles)
        return {'ok': True}

    def stop(self):
        self.controller.stop()
        return {'ok': True}

    def open_logs(self):
        folder = ROOT / 'logs'
        folder.mkdir(parents=True, exist_ok=True)
        os.startfile(folder)
        return {'ok': True}

    # ---------- window / lifecycle ----------
    def launch_edge(self, url):
        profile = ROOT / 'state' / 'webui_profile'
        command = [find_edge(), '--app=' + url, '--user-data-dir=' + str(profile),
                   '--window-size=1240,850', '--no-first-run', '--no-default-browser-check',
                   '--disable-background-mode']
        self.edge_process = subprocess.Popen(command, cwd=ROOT)

    def shutdown(self):
        if self.closing:
            return
        self.closing = True
        self.status = '正在关闭：等待当前操作结束并释放专用浏览器…'
        self.hub.publish(dict(type='busy', **self.busy()))
        self.controller.stop()

        def run():
            deadline = time.monotonic() + 120
            while (self.controller.running or self.validating) and time.monotonic() < deadline:
                time.sleep(.1)
            try:
                self.backend.close()
            except Exception as exc:
                self.append_log('后台退出异常：' + str(exc))
            self.done = True
            if self.server is not None:
                self.server.shutdown()
            if self.edge_process is not None and self.edge_process.poll() is None:
                try:
                    self.edge_process.terminate()
                except OSError:
                    pass

        threading.Thread(target=run, daemon=True).start()


class WorkbenchServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, workbench, host='127.0.0.1', port=0):
        super().__init__((host, port), Handler)
        self.workbench = workbench
        self.token = secrets.token_hex(16)
        workbench.server = self

    @property
    def url(self):
        return f'http://127.0.0.1:{self.server_address[1]}/?t={self.token}'


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'WorkbenchLocal/1.0'

    # ---------- plumbing ----------
    def log_message(self, *args):
        pass

    def _send(self, code, body, content_type='application/json; charset=utf-8'):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(data)

    def _ok(self, body):
        self._send(200, body)

    def _json(self, error, code=400):
        self._send(code, {'ok': False, 'error': error})

    def _authorized(self):
        query = parse_qs(urlparse(self.path).query)
        return query.get('t', [''])[0] == self.server.token

    def _body(self):
        length = int(self.headers.get('Content-Length') or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode('utf-8'))
        except (ValueError, UnicodeDecodeError):
            return {}

    def _static(self, path):
        name = STATIC.get(path)
        if name:
            file = WEBUI / name
            if file.is_file():
                content_type = 'text/html; charset=utf-8' if name.endswith('.html') \
                    else 'text/css; charset=utf-8' if name.endswith('.css') \
                    else 'image/x-icon' if name.endswith('.ico') \
                    else 'application/javascript; charset=utf-8'
                self._send(200, file.read_bytes(), content_type)
                return
        if path == '/favicon.ico':
            icon = ROOT / 'assets' / 'app.ico'
            if icon.is_file():
                self._send(200, icon.read_bytes(), 'image/x-icon')
                return
        self._send(404, 'Not Found', 'text/plain; charset=utf-8')

    # ---------- routes ----------
    def do_GET(self):
        path = urlparse(self.path).path
        if path == '/api/events':
            if not self._authorized():
                return self._json('无效会话', 403)
            events = self.server.workbench.hub.wait(25)
            return self._ok({'events': events})
        return self._static(path)

    def do_POST(self):
        path = urlparse(self.path).path
        if path not in ('/api/init', '/api/config', '/api/state', '/api/commit',
                        '/api/duplicate', '/api/remove', '/api/start', '/api/stop',
                        '/api/logs', '/api/browse', '/api/shutdown', '/api/events'):
            return self._send(404, 'Not Found', 'text/plain; charset=utf-8')
        if not self._authorized():
            return self._json('无效会话', 403)
        workbench = self.server.workbench
        payload = self._body()
        try:
            if path == '/api/init':
                return self._ok(workbench.init_payload())
            if path == '/api/state':
                return self._ok(workbench.state_payload())
            if path == '/api/config':
                engine, action, task_id = payload.get('engine'), payload.get('action'), payload.get('task_id')
                if engine not in ACTIONS or action not in ACTIONS[engine]:
                    raise ValueError('任务功能无效')
                return self._ok(workbench.config_payload(engine, action, task_id))
            if path == '/api/commit':
                return self._ok(workbench.commit(payload))
            if path == '/api/duplicate':
                return self._ok(workbench.duplicate(payload))
            if path == '/api/remove':
                return self._ok(workbench.remove(payload))
            if path == '/api/start':
                return self._ok(workbench.start(payload))
            if path == '/api/stop':
                return self._ok(workbench.stop())
            if path == '/api/logs':
                return self._ok(workbench.open_logs())
            if path == '/api/browse':
                chosen = ask_open_filename()
                return self._ok({'ok': True, 'path': chosen} if chosen else {'ok': False})
            if path == '/api/shutdown':
                threading.Thread(target=workbench.shutdown, daemon=True).start()
                return self._ok({'ok': True})
            if path == '/api/events':
                events = workbench.hub.wait(25)
                return self._ok({'events': events})
        except ValueError as exc:
            return self._json(str(exc))
        except Exception as exc:
            workbench.append_log('接口异常：' + repr(exc))
            return self._json('内部错误：' + str(exc), 500)
        return self._send(404, 'Not Found', 'text/plain; charset=utf-8')


def run_workbench(backend=None, state_path=None):
    """Blocking entry: serve the UI, open the Edge window, wait for shutdown."""
    workbench = Workbench(backend, state_path)
    server = WorkbenchServer(workbench)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    workbench.launch_edge(server.url)

    try:
        while not workbench.done:
            time.sleep(.5)
            if workbench.edge_process is not None and workbench.edge_process.poll() is not None:
                workbench.shutdown()
    finally:
        server.shutdown()
        server.server_close()
    return workbench
