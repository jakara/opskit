#!/usr/bin/env python3
"""Inspect, stage and seal an OpsKit air-gap delivery. Python 3.9+, no PyPI dependencies."""
import argparse
import copy
import datetime
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import uuid
import zipfile
from xml.etree import ElementTree


class BundleError(Exception):
    pass


NAME = re.compile(r'^[a-z0-9][a-z0-9_.-]{0,62}$')
PARAM = re.compile(r'^[A-Z][A-Z0-9_]*$')
IMAGE_ID = re.compile(r'^sha256:[0-9a-f]{64}$')
SECRET = re.compile(r'(PASSWORD|PASSWD|TOKEN|SECRET|API_KEY|PRIVATE_KEY)', re.I)
IMAGE_KEYS = {
    'management': ['WORKBENCH_IMAGE', 'NPM_IMAGE', 'SPUG_IMAGE', 'MARIADB_IMAGE', 'DBX_IMAGE', 'VICTORIALOGS_IMAGE', 'FLUENTBIT_IMAGE'],
    'collector': ['FLUENTBIT_IMAGE'],
    'hub': ['WORKBENCH_IMAGE', 'NPM_IMAGE'],
}
HOOKS = {'install_before', 'install_after', 'upgrade_before', 'upgrade_after', 'check', 'backup', 'restore'}
DENIED_PARTS = {'.git', '.svn', '.hg', '.backups', '.workbench', '.uninstall-backups', '.document-backups', 'node_modules', '__pycache__'}
DOCUMENT_TYPES = {'.md', '.markdown', '.txt', '.pdf', '.docx', '.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg'}
DOCUMENT_ROOT = 'opskit/management/documents'
DOCUMENT_LIMIT = 20 * 1024 * 1024


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8-sig'))
    except (OSError, ValueError) as exc:
        raise BundleError('无法读取 JSON：%s（%s）' % (path, exc)) from exc


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def copy_deployment_file(source, target):
    """Windows 制包也输出 Linux 可执行的 LF shell 文件。"""
    source, target = Path(source), Path(target)
    if target.suffix == '.sh':
        with target.open('w', encoding='utf-8', newline='\n') as stream:
            stream.write(source.read_text(encoding='utf-8-sig'))
        target.chmod(0o755)
    else:
        shutil.copy2(source, target)


def relative_path(value):
    if not isinstance(value, str) or not value or '\\' in value or any(c in value for c in '\x00\r\n\t'):
        raise BundleError('路径必须是非空、使用 / 的相对路径：%r' % value)
    p = PurePosixPath(value)
    if p.is_absolute() or any(s in ('', '.', '..') for s in value.split('/')) or ':' in value:
        raise BundleError('不允许绝对路径或路径越界：%s' % value)
    return p


def safe_path(root, value, must_exist=True, file_only=True):
    root = Path(root).resolve()
    rel = relative_path(value)
    path = root.joinpath(*rel.parts)
    current = root
    for part in rel.parts:
        current = current / part
        if current.is_symlink():
            raise BundleError('不允许软链接：%s' % value)
    if not path.resolve().is_relative_to(root):
        raise BundleError('路径越界：%s' % value)
    if must_exist and not (path.is_file() if file_only else path.exists()):
        raise BundleError('缺少文件：%s' % path)
    return path


def safe_name(value, field):
    if not isinstance(value, str) or not NAME.fullmatch(value):
        raise BundleError('%s 必须为小写字母、数字及 ._-，不超过 63 字符' % field)
    return value


def reject_sensitive_path(value):
    parts = relative_path(value).parts
    name = parts[-1].lower()
    if any(p in DENIED_PARTS or p.lower() in ('backups', 'logs') for p in parts):
        raise BundleError('禁止打包源码依赖、运行数据、日志或备份：%s' % value)
    if name == '.env' or name.startswith('.env.') and not name.endswith(('.example', '.template')):
        raise BundleError('禁止打包真实 .env：%s' % value)
    if name.endswith(('.pem', '.key', '.p12', '.pfx', '.sqlite', '.db', '.log', '.sql', '.dump')) or name.startswith(('id_rsa', 'id_ed25519')):
        raise BundleError('禁止打包凭据、数据库或日志：%s' % value)


def check_text_secrets(path):
    """Catch common accidental credentials; a human still reviews explicit deployment files."""
    data = Path(path).read_bytes()
    if b'-----BEGIN ' in data and b'PRIVATE KEY-----' in data:
        raise BundleError('发现私钥，拒绝打包：%s' % path)
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError:
        return
    check_secret_text(text, path)


def check_secret_text(text, path):
    if '-----BEGIN ' in text and 'PRIVATE KEY-----' in text:
        raise BundleError('发现私钥，拒绝打包：%s' % path)
    for line in text.splitlines():
        if line.lstrip().startswith('#'):
            continue
        match = re.match(r'\s*([A-Za-z0-9_.-]+)\s*[:=]\s*["\']?([^\s"\']+)', line)
        if match and SECRET.search(match[1]) and match[2] not in ('null', 'None', 'true', 'false', '""', "''") and '${' not in match[2]:
            raise BundleError('发现疑似明文凭据，请改成现场参数：%s' % path)
        if re.search(r'://[^\s/@:]+:[^\s/@]+@', line) and '${' not in line:
            raise BundleError('发现含密码的连接地址：%s' % path)


def validate_document(path):
    if not stat.S_ISREG(path.stat().st_mode) or path.stat().st_size > DOCUMENT_LIMIT:
        raise BundleError('文档必须为不超过 20 MiB 的普通文件：%s' % path)
    extension = path.suffix.lower()
    if extension == '.doc':
        raise BundleError('旧版 .doc 请先转换为 .docx 或 PDF：%s' % path)
    if extension not in DOCUMENT_TYPES:
        raise BundleError('不支持的文档格式：%s' % path)
    check_text_secrets(path)
    if extension != '.docx':
        return
    try:
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            names = [member.filename for member in members]
            if len(members) > 2000 or sum(member.file_size for member in members) > 100 * 1024 * 1024:
                raise BundleError('Word 文档解压内容超过 100 MiB 或 2000 项：%s' % path)
            if len(set(names)) != len(names) or not {'[Content_Types].xml', 'word/document.xml'} <= set(names):
                raise BundleError('Word 文档缺少必要内容或含重复项：%s' % path)
            for member in members:
                relative_path(member.filename.rstrip('/'))
                if member.flag_bits & 1 or stat.S_ISLNK(member.external_attr >> 16):
                    raise BundleError('Word 文档不允许加密或链接：%s' % path)
                if member.file_size > 1000 * max(member.compress_size, 1):
                    raise BundleError('Word 文档压缩比例异常，无法安全预览：%s' % path)
                data = archive.read(member)
                if member.filename.endswith('.xml'):
                    node = ElementTree.fromstring(data)
                    paragraphs = [''.join(item.itertext()) for item in node.iter() if item.tag.rsplit('}', 1)[-1] == 'p']
                    check_secret_text('\n'.join(paragraphs), path)
    except (zipfile.BadZipFile, ElementTree.ParseError, RuntimeError, OSError) as exc:
        raise BundleError('Word 文档损坏或无法读取：%s' % path) from exc


def project_documents(repo, selections, destination, app_id):
    """只复制开发者明确选择的文档，保留仓库相对路径及图片引用。"""
    repo = Path(repo).resolve()
    files, case_names = {}, {}
    for value in selections:
        reject_sensitive_path(value)
        selected = safe_path(repo, value, file_only=False)
        candidates = []
        if selected.is_dir():
            for parent, directories, names in os.walk(selected, followlinks=False):
                for name in directories + names:
                    path = Path(parent, name)
                    relative = path.relative_to(repo).as_posix()
                    if name == '.DS_Store' and path.is_file() and not path.is_symlink():
                        continue
                    reject_sensitive_path(relative)
                    safe_path(repo, relative, file_only=False)
                    if not path.is_dir():
                        candidates.append(path)
        else:
            candidates.append(selected)
        for path in candidates:
            relative = path.relative_to(repo).as_posix()
            if path.name == '.DS_Store':
                continue
            library_relative = 'project/%s/%s' % (app_id, relative)
            if (any(part.startswith('.') for part in PurePosixPath(relative).parts) or
                    len(PurePosixPath(library_relative).parts) > 12 or len(library_relative) > 1024):
                raise BundleError('文档路径隐藏、层级超过 12 或过长，无法在 Hub 预览：%s' % relative)
            validate_document(path)
            if relative.casefold() in case_names and case_names[relative.casefold()] != relative:
                raise BundleError('文档路径在大小写不敏感系统上碰撞：%s' % relative)
            case_names[relative.casefold()] = relative
            files[relative] = path
    records = []
    for relative, source in sorted(files.items()):
        name = 'project/%s/%s' % (app_id, relative)
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        records.append({'path': DOCUMENT_ROOT + '/' + name, 'group': 'project', 'source': relative,
                        'sha256': digest(target), 'bytes': target.stat().st_size})
    return records


