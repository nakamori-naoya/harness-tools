#!/usr/bin/env python3
"""明示された plugin repository の配置と manifest を検査する。

workspace の plugin-package-contract.md が定める一つの形に、repository が合っているかだけを見る。
合否は manifest とディレクトリの対応から一意に決まる。

基準資料: 両 marketplace（.claude-plugin/marketplace.json、.agents/plugins/marketplace.json）と、
  package 直下の両 runtime manifest（.claude-plugin/plugin.json、.codex-plugin/plugin.json）
入力: 絶対パスで渡された 1 repository。JSON は json で、SKILL.md の frontmatter は mikefarah/yq v4 で読む
合格述語:
  1. 両 marketplace の name が同じで、package の名前・version・source（./plugins/<package>）が一致する
  2. 両 manifest の名前・version・skills・metadata.harness が一致し、名前と version が marketplace と一致し、
     metadata.harness.marketplace が marketplace の name と一致する。plugins/ 直下に manifest が無い
  3. skills は ./skills/<entry> の list で、skills/ 直下のディレクトリと過不足なく一致し、
     各 SKILL.md の frontmatter name が <entry>
  4. internalPlugins は {<name>: ./internal/<name>} で、internal/ 直下のディレクトリと一致し、<name> は公開入口と重ならない。
     各 <name> は SKILL.md（name = <name>）を持つ内部 skill か、hooks を宣言した両 runtime manifest を持ち
     SKILL.md を持たない hook の部品のどちらか
  5. SKILL.md は skills/<entry>/ と internal/<name>/ にだけあり、.claude-plugin / .codex-plugin は
     package 直下と hook の部品の直下にだけある。package の中に symlink が無い
失敗時の診断: 違反した宣言かパスと、期待した値
正例: self-test の fixture（公開入口 2 つ、内部 skill 1 つ、共有コードのディレクトリ）
反例: self-test の負例（skills が文字列、name の不一致、宣言の無いディレクトリ、置き場の違う SKILL.md など）
意味評価として残す範囲: 責務の分け方、内部 skill が本当に二つの入口で共有される判断か、文章が示す依存

使い方: validate-plugin-repository.py <repository の絶対パス>
       validate-plugin-repository.py --self-test
違反があれば理由を出力して終了コード 1。
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable

RUNTIMES = ("codex", "claude")


class ContractError(ValueError):
    pass


def fail(message: str) -> None:
    raise ContractError(message)


def load_object(path: Path, label: str) -> dict:
    if path.is_symlink() or not path.is_file():
        fail(f"{label} が regular file ではない: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        fail(f"{label} が有効な JSON ではない: {path}: {exc}")
    if not isinstance(value, dict):
        fail(f"{label} は JSON object でなければならない: {path}")
    return value


def relative(value: object, label: str) -> Path:
    if not isinstance(value, str) or not value.startswith("./"):
        fail(f"{label} は ./ から始まるパスでなければならない: {value!r}")
    path = Path(value[2:])
    if not path.parts or ".." in path.parts:
        fail(f"{label} が package の外を指す: {value!r}")
    return path


def catalog(repository: Path, runtime: str) -> tuple[str, list[tuple[str, str, str]]]:
    path = repository / (".agents/plugins/marketplace.json" if runtime == "codex" else ".claude-plugin/marketplace.json")
    data = load_object(path, f"{runtime} marketplace")
    name, plugins = data.get("name"), data.get("plugins")
    if not isinstance(name, str) or not name:
        fail(f"{runtime} marketplace の name が空: {path}")
    if not isinstance(plugins, list) or not plugins:
        fail(f"{runtime} marketplace に package が無い: {path}")
    entries = []
    for index, item in enumerate(plugins):
        source = item.get("source") if isinstance(item, dict) else None
        if runtime == "codex":
            if not isinstance(source, dict) or source.get("source") != "local":
                fail(f"Codex marketplace plugins[{index}] の source が local の object ではない")
            source = source.get("path")
        parts = relative(source, f"{runtime} marketplace source").parts
        if len(parts) != 2 or parts[0] != "plugins":
            fail(f"{runtime} marketplace の source は ./plugins/<package> でなければならない: {source}")
        identity = (item.get("name"), item.get("version"), source)
        if not all(isinstance(value, str) and value for value in identity):
            fail(f"{runtime} marketplace plugins[{index}] の名前か version が空")
        entries.append(identity)
    return name, entries


def skill_name(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        fail(f"SKILL.md が regular file ではない: {path}")
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0] != "---" or "---" not in lines[1:]:
        fail(f"SKILL.md に閉じた frontmatter が無い: {path}")
    yq = shutil.which("yq")
    if yq is None:
        fail("SKILL.md の frontmatter を読むには mikefarah/yq v4 が要る")
    text = "\n".join(lines[1:lines.index("---", 1)]) + "\n"
    result = subprocess.run([yq, "-o=json", "."], input=text, text=True, capture_output=True, check=False)
    try:
        document = json.loads(result.stdout) if result.returncode == 0 else None
    except json.JSONDecodeError:
        document = None
    name = document.get("name") if isinstance(document, dict) else None
    if not isinstance(name, str) or not name.strip():
        fail(f"SKILL.md の frontmatter に name が無い: {path}")
    return name


def subdirectories(path: Path) -> set[str]:
    return {child.name for child in path.iterdir() if child.is_dir()} if path.is_dir() else set()


def validate_internal(package: Path, internal_name: str) -> bool:
    """内部 skill なら False、hook の部品なら True を返す。"""
    root = package / "internal" / internal_name
    if root.is_symlink() or not root.is_dir():
        fail(f"internalPlugins のディレクトリが無い: {root}")
    manifests = [root / f".{rt}-plugin/plugin.json" for rt in RUNTIMES]
    if not any(path.exists() for path in manifests):
        declared = skill_name(root / "SKILL.md")
        if declared != internal_name:
            fail(f"内部 skill の SKILL.md の name がディレクトリ名と違う: {root / 'SKILL.md'}: {declared!r}")
        return False
    nested = [load_object(path, f"{internal_name} {rt} manifest") for path, rt in zip(manifests, RUNTIMES)]
    if not all(isinstance(item.get("hooks"), str) and item["hooks"] for item in nested):
        fail(f"hooks を宣言しない内部 skill に runtime manifest を置かない: {root}")
    if {(item.get("name"), item.get("version")) for item in nested} != {(internal_name, nested[0].get("version"))}:
        fail(f"hook の部品の両 manifest の名前と version が一致しない: {root}")
    for rt, item in zip(RUNTIMES, nested):
        hook = root / relative(item["hooks"], f"{internal_name} {rt} hooks")
        if hook.is_symlink() or not hook.is_file():
            fail(f"hook の部品が宣言した hooks のファイルが無い: {hook}")
    if (root / "SKILL.md").exists():
        fail(f"hook の部品は SKILL.md を持たない: {root / 'SKILL.md'}")
    return True


def validate_package(repository: Path, identity: tuple[str, str, str], marketplace: str) -> None:
    name, version, source = identity
    package = repository / relative(source, f"{name} source")
    if package.is_symlink() or not package.is_dir():
        fail(f"package のディレクトリが無い: {package}")
    for path in package.rglob("*"):
        if path.is_symlink():
            fail(f"package の中に symlink がある: {path}")
    for rt in RUNTIMES:
        if (repository / "plugins" / f".{rt}-plugin").exists():
            fail(f"plugins/ 直下に manifest を置かない: {repository / 'plugins' / f'.{rt}-plugin'}")

    manifests = [load_object(package / f".{rt}-plugin/plugin.json", f"{name} {rt} manifest") for rt in RUNTIMES]
    compared = [{key: m.get(key) for key in ("name", "version", "skills")} | {"harness": (m.get("metadata") or {}).get("harness")}
                for m in manifests]
    if compared[0] != compared[1]:
        fail(f"{name} の Codex と Claude の manifest（名前・version・skills・metadata.harness）が一致しない")
    manifest, harness = manifests[0], compared[0]["harness"]
    if (manifest.get("name"), manifest.get("version")) != (name, version):
        fail(f"{name} の manifest の名前か version が marketplace と違う")
    if not isinstance(harness, dict) or harness.get("marketplace") != marketplace:
        fail(f"{name} の metadata.harness.marketplace が marketplace の name {marketplace!r} と違う")

    skills = manifest.get("skills")
    if not isinstance(skills, list) or not skills or len(skills) != len(set(map(str, skills))):
        fail(f"{name} の skills は ./skills/<entry> の重複の無い list でなければならない（\"./skills/\" は不可）")
    public = set()
    for value in skills:
        parts = relative(value, f"{name} skills").parts
        if len(parts) != 2 or parts[0] != "skills":
            fail(f"公開入口は ./skills/<entry> でなければならない: {value}")
        declared = skill_name(package / "skills" / parts[1] / "SKILL.md")
        if declared != parts[1]:
            fail(f"公開入口の SKILL.md の name がディレクトリ名と違う: {package / value[2:] / 'SKILL.md'}: {declared!r}")
        public.add(parts[1])
    if subdirectories(package / "skills") != public:
        fail(f"{name} の skills/ 直下が manifest の skills と一致しない: {sorted(subdirectories(package / 'skills') ^ public)}")

    internal = harness.get("internalPlugins", {})
    if not isinstance(internal, dict) or not all(isinstance(v, str) for v in internal.values()):
        fail(f"{name} の internalPlugins は {{<name>: ./internal/<name>}} でなければならない")
    sidecars = set()
    for internal_name, value in internal.items():
        if value != f"./internal/{internal_name}":
            fail(f"internalPlugins.{internal_name} は ./internal/{internal_name} でなければならない: {value}")
        if internal_name in public:
            fail(f"内部 skill の名前が公開入口と重なる: {internal_name}")
        if validate_internal(package, internal_name):
            sidecars.add(package / "internal" / internal_name)
    if subdirectories(package / "internal") != set(internal):
        fail(f"{name} の internal/ 直下が internalPlugins と一致しない: {sorted(subdirectories(package / 'internal') ^ set(internal))}")

    allowed = {package / "skills" / e / "SKILL.md" for e in public} | {package / "internal" / i / "SKILL.md" for i in internal}
    for path in package.rglob("SKILL.md"):
        if path not in allowed:
            fail(f"SKILL.md は skills/<entry>/ か internal/<name>/ にだけ置く: {path}")
    for path in list(package.rglob(".codex-plugin")) + list(package.rglob(".claude-plugin")):
        if path.parent != package and path.parent not in sidecars:
            fail(f".claude-plugin / .codex-plugin は package 直下と hook の部品の直下にだけ置く: {path}")


def validate_repository(repository: Path) -> None:
    if not repository.is_absolute() or not repository.is_dir():
        fail(f"repository は実在する絶対パスで渡す: {repository}")
    codex_name, codex = catalog(repository, "codex")
    claude_name, claude = catalog(repository, "claude")
    if codex_name != claude_name or codex != claude:
        fail("Codex と Claude の marketplace の name か package が一致しない")
    for identity in codex:
        validate_package(repository, identity, codex_name)
    print(f"Root contract: passed ({repository}, packages={len(codex)})")


# ---- self-test ------------------------------------------------------------------------------

P = "plugins/pkg"


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def skill(root: Path, name: str) -> None:
    write(root / "SKILL.md", f"---\nname: {name}\ndescription: fixture\n---\n\n# {name}\n")


def edit_manifest(root: Path, change: Callable[[dict], None]) -> None:
    for rt in RUNTIMES:
        path = root / P / f".{rt}-plugin" / "plugin.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        change(data)
        path.write_text(json.dumps(data), encoding="utf-8")


def write_fixture(root: Path) -> None:
    write(root / ".claude-plugin/marketplace.json", json.dumps(
        {"name": "pkg", "plugins": [{"name": "pkg", "version": "1.0.0", "source": "./plugins/pkg"}]}))
    write(root / ".agents/plugins/marketplace.json", json.dumps(
        {"name": "pkg", "plugins": [{"name": "pkg", "version": "1.0.0", "source": {"source": "local", "path": "./plugins/pkg"}}]}))
    skill(root / P / "skills/alpha", "alpha")
    write(root / P / "skills/alpha/scripts/check.py", "print('ok')\n")
    skill(root / P / "skills/beta", "beta")
    skill(root / P / "internal/shared-judgment", "shared-judgment")
    write(root / P / "lib/store.py", "STORE = {}\n")
    manifest = {"name": "pkg", "version": "1.0.0", "skills": ["./skills/alpha", "./skills/beta"],
                "metadata": {"harness": {"marketplace": "pkg", "internalPlugins": {"shared-judgment": "./internal/shared-judgment"}}}}
    for rt in RUNTIMES:
        extra = {"interface": {"capabilities": ["Skills"]}} if rt == "codex" else {}
        write(root / P / f".{rt}-plugin/plugin.json", json.dumps(manifest | extra))


def make_sidecar(root: Path, *, create_hooks: bool) -> None:
    edit_manifest(root, lambda m: m["metadata"]["harness"]["internalPlugins"].__setitem__("session-hooks", "./internal/session-hooks"))
    for rt in RUNTIMES:
        write(root / P / f"internal/session-hooks/.{rt}-plugin/plugin.json",
              json.dumps({"name": "session-hooks", "version": "1.0.0", "hooks": f"./hooks/{rt}.json"}))
        if create_hooks:
            write(root / P / f"internal/session-hooks/hooks/{rt}.json", '{"hooks":{}}\n')


def expect(label: str, expected: str | None, mutate: Callable[[Path], None]) -> None:
    with tempfile.TemporaryDirectory(prefix="root-contract-") as temporary:
        root = Path(temporary).resolve() / "repo"
        write_fixture(root)
        mutate(root)
        try:
            validate_repository(root)
        except ContractError as exc:
            if expected is None or expected not in str(exc):
                fail(f"「{label}」が期待と違う理由で失敗した: {exc}")
            print(f"Root negative: passed ({label})")
            return
        if expected is not None:
            fail(f"負例「{label}」を拒めない")
        print(f"Root positive: passed ({label})")


def self_test() -> None:
    expect("正例", None, lambda r: None)
    expect("SKILL.md の無い共有コードのディレクトリ", None, lambda r: write(r / P / "core/scripts/tool", "#!/bin/sh\n"))
    expect("引用符とコメント付きの frontmatter name", None,
           lambda r: write(r / P / "skills/beta/SKILL.md", '---\nname: "beta" # comment\ndescription: x\n---\nname: decoy\n'))
    expect("hook の部品", None, lambda r: make_sidecar(r, create_hooks=True))
    expect("skills が文字列", "skills は ./skills/<entry>", lambda r: edit_manifest(r, lambda m: m.__setitem__("skills", "./skills/")))
    expect("SKILL.md の name が違う", "name がディレクトリ名と違う", lambda r: skill(r / P / "skills/beta", "other"))
    expect("宣言の無い公開入口", "skills/ 直下が manifest", lambda r: skill(r / P / "skills/gamma", "gamma"))
    expect("宣言の無い内部 skill", "internal/ 直下が internalPlugins", lambda r: skill(r / P / "internal/extra", "extra"))
    expect("内部 skill と公開入口の名前が重なる", "公開入口と重なる", lambda r: (
        skill(r / P / "internal/alpha", "alpha"),
        edit_manifest(r, lambda m: m["metadata"]["harness"]["internalPlugins"].__setitem__("alpha", "./internal/alpha"))))
    expect("manifest の version が marketplace と違う", "marketplace と違う", lambda r: edit_manifest(r, lambda m: m.__setitem__("version", "2.0.0")))
    expect("両 manifest の skills が違う", "manifest（名前・version・skills", lambda r: (
        write(r / P / ".claude-plugin/plugin.json", json.dumps(json.loads((r / P / ".claude-plugin/plugin.json").read_text()) | {"skills": ["./skills/alpha"]}))))
    expect("harness.marketplace の不一致", "metadata.harness.marketplace", lambda r: edit_manifest(r, lambda m: m["metadata"]["harness"].__setitem__("marketplace", "other")))
    expect("marketplace の source が 3 階層", "./plugins/<package>", lambda r: write(r / ".claude-plugin/marketplace.json", json.dumps(
        {"name": "pkg", "plugins": [{"name": "pkg", "version": "1.0.0", "source": "./plugins/pkg/x"}]})))
    expect("置き場の違う SKILL.md", "SKILL.md は skills/<entry>/", lambda r: skill(r / P / "skills/alpha/nested", "nested"))
    expect("入口の中の runtime manifest", "package 直下と hook", lambda r: write(r / P / "skills/alpha/.codex-plugin/plugin.json", "{}"))
    expect("plugins/ 直下の manifest", "plugins/ 直下", lambda r: write(r / "plugins/.claude-plugin/plugin.json", "{}"))
    expect("package の中の symlink", "symlink", lambda r: (r / P / "skills/alpha/link").symlink_to(r / P / "lib"))
    expect("hooks のファイルが無い hook の部品", "hooks のファイルが無い", lambda r: make_sidecar(r, create_hooks=False))
    expect("SKILL.md を持つ hook の部品", "SKILL.md を持たない", lambda r: (make_sidecar(r, create_hooks=True), skill(r / P / "internal/session-hooks", "session-hooks")))
    print("Root contract self-test: passed")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("repository", nargs="?")
    args = parser.parse_args()
    if (args.repository is None) == (not args.self_test):
        parser.error("repository の絶対パスか --self-test のどちらか一つを渡す")
    try:
        self_test() if args.self_test else validate_repository(Path(args.repository))
    except (OSError, UnicodeError, ContractError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
