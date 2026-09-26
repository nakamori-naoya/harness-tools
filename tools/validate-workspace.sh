#!/usr/bin/env bash
# workspace root の規約入口と、明示された新規・変更対象 repository だけへ全体契約と負例を fail-closed で適用する。
#
#   validate-workspace.sh --workspace <workspace root の絶対path> /absolute/path/to/changed-repository [...]
#
# workspace root の scripts/validate.sh がこの script へ委譲する（薄い入口）。root の絶対 path は入口が渡す。
# 決定的に検査できる workspace 規則だけを確認する。文書の明確さ・妥当性・十分性は判定対象にしない。
set -uo pipefail

TOOLS=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

WORKSPACE=''
if [ "${1:-}" = '--workspace' ]; then
  [ "$#" -ge 2 ] || { echo '[error] --workspace に値が無い' >&2; exit 2; }
  WORKSPACE="$2"; shift 2
fi
case "$WORKSPACE" in
  /*) ;;
  *) echo 'usage: validate-workspace.sh --workspace /absolute/workspace/root /absolute/path/to/changed-repository [...]' >&2; exit 2 ;;
esac
[ -d "$WORKSPACE" ] || { echo "[error] workspace root が無い: $WORKSPACE" >&2; exit 2; }
if [ "$#" -eq 0 ]; then
  echo 'usage: bash scripts/validate.sh /absolute/path/to/changed-repository [...]  （workspace root から。本体: harness-tools/tools/validate-workspace.sh --workspace <root> <repository>...）' >&2
  exit 2
fi

status=0

for cmd in jq rg; do
  command -v "$cmd" >/dev/null 2>&1 || { echo "[error] root validation requires $cmd" >&2; exit 2; }
done
command -v yq >/dev/null 2>&1 && yq --version 2>/dev/null | rg -q 'mikefarah' \
  || { echo '[error] root validation requires mikefarah/yq v4' >&2; exit 2; }

bash "$TOOLS/test-install-plugins.sh" || status=1

for name in harness-principles plugin-package-contract deterministic-validation; do
  rule="$WORKSPACE/.agents/rules/$name.md"
  [ -f "$rule" ] || { echo "workspace rule is missing: $rule" >&2; status=1; continue; }
  rg -Fq "$rule" "$WORKSPACE/AGENTS.md" \
    || { echo "root AGENTS.md does not reference workspace rule: $rule" >&2; status=1; }
done

rg -Fxq '@AGENTS.md' "$WORKSPACE/CLAUDE.md" \
  || { echo "root CLAUDE.md does not import root AGENTS.md: $WORKSPACE/CLAUDE.md" >&2; status=1; }

python3 "$TOOLS/validate-plugin-repository.py" --self-test >/dev/null || status=1

for repository in "$@"; do
  case "$repository" in
    /*) ;;
    *) echo "repository must be an absolute path: $repository" >&2; status=1; continue ;;
  esac
  [ -f "$repository/AGENTS.md" ] \
    || { echo "plugin repository AGENTS.md is missing: $repository/AGENTS.md" >&2; status=1; }
  # Claude Code は AGENTS.md を読まず、CLAUDE.md の `@AGENTS.md` を通してだけ規約を読む。
  # clone した作業場所にも要るので、手元にあることではなく git が管理していることを求める。
  git -C "$repository" ls-files --error-unmatch CLAUDE.md >/dev/null 2>&1 \
    || { echo "plugin repository CLAUDE.md is not tracked by git: $repository/CLAUDE.md" >&2; status=1; }
  if [ -f "$repository/AGENTS.md" ]; then
    rg -Fq "$WORKSPACE/AGENTS.md" "$repository/AGENTS.md" \
      || { echo "plugin AGENTS.md does not reference workspace AGENTS.md: $repository/AGENTS.md -> $WORKSPACE/AGENTS.md" >&2; status=1; }
  fi
  if [ -f "$repository/CLAUDE.md" ]; then
    rg -Fxq '@AGENTS.md' "$repository/CLAUDE.md" \
      || { echo "plugin CLAUDE.md does not import its AGENTS.md: $repository/CLAUDE.md" >&2; status=1; }
  fi
  python3 "$TOOLS/validate-plugin-repository.py" "$repository" || status=1
done

if [ "$status" -eq 0 ]; then
  echo 'Root validation: passed'
else
  echo 'Root validation: failed'
fi
exit "$status"
