#!/usr/bin/env python3
"""在 Spug 容器内登记服务、只读健康检查和受限宿主服务的手动操作。"""
import json
import os
from pathlib import Path
import re
import stat
import sys
from types import SimpleNamespace
from uuid import uuid4


VERSION = 'v4.1.1'
MARKER = '[opskit:self-management:v1]'
BACKUPS = Path('/data/backups/opskit-spug')
SERVICES = (
    ('hub', '工作台', 'workbench', 'http://workbench:8090/healthz', '/', '1', ''),
    ('dbx', 'DBX', 'dbx', 'http://dbx:4224/dbx/', '/dbx/', '1', ''),
    ('victorialogs', 'VictoriaLogs', 'victorialogs',
     'http://victorialogs:9428/logs/select/logsql/query?query=_time%3A1m&limit=1',
     '/logs/select/vmui/', '1', ''),
    ('npm', 'NPM', 'proxy-manager', 'proxy-manager', '/npm/', '2', '81'),
    ('mariadb', 'Spug MariaDB', 'spugdb', 'spugdb', '仅管理网络', '2', '3306'),
    ('spug', 'Spug', 'spug', 'http://127.0.0.1/', '/spug/', '1', ''),
    ('fluentbit', '管理端日志采集', 'internal-logs', 'http://internal-logs:2020/api/v1/health',
     '管理端内部采集', '1', ''),
)
CONTROL_SERVICES = (('dbx', 'DBX', 'DBX_IMAGE'),
                    ('victorialogs', 'VictoriaLogs', 'VICTORIALOGS_IMAGE'))
CONTROL_ACTIONS = (('start', '启动'), ('stop', '停止'), ('restart', '重启'), ('upgrade', '升级'))
CONTROL_COMMAND = '''import os
import subprocess
import sys

action, service, app_id, environment_id = %r
command = [sys.executable, '/opt/opskit/control_client.py', action, service]
try:
    if action == 'upgrade':
        os.chdir('/data/spug/spug_api')
        sys.path.insert(0, '/data/spug/spug_api')
        os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'spug.settings')
        import django
        django.setup()
        from apps.config.models import Config
        image = Config.objects.get(type='app', o_id=app_id, env_id=environment_id, key='image').value
        if not isinstance(image, str) or not image.strip():
            raise ValueError('missing image')
        command.extend(['--image', image])
    result = subprocess.run(command, timeout=3500)
except Exception:
    print('OpsKit 服务操作失败；请检查控制通道和配置中心的 image 配置。', file=sys.stderr)
    sys.exit(1)
sys.exit(result.returncode)
'''
HEALTH_COMMAND = '''import json
import os
import socket
import sys
from urllib.request import build_opener, ProxyHandler

checks = %s
opener = build_opener(ProxyHandler({}))
healthy = True
for key, target, kind, port in checks:
    ok = False
    try:
        if key == 'mariadb':
            import MySQLdb
            connection = MySQLdb.connect(
                host=os.environ['MYSQL_HOST'], port=int(os.environ.get('MYSQL_PORT', '3306')),
                user=os.environ.get('MYSQL_USER', 'spug'), passwd=os.environ['MYSQL_PASSWORD'],
                db=os.environ.get('MYSQL_DATABASE', 'spug'), connect_timeout=5)
            try:
                cursor = connection.cursor()
                cursor.execute('SELECT 1')
                ok = cursor.fetchone() == (1,)
                cursor.close()
            finally:
                connection.close()
        elif kind == '1':
            with opener.open(target, timeout=10) as response:
                ok = 200 <= response.status < 400
        else:
            with socket.create_connection((target, int(port)), timeout=5):
                ok = True
    except Exception:
        pass
    healthy = healthy and ok
    print(json.dumps({'service': key, 'healthy': ok}, ensure_ascii=False))
sys.exit(0 if healthy else 1)
''' % repr([(key, target, kind, port) for key, _, _, target, _, kind, port in SERVICES])


