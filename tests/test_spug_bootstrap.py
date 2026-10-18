"""Spug 初始化的隔离边界、create-only 保留和真实巡检分支。"""
from contextlib import contextmanager, redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


SCRIPT = Path(__file__).resolve().parents[1] / 'management/scripts/spug_bootstrap.py'
spec = importlib.util.spec_from_file_location('spug_bootstrap', SCRIPT)
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)


class Record(SimpleNamespace):
    def save(self, update_fields=None):
        pass


class Manager:
    def __init__(self):
        self.rows = []

    def all(self):
        return self.rows[:]

    def filter(self, **query):
        def matches(row):
            for key, value in query.items():
                if key.endswith('__startswith'):
                    if not (getattr(row, key[:-12], '') or '').startswith(value):
                        return False
                elif key.endswith('__in'):
                    if getattr(row, key[:-4], None) not in value:
                        return False
                elif getattr(row, key, None) != value:
                    return False
            return True
        return Rows(row for row in self.rows if matches(row))

    def create(self, **values):
        row = Record(id=max((row.id for row in self.rows), default=0) + 1, **values)
        self.rows.append(row)
        return row

    def get(self, **query):
        rows = self.filter(**query)
        if len(rows) != 1:
            raise RuntimeError('configuration must be unique')
        return rows[0]


class Rows(list):
    def exists(self):
        return bool(self)


class Runtime:
    def __init__(self):
        self.settings = SimpleNamespace(SPUG_VERSION='v4.1.1', MONITOR_KEY='monitor', SCHEDULE_KEY='schedule')
        self.models = []
        for name in ('User', 'Environment', 'App', 'Service', 'Config', 'ConfigHistory', 'Detection', 'Task', 'History'):
            model = SimpleNamespace(objects=Manager(), _meta=SimpleNamespace(get_field=lambda field: field))
            setattr(self, name, model)
            self.models.append(model)
        self.User.objects.create(username='admin', is_supper=True, is_active=True, is_deleted=False)
        self.committed = False
        self.callbacks = []
        self.events = []
        self.redis = SimpleNamespace(ping=Mock(return_value=True), lpush=self.push)
        self.transaction = SimpleNamespace(atomic=self.atomic, on_commit=self.callbacks.append)
        self.serializers = SimpleNamespace(serialize=lambda fmt, objects, **kwargs:
                                           json.dumps([vars(row) for row in objects],
                                                      default=lambda row: row.id, **kwargs))
        self.now = lambda: '2026-10-18 12:00:00'

    @contextmanager
    def atomic(self):
        original = [model.objects.rows[:] for model in self.models]
        self.committed = False
        try:
            yield
        except Exception:
            for model, rows in zip(self.models, original):
                model.objects.rows = rows
            self.callbacks.clear()
            raise
        self.committed = True
        callbacks, self.callbacks = self.callbacks, []
        self.transaction.on_commit = self.callbacks.append
        for callback in callbacks:
            callback()

    def push(self, queue, message):
        assert self.committed, 'notifications must follow commit'
        self.events.append((queue, json.loads(message)))


class SpugBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.backups = Path(self.folder.name)
        self.runtime = Runtime()
        self.context = {'OPS_PUBLIC_HOST': 'ops.example.internal', 'OPS_PUBLIC_SCHEME': 'https',
                        'OPS_PUBLIC_PORT': '443', 'SPUG_ADMIN_USER': 'admin', 'DBX_PASSWORD': 'SECRET_MUST_NOT_PERSIST',
                        'DBX_IMAGE': 'dbx.example/dbx:0.6.40',
                        'VICTORIALOGS_IMAGE': 'logs.example/victoria-logs:v1.53.0'}

    def run_bootstrap(self):
        return bootstrap.bootstrap(self.context, self.runtime, self.backups)

    def test_creates_working_objects_and_official_event_types_after_commit(self):
        result = self.run_bootstrap()
        self.assertEqual(len(self.runtime.Environment.objects.rows), 1)
        self.assertEqual(len(self.runtime.App.objects.rows), 7)
        self.assertEqual(len(self.runtime.Config.objects.rows), 30)
        self.assertEqual(len(self.runtime.ConfigHistory.objects.rows), 30)
        self.assertEqual(len(result['monitors']), 7)
        self.assertEqual(len(result['tasks']), 1)
        self.assertEqual(len(self.runtime.events), 8)
        for queue, event in self.runtime.events:
            self.assertEqual(event['action'], 'add')
            self.assertIsInstance(event['targets'], str if queue == 'monitor' else list)
        task = self.runtime.Task.objects.get(id=result['tasks'][0])
        self.assertEqual(json.loads(task.targets), ['local'])
        self.assertEqual(task.trigger_args, '60')
        self.assertEqual(json.loads(task.rst_notify), {'mode': '0'})
        data = json.dumps([vars(row) for model in self.runtime.models for row in model.objects.rows], default=str)
        self.assertNotIn('SECRET_MUST_NOT_PERSIST', data + json.dumps(result))

    def test_repeated_run_preserves_edits_names_and_paused_state(self):
        first = self.run_bootstrap()
        counts = [len(model.objects.rows) for model in self.runtime.models]
        app = self.runtime.App.objects.rows[0]
        app.name = '用户自己的名字'
        config = self.runtime.Config.objects.rows[0]
        config.value = 'user-custom-value'
        detection = self.runtime.Detection.objects.rows[0]
        detection.name = '已重命名的健康检查'
        detection.is_active = False
        task = self.runtime.Task.objects.get(id=first['tasks'][0])
        task.command = 'print("user-owned-command")'
        task.is_active = False
        control = self.runtime.Task.objects.get(id=first['control_tasks'][0])
        control.name = '用户重命名的启动操作'
        control.command = 'print("user-owned-control")'
        image = self.runtime.Config.objects.get(key='image', value=self.context['DBX_IMAGE'])
        image.value = 'user.example/dbx:custom'
        self.runtime.events.clear()
        second = self.run_bootstrap()
        self.assertEqual(counts, [len(model.objects.rows) for model in self.runtime.models])
        self.assertEqual(second['created'], 0)
        self.assertEqual(second['conflicts'], [])
        self.assertEqual(app.name, '用户自己的名字')
        self.assertEqual(config.value, 'user-custom-value')
        self.assertEqual(task.command, 'print("user-owned-command")')
        self.assertEqual(control.name, '用户重命名的启动操作')
        self.assertEqual(control.command, 'print("user-owned-control")')
        self.assertEqual(image.value, 'user.example/dbx:custom')
        self.assertEqual(len(self.runtime.events), 6)
        self.assertNotEqual(first['backup'], second['backup'])

    def test_old_installation_stock_capabilities_migrate_with_backup_and_history(self):
        first = self.run_bootstrap()
        old_desc = bootstrap.MARKER + ' 仅登记与健康检查，未配置发布或宿主机控制'
        old_capability = '登记、HTTP/TCP健康监控；巡检含MariaDB SELECT 1；无宿主机启停/升级权限'
        controlled = self.runtime.App.objects.filter(key__in=['opskit_dbx', 'opskit_victorialogs'])
        for app in controlled:
            app.desc = old_desc
            self.runtime.Config.objects.get(type='app', o_id=app.id, key='capability').value = old_capability
        unrelated = self.runtime.App.objects.get(key='opskit_hub')
        unrelated.desc = old_desc
        self.runtime.Task.objects.rows = [row for row in self.runtime.Task.objects.rows if row.id in first['tasks']]
        self.runtime.Config.objects.rows = [row for row in self.runtime.Config.objects.rows if row.key != 'image']
        self.runtime.ConfigHistory.objects.rows = [row for row in self.runtime.ConfigHistory.objects.rows if row.key != 'image']
        result = self.run_bootstrap()
        self.assertEqual(len(result['control_tasks']), 8)
        self.assertEqual(len(result['tasks']), 1)
        self.assertEqual(unrelated.desc, old_desc)
        saved = json.loads(Path(result['backup']).read_text())
        for app in controlled:
            config = self.runtime.Config.objects.get(type='app', o_id=app.id, key='capability')
            self.assertIn('手动任务启动/停止/重启/升级', app.desc)
            self.assertIn('手动任务启动/停止/重启/升级', config.value)
            self.assertTrue(any(row.get('key') == app.key and row.get('desc') == old_desc for row in saved))
            self.assertTrue(any(row.get('o_id') == app.id and row.get('key') == 'capability' and row.get('value') == old_capability for row in saved))
            changes = self.runtime.ConfigHistory.objects.filter(type='app', o_id=app.id, key='capability', action='2')
            self.assertEqual(len(changes), 1)
            self.assertEqual(changes[0].old_value, old_capability)
            self.assertEqual(changes[0].value, config.value)
        before = len(self.runtime.ConfigHistory.objects.rows)
        self.run_bootstrap()
        self.assertEqual(len(self.runtime.ConfigHistory.objects.rows), before)

    def test_similar_or_manually_edited_capabilities_are_not_migrated(self):
        self.run_bootstrap()
        app = self.runtime.App.objects.get(key='opskit_dbx')
        app.desc = bootstrap.MARKER + ' 仅登记与健康检查，未配置发布或宿主机控制（人工补充）'
        config = self.runtime.Config.objects.get(type='app', o_id=app.id, key='capability')
        config.value = '登记、HTTP/TCP健康监控；巡检含MariaDB SELECT 1；无宿主机启停/升级权限（暂不授权）'
        before = app.desc, config.value, len(self.runtime.ConfigHistory.objects.rows)
        self.run_bootstrap()
        self.assertEqual((app.desc, config.value, len(self.runtime.ConfigHistory.objects.rows)), before)

    def test_foreign_app_key_is_preserved_without_attaching_configuration(self):
        foreign = self.runtime.App.objects.create(key='opskit_dbx', name='Existing DBX', desc='user-owned')
        result = self.run_bootstrap()
        self.assertIn('dbx', result['conflicts'])
        self.assertEqual(foreign.desc, 'user-owned')
        self.assertFalse(self.runtime.Config.objects.filter(o_id=foreign.id).exists())
        self.assertEqual(len(result['monitors']), 6)
        self.assertEqual(len(result['control_tasks']), 4)

    def test_service_key_collision_and_duplicate_environment_are_preserved(self):
        self.runtime.Service.objects.create(key='opskit_dbx', name='公共配置', desc='user-owned')
        result = self.run_bootstrap()
        self.assertIn('dbx', result['conflicts'])
        self.assertFalse(self.runtime.App.objects.filter(key='opskit_dbx').exists())
        self.runtime.Environment.objects.create(key='opskit_management', name='duplicate', desc=bootstrap.MARKER)
        before = [len(model.objects.rows) for model in self.runtime.models]
        result = self.run_bootstrap()
        self.assertIn('environment', result['conflicts'])
        self.assertEqual(before, [len(model.objects.rows) for model in self.runtime.models])

    def test_retry_after_post_commit_queue_failure_does_not_duplicate_objects(self):
        self.runtime.redis.lpush = Mock(side_effect=RuntimeError('Redis failure'))
        with self.assertRaises(RuntimeError):
            self.run_bootstrap()
        before = [len(model.objects.rows) for model in self.runtime.models]
        self.runtime.redis.lpush = self.runtime.push
        result = self.run_bootstrap()
        self.assertEqual(result['created'], 0)
        self.assertEqual(before, [len(model.objects.rows) for model in self.runtime.models])
        self.assertEqual(len(self.runtime.events), 8)

    def test_backup_is_private_verified_and_contains_original_values(self):
        self.runtime.App.objects.create(key='opskit_dbx', name='用户 DBX', desc='user-owned')
        self.runtime.App.objects.create(key='business', name='无关业务应用', desc='user-owned')
        self.runtime.History.objects.create(task_id=999, output='UNRELATED_SECRET_HISTORY')
        result = self.run_bootstrap()
        path = Path(result['backup'])
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        content = json.loads(path.read_text())
        self.assertEqual([item['key'] for item in content], ['opskit_dbx'])
        self.assertNotIn('SECRET_MUST_NOT_PERSIST', path.read_text())
        self.assertNotIn('UNRELATED_SECRET_HISTORY', path.read_text())

    def test_backup_includes_associated_configs_environment_and_task_history(self):
        first = self.run_bootstrap()
        app = self.runtime.App.objects.rows[0]
        environment = self.runtime.Environment.objects.create(key='other', name='另一个环境', desc='user-owned')
        self.runtime.Config.objects.create(type='app', o_id=app.id, env_id=environment.id,
                                           key='custom', value='user-value', desc='user-owned')
        for task_id in first['tasks'] + first['control_tasks']:
            self.runtime.History.objects.create(task_id=task_id, output=f'RELATED_HISTORY_{task_id}')
        self.runtime.History.objects.create(task_id=999, output='UNRELATED_HISTORY')
        result = self.run_bootstrap()
        content = Path(result['backup']).read_text()
        for task_id in first['tasks'] + first['control_tasks']:
            self.assertIn(f'RELATED_HISTORY_{task_id}', content)
        self.assertIn('user-value', content)
        self.assertIn('另一个环境', content)
        self.assertNotIn('UNRELATED_HISTORY', content)

    def test_manual_controls_are_never_scheduled_or_executed_during_bootstrap(self):
        with patch('subprocess.run') as execute:
            result = self.run_bootstrap()
        execute.assert_not_called()
        controls = self.runtime.Task.objects.filter(id__in=result['control_tasks'])
        self.assertEqual(len(controls), 8)
        self.assertEqual(len(self.runtime.Task.objects.rows), 9)
        for task in controls:
            self.assertEqual(task.interpreter, 'python')
            self.assertEqual(json.loads(task.targets), ['local'])
            self.assertEqual(task.trigger, 'date')
            self.assertEqual(task.trigger_args, '2000-01-01 00:00:00')
            self.assertFalse(task.is_active)
            self.assertIn('更多→执行测试', task.desc)
        scheduled = [event['id'] for queue, event in self.runtime.events if queue == 'schedule']
        self.assertEqual(scheduled, result['tasks'])

    def test_control_task_name_collision_preserves_user_task(self):
        task = self.runtime.Task.objects.create(name='OpsKit · DBX 停止', desc='user-owned', command='custom')
        result = self.run_bootstrap()
        self.assertIn('task:control:dbx:stop', result['conflicts'])
        self.assertEqual(len(result['control_tasks']), 7)
        self.assertEqual(task.command, 'custom')
        self.assertIn('custom', Path(result['backup']).read_text())

    def test_control_commands_wait_for_exit_status_and_only_upgrade_reads_current_image(self):
        result = self.run_bootstrap()
        image = self.runtime.Config.objects.get(key='image', value=self.context['DBX_IMAGE'])
        image.value = 'dbx.example/dbx:next;$(touch /tmp/unwanted)'
        django = SimpleNamespace(setup=Mock())
        modules = {'django': django, 'apps.config.models': SimpleNamespace(Config=self.runtime.Config)}
        for task in self.runtime.Task.objects.filter(id__in=result['control_tasks']):
            with self.subTest(task=task.name), patch.dict(sys.modules, modules), \
                    patch.object(sys, 'path', sys.path[:]), patch('os.chdir'), \
                    patch('subprocess.run', return_value=SimpleNamespace(returncode=7)) as execute, \
                    self.assertRaises(SystemExit) as exit_status:
                exec(task.command, {})
            arguments = execute.call_args.args[0]
            self.assertEqual(arguments[:2], [sys.executable, '/opt/opskit/control_client.py'])
            self.assertIn(arguments[2], ('start', 'stop', 'restart', 'upgrade'))
            self.assertIn(arguments[3], ('dbx', 'victorialogs'))
            self.assertEqual(execute.call_args.kwargs, {'timeout': 3500})
            if arguments[2] == 'upgrade':
                expected = image.value if arguments[3] == 'dbx' else self.context['VICTORIALOGS_IMAGE']
                self.assertEqual(arguments[4:], ['--image', expected])
            else:
                self.assertEqual(arguments[4:], [])
            self.assertEqual(exit_status.exception.code, 7)
        self.assertEqual(django.setup.call_count, 2)

    def test_upgrade_without_unique_image_fails_before_request_and_redacts_errors(self):
        self.run_bootstrap()
        task = self.runtime.Task.objects.get(name='OpsKit · DBX 升级')
        image = self.runtime.Config.objects.get(key='image', value=self.context['DBX_IMAGE'])
        django = SimpleNamespace(setup=Mock())
        modules = {'django': django, 'apps.config.models': SimpleNamespace(Config=self.runtime.Config)}
        for invalid in ('', None):
            image.value = invalid
            output = io.StringIO()
            with patch.dict(sys.modules, modules), patch.object(sys, 'path', sys.path[:]), \
                    patch('os.chdir'), patch('subprocess.run') as execute, \
                    patch('sys.stderr', output), self.assertRaises(SystemExit) as exit_status:
                exec(task.command, {})
            execute.assert_not_called()
            self.assertEqual(exit_status.exception.code, 1)
            self.assertIn('image', output.getvalue())
            self.assertNotIn('Traceback', output.getvalue())
        image.value = self.context['DBX_IMAGE']
        self.runtime.Config.objects.create(**{key: value for key, value in vars(image).items() if key != 'id'})
        with patch.dict(sys.modules, modules), patch.object(sys, 'path', sys.path[:]), \
                patch('os.chdir'), patch('subprocess.run') as execute, \
                patch('sys.stderr', io.StringIO()), self.assertRaises(SystemExit) as exit_status:
            exec(task.command, {})
        execute.assert_not_called()
        self.assertEqual(exit_status.exception.code, 1)

    def test_failed_backup_prevents_database_changes(self):
        with patch.object(bootstrap, 'backup_records', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.run_bootstrap()
        self.assertFalse(self.runtime.Environment.objects.rows)
        self.assertFalse(self.runtime.App.objects.rows)
        self.assertFalse(self.runtime.events)

    def test_creation_failure_rolls_back_and_sends_no_events(self):
        self.runtime.Config.objects.create = Mock(side_effect=RuntimeError('database failure'))
        with self.assertRaises(RuntimeError):
            self.run_bootstrap()
        self.assertFalse(self.runtime.Environment.objects.rows)
        self.assertFalse(self.runtime.App.objects.rows)
        self.assertFalse(self.runtime.events)
        self.assertEqual(len(list(self.backups.iterdir())), 1)

    def test_wrong_version_and_ambiguous_admin_fail_before_backup(self):
        self.runtime.settings.SPUG_VERSION = 'v4.1.2'
        with self.assertRaises(bootstrap.BootstrapError):
            self.run_bootstrap()
        self.assertFalse(list(self.backups.iterdir()))
        self.runtime.settings.SPUG_VERSION = 'v4.1.1'
        self.runtime.User.objects.rows[0].username = 'renamed'
        self.runtime.User.objects.create(username='other', is_supper=True, is_active=True, is_deleted=False)
        with self.assertRaises(bootstrap.BootstrapError):
            self.run_bootstrap()
        self.assertFalse(list(self.backups.iterdir()))

    def test_cli_error_redacts_untrusted_input_without_importing_django(self):
        context = dict(self.context, OPS_PUBLIC_HOST='INVALID SECRET_HOST')
        result = subprocess.run([sys.executable, str(SCRIPT)], input=json.dumps(context),
                                text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, '')
        self.assertIn('初始化失败', result.stderr)
        self.assertNotIn('SECRET', result.stderr)
        self.assertNotIn('Traceback', result.stderr)

    def test_health_task_runs_sql_http_tcp_and_redacts_failure_details(self):
        database = Mock()
        database.cursor.return_value.fetchone.return_value = (1,)
        mysql = SimpleNamespace(connect=Mock(return_value=database))
        response = Mock(status=200)
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        opener = SimpleNamespace(open=Mock(return_value=response))
        connection = Mock()
        connection.__enter__ = Mock(return_value=connection)
        connection.__exit__ = Mock(return_value=False)
        environment = {'MYSQL_HOST': 'spugdb', 'MYSQL_PASSWORD': 'SECRET_SQL_PASSWORD'}
        with patch.dict(sys.modules, MySQLdb=mysql), patch.dict(os.environ, environment), \
                patch('urllib.request.build_opener', return_value=opener), \
                patch('socket.create_connection', return_value=connection):
            output = io.StringIO()
            with redirect_stdout(output), self.assertRaises(SystemExit) as exit_status:
                exec(bootstrap.HEALTH_COMMAND, {})
            self.assertEqual(exit_status.exception.code, 0)
            self.assertEqual(len(output.getvalue().splitlines()), 7)
            database.cursor.return_value.execute.assert_called_once_with('SELECT 1')
            mysql.connect.side_effect = RuntimeError('SECRET_SQL_PASSWORD')
            output = io.StringIO()
            with redirect_stdout(output), self.assertRaises(SystemExit) as exit_status:
                exec(bootstrap.HEALTH_COMMAND, {})
            self.assertEqual(exit_status.exception.code, 1)
            self.assertIn('"healthy": false', output.getvalue())
            self.assertNotIn('SECRET', output.getvalue())


if __name__ == '__main__':
    unittest.main()
