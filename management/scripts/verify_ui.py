#!/usr/bin/env python3
"""部署前验证固定上游身份与预编译子路径前端；不访问现有容器或数据。"""
import hashlib
import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / 'ui'


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def docker(*args):
    try:
        return subprocess.run(['docker', *args], check=True, capture_output=True, timeout=60).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError('无法只读验证前端所需镜像，请检查 Docker 和本地镜像') from exc


def verify(v, hub_only=False):
    versions = json.loads((ASSETS / 'versions.json').read_text())
    manifests = json.loads((ASSETS / 'static-hashes.json').read_text())
    for name in ('npm',) if hub_only else ('npm', 'spug'):
        expected = manifests[name]
        folder = ASSETS / name
        actual = {str(path.relative_to(folder)) for path in folder.rglob('*') if path.is_file()}
        if actual != set(expected):
            raise RuntimeError(f'{name} 前端文件清单不符，拒绝部署')
        for relative, digest in expected.items():
            if sha256((folder / relative).read_bytes()) != digest:
                raise RuntimeError(f'{name} 前端校验和不符：{relative}')

        image = v[name.upper() + '_IMAGE']
        spec = versions[name]
        metadata = json.loads(docker('image', 'inspect', image))[0]
        config = metadata.get('Config') or {}
        if name == 'npm':
            env = dict(value.split('=', 1) for value in config.get('Env', []) if '=' in value)
            identity = env.get('NPM_BUILD_VERSION'), env.get('NPM_BUILD_COMMIT')
        else:
            labels = config.get('Labels') or {}
            identity = labels.get('org.opencontainers.image.version'), labels.get('org.opencontainers.image.revision')
        if identity != (spec['version'], spec['revision']):
            raise RuntimeError(f'{name} 镜像版本或 revision 未适配，拒绝部署')

        digests = {value.rsplit('@', 1)[-1] for value in metadata.get('RepoDigests', [])}
        digests.add((metadata.get('Descriptor') or {}).get('digest'))
        if spec['image_index_digest'] not in digests:
            # 离线 save/load 可能丢 RepoDigests，以原始入口字节核验同一前端构建。
            upstream = docker('run', '--rm', '--network', 'none', '--read-only', '--entrypoint', 'cat',
                              image, spec['mount'] + '/index.html')
            if sha256(upstream) != spec['upstream_index_sha256']:
                raise RuntimeError(f'{name} 镜像摘要及原始前端均未适配，拒绝部署')
        print(f'通过：{name} 固定版本和子路径前端完整性')


if __name__ == '__main__':
    from common import load_env
    try:
        values = load_env(ROOT / '.env')
        verify(values, hub_only='--hub-only' in sys.argv or values.get('HUB_ONLY') == 'true')
    except (KeyError, ValueError, RuntimeError, OSError) as exc:
        print(f'前端验证未通过：{exc}', file=sys.stderr)
        sys.exit(1)
