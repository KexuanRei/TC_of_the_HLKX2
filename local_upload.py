"""Local-XLSX adapter for the existing modern cloud-file replacement engine.

No OLD operations or source downloads. Local uploads finish on the target's
current WPS success notice, retaining the pre-upload backup and target checks.
"""
import copy
import json
from pathlib import Path
from zipfile import ZipFile, BadZipFile

from queue_core import atomic_json

LOCAL_ACTION = 'local_upload'
LOCAL_OPEN = 'open_local_target'
LOCAL_MARKER = '_workbench_local_excel'


def default_config():
    return {'source': {'local_file': ''},
            'upload': {'share_url': '', 'cloud_filename': ''},
            'monitor': {'enabled': True}}


def validate_local_config(config):
    from cloud_file_replace import validate_filename, _link
    from edge_copy import validate_url
    expected = default_config()
    if not isinstance(config, dict) or set(config) != set(expected):
        raise ValueError('本地Excel覆盖配置格式不正确')
    result = copy.deepcopy(config)
    for section, keys in expected.items():
        if not isinstance(result[section], dict) or set(result[section]) != set(keys):
            raise ValueError('本地Excel覆盖配置字段不正确：' + section)
    path = result['source']['local_file']
    if not isinstance(path, str) or not path.strip():
        raise ValueError('请选择要上传的本地Excel文件')
    path = Path(path.strip())
    if not path.is_absolute() or path.suffix.lower() != '.xlsx':
        raise ValueError('本地Excel必须是完整路径的 .xlsx 文件')
    # Existence is deliberately checked at execution, so a preceding queue
    # task can create the file before this task starts.
    result['source']['local_file'] = str(path)
    link = result['upload']['share_url']
    if not isinstance(link, str):
        raise ValueError('目标云文件分享链接必须为文本')
    link = link.strip()
    validate_url(link, '目标云文件分享链接')
    _link(link)
    result['upload']['share_url'] = link
    name = validate_filename(result['upload']['cloud_filename'])
    if not name:
        raise ValueError('请填写目标文件在“我的云文档”根目录显示的完整文件名（含 .xlsx）')
    result['upload']['cloud_filename'] = name
    if not isinstance(result['monitor']['enabled'], bool):
        raise ValueError('操作日志开关必须为布尔值')
    return result


def engine_config(config, sheet_name='Sheet1'):
    from web_app import _defaults
    full = _defaults()
    full['source']['format_file'] = config['source']['local_file']
    full['source']['sheet_name'] = sheet_name
    full['upload'].update(config['upload'], sheet_name=sheet_name)
    full['monitor'] = copy.deepcopy(config['monitor'])
    return full


def source_sheet(path):
    from lxml import etree
    source = Path(path)
    if not source.is_file():
        raise ValueError('本地Excel文件不存在，请确认前置任务已保存文件：' + str(source))
    try:
        with ZipFile(source) as book:
            root = etree.fromstring(book.read('xl/workbook.xml'),
                                    etree.XMLParser(resolve_entities=False, no_network=True))
            sheet = root.find('{http://schemas.openxmlformats.org/spreadsheetml/2006/main}sheets/'
                              '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}sheet')
            if sheet is None or not sheet.get('name'):
                raise ValueError('本地Excel文件没有工作表')
            return sheet.get('name')
    except (OSError, BadZipFile, KeyError, etree.XMLSyntaxError) as exc:
        raise ValueError('本地Excel无法读取或不是有效的 .xlsx 文件：' + str(source)) from exc


def local_message(message):
    return str(message).replace('表C', '本地Excel').replace('原表A文件', '目标云文件').replace('原表A', '目标云文件').replace('表A', '目标云文件')


class LocalCopierView:
    """Scope config writes to this feature; never change online-sync defaults."""
    def __init__(self, copier):
        self.copier = copier

    def __getattr__(self, name):
        return getattr(self.copier, name)

    def _write_config_file(self, config):
        atomic_json(Path(self.root) / 'local_upload_last.json', config)

    def set_sync_config(self, config):
        # The queue owns scheduling. Do not install a source-table watcher.
        pass

    def update_upload(self, **values):
        if 'message' in values:
            values['message'] = local_message(values['message'])
        return self.copier.update_upload(**values)


def run_local_upload(copier, config, *, completion='notice'):
    from cloud_file_replace import JOURNAL, replace_cloud_file, _link
    from sync_runner import _stop_requested, SyncStopped
    config = validate_local_config(config)
    if _stop_requested(copier):
        raise SyncStopped()
    if completion == 'notice':
        sheet = source_sheet(config['source']['local_file'])
        return replace_cloud_file(LocalCopierView(copier), engine_config(config, sheet), completion='notice')
    journal = Path(copier.root) / JOURNAL
    recovering = journal.exists()
    sheet = 'Sheet1'
    if recovering:
        record = json.loads(journal.read_text(encoding='utf-8'))
        if (record['state']['link'] != _link(config['upload']['share_url']) or
                record.get('filename') != config['upload']['cloud_filename']):
            raise ValueError('仍有其他云文件的覆盖待核验，请先使用原任务恢复核验；本次没有上传')
        sheet = (record.get('sheets') or ['Sheet1'])[0]
    else:
        sheet = source_sheet(config['source']['local_file'])
    result = replace_cloud_file(LocalCopierView(copier), engine_config(config, sheet))
    if recovering:
        if _stop_requested(copier):
            raise SyncStopped()
        # A verified pending upload satisfies this task only when the current
        # source still has exactly the recorded bytes. Never treat a different
        # file, missing file or a legacy record without this hash as uploaded.
        from values_workbook import file_hash
        current_hash = None
        try:
            current_hash = file_hash(config['source']['local_file'])
        except OSError:
            pass
        if current_hash and current_hash == record.get('source_sha256'):
            result['message'] = '上次上传已恢复核验完成；当前本地Excel与上次上传源文件完全一致，本任务已完成，无需重复上传。'
            return result
        # The journal might describe a different source file or older bytes.
        # Verification of that revision must not count as this task's upload.
        raise ValueError('上次云文件覆盖已恢复核验完成。本次本地Excel尚未重新上传；请核对文件后重新开始任务。')
    result['message'] = local_message(result['message'])
    return result


class LocalUploadMixin:
    """Route only the new operation through the existing worker's sync lane."""
    def submit_local_upload(self, config):
        config = validate_local_config(config)
        self._reset_stop()
        self._enqueue(('sync', {LOCAL_MARKER: config}), '选择本地Excel上传',
                      '正在读取本地Excel，准备备份并覆盖目标云文件…', upload=True)

    def _run_sync(self, config):
        if LOCAL_MARKER in config:
            return run_local_upload(self, config[LOCAL_MARKER])
        return super()._run_sync(config)
