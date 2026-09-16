#!/usr/bin/env bash
# 各 repository の evals/scenarios.json を tools/evaluate-skills.py（生成 model ＋ 独立 judge）で実行し、
# <repository>/evals/runs/<YYYY-MM-DD>/<label>.json へ記録する。合否 gate ではない。
#
#   bash scripts/run-evals.sh --model <model id> --judge-model <model id> [--effort low|medium|high] [--label <name>] \
#     /absolute/path/to/repository [...]
#
# - 生成と judge は tools/claude-eval-adapter.py（`claude --print`）で呼ぶ。資格情報は実行者の環境のもの。
#   local で手動実行する。CI には置かない。
# - exit 0 は「全 repository で全 case の記録が書けた」ことだけを示す。criterion の pass/fail は記録の中の
#   意味証拠であり、前回の記録（同 directory の過去日付）と agent が読み比べて判断する。
# - 記録を上書きしない。同じ日に再実行するときは --label で別名を付ける（例: --label after-fix）。
# - repository が無い、evals/scenarios.json が無い、adapter が失敗した、いずれも非 0 で終了する。
set -uo pipefail

TOOLS=$(cd "$(dirname "${BASH_SOURCE[0]}")/../tools" && pwd)
ADAPTER="$TOOLS/claude-eval-adapter.py"

usage() {
  sed -n 2,13p "${BASH_SOURCE[0]}" >&2
  exit 2
}

MODEL='' JUDGE='' EFFORT=high LABEL=evaluation
repositories=()
while [ $# -gt 0 ]; do
  case "$1" in
    --model) [ "$#" -ge 2 ] || usage; MODEL="$2"; shift 2 ;;
    --judge-model) [ "$#" -ge 2 ] || usage; JUDGE="$2"; shift 2 ;;
    --effort) [ "$#" -ge 2 ] || usage; EFFORT="$2"; shift 2 ;;
    --label) [ "$#" -ge 2 ] || usage; LABEL="$2"; shift 2 ;;
    -h|--help) usage ;;
    --*) echo "[error] 未知の引数: $1" >&2; usage ;;
    *) repositories+=("$1"); shift ;;
  esac
done
[ -n "$MODEL" ] && [ -n "$JUDGE" ] || { echo '[error] --model と --judge-model は必須' >&2; usage; }
[ "$MODEL" != "$JUDGE" ] || { echo '[error] 生成 model と judge model は別にする' >&2; exit 2; }
case "$EFFORT" in low|medium|high) ;; *) echo "[error] --effort は low / medium / high: $EFFORT" >&2; exit 2 ;; esac
case "$LABEL" in ''|*/*|*[!A-Za-z0-9._-]*) echo "[error] --label は [A-Za-z0-9._-] だけ: $LABEL" >&2; exit 2 ;; esac
[ "${#repositories[@]}" -gt 0 ] || { echo '[error] repository の絶対 path を 1 つ以上渡す' >&2; usage; }

DATE=$(date +%F)
COMMAND=$(jq -cn --arg adapter "$ADAPTER" '["python3", $adapter]')
SETTINGS=$(jq -cn --arg effort "$EFFORT" '{effort: $effort}')

failed=0
for repository in "${repositories[@]}"; do
  case "$repository" in /*) ;; *) echo "[error] 絶対 path で渡す: $repository" >&2; failed=1; continue ;; esac
  [ -d "$repository" ] || { echo "[error] repository が無い: $repository" >&2; failed=1; continue; }
  fixtures="$repository/evals/scenarios.json"
  [ -f "$fixtures" ] || { echo "[error] evals/scenarios.json が無い: $fixtures" >&2; failed=1; continue; }
  output_dir="$repository/evals/runs/$DATE"
  output="$output_dir/$LABEL.json"
  [ ! -e "$output" ] || { echo "[error] 記録が既にある。--label で別名を付ける: $output" >&2; failed=1; continue; }
  mkdir -p "$output_dir" || { failed=1; continue; }
  printf '\n=== %s → %s ===\n' "$fixtures" "$output"
  if python3 "$TOOLS/evaluate-skills.py" --fixtures "$fixtures" \
      --model-command "$COMMAND" --judge-command "$COMMAND" \
      --model "$MODEL" --judge-model "$JUDGE" --settings "$SETTINGS" --output "$output"; then
    echo "recorded: $output"
  else
    echo "[error] 記録を完了できなかった case がある（記録内の error を読む）: $output" >&2
    failed=1
  fi
done

if [ "$failed" -eq 0 ]; then
  echo 'run-evals: all records written（criterion の判定は記録を読んで意味評価する）'
else
  echo 'run-evals: failed' >&2
fi
exit "$failed"
