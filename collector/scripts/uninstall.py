#!/usr/bin/env python3
"""按 Compose 标签及安装路径卸载；清空前保存并核验本地文件和数据卷。"""
import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tarfile
import time

from common import ROOT, powershell, save

CONTAINER_FORMAT = ('{"Id":{{json .Id}},"Name":{{json .Name}},"Labels":{{json .Config.Labels}},'
                    '"Mounts":{{json .Mounts}}}')


def docker(*arguments, **kwargs):
    return subprocess.run(['docker', *arguments], check=True, **kwargs)


def output(*arguments):
    try:
        return docker(*arguments, capture_output=True, text=True, encoding='utf-8').stdout.strip()
    except subprocess.CalledProcessError as exc:
        raise RuntimeError('Docker 查询失败，请确认已启动且当前账户有权限：' + (exc.stderr or '').strip()) from exc


def same_path(first, second):
    return os.path.normcase(os.path.realpath(first)) == os.path.normcase(os.path.realpath(second))


def owner(container, roots):
    labels = container.get('Labels') or {}
    directory = labels.get('com.docker.compose.project.working_dir', '')
    files = labels.get('com.docker.compose.project.config_files', '').split(',')
    for root in roots:
        known = [root / name for name in ('compose.yaml', 'hub.compose.yaml', 'legacy.compose.yaml')]
        if directory and same_path(directory, root) and any(same_path(file, path) for file in files for path in known if file):
            return root
    return None


def inventory(roots, hub_only=False):
    endpoint = os.environ.get('DOCKER_HOST') if not os.environ.get('DOCKER_CONTEXT') else None
    if not endpoint:
        endpoint = output('context', 'inspect', '--format', '{{(index .Endpoints "docker").Host}}')
    if not endpoint.startswith(('unix://', 'npipe://')):
        raise RuntimeError('卸载只允许本机 Docker；请先切回本机 Docker 上下文。')
    if output('info', '--format', '{{.OSType}}') != 'linux':
        raise RuntimeError('请切换到安装 OpsKit 时使用的本机 Linux 容器引擎后再卸载。')
    ids = output('ps', '-aq', '--filter', 'label=com.docker.compose.project').split()
    containers = [json.loads(line) for line in output('inspect', '--format', CONTAINER_FORMAT, *ids).splitlines()] if ids else []
    owned = [c for c in containers if owner(c, roots)]
    if hub_only:
        owned = [c for c in owned if any(same_path(file, roots[0] / 'hub.compose.yaml')
                                        for file in c['Labels']['com.docker.compose.project.config_files'].split(','))]
    projects = {c['Labels']['com.docker.compose.project'] for c in owned}
    selected = {c['Id'] for c in owned}
    for c in containers:
        if c['Labels']['com.docker.compose.project'] in projects and c['Id'] not in selected:
            raise RuntimeError('同名 Compose 项目还包含其他目录或安装范围的容器，已拒绝卸载：' + c['Labels']['com.docker.compose.project'])
    volumes, networks, orphans = [], [], []
    for project in sorted(projects):
        names = output('volume', 'ls', '-q', '--filter', 'label=com.docker.compose.project=' + project).splitlines()
        for name in names:
            mounts = [(c, m) for c in owned for m in c.get('Mounts', []) if m.get('Type') == 'volume' and m.get('Name') == name]
            # 无法用现有容器读取的孤立卷没有可核验备份，不猜测其内容或归属。
            if not mounts:
                orphans.append(name)
                continue
            users = output('ps', '-aq', '--filter', 'volume=' + name).split()
            if any(not any(c['Id'].startswith(user) for c in owned) for user in users):
                raise RuntimeError(f'数据卷 {name} 还被其他容器使用，已拒绝卸载。')
            c, mount = mounts[0]
            volumes.append({'name': name, 'container': c['Id'], 'destination': mount['Destination']})
        networks.extend(output('network', 'ls', '-q', '--filter', 'label=com.docker.compose.project=' + project).split())
    for network in networks:
        members = json.loads(output('network', 'inspect', '--format', '{{json .Containers}}', network)) or {}
        if any(member not in selected for member in members):
            raise RuntimeError(f'网络 {network} 还连接了其他容器，已拒绝卸载。')
    remaining = [c for c in containers if owner(c, roots) and c['Id'] not in selected]
    return {'containers': owned, 'volumes': volumes, 'networks': networks, 'remaining': remaining, 'orphans': orphans}