def library_documents(bundle, source, manifest, project_records):
    app_id = manifest['application']['id']
    root, records = bundle / DOCUMENT_ROOT, list(project_records)
    builtins = ['README.md', 'management/README.md', 'collector/README.md', 'management/ai/README.md']
    # 只补可信静态说明，不把旧客户的 documents 目录带入新发行版。
    if (source / 'management/documents/README.md').is_file():
        builtins.append('management/documents/README.md')
    for relative in builtins:
        path = safe_path(source, relative)
        target = root / 'builtin' / app_id / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        records.append({'path': target.relative_to(bundle).as_posix(), 'group': 'builtin', 'source': relative,
                        'sha256': digest(target), 'bytes': target.stat().st_size})
    for name in ('现场操作.md', 'Spug操作.md'):
        target = root / 'delivery' / app_id / name
        target.parent.mkdir(parents=True, exist_ok=True)
        text = (bundle / 'docs' / name).read_text(encoding='utf-8')
        text = text.replace('../opskit/management/README.md', '../../builtin/%s/management/README.md' % app_id)
        text = text.replace('../opskit/collector/README.md', '../../builtin/%s/collector/README.md' % app_id)
        target.write_text(text, encoding='utf-8')
        records.append({'path': target.relative_to(bundle).as_posix(), 'group': 'delivery', 'source': 'docs/' + name,
                        'sha256': digest(target), 'bytes': target.stat().st_size})
    manifest['documents'] = sorted(records, key=lambda item: item['path'])


def run(command, **kwargs):
    try:
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                check=True, encoding='utf-8', **kwargs)
        return result.stdout
    except FileNotFoundError as exc:
        raise BundleError('找不到必要工具：%s' % command[0]) from exc
    except subprocess.CalledProcessError as exc:
        raise BundleError('命令执行失败：%s\n%s' % (' '.join(command[:4]), exc.stderr[-3000:])) from exc


def compose_document(repo, filename, project_name=None):
    path = safe_path(repo, filename)
    reject_sensitive_path(filename)
    # An explicit empty env file prevents development .env from entering rendering.
    with tempfile.TemporaryDirectory(prefix='opskit-compose-') as temporary:
        empty = Path(temporary) / 'empty.env'
        empty.write_text('', encoding='utf-8')
        project = ['--project-name', project_name] if project_name else []
        result = run(['docker', 'compose', *project, '--env-file', str(empty), '-f', str(path), 'config',
                      '--no-interpolate', '--no-env-resolution', '--no-path-resolution', '--format', 'json'], cwd=str(path.parent))
    try:
        document = json.loads(result)
    except ValueError as exc:
        raise BundleError('Docker Compose 未返回有效 JSON，请更新 Compose 插件') from exc
    if not isinstance(document.get('services'), dict) or not document['services']:
        raise BundleError('Compose 没有服务：%s' % filename)
    return document


def inspect_repo(repo):
    repo = Path(repo).resolve()
    if not repo.is_dir():
        raise BundleError('仓库目录不存在：%s' % repo)
    compose, dockerfiles, configs = [], [], []
    for parent, directories, files in os.walk(repo, followlinks=False):
        directories[:] = sorted(d for d in directories if d not in DENIED_PARTS and d not in ('dist', 'vendor', 'target') and not Path(parent, d).is_symlink())
        for name in sorted(files):
            path = Path(parent, name)
            if path.is_symlink():
                continue
            rel = path.relative_to(repo).as_posix()
            if name.lower() in ('compose.yaml', 'compose.yml', 'docker-compose.yml', 'docker-compose.yaml') or name.lower().endswith(('.compose.yaml', '.compose.yml')):
                compose.append(rel)
            if name == 'Dockerfile' or name.startswith('Dockerfile.'):
                dockerfiles.append(rel)
            if name == 'opskit-delivery.json':
                configs.append(rel)
    return {'repo': str(repo), 'compose_files': compose, 'dockerfiles': dockerfiles, 'delivery_configs': configs,
            'pending': ['确认目标龙蜥版本和 CPU 架构', '显式填写主机角色、目标平台镜像、现场参数、必要配置文件',
                        '提供真实业务检查；有状态角色提供备份及明确回退策略', '准备已在断网目标系统验证的离线基础依赖',
                        '在隔离环境逐角色安装并验收，然后用 seal 生成正式交付包'],
            'note': '只枚举路径，不读取 .env，不执行仓库代码，不自动构建或部署。'}


def validate_config(config):
    if config.get('schema_version') != 1:
        raise BundleError('制包配置 schema_version 必须为 1')
    kind = config.get('kind', 'application')
    if kind not in ('application', 'opskit'):
        raise BundleError('kind 仅支持 application 或 opskit')
    overrides = config.get('opskit_images', {})
    if (not isinstance(overrides, dict) or set(overrides) - set(IMAGE_KEYS['management'] + IMAGE_KEYS['collector']) or
            any(not isinstance(ref, str) or not ref or '$' in ref or any(c.isspace() for c in ref) for ref in overrides.values())):
        raise BundleError('opskit_images 只允许 7 个已知镜像变量和显式镜像引用')
    documents = config.get('documents', [])
    if not isinstance(documents, list) or any(not isinstance(value, str) for value in documents):
        raise BundleError('documents 必须为仓库相对文件/目录路径的字符串数组')
    for value in documents:
        reject_sensitive_path(value)
    app, target, roles = config.get('application', {}), config.get('target', {}), config.get('roles')
    if set(app) != {'id', 'name', 'version'}:
        raise BundleError('application 仅允许 id、name、version')
    safe_name(app.get('id'), 'application.id')
    safe_name(app.get('version'), 'application.version')
    if not isinstance(app.get('name'), str) or not app['name'].strip():
        raise BundleError('application.name 不能为空')
    if target.get('os') != 'anolis' or target.get('version') not in ('8.9', '8.10') or target.get('arch') not in ('amd64', 'arm64'):
        raise BundleError('目标仅支持 Anolis 8.9 / 8.10，amd64 / arm64')
    if set(target) != {'os', 'version', 'arch'}:
        raise BundleError('target 仅允许 os、version、arch')
    if not isinstance(roles, list) or kind == 'application' and not roles:
        raise BundleError('至少需要一个业务角色')
    if kind == 'opskit' and roles:
        raise BundleError('OpsKit 本体气隙包的 roles 必须为空，不包含业务角色')
    ids, projects = set(), set()
    for role in roles:
        if set(role) - {'id', 'name', 'project_name', 'compose', 'images', 'files', 'parameters', 'checks', 'stateful', 'rollback', 'hooks', 'site_notes'}:
            raise BundleError('角色包含契约之外的字段，请勿把开发配置直接复制进交付配置')
        ident = safe_name(role.get('id'), 'role.id')
        if ident in ids or ident in ('management', 'collector', 'hub'):
            raise BundleError('业务角色 ID 重复或使用保留名称：%s' % ident)
        ids.add(ident)
        project = safe_name(role.get('project_name'), 'role.project_name')
        if '.' in project:
            raise BundleError('Compose project_name 不允许点号，请使用小写字母、数字、下划线和连字符')
        if project in projects or project in ('opskit', 'opskit-hub', 'opskit-collector'):
            raise BundleError('Compose project_name 重复或使用 OpsKit 保留名称')
        projects.add(project)
        relative_path(role.get('compose'))
        if not isinstance(role.get('stateful'), bool):
            raise BundleError('%s 必须明确 stateful=true|false' % ident)
        if role.get('rollback') not in ('app-only', 'restore-hook', 'unsupported'):
            raise BundleError('%s 必须明确 rollback 策略' % ident)
        hooks = role.get('hooks', {})
        if not isinstance(hooks, dict) or set(hooks) - HOOKS:
            raise BundleError('%s hooks 含未知操作' % ident)
        if role['stateful'] and not hooks.get('backup'):
            raise BundleError('%s 有状态，必须提供 backup hook' % ident)
        if role['rollback'] == 'restore-hook' and not hooks.get('restore'):
            raise BundleError('%s restore-hook 回退必须提供 restore hook' % ident)
        checks = role.get('checks', [])
        if not checks and not hooks.get('check'):
            raise BundleError('%s 必须提供 checks 或真实 check hook' % ident)
        if not isinstance(checks, list):
            raise BundleError('checks 必须为列表')
        for check in checks:
            if not isinstance(check, dict) or not isinstance(check.get('url'), str) or not re.match(r'^https?://', check['url']) or not isinstance(check.get('status', 200), int):
                raise BundleError('%s 含无效 HTTP 检查' % ident)
            if not 100 <= check.get('status', 200) <= 599:
                raise BundleError('HTTP 检查状态码无效')
        parameters, keys = role.get('parameters', []), set()
        if not isinstance(parameters, list):
            raise BundleError('parameters 必须为列表')
        for parameter in parameters:
            if set(parameter) - {'key', 'label', 'type', 'default', 'required', 'secret'}:
                raise BundleError('parameter 含未知字段')
            key = parameter.get('key', '')
            if not PARAM.fullmatch(key) or key in keys:
                raise BundleError('%s 参数 key 无效或重复' % ident)
            keys.add(key)
            if parameter.get('type', 'text') not in ('text', 'port', 'host', 'integer'):
                raise BundleError('不支持的参数类型：%s' % key)
            if not isinstance(parameter.get('required', False), bool) or not isinstance(parameter.get('secret', False), bool):
                raise BundleError('参数 required / secret 必须为布尔值')
            if (parameter.get('secret') or SECRET.search(key)) and parameter.get('default') not in (None, ''):
                raise BundleError('秘密参数不得携带制包机默认值：%s' % key)
            if SECRET.search(key) and parameter.get('secret') is not True:
                raise BundleError('秘密参数必须标记 secret=true，在现场隐藏输入：%s' % key)
        if not isinstance(role.get('images', {}), dict) or not isinstance(role.get('files', []), list):
            raise BundleError('%s images/files 格式无效' % ident)
        notes = role.get('site_notes', [])
        if not isinstance(notes, list) or any(not isinstance(note, str) or not note.strip() for note in notes):
            raise BundleError('%s site_notes 必须是非空字符串数组' % ident)
        for note in notes:
            if '\x00' in note or re.search(r'(?i)(password|passwd|token|secret)\s*[:=]\s*\S+', note) or re.search(r'://[^\s/@:]+:[^\s/@]+@', note):
                raise BundleError('site_notes 不能包含凭据，请只写专有操作和部署顺序')
    return config


