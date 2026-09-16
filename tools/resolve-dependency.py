#!/usr/bin/env python3
"""Resolve one name-qualified plugin dependency without pinning its version.

保守時の依存検査だけを担う（プラグイン間依存の規則.md §4・§5.1）。

  resolve-dependency.py --plugin-root <入口の絶対path> --plugin <名> --marketplace <名>
                        [--logical <論理名>] [--contract <marketplace/plugin>] [--bindings <lock>]
      1 つの依存を dev-map（HARNESS_PLUGIN_DEV_ROOTS）→ 同一 repository → installed cache の順に解き、
      外部依存なら implements の契約 ID が指す公開 playbook の入口へ解決して JSON を標準出力へ返す。
      --bindings を渡すと lock の snapshot を使い、content hash の drift を検査する。
  resolve-dependency.py --create-lock --entry <入口名> --repo-root <repo> [--scope-root <dir>] --lock <path>
      dependencies.yml（personal / project / scope の 1 層）を run 用 snapshot として lock へ書く。
  resolve-dependency.py --check-steps [<step id>]  < {"deps":…,"playbook_root":…,"playbook":…}
      解決済み依存と隣接 playbook.yml の steps を突き合わせ、外部依存を skill: / script: で呼ぶ形、
      `${.deps…}` 参照、implements の能力（types / actions）との不一致を拒否する。

`dep_references` / `dep_reference_violation` / `CONFIG_REFERENCE` と候補探索（dev_candidate /
repository_candidate / cache_candidate）は lint-consumer-contract.py が import して使う。
失敗は `[error:<code>] key=value …` を標準エラーへ出して終了 code 2。
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys


IDENTIFIER = re.compile(r"^[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*$")
CAPABILITY_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
IMPLEMENTS_KEYS = {"id", "version", "kind", "playbook", "types", "actions"}
BINDING_VALUE_KEYS = {"plugin", "marketplace"}
LOCK_KEYS = {"schema", "entry_playbook", "bindings_file", "bindings_layer", "bindings", "entries"}
LOCK_ENTRY_KEYS = {
    "marketplace", "plugin", "version", "root", "package_root",
    "content_hash", "source_kind",
}
CAPABILITY_KEYS = {"document_type": "types", "action": "actions"}
REQUIRED_IMPLEMENTATION_VERSIONS = {"write-doc/write-doc": 2}
# 公開 playbook の入口 file（plugin-package-contract.md）。契約版によらず同じ。
ENTRY_FILES = ("playbook.yml", "CONTRACT.md", "SKILL.md")
ENTRY_FILES_BY_CONTRACT = {1: ENTRY_FILES, 2: ENTRY_FILES}

PROPERTY_REFERENCE = re.compile(r"^\$\{\s*(\.[A-Za-z0-9_.\[\]\"'-]+)\s*\}$")

# ${.deps...} の解析はここ 1 箇所だけで行う。resolver も lint もこの関数を使う。
# ドット形・ブラケット形・引用形を同じ segment 列へ正規化してから許可形と突き合わせる。
DEP_REFERENCE = re.compile(
    r"\$\{\s*\.deps(?P<accessors>(?:\s*\.\s*[A-Za-z0-9_-]+"
    r"|\s*\.\s*\"[^\"]+\""
    r"|\s*\.\s*'[^']+'"
    r"|\s*\[\s*\"[^\"]+\"\s*\]"
    r"|\s*\[\s*'[^']+'\s*\])*)\s*\}(?P<suffix>[^\s\"'`]*)"
)
DEP_ACCESSOR = re.compile(
    r"\.\s*(?P<bare>[A-Za-z0-9_-]+)"
    r"|\.\s*\"(?P<dq>[^\"]+)\""
    r"|\.\s*'(?P<sq>[^']+)'"
    r"|\[\s*\"(?P<bdq>[^\"]+)\"\s*\]"
    r"|\[\s*'(?P<bsq>[^']+)'\s*\]"
)
# 依存先の解決済み YAML をプロパティで読む形。外部依存に対しては禁止する。
CONFIG_REFERENCE = re.compile(r"\$\{\s*(?P<name>[A-Za-z0-9_-]+)\s*:")


def dep_reference_segments(accessors: str) -> list[str]:
    segments = []
    for match in DEP_ACCESSOR.finditer(accessors):
        segments.append(next(value for value in match.groups() if value is not None))
    return segments


def dep_references(text: str):
    """文字列に現れる ${.deps...} をすべて (論理名, 続きのsegment列, suffix, 原文) で返す。"""
    for match in DEP_REFERENCE.finditer(text):
        segments = dep_reference_segments(match.group("accessors"))
        if not segments:
            continue
        yield segments[0], segments[1:], match.group("suffix") or "", match.group(0)


def dep_reference_violation(
    segments: list[str], suffix: str, external: bool, skills: object = None
) -> str | None:
    """${.deps.<名前>…} を 1 件判定する。**resolver も lint もこの関数だけを使う。**

    外部依存への参照は形を問わずすべて `external-dependency-path` で拒否する。消費側が使えるのは公開契約
    （CONTRACT.md）の入口・入力・出力だけで、依存先の path を文字列で組み立てる形は
    plugin-package-contract.md が禁止参照形として退ける。内部依存（同一 package）は内部契約なので
    形は縛らないが、`.skills.<名前>` だけは解決結果に実在する名前に限る。
    綴り違いの skill 名を静かに通すと、実行時まで誰も気付けない。
    解決結果が手元に無い（skills が None）ときは、名前の存否を判定しない。"""
    if external:
        return "external-dependency-path"
    if segments and segments[0] == "skills":
        if skills is None:
            return None
        if len(segments) != 2 or segments[1] not in skills:
            return "internal-skill-unknown"
    return None
SEMVER = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)


def fail(code: str, **fields: str) -> None:
    detail = " ".join(f"{key}={value}" for key, value in fields.items())
    print(f"[error:{code}] {detail}".rstrip(), file=sys.stderr)
    raise SystemExit(2)


def load_json(path: Path, code: str) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        fail(code, path=str(path), reason=str(exc))


def contained(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def identifier_component(value: str, label: str) -> str:
    """Return an identifier that is safe to use as one path component."""
    component = os.path.basename(value)
    if component != value or not IDENTIFIER.fullmatch(component):
        fail("dependency-invalid", **{label: value}, reason="identity")
    return component


def resolved_descendant(root: Path, raw: str, plugin: str, source_kind: str) -> Path:
    """Resolve a manifest-declared path and keep it below its package root."""
    boundary = os.path.realpath(os.fspath(root))
    lexical = os.path.abspath(os.path.join(boundary, raw))
    candidate = os.path.realpath(lexical)
    if not candidate.startswith(boundary + os.sep):
        fail("dependency-invalid", plugin=plugin, source_kind=source_kind, reason="path-escape")
    if candidate != lexical:
        fail("dependency-invalid", plugin=plugin, source_kind=source_kind, reason="path-symlink")
    if not os.path.isdir(candidate):
        fail("dependency-invalid", plugin=plugin, source_kind=source_kind, reason="root-not-directory")
    return Path(candidate)


def runtime_cache_roots() -> dict[str, list[Path]]:
    """runtime ごとの既知 installed cache root。明示の cache 指定、設定 directory
    （CLAUDE_CONFIG_DIR / CODEX_HOME）から導いた場所、既定の順に見る。"""

    def roots(cache_env: str, home_env: str, default_home: str) -> list[Path]:
        found: list[Path] = []
        override = os.environ.get(cache_env, "")
        if override:
            found.append(Path(override))
        home = os.environ.get(home_env, "")
        if home:
            found.append(Path(home) / "plugins" / "cache")
        found.append(Path.home() / default_home / "plugins" / "cache")
        return found

    return {
        "claude": roots("CLAUDE_PLUGIN_CACHE", "CLAUDE_CONFIG_DIR", ".claude"),
        "codex": roots("CODEX_PLUGIN_CACHE", "CODEX_HOME", ".codex"),
    }


def runtime_for(plugin_root: Path) -> str:
    explicit = os.environ.get("HARNESS_PLUGIN_RUNTIME", "")
    if explicit:
        if explicit not in {"claude", "codex"}:
            fail("dependency-runtime-unresolved", runtime=explicit)
        return explicit
    # **解決しようとしている plugin_root がどの cache に入っているか**を、
    # 環境変数の有無より先に見る。shell に CODEX_HOME があるだけで
    # Claude Code の cache を codex と呼んでしまう事故を防ぐ。
    resolved = plugin_root.resolve()
    detected = sorted(
        {
            name
            for name, roots in runtime_cache_roots().items()
            for root in roots
            if root.exists() and contained(root.resolve(), resolved)
        }
    )
    if len(detected) == 1:
        return detected[0]
    if not detected:
        # cache の外（開発中の checkout など）なら、動いている runtime を環境から推す。
        if os.environ.get("CLAUDE_PLUGIN_ROOT"):
            return "claude"
        if os.environ.get("CODEX_HOME"):
            return "codex"
    fail("dependency-runtime-unresolved", plugin_root=str(plugin_root))


def manifest_path(root: Path, runtime: str) -> Path:
    directory = ".codex-plugin" if runtime == "codex" else ".claude-plugin"
    return root / directory / "plugin.json"


def load_yaml(path: Path, code: str) -> object:
    """YAMLはyqで読む。独自パーサを持たない（重複キーの扱いもyqへ委ねる）。"""
    try:
        result = subprocess.run(
            ["yq", "-o=json", "-I=0", ".", os.fspath(path)],
            capture_output=True, text=True,
        )
    except OSError as exc:
        fail(code, path=str(path), reason=str(exc))
    if result.returncode:
        fail(code, path=str(path), reason="yaml-parse")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        fail(code, path=str(path), reason="yaml-parse")


def canonical_input_file(path: Path, code: str, label: str) -> Path:
    """束縛・入力として渡されたfileのidentityを検証する（絶対・正規・非symlink）。"""
    if not path.is_absolute() or any(part.is_symlink() for part in [path, *path.parents]):
        fail(code, path=str(path), reason=label + "-symlink")
    try:
        canonical = path.resolve(strict=True)
    except OSError as exc:
        fail(code, path=str(path), reason=label + "-unavailable", detail=str(exc))
    if canonical != path or not canonical.is_file():
        fail(code, path=str(path), reason=label + "-noncanonical")
    return canonical


def contract_id(value: object, label: str = "contract", code: str = "binding-key-invalid") -> tuple[str, str]:
    """契約IDを marketplace / plugin へ分解する。/ はちょうど1個。"""
    if not isinstance(value, str):
        fail(code, **{label: str(value)}, reason="contract-id-form")
    parts = value.split("/")
    if len(parts) != 2:
        fail(code, **{label: value}, reason="contract-id-form")
    for part in parts:
        if os.path.basename(part) != part or not IDENTIFIER.fullmatch(part):
            fail(code, **{label: value}, reason="contract-id-form")
    return parts[0], parts[1]


def harness_metadata(data: object) -> dict:
    if not isinstance(data, dict):
        return {}
    metadata = data.get("metadata")
    if not isinstance(metadata, dict):
        return {}
    harness = metadata.get("harness")
    return harness if isinstance(harness, dict) else {}


def owning_bundle(plugin_root: Path, runtime: str) -> tuple[Path, dict]:
    """所属package（bundle）を返す。

    公開入口 / 内部skill の root から上へ辿り、metadata.harness.marketplace を宣言する
    最初の manifest を採る。package root にだけ manifest がある（plugin-package-contract.md）。"""
    for ancestor in (plugin_root, *plugin_root.parents):
        candidate = manifest_path(ancestor, runtime)
        if not candidate.is_file() or candidate.is_symlink():
            continue
        data = load_json(candidate, "dependency-invalid")
        harness = harness_metadata(data)
        if isinstance(harness.get("marketplace"), str) and harness["marketplace"]:
            return ancestor.resolve(strict=True), data
    fail("dependency-invalid", reason="owning-bundle-unresolved", plugin_root=str(plugin_root))


def classify_dependency(
    bundle_root: Path,
    bundle: dict,
    marketplace: str,
    plugin: str,
    candidate: dict,
) -> str:
    """内部 = 自分のbundleが宣言したmarketplaceと一致し、かつ解決先がbundle自身か、
    internalPlugins の宣言pathと一致するもの。それ以外はすべて外部。"""
    harness = harness_metadata(bundle)
    if harness.get("marketplace") != marketplace:
        return "external"
    resolved = Path(candidate["package_root"]).resolve()
    if resolved == bundle_root:
        return "internal"
    internals = harness.get("internalPlugins")
    relative = internals.get(plugin) if isinstance(internals, dict) else None
    if not isinstance(relative, str):
        return "external"
    declared_root = resolved_descendant(bundle_root, relative, plugin, "owning-bundle")
    if resolved != declared_root or not contained(bundle_root, resolved):
        fail("external-dependency-internal-redirect", plugin=plugin,
             declared=str(declared_root), resolved=str(resolved))
    return "internal"


def validate_implements(root: Path, data: object, plugin: str, source_kind: str) -> list[dict]:
    """§3.4 I1〜I9。I10（両runtime一致）は配布validatorの担当。"""
    harness = harness_metadata(data)
    contract_version = harness.get("contractVersion", 1)
    if type(contract_version) is not int or contract_version not in ENTRY_FILES_BY_CONTRACT:
        fail("dependency-incompatible", reason="contract-version", version=str(contract_version))
    declared_market = harness.get("marketplace")
    if declared_market is not None and (
        not isinstance(declared_market, str) or not IDENTIFIER.fullmatch(declared_market)
    ):
        fail("implements-marketplace-mismatch", plugin=plugin, source_kind=source_kind,
             reason="marketplace-invalid")
    raw = harness.get("implements")
    if raw is None:
        return []
    if not isinstance(raw, list):
        fail("implements-schema", plugin=plugin, source_kind=source_kind, reason="not-array")
    # 差し替え先は他人の契約IDを実装する（stub-docs/stub-write-doc が write-doc/write-doc を
    # 実装する）ので、id の marketplace 部と自己宣言の一致は求めない。宣言そのものは必須。
    if raw and declared_market is None:
        fail("implements-marketplace-mismatch", plugin=plugin, source_kind=source_kind,
             reason="marketplace-missing")
    playbooks = harness.get("playbooks") if isinstance(harness.get("playbooks"), dict) else {}
    seen: set[str] = set()
    entries: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            fail("implements-schema", plugin=plugin, source_kind=source_kind, reason="entry-not-object")
        unknown = sorted(set(item) - IMPLEMENTS_KEYS)
        if unknown:
            fail("implements-schema", plugin=plugin, source_kind=source_kind,
                 reason="unknown-key", key=unknown[0])
        for required in ("id", "version", "kind", "playbook"):
            if required not in item:
                fail("implements-schema", plugin=plugin, source_kind=source_kind,
                     reason="missing-" + required)
        contract_id(item["id"], "id", "implements-id-invalid")
        if type(item["version"]) is not int or item["version"] != contract_version:
            fail("implements-version-invalid", plugin=plugin, source_kind=source_kind,
                 version=str(item["version"]))
        if item["kind"] != "playbook":
            fail("implements-kind-invalid", plugin=plugin, source_kind=source_kind,
                 kind=str(item["kind"]))
        name = item["playbook"]
        if not isinstance(name, str) or name not in playbooks:
            fail("implements-entry-missing", plugin=plugin, source_kind=source_kind,
                 playbook=str(name), reason="undeclared-playbook")
        entry_root = resolved_descendant(root, playbooks[name], plugin, source_kind)
        for relative in ENTRY_FILES_BY_CONTRACT[contract_version]:
            member = safe_path(entry_root, relative, exists=False)
            if not member.is_file() or member.is_symlink():
                fail("implements-entry-missing", plugin=plugin, source_kind=source_kind,
                     playbook=name, missing=relative)
        for key in ("types", "actions"):
            if key not in item:
                continue
            values = item[key]
            if not isinstance(values, list) or not all(
                isinstance(value, str) and CAPABILITY_SLUG.fullmatch(value) for value in values
            ):
                fail("implements-capability-invalid", plugin=plugin, source_kind=source_kind,
                     capability=key)
        if item["id"] in seen:
            fail("implements-duplicate", plugin=plugin, source_kind=source_kind, id=item["id"])
        seen.add(item["id"])
        entries.append(item)
    return entries


def contract_entry_paths(package_root: Path, data: object, entry: dict,
                         plugin: str, source_kind: str) -> tuple[Path, Path, str]:
    """契約が選んだ入口を返す。

    外部依存の root は implements[] のうち契約IDが一致する要素の playbook が
    bundle の playbooks map で指す directory である。"""
    harness = harness_metadata(data)
    playbooks = harness.get("playbooks") if isinstance(harness.get("playbooks"), dict) else {}
    relative = playbooks.get(entry.get("playbook"))
    if not isinstance(relative, str):
        fail("implements-entry-missing", plugin=plugin, source_kind=source_kind,
             playbook=str(entry.get("playbook")), reason="undeclared-playbook")
    root = resolved_descendant(package_root, relative, plugin, source_kind)
    skill = safe_path(root, "SKILL.md")
    if not skill.is_file() or skill.is_symlink():
        fail("implements-entry-missing", plugin=plugin, source_kind=source_kind,
             playbook=str(entry.get("playbook")), missing="SKILL.md")
    return root, skill, skill_name(skill)


def load_bindings(path: Path) -> dict[str, dict[str, str]]:
    """§2.3 B1〜B6。dependencies.yml を読んで 契約ID → 実体 を返す。"""
    checked = canonical_input_file(path, "binding-file-unsafe", "bindings-file")
    data = load_yaml(checked, "binding-file-invalid")
    if not isinstance(data, dict) or set(data) != {"version", "bindings"}:
        fail("binding-file-invalid", path=str(checked), reason="top-level-keys")
    if type(data["version"]) is not int or data["version"] != 1:
        fail("binding-file-invalid", path=str(checked), reason="version")
    bindings = data["bindings"]
    if bindings is None:
        bindings = {}
    if not isinstance(bindings, dict):
        fail("binding-file-invalid", path=str(checked), reason="bindings-not-mapping")
    result: dict[str, dict[str, str]] = {}
    for key, value in bindings.items():
        contract_id(key, "contract")
        if not isinstance(value, dict) or set(value) != BINDING_VALUE_KEYS:
            fail("binding-schema", contract=str(key), reason="value-keys")
        for field in sorted(BINDING_VALUE_KEYS):
            if not isinstance(value[field], str) or not IDENTIFIER.fullmatch(value[field]) \
                    or os.path.basename(value[field]) != value[field]:
                fail("binding-schema", contract=str(key), reason=field)
        result[key] = {"marketplace": value["marketplace"], "plugin": value["plugin"]}
    return result


def bindings_layers(repo_root: Path, scope_root: Path | None) -> list[tuple[str, Path]]:
    base = os.environ.get("XDG_CONFIG_HOME") or os.fspath(Path.home() / ".config")
    layers = [
        ("personal", Path(base) / "harness-plugins/dependencies.yml"),
        ("project", repo_root / ".harness-plugins/dependencies.yml"),
    ]
    if scope_root is not None:
        layers.append(("scope", scope_root / "dependencies.yml"))
    return layers


def select_bindings_file(repo_root: Path, scope_root: Path | None) -> tuple[str, Path | None]:
    """3層から1ファイルだけを選ぶ。層はマージしない。入口自身もscope層を選ぶ。"""
    layer, selected = "none", None
    for name, path in bindings_layers(repo_root, scope_root):
        if path.is_file():
            layer, selected = name, path
    return layer, selected


def load_lock(path: Path) -> dict:
    checked = canonical_input_file(path, "binding-file-missing", "bindings-lock")
    try:
        data = json.loads(checked.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        fail("binding-lock-invalid", path=str(checked), reason="parse")
    if not isinstance(data, dict) or set(data) != LOCK_KEYS or data.get("schema") != 1:
        fail("binding-lock-invalid", path=str(checked), reason="top-level-keys")
    if not isinstance(data.get("entries"), dict) or not isinstance(data.get("bindings"), dict):
        fail("binding-lock-invalid", path=str(checked), reason="entries")
    for key, value in data["bindings"].items():
        contract_id(key, "contract")
        if not isinstance(value, dict) or set(value) != BINDING_VALUE_KEYS:
            fail("binding-schema", contract=str(key), reason="value-keys")
    for key, entry in data["entries"].items():
        contract_id(key, "contract")
        if not isinstance(entry, dict) or set(entry) != LOCK_ENTRY_KEYS:
            fail("binding-lock-invalid", path=str(checked), contract=str(key), reason="entry-keys")
    if data.get("bindings_layer") not in {"personal", "project", "scope", "none"}:
        fail("binding-lock-invalid", path=str(checked), reason="bindings-layer")
    return data


def write_lock(path: Path, entry_playbook: str, layer: str, bindings_file: Path | None,
               bindings: dict[str, dict[str, str]]) -> None:
    """入口が選んだ dependencies.yml の bindings 全体を snapshot として書く。

    子は元ファイルを再読込しない。実行の途中で元ファイルが書き換わっても、
    入れ子の各段が同じ束縛を見る。"""
    payload = {
        "schema": 1,
        "entry_playbook": entry_playbook,
        "bindings_file": str(bindings_file) if bindings_file else None,
        "bindings_layer": layer,
        "bindings": bindings,
        "entries": {},
    }
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def append_lock(path: Path, contract: str, entry: dict) -> None:
    """同じrunのあいだ1ファイルを共有する。追記は排他ロック付きで行う。"""
    descriptor = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "r+", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            data = json.load(handle)
        except json.JSONDecodeError:
            fail("binding-lock-invalid", path=str(path), reason="parse")
        if not isinstance(data, dict) or not isinstance(data.get("entries"), dict):
            fail("binding-lock-invalid", path=str(path), reason="entries")
        data["entries"][contract] = entry
        handle.seek(0)
        handle.truncate()
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def internal_candidate(bundle_root: Path, bundle: dict, runtime: str, plugin: str, root: Path,
                       source_kind: str) -> dict[str, str]:
    """内部 skill（internal/<name>/、nested manifest 無し）の候補。identity と契約版は所属 package の manifest から取る。"""
    canonical = canonical_input_root(root, "internal-root")
    if not contained(bundle_root, canonical):
        fail("dependency-invalid", plugin=plugin, source_kind=source_kind, reason="path-escape")
    harness = harness_metadata(bundle)
    contract = harness.get("contractVersion", 1)
    if type(contract) is not int or contract not in ENTRY_FILES_BY_CONTRACT:
        fail("dependency-incompatible", reason="contract-version", version=str(contract))
    version = bundle.get("version")
    if not isinstance(version, str) or semver_key(version) is None:
        fail("dependency-invalid", plugin=plugin, source_kind=source_kind, reason="manifest-version-invalid")
    skill = safe_path(canonical, "SKILL.md")
    if not skill.is_file() or skill.is_symlink():
        fail("dependency-invalid", plugin=plugin, source_kind=source_kind, reason="skill-entry-missing", path=str(skill))
    name = skill_name(skill)
    if name != plugin:
        fail("dependency-invalid", plugin=plugin, source_kind=source_kind, reason="manifest-identity-mismatch")
    return {
        "contract_version": contract,
        "content_hash": content_hash(canonical),
        "skills": {name: str(skill)},
        "prerelease_policy": "explicit-opt-in",
        "plugin": plugin,
        "version": version,
        "runtime": runtime,
        "source_kind": source_kind,
        "root": str(canonical),
        "package_root": str(bundle_root),
        "manifest": str(manifest_path(bundle_root, runtime)),
        "marketplace_declared": harness.get("marketplace"),
        "implements": [],
    }


def validate_candidate(
    root: Path,
    runtime: str,
    plugin: str,
    source_kind: str,
    containment: Path | None = None,
    expected_directory_version: str | None = None,
) -> dict[str, str]:
    try:
        canonical = root.resolve(strict=True)
    except OSError as exc:
        fail("dependency-invalid", plugin=plugin, source_kind=source_kind, reason=str(exc))
    if not canonical.is_dir():
        fail("dependency-invalid", plugin=plugin, source_kind=source_kind, reason="root-not-directory")
    if containment is not None:
        boundary = containment.resolve(strict=True)
        if not contained(boundary, canonical):
            fail("dependency-invalid", plugin=plugin, source_kind=source_kind, reason="path-escape")
    manifest = safe_path(canonical, str(manifest_path(canonical, runtime).relative_to(canonical)))
    if not manifest.is_file() or manifest.is_symlink():
        fail("dependency-invalid", plugin=plugin, source_kind=source_kind, reason="manifest-missing")
    data = load_json(manifest, "dependency-invalid")
    if not isinstance(data, dict) or data.get("name") != plugin:
        fail("dependency-invalid", plugin=plugin, source_kind=source_kind, reason="manifest-identity-mismatch")
    version = data.get("version")
    if not isinstance(version, str) or semver_key(version) is None:
        fail("dependency-invalid", plugin=plugin, source_kind=source_kind, reason="manifest-version-invalid")
    if expected_directory_version is not None and version != expected_directory_version:
        fail("dependency-invalid", plugin=plugin, source_kind=source_kind, reason="cache-version-mismatch")
    metadata = data.get("metadata", {})
    if metadata is None:
        metadata = {}
    if not isinstance(metadata, dict):
        fail("dependency-invalid", plugin=plugin, source_kind=source_kind, reason="manifest-metadata-invalid")
    harness = metadata.get("harness", {})
    if harness is None:
        harness = {}
    if not isinstance(harness, dict):
        fail("dependency-invalid", plugin=plugin, source_kind=source_kind, reason="manifest-harness-metadata-invalid")
    entry_root = canonical
    contract = harness.get("contractVersion", 1)
    if type(contract) is not int or contract not in ENTRY_FILES_BY_CONTRACT:
        fail("dependency-incompatible", reason="contract-version", version=str(contract))
    implements = validate_implements(canonical, data, plugin, source_kind)
    skills = public_skills(canonical, data)
    return {
        "contract_version": contract,
        "content_hash": content_hash(canonical),
        "skills": skills,
        "prerelease_policy": "explicit-opt-in",
        "plugin": plugin,
        "version": version,
        "runtime": runtime,
        "source_kind": source_kind,
        "root": str(entry_root),
        "package_root": str(canonical),
        "manifest": str(manifest),
        "marketplace_declared": harness.get("marketplace"),
        "implements": implements,
    }



def canonical_input_root(root: Path, label: str = "path-root") -> Path:
    """Validate persisted root identity without rebasing through new symlinks."""
    if not root.is_absolute() or any(path.is_symlink() for path in [root, *root.parents]):
        fail("dependency-invalid", reason=label + "-symlink")
    try:
        canonical = root.resolve(strict=True)
    except OSError as exc:
        fail("dependency-invalid", reason=label + "-unavailable", detail=str(exc))
    if canonical != root or not canonical.is_dir():
        fail("dependency-invalid", reason=label + "-noncanonical")
    return canonical


def safe_path(root: Path, raw: str, exists: bool = True) -> Path:
    if not isinstance(raw, str) or not raw or Path(raw).is_absolute() or ".." in Path(raw).parts or "\\" in raw:
        fail("dependency-invalid", reason="path-format", path=str(raw))
    boundary = canonical_input_root(root)
    lexical = Path(os.path.abspath(boundary / raw))
    if not contained(boundary, lexical):
        fail("dependency-invalid", reason="path-escape", path=raw)
    current = boundary
    for part in lexical.relative_to(boundary).parts:
        current /= part
        if current.is_symlink():
            fail("dependency-invalid", reason="path-symlink", path=raw)
    if exists and not lexical.exists():
        fail("dependency-invalid", reason="path-missing", path=raw)
    return lexical


def skill_name(path: Path) -> str:
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0] != "---" or "---" not in lines[1:]:
        fail("dependency-invalid", reason="skill-frontmatter", path=str(path))
    header = lines[1:lines[1:].index("---") + 1]
    names = [line[6:].strip().strip("\"'") for line in header if line.startswith("name: ")]
    if len(names) != 1 or not IDENTIFIER.fullmatch(names[0]):
        fail("dependency-invalid", reason="skill-name", path=str(path))
    return names[0]


def public_skills(root: Path, data: dict) -> dict[str, str]:
    root = canonical_input_root(root)
    declared = data.get("skills")
    if declared is None:
        paths = [root / "SKILL.md"] if (root / "SKILL.md").is_file() else list((root / "skills").glob("*/SKILL.md"))
    else:
        if isinstance(declared, str):
            declared = [declared]
        if not isinstance(declared, list) or not declared:
            fail("dependency-invalid", reason="skills-schema")
        paths = []
        for raw in declared:
            member = safe_path(root, raw)
            candidates = [member / "SKILL.md"] if (member / "SKILL.md").exists() else list(member.glob("*/SKILL.md"))
            if not candidates:
                fail("dependency-invalid", reason="skill-entry-missing", path=str(member))
            paths.extend(candidates)
    result = {}
    for path in paths:
        path = safe_path(root, str(path.relative_to(root)))
        name = skill_name(path)
        if name in result:
            fail("dependency-invalid", reason="duplicate-skill", skill=name)
        result[name] = str(path)
    return result


def content_hash(root: Path) -> str:
    root = canonical_input_root(root)
    digest = hashlib.sha256()
    for directory, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in {".git", "__pycache__", ".harness-plugin-test-cache"})
        for name in sorted(dirs + files):
            path = Path(directory) / name
            if path.is_symlink():
                fail("dependency-invalid", reason="path-symlink", path=str(path))
        for name in sorted(files):
            path = Path(directory) / name
            if path.suffix == ".pyc":
                continue
            digest.update(str(path.relative_to(root)).encode() + b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
    return digest.hexdigest()


def iter_strings(value: object):
    """step の中に現れるすべての文字列（キーを含む）を順に返す。"""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str):
                yield key
            yield from iter_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from iter_strings(item)


def lookup(data: object, dotted: str) -> object:
    """${.a.b} の静的scalarだけを引く。無い、または scalar でなければ None。"""
    current = data
    for part in dotted.strip().lstrip(".").split("."):
        part = part.strip().strip("\"'[]")
        if not part or not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current if type(current) in {str, int, float, bool} else None


def resolve_step_input(config: dict, step: dict, key: str):
    """能力検査（implements の types / actions）に使う静的 scalar だけを返す。

    文字列 literal と、自分の playbook property を指す `${.a.b}` の静的 scalar が対象。
    `${output_directory}` のような実行時 placeholder を含む値、object / list / null は
    実行担当が組み立てる動的値なので None を返し、検査しない。"""
    block = step.get("input")
    if not isinstance(block, dict) or key not in block:
        return None
    raw = block[key]
    if not isinstance(raw, str):
        return None
    matched = PROPERTY_REFERENCE.fullmatch(raw)
    if matched:
        return lookup(config.get("playbook", {}), matched.group(1))
    placeholder_start = raw.find("${")
    if placeholder_start >= 0 and raw.find("}", placeholder_start + 2) >= 0:
        return None
    return raw


def contract_entry(dep: dict) -> dict | None:
    contract = dep.get("contract")
    required_version = REQUIRED_IMPLEMENTATION_VERSIONS.get(contract, 1)
    for entry in dep.get("implements") or []:
        if (isinstance(entry, dict) and entry.get("id") == contract
                and entry.get("version") == required_version):
            return entry
    return None


def check_steps(config: dict, selected: str | None = None) -> None:
    deps = config["deps"]
    available: dict[str, tuple[str, str]] = {}
    external = {name for name, dep in deps.items() if dep.get("dependency_scope") == "external"}
    for name, dep in deps.items():
        root = Path(dep.get("package_root", dep["root"]))
        root = canonical_input_root(root, "package-root")
        entry_root = canonical_input_root(Path(dep["root"]), "entry-root")
        if not contained(root, entry_root):
            fail("dependency-invalid", reason="entry-root-escape")
        # Inspect the lexical path before reading; hashing afterwards is too late.
        try:
            relative_manifest = Path(dep["manifest"]).relative_to(root)
        except ValueError:
            fail("dependency-invalid", reason="manifest-path-escape")
        checked_manifest = safe_path(root, str(relative_manifest))
        if not checked_manifest.is_file():
            fail("dependency-invalid", reason="manifest-not-file")
        manifest = load_json(checked_manifest, "dependency-invalid")
        if dep.get("content_hash") != content_hash(root):
            fail("dependency-changed", reason="content-hash", plugin=dep.get("plugin", "unknown"))
        contract = manifest.get("metadata", {}).get("harness", {}).get("contractVersion", 1)
        if (type(contract) is not int or contract not in ENTRY_FILES_BY_CONTRACT
                or contract != dep.get("contract_version")):
            fail("dependency-incompatible", reason="contract-version")
        if validate_implements(root, manifest, dep.get("plugin", "unknown"), "check-steps") != (dep.get("implements") or []):
            fail("dependency-changed", reason="implements", plugin=dep.get("plugin", "unknown"))
        if dep.get("dependency_scope") == "external":
            entry = contract_entry(dep)
            if entry is None:
                fail("external-dependency-no-playbook", plugin=name, contract=str(dep.get("contract")))
            entry_root, entry_skill_path, entry_skill = contract_entry_paths(
                root, manifest, entry, dep.get("plugin", "unknown"), "check-steps")
            if (str(entry_root), str(entry_skill_path), entry_skill) != (
                    dep["root"], dep.get("entry"), dep.get("entry_skill")):
                fail("dependency-changed", reason="entry", plugin=dep.get("plugin", "unknown"))
        for skill, path in public_skills(root, manifest).items():
            if skill in available and available[skill][0] != path:
                fail("dependency-invalid", reason="duplicate-skill", skill=skill)
            available[skill] = (path, name)
    matched = selected is None
    for step in config["playbook"]["steps"]:
        if selected == step["id"]:
            matched = True
        active = selected == step["id"] if selected else step.get("when") is None

        # (c) step 内の全文字列から、許した形以外の依存参照を拒否する。
        # when 付きの非活性 step も対象にする。条件に違反を隠せてしまうと規則が規則でなくなる。
        for text in iter_strings(step):
            for name, segments, suffix, raw in dep_references(text):
                if name not in deps:
                    continue
                code = dep_reference_violation(
                    segments, suffix, name in external, deps[name].get("skills"))
                if code:
                    fail(code, step=step["id"], plugin=name, reference=raw)
            for match in CONFIG_REFERENCE.finditer(text):
                if match.group("name") in external:
                    fail("external-dependency-config", step=step["id"],
                         plugin=match.group("name"), reference=match.group(0))

        if "skill" in step:
            identifier_component(step["skill"], "skill")
            # (a) 外部依存の skill は呼べない。when に関係なく落とす。
            if step["skill"] in available and available[step["skill"]][1] in external:
                fail("external-dependency-skill", step=step["id"],
                     skill=step["skill"], plugin=available[step["skill"]][1])
            # 同 package の公開入口・内部 skill は requires に載らない。その実在は root validator が検査する。
        if "script" in step:
            owner = step.get("plugin")
            if owner:
                identifier_component(owner, "plugin")
            # (b) 外部依存の script は実行できない。when に関係なく落とす。
            if owner in external:
                fail("external-dependency-script", step=step["id"], plugin=owner)
            base = Path(deps[owner]["root"]) if owner in deps else Path(config["playbook_root"])
            if owner and owner not in deps and active:
                fail("dependency-invalid", reason="script-owner-missing", plugin=owner)
            path = safe_path(base, step["script"], exists=active)
            if active and not path.is_file():
                fail("dependency-invalid", reason="script-not-file", path=str(path))
        if "playbook" in step:
            name = identifier_component(step["playbook"], "playbook")
            if active:
                if name not in deps:
                    fail("dependency-invalid", reason="playbook-owner-missing", plugin=name)
                dep = deps[name]
                base = Path(dep["root"])
                safe_path(base, "playbook.yml")
                if name in external:
                    entry = contract_entry(dep)
                    if entry is None:
                        fail("external-dependency-no-playbook", plugin=name,
                             contract=str(dep.get("contract")))
                    # (e) 能力検査：解決できた入力値だけを implements と突き合わせる
                    for key, declared_key in CAPABILITY_KEYS.items():
                        value = resolve_step_input(config, step, key)
                        if value is None:
                            continue
                        declared = entry.get(declared_key)
                        if declared is None:
                            fail("binding-capability-undeclared", plugin=name,
                                 capability=declared_key, value=str(value))
                        if value not in declared:
                            fail("binding-capability-unsupported", plugin=name,
                                 capability=declared_key, value=str(value),
                                 contract=str(dep.get("contract")))
    if not matched:
        fail("dependency-invalid", reason="unknown-step", step=str(selected))


def dev_candidate(identity: str, runtime: str, plugin: str) -> dict[str, str] | None:
    raw = os.environ.get("HARNESS_PLUGIN_DEV_ROOTS", "")
    if not raw:
        return None
    path = Path(raw)
    data = load_json(path, "dependency-invalid")
    if not isinstance(data, dict) or data.get("schema") != 1 or not isinstance(data.get("dependencies"), dict):
        fail("dependency-invalid", plugin=plugin, source_kind="dev-map", reason="schema")
    root = data["dependencies"].get(identity)
    if root is None:
        return None
    if not isinstance(root, str) or not Path(root).is_absolute():
        fail("dependency-invalid", plugin=plugin, source_kind="dev-map", reason="root-must-be-absolute")
    return validate_candidate(Path(root), runtime, plugin, "dev-map")


def repository_candidate(plugin_root: Path, marketplace: str, runtime: str, plugin: str) -> dict[str, str] | None:
    rel_market = Path(".agents/plugins/marketplace.json") if runtime == "codex" else Path(".claude-plugin/marketplace.json")
    for ancestor in (plugin_root, *plugin_root.parents):
        bundle_manifest = manifest_path(ancestor, runtime)
        if bundle_manifest.is_file():
            bundle = load_json(bundle_manifest, "dependency-invalid")
            if harness_metadata(bundle).get("marketplace") == marketplace:
                if bundle.get("name") == plugin:
                    return validate_candidate(ancestor, runtime, plugin, "repository", ancestor)
                internal = harness_metadata(bundle).get("internalPlugins", {})
                relative = internal.get(plugin) if isinstance(internal, dict) else None
                if isinstance(relative, str):
                    candidate = resolved_descendant(ancestor, relative, plugin, "repository")
                    return internal_candidate(ancestor.resolve(strict=True), bundle, runtime, plugin, candidate, "repository")
        manifest = ancestor / rel_market
        if not manifest.is_file():
            continue
        data = load_json(manifest, "dependency-invalid")
        if not isinstance(data, dict) or data.get("name") != marketplace:
            return None
        matches = [item for item in data.get("plugins", []) if isinstance(item, dict) and item.get("name") == plugin]
        if len(matches) > 1:
            fail("dependency-invalid", plugin=plugin, marketplace=marketplace, source_kind="repository", reason="marketplace-entry")
        if len(matches) == 1:
            source = matches[0].get("source")
            if runtime == "codex":
                if not isinstance(source, dict) or source.get("source") != "local" or not isinstance(source.get("path"), str):
                    fail("dependency-invalid", plugin=plugin, marketplace=marketplace, source_kind="repository", reason="source")
                relative = source["path"]
            else:
                if not isinstance(source, str):
                    fail("dependency-invalid", plugin=plugin, marketplace=marketplace, source_kind="repository", reason="source")
                relative = source
        else:
            bundle_manifest = manifest_path(ancestor, runtime)
            bundle = load_json(bundle_manifest, "dependency-invalid")
            if harness_metadata(bundle).get("marketplace") != marketplace:
                fail("dependency-invalid", plugin=plugin, marketplace=marketplace, source_kind="repository", reason="bundle-identity")
            internal = harness_metadata(bundle).get("internalPlugins", {})
            relative = internal.get(plugin) if isinstance(internal, dict) else None
            if not isinstance(relative, str):
                fail("dependency-invalid", plugin=plugin, marketplace=marketplace, source_kind="repository", reason="marketplace-entry")
            candidate = resolved_descendant(ancestor, relative, plugin, "repository")
            return internal_candidate(ancestor.resolve(strict=True), bundle, runtime, plugin, candidate, "repository")
        candidate = resolved_descendant(ancestor, relative, plugin, "repository")
        return validate_candidate(candidate, runtime, plugin, "repository", ancestor)
    return None


def semver_key(value: str) -> tuple[int, int, int, int, tuple[tuple[int, int | str], ...]] | None:
    matched = SEMVER.fullmatch(value)
    if matched is None:
        return None
    core_and_pre = value.split("+", 1)[0]
    core, separator, prerelease = core_and_pre.partition("-")
    major, minor, patch = (int(part) for part in core.split("."))
    if not separator:
        return major, minor, patch, 1, ()
    identifiers: list[tuple[int, int | str]] = []
    for part in prerelease.split("."):
        if part.isdigit() and len(part) > 1 and part.startswith("0"):
            return None
        identifiers.append((0, int(part)) if part.isdigit() else (1, part))
    return major, minor, patch, 0, tuple(identifiers)


def selected_cache_root(runtime: str, plugin_root: Path, plugin: str) -> Path:
    override = os.environ.get("HARNESS_PLUGIN_CACHE_ROOT", "")
    if override:
        test_cache = plugin_root / ".harness-plugin-test-cache"
        if os.path.realpath(override) != os.path.realpath(test_cache):
            fail("dependency-invalid", plugin=plugin, source_kind="installed-cache", reason="test-cache-root-mismatch")
        return test_cache
    for ancestor in (plugin_root, *plugin_root.parents):
        if ancestor.name == "cache" and ancestor.parent.name == "plugins" and ancestor.is_dir():
            return ancestor
    fail("dependency-invalid", plugin=plugin, runtime=runtime, source_kind="installed-cache", reason="cache-root-unresolved")


def named_directory(parent: Path, expected: str) -> Path | None:
    if not parent.is_dir() or parent.is_symlink():
        return None
    matches = [child for child in parent.iterdir() if child.name == expected and child.is_dir() and not child.is_symlink()]
    if len(matches) > 1:
        fail("dependency-invalid", source_kind="installed-cache", reason="duplicate-cache-directory")
    return matches[0] if matches else None


def cache_candidate(marketplace: str, runtime: str, plugin: str, plugin_root: Path) -> dict[str, str] | None:
    cache = selected_cache_root(runtime, plugin_root, plugin)
    marketplace_cache = named_directory(cache, marketplace)
    if marketplace_cache is None:
        return None
    plugin_cache = named_directory(marketplace_cache, plugin)
    if plugin_cache is not None:
        candidates = [
            (key, child)
            for child in plugin_cache.iterdir()
            if child.is_dir() and not child.is_symlink() and (key := semver_key(child.name)) is not None
            and (key[3] == 1 or os.environ.get("HARNESS_PLUGIN_ALLOW_PRERELEASE") == "1")
        ]
        if candidates:
            _, candidate = max(candidates, key=lambda item: (item[0], item[1].name))
            return validate_candidate(
                candidate,
                runtime,
                plugin,
                "installed-cache",
                cache,
                expected_directory_version=candidate.name,
            )

    # 内部pluginは、呼び出し元自身が同じpackage内にある場合だけ
    # repository_candidateで解決する。外部repositoryからcache内の内部実装を
    # 指定されても公開契約として扱わない。
    return None


def resolve_plugin_root(declared: str) -> Path:
    """解決の起点（公開入口または内部 skill の directory）。絶対 path で明示し、symlink を含まない。"""
    root = Path(declared)
    if not root.is_absolute():
        fail("dependency-invalid", reason="plugin-root-not-absolute", declared=declared)
    return canonical_input_root(root, "plugin-root")


def command_create_lock(argv: list[str]) -> int:
    """入口が3層から dependencies.yml を1つ選び、run用の束縛lockを作る。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--create-lock", action="store_true")
    parser.add_argument("--entry", required=True)
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--scope-root")
    parser.add_argument("--lock", required=True)
    args = parser.parse_args(argv)
    repo_root = Path(args.repo_root)
    scope_root = Path(args.scope_root) if args.scope_root else None
    layer, selected = select_bindings_file(repo_root, scope_root)
    bindings: dict[str, dict[str, str]] = {}
    if selected is not None:
        selected = canonical_input_file(selected, "binding-file-unsafe", "bindings-file")
        bindings = load_bindings(selected)
    lock = Path(args.lock)
    if lock.is_symlink() or not lock.parent.is_dir():
        fail("binding-file-unsafe", path=str(lock), reason="lock-path")
    write_lock(lock, args.entry, layer, selected, bindings)
    print(str(lock))
    return 0


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "--check-steps":
        config = json.load(sys.stdin)
        check_steps(config, sys.argv[2] if len(sys.argv) == 3 else None)
        return 0
    if len(sys.argv) > 1 and sys.argv[1] == "--create-lock":
        return command_create_lock(sys.argv[1:])
    parser = argparse.ArgumentParser()
    parser.add_argument("--plugin-root", required=True)
    parser.add_argument("--plugin", required=True)
    parser.add_argument("--marketplace", required=True)
    parser.add_argument("--logical")
    parser.add_argument("--contract")
    parser.add_argument("--bindings")
    args = parser.parse_args()
    plugin = identifier_component(args.plugin, "plugin")
    marketplace = identifier_component(args.marketplace, "marketplace")
    logical = identifier_component(args.logical or args.plugin, "logical")
    contract = args.contract or f"{marketplace}/{plugin}"
    contract_id(contract, "contract")
    plugin_root = resolve_plugin_root(args.plugin_root)
    runtime = runtime_for(plugin_root)
    bundle_root, bundle = owning_bundle(plugin_root, runtime)

    lock_path = None
    lock: dict = {"entries": {}, "bindings": {}, "bindings_file": None, "bindings_layer": "none"}
    if args.bindings and args.bindings != "none":
        lock_path = canonical_input_file(Path(args.bindings), "binding-file-missing", "bindings-lock")
        lock = load_lock(lock_path)
    entries = lock.get("entries", {})
    bindings_file = lock.get("bindings_file")
    # 元ファイルは読み直さない。入口が固定した snapshot だけを見る。
    table = lock.get("bindings") or {}
    bound = contract in table
    requested = {"marketplace": marketplace, "plugin": plugin}
    expected_hash = None
    if contract in entries:
        effective = entries[contract]
        expected_hash = effective.get("content_hash")
    elif bound:
        effective = table[contract]
    else:
        effective = requested
    binding_source = {
        "layer": lock.get("bindings_layer") if bound else "bundled",
        "file": bindings_file if bound else None,
        "lock": str(lock_path) if lock_path else None,
    }

    effective_market = identifier_component(effective["marketplace"], "marketplace")
    effective_plugin = identifier_component(effective["plugin"], "plugin")
    identity = f"{effective_market}/{effective_plugin}"
    candidate = dev_candidate(identity, runtime, effective_plugin)
    if candidate is None:
        candidate = repository_candidate(plugin_root, effective_market, runtime, effective_plugin)
    if candidate is None:
        candidate = cache_candidate(effective_market, runtime, effective_plugin, plugin_root)
    if candidate is None:
        fail(
            "dependency-missing",
            plugin=effective_plugin,
            marketplace=effective_market,
            runtime=runtime,
            install=f"{effective_plugin}@{effective_market}",
        )
    scope = classify_dependency(bundle_root, bundle, marketplace, plugin, candidate)
    if scope == "external":
        required_version = REQUIRED_IMPLEMENTATION_VERSIONS.get(contract, 1)
        entry = next(
            (item for item in candidate["implements"]
             if item["id"] == contract and item["version"] == required_version),
            None,
        )
        if entry is None:
            code = "binding-not-implemented" if bound else "external-dependency-no-playbook"
            fail(code, plugin=effective_plugin, marketplace=effective_market, contract=contract)
        if bound:
            candidate_bundle, _ = owning_bundle(Path(candidate["package_root"]), runtime)
            if candidate_bundle != Path(candidate["package_root"]).resolve():
                fail("binding-internal-plugin", contract=contract, plugin=effective_plugin)
        package_root = Path(candidate["package_root"])
        manifest_data = load_json(Path(candidate["manifest"]), "dependency-invalid")
        entry_root, entry_skill_path, entry_skill = contract_entry_paths(
            package_root, manifest_data, entry, effective_plugin, candidate["source_kind"])
        candidate["root"] = str(entry_root)
        candidate["entry"] = str(entry_skill_path)
        candidate["entry_skill"] = entry_skill
    elif bound:
        fail("binding-internal-dependency", contract=contract)

    if expected_hash is not None and candidate["content_hash"] != expected_hash:
        fail("binding-drift", contract=contract, expected=expected_hash,
             actual=candidate["content_hash"])
    candidate.setdefault("entry", None)
    candidate.setdefault("entry_skill", None)
    candidate["marketplace"] = effective_market
    candidate["dependency_scope"] = scope
    candidate["contract"] = contract
    candidate["logical"] = logical
    candidate["requested"] = requested
    candidate["binding_source"] = binding_source
    if lock_path is not None and scope == "external" and contract not in entries:
        append_lock(lock_path, contract, {
            "marketplace": effective_market,
            "plugin": effective_plugin,
            "version": candidate["version"],
            "root": candidate["root"],
            "package_root": candidate["package_root"],
            "content_hash": candidate["content_hash"],
            "source_kind": candidate["source_kind"],
        })
    print(json.dumps(candidate, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
