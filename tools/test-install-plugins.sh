#!/usr/bin/env bash
# install-plugins.shがCLI終了状態と導入後identity/version/sourceを検証することを、一時registryとstubで確かめる。
set -uo pipefail

TOOLS=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
TEST_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/install-plugins-test.XXXXXX") || exit 2
trap 'rm -rf "$TEST_ROOT"' EXIT
BIN="$TEST_ROOT/bin"
CLAUDE_TEST_CONFIG="$TEST_ROOT/claude"
CODEX_TEST_CONFIG="$TEST_ROOT/codex"
mkdir -p "$BIN" "$CLAUDE_TEST_CONFIG/plugins/marketplaces" "$CLAUDE_TEST_CONFIG/plugins/cache" \
  "$CODEX_TEST_CONFIG/.tmp/marketplaces"

CATALOG='grill|grill-plugins|grill
agent-work-policy|agent-work-policy-plugins|agent-work-policy
write-doc|write-doc-plugins|write-doc
rdb-design|rdb-design-plugins|rdb-design
product-planning|product-planning-plugins|product-planning
bdd-discovery-and-formulation|bdd-discovery-and-formulation-plugins|bdd-discovery-and-formulation
domain-modeling|domain-modeling-plugins|domain-modeling
collect-and-digest|collect-and-digest-plugins|collect-and-digest
pull-request|pull-request-plugins|pull-request
system-design|system-design-plugins|system-design
testing-strategy|testing-strategy-plugins|testing-strategy
development-convention|development-convention-plugins|development-convention
go-convention|go-convention-plugins|go-convention
react-convention|react-convention-plugins|react-convention
agent-fleet|agent-fleet-plugins|agent-fleet-core agent-fleet-herdr
agent-roles|agent-roles-plugins|agent-roles
skill-authoring|skill-authoring-plugins|skill-authoring'

claude_marketplaces='[]'
claude_installed='[]'
while IFS='|' read -r name repo plugins; do
  location="$CLAUDE_TEST_CONFIG/plugins/marketplaces/$name"
  mkdir -p "$location/.claude-plugin"
  entries='[]'
  for plugin in $plugins; do
    version=1.2.3
    entries=$(printf '%s' "$entries" | jq --arg name "$plugin" --arg version "$version" --arg path "./plugins/$plugin" '. + [{name:$name,version:$version,source:$path}]')
    install_path="$CLAUDE_TEST_CONFIG/plugins/cache/$name/$plugin/$version"
    mkdir -p "$install_path"
    claude_installed=$(printf '%s' "$claude_installed" | jq --arg id "${plugin}@${name}" --arg version "$version" --arg path "$install_path" '. + [{id:$id,version:$version,scope:"user",enabled:true,installPath:$path}]')
  done
  printf '%s\n' "$entries" | jq --arg name "$name" '{name:$name,plugins:.}' > "$location/.claude-plugin/marketplace.json"
  claude_marketplaces=$(printf '%s' "$claude_marketplaces" | jq --arg name "$name" --arg repo "test-owner/$repo" --arg location "$location" '. + [{name:$name,source:"github",repo:$repo,installLocation:$location}]')
done <<< "$CATALOG"
printf '%s\n' "$claude_marketplaces" > "$TEST_ROOT/claude-marketplaces.json"
printf '%s\n' "$claude_installed" > "$TEST_ROOT/claude-plugins.json"
jq 'map(select(.id != "grill@grill"))' "$TEST_ROOT/claude-plugins.json" > "$TEST_ROOT/claude-plugins-without-grill.json"
jq 'map(if .id == "grill@grill" then .scope = "project" else . end)' "$TEST_ROOT/claude-plugins.json" > "$TEST_ROOT/claude-plugins-project-grill.json"