def validate_hook(path):
    check_text_secrets(path)
    meaningful = []
    for line in path.read_text(encoding='utf-8-sig').splitlines():
        value = line.strip()
        if not value or value.startswith('#') or re.fullmatch(r'set\s+-[A-Za-z]+(?:\s+pipefail)?', value):
            continue
        meaningful.append(value)
    if not meaningful or all(re.fullmatch(r'(exit\s+0|true|:|echo\s+.*|printf\s+.*)', line) for line in meaningful):
        raise BundleError('hook 只有空操作或输出，不能充当真实备份、恢复或验收：%s' % path)


def variable_keys(value):
    return set(re.findall(r'(?<!\$)\$\{([A-Za-z_][A-Za-z0-9_]*)', value))


def role_document(repo, role, destination):
    doc = compose_document(repo, role['compose'], role['project_name'])
    if doc.get('include') or doc.get('secrets'):
        raise BundleError('请将 Compose include 展开；秘密通过现场 parameters 注入，不打包 Compose secrets')
    for kind in ('networks', 'volumes'):
        for key, resource in (doc.get(kind) or {}).items():
            if resource.get('external'):
                raise BundleError('气隙包不能复用外部网络或数据卷：%s.%s' % (kind, key))
            expected = role['project_name'] + '_' + key
            if resource.get('name', expected) != expected:
                raise BundleError('网络/卷名称必须属于当前项目 %s：%s.%s' % (role['project_name'], kind, key))
    files, destinations = {}, set()
    for item in role.get('files', []):
        source, target = item.get('source'), item.get('target')
        reject_sensitive_path(source)
        reject_sensitive_path(target)
        path = safe_path(repo, source)
        target_path = safe_path(destination, target, must_exist=False)
        if target == 'compose.json' or relative_path(target).parts[0] == 'hooks' or target in destinations:
            raise BundleError('角色文件目标重复或占用保留路径：%s' % target)
        destinations.add(target)
        check_text_secrets(path)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        copy_deployment_file(path, target_path)
        files[path.resolve()] = target
    known_services = set(doc['services'])
    if set(role.get('images', {})) - known_services:
        raise BundleError('images 包含 Compose 中不存在的服务')
    images = {}
    for service, settings in doc['services'].items():
        safe_name(service, 'Compose 服务名')
        if settings.get('container_name'):
            raise BundleError('删除 container_name，避免与现场已有容器冲突：%s' % service)
        if settings.get('env_file'):
            raise BundleError('请将 env_file 改为现场 parameters + environment，禁止读取开发 .env：%s' % service)
        if settings.get('develop'):
            raise BundleError('不能把 Compose develop 配置带到现场：%s' % service)
        reference = role.get('images', {}).get(service, settings.get('image'))
        if not isinstance(reference, str) or not reference or '$' in reference or any(c.isspace() for c in reference):
            raise BundleError('服务 %s 的 image 必须是已构建、无变量的显式镜像引用' % service)
        if settings.get('build') and service not in role.get('images', {}):
            raise BundleError('服务 %s 存在 build，必须先按目标平台构建并填写 images 映射' % service)
        images[service] = reference
        settings.pop('build', None)
        settings.pop('pull_policy', None)
        settings.pop('profiles', None)
        for key, value in (settings.get('environment') or {}).items():
            if SECRET.search(key) and value not in (None, '') and not variable_keys(str(value)):
                raise BundleError('Compose 含明文秘密：%s.%s' % (service, key))
            if SECRET.search(key) and value and re.search(r'\$\{[^}]+:?-([^}]+)\}', str(value)):
                raise BundleError('秘密参数不能在 Compose 中携带默认密码：%s.%s' % (service, key))
            if value is None:
                raise BundleError('Compose 环境变量不能继承制包机值，请显式使用现场参数：%s.%s' % (service, key))
            if value and re.search(r'://[^\s/@:]+:[^\s/@]+@', str(value)) and not variable_keys(str(value)):
                raise BundleError('Compose 环境变量含明文连接密码：%s.%s' % (service, key))
        for volume in settings.get('volumes', []):
            if not isinstance(volume, dict):
                raise BundleError('Compose config 未将挂载转换为结构化格式')
            if volume.get('type') == 'volume' and not role['stateful']:
                raise BundleError('命名卷/匿名卷必须明确 stateful=true：%s' % service)
            if volume.get('type') == 'bind':
                source = volume.get('source', '')
                if not volume.get('read_only', False) and not role['stateful']:
                    raise BundleError('可写挂载必须明确 stateful=true：%s' % service)
                if variable_keys(source):
                    if not role['stateful']:
                        raise BundleError('现场挂载路径必须明确有状态并提供备份')
                else:
                    original = (safe_path(repo, role['compose']).parent / source).resolve()
                    if original not in files:
                        raise BundleError('绑定挂载必须在 files 中显式列出必要文件：%s' % source)
                    volume['source'] = './' + files[original]
        if settings.get('extends'):
            raise BundleError('请展开 Compose extends 后制包')
    for config in (doc.get('configs') or {}).values():
        if 'file' in config:
            original = (safe_path(repo, role['compose']).parent / config['file']).resolve()
            if original not in files:
                raise BundleError('Compose configs.file 必须显式列入 files')
            config['file'] = './' + files[original]
    parameters = {p['key'] for p in role.get('parameters', [])}
    # JSON includes escaped $${VAR}; variable_keys deliberately ignores those shell variables.
    serialized = json.dumps(doc, ensure_ascii=False)
    if re.search(r'(?<!\$)\$[A-Za-z_]', serialized):
        raise BundleError('Compose 现场变量必须使用 ${PARAMETER} 形式，不使用 $PARAMETER')
    missing = (variable_keys(serialized) | variable_keys(json.dumps(role.get('checks', []), ensure_ascii=False))) - parameters
    if missing:
        raise BundleError('Compose 中的变量必须全部定义为现场参数：%s' % ', '.join(sorted(missing)))
    doc['name'] = role['project_name']
    hooks = {}
    for action, source in role.get('hooks', {}).items():
        reject_sensitive_path(source)
        path = safe_path(repo, source)
        validate_hook(path)
        target = destination / 'hooks' / (action + '.sh')
        target.parent.mkdir(parents=True, exist_ok=True)
        copy_deployment_file(path, target)
        target.chmod(0o755)
        hooks[action] = 'application/%s/hooks/%s.sh' % (role['id'], action)
    output = copy.deepcopy(role)
    output.pop('images', None)
    output.pop('files', None)
    output.update(directory='application/' + role['id'], compose='application/%s/compose.json' % role['id'], hooks=hooks)
    return doc, output, images


def image_platform(info):
    if not isinstance(info, dict) or not IMAGE_ID.fullmatch(info.get('Id', '')):
        raise BundleError('Docker image inspect 缺少完整镜像 ID')
    return '%s/%s' % (info.get('Os'), info.get('Architecture'))


