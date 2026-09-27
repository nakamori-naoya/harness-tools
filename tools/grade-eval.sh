#!/usr/bin/env bash
# claude plugin eval が残した作業場所の成果物を、別の Claude（採点役）に条件ごとに判定させ、点数にする。
# どの plugin の evals からも呼べる。引数はどれも絶対パスで受け取り、この tool の置き場やカレントディレクトリから推測しない。
#
#   bash tools/grade-eval.sh <ケースのディレクトリ> <作業場所> [期待する判定の expected.md]
#
# evals の形は、ケースのパスからの相対で決める。
#   - evals の根：ケースから親へ辿って、最初に criteria/brief.md を持つディレクトリ。採点役への指示 brief.md と、
#     資料の種類ごとの共通の条件 criteria/<種類>.md を置く。結果は <evals の根>/results/grading/ に書く。
#   - お題に共通する条件：ケースの親の criteria.md（あれば）。
#   - ケースに固有の条件：<ケース>/grading/criteria.md。先頭の注記で、共通の条件の種類（common）、判定する資料
#     （document）、作業の記録（record、無くてよい）、止まったときだけ判定する条件（when-stopped、無くてよい）を決める。
#   - 採点役に渡す材料：<ケース>/materials/ か、お題の materials/。
#
# <作業場所> は、--keep-temp で残した一時ディレクトリ（例 /private/tmp/e-XXXX）か、
# 資料と記録を作業場所と同じ形で置いたディレクトリ（較正の資料など）である。
# 採点役は Read、Glob、Grep だけを使い、写しを置いた採点用のディレクトリの中だけを読める。
# 残した作業場所の中では何も実行しない（git の設定を読ませないため）。成果物は写してから読む。
# 採点役は既定で3回、互いに独立に回し、条件ごとに多数決を取る。多数決の判定に条件の重みを掛け、
# 100点満点の点数にする。expected.md を渡すと、多数決の判定と条件ごとに突き合わせ、一致の数を出す。
#
# 環境変数: GRADE_MODEL（既定 sonnet）、GRADE_RUNS（既定 3、奇数）、GRADE_MAX_BUDGET_USD（1回あたり、既定 2）
set -euo pipefail

