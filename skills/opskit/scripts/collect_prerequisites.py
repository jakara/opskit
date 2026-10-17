#!/usr/bin/env python3
"""在隔离的目标架构龙蜥容器中收集、签名检查并断网验收 RPM 依赖。"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request


PYTHON_PACKAGES = ['python3.11']
RUNTIME_PACKAGES = ['docker-ce', 'docker-ce-cli', 'containerd.io',
                    'docker-buildx-plugin', 'docker-compose-plugin', 'iproute', 'procps-ng']
DOCKER_KEY_FINGERPRINT = '060A61C51B558A7F742B77AAC52FEB6B621E9F35'
COLLECT_SCRIPT = r'''set -euo pipefail
export LC_ALL=C
dnf -y --setopt=install_weak_deps=False install dnf-plugins-core createrepo_c curl gnupg2
mkdir -p /bundle/keys /bundle/rpms /bundle/verification /tmp/python-rpms /tmp/runtime-rpms
cp /etc/pki/rpm-gpg/* /bundle/keys/
if ! dnf -q list --available docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin >/dev/null 2>&1; then
  if [ -d /bundle/docker-source ]; then
    cp /bundle/docker-source/RPM-GPG-KEY-docker /bundle/keys/
    createrepo_c /bundle/docker-source
    docker_baseurl=file:///bundle/docker-source
  else
    curl --fail --location --proto '=https' --tlsv1.2 https://download.docker.com/linux/rhel/gpg -o /bundle/keys/RPM-GPG-KEY-docker
    docker_baseurl='https://download.docker.com/linux/rhel/8/$basearch/stable'
  fi
  fingerprint=$(gpg --batch --show-keys --with-colons /bundle/keys/RPM-GPG-KEY-docker | awk -F: '$1=="fpr" {print $10;exit}')
  [ "$fingerprint" = "060A61C51B558A7F742B77AAC52FEB6B621E9F35" ] || { echo 'Docker 签名公钥指纹不匹配' >&2; exit 1; }
  cat > /etc/yum.repos.d/opskit-docker.repo <<REPO
[opskit-docker]
name=Docker official RHEL 8 stable
baseurl=$docker_baseurl
enabled=1
gpgcheck=1
gpgkey=file:///bundle/keys/RPM-GPG-KEY-docker
REPO
fi
dnf -y --setopt=install_weak_deps=False download --resolve --alldeps --destdir /tmp/python-rpms python3.11
dnf -y --setopt=install_weak_deps=False download --resolve --alldeps --destdir /tmp/runtime-rpms docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin iproute procps-ng
# --alldeps 收集正向依赖；实际事务还可能需要同步升级已装软件的反向依赖
# （如 rpm-libs 对应的 python3-rpm/rpm-build-libs），必须同时纳入离线仓库。
dnf -y --setopt=install_weak_deps=False --downloadonly --downloaddir=/tmp/runtime-rpms install python3.11 docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin iproute procps-ng
cp /tmp/python-rpms/*.rpm /bundle/rpms/
printf '%s\n' /tmp/python-rpms/*.rpm | sed 's@.*/@@' > /bundle/python-packages.txt
cp /tmp/runtime-rpms/*.rpm /bundle/rpms/
for key in /bundle/keys/*; do rpmkeys --import "$key"; done
for package in /bundle/rpms/*.rpm; do
  signature=$(rpmkeys --checksig "$package")
  printf '%s\n' "$signature"
  [[ "$signature" == *'digests signatures OK' ]] || { echo 'RPM lacks a trusted signature' >&2; exit 1; }
done
createrepo_c /bundle/rpms
'''

VERIFY_SCRIPT = r'''set -euo pipefail
export LC_ALL=C
for key in /bundle/keys/*; do rpmkeys --import "$key"; done
for package in /bundle/rpms/*.rpm; do
  signature=$(rpmkeys --checksig "$package")
  printf '%s\n' "$signature"
  [[ "$signature" == *'digests signatures OK' ]] || { echo 'RPM lacks a trusted signature' >&2; exit 1; }
done
dnf -y --disablerepo='*' --repofrompath=opskit-offline,file:///bundle/rpms --setopt=opskit-offline.gpgcheck=1 --setopt=opskit-offline.repo_gpgcheck=0 --setopt=opskit-offline.module_hotfixes=1 --setopt=install_weak_deps=False install python3.11 docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin iproute procps-ng
python3.11 -c 'import sys; assert sys.version_info >= (3,9); print("OPSKIT_PYTHON="+sys.version.split()[0])'
printf 'OPSKIT_DOCKER='; docker --version
printf 'OPSKIT_COMPOSE='; docker compose version --short
ss --version
ps --version
'''


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def run(command, *, log=None):
    """不通过宿主 shell，不挂载 Docker socket 或业务仓库。"""
    if log:
        with log.open('w', encoding='utf-8') as stream:
            result = subprocess.run(command, stdout=stream, stderr=subprocess.STDOUT, timeout=1800)
        if result.returncode:
            tail = log.read_text(encoding='utf-8', errors='replace')[-6000:]
            raise RuntimeError('一次性龙蜥容器执行失败；日志：%s\n%s' % (log, tail))
        return log.read_text(encoding='utf-8', errors='replace')
    return subprocess.check_output(command, text=True, encoding='utf-8', timeout=1800)


def marker(output, name):
    for line in output.splitlines():
        if line.startswith(name + '='):
            return line.partition('=')[2].strip()
    raise RuntimeError('断网验收缺少证据字段：' + name)


def fetch_official_docker_cache(stage, arch):
    """仅在容器 TLS 路径故障时由制包端取官方文件，签名仍在目标容器核验。"""
    rpm_arch = {'amd64': 'x86_64', 'arm64': 'aarch64'}[arch]
    base = 'https://download.docker.com/linux/rhel/8/%s/stable/Packages/' % rpm_arch
    cache = stage / 'docker-source'
    cache.mkdir()

    def fetch(url, path):
        with urllib.request.urlopen(url, timeout=90) as response, path.open('wb') as stream:
            if not response.geturl().startswith('https://'):
                raise RuntimeError('官方 Docker 下载被重定向至非 HTTPS 地址')
            shutil.copyfileobj(response, stream)

    index = stage / 'verification' / 'docker-official-index.html'
    fetch(base, index)
    listing = index.read_text(encoding='utf-8')
    downloaded = []
    for package in RUNTIME_PACKAGES[:5]:
        pattern = r'href="(' + re.escape(package) + r'-([0-9]+(?:\.[0-9]+)*)-([0-9]+)\.el8\.' + rpm_arch + r'\.rpm)"'
        candidates = re.findall(pattern, listing)
        if not candidates:
            raise RuntimeError('Docker 官方目录缺少目标包：' + package)
        filename, _, _ = max(candidates, key=lambda value: (tuple(map(int, value[1].split('.'))), int(value[2])))
        fetch(base + filename, cache / filename)
        downloaded.append({'url': base + filename, 'sha256': sha256(cache / filename)})
    fetch('https://download.docker.com/linux/rhel/gpg', cache / 'RPM-GPG-KEY-docker')
    (stage / 'verification' / 'docker-official-downloads.json').write_text(json.dumps(downloaded, indent=2) + '\n', encoding='utf-8')


def collect(target, output):
    if target.get('os') != 'anolis' or target.get('version') not in ('8.9', '8.10') or target.get('arch') not in ('amd64', 'arm64'):
        raise ValueError('仅支持 Anolis 8.9/8.10、amd64/arm64')
    output = Path(output).resolve()
    if output.exists() and any(output.iterdir()):
        raise ValueError('输出目录必须为空，避免覆盖已有交付依赖：' + str(output))
    output.parent.mkdir(parents=True, exist_ok=True)
    image = 'registry.openanolis.cn/openanolis/anolisos:' + target['version']
    platform = 'linux/' + target['arch']
    if not shutil.which('docker'):
        raise RuntimeError('制包机需要已启动的 Docker；本脚本不会改动宿主依赖或软件源')
    run(['docker', 'pull', '--platform', platform, image])
    inspect = json.loads(run(['docker', 'image', 'inspect', image]))[0]
    if inspect['Architecture'] != target['arch'] or inspect.get('Os', '').lower() != 'linux':
        raise RuntimeError('镜像实际平台与目标不一致')
    stage = Path(tempfile.mkdtemp(prefix='.' + output.name + '-collect-', dir=str(output.parent)))
    (stage / 'verification').mkdir()
    try:
        base = ['docker', 'run', '--rm', '--platform', platform, '--mount', 'type=bind,src=%s,dst=/bundle' % stage]
        download_transport = 'container-https'
        collection_log = stage / 'verification' / 'collection.log'
        try:
            run(base + [image, 'bash', '-c', COLLECT_SCRIPT], log=collection_log)
        except RuntimeError:
            failed_log = collection_log.read_text(encoding='utf-8', errors='replace') if collection_log.exists() else ''
            if 'download.docker.com' not in failed_log or 'curl:' not in failed_log:
                raise
            collection_log.rename(stage / 'verification' / 'container-network-failure.log')
            print('容器到 Docker 官方源的 TLS 路径不可用，改由制包机 HTTPS 下载官方 RPM；目标容器继续核验签名并解析依赖。', file=sys.stderr)
            fetch_official_docker_cache(stage, target['arch'])
            download_transport = 'host-https-verified-container'
            run(base + [image, 'bash', '-c', COLLECT_SCRIPT], log=collection_log)
        shutil.rmtree(stage / 'docker-source', ignore_errors=True)
        # 第二个容器从同一原始镜像启动，不继承下载容器安装的任何软件；禁用网络。
        verify = run(base + ['--network', 'none', image, 'bash', '-c', VERIFY_SCRIPT], log=stage / 'verification' / 'offline-install.log')
        python_version = marker(verify, 'OPSKIT_PYTHON')
        docker_version = marker(verify, 'OPSKIT_DOCKER')
        compose_version = marker(verify, 'OPSKIT_COMPOSE')
        if tuple(int(x) for x in python_version.split('.')[:2]) < (3, 9):
            raise RuntimeError('断网验收 Python 版本低于 3.9')
        python_files = set((stage / 'python-packages.txt').read_text().splitlines())
        package_files = sorted((stage / 'rpms').glob('*.rpm'))
        if not package_files or not (stage / 'rpms' / 'repodata' / 'repomd.xml').is_file():
            raise RuntimeError('未获得完整 RPM 本地仓库')
        bootstrap = Path(__file__).resolve().parents[1] / 'assets' / 'site' / 'offline-bootstrap.sh'
        with (stage / 'bootstrap.sh').open('w', encoding='utf-8', newline='\n') as stream:
            stream.write(bootstrap.read_text(encoding='utf-8-sig'))
        (stage / 'bootstrap.sh').chmod(0o755)
        (stage / 'target.txt').write_text('\n'.join((target['os'], target['version'], target['arch'])) + '\n', encoding='utf-8')
        manifest = {
            'schema_version': 1, 'target': target,
            'files': [{'path': p.relative_to(stage).as_posix(), 'sha256': sha256(p), 'group': 'python' if p.name in python_files else 'runtime'} for p in package_files],
            'keys': [{'path': p.relative_to(stage).as_posix(), 'sha256': sha256(p)} for p in sorted((stage / 'keys').iterdir()) if p.is_file()],
            'repository_files': [{'path': p.relative_to(stage).as_posix(), 'sha256': sha256(p)} for p in sorted((stage / 'rpms' / 'repodata').iterdir()) if p.is_file()],
            'packages': {'python': PYTHON_PACKAGES, 'runtime': RUNTIME_PACKAGES},
            'verification': {'offline_install': True, 'network': 'none', 'container_image': image, 'image_id': inspect['Id'], 'platform': platform, 'python_version': python_version, 'docker_version': docker_version, 'compose_version': compose_version, 'docker_download_transport': download_transport, 'scope': '干净目标架构容器内的签名校验、断网 RPM 事务和 CLI；未验证 systemd、Docker daemon 或真实 VM', 'log': 'verification/offline-install.log', 'commands': ['rpmkeys --checksig 全部 RPM', 'dnf --disablerepo=* 本地签名仓库安装', 'python3.11 >= 3.9', 'docker --version', 'docker compose version', 'ss --version', 'ps --version']},
            'sources': ['https://openanolis.cn/anolisos', 'https://docs.docker.com/engine/install/rhel/'],
        }
        (stage / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        checksum_files = sorted(p for p in stage.rglob('*') if p.is_file())
        (stage / 'SHA256SUMS').write_text(''.join('%s  %s\n' % (sha256(p), p.relative_to(stage).as_posix()) for p in checksum_files), encoding='utf-8')
        if output.exists():
            output.rmdir()
        stage.rename(output)
        return manifest
    except Exception:
        # 保留失败日志便于定位，失败目录没有可交付 manifest。
        (stage / 'manifest.json').unlink(missing_ok=True)
        print('失败证据保留于：' + str(stage), file=sys.stderr)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--os', choices=['anolis'], required=True)
    parser.add_argument('--version', choices=['8.9', '8.10'], required=True)
    parser.add_argument('--arch', choices=['amd64', 'arm64'], required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        manifest = collect({'os': args.os, 'version': args.version, 'arch': args.arch}, args.output)
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as error:
        parser.exit(1, '依赖收集失败：%s\n' % error)
    print('离线依赖已通过断网验证：%d 个 RPM，Python %s，输出 %s' % (len(manifest['files']), manifest['verification']['python_version'], args.output))


if __name__ == '__main__':
    main()
