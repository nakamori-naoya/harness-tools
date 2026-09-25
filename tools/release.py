#!/usr/bin/env python3
"""両 marketplace と両 runtime manifest の version を同時に上げる。

  release.py --repo <plugin repository の絶対path> --plugin <名> --version <semver> [--apply]

--apply 無しは書き換える file の計画を JSON で返すだけ。--apply で書き換える。
変更の要点は commit と PR が持ち、この tool は記録の file を作らない。
"""
import argparse
import json
import os
import tempfile
from pathlib import Path
import re
import sys


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--repo', required=True, help='released plugin repository (absolute path)')
    p.add_argument('--plugin', required=True)
    p.add_argument('--version', required=True)
    p.add_argument('--apply', action='store_true')
    a = p.parse_args()
    if not re.fullmatch(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?', a.version):
        p.error('version must be semantic version')
    if not re.fullmatch(r'[A-Za-z0-9._-]+', a.plugin) or '..' in a.plugin:
        p.error('invalid plugin identity')
    if '-' in a.version.split('+', 1)[0] and any(part.isdigit() and len(part) > 1 and part.startswith('0') for part in a.version.split('+', 1)[0].split('-', 1)[1].split('.')):
        p.error('numeric prerelease identifiers cannot have leading zeros')
    root = Path(a.repo).resolve()
    publications = []
    package = None
    identity = None
    for runtime, relative in [('codex', '.agents/plugins/marketplace.json'), ('claude', '.claude-plugin/marketplace.json')]:
        catalog_path = root / relative
        catalog = json.loads(catalog_path.read_text())
        matches = [entry for entry in catalog['plugins'] if entry.get('name') == a.plugin]
        if len(matches) != 1:
            p.error('public plugin must have exactly one entry in each runtime marketplace')
        entry = matches[0]
        source = entry.get('source')
        if runtime == 'codex':
            if not isinstance(source, dict) or source.get('source') != 'local':
                p.error('public Codex source must be local')
            source = source.get('path')
        if not isinstance(source, str) or not source.startswith('./plugins') or Path(source).is_absolute() or '..' in Path(source).parts or '\\' in source:
            p.error('public package source must be a repository-local plugins path')
        candidate = root / source
        if not candidate.resolve().is_relative_to(root / 'plugins') or not candidate.is_dir():
            p.error('public package source is missing or escapes plugins')
        current = root
        for part in Path(source).parts:
            current /= part
            if current.is_symlink():
                p.error('public package source contains symlink')
        manifest_path = candidate / f'.{runtime}-plugin/plugin.json'
        if not manifest_path.is_file() or manifest_path.is_symlink() or manifest_path.parent.is_symlink():
            p.error('public runtime manifest missing or symlink')
        manifest = json.loads(manifest_path.read_text())
        current_identity = (manifest.get('name'), manifest.get('version'))
        if current_identity != (a.plugin, entry.get('version')):
            p.error('catalog and public runtime manifest identity/version mismatch')
        if package is not None and (candidate.resolve() != package or current_identity != identity):
            p.error('runtime catalogs must identify the same public package and version')
        package = candidate.resolve()
        identity = current_identity
        publications.append((catalog_path, catalog, entry, manifest_path, manifest))
    changed = {}
    for catalog_path, catalog, entry, manifest_path, manifest in publications:
        entry['version'] = a.version
        manifest['version'] = a.version
        changed[str(catalog_path.relative_to(root))] = catalog
        changed[str(manifest_path.relative_to(root))] = manifest
    plan = {'plugin': a.plugin, 'version': a.version, 'files': list(changed)}
    if a.apply:
        content = {root / relative: json.dumps(data, ensure_ascii=False, indent=2) + '\n' for relative, data in changed.items()}
        for path in content:
            if path.is_symlink() or any(parent.is_symlink() for parent in path.parents if parent != root):
                p.error('release target symlink')
        originals = {path: path.read_bytes() if path.exists() else None for path in content}
        staged = {}
        try:
            for path, text in content.items():
                fd, name = tempfile.mkstemp(prefix='.release-', dir=path.parent)
                with os.fdopen(fd, 'w') as output:
                    output.write(text)
                staged[path] = Path(name)
            for path, temporary in staged.items():
                os.replace(temporary, path)
        except BaseException:
            for path, original in originals.items():
                if original is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_bytes(original)
            raise
        finally:
            for temporary in staged.values():
                temporary.unlink(missing_ok=True)
    print(json.dumps({'applied': a.apply, 'release': plan}, ensure_ascii=False, indent=2))

if __name__ == '__main__':
    main()