usage() { echo "使い方: bash tools/grade-eval.sh <ケースのディレクトリの絶対パス> <作業場所の絶対パス> [expected.md の絶対パス]" >&2; exit 2; }
[ $# -ge 2 ] && [ $# -le 3 ] || usage
for arg in "$@"; do case "$arg" in /*) ;; *) usage ;; esac; done
[ -d "$1" ] || { echo "ケースのディレクトリが無い: $1" >&2; exit 2; }
[ -d "$2" ] || { echo "作業場所が無い: $2" >&2; exit 2; }
CASE_DIR=$(cd "$1" && pwd)
KEPT=$2
EXPECTED=${3:-}
MODEL=${GRADE_MODEL:-sonnet}
RUNS=${GRADE_RUNS:-3}
BUDGET=${GRADE_MAX_BUDGET_USD:-2}
[ $((RUNS % 2)) -eq 1 ] || { echo "GRADE_RUNS は多数決が割れない奇数にする: $RUNS" >&2; exit 2; }

# 共通の条件、お題に共通する条件、ケースに固有の条件の順につなげて採点役に渡す。
# when-stopped の注記に並べた条件は、資料が無く止まったときに、報告と記録だけで判定する。
SPECIFIC="$CASE_DIR/grading/criteria.md"
[ -f "$SPECIFIC" ] || { echo "ケースに grading/criteria.md が無い: $CASE_DIR" >&2; exit 2; }
note() { sed -n "s/^<!-- $1: \(.*\) -->\$/\1/p" "$SPECIFIC" | head -1; }
KIND=$(note common); DOCUMENT=$(note document); WHEN_STOPPED=$(note when-stopped)
RECORD=$(note record)
[ -n "$KIND" ] && [ -n "$DOCUMENT" ] || { echo "grading/criteria.md の先頭に common と document の注記が無い" >&2; exit 2; }
EVALS_DIR=$(cd "$CASE_DIR" && while [ ! -f criteria/brief.md ] && [ "$PWD" != / ]; do cd ..; done; pwd)
[ "$EVALS_DIR" != / ] || { echo "ケースの親に criteria/brief.md を持つ evals の根が無い: $CASE_DIR" >&2; exit 2; }
BRIEF="$EVALS_DIR/criteria/brief.md"
COMMON="$EVALS_DIR/criteria/$KIND.md"
[ -f "$BRIEF" ] && [ -f "$COMMON" ] || { echo "共通の指示か条件が無い: $BRIEF $COMMON" >&2; exit 2; }
TOPIC="$CASE_DIR/../criteria.md"
# 採点役に渡す材料は、ケースの materials/ か、お題の materials/ にある。
if [ -d "$CASE_DIR/materials" ]; then MATERIALS="$CASE_DIR/materials"; else MATERIALS="$CASE_DIR/../materials"; fi
[ -d "$MATERIALS" ] || { echo "materials/ が無い: $CASE_DIR" >&2; exit 2; }

# 残した作業場所は封じられている（mode 000）ので、読める mode に戻してから成果物の場所を決める。
if [ -d "$KEPT/sealed" ]; then
  chmod 700 "$KEPT" "$KEPT/sealed"
  WORK="$KEPT/sealed/home/cwd"
else
  WORK=$KEPT
fi

CASE_NAME=$(basename "$CASE_DIR")
RESULTS="$EVALS_DIR/results/grading"
mkdir -p "$RESULTS"
STAMP=$(date -u +%Y-%m-%dT%H-%M-%SZ)
BASE="$RESULTS/$CASE_NAME-$STAMP"

# 条件をつなげる。資料が無いときは、止まったときに判定する条件だけを残し、それも無ければ採点役を呼ばず0点にする。
CRITERIA=$(cat "$COMMON"; [ -f "$TOPIC" ] && printf '\n---\n\n' && cat "$TOPIC"; printf '\n---\n\n'; grep -v '^<!-- ' "$SPECIFIC")
STOPPED=no
if [ ! -f "$WORK/$DOCUMENT" ]; then
  STOPPED=yes
  if [ -n "$WHEN_STOPPED" ]; then
    echo "判定する資料が無い: ${WORK}/${DOCUMENT}（止まったときに判定する条件だけで点数にする: ${WHEN_STOPPED}）" >&2
    CRITERIA=$(printf '%s\n' "$CRITERIA" | python3 -c '
import re, sys
keep = sys.argv[1].split()
sections = re.split(r"(?m)^(?=### )", sys.stdin.read())
print("".join(s for s in sections if s.startswith("### ") and s.split()[1] in keep))' "$WHEN_STOPPED")
  else
    echo "判定する資料が無い: ${WORK}/${DOCUMENT}（すべての条件を FAIL として点数にする）" >&2
    RUNS=0
  fi
fi

STAGE=$(mktemp -d "${TMPDIR:-/tmp}/grade-eval.XXXXXX")
mkdir -p "$STAGE/work" "$STAGE/materials"
# 判定する資料と作業の記録が入った最上位のディレクトリだけを写す（作業場所のほかのものは読ませない）。
for rel in "$DOCUMENT" "$RECORD"; do
  [ -n "$rel" ] || continue
  top=${rel%%/*}
  [ -e "$WORK/$top" ] && [ ! -e "$STAGE/work/$top" ] && cp -R "$WORK/$top" "$STAGE/work/$top"
done
cp -R "$MATERIALS/." "$STAGE/materials/"
# 実行の担当が最後に返した報告を、plugin eval の記録から写す（止まった理由はここにある）。
TRACE="$KEPT/out/trace.jsonl"
[ -f "$TRACE" ] && python3 -c '
import json, sys
text = ""
for line in open(sys.argv[1]):
    m = json.loads(line)
    if m.get("type") == "result":
        text = m.get("result") or ""
open(sys.argv[2], "w").write(text)' "$TRACE" "$STAGE/work/report.md"

TARGET=$(printf '## この採点の対象\n\n- 判定する資料: `work/%s`\n' "$DOCUMENT"; [ -n "$RECORD" ] && printf -- '- 作業の記録: `work/%s`\n' "$RECORD"; printf -- '- 実行の担当の最後の報告: `work/report.md`\n')
[ "$STOPPED" = yes ] && TARGET=$(printf '%s\n資料は保存されていない。実行の担当は途中で止まった。下の条件を、報告と記録だけで判定する。\n' "$TARGET")
PROMPT=$(printf '%s\n\n%s\n\n---\n\n%s\n' "$(cat "$BRIEF")" "$TARGET" "$CRITERIA")
printf '%s\n' "$CRITERIA" > "$BASE.criteria.md"

# 採点役は採点用のディレクトリを作業場所にし、そこから外は読めない。回ごとに独立に並べて走らせる。
pids=()
for i in $(seq 1 "$RUNS"); do
  (cd "$STAGE" && claude -p "$PROMPT" \
    --model "$MODEL" \
    --restricted --strict-mcp-config \
    --tools Read Glob Grep \
    --allowed-tools Read Glob Grep \
    --permission-mode dontAsk \
    --no-session-persistence \
    --max-budget-usd "$BUDGET" \
    --output-format json < /dev/null) > "$BASE.vote$i.json" &
  pids+=($!)
done
for pid in "${pids[@]}"; do wait "$pid"; done

python3 "$(dirname "${BASH_SOURCE[0]}")/grade-eval-score.py" "$BASE" "$RUNS" "$EXPECTED"
