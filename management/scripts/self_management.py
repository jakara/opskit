#!/usr/bin/env python3
"""初始化内置服务登记、健康巡检及数据库连接，结果不包含凭据。"""
import json
import subprocess
import time

from common import ROOT, save


def spug_run(code, values=None):
    result = subprocess.run(
        ['docker', 'compose', 'exec', '-T', '-w', '/data/spug/spug_api', 'spug', 'python3', '-c', code],
        cwd=ROOT, input=json.dumps(values) if values is not None else '', text=True,
        capture_output=True, timeout=60)
    if result.returncode:
        raise RuntimeError('Spug 自管理初始化或巡检未通过，请检查容器日志；配置和备份已保留')
    try:
        return json.loads(result.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise RuntimeError('Spug 初始化没有返回有效结果') from None


def initialize(v):
    print('等待 Spug 调度和监控进程启动（最多 4 分钟）……')
    deadline = time.monotonic() + 240
    while True:
        result = subprocess.run(['docker', 'compose', 'exec', '-T', 'spug', 'supervisorctl', 'status'],
                                cwd=ROOT, text=True, capture_output=True, timeout=20)
        lines = result.stdout.splitlines()
        if all(any(line.startswith(name + ' ') and 'RUNNING' in line for line in lines)
               for name in ('spug-monitor', 'spug-scheduler', 'spug-worker')):
            break
        if time.monotonic() >= deadline:
            raise RuntimeError('Spug 监控或调度进程未就绪，请检查启动日志后重跑')
        time.sleep(2)
    spug = spug_run((ROOT / 'scripts/spug_bootstrap.py').read_text(encoding='utf-8'),
                    {key: v[key] for key in ('OPS_PUBLIC_SCHEME', 'OPS_PUBLIC_HOST', 'OPS_PUBLIC_PORT', 'SPUG_ADMIN_USER', 'DBX_IMAGE', 'VICTORIALOGS_IMAGE')})
    control = subprocess.run(['docker', 'compose', 'exec', '-T', 'spug', 'python3',
                              '/opt/opskit/control_client.py', 'status', 'dbx'],
                             cwd=ROOT, capture_output=True, text=True, timeout=30)
    if control.returncode:
        raise RuntimeError('Spug 宿主执行通道未就绪，请核对目录权限和发现进程后重跑')
    # 立即运行同一只读检查；定时任务仍由 Spug 自身每 60 秒执行。
    from spug_bootstrap import HEALTH_COMMAND
    probe = """import json, subprocess
r = subprocess.run(['python3', '-c', %r], capture_output=True, text=True, timeout=45)
print(json.dumps({'healthy': r.returncode == 0, 'services': [json.loads(line) for line in r.stdout.splitlines()]}))
""" % HEALTH_COMMAND
    if len(spug.get('tasks', [])) == 1 and not spug.get('conflicts'):
        deadline = time.monotonic() + 90
        while True:
            health = spug_run(probe)
            if health['healthy']:
                break
            if time.monotonic() >= deadline:
                failed = ', '.join(item['service'] for item in health['services'] if not item['healthy'])
                raise RuntimeError('内置服务健康巡检未通过：' + failed + '；配置已保留，请排查后重跑')
            time.sleep(2)
    else:
        health = {'healthy': False, 'services': [], 'preserved_conflicts': True}
        print('Spug 中已有同名人工配置，已保留；请在 WebUI 核对冲突项。')
    from dbx_bootstrap import initialize as initialize_dbx
    dbx = initialize_dbx(v)
    result = {'spug': spug, 'health': health, 'dbx': dbx, 'control_channel': True}
    save(ROOT / '.workbench/self-management.json', json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print('已初始化 Spug 健康巡检及 DBX/日志服务的手动启停、重启和升级任务；已有配置保留。')
    print('VictoriaLogs 日志使用 LogsQL 查询；DBX 预置 Spug MariaDB，不创建不兼容的日志数据库连接。')
    return result
