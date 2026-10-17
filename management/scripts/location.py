#!/usr/bin/env python3
"""选择安装位置，校验完整副本后从新位置继续；不覆盖已有安装。"""
import argparse
import hashlib
import os
from pathlib import Path
import platform
import shutil
import stat
import sys


IGNORED = {'.git', '__pycache__', '.DS_Store', '.venv', 'node_modules'}


def linked(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    # Python 3.9 尚无 Path.is_junction；Windows 重解析点也不允许作为复制路径。
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, 'st_file_attributes', 0) & 0x400)


def digest(path):
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def recommendations(source, system=None, home=None, environment=None):
    system = system or platform.system()
    home = Path(home or Path.home())
    environment = os.environ if environment is None else environment
    name = 'opskit'
    if not (source / 'management').is_dir():
        name += '-collector' if (source / 'fluent-bit.conf').is_file() else '-management'
    if system == 'Windows':
        return [(Path(environment.get('LOCALAPPDATA', home / 'AppData/Local')) / name, '当前用户应用目录，推荐'),
                (home / name, '当前用户目录'),
                (Path(environment.get('ProgramData', 'C:/ProgramData')) / name, '系统共享目录，需要写入权限')]
    if system == 'Darwin':
        return [(home / 'Applications' / name, '个人应用目录，推荐'),
                (home / name, '当前用户目录'),
                (Path('/usr/local/share') / name, '系统共享目录，需要写入权限')]
    return [(Path('/opt') / name, '系统应用目录，需要写入权限'),
            (Path('/srv') / name, '服务器服务目录，需要写入权限'),
            (home / name, '当前用户目录，无需管理员权限')]


def destination_path(source, value):
    if not value or any(ord(char) < 32 for char in str(value)):
        raise ValueError('安装目录不能为空或含控制字符')
    raw = Path(value).expanduser().absolute()
    for path in (raw, *raw.parents):
        # macOS 的系统别名；其余链接不作为安装目标，避免写到意外位置。
        system_alias = sys.platform == 'darwin' and path in (Path('/tmp'), Path('/var'))
        if not system_alias and linked(path):
            raise ValueError('安装目录不能经过软链接或目录联接：' + str(path))
    target = raw.resolve()
    if target == source:
        return target
    if target.is_relative_to(source) or source.is_relative_to(target):
        raise ValueError('新位置不能是安装包的父目录或子目录，请选择独立目录')
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise ValueError('目标位置已有内容，已保留原配置；请选择空目录、新目录或保留当前位置')
    return target


def inventory(source):
    files, directories = {}, []
    for parent, names, filenames in os.walk(source, followlinks=False):
        folder = Path(parent)
        names[:] = sorted(name for name in names if name not in IGNORED and not (folder == source and name == 'dist'))
        for name in names:
            path = folder / name
            if linked(path):
                raise ValueError('安装包含链接，停止复制：' + str(path.relative_to(source)))
            directories.append(path.relative_to(source))
        for name in sorted(filenames):
            if name in IGNORED or name.endswith(('.pyc', '.pyo')):
                continue
            path = folder / name
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                raise ValueError('安装包含链接或特殊文件，停止复制：' + str(path.relative_to(source)))
            files[path.relative_to(source)] = (digest(path), info.st_size, stat.S_IMODE(info.st_mode))
    return files, directories


def copy_package(source, destination):
    destination = destination_path(source, destination)
    if destination == source:
        return destination
    files, directories = inventory(source)
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    print('正在复制安装包并校验 {} 个文件：{}'.format(len(files), destination), flush=True)
    try:
        for name in directories:
            (destination / name).mkdir(mode=0o700)
        for name, (checksum, size, mode) in files.items():
            target = destination / name
            # 独占创建，既不覆盖配置，也不跟随复制期间插入的文件链接。
            with target.open('xb') as outgoing:
                private = (target.name.startswith('.env') and target.name != '.env.example') or target.name.endswith('.env')
                if private and sys.platform == 'win32':
                    from common import protect_private
                    protect_private(target)
                with (source / name).open('rb') as incoming:
                    shutil.copyfileobj(incoming, outgoing, 1024 * 1024)
            shutil.copystat(source / name, target)
            if private and sys.platform != 'win32':
                target.chmod(0o600)
            if target.stat().st_size != size or digest(target) != checksum:
                raise ValueError('复制文件校验失败：' + str(name))
            if sys.platform != 'win32' and not private and stat.S_IMODE(target.stat().st_mode) != mode:
                raise ValueError('复制文件权限校验失败：' + str(name))
        after, after_directories = inventory(source)
        if after != files or after_directories != directories:
            raise ValueError('复制期间源安装包发生变化，请停止其他写入后重新选择新目录')
        actual, actual_directories = inventory(destination)
        if (set(actual) != set(files) or actual_directories != directories or
                any(actual[name][:2] != record[:2] for name, record in files.items())):
            raise ValueError('目标目录文件发生变化，停止启动')
    except BaseException:
        print('复制未完成，未启动目标安装器；原安装包和目标文件均已保留：' + str(destination), file=sys.stderr)
        raise
    print('复制与校验完成，原安装包保留：' + str(source), flush=True)
    return destination


