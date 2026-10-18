#!/usr/bin/env python3
"""Build on the packaging machine; the customer installs the generated static folders."""
import argparse
import gzip
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
from verify import ROOT, VERSIONS, sha256, verify_sources

SOURCE_FILES = ['versions.json', 'verify.py', 'build_frontends.py', 'README.md', 'npm-prefix.patch',
                'spug-prefix.patch', 'spug-package-lock.json', 'npm-LICENSE.txt', 'spug-LICENSE.txt',
                'upstream/npm-v2.16.0.tar.gz', 'upstream/spug-049984e.tar.gz']


def finalize(output):
    output.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ROOT / "versions.json", output / "versions.json")
    source_package = output / 'spug' / 'opskit-corresponding-source.tar.gz'
    with source_package.open('wb') as raw, gzip.GzipFile(filename='', fileobj=raw, mode='wb', mtime=0) as zipped:
        with tarfile.open(fileobj=zipped, mode='w') as archive:
            for relative in sorted(SOURCE_FILES):
                path = ROOT / relative
                info = archive.gettarinfo(str(path), arcname=relative)
                info.mtime = info.uid = info.gid = 0
                info.uname = info.gname = ''
                with path.open('rb') as stream:
                    archive.addfile(info, stream)
    hashes = {name: {str(p.relative_to(output / name)): sha256(p) for p in sorted((output / name).rglob('*')) if p.is_file()}
              for name in VERSIONS}
    (output / 'static-hashes.json').write_text(json.dumps(hashes, indent=2) + '\n')
    print(json.dumps({name: {'files': len(hashes[name]), 'bytes': sum(p.stat().st_size for p in (output / name).rglob('*') if p.is_file())}
                      for name in VERSIONS}, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    output = parser.parse_args().output.resolve()
    verify_sources()
    node = subprocess.check_output(['node', '--version'], text=True).strip()
    if node != 'v24.21.0':
        raise RuntimeError(f'Build requires the verified Node v24.21.0; found {node}. Customer deployment needs no Node.')
    work = Path(tempfile.mkdtemp(prefix='opskit-prefix-build-'))
    try:
        env = dict(os.environ, npm_config_cache=str(work / 'npm-cache'), npm_config_update_notifier='false')
        for name, item in VERSIONS.items():
            src = work / name
            src.mkdir()
            subprocess.run(['tar', '-xzf', str(ROOT / item['archive']), '-C', str(src), '--strip-components=1'], check=True)
            with (ROOT / item['patch']).open('rb') as patch:
                subprocess.run(['patch', '--batch', '--fuzz=0', '-p1'], cwd=src, stdin=patch, check=True)
            frontend = src / ('frontend' if name == 'npm' else 'spug_web')
            with (ROOT / f'{name}-build.log').open('w') as log:
                if name == 'npm':
                    commands = [
                        ['npm', 'exec', '--yes', '--package', 'yarn@1.22.22', '--', 'yarn', 'install', '--frozen-lockfile', '--non-interactive', '--ignore-scripts', '--cache-folder', str(work / 'yarn-cache')],
                        ['npm', 'run', 'locale-compile'], ['npm', 'run', 'build']]
                else:
                    shutil.copyfile(ROOT / item['lock'], frontend / 'package-lock.json')
                    env.update(PUBLIC_URL='/spug', CI='false')
                    commands = [['npm', 'ci', '--no-audit', '--no-fund', '--legacy-peer-deps'], ['npm', 'run', 'build']]
                for cmd in commands:
                    subprocess.run(cmd, cwd=frontend, env=env, stdout=log, stderr=subprocess.STDOUT, check=True)
            destination = output / name
            if destination.exists():
                shutil.rmtree(destination)
            shutil.copytree(frontend / ('dist' if name == 'npm' else 'build'), destination)
            shutil.copyfile(ROOT / f'{name}-LICENSE.txt', destination / 'LICENSE.txt')
        finalize(output)
        subprocess.run(['python3', str(ROOT / 'verify.py'), '--output', str(output)], check=True)
    finally:
        shutil.rmtree(work)


if __name__ == '__main__':
    main()
