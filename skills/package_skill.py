#!/usr/bin/env python3
"""为可分发 Skill 生成当前 OpsKit 运行源码快照，不夹带本机配置。"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import zipfile

ROOT = Path(__file__).resolve().parents[1]
NAME = 'opskit'
EXCLUDED = {'.git', '.DS_Store', '__pycache__', '.env', 'collector.env', '.backups',
            '.uninstall-backups', '.document-backups', 'backups', '.workbench', 'offline-images', 'dist', 'node_modules'}


def copy_clean(source, destination, runtime_only=False):
    for path in source.rglob('*'):
        relative = path.relative_to(source)
        if set(relative.parts) & EXCLUDED or path.suffix in {'.pyc', '.pyo', '.tar', '.tgz'}:
            continue
        if runtime_only and relative.parts[0] == 'documents' and relative.as_posix() != 'documents/README.md':
            continue
        if path.is_symlink():
            raise ValueError(f'分发源码不能包含软链接：{path}')
        vendor_asset = relative.parts[:2] == ('portal', 'vendor') and (
            path.suffix in {'.txt', '.mjs', '.bcmap', '.pfb', '.ttf', '.wasm'} or
            path.name.upper().startswith(('LICENSE', 'COPYING', 'NOTICE')))
        if runtime_only and not vendor_asset and path.name != '.env.example' and path.suffix not in {
                '.py', '.sh', '.ps1', '.cmd', '.md', '.yaml', '.yml', '.html', '.css', '.js', '.conf'}:
            continue
        if path.name.startswith('.env') and path.name != '.env.example':
            continue
        if path.is_file() and path.name != 'outer-nginx.conf.example':
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if path.suffix == '.sh':
                with target.open('w', encoding='utf-8', newline='\n') as stream:
                    stream.write(path.read_text(encoding='utf-8-sig'))
                target.chmod(0o755)
            else:
                shutil.copy2(path, target)


def stage(destination):
    destination = destination.resolve()
    if destination.is_relative_to(ROOT / 'skills' / NAME):
        raise ValueError('暂存目录不能位于 Skill 源目录内')
    if destination.exists():
        raise ValueError(f'输出目录已存在，拒绝覆盖：{destination}')
    destination.mkdir(parents=True)
    copy_clean(ROOT / 'skills' / NAME, destination)
    runtime = destination / 'assets/opskit'
    runtime.mkdir(parents=True)
    for name in ('management', 'collector'):
        copy_clean(ROOT / name, runtime / name, runtime_only=True)
    for name in ('README.md', 'install.sh', 'install.cmd', 'install.ps1',
                 'uninstall.sh', 'uninstall.cmd', 'uninstall.ps1', '.gitignore'):
        if name.endswith('.sh'):
            with (runtime / name).open('w', encoding='utf-8', newline='\n') as stream:
                stream.write((ROOT / name).read_text(encoding='utf-8-sig'))
            (runtime / name).chmod(0o755)
        else:
            shutil.copy2(ROOT / name, runtime / name)
    documents = runtime / 'management/documents'
    for name in ('README.md', 'management/README.md', 'collector/README.md', 'management/ai/README.md',
                 'management/documents/README.md'):
        target = documents / 'builtin' / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(runtime / name, target)
    hashes = {p.relative_to(runtime).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in runtime.rglob('*') if p.is_file()}
    documentation = {key: value for key, value in hashes.items() if key.startswith('management/documents/')}
    commit = subprocess.run(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], text=True,
                            capture_output=True).stdout.strip() or None
    dirty = bool(subprocess.run(['git', '-C', str(ROOT), 'status', '--porcelain'], text=True,
                                capture_output=True).stdout.strip())
    (runtime / 'OPSKIT_SOURCE.json').write_text(json.dumps(
        {'base_commit': commit, 'working_tree_changes': dirty,
         'sha256': {key: value for key, value in hashes.items() if key not in documentation},
         'documentation_sha256': documentation},
        ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    for relative in ('SKILL.md', 'scripts/build_bundle.py', 'scripts/collect_prerequisites.py',
                     'assets/site/site.py', 'assets/site/install.sh', 'assets/site/offline-bootstrap.sh'):
        if not (destination / relative).is_file():
            raise ValueError(f'Skill 资源缺失：{relative}')
    return destination


def main():
    parser = argparse.ArgumentParser(description='打包带有当前 OpsKit 快照的自包含 Skill')
    parser.add_argument('--stage', type=Path, required=True, help='新目录，末级名称必须为 opskit')
    parser.add_argument('--archive', type=Path, help='可选 .skill 输出；也可再用 skill-creator 标准打包器处理暂存目录')
    args = parser.parse_args()
    if args.stage.name != NAME:
        parser.error('暂存目录末级名称必须为 opskit')
    result = stage(args.stage)
    if args.archive:
        if args.archive.exists():
            raise ValueError('拒绝覆盖已有 Skill 包')
        args.archive.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(args.archive, 'w', zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(result.rglob('*')):
                if path.is_file():
                    archive.write(path, path.relative_to(result.parent))
        with zipfile.ZipFile(args.archive) as archive:
            if archive.testzip():
                raise ValueError('Skill 压缩校验失败')
        print(args.archive.resolve())
    else:
        print(result)


if __name__ == '__main__':
    main()
