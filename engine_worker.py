"""One interpreter per original project; line-delimited JSON over local pipes."""
import copy
import json
import os
import sys
import threading
from pathlib import Path


def configure_stdio():
    # A frozen Windows process can ignore PYTHONIOENCODING and inherit CP936.
    # Configure the pipes themselves before reading any request or status text.
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8',
                               errors='backslashreplace' if stream is sys.stderr else 'strict',
                               newline='\n')
    # Grandchild processes (the dedicated Edge, shell helpers) must not inherit
    # the protocol pipe or hold it open: stray console text would corrupt the
    # line protocol read by the main app.
    if os.name == 'nt':
        for fd in (0, 1, 2):
            try:
                os.set_inheritable(fd, False)
            except OSError:
                pass


def serve(engine):
    configure_stdio()
    from catalog import ROOT
    root = ROOT / 'engines' / engine
    sys.path.insert(0, str(ROOT))  # keep project modules importable after chdir
    os.chdir(root)
    sys.path.insert(0, str(root))
    protocol = sys.stdout
    sys.stdout = sys.stderr  # Original libraries may print; keep the protocol clean.
    from web_app import validate_config
    from edge_copy import EdgeCopier
    from local_upload import (LocalUploadMixin, LOCAL_ACTION, LOCAL_OPEN,
                              validate_local_config, engine_config)
    from table_download import (DownloadOnlyMixin, DOWNLOAD_ACTION, DOWNLOAD_OPEN,
                                validate_download_config)

    class QueueCopier(DownloadOnlyMixin, LocalUploadMixin, EdgeCopier):
        """Scheduling belongs solely to the outer queue; normal operations are inherited."""
        def __init__(self, root):
            super().__init__(root)
            self.queue_stop = threading.Event()

        def _poll_watch(self):
            # A background watcher would bypass FIFO and race the other engine.
            pass

        def checkpoint(self):
            if self.queue_stop.is_set():
                raise ValueError('已请求停止，当前操作在检查点结束；已经完成的写入不会撤销')

        def _select(self, *args, **kwargs):
            self.checkpoint()
            return super()._select(*args, **kwargs)

        def _capture(self, *args, **kwargs):
            self.checkpoint()
            return super()._capture(*args, **kwargs)

        def _ensure_context(self, *args, **kwargs):
            self.checkpoint()
            return super()._ensure_context(*args, **kwargs)

    copier = QueueCopier(root)
    last_action = None

    def submit(action, config):
        nonlocal last_action
        if copier.state()['busy']:
            raise ValueError('原项目仍在执行当前操作')
        local = engine == 'modern' and action in (LOCAL_ACTION, LOCAL_OPEN)
        downloading = engine == 'modern' and action in (DOWNLOAD_ACTION, DOWNLOAD_OPEN)
        config = (validate_local_config(config) if local else
                  validate_download_config(config) if downloading else validate_config(copy.deepcopy(config)))
        copier.queue_stop.clear()
        # Prevent stale errors in the other status channel from contaminating this job.
        with copier.lock:
            copier.upload_status = dict(busy=False, message='')
        last_action = action
        if engine == 'modern':
            copier.activity.configure(config)
            if action == LOCAL_ACTION:
                copier.submit_local_upload(config)
            elif action == LOCAL_OPEN:
                copier.submit_open(engine_config(config), 'a')
            elif action == 'sync':
                copier.submit_sync(config)
            elif action == DOWNLOAD_ACTION:
                copier.submit_download_only(config)
            elif action == DOWNLOAD_OPEN:
                copier.submit_open(config, 'c')
            elif action == 'download_b':
                copier.submit_download_table_b(config)
            elif action in ('open_c', 'open_a'):
                copier.submit_open(config, action[-1])
            else:
                raise ValueError('未知同步功能')
        elif action in ('copy', 'prepare'):
            source = config['source']
            copier.submit(action, config, source['copy_mode'], source['copy_range'], True)
        elif action in ('upload', 'prepare_upload'):
            copier.submit_upload(config, config['upload']['destination'], prepare=action == 'prepare_upload')
        else:
            raise ValueError('未知复制功能')

    def state():
        value = copier.state()
        uploading = engine == 'modern' and last_action not in ('open_c', DOWNLOAD_OPEN) or last_action in ('upload', 'prepare_upload')
        channel = value.get('upload', {}) if uploading else value
        error = value.get('error') or channel.get('error')
        if engine == 'modern':
            error = error or value.get('upload', {}).get('error')
        if value['busy'] and copier.thread and not copier.thread.is_alive():
            raise RuntimeError('原项目工作线程意外退出，请关闭并重新打开工作台，查看后台日志')
        return {'busy': value['busy'], 'message': channel.get('message') or value.get('message', ''),
                'error': error, 'stopped': copier.queue_stop.is_set()}

    try:
        for line in sys.stdin:
            try:
                request = json.loads(line)
                command = request['command']
                if command == 'validate':
                    if engine == 'modern' and request.get('action') == LOCAL_ACTION:
                        result = validate_local_config(request['config'])
                    elif engine == 'modern' and request.get('action') == DOWNLOAD_ACTION:
                        result = validate_download_config(request['config'])
                    else:
                        result = validate_config(copy.deepcopy(request['config']))
                elif command == 'submit':
                    submit(request['action'], request['config'])
                    result = {'accepted': True}
                elif command == 'state':
                    result = state()
                elif command == 'stop':
                    copier.queue_stop.set()
                    if engine == 'modern':
                        copier.request_stop()
                    result = {'stopping': True}
                elif command == 'close':
                    if copier.state()['busy']:
                        raise ValueError('当前任务尚未结束')
                    result = {'closing': True}
                else:
                    raise ValueError('未知后台命令')
                response = {'ok': True, 'result': result}
            except Exception as exc:
                response = {'ok': False, 'error': str(exc)}
            # ASCII JSON is a second guard against Windows locale differences.
            protocol.write(json.dumps(response, ensure_ascii=True, default=str) + '\n')
            protocol.flush()
            if response['ok'] and command == 'close':
                break
    finally:
        copier.close()


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--check-runtime' and sys.argv[2] in ('modern', 'legacy'):
        from runtime_check import check_engine
        print(json.dumps(check_engine(sys.argv[2]), ensure_ascii=False))
        raise SystemExit(0)
    if len(sys.argv) != 2 or sys.argv[1] not in ('modern', 'legacy'):
        raise SystemExit('Specify modern or legacy')
    serve(sys.argv[1])