class BootstrapError(RuntimeError):
    pass


def load_runtime():
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'spug.settings')
    import django
    django.setup()
    from django.conf import settings
    from django.core import serializers
    from django.db import transaction
    from django_redis import get_redis_connection
    from apps.account.models import User
    from apps.app.models import App
    from apps.config.models import Environment, Service, Config, ConfigHistory
    from apps.monitor.models import Detection
    from apps.schedule.models import Task, History
    from libs import human_datetime
    return SimpleNamespace(settings=settings, serializers=serializers, transaction=transaction,
                           redis=get_redis_connection(), User=User, App=App, Environment=Environment, Service=Service,
                           Config=Config, ConfigHistory=ConfigHistory, Detection=Detection,
                           Task=Task, History=History, now=human_datetime)


def public_url(context):
    scheme = context.get('OPS_PUBLIC_SCHEME', 'https')
    host = context.get('OPS_PUBLIC_HOST', 'ops.example.internal')
    port = str(context.get('OPS_PUBLIC_PORT', '443' if scheme == 'https' else '80'))
    if (scheme not in ('http', 'https') or not isinstance(host, str) or
            not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?', host) or
            not port.isdigit() or not 1 <= int(port) <= 65535):
        raise BootstrapError('Spug 初始化的访问地址无效')
    suffix = '' if port == ('443' if scheme == 'https' else '80') else ':' + port
    return f'{scheme}://{host}{suffix}'


def backup_records(runtime, folder):
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)

    def matching(model, **lookup):
        rows = list(model.objects.filter(desc__startswith=MARKER)) + list(model.objects.filter(**lookup))
        return list({row.id: row for row in rows}.values())

    keys = ['opskit_' + item[0] for item in SERVICES]
    apps = matching(runtime.App, key__in=keys)
    services = matching(runtime.Service, key__in=keys)
    configs, changes = [], []
    for model, selected in ((runtime.App, apps), (runtime.Service, services)):
        query = {'type': 'app' if model is runtime.App else 'src', 'o_id__in': [row.id for row in selected]}
        configs.extend(runtime.Config.objects.filter(**query))
        changes.extend(runtime.ConfigHistory.objects.filter(**query))
    environments = matching(runtime.Environment, key='opskit_management')
    related_environments = runtime.Environment.objects.filter(id__in=[row.env_id for row in configs])
    environments = list({row.id: row for row in [*environments, *related_environments]}.values())
    detections = matching(runtime.Detection, name__in=['OpsKit · ' + item[1] + ' 健康' for item in SERVICES])
    task_names = ['OpsKit · 管理服务只读巡检'] + [
        f'OpsKit · {name} {label}' for _, name, _ in CONTROL_SERVICES for _, label in CONTROL_ACTIONS]
    tasks = matching(runtime.Task, name__in=task_names)
    histories = list(runtime.History.objects.filter(task_id__in=[row.id for row in tasks]))
    objects = [*environments, *apps, *services, *configs, *changes, *detections, *tasks, *histories]
    content = runtime.serializers.serialize('json', objects, ensure_ascii=False).encode('utf-8')
    path = folder / f'pre-bootstrap-{uuid4().hex}.json'
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'wb') as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    if (path.read_bytes() != content or stat.S_IMODE(path.stat().st_mode) != 0o600 or
            len(json.loads(content)) != len(objects)):
        raise BootstrapError('Spug 初始化备份校验失败；没有修改配置')
    return str(path)