def registry_repository(reference):
    base = reference.split('@', 1)[0]
    last = base.rsplit('/', 1)[-1]
    return base.rsplit(':', 1)[0] if ':' in last else base


def archive_metadata(archive, platform):
    """Preserve both Engine IDs: containerd's manifest/index ID and classic config ID differ."""
    try:
        with tarfile.open(archive, 'r:') as stream:
            entries = json.load(stream.extractfile('manifest.json'))
            if len(entries) != 1:
                raise BundleError('导出归档必须只含一个目标平台镜像，请先为目标平台镜像使用独立引用')
            entry = entries[0]
            if entry.get('RepoTags'):
                raise BundleError('按 ID 导出仍携带镜像标签，停止以免在现场覆盖其他应用标签')
            config_name = entry['Config']
            relative_path(config_name)
            member = stream.getmember(config_name)
            if not member.isfile():
                raise BundleError('镜像 Config 必须为普通文件')
            data = stream.extractfile(member).read()
            config = json.loads(data)
            if '%s/%s' % (config.get('os'), config.get('architecture')) != platform:
                raise BundleError('导出归档内的实际平台不匹配')
            layers = config.get('rootfs', {}).get('diff_ids')
            if not isinstance(layers, list) or any(not IMAGE_ID.fullmatch(value) for value in layers):
                raise BundleError('导出镜像缺少完整 rootfs.diff_ids')
            return {'config_digest': 'sha256:' + hashlib.sha256(data).hexdigest(), 'rootfs_diff_ids': layers}
    except (tarfile.TarError, ValueError, KeyError, AttributeError, TypeError) as exc:
        raise BundleError('镜像归档不是可验证的 Docker save 格式：%s' % archive) from exc


def resolve_image(reference, platform, use_local=False):
    if use_local:
        inspected = json.loads(run(['docker', 'image', 'inspect', reference]))
    else:
        raw = json.loads(run(['docker', 'buildx', 'imagetools', 'inspect', reference, '--raw']))
        if raw.get('manifests'):
            matches = [m for m in raw['manifests'] if '%s/%s' % (m.get('platform', {}).get('os'), m.get('platform', {}).get('architecture')) == platform]
            if len(matches) != 1:
                raise BundleError('镜像 %s 未提供唯一目标平台 %s' % (reference, platform))
            image_digest = matches[0].get('digest', '')
        else:
            descriptor = json.loads(run(['docker', 'buildx', 'imagetools', 'inspect', reference, '--format', '{{json .Manifest}}']))
            image_digest = descriptor.get('digest', '')
        if not IMAGE_ID.fullmatch(image_digest):
            raise BundleError('无法锁定 registry 镜像 digest：%s' % reference)
        locked = registry_repository(reference) + '@' + image_digest
        run(['docker', 'pull', '--platform', platform, locked])
        inspected = json.loads(run(['docker', 'image', 'inspect', locked]))
    if len(inspected) != 1 or image_platform(inspected[0]) != platform:
        raise BundleError('镜像架构不匹配：%s，需要 %s' % (reference, platform))
    return inspected[0]['Id']


def copy_opskit(source, destination):
    source = Path(source).resolve()
    if not (source / 'management/compose.yaml').is_file() or not (source / 'collector/compose.yaml').is_file():
        raise BundleError('--opskit-source 不是完整 OpsKit 运行源码')
    root_files = {'README.md', 'install.sh', 'install.ps1', 'install.cmd', 'uninstall.sh', 'uninstall.ps1', 'uninstall.cmd', '.gitignore', 'OPSKIT_SOURCE.json'}
    extensions = {'.py', '.sh', '.ps1', '.cmd', '.md', '.txt', '.yaml', '.yml', '.html', '.css', '.js', '.conf'}
    paths = [p for p in source.iterdir() if p.name in root_files]
    for module in ('management', 'collector'):
        for parent, directories, files in os.walk(source / module, followlinks=False):
            directories[:] = [d for d in directories if d not in DENIED_PARTS and d not in ('backups', 'data', 'offline-images')
                               and not (Path(parent) == source / 'management' and d == 'documents')]
            for directory in directories:
                if Path(parent, directory).is_symlink():
                    raise BundleError('OpsKit 运行源码包含链接：%s' % Path(parent, directory))
            for name in files:
                path = Path(parent, name)
                vendor_asset = path.is_relative_to(source / 'management/portal/vendor') and (
                    path.suffix in {'.mjs', '.bcmap', '.pfb', '.ttf', '.wasm'} or
                    path.name.upper().startswith(('LICENSE', 'COPYING', 'NOTICE')))
                ui_asset = path.is_relative_to(source / 'management/ui') and (
                    path.suffix in {'.json', '.png', '.jpg', '.xml', '.xlsx', '.svg', '.ico', '.woff', '.woff2', '.ttf', '.webmanifest', '.txt'} or
                    path.name == 'opskit-corresponding-source.tar.gz')
                if ui_asset or vendor_asset or path.suffix in extensions or name == '.env.example':
                    paths.append(path)
    for path in sorted(paths):
        relative = path.relative_to(source).as_posix()
        safe_path(source, relative)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        copy_deployment_file(path, target)


def opskit_source(argument=None):
    if argument:
        return Path(argument).resolve()
    script = Path(__file__).resolve()
    snapshot = script.parent.parent / 'assets/opskit'
    if (snapshot / 'management/compose.yaml').is_file():
        return snapshot
    for candidate in script.parents:
        if (candidate / 'management/compose.yaml').is_file() and (candidate / 'collector/compose.yaml').is_file():
            return candidate
    raise BundleError('找不到 OpsKit 快照，请使用完整 Skill 压缩包，或指定 --opskit-source')


def verify_prerequisites(directory, target):
    directory = Path(directory).resolve()
    manifest = read_json(safe_path(directory, 'manifest.json'))
    if manifest.get('schema_version') != 1 or manifest.get('target') != target:
        raise BundleError('离线依赖目标系统/架构与发行包不一致')
    evidence = manifest.get('verification', {})
    if evidence.get('offline_install') is not True:
        raise BundleError('离线依赖没有真实 offline_install 验证，不能生成完整暂存包')
    if evidence.get('network') != 'none' or not all(evidence.get(key) for key in ('container_image', 'image_id', 'platform', 'python_version', 'docker_version', 'compose_version', 'commands', 'log')):
        raise BundleError('离线依赖缺少断网测试平台、命令、版本及日志证据')
    if evidence['platform'] != 'linux/' + target['arch']:
        raise BundleError('离线依赖测试平台与目标架构不一致')
    files = manifest.get('files', [])
    keys = manifest.get('keys', [])
    if not files or not keys or {f.get('group') for f in files} != {'runtime', 'python'}:
        raise BundleError('离线依赖必须包含 runtime、python 两组完整 RPM 和签名公钥')
    seen = set()
    for item in files + keys + manifest.get('repository_files', []):
        name = item.get('path')
        if name in seen:
            raise BundleError('离线依赖清单重复：%s' % name)
        seen.add(name)
        path = safe_path(directory, name)
        if not re.fullmatch(r'[0-9a-f]{64}', item.get('sha256', '')) or digest(path) != item['sha256']:
            raise BundleError('离线依赖校验失败：%s' % name)
    safe_path(directory, 'bootstrap.sh')
    safe_path(directory, evidence['log'])
    for name in ('target.txt', 'python-packages.txt'):
        safe_path(directory, name)
    verify_checksums(directory)
    return manifest


def copy_prerequisites(source, destination, target):
    verify_prerequisites(source, target)
    # Repository metadata and evidence logs are required too. SHA256SUMS verifies
    # the complete file set and safe_path rejects links before any copy.
    shutil.copytree(source, destination)
    (destination / 'bootstrap.sh').chmod(0o755)


def image_defaults(source, config):
    defaults = {}
    for module in ('management', 'collector'):
        document = compose_document(source, '%s/compose.yaml' % module)
        for service in document['services'].values():
            value = service.get('image', '')
            match = re.fullmatch(r'\$\{([A-Z][A-Z0-9_]*):-([^}]+)\}', value)
            if not match:
                raise BundleError('OpsKit Compose 镜像必须具有可锁定的环境默认值：%s' % value)
            defaults[match[1]] = match[2]
    expected = set(IMAGE_KEYS['management'] + IMAGE_KEYS['collector'])
    if set(defaults) != expected or set(config.get('opskit_images', {})) - expected:
        raise BundleError('OpsKit 镜像变量集合不匹配，请更新制包器契约')
    defaults.update(config.get('opskit_images', {}))
    if any(not isinstance(ref, str) or not ref or '$' in ref or any(c.isspace() for c in ref) for ref in defaults.values()):
        raise BundleError('opskit_images 必须使用显式镜像引用')
    return defaults


