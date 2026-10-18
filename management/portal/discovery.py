#!/usr/bin/env python3
"""本机只读发现：Docker 元数据、TCP 监听与 HTTP HEAD 探测。"""
import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.client import HTTPConnection, HTTPSConnection, HTTPException
import ipaddress
import json
import os
from pathlib import Path
import re
import signal
import ssl
import subprocess
import sys
import tempfile
import threading
import time

WEB_PORTS = {80, 443, 3000, 4000, 4173, 5000, 5173, 5678, 7860, 8000, 8008,
             8080, 8081, 8090, 8181, 8443, 8501, 8888, 9000, 9090, 9428}
PORT_MAPPING = re.compile(r'(\[[^\]]+\]|[^,\s]+):(\d+)->(\d+)/(tcp|udp)')
DOCKER_FORMAT = ('{"id":{{json .ID}},"name":{{json .Names}},"image":{{json .Image}},'
                 '"state":{{json .State}},"status":{{json .Status}},'
                 '"project":{{json (.Label "com.docker.compose.project")}},"ports":{{json .Ports}}}')



def container_ports(container):
    ports = []
    for match in PORT_MAPPING.finditer(container.get('ports', '')):
        bind, published, target, protocol = match.groups()
        address = bind.strip('[]')
        if address in ('0.0.0.0', '::'):
            address = '127.0.0.1'
        if ':' in address:
            address = '[' + address + ']'
        url = ''
        if container['state'] == 'running' and protocol == 'tcp' and int(target) in WEB_PORTS:
            scheme = 'https' if int(target) in (443, 8443) else 'http'
            url = f'{scheme}://{address}:{published}/'
        ports.append({'binding': f'{bind}:{published}', 'target': target + '/' + protocol,
                      'url': url, 'port': int(published), 'bind': bind.strip('[]')})
    return ports


class DockerDiscovery:
    def __init__(self):
        self.lock = threading.Lock()
        self.updated = 0
        self.result = ([], None)

    def read(self):
        with self.lock:
            if time.monotonic() - self.updated < 5:
                return self.result
            try:
                endpoint = os.environ.get('DOCKER_HOST') if not os.environ.get('DOCKER_CONTEXT') else None
                if not endpoint:
                    endpoint = subprocess.run(['docker', 'context', 'inspect', '--format', '{{(index .Endpoints "docker").Host}}'],
                                              capture_output=True, text=True, check=True, timeout=10).stdout.strip()
                if not endpoint.startswith(('unix://', 'npipe://')):
                    self.result = ([], '当前为远程 Docker 上下文，请切换到本机上下文后刷新。')
                else:
                    output = subprocess.run(['docker', 'ps', '-a', '--format', DOCKER_FORMAT],
                                            capture_output=True, text=True, check=True, timeout=15).stdout
                    containers = []
                    for line in output.splitlines():
                        item = json.loads(line)
                        item['project'] = item.get('project') or '未分组'
                        item['published_ports'] = container_ports(item)
                        containers.append(item)
                    self.result = (containers, None)
            except (OSError, subprocess.SubprocessError, ValueError):
                self.result = ([], '暂时无法读取 Docker，请确认 Docker 已启动且当前账户有访问权限。')
            self.updated = time.monotonic()
            return self.result


def listener(address, name='本机服务'):
    bind, separator, port = address.rpartition(':')
    bind = bind.strip('[]')
    if not separator or not port.isdigit() or not 1 <= int(port) <= 65535:
        return None
    if bind == '*':
        bind = '0.0.0.0'
    try:
        ipaddress.ip_address(bind)
    except ValueError:
        return None
    return {'bind': bind, 'port': int(port), 'name': name[:100]}


def parse_listeners(output, source):
    result = []
    if source == 'windows':
        records = json.loads(output.lstrip('\ufeff') or '[]')
        if isinstance(records, dict):
            records = [records]
        if records is None:
            records = []
        if not isinstance(records, list):
            raise ValueError('无效的 Windows 监听清单')
        for record in records:
            if not isinstance(record, dict):
                continue
            item = listener(f"{record.get('bind', '')}:{record.get('port', '')}", str(record.get('name') or '本机服务'))
            if item:
                result.append(item)
        return result
    name = '本机服务'
    for line in output.splitlines():
        item = None
        if source == 'lsof':
            if line.startswith('c'):
                name = line[1:] or '本机服务'
            elif line.startswith('n'):
                item = listener(line[1:], name)
        else:
            fields = line.split()
            if len(fields) >= 5:
                match = re.search(r'users:\(\("([^"\n]+)"', line)
                item = listener(fields[3], match.group(1) if match else '本机服务')
        if item:
            result.append(item)
    return result


