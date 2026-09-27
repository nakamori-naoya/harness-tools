#!/usr/bin/env bash
# harness-tools 自身の自己検査。各 tool の self-test と、入口 script の引数契約（負例）を実行する。
# plugin repository は検査しない（それは tools/validate-plugin-repository.py と各 repository の scripts/validate.sh の仕事）。
#
#   bash scripts/validate.sh
set -uo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
TMP_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/harness-tools-validation.XXXXXX") || exit 2
trap 'rm -rf "$TMP_ROOT"' EXIT
export PYTHONDONTWRITEBYTECODE=1
failed=0
pass() { printf 'PASS: %s\n' "$1"; }
fail() { printf 'FAIL: %s\n' "$1" >&2; failed=1; }
section() { printf '\n=== %s ===\n' "$1"; }

section '前提 CLI'
for cmd in python3 bash jq rg yq; do
  command -v "$cmd" >/dev/null 2>&1 && pass "command $cmd" || fail "command $cmd が無い"
done
yq --version 2>/dev/null | rg -q 'mikefarah' && pass 'yq は mikefarah v4' || fail 'yq は mikefarah/yq v4 が要る'

section '構文'
while IFS= read -r script; do bash -n "$script" && pass "bash -n ${script#"$ROOT"/}" || fail "bash -n ${script#"$ROOT"/}"; done \
  < <(find "$ROOT/tools" "$ROOT/ci" "$ROOT/scripts" -type f -name '*.sh' | sort)
while IFS= read -r script; do
  PYTHONPYCACHEPREFIX="$TMP_ROOT/pycache" python3 -m py_compile "$script" && pass "py_compile ${script#"$ROOT"/}" || fail "py_compile ${script#"$ROOT"/}"
done < <(find "$ROOT/tools" -type f -name '*.py' | sort)

section 'harness-tools は plugin package ではない'
if [ ! -e "$ROOT/.claude-plugin" ] && [ ! -e "$ROOT/.agents/plugins" ] && [ ! -e "$ROOT/.codex-plugin" ] && [ ! -e "$ROOT/plugins" ]; then
  pass 'marketplace / manifest / plugins を持たない'
else
  fail 'marketplace / manifest / plugins を持っている'
fi
[ "$(find "$ROOT" -type l -not -path '*/.git/*' | wc -l | tr -d ' ')" -eq 0 ] && pass 'symlink なし' || fail 'symlink がある'

section 'CI workflow の action は commit SHA で固定'
actions=$(rg -o 'uses:\s+\S+' "$ROOT/.github/workflows/validate.yml" | sed 's/uses:[[:space:]]*//')
if [ -n "$actions" ] && ! printf '%s\n' "$actions" | rg -v -q '^[A-Za-z0-9_-]+/[A-Za-z0-9_-]+@[0-9a-f]{40}$'; then
  pass "actions: $(printf '%s' "$actions" | wc -l | tr -d ' ') 件すべて SHA 固定"
else
  fail 'SHA 固定でない action がある'
fi

section 'tools/validate-plugin-repository.py --self-test'
python3 "$ROOT/tools/validate-plugin-repository.py" --self-test && pass 'root 契約 self-test' || fail 'root 契約 self-test'

section 'tools/test-hardening.py（tool の自己検査。--repository 無し）'
python3 "$ROOT/tools/test-hardening.py" 2>"$TMP_ROOT/hardening.err" && pass 'test-hardening' || { cat "$TMP_ROOT/hardening.err" >&2; fail 'test-hardening'; }

section 'tools/test-install-plugins.sh'
bash "$ROOT/tools/test-install-plugins.sh" && pass 'install-plugins 契約' || fail 'install-plugins 契約'

section '入口 script の引数契約（負例）'
python3 "$ROOT/tools/release.py" --plugin x --version 1.0.0 >/dev/null 2>&1; [ "$?" -eq 2 ] && pass 'release.py は --repo 必須' || fail 'release.py が --repo 無しで動いた'
bash "$ROOT/tools/validate-workspace.sh" >/dev/null 2>&1; [ "$?" -eq 2 ] && pass 'validate-workspace.sh は --workspace 必須' || fail 'validate-workspace.sh が --workspace 無しで動いた'
bash "$ROOT/tools/validate-workspace.sh" --workspace "$TMP_ROOT/none" /x >/dev/null 2>&1; [ "$?" -eq 2 ] && pass 'validate-workspace.sh は存在しない workspace を拒否' || fail 'validate-workspace.sh が存在しない workspace で動いた'
bash "$ROOT/ci/validate.sh" >/dev/null 2>&1; [ "$?" -eq 2 ] && pass 'ci/validate.sh は絶対 path 必須' || fail 'ci/validate.sh が引数無しで動いた'
bash "$ROOT/ci/validate.sh" "$TMP_ROOT/missing" >/dev/null 2>&1; [ "$?" -eq 2 ] && pass 'ci/validate.sh は存在しない repository を拒否' || fail 'ci/validate.sh が存在しない repository で動いた'
mkdir -p "$TMP_ROOT/no-validate"
bash "$ROOT/ci/validate.sh" "$TMP_ROOT/no-validate" >/dev/null 2>&1; [ "$?" -eq 2 ] && pass 'ci/validate.sh は scripts/validate.sh の無い repository を拒否' || fail 'ci/validate.sh が validate.sh 無しで動いた'

