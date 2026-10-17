#!/usr/bin/env python3
"""气隙交付包的 Linux 现场入口；只操作当前应用的固定 Compose 项目。"""
import argparse
from datetime import datetime, timezone
import getpass
import hashlib
import ipaddress
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

BUNDLE = Path(__file__).resolve().parent.parent
ACTIONS = ('install', 'upgrade', 'check', 'backup', 'rollback', 'sync-docs')
BUILTINS = ('management', 'collector', 'hub')
IDENTIFIER = re.compile(r'^[a-z0-9][a-z0-9_.-]{0,62}$')
PROJECT = re.compile(r'^[a-z0-9][a-z0-9_-]{0,62}$')
KEY = re.compile(r'^[A-Z][A-Z0-9_]*$')


class SiteError(RuntimeError):
    pass


def stamp():
    return datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')


def digest(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def child(root, relative):
    """Manifest 与校验清单中的路径只能是包内普通路径。"""
    if not isinstance(relative, str) or not relative or '\\' in relative or any(c in relative for c in '\n\r\x00'):
        raise SiteError('交付包包含无效路径')
    parts = PurePosixPath(relative).parts
    if PurePosixPath(relative).is_absolute() or any(p in ('.', '..') for p in parts) or ':' in relative:
        raise SiteError('交付包路径越界：' + relative)
    result = root.joinpath(*parts)
    reject_links(result, root)
    return result


def reject_links(path, stop=None):
    for part in [path] + list(path.parents):
        if part.is_symlink():
            raise SiteError('拒绝使用软链接路径：' + str(part))
        if stop is not None and part == stop:
            break


def verify_tree(root):
    reject_links(root)
    sums = root / 'SHA256SUMS'
    if not sums.is_file() or sums.is_symlink():
        raise SiteError('交付包缺少 SHA256SUMS')
    listed = {}
    for line in sums.read_text(encoding='utf-8').splitlines():
        match = re.fullmatch(r'([a-f0-9]{64})  (.+)', line)
        if not match or match[2] in listed or match[2] == 'SHA256SUMS':
            raise SiteError('SHA256SUMS 格式错误或存在重复项')
        path = child(root, match[2])
        if not path.is_file() or digest(path) != match[1]:
            raise SiteError('交付文件校验失败：' + match[2])
        listed[match[2]] = match[1]
    actual = set()
    for path in root.rglob('*'):
        if path.is_symlink():
            raise SiteError('交付包中不允许软链接：' + str(path.relative_to(root)))
        if not path.is_dir() and not path.is_file():
            raise SiteError('交付包中不允许特殊文件')
        if path.is_file() and path != sums:
            actual.add(path.relative_to(root).as_posix())
    if actual != set(listed):
        raise SiteError('交付文件与 SHA256SUMS 清单不一致')
    return listed


def read_json(path):
    try:
        return json.loads(path.read_text(encoding='utf-8-sig'))
    except (ValueError, OSError) as exc:
        raise SiteError('无法读取 JSON：' + str(path)) from exc


def load_manifest(root):
    value = read_json(child(root, 'delivery/manifest.json'))
    if value.get('schema_version') != 1:
        raise SiteError('不支持的交付包契约版本')
    for name in ('application', 'target'):
        if not isinstance(value.get(name), dict):
            raise SiteError('交付包缺少 ' + name)
    if not IDENTIFIER.fullmatch(value['application'].get('id', '')):
        raise SiteError('应用 ID 无效')
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,128}', value.get('artifact_id', '')):
        raise SiteError('交付包 artifact_id 无效')
    if not isinstance(value.get('roles'), list) or not value['roles']:
        raise SiteError('交付包缺少业务角色')
    ids, projects = set(), set()
    for role in value['roles']:
        ident = role.get('id', '')
        if not IDENTIFIER.fullmatch(ident) or ident in ids or ident in BUILTINS:
            raise SiteError('业务角色 ID 无效或重复')
        ids.add(ident)
        if not PROJECT.fullmatch(role.get('project_name', '')) or role['project_name'] in projects:
            raise SiteError('Compose 项目名称无效')
        projects.add(role['project_name'])
        directory = child(root, role['directory'])
        compose = child(root, role['compose'])
        if not directory.is_dir() or not compose.is_file() or directory not in compose.parents:
            raise SiteError('角色目录或 Compose 路径无效')
        for hook in role.get('hooks', {}).values():
            path = child(root, hook)
            if not path.is_file() or directory not in path.parents:
                raise SiteError('角色脚本路径无效')
        if role.get('stateful') and not role.get('hooks', {}).get('backup'):
            raise SiteError('有状态角色缺少真实备份脚本')
        if role.get('rollback') not in ('app-only', 'restore-hook', 'unsupported'):
            raise SiteError('角色缺少明确回退策略')
        if role.get('rollback') == 'restore-hook' and not role.get('hooks', {}).get('restore'):
            raise SiteError('数据恢复回退缺少 restore 脚本')
        if not role.get('checks') and not role.get('hooks', {}).get('check'):
            raise SiteError('角色缺少业务验收')
        if not role.get('image_references'):
            raise SiteError('角色缺少锁定镜像清单')
    return value


