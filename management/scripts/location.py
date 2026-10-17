#!/usr/bin/env python3
"""选择安装位置，校验完整副本后继续；覆盖前保留并核验备份。"""
import argparse
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import platform
import shutil
import stat
import sys
import tempfile

from options import read, remember

IGNORED = {'.git', '.backups', '__pycache__', '.DS_Store', '.venv', 'node_modules'}


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
    if target.exists() and not target.is_dir():
        raise ValueError('目标位置是文件，请选择目录')
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


def private_file(name):
    name = name.casefold()
    return name == '.install-options.json' or (name.startswith('.env') and name != '.env.example') or name.endswith('.env')


def preserve_local(path):
    return (private_file(path.name) or path.name.casefold() in ('image-defaults.json', 'outer-nginx.conf.example') or
            any(part.casefold() in ('.workbench', 'documents', 'data', 'backups') for part in path.parts[:-1]))


def copy_file(source, target, record):
    descriptor, temporary = tempfile.mkstemp(prefix='.opskit-copy-', dir=target.parent)
    os.close(descriptor)
    temporary = Path(temporary)
    try:
        private = private_file(target.name)
        if private and sys.platform == 'win32':
            from common import protect_private
            protect_private(temporary)
        with temporary.open('wb') as outgoing, source.open('rb') as incoming:
            shutil.copyfileobj(incoming, outgoing, 1024 * 1024)
        shutil.copystat(source, temporary)
        if private and sys.platform != 'win32':
            temporary.chmod(0o600)
        checksum, size, mode = record
        if temporary.stat().st_size != size or digest(temporary) != checksum:
            raise ValueError('复制文件校验失败：' + str(target))
        if sys.platform != 'win32' and not private and stat.S_IMODE(temporary.stat().st_mode) != mode:
            raise ValueError('复制文件权限校验失败：' + str(target))
        if linked(target):
            raise ValueError('目标文件变为链接，停止写入：' + str(target))
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def backup_package(destination, files, directories):
    parent = destination / '.backups'
    if linked(parent):
        raise ValueError('备份目录不能是链接')
    backup = parent / ('install-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    content = backup / 'files'
    content.mkdir(parents=True, mode=0o700)
    for name in directories:
        (content / name).mkdir(mode=0o700)
    for name, record in files.items():
        copy_file(destination / name, content / name, record)
    copied, copied_directories = inventory(content)
    if (set(copied) != set(files) or copied_directories != directories or
            any(copied[name][:2] != record[:2] for name, record in files.items())):
        raise ValueError('原目录备份校验失败，尚未覆盖文件')
    (backup / 'SHA256SUMS').write_text(''.join('{}  files/{}\n'.format(record[0], name.as_posix())
                                            for name, record in files.items()), encoding='utf-8')
    if inventory(destination) != (files, directories):
        raise ValueError('备份期间目标目录发生变化，尚未覆盖文件；请停止其他写入后重试')
    print('覆盖前备份已核验：' + str(backup), flush=True)
    return backup


def copy_package(source, destination, overwrite=False):
    destination = destination_path(source, destination)
    if destination == source:
        return destination
    files, directories = inventory(source)
    previous, previous_directories = inventory(destination) if destination.exists() else ({}, [])
    if destination.exists() and any(destination.iterdir()) and not overwrite:
        raise ValueError('目标目录已有内容；可确认覆盖或使用 --overwrite，原配置会保留')
    for name in directories:
        target = destination / name
        if target.exists() and not target.is_dir():
            raise ValueError('文件与目录类型冲突，未覆盖：' + str(name))
    for name in files:
        target = destination / name
        if target.exists() and not target.is_file():
            raise ValueError('文件与目录类型冲突，未覆盖：' + str(name))
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    backup = backup_package(destination, previous, previous_directories) if previous or previous_directories else None
    expected = dict(previous)
    written, created_directories = [], []
    preserved = 0
    print('正在复制安装包并校验 {} 个文件：{}'.format(len(files), destination), flush=True)
    try:
        for name in directories:
            path = destination / name
            if not path.exists():
                path.mkdir(mode=0o700)
                created_directories.append(name)
        for name, record in files.items():
            if name in previous and preserve_local(name):
                preserved += 1
                continue
            if (destination / name).exists() and name not in previous:
                raise ValueError('目标目录出现新文件，停止覆盖：' + str(name))
            copy_file(source / name, destination / name, record)
            written.append(name)
            expected[name] = record
        after, after_directories = inventory(source)
        if after != files or after_directories != directories:
            raise ValueError('复制期间源安装包发生变化，请停止其他写入后重新选择新目录')
        actual, actual_directories = inventory(destination)
        if (set(actual) != set(expected) or set(actual_directories) != set(previous_directories) | set(directories) or
                any(actual[name][:2] != record[:2] for name, record in expected.items())):
            raise ValueError('目标目录文件发生变化，停止启动')
    except BaseException:
        if backup:
            try:
                for name in reversed(written):
                    if name in previous:
                        copy_file(backup / 'files' / name, destination / name, previous[name])
                    else:
                        (destination / name).unlink()
                for name in reversed(created_directories):
                    (destination / name).rmdir()
                print('覆盖未完成，已恢复本次修改的原文件；备份保留：' + str(backup), file=sys.stderr)
            except (OSError, ValueError) as error:
                print('自动恢复未完成，请从备份恢复：{}；{}'.format(backup, error), file=sys.stderr)
        print('复制未完成，未启动目标安装器；原安装包和目标文件均已保留：' + str(destination), file=sys.stderr)
        raise
    if preserved:
        print('保留现有配置、数据及文档：{} 个同名文件。'.format(preserved), flush=True)
    print('复制与校验完成，原安装包保留：' + str(source), flush=True)
    return destination


def location_arguments(arguments):
    target, keep, overwrite, remaining = None, False, False, []
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
        elif value == '--overwrite':
            overwrite = True
        else:
            remaining.append(value)
        index += 1
    if keep and target is not None:
        raise ValueError('--install-dir 与 --keep-location 不能同时使用')
    return target, keep, overwrite, remaining


def install_at(source, value, overwrite):
    destination = destination_path(source, value)
    occupied = destination != source and destination.exists() and any(destination.iterdir())
    if occupied and not overwrite:
        if not sys.stdin.isatty():
            raise ValueError('目标目录已有内容，非交互覆盖需显式传入 --overwrite；原配置会保留')
        print('目标目录已有文件，将先备份校验，再覆盖程序文件；现有配置、数据和项目文档保留。')
        answer = input('是否覆盖 {}？[y/N]：'.format(destination)).strip().casefold()
        if answer not in ('y', 'yes', '是'):
            raise ValueError('已取消覆盖，可选择其他目录或就在当前目录安装')
        overwrite = True
    return copy_package(source, destination, overwrite=overwrite)


def recommended_location(source, choices):
    candidates = [path.resolve() for path, _ in choices]
    parent = source.parent
    if source.name in ('management', 'collector') and (parent / 'management').is_dir() and (parent / 'install.sh').is_file():
        candidates += [path.resolve() / source.name for path, _ in recommendations(parent)]
    return any(source == path or (path.exists() and source.samefile(path)) for path in candidates)


def choose(source, arguments):
    target, keep, overwrite, remaining = location_arguments(arguments)
    print('当前安装包位置：' + str(source), flush=True)
    print('当前终端目录：' + str(Path.cwd()), flush=True)
    if target is not None:
        return install_at(source, target, overwrite), remaining
    choices = recommendations(source)
    if recommended_location(source, choices):
        print('当前已在推荐目录，直接在此继续安装。', flush=True)
        return source, remaining
    only_check = any(arg.lower() in ('--help', '-h', '--configure-only', '-configureonly', '-?') for arg in remaining)
    if keep or only_check or not sys.stdin.isatty():
        print('保留当前位置；可用 --install-dir 指定安装目录。', flush=True)
        return source, remaining
    default = 3 if platform.system() == 'Linux' and getattr(os, 'geteuid', lambda: 1)() != 0 else 1
    previous = read(source).get('directory')
    if not previous and source.name in ('management', 'collector') and (source.parent / 'install.sh').is_file():
        parent_choice = read(source.parent).get('directory')
        previous = str(Path(parent_choice) / source.name) if parent_choice else None
    if previous:
        if source.resolve() == Path(previous).resolve():
            print('沿用上次选择的当前位置。', flush=True)
            return source, remaining
        default = previous
    print('\n请选择安装位置（已有文件可确认覆盖，覆盖前自动备份）：')
    for number, (path, description) in enumerate(choices, 1):
        print('  {}) {}（{}）'.format(number, path, description))
    print('  4) 就在当前目录安装：{}\n  5) 输入自定义目录\n  0) 取消安装\n也可直接输入安装目录的绝对路径。'.format(source))
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
        elif Path(answer).expanduser().is_absolute():
            value = answer
        else:
            print('请选择 0—5。')
            continue
        try:
            return install_at(source, value, overwrite), remaining
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
    help_only = any(arg.lower() in ('--help', '-h', '-?') for arg in remaining)
    config_only = any(arg.lower() in ('--configure-only', '-configureonly') for arg in remaining)
    explicit_location = any(arg.lower() in ('--install-dir', '--keep-location') for arg in arguments)
    # 只生成配置的默认原地行为，不覆盖上次明确选择的安装目录。
    if not help_only and (not config_only or explicit_location):
        remember(target, directory=str(target))
        if source != target:
            try:
                remember(source, directory=str(target))
            except OSError as exc:
                print('原解压目录不可写，已在目标位置记录安装选择：' + str(exc), file=sys.stderr)
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
    except (OSError, ValueError, RuntimeError) as error:
        print('安装位置未就绪：' + str(error), file=sys.stderr)
        sys.exit(1)