def digest(path):
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(chunk)
    return result.hexdigest()


def copy_verified(source, target):
    if source.is_symlink():
        raise RuntimeError(f'拒绝跟随运行数据中的软链接：{source}')
    if source.is_dir():
        target.mkdir(mode=0o700, parents=True, exist_ok=True)
        for child in source.iterdir():
            copy_verified(child, target / child.name)
    else:
        save(target, '')
        with source.open('rb') as original, target.open('wb') as copied:
            shutil.copyfileobj(original, copied)
            copied.flush()
            os.fsync(copied.fileno())
        if digest(source) != digest(target):
            raise RuntimeError(f'卸载备份校验失败：{source}')


def process_command(pid):
    if sys.platform == 'win32':
        return powershell("[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false); "
                          f"(Get-CimInstance Win32_Process -Filter 'ProcessId = {pid}').CommandLine",
                          capture_output=True, timeout=15).stdout
    return subprocess.run(['ps', '-p', str(pid), '-o', 'command='], capture_output=True,
                          text=True, check=True, timeout=10).stdout


def stop_process(root, directory, script):
    pidfile = directory / ('discovery.pid' if script == 'discovery.py' else 'server.pid')
    if not pidfile.exists():
        return
    try:
        pid = int(pidfile.read_text(encoding='utf-8').strip())
        if pid <= 0:
            return
        command = process_command(pid)
    except (ValueError, subprocess.CalledProcessError):
        return  # 旧 PID 已不存在；不使用进程名批量终止。
    expected = root / 'portal' / script
    if str(expected) not in command or str(directory) not in command:
        print(f'保留 PID {pid}：其命令不属于当前 OpsKit 目录。')
        return
    os.kill(pid, signal.SIGTERM)
    for _ in range(50):
        try:
            current = process_command(pid)
            if str(expected) not in current or str(directory) not in current:
                return
        except subprocess.CalledProcessError:
            return
        time.sleep(0.1)
    raise RuntimeError(f'发现进程 {pid} 未退出，已停止后续清空操作，请核对后重试。')


def service_file(root):
    name = 'opskit-discovery-' + hashlib.sha256(str(root).encode()).hexdigest()[:8] + '.service'
    return Path('/etc/systemd/system') / name


def admin(*arguments):
    prefix = [] if os.geteuid() == 0 else ['sudo', '--']
    subprocess.run(prefix + list(arguments), check=True)


def stop_discovery(root, backup):
    unit = service_file(root)
    if sys.platform.startswith('linux') and unit.exists():
        if str(root / 'portal/discovery.py') not in unit.read_text(encoding='utf-8'):
            raise RuntimeError(f'发现服务已自定义为其他命令，已保留：{unit}')
        copy_verified(unit, backup / 'systemd' / unit.name)
        admin('systemctl', 'disable', '--now', unit.name)
        admin('rm', '--', str(unit))
        admin('systemctl', 'daemon-reload')
    stop_process(root, root / '.workbench/discovery', 'discovery.py')
    for directory in ('hub', 'development'):
        stop_process(root, root / '.workbench' / directory, 'server.py')


def local_paths(root, hub_only=False, discovery_in_use=False):
    if hub_only:
        names = ['.workbench/hub.env', '.workbench/hub', '.workbench/development']
        if not discovery_in_use:
            names.append('.workbench/discovery')
    else:
        names = ['.env', '.env.install-progress', 'collector.env', 'outer-nginx.conf.example', '.workbench', '.backups']
    return [root / name for name in names if (root / name).exists() or (root / name).is_symlink()]