codex_marketplaces='{"marketplaces":[]}'
codex_installed='{"installed":[]}'
while IFS='|' read -r name repo plugins; do
  root="$CODEX_TEST_CONFIG/.tmp/marketplaces/$name"
  source="https://github.com/test-owner/${repo}.git"
  mkdir -p "$root/.agents/plugins"
  entries='[]'
  for plugin in $plugins; do
    version=1.2.3
    source_path="./plugins/$plugin"
    plugin_path="$root/plugins/$plugin"
    entries=$(printf '%s' "$entries" | jq --arg name "$plugin" --arg version "$version" --arg path "$source_path" '. + [{name:$name,version:$version,source:{source:"local",path:$path}}]')
    codex_installed=$(printf '%s' "$codex_installed" | jq --arg id "${plugin}@${name}" --arg version "$version" --arg path "$plugin_path" --arg source "$source" '.installed += [{pluginId:$id,version:$version,installed:true,enabled:true,source:{source:"local",path:$path},marketplaceSource:{sourceType:"git",source:$source}}]')
  done
  printf '%s\n' "$entries" | jq --arg name "$name" '{name:$name,plugins:.}' > "$root/.agents/plugins/marketplace.json"
  codex_marketplaces=$(printf '%s' "$codex_marketplaces" | jq --arg name "$name" --arg root "$root" --arg source "$source" '.marketplaces += [{name:$name,root:$root,marketplaceSource:{sourceType:"git",source:$source}}]')
done <<< "$CATALOG"
printf '%s\n' "$codex_marketplaces" > "$TEST_ROOT/codex-marketplaces.json"
printf '%s\n' "$codex_installed" > "$TEST_ROOT/codex-plugins.json"
jq '.installed += [{pluginId:"github@openai-curated-remote",version:"1.0.0",installed:true,enabled:true,source:{source:"remote",id:"github"}}]' \
  "$TEST_ROOT/codex-plugins.json" > "$TEST_ROOT/codex-plugins-with-remote.json"
jq '.installed += [{pluginId:"broken@remote",version:"1.0.0",installed:true,enabled:true,source:{source:"remote"}}]' \
  "$TEST_ROOT/codex-plugins.json" > "$TEST_ROOT/codex-plugins-remote-without-id.json"
jq '.installed += [{pluginId:"broken@unknown",version:"1.0.0",installed:true,enabled:true,source:{source:"archive",id:"broken"}}]' \
  "$TEST_ROOT/codex-plugins.json" > "$TEST_ROOT/codex-plugins-unknown-source.json"

cat > "$BIN/claude" <<'STUB'
#!/usr/bin/env bash
case "$1 $2 $3" in
  'plugin marketplace list') cat "$STUB_MARKETPLACES" ;;
  'plugin marketplace update') printf '%s\n' "$*" >> "$STUB_MUTATIONS"; exit 0 ;;
  'plugin list --json')
    calls=$(cat "$STUB_LIST_CALLS" 2>/dev/null || printf '0')
    if [ "$calls" -eq 0 ]; then cat "$STUB_PLUGINS_BEFORE"; else cat "$STUB_PLUGINS_AFTER"; fi
    printf '%s\n' "$((calls + 1))" > "$STUB_LIST_CALLS"
    ;;
  'plugin install '*)
    printf '%s\n' "$*" >> "$STUB_MUTATIONS"
    [ "${3:-}" = "${STUB_FAIL_ID:-}" ] && { echo 'installed-looking text on failure: already installed'; exit 9; }
    exit 0
    ;;
  'plugin update '*) printf '%s\n' "$*" >> "$STUB_MUTATIONS"; exit 0 ;;
  'plugin marketplace add'|'plugin marketplace remove') printf '%s\n' "$*" >> "$STUB_MUTATIONS"; exit 0 ;;
  *) echo "unexpected Claude stub call: $*" >&2; exit 8 ;;
esac
STUB
chmod +x "$BIN/claude"

