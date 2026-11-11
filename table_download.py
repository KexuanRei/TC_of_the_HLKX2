"""Download-only adapter; reuse the original XLSX export without its upload step."""
import copy
from pathlib import Path

DOWNLOAD_ACTION = 'download_c'  # Preserve saved queue IDs; this action now only downloads.
DOWNLOAD_OPEN = 'open_download_c'
DOWNLOAD_MARKER = '_workbench_download_only'


def download_config(config=None):
    """Project old combined-operation snapshots onto download-only settings."""
    config = config if config is not None else {}
    if not isinstance(config, dict):
        raise ValueError('下载表C配置必须为对象')
    source = config.get('source', {})
    monitor = config.get('monitor', {})
    if not isinstance(source, dict) or not isinstance(monitor, dict):
        raise ValueError('下载表C配置字段格式不正确')
    return {'source': {'share_url': copy.deepcopy(source.get('share_url', '')),
                       'sheet_name': copy.deepcopy(source.get('sheet_name', 'Sheet1'))},
            'monitor': {'enabled': copy.deepcopy(monitor.get('enabled', True))}}


def validate_download_config(config):
    from edge_copy import validate_url
    from web_app import _share_link
    result = download_config(config)
    link = result['source']['share_url']
    if not isinstance(link, str) or not link.strip():
        raise ValueError('请填写表C分享链接')
    link = link.strip()
    validate_url(link, '表C分享链接')
    _share_link(link, '表C分享链接')
    if not link.split('/l/', 1)[-1].split('?', 1)[0].split('#', 1)[0].strip('/'):
        raise ValueError('请填写完整的表C分享链接')
    result['source']['share_url'] = link
    sheet = result['source']['sheet_name']
    if not isinstance(sheet, str) or not sheet.strip():
        raise ValueError('请填写表C中用于校验下载文件的工作表名称')
    result['source']['sheet_name'] = sheet.strip()
    if not isinstance(result['monitor']['enabled'], bool):
        raise ValueError('操作日志开关必须为布尔值')
    return result


def run_download_only(copier, config):
    from sync_runner import SyncStopped, _stop_requested
    from table_c_download import download_table_c
    config = validate_download_config(config)
    if _stop_requested(copier):
        raise SyncStopped()
    # Calling the export primitive directly avoids the combined operation's
    # upload, mirror creation, config.json rewrite and background-watch setup.
    result = dict(download_table_c(copier, config))
    path = (Path(copier.root) / result['path']).resolve()
    result['path'] = str(path)
    result['message'] = f'表C下载完成：{path}。本任务仅下载，未执行上传。'
    return result


class DownloadOnlyMixin:
    def submit_download_only(self, config):
        config = validate_download_config(config)
        self._reset_stop()
        self._enqueue(('sync', {DOWNLOAD_MARKER: config}), '下载表C',
                      '正在下载表C到 engines/modern/DATA/表C.xlsx…', upload=True)

    def _run_sync(self, config):
        if DOWNLOAD_MARKER in config:
            return run_download_only(self, config[DOWNLOAD_MARKER])
        return super()._run_sync(config)