def uninstall(roots, hub_only=False, purge=False, yes=False, dry_run=False):
    plan = inventory(roots, hub_only)
    if purge and plan['orphans']:
        raise RuntimeError('存在无法直接备份的孤立卷，请先恢复本套件容器挂载后再清空：' + '、'.join(plan['orphans']))
    paths = [(root, path) for root in roots for path in local_paths(root, hub_only, bool(plan['remaining']))] if purge else []
    services = [root for root in roots if (root / 'hub.compose.yaml').exists() and not plan['remaining']]
    print('即将卸载以下本目录的 OpsKit 资源：')
    print('  安装目录：' + '、'.join(map(str, roots)))
    print('  容器：' + ('、'.join(c['Name'].lstrip('/') for c in plan['containers']) or '无'))
    print('  网络：' + ('、'.join(plan['networks']) or '无'))
    print('  数据：' + ('备份核验后清空配置、收藏及 ' + str(len(plan['volumes'])) + ' 个数据卷' if purge else '保留配置、收藏及全部数据卷'))
    if paths:
        print('  清理路径：' + '、'.join(str(path) for _, path in paths))
    if dry_run:
        print('仅预览，未停止进程、备份或删除任何文件。')
        return None
    if not yes and input('确认卸载请输入 yes；其他输入取消：').strip().lower() != 'yes':
        print('已取消，未作更改。')
        return None
    base = roots[0].parent if len(roots) > 1 or (roots[0].parent / 'install.sh').exists() else roots[0]
    backup = base / '.uninstall-backups' / datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    if paths or (purge and plan['volumes']) or any(service_file(root).exists() for root in services):
        backup.mkdir(mode=0o700, parents=True)
    for root in services:
        stop_discovery(root, backup)
    if plan['containers']:
        docker('stop', '-t', '30', *(c['Id'] for c in plan['containers']))
    for root, path in paths:
        copy_verified(path, backup / 'local' / root.name / path.relative_to(root))
    if purge:
        for volume in plan['volumes']:
            archive = backup / 'volumes' / (volume['name'] + '.tar')
            save(archive, '')
            with archive.open('wb') as stream:
                docker('cp', volume['container'] + ':' + volume['destination'].rstrip('/') + '/.', '-', stdout=stream)
                stream.flush()
                os.fsync(stream.fileno())
            with tarfile.open(archive, 'r:') as tar:
                for member in tar:
                    if member.isfile():
                        with tar.extractfile(member) as content:
                            while content.read(1024 * 1024):
                                pass
    if backup.exists():
        hashes = {str(p.relative_to(backup)): digest(p) for p in backup.rglob('*') if p.is_file()}
        save(backup / 'manifest.json', json.dumps({'sha256': hashes,
             'containers': [c['Name'] for c in plan['containers']], 'purged': purge}, ensure_ascii=False, indent=2))
        for name, expected in hashes.items():
            if digest(backup / name) != expected:
                raise RuntimeError('卸载备份最终核验失败，尚未删除容器或数据。')
    if plan['containers']:
        docker('rm', *(c['Id'] for c in plan['containers']))
    for network in plan['networks']:
        docker('network', 'rm', network)
    if purge:
        for volume in plan['volumes']:
            docker('volume', 'rm', volume['name'])
        for _, path in paths:
            shutil.rmtree(path) if path.is_dir() and not path.is_symlink() else path.unlink()
    print('卸载完成；Docker、Python、镜像、源码及其他应用已保留。')
    print('仅处理有当前安装路径标签可确认归属的资源；没有容器可证明归属的旧孤立卷不会被猜测删除。')
    if backup.exists():
        print(f'可恢复备份（含原凭据，请妥善保管）：{backup}')
    return backup if backup.exists() else None


def main():
    parser = argparse.ArgumentParser(description='OpsKit 本地卸载；仅操作当前安装目录的 Compose 资源')
    parser.add_argument('--all', action='store_true', help='同时卸载同工具包的管理端和采集端')
    parser.add_argument('--hub-only', action='store_true', help='仅卸载独立 Hub 与 NPM')
    parser.add_argument('--purge-data', action='store_true', help='备份并核验后清空配置、收藏和数据卷，用于重新安装')
    parser.add_argument('--yes', action='store_true', help='跳过交互确认')
    parser.add_argument('--dry-run', action='store_true', help='只预览，不修改任何资源')
    args = parser.parse_args()
    if args.all and args.hub_only:
        parser.error('--all 和 --hub-only 不能同时使用')
    roots = [ROOT]
    sibling = ROOT.parent / 'collector'
    if args.all and (ROOT / 'hub.compose.yaml').exists() and (sibling / 'compose.yaml').exists():
        roots.append(sibling.resolve())
    uninstall(roots, args.hub_only, args.purge_data, args.yes, args.dry_run)


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, tarfile.TarError, EOFError) as exc:
        print(f'卸载未完成：{exc}', file=sys.stderr)
        sys.exit(1)