cat > "$BIN/codex" <<'STUB'
#!/usr/bin/env bash
case "$1 $2 $3" in
  'plugin marketplace list') cat "$STUB_CODEX_MARKETPLACES" ;;
  'plugin marketplace upgrade') printf '%s\n' "$*" >> "$STUB_MUTATIONS"; exit 0 ;;
  'plugin list --json') cat "$STUB_CODEX_PLUGINS" ;;
  'plugin add '*)
    printf '%s\n' "$*" >> "$STUB_MUTATIONS"
    [ "${3:-}" = "${STUB_CODEX_FAIL_ID:-}" ] && { echo 'installed-looking text on failure'; exit 9; }
    exit 0
    ;;
  'plugin marketplace add'|'plugin marketplace remove') printf '%s\n' "$*" >> "$STUB_MUTATIONS"; exit 0 ;;
  *) echo "unexpected Codex stub call: $*" >&2; exit 8 ;;
esac
STUB
chmod +x "$BIN/codex"

run_claude() {
  printf '0\n' > "$TEST_ROOT/claude-list-calls"
  : > "$TEST_ROOT/claude-mutations"
  env PATH="$BIN:/usr/bin:/bin" CLAUDE_CONFIG_DIR="$CLAUDE_TEST_CONFIG" \
    STUB_MARKETPLACES="$TEST_ROOT/claude-marketplaces.json" STUB_PLUGINS_BEFORE="$1" STUB_PLUGINS_AFTER="$2" \
    STUB_LIST_CALLS="$TEST_ROOT/claude-list-calls" STUB_MUTATIONS="$TEST_ROOT/claude-mutations" STUB_FAIL_ID="${3:-}" \
    bash "$TOOLS/install-plugins.sh" --runtime claude --owner test-owner
}

run_codex() {
  : > "$TEST_ROOT/codex-mutations"
  env PATH="$BIN:/usr/bin:/bin" CODEX_HOME="$CODEX_TEST_CONFIG" \
    STUB_CODEX_MARKETPLACES="$TEST_ROOT/codex-marketplaces.json" STUB_CODEX_PLUGINS="$1" \
    STUB_MUTATIONS="$TEST_ROOT/codex-mutations" STUB_CODEX_FAIL_ID="${2:-}" bash "$TOOLS/install-plugins.sh" --runtime codex --owner test-owner
}

run_claude "$TEST_ROOT/claude-plugins.json" "$TEST_ROOT/claude-plugins.json" "" >/dev/null \
  || { echo 'FAIL: valid Claude update state was rejected' >&2; exit 1; }
run_claude "$TEST_ROOT/claude-plugins-without-grill.json" "$TEST_ROOT/claude-plugins.json" "" >/dev/null \
  || { echo 'FAIL: valid Claude install transition was rejected' >&2; exit 1; }
run_claude "$TEST_ROOT/claude-plugins-project-grill.json" "$TEST_ROOT/claude-plugins.json" "" >/dev/null \
  || { echo 'FAIL: project-scope Claude entry blocked user-scope install' >&2; exit 1; }
rg -F 'plugin install grill@grill --scope user' "$TEST_ROOT/claude-mutations" >/dev/null \
  || { echo 'FAIL: project-scope Claude entry selected update instead of user install' >&2; exit 1; }
if rg -F 'plugin update grill@grill' "$TEST_ROOT/claude-mutations" >/dev/null; then
  echo 'FAIL: project-scope Claude entry was treated as user-scope registration' >&2; exit 1
fi
if run_claude "$TEST_ROOT/claude-plugins-without-grill.json" "$TEST_ROOT/claude-plugins.json" 'grill@grill' >"$TEST_ROOT/claude-failure.out" 2>&1; then
  echo 'FAIL: Claude nonzero install was accepted because its text looked successful' >&2
  exit 1
fi
rg -F 'install失敗' "$TEST_ROOT/claude-failure.out" >/dev/null \
  || { echo 'FAIL: Claude nonzero diagnosis was not preserved' >&2; exit 1; }

jq 'map(if .id == "grill@grill" then .version = "9.9.9" else . end)' "$TEST_ROOT/claude-plugins.json" > "$TEST_ROOT/claude-version-tampered.json"
if run_claude "$TEST_ROOT/claude-plugins.json" "$TEST_ROOT/claude-version-tampered.json" "" >"$TEST_ROOT/claude-version.out" 2>&1; then
  echo 'FAIL: Claude post-install version mismatch was accepted' >&2
  exit 1
