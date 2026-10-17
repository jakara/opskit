#!/usr/bin/env python3
"""Export the OpsKit snapshot without application images or offline dependencies."""
import argparse
from pathlib import Path
import shutil
import sys
import tarfile
import tempfile

from build_bundle import (BundleError, checksums, digest, opskit_source, read_json,
                          safe_path, verify_checksums)


def export_opskit(output):
    source = opskit_source()
    provenance = read_json(safe_path(source, 'OPSKIT_SOURCE.json'))
    expected = dict(provenance['sha256'])
    expected.update(provenance['documentation_sha256'])
    required = ('README.md', 'install.sh', 'install.cmd', 'install.ps1',
                'management/compose.yaml', 'collector/compose.yaml')
    if not all(name in expected for name in required):
        raise BundleError('OpsKit 快照缺少安装入口或管理端、采集端')
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    archive = output / 'opskit-direct.tar.gz'
    checksum = output / (archive.name + '.sha256')
    if any(path.exists() or path.is_symlink() for path in (archive, checksum)):
        raise BundleError('直出包或校验文件已存在，拒绝覆盖：%s' % output)
    with tempfile.TemporaryDirectory(prefix='.opskit-direct-', dir=str(output)) as directory:
        package = Path(directory) / 'opskit'
        for name, expected_hash in expected.items():
            original = safe_path(source, name)
            if digest(original) != expected_hash:
                raise BundleError('OpsKit 快照校验失败：%s' % name)
            target = package / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(original, target)
            if digest(target) != expected_hash:
                raise BundleError('OpsKit 复制校验失败：%s' % name)
        shutil.copy2(safe_path(source, 'OPSKIT_SOURCE.json'), package / 'OPSKIT_SOURCE.json')
        checksums(package)
        verify_checksums(package)

        def linux_metadata(member):
            member.uid = member.gid = 0
            member.uname = member.gname = ''
            member.mode = 0o755 if member.isdir() or member.name.endswith('.sh') else 0o644
            return member

        staged_archive = Path(directory) / archive.name
        with tarfile.open(staged_archive, 'w:gz') as stream:
            stream.add(package, arcname='opskit', filter=linux_metadata)
        staged_checksum = Path(directory) / checksum.name
        staged_checksum.write_text('%s  %s\n' % (digest(staged_archive), archive.name), encoding='utf-8')
        created = []
        try:
            for staged, final in ((staged_archive, archive), (staged_checksum, checksum)):
                with staged.open('rb') as incoming, final.open('xb') as outgoing:
                    created.append(final)
                    shutil.copyfileobj(incoming, outgoing)
        except BaseException:
            for path in created:
                path.unlink()
            raise
    return archive


def main():
    parser = argparse.ArgumentParser(description='直出 OpsKit 独立安装包，不含业务、镜像或离线依赖')
    parser.add_argument('--output', required=True, help='输出目录，已有归档或校验文件不会被覆盖')
    args = parser.parse_args()
    try:
        print('OpsKit 直出包：%s' % export_opskit(args.output))
        return 0
    except (BundleError, OSError, ValueError, TypeError, KeyError) as exc:
        print('直出未完成：%s' % exc, file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
