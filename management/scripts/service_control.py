#!/usr/bin/env python3
"""宿主受限执行通道；仅处理本安装的 DBX、VictoriaLogs，不提供任意命令。"""
import hashlib
from contextlib import closing
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
import tarfile
import threading
import time

from common import ROOT, env_text, image_failure, load_env, protect_private, save

SERVICES = {'dbx': 'DBX_IMAGE', 'victorialogs': 'VICTORIALOGS_IMAGE'}
ACTIONS = {'start', 'stop', 'restart', 'upgrade', 'status'}
IDENTIFIER = re.compile(r'[a-f0-9]{32}')
IMAGE = re.compile(r'[a-zA-Z0-9][a-zA-Z0-9._:/@-]{0,511}')
INFO = ('{"Id":{{json .Id}},"Image":{{json .Image}},"reference":{{json .Config.Image}},'
        '"Labels":{{json .Config.Labels}},"Mounts":{{json .Mounts}},"Running":{{json .State.Running}}}')
RESTORE = '''from pathlib import Path, PurePosixPath
import shutil, sys, tarfile
root = Path('/restore')
def safe(member, path):
    name = PurePosixPath(member.name)
    if name.is_absolute() or '..' in name.parts or '\\\\' in member.name or not (member.isfile() or member.isdir()):
        raise ValueError('unsafe backup member')
    return member
for child in root.iterdir():
    if child.is_symlink() or not child.is_dir(): child.unlink()
    else: shutil.rmtree(child)
with tarfile.open(fileobj=sys.stdin.buffer, mode='r|') as archive:
    archive.extractall(root, numeric_owner=True, filter=safe)
'''


def repository(image):
    name = image.split('@', 1)[0]
    return name.rsplit(':', 1)[0] if ':' in name.rsplit('/', 1)[-1] else name


def digest(path):
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(chunk)
    return result.hexdigest()


def sync_directory(path):
    if hasattr(os, 'O_DIRECTORY'):
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def durable(path):
    with path.open('rb') as stream:
        os.fsync(stream.fileno())
    sync_directory(path.parent)


def private_directory(path):
    missing = []
    current = path
    while not current.exists():
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700)
        sync_directory(directory.parent)
    if path.is_symlink():
        raise RuntimeError('执行或备份目录不能是软链接')
    protect_private(path, existing=not missing, directory=True)


def validate_archive(path):
    from pathlib import PurePosixPath
    with tarfile.open(path) as archive:
        for member in archive:
            name = PurePosixPath(member.name)
            if name.is_absolute() or '..' in name.parts or '\\' in member.name or not (member.isfile() or member.isdir()):
                raise RuntimeError('备份含软链接、特殊文件或不安全路径，无法保证回退，已拒绝升级')
            if member.isfile():
                with archive.extractfile(member) as stream:
                    for _ in iter(lambda: stream.read(1024 * 1024), b''):
                        pass