fi
rg -F '登録状態不一致' "$TEST_ROOT/claude-version.out" >/dev/null \
  || { echo 'FAIL: Claude version mismatch diagnosis was not preserved' >&2; exit 1; }

jq 'map(if .id == "grill@grill" then .installPath = "/wrong/source" else . end)' "$TEST_ROOT/claude-plugins.json" > "$TEST_ROOT/claude-source-tampered.json"
if run_claude "$TEST_ROOT/claude-plugins.json" "$TEST_ROOT/claude-source-tampered.json" "" >"$TEST_ROOT/claude-source.out" 2>&1; then
  echo 'FAIL: Claude post-install source path mismatch was accepted' >&2
  exit 1
fi
rg -F '登録状態不一致' "$TEST_ROOT/claude-source.out" >/dev/null \
  || { echo 'FAIL: Claude source mismatch diagnosis was not preserved' >&2; exit 1; }

printf '{invalid' > "$TEST_ROOT/invalid-json"
printf '{}\n' > "$TEST_ROOT/wrong-claude-type.json"
printf '42\n[]\n' > "$TEST_ROOT/multi-claude-plugins.json"
for bad in "$TEST_ROOT/invalid-json" "$TEST_ROOT/wrong-claude-type.json" "$TEST_ROOT/multi-claude-plugins.json"; do
  if run_claude "$bad" "$TEST_ROOT/claude-plugins.json" "" >"$TEST_ROOT/claude-schema.out" 2>&1; then
    echo 'FAIL: invalid Claude initial plugin list was accepted' >&2; exit 1
  fi
  [ ! -s "$TEST_ROOT/claude-mutations" ] || { echo 'FAIL: Claude mutated state after invalid initial list' >&2; exit 1; }
done
cp "$TEST_ROOT/claude-marketplaces.json" "$TEST_ROOT/claude-marketplaces-valid.json"
printf '{}\n' > "$TEST_ROOT/wrong-claude-marketplaces.json"
printf '42\n[]\n' > "$TEST_ROOT/multi-claude-marketplaces.json"
for bad in "$TEST_ROOT/wrong-claude-marketplaces.json" "$TEST_ROOT/multi-claude-marketplaces.json"; do
  cp "$bad" "$TEST_ROOT/claude-marketplaces.json"
  if run_claude "$TEST_ROOT/claude-plugins.json" "$TEST_ROOT/claude-plugins.json" "" >"$TEST_ROOT/claude-market-schema.out" 2>&1; then
    echo 'FAIL: invalid Claude initial marketplace list was accepted' >&2; exit 1
  fi
  [ ! -s "$TEST_ROOT/claude-mutations" ] || { echo 'FAIL: Claude mutated state after invalid marketplace list' >&2; exit 1; }
done
mv "$TEST_ROOT/claude-marketplaces-valid.json" "$TEST_ROOT/claude-marketplaces.json"

jq 'map(if .id == "grill@grill" then .scope = "project" else . end)' "$TEST_ROOT/claude-plugins.json" > "$TEST_ROOT/claude-post-project.json"
if run_claude "$TEST_ROOT/claude-plugins.json" "$TEST_ROOT/claude-post-project.json" "" >"$TEST_ROOT/claude-scope.out" 2>&1; then
  echo 'FAIL: Claude project scope was accepted as user scope post-state' >&2; exit 1
fi
rg -F '登録状態不一致' "$TEST_ROOT/claude-scope.out" >/dev/null \
  || { echo 'FAIL: Claude scope mismatch diagnosis was not preserved' >&2; exit 1; }

run_codex "$TEST_ROOT/codex-plugins.json" "" >/dev/null \
  || { echo 'FAIL: valid Codex registry was rejected' >&2; exit 1; }