def probe_http(item):
    address = item['bind']
    if address in ('0.0.0.0', '::'):
        address = '127.0.0.1' if address == '0.0.0.0' else '::1'
    # 只访问监听清单中的本机地址；不跟随重定向，不读页面、Cookie 或凭据。
    schemes = ('https', 'http') if item['port'] in (443, 8443) else ('http', 'https')
    fallback = None
    for scheme in schemes:
        connection = None
        try:
            if scheme == 'https':
                connection = HTTPSConnection(address, item['port'], timeout=0.6,
                                             context=ssl._create_unverified_context())
            else:
                connection = HTTPConnection(address, item['port'], timeout=0.6)
            connection.request('HEAD', '/', headers={'User-Agent': 'OpsKit-Hub/0.4', 'Connection': 'close'})
            response = connection.getresponse()
            if not 100 <= response.status <= 599:
                continue
            host = '[' + address + ']' if ':' in address else address
            found = dict(item, url=f'{scheme}://{host}:{item["port"]}/', status=response.status)
            # HTTPS 服务有时以 HTTP 400 提示改用 TLS，需要再试一次 HTTPS。
            if scheme == 'http' and response.status == 400:
                fallback = found
                continue
            return found
        except (OSError, HTTPException, ValueError):
            pass
        finally:
            if connection:
                connection.close()
    return fallback


def discover_http(containers):
    try:
        source = 'windows' if sys.platform == 'win32' else ('lsof' if sys.platform == 'darwin' else 'ss')
        if source == 'windows':
            script = r'''
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$connections = @(Get-NetTCPConnection -State Listen -ErrorAction Stop | ForEach-Object {
    $process = Get-Process -Id $_.OwningProcess -ErrorAction SilentlyContinue
    [PSCustomObject]@{bind=$_.LocalAddress; port=$_.LocalPort; name=$process.ProcessName}
})
ConvertTo-Json -InputObject $connections -Compress
'''
            command = ['powershell.exe', '-NoProfile', '-NonInteractive', '-EncodedCommand',
                       base64.b64encode(script.encode('utf-16-le')).decode('ascii')]
        else:
            command = ['lsof', '-nP', '-iTCP', '-sTCP:LISTEN', '-Fpcn'] if source == 'lsof' else ['ss', '-H', '-ltnp']
        output = subprocess.run(command, capture_output=True, text=True, encoding='utf-8-sig', timeout=15)
        # lsof 的退出码 1 也可能表示本机没有监听端口。
        if output.returncode not in ((0, 1) if source == 'lsof' else (0,)):
            raise OSError('无法读取监听端口')
        candidates = {}
        for item in parse_listeners(output.stdout, source):
            # 双栈同端口保留一项。
            candidates.setdefault(item['port'], item)
        docker_ports = {}
        for container in containers:
            if container['state'] == 'running':
                for port in container['published_ports']:
                    if port['target'].endswith('/tcp'):
                        docker_ports[port['port']] = port
                        candidates.setdefault(port['port'], dict(bind=port['bind'], port=port['port'], name=container['name']))
        ordered = sorted(candidates.values(), key=lambda item: item['port'])
        warning = '监听端口超过 128 个，本次仅检查前 128 个，其余服务可手动登记。' if len(ordered) > 128 else None
        with ThreadPoolExecutor(max_workers=12) as pool:
            services = [item for item in pool.map(probe_http, ordered[:128]) if item]
        for item in services:
            if item['port'] in docker_ports:
                docker_ports[item['port']]['url'] = item['url']
        return [item for item in services if item['port'] not in docker_ports], warning
    except (OSError, subprocess.SubprocessError, ValueError):
        requirement = 'Windows 需要可用的 PowerShell 和 Get-NetTCPConnection。' if sys.platform == 'win32' else 'macOS 需要 lsof，Linux 需要 ss（iproute 软件包）。'
        return [], '暂时无法读取本机 HTTP 服务；' + requirement


class Discovery:
    def __init__(self):
        self.docker = DockerDiscovery()
        self.lock = threading.Lock()
        self.updated = 0
        self.result = {}

    def read(self):
        with self.lock:
            if time.monotonic() - self.updated >= 15:
                containers, docker_error = self.docker.read()
                services, http_error = discover_http(containers)
                self.result = {'containers': containers, 'docker_error': docker_error,
                               'http_services': services, 'http_error': http_error,
                               'discovered_at': datetime.now(timezone.utc).isoformat()}
                self.updated = time.monotonic()
            return self.result


class SnapshotDiscovery:
    def __init__(self, path):
        self.path = Path(path)

    def read(self):
        try:
            result = json.loads(self.path.read_text(encoding='utf-8'))
            if not isinstance(result['containers'], list) or not isinstance(result['http_services'], list):
                raise ValueError('发现快照格式错误')
            if time.time() - self.path.stat().st_mtime > 90:
                for key in ('docker_error', 'http_error'):
                    result[key] = '宿主机发现结果超过 90 秒未更新，请检查发现进程；以下是上次结果。'
            return result
        except (OSError, ValueError, KeyError, TypeError):
            message = '尚未收到宿主机发现结果，请重新运行管理端安装脚本启动发现进程。'
            return {'containers': [], 'http_services': [], 'docker_error': message, 'http_error': message}


def write_snapshot(path, value):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.discovery-')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='OpsKit 宿主机入口发现及可选受限执行通道')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--control', action='store_true', help='启用本安装 DBX/VictoriaLogs 固定操作')
    args = parser.parse_args()
    stop = threading.Event()
    control = None
    if args.control:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
        from service_control import Controller
        controller = Controller()
        if not args.once:
            control = threading.Thread(target=controller.serve, args=(stop,), daemon=False)
            control.start()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    discovery = Discovery()
    try:
        while not stop.is_set():
            write_snapshot(args.output, discovery.read())
            if args.once:
                break
            stop.wait(30)
    finally:
        stop.set()
        if control:
            control.join()
