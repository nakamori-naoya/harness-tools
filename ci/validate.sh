#!/usr/bin/env bash
# CI の共通 step。1 つの plugin repository に対して、local の検査と同じ command を同じ順で実行する。
#
#   bash ../harness-tools/ci/validate.sh /absolute/path/to/repository
#
#   1. 前提 CLI（python3 3.10+、bash、jq、rg、mikefarah/yq v4）が PATH にある
#   2. root 契約（tools/validate-plugin-repository.py）の self-test と、対象 repository への適用
#   3. 対象 repository の scripts/validate.sh（repository 固有の検査。兄弟 checkout の実配布物を使う検査を含む）
#
# workspace root の規約入口検査（AGENTS.md の絶対 path 参照）は workspace でしか成り立たないので CI では行わない。
# 1 つでも失敗すれば非 0 で終了する。fixture で兄弟 checkout を代用しない。
set -uo pipefail

TOOLS=$(cd "$(dirname "${BASH_SOURCE[0]}")/../tools" && pwd)

repository="${1:-}"
case "$repository" in
  /*) ;;
  *) echo 'usage: validate.sh /absolute/path/to/repository' >&2; exit 2 ;;
esac
[ "$#" -eq 1 ] || { echo 'usage: validate.sh /absolute/path/to/repository' >&2; exit 2; }
[ -d "$repository" ] || { echo "[error] repository が無い: $repository" >&2; exit 2; }
[ -f "$repository/scripts/validate.sh" ] || { echo "[error] scripts/validate.sh が無い: $repository/scripts/validate.sh" >&2; exit 2; }

for cmd in python3 bash jq rg yq; do
  command -v "$cmd" >/dev/null 2>&1 || { echo "[error] 前提 CLI が無い: $cmd" >&2; exit 2; }
done
python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' \
  || { echo '[error] python3 は 3.10 以上' >&2; exit 2; }
yq --version 2>/dev/null | rg -q 'mikefarah' || { echo '[error] yq は mikefarah/yq v4' >&2; exit 2; }

status=0
printf '\n=== root contract: %s ===\n' "$repository"
python3 "$TOOLS/validate-plugin-repository.py" --self-test || status=1
python3 "$TOOLS/validate-plugin-repository.py" "$repository" || status=1

printf '\n=== %s/scripts/validate.sh ===\n' "$repository"
(cd "$repository" && bash scripts/validate.sh) || status=1

if [ "$status" -eq 0 ]; then
  echo "CI validation: passed ($repository)"
else
  echo "CI validation: failed ($repository)"
fi
exit "$status"
