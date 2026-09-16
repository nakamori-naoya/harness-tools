#!/usr/bin/env bash
# run-evals.sh の自己検査。存在しない repository、evals/scenarios.json の欠落、adapter（claude CLI）の失敗、
# 既存記録の保護、引数の不備が、いずれも非 0 で観測できることを stub の claude と一時 repository で確かめる。
# 実 model は呼ばない。
set -uo pipefail

SCRIPTS=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
RUNNER="$SCRIPTS/run-evals.sh"
TEST_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/run-evals-test.XXXXXX") || exit 2
trap 'rm -rf "$TEST_ROOT"' EXIT
BIN="$TEST_ROOT/bin"; mkdir -p "$BIN"
failed=0
pass() { printf 'PASS: %s\n' "$1"; }
fail() { printf 'FAIL: %s\n' "$1" >&2; failed=1; }

# claude の stub。STUB_MODE=ok なら generator / judge の両方に整合する JSON を返し、fail なら非 0 で落ちる。
cat > "$BIN/claude" <<'STUB'
#!/usr/bin/env bash
[ "${STUB_MODE:-}" = ok ] || { echo 'stub: model unavailable' >&2; exit 9; }
request=$(cat)
model=$(printf '%s' "$request" | jq -r '.model')
if printf '%s' "$request" | jq -e 'has("candidate_output")' >/dev/null; then
  result='{"criteria":[{"id":"meaning","pass":true,"quote":"actual answer","reason":"stub assessment"}]}'
else
  result='actual answer'
fi
jq -cn --arg model "$model" --arg result "$result" \
  '{is_error:false, result:$result, usage:{}, modelUsage:{($model):{outputTokens:1}}}'
STUB
chmod +x "$BIN/claude"

repo="$TEST_ROOT/repo"; mkdir -p "$repo/evals" "$repo/plugins/p/skills/s"
printf -- '---\nname: s\ndescription: fixture\n---\nDo the job.\n' > "$repo/plugins/p/skills/s/SKILL.md"
jq -n '{schema:1, cases:[{id:"case", skill:"../plugins/p/skills/s/SKILL.md", messages:[{role:"user",content:"request"}],
        criteria:[{id:"meaning", meaning:"a meaningful answer"}]}]}' > "$repo/evals/scenarios.json"

run() { env PATH="$BIN:$PATH" STUB_MODE="$1" bash "$RUNNER" --model generator --judge-model judge "${@:2}"; }

run ok >/dev/null 2>&1 && fail 'repository 無しが受理された' || pass 'repository 無しは exit 2'
run ok --model x >/dev/null 2>&1 && fail '引数不備が受理された' || pass '引数不備は非 0'
run ok "$TEST_ROOT/missing" >/dev/null 2>&1 && fail '存在しない repository が受理された' || pass '存在しない repository は非 0'
run ok relative/path >/dev/null 2>&1 && fail '相対 path が受理された' || pass '相対 path は非 0'
mkdir -p "$TEST_ROOT/no-fixtures"
run ok "$TEST_ROOT/no-fixtures" >/dev/null 2>&1 && fail 'scenarios.json 欠落が受理された' || pass 'scenarios.json 欠落は非 0'

if run fail "$repo" >"$TEST_ROOT/adapter-fail.out" 2>&1; then
  fail 'adapter 失敗が exit 0 になった'
else
  record="$repo/evals/runs/$(date +%F)/evaluation.json"
  if [ -f "$record" ] && jq -e '.records[0].status == "error" and (.records[0].error | test("adapter failed"))' "$record" >/dev/null; then
    pass 'adapter 失敗は非 0 で、記録に error が残る'
  else
    fail 'adapter 失敗の記録が無い、または error が残っていない'
  fi
fi

run ok "$repo" >/dev/null 2>&1 && fail '既存記録が上書きされた' || pass '既存記録は上書きしない（--label が要る）'

if run ok --label second "$repo" >"$TEST_ROOT/ok.out" 2>&1; then
  record="$repo/evals/runs/$(date +%F)/second.json"
  if jq -e '.schema == 2 and .records[0].status == "recorded" and (.model_command[1] | startswith("/") and endswith("/tools/claude-eval-adapter.py"))' "$record" >/dev/null; then
    pass '全 case 記録で exit 0（adapter は harness-tools の絶対 path）'
  else
    fail '記録の schema / status / adapter path が期待と違う'
  fi
else
  cat "$TEST_ROOT/ok.out" >&2
  fail '正常系が非 0'
fi

[ "$failed" -eq 0 ] && echo 'run-evals self-test: passed' || { echo 'run-evals self-test: failed' >&2; exit 1; }