def write_json(path, value):
    temp = path.with_suffix('.tmp')
    with temp.open('w', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    temp.chmod(0o644)
    os.replace(temp, path)
    sync_directory(path.parent)


class Controller:
    def __init__(self, directory=None):
        self.directory = directory or ROOT / '.workbench/control'
        private_directory(self.directory)
        for name in ('requests', 'results', 'status', 'processing', 'recovery'):
            path = self.directory / name
            private_directory(path)
        self.current = None
        self.log = None

    def command(self, args, timeout=60, env=None, **kwargs):
        result = subprocess.run(args, cwd=ROOT, env=env, timeout=timeout, check=False,
                                stderr=subprocess.PIPE, **({'stdout': subprocess.PIPE} if 'stdout' not in kwargs else {}), **kwargs)
        if self.log:
            output = (result.stderr or b'').decode('utf-8', errors='replace')
            for key, value in load_env(ROOT / '.env').items():
                if 'PASSWORD' in key and value:
                    output = output.replace(value, '[已隐藏]')
            with self.log.open('a', encoding='utf-8') as stream:
                stream.write(json.dumps({'command': args, 'code': result.returncode, 'error': output[-8192:]}, ensure_ascii=False) + '\n')
        if result.returncode:
            reason, _ = image_failure((result.stderr or b'').decode('utf-8', errors='replace'))
            raise RuntimeError('Docker 操作失败：' + reason)
        return result.stdout

    def compose(self, *args, **kwargs):
        values = load_env(ROOT / '.env')
        files = values.get('COMPOSE_FILE', 'compose.yaml').split(os.pathsep)
        if not files or any(name not in ('compose.yaml', 'legacy.compose.yaml') for name in files):
            raise RuntimeError('只允许完整管理端的固定 Compose 文件')
        env = {key: value for key, value in os.environ.items() if not key.startswith('COMPOSE_')}
        env.update(kwargs.pop('overrides', {}))
        command = ['docker', 'compose', '--env-file', str(ROOT / '.env')]
        for name in files:
            command.extend(['-f', str(ROOT / name)])
        return self.command(command + list(args), env=env, **kwargs)

    def owned(self, service, allow_missing=False):
        endpoint = os.environ.get('DOCKER_HOST') if not os.environ.get('DOCKER_CONTEXT') else None
        endpoint = endpoint or self.command(['docker', 'context', 'inspect', '--format', '{{(index .Endpoints "docker").Host}}']).decode().strip()
        if not endpoint.startswith(('unix://', 'npipe://')):
            raise RuntimeError('执行通道只允许本机 Docker 上下文')
        ids = self.compose('ps', '--all', '--quiet', service).decode().split()
        if not ids and allow_missing:
            self.owned('spug')  # 容器重建中断时，以仍在运行的同安装服务核验项目归属。
            return None
        if len(ids) != 1:
            raise RuntimeError('未找到唯一的目标服务容器，请先重跑安装脚本')
        item = json.loads(self.command(['docker', 'inspect', '--format', INFO, ids[0]]))
        labels = item['Labels']
        expected = {str((ROOT / f).resolve()) for f in load_env(ROOT / '.env').get('COMPOSE_FILE', 'compose.yaml').split(os.pathsep)}
        actual = {str(Path(f).resolve()) for f in labels.get('com.docker.compose.project.config_files', '').split(',') if f}
        if (os.path.normcase(labels.get('com.docker.compose.project.working_dir', '')) != os.path.normcase(str(ROOT)) or
                actual != expected or labels.get('com.docker.compose.service') != service):
            raise RuntimeError('目标容器不属于本安装目录，已拒绝操作')
        return item

    def healthy(self, service):
        target = ('http://dbx:4224/dbx/' if service == 'dbx' else
                  'http://victorialogs:9428/logs/select/logsql/query?query=_time%3A1m&limit=1')
        code = "from urllib.request import build_opener,ProxyHandler\nr=build_opener(ProxyHandler({})).open(%r,timeout=5)\nassert r.status==200" % target
        deadline = time.monotonic() + 120
        while True:
            try:
                self.compose('exec', '-T', 'spug', 'python3', '-c', code, timeout=15)
                from check import request
                values = load_env(ROOT / '.env')
                path = '/dbx/' if service == 'dbx' else '/logs/select/vmui/'
                if request(values, path, ('OPS_ADMIN_USER', 'OPS_ADMIN_PASSWORD'))[0] != 200:
                    raise RuntimeError('统一入口尚未恢复')
                return
            except (RuntimeError, OSError):
                if time.monotonic() >= deadline:
                    raise RuntimeError('服务健康检查超时') from None
                time.sleep(2)

    def snapshot(self, info, folder):
        records = []
        for mount in info['Mounts']:
            if mount['Type'] != 'volume' or not mount.get('RW'):
                continue
            users = self.command(['docker', 'ps', '-aq', '--filter', 'volume=' + mount['Name']]).decode().split()
            if any(not info['Id'].startswith(user) for user in users):
                raise RuntimeError('目标数据卷被其他容器使用，拒绝升级')
            archive = folder / (mount['Name'] + '.tar')
            with archive.open('xb') as stream:
                protect_private(archive)
                self.command(['docker', 'cp', info['Id'] + ':' + mount['Destination'].rstrip('/') + '/.', '-'], stdout=stream, timeout=600)
                stream.flush()
                os.fsync(stream.fileno())
            sync_directory(folder)
            validate_archive(archive)
            records.append({'volume': mount['Name'], 'archive': archive.name, 'sha256': digest(archive)})
        if not records:
            raise RuntimeError('没有可核验的数据卷备份，拒绝升级')
        return records

    def recover(self, state, check_health=True):
        service = state['service']; key = SERVICES[service]
        self.owned(service, allow_missing=True)
        if state['stage'] not in ('stopping', 'snapshot'):
            self.command(['docker', 'image', 'inspect', state['alias']])
            for record in state['volumes']:
                if digest(Path(state['folder']) / record['archive']) != record['sha256']:
                    raise RuntimeError('升级备份校验失败；已锁定服务操作，请保留备份并恢复数据')
                validate_archive(Path(state['folder']) / record['archive'])
            self.compose('stop', '--timeout', '30', service)
            for record in state['volumes']:
                name = 'opskit-restore-' + hashlib.sha256((str(ROOT) + record['volume']).encode()).hexdigest()[:24]
                labels = {'opskit.restore.root': str(ROOT), 'opskit.restore.volume': record['volume']}
                self.clean_helper(name, labels)
                with (Path(state['folder']) / record['archive']).open('rb') as stream:
                    try:
                        self.command(['docker', 'run', '--rm', '--name', name,
                                      '--label', 'opskit.restore.root=' + str(ROOT),
                                      '--label', 'opskit.restore.volume=' + record['volume'],
                                      '--network', 'none', '--read-only', '-i', '--mount',
                                      'type=volume,source=' + record['volume'] + ',target=/restore', '--entrypoint',
                                      'python', state['helper'], '-c', RESTORE], stdin=stream, timeout=600)
                    finally:
                        self.clean_helper(name, labels)
            self.compose('up', '-d', '--no-deps', '--pull', 'never', '--force-recreate', service,
                         overrides={key: state['alias']}, timeout=180)
            current = load_env(ROOT / '.env')
            current[key] = state['alias']
            save(ROOT / '.env', env_text(current))
            durable(ROOT / '.env')
        if state['was_running']:
            self.compose('start', service)
            if check_health:
                self.healthy(service)
        else:
            self.compose('stop', '--timeout', '30', service)
        (self.directory / 'recovery' / (service + '.json')).unlink()
        sync_directory(self.directory / 'recovery')

    def clean_helper(self, name, labels):
        ids = self.command(['docker', 'ps', '-aq', '--filter', 'name=^/' + name + '$']).decode().split()
        for identifier in ids:
            item = json.loads(self.command(['docker', 'inspect', '--format', '{{json .Config.Labels}}', identifier]))
            if any(item.get(key) != value for key, value in labels.items()):
                raise RuntimeError('恢复辅助容器名称被其他容器占用，已停止恢复')
            self.command(['docker', 'rm', '-f', identifier])
        if self.command(['docker', 'ps', '-aq', '--filter', 'name=^/' + name + '$']).strip():
            raise RuntimeError('恢复辅助容器尚未退出，已停止后续恢复')

    def upgrade(self, request, info, values, folder):
        service = request['service']; key = SERVICES[service]; image = request.get('image', '')
        if not IMAGE.fullmatch(image) or repository(image) != repository(values[key]):
            raise RuntimeError('升级镜像须与本安装配置使用相同仓库，仅允许更换版本或摘要')
        offline = values.get('OPSKIT_CONTROL_OFFLINE') == 'true'
        if not offline:
            self.command(['docker', 'pull', image], timeout=600)
        metadata = json.loads(self.command(['docker', 'image', 'inspect', image]))[0]
        if metadata['Os'] != 'linux' or metadata['Architecture'] != json.loads(self.command(['docker', 'image', 'inspect', info['Image']]))[0]['Architecture']:
            raise RuntimeError('升级镜像的系统或架构不匹配')
        helper = values['WORKBENCH_IMAGE']
        self.command(['docker', 'image', 'inspect', helper])
        original = (ROOT / '.env').read_bytes()
        alias = repository(values[key]) + ':opskit-rollback-' + request['id']
        self.command(['docker', 'tag', info['Image'], alias])
        save(folder / 'previous.env', original.decode())
        durable(folder / 'previous.env')
        state = {'service': service, 'stage': 'stopping', 'was_running': info['Running'],
                 'alias': alias, 'helper': helper, 'folder': str(folder), 'volumes': []}
        recovery = self.directory / 'recovery' / (service + '.json')
        write_json(recovery, state)
        try:
            self.compose('stop', '--timeout', '30', service)
            state['stage'] = 'snapshot'; write_json(recovery, state)
            state['volumes'] = self.snapshot(info, folder)
            save(folder / 'manifest.json', json.dumps(state, ensure_ascii=False, indent=2))
            durable(folder / 'manifest.json')
            state['stage'] = 'applying'; write_json(recovery, state)
            self.compose('up', '-d', '--no-deps', '--pull', 'never', '--force-recreate', service,
                         overrides={key: image}, timeout=180)
            self.healthy(service)
            if (ROOT / '.env').read_bytes() != original:
                raise RuntimeError('升级期间安装配置被修改，停止提交镜像设置')
            values[key] = image
            save(ROOT / '.env', env_text(values))
            durable(ROOT / '.env')
            recovery.unlink()
            sync_directory(recovery.parent)
            return {'ok': True, 'message': '镜像已应用，数据备份和健康检查通过', 'image': image, 'backup': str(folder)}
        except Exception:
            try:
                self.recover(state)
            except Exception:
                raise RuntimeError('升级及自动回退未完成，已锁定后续操作；请重跑安装脚本恢复，备份：' + str(folder)) from None
            raise RuntimeError('升级未完成，已恢复原服务；请查看备份目录的诊断记录：' + str(folder)) from None

    def handle(self, request):
        if (not isinstance(request, dict) or set(request) - {'id', 'action', 'service', 'created_at', 'image'} or
                not isinstance(request.get('id'), str) or not IDENTIFIER.fullmatch(request['id']) or
                not isinstance(request.get('action'), str) or not isinstance(request.get('service'), str) or
                request.get('action') not in ACTIONS or request.get('service') not in SERVICES or
                not isinstance(request.get('created_at'), (int, float)) or not 0 <= time.time() - request['created_at'] <= 300):
            raise ValueError('请求格式或时效无效')
        if request['action'] != 'upgrade' and 'image' in request:
            raise ValueError('仅升级动作允许指定镜像')
        if (self.directory / 'recovery' / (request['service'] + '.json')).exists():
            raise RuntimeError('该服务有未完成的升级回退，已锁定操作；请重跑安装脚本恢复')
        folder = ROOT / '.backups/service-control' / request['id']
        base = folder.parent
        private_directory(base)
        if folder.exists():
            raise RuntimeError('操作标识已存在，拒绝重放')
        private_directory(folder)
        self.log = folder / 'docker.jsonl'; self.log.touch(mode=0o600)
        protect_private(self.log)
        info = self.owned(request['service'])
        if request['action'] == 'status':
            return {'ok': True, 'running': info['Running'], 'image': info['reference']}
        if request['action'] == 'upgrade':
            return self.upgrade(request, info, load_env(ROOT / '.env'), folder)
        self.compose({'start': 'start', 'stop': 'stop', 'restart': 'restart'}[request['action']], request['service'], timeout=90)
        if request['action'] != 'stop':
            self.healthy(request['service'])
        running = self.owned(request['service'])['Running']
        if running != (request['action'] != 'stop'):
            raise RuntimeError('操作后的容器状态不符，请核对服务')
        return {'ok': True, 'message': '服务操作完成', 'running': running}

    def process(self, path):
        if not IDENTIFIER.fullmatch(path.stem):
            return
        active = self.directory / 'processing' / path.name
        try:
            os.replace(path, active)
        except FileNotFoundError:
            return
        request = {}
        try:
            flags = os.O_RDONLY | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_NOFOLLOW', 0)
            if active.is_symlink():
                raise ValueError('请求不能是软链接')
            fd = os.open(active, flags)
            with os.fdopen(fd, 'rb') as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 4096:
                    raise ValueError('请求必须是独立的小型普通文件')
                raw = stream.read(4097)
                if len(raw) > 4096:
                    raise ValueError('请求过大')
            decoded = json.loads(raw)
            if not isinstance(decoded, dict) or decoded.get('id') != active.stem:
                raise ValueError('请求格式或标识不符')
            request = decoded
            if (self.directory / 'results' / active.name).exists():
                return  # 已处理的请求绝不重放。
            self.current = {key: request.get(key) for key in ('id', 'action', 'service')}
            result = self.handle(request)
        except Exception as exc:
            result = {'ok': False, 'message': str(exc) if isinstance(exc, (ValueError, RuntimeError)) else '操作异常，请检查宿主执行日志和备份；未自动重试'}
        finally:
            self.current = None
            self.log = None
            if active.is_dir() and not active.is_symlink():
                os.replace(active, active.with_suffix('.invalid'))
            else:
                active.unlink(missing_ok=True)
        result.update({'id': path.stem, 'action': request.get('action'), 'service': request.get('service')})
        write_json(self.directory / 'results' / path.name, result)

    def serve(self, stop):
        try:
            self.consume(stop)
        finally:
            stop.set()

    def consume(self, stop):
        # SQLite 仅作跨进程互斥；进程退出自动释放，避免两个宿主进程同时执行升级。
        with closing(sqlite3.connect(self.directory / 'agent.lock', timeout=1)) as lock:
            lock.execute('BEGIN EXCLUSIVE')
            for recovery in (self.directory / 'recovery').glob('*.json'):
                try:
                    self.recover(json.loads(recovery.read_text()))
                except Exception:
                    print('服务回退尚未完成，操作保持锁定：' + recovery.stem, flush=True)
            for pending in (self.directory / 'processing').glob('*.json'):
                write_json(self.directory / 'results' / pending.name, {'id': pending.stem, 'ok': False, 'message': '宿主执行进程曾中断，请检查服务状态和升级备份后再操作'})
                pending.unlink()
            def heartbeat():
                while not stop.is_set():
                    write_json(self.directory / 'status/agent.json', {'updated_at': time.time(), 'pid': os.getpid(), 'operation': self.current})
                    stop.wait(2)
            thread = threading.Thread(target=heartbeat, daemon=True); thread.start()
            try:
                while not stop.is_set():
                    for path in sorted((self.directory / 'requests').glob('*.json'))[:32]:
                        self.process(path)
                    stop.wait(1)
            finally:
                stop.set()
                thread.join(timeout=3)
