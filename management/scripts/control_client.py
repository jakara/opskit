#!/usr/bin/env python3
"""Spug 手动任务客户端：提交固定服务动作并等待真实执行结果。"""
import argparse
import json
import os
from pathlib import Path
import sys
import time
from uuid import uuid4


def execute(action, service, image=None, directory=Path('/opskit-control'), timeout=3400):
    if action not in ('start', 'stop', 'restart', 'upgrade', 'status') or service not in ('dbx', 'victorialogs'):
        raise ValueError('只允许操作本安装的 DBX 或 VictoriaLogs')
    heartbeat = json.loads((directory / 'status/agent.json').read_text())
    if time.time() - heartbeat['updated_at'] > 20:
        raise RuntimeError('宿主执行通道未运行，请在管理机重跑安装脚本')
    identifier = uuid4().hex
    request = {'id': identifier, 'action': action, 'service': service, 'created_at': time.time()}
    if image is not None:
        request['image'] = image
    path = directory / 'requests' / (identifier + '.json')
    temp = path.with_suffix('.tmp')
    with temp.open('x', encoding='utf-8') as stream:
        json.dump(request, stream)
    temp.chmod(0o644)  # 容器与宿主用户可能不同；目录只向 Spug 开放且请求不含凭据。
    os.replace(temp, path)
    print('已提交 %s %s，正在等待实际结果……' % (service, action), flush=True)
    deadline = time.monotonic() + timeout
    result_path = directory / 'results' / (identifier + '.json')
    while time.monotonic() < deadline:
        if result_path.exists():
            result = json.loads(result_path.read_text())
            if any(result.get(key) != request[key] for key in ('id', 'service', 'action')):
                raise RuntimeError('执行结果标识不符')
            print(json.dumps(result, ensure_ascii=False), flush=True)
            return 0 if result.get('ok') else 1
        time.sleep(1)
    raise RuntimeError('等待结果超时，请核对宿主 .workbench/control/results；不要立即重复升级')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('start', 'stop', 'restart', 'upgrade', 'status'))
    parser.add_argument('service', choices=('dbx', 'victorialogs'))
    parser.add_argument('--image')
    args = parser.parse_args()
    try:
        sys.exit(execute(args.action, args.service, args.image))
    except (ValueError, RuntimeError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