def target_probe():
    values = {}
    for line in Path('/etc/os-release').read_text().splitlines():
        key, sep, raw = line.partition('=')
        if sep:
            values[key] = raw.strip('"\'')
    arch = {'x86_64': 'amd64', 'aarch64': 'arm64'}.get(platform.machine(), platform.machine())
    return {'os': values.get('ID'), 'version': values.get('VERSION_ID'), 'arch': arch}


def verify_target(expected):
    if sys.platform != 'linux':
        raise SiteError('本交付包现场运行时只支持清单指定的龙蜥 Linux')
    actual = target_probe()
    if actual != expected:
        raise SiteError('目标环境不匹配；要求 {os} {version} / {arch}，当前 {actual}。尚未修改本机。'.format(**expected, actual=actual))


def parameter_valid(parameter, value):
    if not isinstance(value, str) or any(c in value for c in ("'", '\\', '\n', '\r', '\x00')):
        return False
    if not value:
        return not parameter.get('required', False)
    kind = parameter.get('type', 'text')
    if kind == 'port':
        return value.isdigit() and 1 <= int(value) <= 65535
    if kind == 'integer':
        return bool(re.fullmatch(r'-?[0-9]+', value))
    if kind == 'host':
        try:
            ipaddress.ip_address(value)
            return True
        except ValueError:
            return bool(re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?', value))
    return kind == 'text'


def read_env(path):
    result = {}
    if path is None or not path.exists():
        return result
    reject_links(path)
    for line in path.read_text(encoding='utf-8-sig').splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        key, sep, raw = line.partition('=')
        key = key.strip()
        if not sep or not KEY.fullmatch(key) or key in result:
            raise SiteError('现场参数文件中有无效或重复键')
        parts = shlex.split(raw, comments=False)
        if len(parts) > 1:
            raise SiteError('现场参数含空格时必须使用引号：' + key)
        result[key] = parts[0] if parts else ''
    return result


def parameters(role, supplied, interactive):
    definitions = role.get('parameters', [])
    keys = [p.get('key', '') for p in definitions]
    if len(set(keys)) != len(keys) or any(not KEY.fullmatch(k) for k in keys):
        raise SiteError('交付包参数键无效或重复')
    extra = set(supplied) - set(keys)
    if extra:
        raise SiteError('现场参数含未声明的键：' + ', '.join(sorted(extra)))
    values = {}
    for param in definitions:
        key = param['key']
        default = param.get('default')
        value = supplied.get(key, '' if default is None else str(default))
        while True:
            if interactive:
                hint = '已保存，回车保留' if param.get('secret') and value else ('必填，不回显' if param.get('secret') else value)
                prompt = str(param.get('label', key)) + ' [' + hint + ']：'
                entered = (getpass.getpass(prompt) if param.get('secret') else input(prompt))
                value = entered or value
            if parameter_valid(param, value):
                break
            if not interactive:
                raise SiteError('缺少或无效的现场参数：' + key)
            print('输入无效，请重新填写此项；端口为 1—65535，不接受换行、单引号或反斜杠。')
        values[key] = value
    return values


def env_text(values):
    return '# 现场参数与凭据，仅保存在本机，请勿外传。\n' + ''.join("{}='{}'\n".format(k, v) for k, v in values.items())


def atomic(path, data):
    reject_links(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp = tempfile.mkstemp(prefix='.site-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data if isinstance(data, bytes) else data.encode('utf-8'))
        os.chmod(temp, 0o600)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def save_json(path, data):
    atomic(path, json.dumps(data, ensure_ascii=False, indent=2) + '\n')


def copied(source, destination):
    reject_links(source)
    reject_links(destination)
    shutil.copytree(source, destination)
    for path in source.rglob('*'):
        if path.is_symlink():
            raise SiteError('拒绝复制软链接')
        if path.is_file() and digest(path) != digest(destination / path.relative_to(source)):
            raise SiteError('备份或复制校验失败：' + str(path))


class Runner:
    def __init__(self, directory, project, log):
        self.directory, self.project, self.log = directory, project, log

    def run(self, args, cwd=None, env=None, capture=False, stdin=None, interactive=False):
        settings = dict(os.environ, COMPOSE_PROJECT_NAME=self.project, PYTHONDONTWRITEBYTECODE='1')
        if env:
            settings.update(env)
        if interactive:
            with self.log.open('a', encoding='utf-8') as stream:
                stream.write('启动本机中文配置向导，凭据由终端输入且不记录。\n')
            result = subprocess.run(args, cwd=cwd or self.directory, env=settings, stdin=stdin)
        elif capture:
            result = subprocess.run(args, cwd=cwd or self.directory, env=settings,
                                    text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=stdin)
        else:
            with self.log.open('ab') as stream:
                result = subprocess.run(args, cwd=cwd or self.directory, env=settings,
                                        stdout=stream, stderr=subprocess.STDOUT, stdin=stdin)
        if result.returncode:
            if capture and result.stderr:
                with self.log.open('a', encoding='utf-8') as stream:
                    stream.write(result.stderr)
            raise SiteError('步骤执行失败（退出码 {}），请查看日志：{}'.format(result.returncode, self.log))
        return result.stdout if capture else ''

    def compose(self, compose, env_file, *args, capture=False):
        return self.run(['docker', 'compose', '--project-name', self.project, '--env-file', str(env_file),
                         '-f', str(compose)] + list(args), capture=capture)


def docker_ready(runner, target):
    runner.run(['docker', 'compose', 'version'], capture=True)
    value = json.loads(runner.run(['docker', 'info', '--format', '{{json .}}'], capture=True))
    arch = {'x86_64': 'amd64', 'aarch64': 'arm64'}.get(value.get('Architecture'), value.get('Architecture'))
    if value.get('OSType') != 'linux' or arch != target['arch']:
        raise SiteError('Docker 引擎平台必须与目标 Linux/CPU 相同')


def reject_foreign_project(runner, directory):
    if (directory / 'state.json').exists():
        return
    label = 'label=com.docker.compose.project=' + runner.project
    for resource in ('container', 'network', 'volume'):
        output = runner.run(['docker', resource, 'ls', '--all', '--filter', label, '--format', '{{.ID}}']
                            if resource == 'container' else
                            ['docker', resource, 'ls', '--filter', label, '--format', '{{.Name}}'], capture=True)
        if output.strip():
            raise SiteError('固定 Compose 项目名已被已有资源占用，拒绝接管或修改其他项目：' + runner.project)
    for resource in ('network', 'volume'):
        output = runner.run(['docker', resource, 'ls', '--filter', 'name=' + runner.project + '_',
                             '--format', '{{.Name}}'], capture=True)
        if any(name.startswith(runner.project + '_') for name in output.splitlines()):
            raise SiteError('现场已有同名网络或数据卷但没有本包安装记录，停止复用：' + runner.project)


def image_records(manifest, references):
    records = {item['reference']: item for item in manifest.get('images', [])}
    if len(records) != len(manifest.get('images', [])) or not references or any(ref not in records for ref in references):
        raise SiteError('交付包缺少目标所需镜像')
    output = [records[ref] for ref in references]
    for item in output:
        if item.get('platform') != 'linux/' + manifest['target']['arch'] or not re.fullmatch(r'sha256:[a-f0-9]{64}', item.get('image_id', '')):
            raise SiteError('镜像 ID 或平台不匹配')
        path = child(BUNDLE, item['archive'])
        if not path.is_file() or digest(path) != item['sha256']:
            raise SiteError('镜像归档校验失败')
        if not re.fullmatch(r'sha256:[a-f0-9]{64}', item.get('config_digest', '')):
            raise SiteError('镜像缺少归档配置摘要')
        archive_identity(item)
    return output


def archive_identity(item):
    """Docker Desktop index ID 与经典引擎 config ID 不同，以归档配置内容绑定身份。"""
    try:
        with tarfile.open(child(BUNDLE, item['archive']), 'r:') as archive:
            member = archive.getmember('manifest.json')
            if not member.isfile() or member.size > 4 * 1024 * 1024:
                raise SiteError('镜像归档 manifest 无效')
            manifest = json.load(archive.extractfile(member))
            if len(manifest) != 1 or manifest[0].get('RepoTags'):
                raise SiteError('镜像归档必须是单镜像且不携带原业务标签')
            config_file = archive.getmember(manifest[0]['Config'])
            if not config_file.isfile() or config_file.size > 16 * 1024 * 1024:
                raise SiteError('镜像归档配置无效')
            raw = archive.extractfile(config_file).read()
            config_digest = 'sha256:' + hashlib.sha256(raw).hexdigest()
            if item.get('config_digest') != config_digest:
                raise SiteError('镜像配置摘要与交付清单不一致')
            config = json.loads(raw)
            if config.get('os') + '/' + config.get('architecture') != item['platform']:
                raise SiteError('镜像归档内部 CPU/OS 与清单不一致')
            return config
    except (tarfile.TarError, KeyError, ValueError, TypeError) as exc:
        raise SiteError('无法校验镜像归档内部身份') from exc


def inspect_identity(info, item, config):
    expected_layers = config.get('rootfs', {}).get('diff_ids', [])
    if info.get('Os', '') + '/' + info.get('Architecture', '') != item['platform'] or info.get('RootFS', {}).get('Layers', []) != expected_layers:
        raise SiteError('导入后的镜像平台或文件层与归档不一致')
    actual_config = info.get('Config') or {}
    for key, expected in (config.get('config') or {}).items():
        if actual_config.get(key) != expected:
            raise SiteError('导入后的镜像运行配置与归档不一致')


def import_images(runner, records):
    loaded = {}
    for item in records:
        config = archive_identity(item)
        if item['archive'] not in loaded:
            print('正在从交付介质导入镜像并核验身份（不会联网）……')
            output = runner.run(['docker', 'image', 'load', '-i', str(child(BUNDLE, item['archive']))], capture=True)
            candidates = set(re.findall(r'^Loaded image ID:\s*(sha256:[a-f0-9]{64})\s*$', output, re.MULTILINE))
            if len(candidates) != 1:
                raise SiteError('无法从本次 Docker 导入结果确定唯一镜像身份')
            loaded[item['archive']] = candidates.pop()
        loaded_id = loaded[item['archive']]
        if loaded_id not in (item['image_id'], item.get('config_digest')):
            raise SiteError('本次实际导入的镜像 ID 不属于清单锁定镜像')
        info = json.loads(runner.run(['docker', 'image', 'inspect', loaded_id], capture=True))[0]
        if info.get('Id') != loaded_id:
            raise SiteError('导入后的镜像 ID 不匹配')
        inspect_identity(info, item, config)
        runner.run(['docker', 'image', 'tag', loaded_id, item['reference']])
        item['loaded_image_id'] = loaded_id
        item['rootfs_diff_ids'] = config.get('rootfs', {}).get('diff_ids', [])
        item['runtime_config'] = config.get('config') or {}


def installed_images(runner, state):
    """回退前核验旧别名仍指向旧镜像，不修改其他版本的标签。"""
    images = state.get('images', [])
    if not images:
        raise SiteError('旧版状态缺少镜像锁定记录，无法安全回退')
    for item in images:
        info = json.loads(runner.run(['docker', 'image', 'inspect', item['reference']], capture=True))[0]
        if info.get('Id') != item.get('loaded_image_id'):
            raise SiteError('旧版本镜像缺失或标签发生变化；请先导入原发行包镜像')
        inspect_identity(info, item, {'rootfs': {'diff_ids': item.get('rootfs_diff_ids', [])}, 'config': item.get('runtime_config', {})})


def role_hook(role, name, current):
    source = role.get('hooks', {}).get(name)
    if not source:
        return None
    relative = PurePosixPath(source).relative_to(PurePosixPath(role['directory']))
    return child(current, relative.as_posix())


def hook(runner, role, name, current, backup, action):
    script = role_hook(role, name, current)
    if script is None:
        return
    if not script.is_file():
        raise SiteError('已安装版本缺少脚本：' + name)
    print('正在执行现场步骤：' + name)
    runner.run(['bash', str(script)], cwd=current, env={
        'OPSKIT_APP_DIR': str(current), 'OPSKIT_ENV_FILE': str(current / 'site.env'),
        'OPSKIT_BACKUP_DIR': str(backup), 'OPSKIT_ACTION': action})


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def expanded(text, values):
    def replace(match):
        key = match[1]
        if key not in values:
            raise SiteError('业务验收引用未声明参数：' + key)
        return values[key]
    return re.sub(r'\$\{([A-Z][A-Z0-9_]*)\}', replace, text)


def check_business(runner, role, current, values, action):
    print('正在执行真实业务验收……')
    opener = build_opener(ProxyHandler({}), NoRedirect())
    for check in role.get('checks', []):
        url = expanded(check['url'], values)
        parsed = urlsplit(url)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
            raise SiteError('业务验收地址无效或包含凭据')
        expected = int(check.get('status', 200))
        timeout, attempts = min(float(check.get('timeout', 5)), 30), min(int(check.get('attempts', 12)), 60)
        passed = False
        for attempt in range(max(1, attempts)):
            try:
                with opener.open(Request(url, method='GET'), timeout=timeout) as response:
                    status = response.status
            except HTTPError as exc:
                status = exc.code
            except OSError:
                status = None
            if status == expected:
                passed = True
                break
            if attempt + 1 < attempts:
                time.sleep(1)
        if not passed:
            raise SiteError('业务 HTTP 验收失败，要求状态 {}。请查看应用日志：{}'.format(expected, runner.log))
    hook(runner, role, 'check', current, current.parent / 'backups', action)


def backup_role(runner, role, current, directory, action):
    if not role.get('hooks', {}).get('backup'):
        raise SiteError('该角色没有备份脚本，无法执行数据备份')
    folder = directory / 'backups' / stamp()
    folder.mkdir(parents=True, mode=0o700)
    hook(runner, role, 'backup', current, folder, action)
    files = [p for p in folder.rglob('*') if p.is_file()]
    if any(p.is_symlink() for p in folder.rglob('*')) or not any(p.stat().st_size for p in files):
        raise SiteError('备份脚本未产出非空普通文件，停止操作')
    atomic(folder / 'SHA256SUMS', ''.join(digest(p) + '  ' + p.relative_to(folder).as_posix() + '\n' for p in sorted(files)))
    verify_tree(folder)
    return folder


def installed_state(directory):
    path = directory / 'state.json'
    if not path.is_file():
        raise SiteError('该角色尚未安装，请先选择安装')
    return read_json(path)


def compose_path(current, role):
    return child(current, PurePosixPath(role['compose']).relative_to(role['directory']).as_posix())


def compose_valid(runner, role, current, references):
    value = json.loads(runner.compose(compose_path(current, role), current / 'site.env', 'config', '--format', 'json', capture=True))
    services = value.get('services', {})
    if not services or any(s.get('build') or s.get('image') not in references for s in services.values()):
        raise SiteError('Compose 含未锁定镜像或 build，禁止在现场构建/联网拉取')


def business(manifest, role, args, directory, runner, records):
    current = directory / 'current'
    state = read_json(directory / 'state.json') if (directory / 'state.json').exists() else None
    action = args.action
    if action in ('check', 'backup', 'rollback') or action == 'upgrade':
        state = installed_state(directory)
    if action == 'install' and state:
        raise SiteError('该角色已安装，请使用 upgrade 或 check，避免覆盖现场参数')
    if state and state['project_name'] != role['project_name']:
        raise SiteError('升级包的 Compose 项目名发生变化，停止操作')
    if action in ('check', 'backup'):
        old_role = state['role']
        if action == 'backup':
            folder = backup_role(runner, old_role, current, directory, action)
            print('备份完成并已核验：' + str(folder))
            return {'stateful_backup_passed': True}
        values = parameters(old_role, read_env(current / 'site.env'), False)
        check_business(runner, old_role, current, values, action)
        return {'checks_passed': True}
    if action == 'rollback':
        previous = state.get('previous')
        if not previous or state['role'].get('rollback') == 'unsupported':
            raise SiteError('没有可回退版本，或当前版本明确不支持回退')
        source = Path(previous['snapshot'])
        reject_links(source)
        if directory not in source.parents or not source.is_dir():
            raise SiteError('上版快照路径无效')
        verify_tree(source)
        installed_images(runner, previous)
        compose_valid(runner, previous['role'], source, previous['role']['image_references'])
        backup = None
        if state['role'].get('stateful'):
            backup = backup_role(runner, state['role'], current, directory, action)
        restore = state['role'].get('rollback') == 'restore-hook'
        if restore:
            before = previous.get('backup')
            if not before:
                raise SiteError('上次升级没有数据备份，不能恢复数据')
            backup_source = Path(before)
            if directory not in backup_source.parents:
                raise SiteError('数据备份路径无效')
            verify_tree(backup_source)
            # 只有明确声明 restore-hook 才执行数据恢复；先保留本次回退前数据。
            hook(runner, state['role'], 'restore', current, backup_source, action)
        retired = directory / 'history' / (stamp() + '-before-rollback')
        copied(current, retired)
        shutil.rmtree(current)
        copied(source, current)
        (current / 'SHA256SUMS').unlink()
        runner.compose(compose_path(current, previous['role']), current / 'site.env', 'up', '-d', '--pull', 'never', '--no-build')
        save_json(directory / 'state.json', dict(previous, previous=None))
        values = parameters(previous['role'], read_env(current / 'site.env'), False)
        check_business(runner, previous['role'], current, values, action)
        return {'checks_passed': True, 'stateful_backup_passed': bool(backup)}
    supplied = read_env(Path(args.env_file)) if args.env_file else (read_env(current / 'site.env') if state else {})
    values = parameters(role, supplied, not args.non_interactive)
    backup = None
    if state and (state['role'].get('stateful') or state['role'].get('hooks', {}).get('backup')):
        backup = backup_role(runner, state['role'], current, directory, action)
    previous = None
    if state:
        snapshot = directory / 'history' / stamp()
        snapshot.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        copied(current, snapshot)
        atomic(snapshot / 'SHA256SUMS', ''.join(digest(p) + '  ' + p.relative_to(snapshot).as_posix() + '\n' for p in sorted(snapshot.rglob('*')) if p.is_file()))
        verify_tree(snapshot)
        previous = dict(state, snapshot=str(snapshot), backup=str(backup) if backup else None)
    pending = directory / ('.pending-' + stamp())
    copied(child(BUNDLE, role['directory']), pending)
    atomic(pending / 'site.env', env_text(values))
    compose_valid(runner, role, pending, role['image_references'])
    import_images(runner, records)
    if current.exists():
        shutil.rmtree(current)
    pending.rename(current)
    save_json(directory / 'state.json', {'artifact_id': manifest['artifact_id'], 'application_version': manifest['application']['version'],
                                        'project_name': role['project_name'], 'role': role, 'images': records, 'previous': previous})
    hook(runner, role, action + '_before', current, backup or directory / 'backups', action)
    runner.compose(compose_path(current, role), current / 'site.env', 'up', '-d', '--pull', 'never', '--no-build')
    hook(runner, role, action + '_after', current, backup or directory / 'backups', action)
    check_business(runner, role, current, values, action)
    return {'checks_passed': True, 'stateful_backup_passed': bool(backup)}


def builtin_references(manifest, role):
    configured = manifest.get('opskit_roles', {}).get(role)
    if configured:
        return configured['image_references'] if isinstance(configured, dict) else configured
    keys = {'hub': ('WORKBENCH_IMAGE', 'NPM_IMAGE'), 'collector': ('FLUENTBIT_IMAGE',)}.get(role)
    images = manifest.get('opskit_images', {})
    return [images[k] for k in keys if k in images] if keys else [v for k, v in images.items() if k != 'FLUENTBIT_IMAGE']


def document_records(manifest):
    records = manifest.get('documents')
    if not isinstance(records, list) or not records:
        raise SiteError('本发行包没有可同步的文档清单')
    prefix, app_id, found = 'opskit/management/documents/', manifest['application']['id'], {}
    for record in records:
        name = record.get('path', '')
        if not name.startswith(prefix):
            raise SiteError('文档清单路径不属于管理文档库')
        relative = name[len(prefix):]
        parts = PurePosixPath(relative).parts
        if len(parts) < 3 or parts[0] not in ('delivery', 'project', 'builtin') or parts[1] != app_id or record.get('group') != parts[0]:
            raise SiteError('文档清单分组或应用归属不匹配')
        source = child(BUNDLE, name)
        if relative in found or not source.is_file() or source.stat().st_size != record.get('bytes') or digest(source) != record.get('sha256'):
            raise SiteError('文档清单校验失败：' + relative)
        found[relative] = record
    return found


def sync_documents(manifest, directory, destination=None, initial=False):
    """按应用归属同步文件，保持挂载根目录 inode，网页无需重启。"""
    records = document_records(manifest)
    library = Path(destination).absolute() if destination else directory / 'opskit/management/documents'
    reject_links(library)
    if not library.is_dir():
        raise SiteError('管理文档库尚不存在，请先在管理机安装 management/hub，或明确提供 --documents-dir')
    # 共享中心的不同应用也复用同一把锁；隐藏元数据不会作为可预览文档。
    import fcntl
    reject_links(library / '.opskit-documents.lock')
    lock = (library / '.opskit-documents.lock').open('a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        raise SiteError('此文档库已有同步任务，请待完成后重试') from None
    try:
        index_path = library / '.opskit-sources.json'
        reject_links(index_path)
        index = read_json(index_path) if index_path.exists() else {'schema_version': 1, 'applications': {}}
        if index.get('schema_version') != 1 or not isinstance(index.get('applications'), dict):
            raise SiteError('管理文档库来源记录损坏，停止覆盖')
        app_id = manifest['application']['id']
        previous = index['applications'].get(app_id, {})
        if not isinstance(previous, dict):
            raise SiteError('管理文档库应用归属记录损坏')
        owned = previous.get('files', {})
        if not isinstance(owned, dict):
            raise SiteError('管理文档库应用归属记录损坏')
        for path in library.rglob('*'):
            reject_links(path, library)
            if not path.is_file() and not path.is_dir():
                raise SiteError('文档库含特殊文件，无法安全备份')
        for relative, checksum in owned.items():
            path = child(library, relative)
            parts = PurePosixPath(relative).parts
            if len(parts) < 3 or parts[0] not in ('delivery', 'project', 'builtin') or parts[1] != app_id:
                raise SiteError('文档库来源记录路径越界')
            if path.exists() and (not path.is_file() or digest(path) != checksum):
                raise SiteError('文档已在本地编辑，保留原件并停止同步：' + relative)
        for relative, record in records.items():
            path = child(library, relative)
            if path.exists() and relative not in owned:
                if not initial or not path.is_file() or digest(path) != record['sha256']:
                    raise SiteError('文档与无归属的本地文件冲突，停止覆盖：' + relative)
            if any(parent.exists() and not parent.is_dir() for parent in path.parents if library in parent.parents):
                raise SiteError('文档目标父目录被文件占用：' + relative)
        current = {name: record['sha256'] for name, record in records.items()}
        if owned == current and previous.get('artifact_id') == manifest['artifact_id'] and all(child(library, name).is_file() for name in current):
            return {'checks_passed': True, 'documents_synced': len(current), 'documents_changed': False, 'documents_dir': str(library)}
        backup = library.parent / '.document-backups' / (app_id + '-' + stamp())
        reject_links(backup)
        backup.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        backup.mkdir(mode=0o700)
        copied(library, backup / 'library')
        files = sorted(p for p in (backup / 'library').rglob('*') if p.is_file())
        atomic(backup / 'SHA256SUMS', ''.join(digest(path) + '  ' + path.relative_to(backup).as_posix() + '\n' for path in files))
        for line in (backup / 'SHA256SUMS').read_text().splitlines():
            checksum, name = line.split('  ', 1)
            if digest(child(backup, name)) != checksum:
                raise SiteError('文档库备份核验失败，尚未同步')
        for relative, record in records.items():
            path = child(library, relative)
            if not path.exists() or digest(path) != record['sha256']:
                atomic(path, child(BUNDLE, record['path']).read_bytes())
        for relative in set(owned) - set(records):
            path = child(library, relative)
            if path.exists():
                path.unlink()
        for relative, checksum in current.items():
            if digest(child(library, relative)) != checksum:
                raise SiteError('文档同步核验失败；保留备份：' + str(backup))
        index['applications'][app_id] = {'artifact_id': manifest['artifact_id'], 'version': manifest['application']['version'],
                                          'synced_at': datetime.now(timezone.utc).isoformat(), 'files': current, 'backup': str(backup),
                                          'documents': [{'path': relative, 'group': record['group'], 'source': record.get('source', ''),
                                                         'sha256': record['sha256'], 'bytes': record['bytes']}
                                                        for relative, record in sorted(records.items())]}
        save_json(index_path, index)
        print('文档已同步：{}；已核验备份：{}'.format(library, backup))
        return {'checks_passed': True, 'documents_synced': len(current), 'documents_changed': True,
                'documents_dir': str(library), 'documents_backup': str(backup)}
    finally:
        lock.close()


def check_builtin(runner, role, working, configuration):
    if role != 'hub':
        module = 'collector' if role == 'collector' else 'management'
        runner.run([sys.executable, '-B', str(working / module / 'scripts/check.py')], cwd=working / module)
        return
    values = read_env(configuration)
    port = values.get('OPSKIT_HTTP_PORT', '80')
    opener = build_opener(ProxyHandler({}), NoRedirect())
    try:
        with opener.open('http://127.0.0.1:' + port + '/', timeout=15) as response:
            page = response.read(2 * 1024 * 1024)
        with opener.open('http://127.0.0.1:' + port + '/hub/api/state', timeout=15) as response:
            state = json.load(response)
    except (OSError, ValueError) as exc:
        raise SiteError('Hub 页面或发现 API 验收失败') from exc
    if b'id="builtin-section"' not in page or state.get('api_version') != 1 or not state.get('discovered_at') or state.get('docker_error') or state.get('http_error'):
        raise SiteError('Hub 未完成入口或宿主机发现验收')


def builtin(manifest, role, args, directory, runner, records):
    if args.action not in ('install', 'check'):
        raise SiteError('内置 OpsKit 目前仅提供安装与验收；其数据库升级/备份/恢复须按模块维护说明执行，不会用配置副本冒充数据备份。')
    working = directory / 'opskit'
    module = 'collector' if role == 'collector' else 'management'
    configuration = working / module / ('.workbench/hub.env' if role == 'hub' else '.env')
    if args.action == 'check':
        if not configuration.is_file():
            raise SiteError('此 OpsKit 模块尚未安装')
        check_builtin(runner, role, working, configuration)
        return {'checks_passed': True}
    installed = directory / 'state.json'
    if working.exists() and (not installed.exists() or read_json(installed).get('artifact_id') != manifest['artifact_id']):
        raise SiteError('现有 OpsKit 不属于同一交付版，停止覆盖；升级请按现场维护说明操作')
    if args.non_interactive and not configuration.is_file() and not args.env_file and role != 'hub':
        raise SiteError('管理端/采集端首次非交互安装必须提供完整 --env-file；建议直接运行中文配置向导')
    if args.env_file:
        incoming = read_env(Path(args.env_file))
        for key, reference in manifest.get('opskit_images', {}).items():
            if (module == 'collector') == (key == 'FLUENTBIT_IMAGE') and key in incoming and incoming[key] != reference:
                raise SiteError('现场配置不能覆盖气隙包镜像：' + key)
    if not working.exists():
        copied(child(BUNDLE, manifest.get('opskit_path', 'opskit')), working)
        save_json(installed, {'artifact_id': manifest['artifact_id'], 'project_name': runner.project})
        if manifest.get('documents') and role != 'collector':
            sync_documents(manifest, directory, initial=True)
    if args.env_file:
        source = Path(args.env_file)
        read_env(source)
        if configuration.exists() and source.read_bytes() != configuration.read_bytes():
            raise SiteError('已有现场配置与传入文件不同，保留现有凭据；请在原模块中明确重新配置')
        if not configuration.exists():
            atomic(configuration, source.read_bytes())
    # 现场配置可以调整端口和凭据，但镜像始终采用本交付版锁定别名。
    if configuration.exists():
        values = read_env(configuration)
        locked = manifest.get('opskit_images', {})
        for key, reference in locked.items():
            if (module == 'collector') == (key == 'FLUENTBIT_IMAGE'):
                if key in values and values[key] != reference:
                    raise SiteError('现场配置不能覆盖气隙包镜像：' + key)
    command = ['bash', str(working / 'install.sh'), role, '--offline', '--keep-location']
    if role == 'hub' and configuration.exists():
        command += ['--port', read_env(configuration).get('OPSKIT_HTTP_PORT', '80')]
    # 先完成现有中文向导；非交互缺值在导入镜像/启动前退出，不会等待无意义输入。
    runner.run(command + ['--configure-only'], cwd=working, stdin=subprocess.DEVNULL if args.non_interactive else None,
               interactive=not args.non_interactive)
    import_images(runner, records)
    runner.run(command, cwd=working, stdin=subprocess.DEVNULL if args.non_interactive else None)
    check_builtin(runner, role, working, configuration)
    return {'checks_passed': True}


def choose(manifest, args):
    if not args.action:
        print('现场操作：1 安装 / 2 升级 / 3 验收 / 4 备份 / 5 回退')
        while True:
            answer = input('请选择操作 [1—5]：').strip()
            if answer in ('1', '2', '3', '4', '5'):
                args.action = ACTIONS[int(answer) - 1]
                break
            print('请选择 1—5。')
    if not args.role:
        roles = manifest['roles']
        if args.action == 'sync-docs':
            roles = []
        if args.action == 'backup':
            roles = [r for r in roles if r.get('hooks', {}).get('backup')]
        if args.action == 'rollback':
            roles = [r for r in roles if r.get('rollback') != 'unsupported']
        choices = [(r['id'], r.get('name', r['id'])) for r in roles]
        if args.action in ('install', 'check'):
            choices += [('management', 'OpsKit 管理中心'), ('collector', 'Fluent Bit 日志采集端'), ('hub', '精简管理端')]
        elif args.action == 'sync-docs':
            choices += [('management', 'OpsKit 管理中心文档'), ('hub', '独立 Hub 文档')]
        if not choices:
            raise SiteError('此交付包没有支持该操作的角色，请查看现场操作文档')
        for number, (ident, name) in enumerate(choices, 1):
            print('{} {}（{}）'.format(number, name, ident))
        while True:
            answer = input('请选择本机角色：').strip()
            if answer.isdigit() and 1 <= int(answer) <= len(choices):
                args.role = choices[int(answer) - 1][0]
                break
            print('请选择列表中的编号。')


def main(argv=None):
    parser = argparse.ArgumentParser(description='OpsKit 气隙交付现场向导（无网络）')
    parser.add_argument('--role')
    parser.add_argument('--action', choices=ACTIONS)
    parser.add_argument('--non-interactive', action='store_true')
    parser.add_argument('--env-file')
    parser.add_argument('--state-root', default='/opt/opskit-apps')
    parser.add_argument('--documents-dir', help='sync-docs 时使用已有共享管理中心的 documents 目录')
    args = parser.parse_args(argv)
    os.umask(0o077)
    print('正在核对交付文件、目标系统与镜像清单……')
    verify_tree(BUNDLE)
    manifest = load_manifest(BUNDLE)
    verify_target(manifest['target'])
    if args.non_interactive and (not args.role or not args.action):
        raise SiteError('非交互执行必须填写 --role 和 --action')
    choose(manifest, args)
    role = next((r for r in manifest['roles'] if r['id'] == args.role), None)
    if not role and args.role not in BUILTINS:
        raise SiteError('未知角色：' + args.role)
    if args.documents_dir and args.action != 'sync-docs':
        raise SiteError('--documents-dir 仅用于 sync-docs，不会修改安装路径或接管 OpsKit')
    if args.action == 'sync-docs' and args.role not in ('management', 'hub'):
        raise SiteError('文档同步仅在 management/hub 管理机执行')
    directory = Path(args.state_root).absolute() / manifest['application']['id'] / args.role
    reject_links(directory)
    references = role['image_references'] if role else builtin_references(manifest, args.role)
    records = image_records(manifest, references) if args.action != 'sync-docs' else []
    # 参数先检查，避免缺值或未知字段到导入镜像后才报错。
    if role and args.action in ('install', 'upgrade'):
        existing = directory / 'current/site.env'
        supplied = read_env(Path(args.env_file)) if args.env_file else read_env(existing)
        parameters(role, supplied, False) if args.non_interactive else None
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    # 同一角色只允许一个现场操作，避免重复部署/同时覆盖同一份状态。
    import fcntl  # 目标检查已限定 Linux；制包端 Windows 仍可读取/检查本文件。
    lock = (directory / '.lock').open('a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        raise SiteError('此角色已有现场操作正在执行，请待完成后重试') from None
    log = directory / 'logs' / (stamp() + '-' + args.action + '.log')
    atomic(log, '')
    app_id = manifest['application']['id']
    builtin_project = 'airgap-' + app_id.replace('.', '-')[:30] + '-' + args.role + '-' + hashlib.sha256(app_id.encode()).hexdigest()[:8]
    runner = Runner(directory, role['project_name'] if role else builtin_project, log)
    result = {'schema_version': 1, 'artifact_id': manifest['artifact_id'], 'target': manifest['target'],
              'role_id': args.role, 'action': args.action, 'passed': False, 'checks_passed': False,
              'stateful_backup_passed': False, 'log': str(log)}
    try:
        if args.action != 'sync-docs':
            docker_ready(runner, manifest['target'])
        if args.action == 'install':
            reject_foreign_project(runner, directory)
        if args.action == 'sync-docs':
            if not args.documents_dir and not (directory / 'state.json').is_file():
                raise SiteError('本机尚未安装此应用的管理中心；其他中心请明确使用 --documents-dir')
            details = sync_documents(manifest, directory, args.documents_dir)
        else:
            details = business(manifest, role, args, directory, runner, records) if role else builtin(manifest, args.role, args, directory, runner, records)
        result.update(details, passed=True)
        print('完成：{} / {}。记录：{}'.format(args.role, args.action, directory / 'result.json'))
    except Exception:
        print('操作未完成；现场参数与日志已保留：' + str(log), file=sys.stderr)
        raise
    finally:
        save_json(directory / 'result.json', result)
        lock.close()


if __name__ == '__main__':
    try:
        main()
    except (SiteError, OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print('现场操作失败：' + str(error), file=sys.stderr)
        sys.exit(1)
    except (KeyboardInterrupt, EOFError):
        print('\n操作已取消；重跑同一命令可继续检查。', file=sys.stderr)
        sys.exit(1)