bash "$ROOT/tools/grade-eval.sh" >/dev/null 2>&1; [ "$?" -eq 2 ] && pass 'grade-eval.sh は引数必須' || fail 'grade-eval.sh が引数無しで動いた'
bash "$ROOT/tools/grade-eval.sh" relative/case relative/work >/dev/null 2>&1; [ "$?" -eq 2 ] && pass 'grade-eval.sh は絶対パス必須' || fail 'grade-eval.sh が相対パスで動いた'
mkdir -p "$TMP_ROOT/grade/case/grading" "$TMP_ROOT/grade/work"
bash "$ROOT/tools/grade-eval.sh" "$TMP_ROOT/grade/case" "$TMP_ROOT/grade/work" >/dev/null 2>&1; [ "$?" -eq 2 ] && pass 'grade-eval.sh は固有の条件の無いケースを拒否' || fail 'grade-eval.sh が固有の条件無しで動いた'
GRADE_ENGINE=other bash "$ROOT/tools/grade-eval.sh" "$TMP_ROOT/grade/case" "$TMP_ROOT/grade/work" >/dev/null 2>&1; [ "$?" -eq 2 ] && pass 'grade-eval.sh は知らない採点役を拒否' || fail 'grade-eval.sh が知らない採点役で動いた'

section 'tools/grade-eval.sh の正例（偽の採点役で、同時の採点と失敗した回）'
g="$TMP_ROOT/grade-ok"
mkdir -p "$g/evals/criteria" "$g/evals/topic/case/grading" "$g/evals/topic/materials" "$g/work/out" "$g/bin"
printf '# 指示\n' > "$g/evals/criteria/brief.md"
printf '# 共通\n\n### only\n\n重み: 1\n\nPASS：x\n\nFAIL：y\n' > "$g/evals/criteria/kind.md"
printf '<!-- common: kind -->\n<!-- document: out/doc.md -->\n\n# 固有\n' > "$g/evals/topic/case/grading/criteria.md"
printf 'doc\n' > "$g/work/out/doc.md"
printf 'req\n' > "$g/evals/topic/materials/req.md"
cat > "$g/bin/claude" <<'SHIM'
#!/usr/bin/env bash
if [ -n "${SHIM_FAIL_DIR:-}" ] && mkdir "$SHIM_FAIL_DIR" 2>/dev/null; then exit 1; fi
printf '{"result":"### only\\n判定: PASS\\n根拠: x\\n理由: y\\n","total_cost_usd":0}\n'
SHIM
chmod +x "$g/bin/claude"
( PATH="$g/bin:$PATH" SHIM_FAIL_DIR="$g/failed-once" TMPDIR="$TMP_ROOT" bash "$ROOT/tools/grade-eval.sh" "$g/evals/topic/case" "$g/work" >"$g/a.out" 2>&1 ) &
( PATH="$g/bin:$PATH" TMPDIR="$TMP_ROOT" bash "$ROOT/tools/grade-eval.sh" "$g/evals/topic/case" "$g/work" >"$g/b.out" 2>&1 ) &
wait
rg -q '点数: 100.0' "$g/a.out" && rg -q '失敗 1 回' "$g/a.out" && pass 'grade-eval.sh は失敗した回があっても点数を出す' || { cat "$g/a.out" >&2; fail 'grade-eval.sh が失敗した回で点数を出さなかった'; }
rg -q '点数: 100.0' "$g/b.out" && rg -q '失敗 0 回' "$g/b.out" && pass 'grade-eval.sh は同時に採点しても結果が混ざらない' || { cat "$g/b.out" >&2; fail 'grade-eval.sh の同時の採点が混ざった'; }

section 'ci/validate.sh の正例（最小の plugin repository）'
fixture="$TMP_ROOT/fixture-plugins"
mkdir -p "$fixture/.claude-plugin" "$fixture/.agents/plugins" "$fixture/plugins/fixture/.claude-plugin" "$fixture/plugins/fixture/.codex-plugin" "$fixture/plugins/fixture/skills/do-work" "$fixture/scripts"
printf '{"name":"fixture","plugins":[{"name":"fixture","version":"1.0.0","source":"./plugins/fixture"}]}\n' > "$fixture/.claude-plugin/marketplace.json"
printf '{"name":"fixture","plugins":[{"name":"fixture","version":"1.0.0","source":{"source":"local","path":"./plugins/fixture"}}]}\n' > "$fixture/.agents/plugins/marketplace.json"
for runtime in claude codex; do
  printf '{"name":"fixture","version":"1.0.0","skills":["./skills/do-work"],"metadata":{"harness":{"marketplace":"fixture"}}}\n' > "$fixture/plugins/fixture/.$runtime-plugin/plugin.json"
done
printf -- '---\nname: do-work\ndescription: fixture\n---\nfixture\n' > "$fixture/plugins/fixture/skills/do-work/SKILL.md"
printf '#!/usr/bin/env bash\necho fixture validate.sh\n' > "$fixture/scripts/validate.sh"
bash "$ROOT/ci/validate.sh" "$fixture" >"$TMP_ROOT/ci-ok.out" 2>&1 && pass 'ci/validate.sh 正例は exit 0' || { cat "$TMP_ROOT/ci-ok.out" >&2; fail 'ci/validate.sh 正例'; }
printf '#!/usr/bin/env bash\nexit 1\n' > "$fixture/scripts/validate.sh"
bash "$ROOT/ci/validate.sh" "$fixture" >/dev/null 2>&1 && fail 'repository の validate.sh 失敗が exit 0 になった' || pass 'repository の validate.sh 失敗は非 0'

if [ "$failed" -eq 0 ]; then echo 'harness-tools validation: passed'; else echo 'harness-tools validation: failed' >&2; fi
exit "$failed"
