import json
import os
import queue
import subprocess
import sys
import threading
import time
from catalog import ROOT
from queue_core import Cancelled


class EngineProcess:
    def __init__(self, engine):
        (ROOT / 'logs').mkdir(exist_ok=True)
        self.log = (ROOT / 'logs' / (engine + '-worker.log')).open('a', encoding='utf-8')
        executable = sys.executable
        if getattr(sys, 'frozen', False):
            command = [str(ROOT / 'task_worker.exe'), engine]
        else:
            if executable.lower().endswith('pythonw.exe'):
                executable = str(__import__('pathlib').Path(executable).with_name('python.exe'))
            command = [executable, '-u', str(ROOT / 'engine_worker.py'), engine]
        self.process = subprocess.Popen(command,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log,
            encoding='utf-8', env=dict(os.environ, PYTHONIOENCODING='utf-8'),
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), cwd=ROOT)
        # Read raw bytes so foreign output from inherited handles cannot kill
        # the transport; lines are decoded one by one in _read.
        self.raw_stdout = self.process.stdout.buffer
        self.responses = queue.Queue()
        self.lock = threading.Lock()
        self.broken = False
        self.closing = False
        self.transport_error = ''
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        try:
            for raw in self.raw_stdout:
                for codec in ('utf-8', 'gbk'):
                    try:
                        line = raw.decode(codec)
                        break
                    except UnicodeDecodeError:
                        line = None
                if line is None:
                    self._log_noise(raw, '无法解码的输出')
                    continue
                try:
                    self.responses.put(json.loads(line))
                except ValueError:
                    # Grandchild processes (dedicated Edge, shell helpers) may
                    # inherit the pipe and write console text; keep the bridge.
                    self._log_noise(raw, '非协议输出')
        except Exception as exc:
            if not self.closing:
                self._fail_transport(f'后台通信失败：{exc}。请关闭并重新打开工作台。')
        finally:
            if not self.closing:
                self._fail_transport('后台进程已退出，请查看 logs 下的日志并重新打开工作台。')

    def _log_noise(self, raw, reason):
        try:
            self.log.write(f'忽略后台进程输出（{reason}）：{raw[:200]!r}\n')
            self.log.flush()
        except OSError:
            pass

    def _fail_transport(self, message):
        if not self.broken:
            self.transport_error = message
            self.broken = True
            # Wake a pending call, and reject all later requests immediately.
            self.responses.put({'ok': False, 'error': message})

    def call(self, command, **data):
        with self.lock:
            if self.broken:
                raise RuntimeError(self.transport_error or '后台通信已中断，请关闭并重新打开工作台')
            if self.process.poll() is not None:
                raise RuntimeError('后台进程已退出，请查看 logs 下的日志')
            try:
                self.process.stdin.write(json.dumps(dict(command=command, **data), ensure_ascii=True) + '\n')
                self.process.stdin.flush()
            except (OSError, UnicodeError) as exc:
                self._fail_transport(f'后台通信写入失败：{exc}。请关闭并重新打开工作台。')
                raise RuntimeError(self.transport_error) from exc
            try:
                response = self.responses.get(timeout=120)
            except queue.Empty:
                self._fail_transport('后台响应超时，队列已停止派发；请查看日志确认当前操作状态')
                raise RuntimeError(self.transport_error)
            if not response['ok']:
                raise ValueError(response['error'])
            return response['result']

    def close(self):
        self.closing = True
        try:
            if self.process.poll() is None:
                self.call('close')
                self.process.wait(timeout=15)
        finally:
            if self.process.poll() is not None:
                for stream in (self.process.stdin, self.process.stdout, self.log):
                    stream.close()


class Backend:
    def __init__(self):
        self.engines = {}
        self.lock = threading.Lock()

    def engine(self, key):
        with self.lock:
            if key not in self.engines:
                self.engines[key] = EngineProcess(key)
            return self.engines[key]

    def validate(self, engine, config, action=None):
        return self.engine(engine).call('validate', config=config, action=action)

    def _wait(self, process, stop_event, progress):
        stopped, previous = False, None
        while True:
            if stop_event.is_set() and not stopped:
                process.call('stop')
                stopped = True
            state = process.call('state')
            message = state['message']
            if message != previous:
                progress(message)
                previous = message
            if not state['busy']:
                if stopped or stop_event.is_set():
                    raise Cancelled()
                if state['error']:
                    raise RuntimeError(state['error'])
                return message
            time.sleep(.2)  # Do not spin while stop is set and cleanup is still running.

    def execute(self, task, stop_event, progress):
        # A timed-out submit may still be running. Never dispatch to either
        # engine until that uncertainty has been resolved by restarting.
        if any(p.broken or p.process.poll() is not None for p in self.engines.values()):
            raise RuntimeError('后台状态不确定，已禁止继续派发任务。请检查日志并重新打开工作台。')
        process = self.engine(task['engine'])
        if stop_event.is_set():
            raise Cancelled()
        if task['engine'] == 'legacy' and task['action'] == 'copy':
            # Original copy requires an opened source page; it does not open it itself.
            process.call('submit', action='prepare', config=task['config'])
            self._wait(process, stop_event, progress)
        if stop_event.is_set():
            raise Cancelled()
        process.call('submit', action=task['action'], config=task['config'])
        return self._wait(process, stop_event, progress)

    def close(self):
        errors = []
        for process in self.engines.values():
            try:
                process.close()
            except Exception as exc:
                errors.append(str(exc))
        if errors:
            raise RuntimeError('\n'.join(errors))