def bootstrap(context, runtime=None, backup_folder=BACKUPS):
    if not isinstance(context, dict):
        raise BootstrapError('Spug 初始化需要 JSON 对象')
    base_url = public_url(context)
    username = context.get('SPUG_ADMIN_USER', os.environ.get('SPUG_ADMIN_USER', 'admin'))
    if not isinstance(username, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_.-]{0,31}', username):
        raise BootstrapError('Spug 初始化管理员标识无效')
    runtime = runtime or load_runtime()
    if runtime.settings.SPUG_VERSION != VERSION:
        raise BootstrapError('Spug 初始化仅支持已核验的 v4.1.1，请核对镜像版本')
    for model, fields in ((runtime.Environment, ('key', 'created_by')),
                          (runtime.App, ('key', 'created_by')),
                          (runtime.Config, ('type', 'o_id', 'env', 'key', 'updated_by')),
                          (runtime.Detection, ('type', 'targets', 'extra', 'rate', 'notify_mode', 'notify_grp')),
                          (runtime.Task, ('interpreter', 'targets', 'trigger', 'trigger_args', 'rst_notify'))):
        for field in fields:
            model._meta.get_field(field)
    actors = list(runtime.User.objects.filter(username=username, is_supper=True, is_active=True, is_deleted=False))
    if not actors:
        actors = list(runtime.User.objects.filter(is_supper=True, is_active=True, is_deleted=False))
    if len(actors) != 1:
        raise BootstrapError('Spug 初始化需要一个可确定的现有管理员；账户未作更改')
    actor = actors[0]
    runtime.redis.ping()
    result = {'created': 0, 'preserved': 0, 'conflicts': [], 'monitors': [], 'tasks': [], 'control_tasks': [],
              'scope': 'container-network-health-and-service-control', 'backup': backup_records(runtime, backup_folder)}
    notifications = []

    def ensure(model, key, lookup, defaults):
        matches = list(model.objects.filter(**lookup))
        if len(matches) > 1 or (matches and not (matches[0].desc or '').startswith(MARKER)):
            result['conflicts'].append(key)
            return None, False
        if matches:
            result['preserved'] += 1
            return matches[0], False
        result['created'] += 1
        return model.objects.create(**lookup, **defaults), True

    def named(model, key, name, defaults, description='仅登记及容器网络健康，无宿主机启停权限'):
        marker = f'{MARKER} {key}'
        owned = list(model.objects.filter(desc__startswith=marker + ' '))
        if len(owned) == 1:
            result['preserved'] += 1
            return owned[0]
        if len(owned) > 1 or model.objects.filter(name=name).exists():
            result['conflicts'].append(key)
            return None
        result['created'] += 1
        return model.objects.create(name=name, desc=marker + ' ' + description,
                                    created_by=actor, **defaults)

    with runtime.transaction.atomic():
        environment, _ = ensure(runtime.Environment, 'environment', {'key': 'opskit_management'},
                                {'name': 'OpsKit 管理中心', 'desc': MARKER, 'created_by': actor})
        if environment:
            for key, name, service, target, entry, kind, port in SERVICES:
                if runtime.Service.objects.filter(key='opskit_' + key).exists():
                    result['conflicts'].append(key)
                    continue
                controlled = key in {item[0] for item in CONTROL_SERVICES}
                capability = ('登记、HTTP/TCP健康监控；巡检含MariaDB SELECT 1；'
                              + ('通过手动任务启动/停止/重启/升级本安装服务' if controlled else '无宿主机启停/升级权限'))
                app, _ = ensure(runtime.App, key, {'key': 'opskit_' + key},
                                {'name': 'OpsKit · ' + name, 'created_by': actor,
                                 'desc': MARKER + ' ' + capability})
                if not app:
                    continue
                if controlled and app.desc == MARKER + ' 仅登记与健康检查，未配置发布或宿主机控制':
                    app.desc = MARKER + ' ' + capability
                    app.save(update_fields=['desc'])
                values = {'compose_service': service, 'internal_endpoint': target + (':' + port if kind == '2' else ''),
                          'entry': base_url.rstrip('/') + entry if entry.startswith('/') else entry,
                          'capability': capability}
                if controlled:
                    image_key = next(item[2] for item in CONTROL_SERVICES if item[0] == key)
                    values['image'] = context.get(image_key, os.environ.get(image_key, ''))
                for config_key, value in values.items():
                    config, created = ensure(runtime.Config, key + '.' + config_key,
                                             {'type': 'app', 'o_id': app.id, 'env_id': environment.id, 'key': config_key},
                                             {'value': value, 'desc': MARKER,
                                              'updated_at': runtime.now(), 'updated_by': actor})
                    old_value, action = None, '1' if created else None
                    if (config and not created and controlled and config_key == 'capability' and
                            config.value == '登记、HTTP/TCP健康监控；巡检含MariaDB SELECT 1；无宿主机启停/升级权限'):
                        old_value, action = config.value, '2'
                        config.value, config.updated_at, config.updated_by = value, runtime.now(), actor
                        config.save(update_fields=['value', 'updated_at', 'updated_by'])
                    if action:
                        runtime.ConfigHistory.objects.create(
                            type='app', o_id=app.id, env_id=environment.id, key=config_key,
                            value=value, old_value=old_value, desc=MARKER, action=action,
                            updated_at=runtime.now(), updated_by=actor)
                detection = named(runtime.Detection, 'monitor:' + key, 'OpsKit · ' + name + ' 健康',
                                  {'group': 'OpsKit 管理中心', 'type': kind, 'targets': json.dumps([target]),
                                   'extra': port, 'rate': 1, 'threshold': 3, 'quiet': 60,
                                   'notify_mode': '[]', 'notify_grp': '[]', 'is_active': True})
                if detection:
                    result['monitors'].append(detection.id)
                    if detection.is_active:
                        message = {'id': detection.id, 'action': 'add'}
                        message.update({field: getattr(detection, field) for field in
                                        ('targets', 'extra', 'rate', 'type', 'threshold', 'quiet')})
                        notifications.append((runtime.settings.MONITOR_KEY, message))
                if controlled:
                    for action, label in CONTROL_ACTIONS:
                        control = named(runtime.Task, f'task:control:{key}:{action}', f'OpsKit · {name} {label}',
                                        {'type': 'OpsKit 手动服务操作', 'interpreter': 'python',
                                         'command': CONTROL_COMMAND % ((action, key, app.id, environment.id),),
                                         'targets': '["local"]', 'trigger': 'date',
                                         'trigger_args': '2000-01-01 00:00:00', 'is_active': False,
                                         'rst_notify': '{"mode":"0"}'},
                                        '手动执行：更多→执行测试；不激活调度；升级读取配置中心 image，执行结果见历史记录')
                        if control:
                            result['control_tasks'].append(control.id)
            task = named(runtime.Task, 'task:health', 'OpsKit · 管理服务只读巡检',
                         {'type': 'OpsKit 只读巡检', 'interpreter': 'python', 'command': HEALTH_COMMAND,
                          'targets': '["local"]', 'trigger': 'interval', 'trigger_args': '60',
                          'is_active': True, 'rst_notify': '{"mode":"0"}'})
            if task:
                result['tasks'].append(task.id)
                if task.is_active and task.trigger != 'monitor':
                    notifications.append((runtime.settings.SCHEDULE_KEY,
                                          {'id': task.id, 'action': 'add', 'interpreter': task.interpreter,
                                           'trigger': task.trigger, 'trigger_args': task.trigger_args,
                                           'command': task.command, 'targets': json.loads(task.targets)}))
        def notify():
            for queue, message in notifications:
                runtime.redis.lpush(queue, json.dumps(message))
        runtime.transaction.on_commit(notify)
    return result


if __name__ == '__main__':
    try:
        print(json.dumps(bootstrap(json.load(sys.stdin)), ensure_ascii=False))
    except Exception:
        # 数据库/配置异常可能夹带凭据，终端只保留固定提示。
        print('Spug 自管理初始化失败；未覆盖既有配置，请检查版本、管理员、备份权限和调度进程后重试。', file=sys.stderr)
        sys.exit(1)
