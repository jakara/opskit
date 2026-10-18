#!/usr/bin/env python3
"""Verify the exact supported upstream sources, patches and shipped static bytes."""
import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VERSIONS = json.loads((ROOT / 'versions.json').read_text())


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_sources():
    for name, item in VERSIONS.items():
        for path_key, hash_key in [('archive', 'source_sha256'), ('patch', 'patch_sha256'), ('lock', 'lock_sha256')]:
            if path_key in item:
                path = ROOT / item[path_key]
                if not path.is_file() or sha256(path) != item[hash_key]:
                    raise RuntimeError(f'{name}: unsupported or modified {path_key}: {path}')


def verify_static(output):
    manifest = json.loads((output / 'static-hashes.json').read_text())
    for name, hashes in manifest.items():
        folder = output / name
        actual = {str(p.relative_to(folder)) for p in folder.rglob('*') if p.is_file()}
        if actual != set(hashes):
            raise RuntimeError(f'{name}: static file inventory differs')
        for relative, expected in hashes.items():
            if sha256(folder / relative) != expected:
                raise RuntimeError(f'{name}: static checksum differs: {relative}')
            if relative.endswith('.map') or 'node_modules' in Path(relative).parts:
                raise RuntimeError(f'{name}: build-only material in static output: {relative}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sources-only', action='store_true')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    verify_sources()
    if not args.sources_only:
        if args.output is None:
            parser.error("--output is required unless --sources-only")
        verify_static(args.output)
    print('Verified exact source archives, patches' + (', static outputs' if not args.sources_only else ''))


if __name__ == '__main__':
    main()
