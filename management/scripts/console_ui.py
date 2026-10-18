"""共享的 Rich 安装界面；依赖随包核验加载，不修改宿主 Python。"""
import hashlib
import json
from pathlib import Path
import sys

_vendor = Path(__file__).resolve().parent / 'vendor'
try:
    for _item in json.loads((_vendor / 'manifest.json').read_text(encoding='utf-8')):
        _wheel = _vendor / _item['filename']
        if _wheel.name != _item['filename'] or hashlib.sha256(_wheel.read_bytes()).hexdigest() != _item['sha256']:
            raise ValueError('invalid wheel')
        sys.path.insert(0, str(_wheel))
except (OSError, ValueError, KeyError, TypeError):
    raise SystemExit('安装界面资源缺失或校验失败，请重新解压完整 OpsKit 安装包。') from None

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text


def console():
    return Console(highlight=False, markup=False)


def banner(scope):
    content = Text('OpsKit', style='bold #68b89a')
    content.append('  /  ' + scope + '\n', style='bold')
    content.append('应用 · 数据库 · 日志 · 交付文档', style='dim')
    console().print(Panel(content, box=box.ROUNDED, border_style='#417863', padding=(1, 2)))


def step(number, total, title, description=''):
    console().print()
    console().rule(Text(f'{number:02d} / {total:02d}  {title}', style='bold #68b89a'), align='left', style='#417863')
    if description:
        note(description)


def note(message, style='dim'):
    console().print(Text(str(message), style=style))


def ask(label, default, validator, secret=False):
    hint = ('回车自动生成密码' if default else '填写管理端发放的密码') if secret else str(default)
    prompt = Text.assemble((label, 'bold'), (f' [{hint}]：', 'dim'))
    while True:
        value = console().input(prompt, password=secret and sys.stdin.isatty()).strip() or str(default)
        if not sys.stdin.isatty():
            console().print()
        if validator(value):
            return value
        note('输入无效，请按提示重新填写。', 'yellow')


def summary(title, rows):
    table = Table.grid(padding=(0, 2), expand=False)
    table.add_column(style='dim', no_wrap=True)
    table.add_column(overflow='fold')
    for label, value in rows:
        table.add_row(Text(str(label)), Text(str(value)))
    console().print(Panel(table, title=Text(title, style='bold'), box=box.ROUNDED, border_style='#68b89a', padding=(1, 2)))


def error(message):
    Console(stderr=True, highlight=False, markup=False).print(Text(str(message), style="bold red"))


def modules():
    banner('安装向导')
    summary('选择安装模块', [('1', '管理中心（Hub 及完整运维工具）'),
                              ('2', '日志采集端（Fluent Bit，每台业务机安装一次）'),
                              ('3', '精简管理端')])


if __name__ == '__main__':
    modules()
