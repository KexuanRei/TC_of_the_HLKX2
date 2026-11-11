"""Persistent FIFO plan, interruptible waits, and a single execution owner."""
import copy
import json
import math
import os
import threading
import time
import uuid
from pathlib import Path


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    os.replace(temporary, path)


class Cancelled(Exception):
    pass


class QueueController:
    def __init__(self, path, backend, notify=lambda event: None):
        self.path, self.backend, self.notify = Path(path), backend, notify
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.running = False
        self.thread = None
        self.tasks = []
        self.settings = {'interval': 15, 'cycles': 0}
        self.load_error = ''
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding='utf-8'))
                tasks = data['tasks']
                if not isinstance(tasks, list):
                    raise ValueError('tasks 必须为列表')
                ids = set()
                from catalog import ACTIONS
                for task in tasks:
                    if task['action'] not in ACTIONS[task['engine']] or not isinstance(task['config'], dict):
                        raise ValueError('任务内容无效')
                    if not isinstance(task['name'], str) or task['id'] in ids:
                        raise ValueError('任务名称或标识无效')
                    ids.add(task['id'])
                    if (task['engine'] == 'modern' and task['action'] == 'download_c'
                            and task['name'] == '下载表C并整表上传'):
                        task['name'] = '下载表C'
                    task['status'] = '待执行'
                self.tasks = tasks
                self.settings = self.validate_settings(**data.get('settings', self.settings))
            except Exception as exc:
                self.tasks = []
                self.load_error = f'队列文件无法读取，原文件已保留：{exc}'

    @staticmethod
    def validate_settings(interval=15, cycles=0):
        if isinstance(interval, bool) or not isinstance(interval, (int, float)) or not math.isfinite(interval) or not 0 <= interval <= 86400:
            raise ValueError('任务间隔必须在 0～86400 秒之间')
        if isinstance(cycles, bool) or not isinstance(cycles, int) or cycles < 0:
            raise ValueError('循环次数必须是非负整数，0 表示无限循环')
        return {'interval': interval, 'cycles': cycles}

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(self.tasks)

    def _idle(self):
        if self.running:
            raise ValueError('请先停止任务，等待当前操作结束后再编辑队列')
        if self.load_error:
            raise ValueError(self.load_error + '\n请修复或移走 state/queue.json 后重新打开程序。')

    def _save(self):
        atomic_json(self.path, {'version': 1, 'tasks': self.tasks, 'settings': self.settings})

    def add(self, engine, action, config, name):
        from catalog import ACTIONS
        if engine not in ACTIONS or action not in ACTIONS[engine]:
            raise ValueError('任务功能无效')
        with self.lock:
            self._idle()
            task = dict(id=uuid.uuid4().hex, engine=engine, action=action,
                        name=name.strip() or ACTIONS[engine][action][0], config=copy.deepcopy(config), status='待执行')
            self.tasks.append(task)
            try:
                self._save()
            except Exception:
                self.tasks.pop()
                raise
        self.notify({'type': 'changed'})
        return task['id']

    def update(self, task_id, config, name):
        with self.lock:
            self._idle()
            task = next(t for t in self.tasks if t['id'] == task_id)
            old = copy.deepcopy(task)
            task.update(config=copy.deepcopy(config), name=name.strip() or task['name'], status='待执行')
            try:
                self._save()
            except Exception:
                task.clear()
                task.update(old)
                raise
        self.notify({'type': 'changed'})

    def remove(self, task_id):
        with self.lock:
            self._idle()
            old = self.tasks
            self.tasks = [t for t in self.tasks if t['id'] != task_id]
            try:
                self._save()
            except Exception:
                self.tasks = old
                raise
        self.notify({'type': 'changed'})

    def start(self, interval=15, cycles=0):
        settings = self.validate_settings(interval, cycles)
        with self.lock:
            self._idle()
            if not self.tasks:
                raise ValueError('请先在功能区创建至少一个任务')
            self.settings = settings
            self._save()
            plan = copy.deepcopy(self.tasks)
            for task in self.tasks:
                task['status'] = '待执行'
            self.stop_event = threading.Event()
            self.running = True
            self.thread = threading.Thread(target=self._run, args=(plan, settings), daemon=True)
            self.thread.start()

    def stop(self):
        with self.lock:
            if self.running:
                self.stop_event.set()
                self.notify({'type': 'stopping', 'message': '正在停止：后续任务已取消，等待当前操作到达停止检查点…'})

    def _status(self, task_id, value):
        with self.lock:
            for task in self.tasks:
                if task['id'] == task_id:
                    task['status'] = value
        self.notify({'type': 'changed'})

    def _run(self, plan, settings):
        outcome = '队列已完成'
        active = None
        cycle = 0
        try:
            while not settings['cycles'] or cycle < settings['cycles']:
                cycle += 1
                for index, task in enumerate(plan):
                    if self.stop_event.is_set():
                        raise Cancelled()
                    active = task
                    self._status(task['id'], f'第{cycle}轮 · 执行中')
                    self.notify({'type': 'progress', 'message': f'第 {cycle} 轮 · {index + 1}/{len(plan)} · {task["name"]}'})
                    result = self.backend.execute(task, self.stop_event, lambda msg: self.notify({'type': 'detail', 'message': msg}))
                    if self.stop_event.is_set():
                        raise Cancelled()
                    self._status(task['id'], f'第{cycle}轮 · 成功')
                    self.notify({'type': 'log', 'message': f'{task["name"]}：{result or "完成"}'})
                    active = None
                    last = index == len(plan) - 1 and settings['cycles'] and cycle == settings['cycles']
                    if not last:
                        deadline = time.monotonic() + settings['interval']
                        while time.monotonic() < deadline:
                            remaining = max(0, math.ceil(deadline - time.monotonic()))
                            self.notify({'type': 'waiting', 'message': f'间隔等待 · {remaining} 秒后执行下一项'})
                            if self.stop_event.wait(min(.25, max(0, deadline - time.monotonic()))):
                                raise Cancelled()
        except Cancelled:
            outcome = '已停止；再次开始将从队列第一项执行'
            if active:
                self._status(active['id'], '已停止 / 请核对执行结果')
        except Exception as exc:
            outcome = f'任务失败，队列已停止：{exc}'
            if active:
                self._status(active['id'], '失败')
            self.notify({'type': 'error', 'message': outcome})
        finally:
            with self.lock:
                self.running = False
            self.notify({'type': 'finished', 'message': outcome})

    def auxiliary(self, engine, action, config):
        """Login preparation also reserves the same execution lane."""
        with self.lock:
            self._idle()
            self.running = True
            self.stop_event = threading.Event()
        def run():
            outcome = '准备操作结束，请在专用 Edge 中确认登录及工作表'
            try:
                task = dict(engine=engine, action=action, config=copy.deepcopy(config))
                result = self.backend.execute(task, self.stop_event, lambda msg: self.notify({'type': 'detail', 'message': msg}))
                self.notify({'type': 'log', 'message': result})
            except Cancelled:
                outcome = '已停止打开操作'
                self.notify({'type': 'log', 'message': '已停止打开操作'})
            except Exception as exc:
                outcome = '打开操作失败：' + str(exc)
                self.notify({'type': 'error', 'message': str(exc)})
            finally:
                with self.lock:
                    self.running = False
                self.notify({'type': 'finished', 'message': outcome})
        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()
