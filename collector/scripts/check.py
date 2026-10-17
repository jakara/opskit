#!/usr/bin/env python3
"""检查采集端配置、运行状态和管理端连通性。"""
import json
import subprocess
import sys

from common import ROOT, load_env, run
from install import check_connection, validate

if __name__ == '__main__':
    try:
        values = load_env(ROOT / '.env')
        validate(values)
        run(['docker', 'compose', 'config', '--quiet'])
        state = run(['docker', 'compose', 'ps', '--all', '--format', 'json'], capture_output=True, text=True).stdout
        states = json.loads(state) if state.strip().startswith('[') else [json.loads(line) for line in state.splitlines() if line.strip()]
        if len(states) != 1 or states[0].get('State') != 'running':
            raise RuntimeError('Fluent Bit 未运行，请检查容器日志')
        check_connection(values)
        print('通过：采集端容器运行中；文件读取及日志可检索性仍需追加记录后确认。')
    except (KeyError, ValueError, RuntimeError, OSError, subprocess.CalledProcessError) as exc:
        print(f'检查未通过：{exc}', file=sys.stderr)
        sys.exit(1)