def checksums(directory):
    directory = Path(directory)
    paths = []
    for parent, directories, files in os.walk(directory, followlinks=False):
        for name in directories + files:
            if Path(parent, name).is_symlink():
                raise BundleError('交付目录不允许链接：%s' % Path(parent, name))
        for name in files:
            path = Path(parent, name)
            if path.name != 'SHA256SUMS' or path.parent != directory:
                paths.append(path)
    lines = ['%s  %s\n' % (digest(path), path.relative_to(directory).as_posix()) for path in sorted(paths)]
    (directory / 'SHA256SUMS').write_text(''.join(lines), encoding='utf-8')


def verify_checksums(directory):
    directory = Path(directory)
    expected = {}
    for line in safe_path(directory, 'SHA256SUMS').read_text(encoding='utf-8').splitlines():
        if not re.fullmatch(r'[0-9a-f]{64}  .+', line):
            raise BundleError('SHA256SUMS 格式错误')
        checksum, name = line.split('  ', 1)
        if name in expected:
            raise BundleError('SHA256SUMS 有重复文件')
        expected[name] = checksum
        if digest(safe_path(directory, name)) != checksum:
            raise BundleError('暂存包文件校验失败：%s' % name)
    actual = set()
    for parent, dirs, files in os.walk(directory, followlinks=False):
        for name in dirs + files:
            if Path(parent, name).is_symlink():
                raise BundleError('暂存包存在链接')
        actual.update(Path(parent, name).relative_to(directory).as_posix() for name in files)
    if actual != set(expected) | {'SHA256SUMS'}:
        raise BundleError('暂存目录文件集合与 SHA256SUMS 不一致')