run_codex "$TEST_ROOT/codex-plugins-with-remote.json" "" >/dev/null \
  || { echo 'FAIL: legal remote Codex entry blocked local plugin installation' >&2; exit 1; }
rg -F 'plugin add grill@grill' "$TEST_ROOT/codex-mutations" >/dev/null \
  || { echo 'FAIL: mixed local/remote Codex registry stopped before target mutation' >&2; exit 1; }
for bad in "$TEST_ROOT/codex-plugins-remote-without-id.json" "$TEST_ROOT/codex-plugins-unknown-source.json"; do
  if run_codex "$bad" "" >"$TEST_ROOT/codex-source-kind.out" 2>&1; then
    echo 'FAIL: invalid Codex source-specific shape was accepted' >&2; exit 1
  fi
  [ ! -s "$TEST_ROOT/codex-mutations" ] || { echo 'FAIL: Codex mutated state after invalid source-specific shape' >&2; exit 1; }
done
if run_codex "$TEST_ROOT/codex-plugins.json" 'grill@grill' >"$TEST_ROOT/codex-failure.out" 2>&1; then
  echo 'FAIL: Codex nonzero install was accepted' >&2
  exit 1
fi
rg -F 'install失敗' "$TEST_ROOT/codex-failure.out" >/dev/null \
  || { echo 'FAIL: Codex nonzero diagnosis was not preserved' >&2; exit 1; }

jq '.installed |= map(if .pluginId == "grill@grill" then .marketplaceSource.source = "https://github.com/wrong/source.git" else . end)' "$TEST_ROOT/codex-plugins.json" > "$TEST_ROOT/codex-source-tampered.json"
if run_codex "$TEST_ROOT/codex-source-tampered.json" "" >"$TEST_ROOT/codex-source.out" 2>&1; then
  echo 'FAIL: Codex post-install source mismatch was accepted' >&2
  exit 1
fi
rg -F '登録状態不一致' "$TEST_ROOT/codex-source.out" >/dev/null \
  || { echo 'FAIL: Codex source mismatch diagnosis was not preserved' >&2; exit 1; }

printf '42\n{"installed":[]}\n' > "$TEST_ROOT/multi-codex-plugins.json"
for bad in "$TEST_ROOT/invalid-json" "$TEST_ROOT/wrong-claude-type.json" "$TEST_ROOT/multi-codex-plugins.json"; do
  if run_codex "$bad" "" >"$TEST_ROOT/codex-schema.out" 2>&1; then
    echo 'FAIL: invalid Codex initial plugin list was accepted' >&2; exit 1
  fi
  [ ! -s "$TEST_ROOT/codex-mutations" ] || { echo 'FAIL: Codex mutated state after invalid initial plugin list' >&2; exit 1; }
done
cp "$TEST_ROOT/codex-marketplaces.json" "$TEST_ROOT/codex-marketplaces-valid.json"
printf '[]\n' > "$TEST_ROOT/wrong-codex-marketplaces.json"
printf '42\n{"marketplaces":[]}\n' > "$TEST_ROOT/multi-codex-marketplaces.json"
for bad in "$TEST_ROOT/wrong-codex-marketplaces.json" "$TEST_ROOT/multi-codex-marketplaces.json"; do
  cp "$bad" "$TEST_ROOT/codex-marketplaces.json"
  if run_codex "$TEST_ROOT/codex-plugins.json" "" >"$TEST_ROOT/codex-market-schema.out" 2>&1; then
    echo 'FAIL: invalid Codex initial marketplace list was accepted' >&2; exit 1
  fi
  [ ! -s "$TEST_ROOT/codex-mutations" ] || { echo 'FAIL: Codex mutated state after invalid marketplace list' >&2; exit 1; }
done
mv "$TEST_ROOT/codex-marketplaces-valid.json" "$TEST_ROOT/codex-marketplaces.json"

echo 'Install contract self-test: passed (single-value initial JSON/schema fail-closed with zero mutations, mixed Codex local/remote entries, Claude user scope, CLI nonzero, post-state identity/version/source/scope mismatch)'
