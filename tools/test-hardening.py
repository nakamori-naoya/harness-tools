#!/usr/bin/env python3
"""release.py の回帰検査と、plugin repository の CI workflow の action の固定を検査する。

  test-hardening.py [--repository <plugin repository の絶対path>] [unittest の引数…]

release.py は合成 fixture だけで検査する。--repository を渡したときだけ、その repository の
.github/workflows/validate.yml の action がすべて commit SHA で固定されているかも検査する。

基準資料: release.py の引数契約と、workflow の `uses:` の値
合格述語: release.py が両 marketplace と両 manifest の version だけを書き換えて記録の file を作らず、欠けた manifest では何も書かずに失敗し、
  不正な semver を拒む。workflow の `uses:` はすべて `<owner>/<repo>@<40 桁の SHA>` である
意味評価として残す範囲: どの action を使うべきか、release の中身が適切か
"""
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

TOOLS = Path(__file__).resolve().parent
REPOSITORY = None


class Hardening(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='hardening-')
        self.base = Path(self.temp.name).resolve()

    def tearDown(self):
        self.temp.cleanup()

    def call(self, *args):
        return subprocess.run(list(map(str, args)), text=True, capture_output=True, cwd=self.base, timeout=60)

    def test_ci_actions_are_pinned(self):
        if REPOSITORY is None:
            self.skipTest('--repository が無い')
        workflow = (REPOSITORY/'.github/workflows/validate.yml').read_text()
        for action in re.findall(r'uses:\s+([^\s#]+)', workflow):
            self.assertRegex(action, r'^[A-Za-z0-9_-]+/[A-Za-z0-9_-]+@[0-9a-f]{40}$')

    def test_release_updates_all_matching_declarations_only(self):
        repo = self.base/'release'
        for runtime in ['codex', 'claude']:
            path = repo/f'plugins/p/.{runtime}-plugin/plugin.json'
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({'name': 'fixture', 'version': '1.0.0', 'requires': [{'plugin': 'other', 'marketplace': 'other'}]}))
        for relative in ['.agents/plugins/marketplace.json', '.claude-plugin/marketplace.json']:
            source = {'source': 'local', 'path': './plugins/p'} if relative.startswith('.agents') else './plugins/p'
            (repo/relative).parent.mkdir(parents=True, exist_ok=True)
            (repo/relative).write_text(json.dumps({'plugins': [{'name': 'fixture', 'version': '1.0.0', 'source': source}]}))
        sidecar = repo/'plugins/p/internal/sidecar'
        for runtime in ['codex', 'claude']:
            path = sidecar/f'.{runtime}-plugin/plugin.json'
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({'name': 'fixture', 'version': '0.4.0', 'hooks': './hooks/x.json'}))
        sidecar_before = {str(path): path.read_bytes() for path in sidecar.rglob('plugin.json')}
        r = self.call('python3', TOOLS/'release.py', '--repo', repo, '--plugin', 'fixture', '--version', '2.0.0', '--apply')
        self.assertEqual(r.returncode, 0, r.stderr)
        for runtime in ['codex', 'claude']:
            data = json.loads((repo/f'plugins/p/.{runtime}-plugin/plugin.json').read_text())
            self.assertEqual(data['version'], '2.0.0')
            self.assertEqual(data['requires'], [{'plugin': 'other', 'marketplace': 'other'}])
        self.assertEqual(sidecar_before, {str(path): path.read_bytes() for path in sidecar.rglob('plugin.json')})
        self.assertFalse((repo/'releases').exists())
        (repo/'plugins/p/.claude-plugin/plugin.json').unlink()
        before = {str(p): p.read_bytes() for p in repo.rglob('*.json')}
        r = self.call('python3', TOOLS/'release.py', '--repo', repo, '--plugin', 'fixture', '--version', '3.0.0', '--apply')
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(before, {str(p): p.read_bytes() for p in repo.rglob('*.json')})
        for version in ['1.0.0-alpha..1', '1.0.0-.', '1.0.0-01']:
            r = self.call('python3', TOOLS/'release.py', '--repo', repo, '--plugin', 'fixture', '--version', version)
            self.assertEqual(r.returncode, 2)


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--repository')
    options, remaining = parser.parse_known_args()
    if options.repository is not None:
        REPOSITORY = Path(options.repository)
        if not REPOSITORY.is_absolute() or not REPOSITORY.is_dir():
            raise SystemExit('--repository は実在する絶対path: ' + options.repository)
    unittest.main(argv=[sys.argv[0]] + remaining)
