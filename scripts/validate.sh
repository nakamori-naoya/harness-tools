#!/usr/bin/env bash
# harness-tools 自身の自己検査。各 tool の self-test / unittest と、入口 script の引数契約（負例）を実行する。
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

section 'scripts/test-run-evals.sh'
bash "$ROOT/scripts/test-run-evals.sh" && pass 'run-evals 自己検査' || fail 'run-evals 自己検査'

section '入口 script の引数契約（負例）'
python3 "$ROOT/tools/doctor.py" >/dev/null 2>&1; [ "$?" -eq 2 ] && pass 'doctor.py は --repository 必須' || fail 'doctor.py が --repository 無しで動いた'
python3 "$ROOT/tools/lint-consumer-contract.py" >/dev/null 2>&1; [ "$?" -eq 2 ] && pass 'lint-consumer-contract.py は --repo 必須' || fail 'lint が --repo 無しで動いた'
python3 "$ROOT/tools/release.py" --plugin x --version 1.0.0 --notes n --breaking b --migration m --checks /dev/null >/dev/null 2>&1; [ "$?" -eq 2 ] && pass 'release.py は --repo 必須' || fail 'release.py が --repo 無しで動いた'
bash "$ROOT/tools/validate-workspace.sh" >/dev/null 2>&1; [ "$?" -eq 2 ] && pass 'validate-workspace.sh は --workspace 必須' || fail 'validate-workspace.sh が --workspace 無しで動いた'
bash "$ROOT/tools/validate-workspace.sh" --workspace "$TMP_ROOT/none" /x >/dev/null 2>&1; [ "$?" -eq 2 ] && pass 'validate-workspace.sh は存在しない workspace を拒否' || fail 'validate-workspace.sh が存在しない workspace で動いた'
bash "$ROOT/ci/validate.sh" >/dev/null 2>&1; [ "$?" -eq 2 ] && pass 'ci/validate.sh は絶対 path 必須' || fail 'ci/validate.sh が引数無しで動いた'
bash "$ROOT/ci/validate.sh" "$TMP_ROOT/missing" >/dev/null 2>&1; [ "$?" -eq 2 ] && pass 'ci/validate.sh は存在しない repository を拒否' || fail 'ci/validate.sh が存在しない repository で動いた'
mkdir -p "$TMP_ROOT/no-validate"
bash "$ROOT/ci/validate.sh" "$TMP_ROOT/no-validate" >/dev/null 2>&1; [ "$?" -eq 2 ] && pass 'ci/validate.sh は scripts/validate.sh の無い repository を拒否' || fail 'ci/validate.sh が validate.sh 無しで動いた'

section 'ci/validate.sh の正例（最小の plugin repository）'
fixture="$TMP_ROOT/fixture-plugins"
mkdir -p "$fixture/.claude-plugin" "$fixture/.agents/plugins" "$fixture/plugins/fixture/.claude-plugin" "$fixture/plugins/fixture/.codex-plugin" "$fixture/plugins/fixture/skills/do-work" "$fixture/scripts"
printf '{"name":"fixture","plugins":[{"name":"fixture","version":"1.0.0","source":"./plugins/fixture"}]}\n' > "$fixture/.claude-plugin/marketplace.json"
printf '{"name":"fixture","plugins":[{"name":"fixture","version":"1.0.0","source":{"source":"local","path":"./plugins/fixture"}}]}\n' > "$fixture/.agents/plugins/marketplace.json"
for runtime in claude codex; do
  printf '{"name":"fixture","version":"1.0.0","skills":["./skills/do-work"],"metadata":{"harness":{"marketplace":"fixture","contractVersion":1}}}\n' > "$fixture/plugins/fixture/.$runtime-plugin/plugin.json"
done
printf -- '---\nname: do-work\ndescription: fixture\n---\nfixture\n' > "$fixture/plugins/fixture/skills/do-work/SKILL.md"
printf '#!/usr/bin/env bash\necho fixture validate.sh\n' > "$fixture/scripts/validate.sh"
bash "$ROOT/ci/validate.sh" "$fixture" >"$TMP_ROOT/ci-ok.out" 2>&1 && pass 'ci/validate.sh 正例は exit 0' || { cat "$TMP_ROOT/ci-ok.out" >&2; fail 'ci/validate.sh 正例'; }
printf '#!/usr/bin/env bash\nexit 1\n' > "$fixture/scripts/validate.sh"
bash "$ROOT/ci/validate.sh" "$fixture" >/dev/null 2>&1 && fail 'repository の validate.sh 失敗が exit 0 になった' || pass 'repository の validate.sh 失敗は非 0'

if [ "$failed" -eq 0 ]; then echo 'harness-tools validation: passed'; else echo 'harness-tools validation: failed' >&2; fi
exit "$failed"
