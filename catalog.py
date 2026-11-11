"""Task definitions. Business operations live in the unchanged engines/ sources."""
from pathlib import Path
import sys

ROOT = Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent
ENGINES = {'modern': 'WPS 云文件同步', 'legacy': 'OLD 表格复制与上传'}
ACTIONS = {
    'modern': {
        'local_upload': ('选择本地Excel上传', '选择本地 XLSX；备份后覆盖目标云文件，显示“上传完成”即成功，不再下载校验。'),
        'sync': ('表C → 表A 同步', '下载完整表C、公式转保存值、备份并覆盖原云文件，回读校验。'),
        'download_c': ('下载表C', '仅下载完整表C到 engines/modern/DATA/表C.xlsx；下载后不自动上传。'),
        'download_b': ('下载表A历史快照', '下载表A并保留历史快照。'),
    },
    'legacy': {
        'copy': ('表A → 表A1 复制', '保留分批复制、隐藏列处理、条件筛选及本地格式设置。'),
        'upload': ('表A1 → 表D 上传', '保留指定区域、粘贴位置、格式、空白覆盖和原有校验。'),
    },
}
OPEN_ACTIONS = {'modern': [('open_c', '打开表C / 登录'), ('open_a', '打开表A / 登录')],
                'legacy': [('prepare', '打开表A / 登录'), ('prepare_upload', '打开表D / 登录')]}
SECTIONS = {'source': '来源设置', 'upload': '上传设置', 'local': '本地文件',
            'sync': '同步选项', 'filter': '筛选选项', 'monitor': '操作日志', 'schedule': '旧版定时参数'}
LABELS = {
    'share_url': '分享链接', 'sheet_name': '工作表名称', 'max_cells': '读取单元格上限',
    'scan_range': '扫描范围（留空为整表）', 'batch_rows': '每批行数', 'format_file': '表C格式文件',
    'format_sheet': '格式工作表', 'local_file': '本地 XLSX 文件', 'cloud_filename': '云端完整文件名',
    'batch_cells': '每批单元格上限', 'enabled': '启用', 'wait_seconds': '旧同步等待秒数',
    'max_pastes': '最多同步变更行（0不限）', 'primary_key_column': '主键列',
    'data_start_row': '数据起始行', 'watch_enabled': '旧版即时同步开关', 'watch_debounce': '旧版防抖秒数',
    'skip_columns': '跳过的列', 'sync_columns': '同步列', 'insert_enabled': '插入新记录',
    'start_cell': '起始单元格', 'legacy_scan': '旧检索方式', 'table_b_snapshot': '上传前保存表A快照',
    'header_row': '表头行', 'mirror_sheet': '镜像工作表', 'omitted_columns': '舍弃的隐藏列',
    'copy_mode': '复制范围模式', 'copy_range': '复制区域', 'copy_batch_rows': '复制每批行数',
    'one_shot': '一次复制', 'auto_hidden_columns': '自动识别隐藏列', 'paste_start': '表A1粘贴起点',
    'filter_enabled': '按列筛选', 'filter_column': '筛选列字母', 'filter_value': '筛选值',
    'destination': '表D粘贴起点 / 目标区域', 'overflow': '目标区域不足时',
    'skip_dimensions': '跳过行高列宽设置', 'blank_overwrite': '允许空白覆盖（清空目标区域）',
    'source_range': '表A1源区域（留空为整表）', 'copy_enabled': '旧版定时复制',
    'upload_enabled': '旧版定时上传', 'copy_minutes': '旧版复制间隔（分钟）', 'upload_minutes': '旧版上传间隔（分钟）',
}
CHOICES = {'copy_mode': {'all': '整表复制', 'selection': '指定区域'},
           'overflow': {'stop': '停止并报错', 'clip': '按目标区域裁剪'}}
