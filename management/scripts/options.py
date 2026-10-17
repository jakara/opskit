"""记住安装入口选项；只保存模块和目录，不保存运行参数或凭据。"""
import argparse
import json
from pathlib import Path
import sys

from common import save


MODULES = {'1': '1', 'management': '1', 'production': '1',
           '2': '2', 'collector': '2', '3': '3', 'hub': '3', 'development': '3'}


def read(source):
    path = source / '.install-options.json'
    values = json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else {}
    if not isinstance(values, dict) or set(values) - {'module', 'directory'} or any(
            not isinstance(value, str) or any(ord(char) < 32 for char in value) for value in values.values()):
        raise ValueError('安装选项记录无效：' + str(path))
    if values.get('module', '1') not in ('1', '2', '3'):
        raise ValueError('安装模块记录无效：' + str(path))
    return values


def remember(source, **values):
    save(source / '.install-options.json', json.dumps(dict(read(source), **values), ensure_ascii=False, indent=2) + '\n')


def main():
    parser = argparse.ArgumentParser(description='读取或记录 OpsKit 安装模块')
    parser.add_argument('action', choices=('read', 'write'))
    parser.add_argument('source', type=Path)
    parser.add_argument('module', nargs='?', choices=tuple(MODULES))
    args = parser.parse_args()
    if args.action == 'read':
        print(read(args.source).get('module', '1'))
    else:
        if not args.module:
            parser.error('缺少安装模块')
        remember(args.source, module=MODULES[args.module])


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError) as exc:
        print('无法保存或读取安装选项：' + str(exc), file=sys.stderr)
        sys.exit(1)