def location_arguments(arguments):
    target, keep, remaining = None, False, []
    index = 0
    while index < len(arguments):
        value = arguments[index]
        if value == '--install-dir':
            index += 1
            if index >= len(arguments) or arguments[index].startswith('--'):
                raise ValueError('--install-dir 需要一个目录')
            if target is not None:
                raise ValueError('--install-dir 不能重复指定')
            target = arguments[index]
        elif value.startswith('--install-dir='):
            if target is not None:
                raise ValueError('--install-dir 不能重复指定')
            target = value.split('=', 1)[1]
        elif value == '--keep-location':
            keep = True
        else:
            remaining.append(value)
        index += 1
    if keep and target is not None:
        raise ValueError('--install-dir 与 --keep-location 不能同时使用')
    return target, keep, remaining


def choose(source, arguments):
    target, keep, remaining = location_arguments(arguments)
    print('当前安装包位置：' + str(source), flush=True)
    print('当前终端目录：' + str(Path.cwd()), flush=True)
    if target is not None:
        return copy_package(source, target), remaining
    only_check = any(arg.lower() in ('--help', '-h', '--configure-only', '-configureonly', '-?') for arg in remaining)
    if keep or only_check or not sys.stdin.isatty():
        print('保留当前位置；可用 --install-dir 指定安装目录。', flush=True)
        return source, remaining
    choices = recommendations(source)
    default = 3 if platform.system() == 'Linux' and getattr(os, 'geteuid', lambda: 1)() != 0 else 1
    print('\n请选择安装位置（已安装的非空目录不会被覆盖）：')
    for number, (path, description) in enumerate(choices, 1):
        print('  {}) {}（{}）'.format(number, path, description))
    print('  4) 保留当前位置\n  5) 自定义目录\n  0) 取消安装')
    while True:
        answer = input('请选择 [{}]：'.format(default)).strip() or str(default)
        if answer == '0':
            raise KeyboardInterrupt
        if answer == '4':
            return source, remaining
        if answer == '5':
            value = input('请输入安装目录：').strip()
        elif answer in ('1', '2', '3'):
            value = choices[int(answer) - 1][0]
        else:
            print('请选择 0—5。')
            continue
        try:
            return copy_package(source, value), remaining
        except (OSError, ValueError) as error:
            print('无法使用该目录：{}。请重新选择；系统目录需要写入权限。'.format(error))


def main():
    parser = argparse.ArgumentParser(description='选择 OpsKit 安装位置并从副本继续')
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--entry', choices=('install.sh', 'install.ps1'), required=True)
    parser.add_argument('arguments', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    source = args.source.resolve()
    if not (source / args.entry).is_file():
        raise ValueError('安装包缺少入口：' + args.entry)
    arguments = args.arguments[1:] if args.arguments[:1] == ['--'] else args.arguments
    target, remaining = choose(source, arguments)
    if args.entry.endswith('.ps1'):
        shell = shutil.which('powershell.exe') or shutil.which('pwsh')
        if not shell:
            raise ValueError('未找到 PowerShell，无法从目标位置继续安装')
        command = [shell, '-NoLogo', '-NoProfile', '-ExecutionPolicy', 'RemoteSigned', '-File', str(target / args.entry), *remaining]
    else:
        command = ['bash', str(target / args.entry), *remaining]
    print('继续运行安装入口：' + str(target / args.entry), flush=True)
    environment = dict(os.environ, OPSKIT_LOCATION_READY=str(target))
    os.chdir(target)
    os.execvpe(command[0], command, environment)


if __name__ == '__main__':
    try:
        main()
    except (EOFError, KeyboardInterrupt):
        print('\n已取消安装；未启动服务。', file=sys.stderr)
        sys.exit(1)
    except (OSError, ValueError) as error:
        print('安装位置未就绪：' + str(error), file=sys.stderr)
        sys.exit(1)