def write_documents(bundle, manifest):
    app, target, roles = manifest['application'], manifest['target'], manifest['roles']
    opskit_only = manifest.get('kind', 'application') == 'opskit'
    dirname = bundle.name
    archive = dirname + '.tar.gz'
    base = '/opt/opskit-apps/' + app['id']
    size_mib = max(1, (sum(p.stat().st_size for p in bundle.rglob('*') if p.is_file()) + 1048575) // 1048576)
    recommended_mib = max(2048, size_mib * 3)
    lines = ['# %s %s 现场操作\n' % (app['name'], app['version']),
             '目标：龙蜥 %s，%s。%s暂存目录尚未通过断网验收时不能交付。\n' % (target['version'], target['arch'],
                 '本包只安装 OpsKit 管理中心、精简管理端及日志采集端，不包含业务应用。' if opskit_only else '先确认每台服务器分配的角色。'),
             '## 复制、校验与解压\n',
             '将正式 `%s` 和同名 `.sha256` 文件通过客户批准的通道复制到各目标主机 `/opt/opskit-deliveries/`。下面命令由有 sudo 权限的运维账户执行：' % archive,
             '```bash', 'cd /opt/opskit-deliveries', 'sha256sum -c %s.sha256' % archive,
             'tar -xzf %s' % archive, 'cd %s' % dirname, 'sha256sum --strict -c SHA256SUMS', '```',
             '两次校验必须全部通过。目录已经存在时停止，不要覆盖上次解压目录。解压路径不要包含空格；保留上一版发行包。\n',
             '镜像、依赖和运行资源约 %s MiB。交付目录建议至少留 %s MiB 可用空间，用于压缩包、解压与版本留存；Docker 数据目录、数据库和后续备份另行预留业务所需空间。执行 `df -h /opt /var/lib/docker` 核对，Docker 数据目录有调整时检查对应磁盘。' % (size_mib, recommended_mib),
             '## 按主机角色安装\n',
             '| 主机职责 | 选择的角色 | 安装范围 | 现场主机 |', '|---|---|---|---|',
             '| 管理机 | management | 一台；完整 Hub/NPM/Spug/DBX/日志中心 | 现场填写 IP |',
             *['| %s | %s | 仅承担本业务角色的主机 | 现场填写 IP |' % (role.get('name', role['id']), role['id']) for role in roles],
             '| 日志采集 | collector | 每台需要采集日志的主机 | 按现场采集范围填写 IP |',
             '| 仅需入口工作台 | hub | 可选；同一目录不要与完整 management 重复安装 | 按方案填写 |\n',
             ('建议顺序：管理中心 → 需要采集日志的主机上的采集器。已有业务系统继续按其自身部署方式管理；本包不安装或升级业务服务。精简管理端只含 Hub/NPM，与完整管理中心二选一。' if opskit_only else
              '建议顺序：管理中心 → 数据库/业务依赖 → 业务服务 → 日志采集器。业务角色的具体先后和客户专有步骤见下方逐角色说明。只在承担对应角色的服务器执行命令，不把每个角色都装在每台机器上。'),
             '管理中心（仅管理机）：', '```bash', 'sudo bash install.sh --role management --action install', '```',
             '按中文向导填写内网地址、监听地址和访问账户；首次配置管理中心仍使用现有 OpsKit 向导。记录它显示的 Hub、Spug、DBX、日志入口，以及采集端所需信息。Hub 占用 80 端口，外层代理按现场方案接入。',
             ('管理中心安装并检查成功后，在每台需要采集日志的主机安装采集器：' if opskit_only else '完成该机业务角色后，再安装日志采集器（每台需要采集日志的业务机）：'),
             '```bash', 'sudo bash install.sh --role collector --action install', '```',
             '采集器向导填写本机业务日志目录、管理中心内网地址和专用日志写入账户；不要使用运维登录账户。',
             '需要减少重复填写时，从管理机 `%s/management/opskit/management/collector.env` 通过受控通道复制连接配置到业务机 `/opt/opskit-collector-connection.env`；该文件含日志写入凭据，不能放进发行包。首次采集安装可执行：' % base,
             '```bash', 'sudo bash install.sh --role collector --action install --env-file /opt/opskit-collector-connection.env', '```',
             '向导会复用连接信息，再填写本机标签和日志目录；不要复制管理机的 `.env`。完整参数说明见 [管理端说明](../opskit/management/README.md) 与 [采集端说明](../opskit/collector/README.md)。',
             '如果管理中心与采集端分机，管理向导中 HTTP 监听需填写管理机内网 IP 或 0.0.0.0，不能保留仅本机可达的 127.0.0.1；采集器 OPS_HOST 填可达的管理机地址，OPS_VHOST 使用同一个运维域名。外层 HTTPS 配置由现场已有代理负责。',
             '管理与采集模块安装后分别执行：', '```bash',
             'sudo bash install.sh --role management --action check',
             'sudo bash install.sh --role collector --action check', '```',
             '上述两个命令分别在对应主机执行；结果都应显示“完成”，result.json 的 passed/checks_passed=true。采集检查还需实际追加一条业务日志，到管理端按 project/host/app/env 标签检索确认，单看容器运行不算日志链路通过。',
             '管理状态和操作日志：`%s/management/result.json`、`%s/management/logs/`；管理配置：`%s/management/opskit/management/.env`。' % (base, base, base),
             '采集状态和操作日志：`%s/collector/result.json`、`%s/collector/logs/`；采集配置：`%s/collector/opskit/collector/.env`。配置包含凭据，只能受控交接。' % (base, base, base),
             '本气隙包安装禁止联网拉取镜像或现场构建。基础依赖缺失时自动使用包内已验收 RPM，系统 Python 不替换。系统版本、CPU 或校验不匹配时停止，索取对应发行包。\n',
             '## 出错时先停止\n',
             '校验、备份、镜像导入、启动或业务检查任一步失败，都停止后续主机。不要删除数据卷、不要绕过检查，也不要把失败当作已自动回退。保留终端错误、result.json 和本次日志，交给开发人员决定重试或回退。\n']
    spug = ['# Spug 自定义发布操作\n',
            ('本包不包含业务应用，也未自动注册 Spug 应用或发布配置。先按《现场操作》安装管理中心，在向导给出的 Spug 地址登录；Spug 自身管理员与主机 SSH 账户分别管理。' if opskit_only else
             '本文是预置的逐字段配置说明，尚未自动注册 Spug 应用或发布配置。首次安装由工程师在终端按《现场操作》完成中文向导；Spug 流程用于已有安装的后续升级。'),
            '目标主机必须已通过 SSH 纳管。默认下述命令使用 root 运维账户；其他 SSH 账户应由管理员限定入口路径和必要 sudo 权限，并将 `bash install.sh` 改为 `sudo -n bash install.sh`。不要授予任意整机免密命令权限。\n',
            ('在主机管理中逐台填写现场 IP、SSH 端口与被授权账户，并测试连接。可使用主机终端、批量执行及已审批的任务开展运维；业务发布需要另行使用适配该业务的气隙包及其发布手册，不在此通用包中虚构升级或回退命令。' if opskit_only else
             '先按管理向导给出的 Spug 地址登录，在主机管理中为每台目标机填写现场 IP、SSH 端口与被授权账户，测试连接成功；再建立应用和客户现场环境。主机承担的角色必须与《现场操作》表对应；SSH 连接未通过或角色首次安装未成功时停止发布。Spug 自身管理员与主机 SSH 账户是不同配置。\n'),
            '## 先分发、校验，再申请发布\n',
            '离线镜像包较大。通过客户批准的文件通道预先复制完整包与 `.sha256` 到各主机 `/opt/opskit-deliveries/`，逐台执行：',
            '```bash', 'cd /opt/opskit-deliveries', 'sha256sum -c %s.sha256' % archive, 'tar -xzf %s' % archive,
            'cd %s' % dirname, 'sha256sum --strict -c SHA256SUMS', '```',
            '解压目录已存在时停止，先确认是否为本次发行版，不覆盖已存在内容。不能只传 Compose；缺镜像、依赖或脚本就不是完整交付。\n']
    if opskit_only:
        lines += ['## 精简管理端（可选）\n', '只需要 Hub 与 NPM 时在该主机执行，不能与完整管理中心在同一状态目录重复安装：',
                  '```bash', 'sudo bash install.sh --role hub --action install', 'sudo bash install.sh --role hub --action check', '```',
                  '精简管理端不提供 Spug、DBX 或集中日志接收；需要采集端时必须已有可达的完整日志中心。',
                  '## OpsKit 本体维护\n', '此包只提供 OpsKit 本体的首次安装、检查和文档同步，不提供其数据库跨版本自动升级或回退。',
                  '管理端配置、数据库和历史日志应按内置管理说明备份；不要用业务角色的 upgrade/rollback 命令维护 OpsKit 本体。']
        spug += ['## OpsKit 本体维护边界\n', 'OpsKit 管理端与采集端只支持本包提供的 install/check；下方文档同步是独立动作，不升级组件或迁移数据库。',
                 '业务应用版本更新应使用针对该应用构建、验收的交付包，并遵循其 Spug 发布配置和备份/回退说明。']
    for role in roles:
        ident, name = role['id'], role.get('name', role['id'])
        state = base + '/' + ident
        lines += ['## 业务角色：%s（%s）\n' % (name, ident)]
        if role.get('site_notes'):
            lines.append('此应用专有要求（必须逐项执行）：')
            lines.extend('%s. %s' % (number, note) for number, note in enumerate(role['site_notes'], 1))
        lines += ['首次安装（尚未安装该角色时）：', '```bash',
                  'sudo bash install.sh --role %s --action install' % ident, '```',
                  '首次安装按向导填写下表参数。已安装时使用 upgrade，不能再次使用 install 覆盖现场状态。',
                  '| 参数 | 含义 | 默认值 | 必填 |', '|---|---|---|---|']
        for parameter in role.get('parameters', []):
            default = '现场输入（隐藏）' if parameter.get('secret') else str(parameter.get('default') if parameter.get('default') is not None else '现场输入')
            lines.append('| %s | %s | %s | %s |' % (parameter['key'], parameter.get('label', parameter['key']),
                         default.replace('|', '\\|'), '是' if parameter.get('required') else '否'))
        if not role.get('parameters'):
            lines.append('| 无 | 此角色无可填写参数 | — | — |')
        lines += ['\n安装完成后单独验收：', '```bash', 'sudo bash install.sh --role %s --action check' % ident,
                  'sudo cat %s/result.json' % state, '```',
                  '成功判据：终端显示 `完成：%s / check`，result.json 中 `action` 为 `check`，`passed` 和 `checks_passed` 均为 `true`。' % ident]
        for check in role.get('checks', []):
            lines.append('- 业务 HTTP 检查：`%s`，参数使用现场填写值，要求 HTTP 状态 `%s`。' % (check['url'], check.get('status', 200)))
        if role.get('hooks', {}).get('check'):
            lines.append('- 专有验收脚本：`%s` 必须成功退出；具体业务判据见上述专有要求。' % role['hooks']['check'])
        lines += ['\n现场文件位置（不要放回发行包目录或上传到公开附件）：',
                  '- 现场参数：`%s/current/site.env`，包含密码，受限权限保存。' % state,
                  '- 已安装版本及上版记录：`%s/state.json`。' % state,
                  '- 操作结果：`%s/result.json`；详细日志：`%s/logs/`。' % (state, state),
                  '- 历史部署快照：`%s/history/`；数据备份：`%s/backups/`。' % (state, state)]
        if role['stateful'] or role.get('hooks', {}).get('backup'):
            lines += ['\n首次安装后和重要维护前执行备份，成功时显示已核验的备份目录：', '```bash',
                      'sudo bash install.sh --role %s --action backup' % ident, '```',
                      '备份目录必须有真实非空数据文件和 SHA256SUMS；仅复制 Compose 或输出“成功”不算数据备份。备份恢复演练由负责该系统的开发人员在验收环境完成。']
        lines += ['\n升级：先确认待升级版本与数据兼容策略，切换到新发行包目录，然后执行：', '```bash',
                  'sudo bash install.sh --role %s --action upgrade' % ident,
                  'sudo bash install.sh --role %s --action check' % ident, '```',
                  '升级复用已有现场参数；新增必填参数会提示填写。有状态角色在启动新版前自动执行已安装版的备份脚本，失败就停止。']
        lines.append('默认值更新不会覆盖已有参数。如果版本、路径或外部连接参数需要变更，在本次 upgrade 向导中明确填写；Spug 非交互升级不会替用户改旧参数。')
        if role['rollback'] == 'unsupported':
            lines.append('回退策略 `unsupported`：此发行版明确不支持自动回退，失败后停止并联系开发人员。')
        else:
            lines += ['回退命令（只针对记录中的上一版，不提供任意版本选择）：', '```bash',
                      'sudo bash install.sh --role %s --action rollback' % ident,
                      'sudo bash install.sh --role %s --action check' % ident, '```']
            if role['rollback'] == 'app-only':
                lines.append('回退策略 `app-only`：只恢复上版应用配置/镜像，业务数据不恢复。该版本必须由开发人员已确认数据库和数据格式向后兼容。没有上一版快照或所需镜像时入口会停止。')
            else:
                lines.append('回退策略 `restore-hook`：先备份当前数据，再用本版恢复脚本读取上次升级前已核验的数据备份，恢复上版应用。缺上一版快照、镜像或升级前备份时停止；恢复脚本必须已在断网验收环境演练。')
        spug += ['## %s\n' % name, '| Spug 字段 | 填写内容 |', '|---|---|', '| 应用名称 | %s |' % app['name'],
                 '| 应用标识 | %s-%s |' % (app['id'], ident),
                 '| 发布环境 | 客户现场 |', '| 发布方式 | 自定义发布 |', '| 发布配置名称 | %s · %s |' % (app['name'], name),
                 '| 目标主机 | 仅选择承担 %s 角色的主机 |' % ident,
                 '| 执行方式 | 串行；首台全部验收成功后再进行下一台 |',
                 '| 文件传输 | 完整包已预分发，默认不重复上传；不能只上传 compose |',
                 '| 本地动作 | 无；现场不 Git 拉取、不构建 |', '| 目标主机动作 | 原样复制下方升级命令 |\n',
                 '```bash', 'set -euo pipefail', 'cd /opt/opskit-deliveries/%s' % dirname,
                 'bash install.sh --role %s --action upgrade --non-interactive' % ident,
                 'bash install.sh --role %s --action check --non-interactive' % ident, '```',
                 '升级会读取 `%s/current/site.env`；新增必填参数无法由已有默认值满足时在部署前失败。先由工程师在终端交互执行 upgrade 完成参数确认，不能在 Spug 备注、命令或公开附件里填密码。' % state,
                 '发布申请选择本配置与目标主机，备注发行版本 `%s`，审核后执行。终端显示 `完成：%s / check` 且 result.json 的 passed/checks_passed=true 后才标记成功，保存 Spug 执行记录。' % (app['version'], ident),
                 '任一步失败立即停止后续主机，收集 `%s/result.json` 和 `%s/logs/`；需要回退时按《现场操作》中本角色已声明的策略执行，不自行删除数据或换镜像。\n' % (state, state)]
    document_steps = ['## 文档中心与文档更新\n',
                      '安装 management 或 hub 后，打开 Hub 主页中的“文档中心”，可预览本包交付手册、内置说明和开发者指定的项目文档。Markdown、文本原件可继续用 vim 查看；Word 原件与图片等资源保持在文件夹中。',
                      ('需要更新本包说明时，把完整新包复制、校验并解压到管理机，在新包目录执行以下命令，只同步文档，不更改 OpsKit 镜像、端口或凭据：' if opskit_only else
                       '新发行版不会自动假设业务机就是管理机。业务发布完成后，把完整新包复制、校验并解压到管理机，在新包目录执行以下命令，只同步文档，不更改 OpsKit 镜像、端口或凭据：'),
                      '```bash', 'sudo bash install.sh --role management --action sync-docs --non-interactive', '```',
                      '仅安装 hub 时将 management 改成 hub。默认管理库位于 `%s/management/opskit/management/documents`，Hub 独立角色对应 `%s/hub/opskit/management/documents`。' % (base, base),
                      '向其他已安装的 OpsKit 管理中心汇入文档时，显式指定该中心真实的 documents 目录：',
                      '```bash', 'sudo bash install.sh --role management --action sync-docs --non-interactive --documents-dir /实际路径/opskit/management/documents', '```',
                      '每个应用使用自己的 delivery/project/builtin 子目录；更新前先校验并备份原文档库，保留其他应用、手工文档和现场配置。同应用已被本地编辑或与无归属文件发生冲突时停止，不覆盖。首次将未登记的同名文档汇入旧中心时，也须先人工确认并迁移冲突文件。同步完成后刷新 Hub 文档列表即可，无需重启容器。备份路径会在终端显示。']
    lines += document_steps
    spug += ['## 管理中心的文档发布\n',
             '另建一条仅选择管理机的自定义发布配置，完整新版包已在管理机预分发并校验后，执行：',
             '```bash', 'set -euo pipefail', 'cd /opt/opskit-deliveries/%s' % dirname,
             'bash install.sh --role management --action sync-docs --non-interactive', '```',
             '业务主机升级命令不附带此动作；仅安装 hub 时改用 --role hub，已有共享中心使用《现场操作》中的 --documents-dir 选项。不把文档同步当作 OpsKit 自身升级；同步失败停止文档发布并查看错误与备份。']
    lines += ['## 交接\n', '记录客户、主机、角色、发行版本、部署时间、访问地址及实际%s验收结果。凭据通过客户批准的密码通道交接，不写入发布备注、聊天或普通工单附件。' % ('入口和日志链路' if opskit_only else '业务'),
              '保留本版和上版发行包、现场状态与数据备份。不要运行带 --purge-data 的卸载命令来处理升级失败。']
    (bundle / 'docs').mkdir(parents=True, exist_ok=True)
    (bundle / 'docs/现场操作.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    (bundle / 'docs/Spug操作.md').write_text('\n'.join(spug) + '\n', encoding='utf-8')


def build(repo, config_path, output, source=None, prerequisites=None, use_local=False):
    original_repo = Path(repo).absolute()
    repo = original_repo.resolve()
    unresolved_config = Path(config_path).absolute()
    if unresolved_config.is_relative_to(original_repo):
        relative_config = unresolved_config.relative_to(original_repo).as_posix()
    elif unresolved_config.is_relative_to(repo):
        relative_config = unresolved_config.relative_to(repo).as_posix()
    else:
        raise BundleError('制包配置必须是业务仓库内的普通文件')
    config_path = safe_path(repo, relative_config)
    config = validate_config(read_json(config_path))
    source = opskit_source(source)
    if not prerequisites:
        raise BundleError('缺少 --prerequisites-dir：请先准备并离线验证目标系统基础依赖，不能用空目录代替')
    verify_prerequisites(prerequisites, config['target'])
    artifact = uuid.uuid4().hex
    app, target = config['application'], config['target']
    dirname = '%s-%s-anolis%s-%s' % (app['id'], app['version'], target['version'], target['arch'])
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    bundle = output / dirname
    if bundle.exists():
        raise BundleError('输出已存在，禁止覆盖已有交付：%s' % bundle)
    # Atomic staging avoids leaving a partial directory that resembles a deliverable.
    temporary = Path(tempfile.mkdtemp(prefix='.opskit-build-', dir=str(output)))
    try:
        project_records = project_documents(repo, config.get('documents', []), temporary / DOCUMENT_ROOT, app['id'])
        docs, roles, requested = {}, [], []
        for role in config['roles']:
            destination = temporary / 'application' / role['id']
            destination.mkdir(parents=True)
            doc, rendered_role, images = role_document(repo, role, destination)
            docs[role['id']] = doc
            roles.append(rendered_role)
            for service, reference in images.items():
                requested.append((role['id'], service, reference))
        defaults = image_defaults(source, config)
        requested += [('opskit', key.lower().replace('_image', '').replace('_', '-'), ref) for key, ref in defaults.items()]
        records, resolved, archives, role_refs, ops_refs = [], {}, {}, {r['id']: [] for r in roles}, {}
        (temporary / 'images').mkdir()
        platform = 'linux/' + target['arch']
        for role, service, reference in requested:
            if reference not in resolved:
                resolved[reference] = resolve_image(reference, platform, use_local)
            image_id = resolved[reference]
            if image_id not in archives:
                archive = 'images/' + image_id.split(':')[1] + '.tar'
                run(['docker', 'image', 'save', '--output', str(temporary / archive), image_id])
                if not (temporary / archive).is_file() or (temporary / archive).stat().st_size == 0:
                    raise BundleError('镜像导出为空：%s' % reference)
                archives[image_id] = (archive, digest(temporary / archive), archive_metadata(temporary / archive, platform))
            alias = 'opskit-airgap/%s-%s-%s:%s-%s' % (app['id'], role, service, app['version'], artifact[:12])
            archive, checksum, metadata = archives[image_id]
            records.append({'reference': alias, 'source_reference': reference, 'image_id': image_id,
                            'platform': platform, 'archive': archive, 'sha256': checksum, **metadata})
            if role == 'opskit':
                key = next(key for key in defaults if key.lower().replace('_image', '').replace('_', '-') == service)
                ops_refs[key] = alias
            else:
                docs[role]['services'][service]['image'] = alias
                role_refs[role].append(alias)
        for role in roles:
            role['image_references'] = role_refs[role['id']]
            write_json(temporary / role['compose'], docs[role['id']])
        copy_opskit(source, temporary / 'opskit')
        for module in ('management', 'collector'):
            write_json(temporary / 'opskit' / module / 'image-defaults.json', {key: ops_refs[key] for key in IMAGE_KEYS[module]})
        copy_prerequisites(prerequisites, temporary / 'prerequisites', target)
        site_assets = Path(__file__).resolve().parent.parent / 'assets/site'
        for filename, dest in (('site.py', 'delivery/site.py'), ('install.sh', 'install.sh')):
            path = safe_path(site_assets, filename)
            target_path = temporary / dest
            target_path.parent.mkdir(parents=True, exist_ok=True)
            copy_deployment_file(path, target_path)
            target_path.chmod(0o755)
        manifest = {'schema_version': 1, 'artifact_id': artifact, 'created_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    'kind': config.get('kind', 'application'),
                    'application': app, 'target': target, 'roles': roles, 'images': records, 'opskit_path': 'opskit',
                    'opskit_images': ops_refs, 'opskit_roles': {name: {'image_references': [ops_refs[key] for key in keys]} for name, keys in IMAGE_KEYS.items()},
                    'prerequisites': 'prerequisites/manifest.json',
                    'verification': {'status': 'pending', 'images_exported': True, 'prerequisites_verified': True,
                                     'target_smoke': False, 'image_source_mode': 'local-verified-platform' if use_local else 'registry-immutable-digest'}}
        write_json(temporary / 'delivery/manifest.json', manifest)
        # Use final name in documents, while keeping atomic build staging private.
        write_documents(temporary, manifest)
        for doc in ('docs/现场操作.md', 'docs/Spug操作.md'):
            p = temporary / doc
            p.write_text(p.read_text(encoding='utf-8').replace(temporary.name, dirname), encoding='utf-8')
        library_documents(temporary, source, manifest, project_records)
        write_json(temporary / 'delivery/manifest.json', manifest)
        checksums(temporary)
        temporary.rename(bundle)
        return bundle
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def build_opskit(target, output, source=None, prerequisites=None, use_local=False, kit_version=None, images=None):
    source = opskit_source(source)
    if kit_version is None:
        title = safe_path(source, 'README.md').read_text(encoding='utf-8-sig').partition('\n')[0]
        match = re.search(r'\bv([0-9]+\.[0-9]+\.[0-9]+(?:-[a-z0-9.-]+)?)\b', title)
        if not match:
            raise BundleError('OpsKit README 首行没有版本号，请指定 --kit-version')
        kit_version = match[1]
    config = validate_config({'schema_version': 1, 'kind': 'opskit',
                             'application': {'id': 'opskit', 'name': 'OpsKit 运维工具包', 'version': kit_version},
                             'target': target, 'roles': [], 'opskit_images': {} if images is None else images})
    with tempfile.TemporaryDirectory(prefix='opskit-only-config-') as directory:
        path = Path(directory) / 'opskit-delivery.json'
        write_json(path, config)
        return build(directory, path, output, source, prerequisites, use_local)


def validate_acceptance(manifest, report):
    kind, roles = manifest.get('kind', 'application'), manifest.get('roles')
    if kind not in ('application', 'opskit') or not isinstance(roles, list):
        raise BundleError('暂存包 kind / roles 无效')
    if kind == 'application' and not roles or kind == 'opskit' and roles:
        raise BundleError('业务包必须含业务角色；OpsKit 本体包不得含业务角色')
    if kind == 'opskit':
        expected = set(IMAGE_KEYS['management'] + IMAGE_KEYS['collector'])
        references = manifest.get('opskit_images', {})
        images = manifest.get('images', [])
        if (not isinstance(references, dict) or set(references) != expected or
                any(not isinstance(value, str) for value in references.values()) or not isinstance(images, list) or
                len(images) != len(expected) or len(set(references.values())) != len(expected) or
                {item.get('reference') for item in images if isinstance(item, dict)} != set(references.values())):
            raise BundleError('OpsKit 本体包必须包含完整的 7 个内置镜像')
        modules = manifest.get('opskit_roles', {})
        for name, keys in IMAGE_KEYS.items():
            module = modules.get(name) if isinstance(modules, dict) else None
            if not isinstance(module, dict) or module.get('image_references') != [references[key] for key in keys]:
                raise BundleError('OpsKit 本体包缺少完整模块镜像引用：%s' % name)
    if report.get('schema_version') != 1 or report.get('artifact_id') != manifest.get('artifact_id') or report.get('target') != manifest.get('target'):
        raise BundleError('验收报告 schema / artifact_id / target 与暂存包不匹配')
    if report.get('network') != 'disabled' or report.get('method') not in ('anolis-vm', 'anolis-container'):
        raise BundleError('验收必须在目标龙蜥断网 VM / 容器执行，并记录测试方式')
    records = report.get('roles')
    if not isinstance(records, list) or len(records) != len(manifest['roles']):
        raise BundleError('验收报告必须覆盖每个业务角色')
    by_id = {r.get('id'): r for r in records if isinstance(r, dict)}
    if len(by_id) != len(records) or set(by_id) != {r['id'] for r in manifest['roles']}:
        raise BundleError('验收角色漏项、重复或不匹配')
    for role in manifest['roles']:
        record = by_id[role['id']]
        if record.get('passed') is not True or record.get('checks_passed') is not True or record.get('action') != 'install':
            raise BundleError('角色 %s 未通过真实安装和业务检查' % role['id'])
        if role['stateful'] and record.get('stateful_backup_passed') is not True:
            raise BundleError('有状态角色 %s 未通过真实备份验证' % role['id'])
    if report.get('opskit', {}).get('management') is not True or report.get('opskit', {}).get('collector') is not True:
        raise BundleError('完整包必须验证 OpsKit 管理端和采集端安装，Compose 解析不能代替运行验收')
    if report.get('prerequisites', {}).get('verified') is not True:
        raise BundleError('验收报告缺少离线基础依赖验证')


def seal(bundle, acceptance, output):
    bundle = Path(bundle).resolve()
    verify_checksums(bundle)
    manifest = read_json(safe_path(bundle, 'delivery/manifest.json'))
    report = read_json(acceptance)
    validate_acceptance(manifest, report)
    verify_prerequisites(bundle / 'prerequisites', manifest['target'])
    for record in manifest['images']:
        if digest(safe_path(bundle, record['archive'])) != record['sha256']:
            raise BundleError('镜像归档校验失败')
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    archive = output / (bundle.name + '.tar.gz')
    if archive.exists():
        raise BundleError('正式归档已存在，禁止覆盖：%s' % archive)
    # Seal a copy; preserve the tested staging tree and its original checksums.
    with tempfile.TemporaryDirectory(prefix='.opskit-seal-', dir=str(output)) as directory:
        sealed = Path(directory) / bundle.name
        shutil.copytree(bundle, sealed)
        write_json(sealed / 'delivery/verification/acceptance.json', report)
        manifest['verification'].update(status='verified', target_smoke=True, method=report['method'],
                                        acceptance='delivery/verification/acceptance.json',
                                        acceptance_sha256=digest(sealed / 'delivery/verification/acceptance.json'))
        write_json(sealed / 'delivery/manifest.json', manifest)
        checksums(sealed)
        verify_checksums(sealed)
        temporary_archive = Path(directory) / archive.name
        def linux_metadata(member):
            member.uid = member.gid = 0
            member.uname = member.gname = ''
            member.mode = 0o755 if member.isdir() or member.name.endswith('.sh') else 0o644
            return member

        with tarfile.open(temporary_archive, 'w:gz') as stream:
            stream.add(sealed, arcname=bundle.name, recursive=True, filter=linux_metadata)
        # No link, traversal or machine owner metadata in the archive.
        with tarfile.open(temporary_archive) as stream:
            for member in stream.getmembers():
                if member.issym() or member.islnk() or member.name.startswith('/') or '..' in PurePosixPath(member.name).parts:
                    raise BundleError('正式归档包含不安全路径')
        temporary_archive.rename(archive)
    (output / (archive.name + '.sha256')).write_text('%s  %s\n' % (digest(archive), archive.name), encoding='utf-8')
    return archive


def main(argv=None):
    parser = argparse.ArgumentParser(description='OpsKit 气隙交付制包器：检查 → 完整暂存 → 断网验收 → 正式封包')
    commands = parser.add_subparsers(dest='command', required=True)
    inspect_parser = commands.add_parser('inspect', help='只读检查业务仓库部署入口')
    inspect_parser.add_argument('--repo', required=True)
    build_parser = commands.add_parser('build', help='构造待验收的完整暂存目录，不生成正式交付归档')
    build_parser.add_argument('--repo', required=True)
    build_parser.add_argument('--config', required=True)
    build_parser.add_argument('--output', required=True)
    build_parser.add_argument('--opskit-source')
    build_parser.add_argument('--prerequisites-dir', required=True)
    build_parser.add_argument('--use-local-images', action='store_true', help='仅用于已按目标架构构建/验证的本地镜像；仍检查 image ID 和平台')
    opskit_parser = commands.add_parser('build-opskit', help='无需业务仓库，构造含全部内置镜像和系统依赖的 OpsKit 待验收气隙包')
    opskit_parser.add_argument('--os', choices=['anolis'], required=True)
    opskit_parser.add_argument('--version', choices=['8.9', '8.10'], required=True)
    opskit_parser.add_argument('--arch', choices=['amd64', 'arm64'], required=True)
    opskit_parser.add_argument('--output', required=True)
    opskit_parser.add_argument('--opskit-source')
    opskit_parser.add_argument('--prerequisites-dir', required=True)
    opskit_parser.add_argument('--use-local-images', action='store_true', help='仅用于已按目标架构构建/验证的本地镜像')
    opskit_parser.add_argument('--kit-version', help='发行版本；缺省读取 OpsKit README 首行的 vX.Y.Z')
    opskit_parser.add_argument('--image', action='append', default=[], metavar='KEY=REF', help='覆盖指定内置镜像来源，可重复；仅允许 7 个已知 *_IMAGE 变量')
    seal_parser = commands.add_parser('seal', help='验证真实验收报告后生成可交付 tar.gz')
    seal_parser.add_argument('--bundle', required=True)
    seal_parser.add_argument('--acceptance', required=True)
    seal_parser.add_argument('--output', required=True)
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == 'inspect':
            print(json.dumps(inspect_repo(arguments.repo), ensure_ascii=False, indent=2))
        elif arguments.command == 'build':
            path = build(arguments.repo, arguments.config, arguments.output, arguments.opskit_source,
                         arguments.prerequisites_dir, arguments.use_local_images)
            print('完整暂存目录已生成，尚未通过现场断网验收，不能交付：%s' % path)
        elif arguments.command == 'build-opskit':
            target = {'os': arguments.os, 'version': arguments.version, 'arch': arguments.arch}
            images = {}
            for value in arguments.image:
                key, separator, reference = value.partition('=')
                if not separator:
                    raise BundleError('--image 必须为 KEY=REF')
                images[key] = reference
            path = build_opskit(target, arguments.output, arguments.opskit_source,
                                arguments.prerequisites_dir, arguments.use_local_images, arguments.kit_version, images)
            print('OpsKit 本体完整暂存目录已生成，须断网验收管理端、采集端和基础依赖后封包：%s' % path)
        else:
            print('正式交付包：%s' % seal(arguments.bundle, arguments.acceptance, arguments.output))
        return 0
    except (BundleError, OSError, ValueError, TypeError, KeyError) as exc:
        print('制包未完成：%s' % exc, file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
