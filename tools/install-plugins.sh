#!/usr/bin/env bash
# harness-pluginsv2 の plugin を、GitHub の marketplace 経由で Claude Code と Codex に導入・更新する。
#
#   bash install-plugins.sh [--runtime claude|codex|both] [--owner <github owner>]
#
# 正本はこの file（harness-tools/tools/install-plugins.sh）。workspace root の install-plugins.sh はここへ委譲する。
#
# 既定は両 runtime。導入先は personal 設定（Claude: $CLAUDE_CONFIG_DIR、Codex: $CODEX_HOME）で、
# 未設定なら ~/.claude-personal と ~/.codex-personal を使う。
# ローカル path で登録済みの marketplace は GitHub 登録へ置き換える。何度実行しても同じ結果になる。
set -uo pipefail

RUNTIME=both
OWNER=nakamori-naoya
while [ $# -gt 0 ]; do
  case "$1" in
    --runtime) [ "$#" -ge 2 ] || { echo "[error] --runtime に値が無い" >&2; exit 2; }; RUNTIME="$2"; shift 2 ;;
    --owner) [ "$#" -ge 2 ] || { echo "[error] --owner に値が無い" >&2; exit 2; }; OWNER="$2"; shift 2 ;;
    -h|--help) sed -n 2,8p "$0"; exit 0 ;;
    *) echo "[error] 未知の引数: $1" >&2; exit 2 ;;
  esac
done
case "$RUNTIME" in claude|codex|both) ;; *) echo "[error] --runtime は claude / codex / both" >&2; exit 2 ;; esac
case "$OWNER" in ''|*/*|*[!A-Za-z0-9_.-]*) echo "[error] --owner が不正" >&2; exit 2 ;; esac

export CLAUDE_CONFIG_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude-personal}"
export CODEX_HOME="${CODEX_HOME:-$HOME/.codex-personal}"

# marketplace 名 | GitHub repository | その marketplace が公開する plugin（空白区切り）
MARKETPLACES='
grill|grill-plugins|grill
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
agent-fleet|agent-fleet-plugins|agent-fleet-core agent-fleet-herdr
agent-roles|agent-roles-plugins|agent-roles
skill-authoring|skill-authoring-plugins|skill-authoring
'

fail=0
note() { printf '%s\n' "$*"; }

validate_claude_marketplaces() {
  printf '%s' "$1" | jq -se '
    length == 1 and (.[0] |
    type == "array" and
    all(.[]; type == "object" and (.name | type == "string") and (.name | length > 0)
      and (.source | type == "string")
      and (if .source == "github" then (.repo | type == "string") and (.installLocation | type == "string") else true end)))
  ' >/dev/null
}

validate_claude_plugins() {
  printf '%s' "$1" | jq -se '
    length == 1 and (.[0] | type == "array" and
    all(.[]; type == "object" and (.id | type == "string") and (.scope | type == "string")
      and (.version | type == "string") and (.enabled | type == "boolean") and (.installPath | type == "string")))
  ' >/dev/null
}

validate_codex_marketplaces() {
  printf '%s' "$1" | jq -se '
    length == 1 and (.[0] | type == "object" and (.marketplaces | type == "array") and
    all(.marketplaces[]; type == "object" and (.name | type == "string") and (.root | type == "string")
      and (.marketplaceSource | type == "object") and (.marketplaceSource.sourceType | type == "string")
      and (.marketplaceSource.source | type == "string")))
  ' >/dev/null
}

validate_codex_plugins() {
  printf '%s' "$1" | jq -se '
    length == 1 and (.[0] | type == "object" and (.installed | type == "array") and
    all(.installed[]; type == "object" and (.pluginId | type == "string") and (.pluginId | length > 0)
      and (.version | type == "string") and (.version | length > 0)
      and (.installed | type == "boolean") and (.enabled | type == "boolean")
      and (.source | type == "object") and (.source.source | type == "string")
      and (if .source.source == "local" then
        (.source.path | type == "string") and (.source.path | length > 0)
        and (.marketplaceSource | type == "object")
        and (.marketplaceSource.sourceType | type == "string")
        and (.marketplaceSource.source | type == "string")
      elif .source.source == "remote" then
        (.source.id | type == "string") and (.source.id | length > 0)
      else false end)))
  ' >/dev/null
}

install_claude() {
  command -v claude >/dev/null 2>&1 || { echo "[error] claude が無い" >&2; fail=1; return; }
  note "== Claude Code (CLAUDE_CONFIG_DIR=$CLAUDE_CONFIG_DIR, scope=user)"
  local registered installed_before
  if ! registered=$(claude plugin marketplace list --json 2>/dev/null) \
    || ! installed_before=$(claude plugin list --json 2>/dev/null); then
    echo "[error] Claude導入前状態の取得に失敗" >&2; fail=1; return
  fi
  if ! validate_claude_marketplaces "$registered" || ! validate_claude_plugins "$installed_before"; then
    echo "[error] Claude導入前状態のschemaが不正" >&2; fail=1; return
  fi
  while IFS='|' read -r name repo plugins; do
    [ -n "$name" ] || continue
    if printf '%s' "$registered" | jq -e --arg name "$name" 'any(.[]; .name == $name)' >/dev/null; then
      if printf '%s' "$registered" | jq -e --arg name "$name" --arg repo "${OWNER}/${repo}" \
        'any(.[]; .name == $name and .source == "github" and .repo == $repo)' >/dev/null; then
        if ! claude plugin marketplace update "$name" >/dev/null 2>&1; then
          echo "[error] marketplace update 失敗: $name" >&2; fail=1; continue
        fi
      else
        # ローカル path など GitHub 以外の登録は置き換える
        if ! claude plugin marketplace remove "$name" >/dev/null 2>&1 \
          || ! claude plugin marketplace add "${OWNER}/${repo}" --scope user >/dev/null 2>&1; then
          echo "[error] marketplace置換失敗: $name" >&2; fail=1; continue
        fi
      fi
    else
      if ! claude plugin marketplace add "${OWNER}/${repo}" --scope user >/dev/null 2>&1; then
        echo "[error] marketplace add 失敗: $name" >&2; fail=1; continue
      fi
    fi
    for p in $plugins; do
      if printf '%s' "$installed_before" | jq -e --arg id "${p}@${name}" 'any(.[]; .id == $id and .scope == "user")' >/dev/null; then
        if ! out=$(claude plugin update "${p}@${name}" --scope user 2>&1); then
          echo "[error] ${p}@${name} update失敗: $out" >&2; fail=1
        fi
      elif ! out=$(claude plugin install "${p}@${name}" --scope user 2>&1); then
        echo "[error] ${p}@${name} install失敗: $out" >&2; fail=1
      fi
    done
  done <<<"$MARKETPLACES"
  local marketplaces installed location expected_version expected_path
  if ! marketplaces=$(claude plugin marketplace list --json 2>/dev/null) \
    || ! installed=$(claude plugin list --json 2>/dev/null); then
    echo "[error] Claude導入状態の取得に失敗" >&2; fail=1; return
  fi
  if ! validate_claude_marketplaces "$marketplaces" || ! validate_claude_plugins "$installed"; then
    echo "[error] Claude導入後状態のschemaが不正" >&2; fail=1; return
  fi
  while IFS='|' read -r name repo plugins; do
    [ -n "$name" ] || continue
    location=$(printf '%s' "$marketplaces" | jq -er --arg name "$name" --arg repo "${OWNER}/${repo}" \
      '.[] | select(.name == $name and .source == "github" and .repo == $repo) | .installLocation') \
      || { echo "[error] Claude marketplace source不一致: $name" >&2; fail=1; continue; }
    for p in $plugins; do
      expected_version=$(jq -er --arg p "$p" '.plugins[] | select(.name == $p) | .version' "$location/.claude-plugin/marketplace.json" 2>/dev/null) \
        || { echo "[error] Claude marketplace catalog identity不明: ${p}@${name}" >&2; fail=1; continue; }
      expected_path="$CLAUDE_CONFIG_DIR/plugins/cache/$name/$p/$expected_version"
      if printf '%s' "$installed" | jq -e --arg id "${p}@${name}" --arg version "$expected_version" --arg path "$expected_path" \
        'any(.[]; .id == $id and .scope == "user" and .version == $version and .enabled == true and .installPath == $path)' >/dev/null; then
        note "  ${p}@${name}: verified version=${expected_version} source=${OWNER}/${repo}"
      else
        echo "[error] Claude plugin登録状態不一致: ${p}@${name}" >&2; fail=1
      fi
    done
  done <<<"$MARKETPLACES"
  note "  対話セッションへの反映には Claude Code の再起動が要る"
}

install_codex() {
  command -v codex >/dev/null 2>&1 || { echo "[error] codex が無い" >&2; fail=1; return; }
  note "== Codex (CODEX_HOME=$CODEX_HOME)"
  local registered installed_before
  if ! registered=$(codex plugin marketplace list --json 2>/dev/null) \
    || ! installed_before=$(codex plugin list --json 2>/dev/null); then
    echo "[error] Codex導入前状態の取得に失敗" >&2; fail=1; return
  fi
  if ! validate_codex_marketplaces "$registered" || ! validate_codex_plugins "$installed_before"; then
    echo "[error] Codex導入前状態のschemaが不正" >&2; fail=1; return
  fi
  while IFS='|' read -r name repo plugins; do
    [ -n "$name" ] || continue
    root=$(printf '%s\n' "$registered" | jq -r --arg name "$name" '.marketplaces[] | select(.name == $name) | .root')
    source=$(printf '%s\n' "$registered" | jq -r --arg name "$name" '.marketplaces[] | select(.name == $name) | .marketplaceSource.source')
    if [ "$root" = "$CODEX_HOME/.tmp/marketplaces/$name" ] \
      && [ "$source" = "https://github.com/${OWNER}/${repo}.git" ]; then
      if ! codex plugin marketplace upgrade "$name" >/dev/null 2>&1; then
        echo "[error] marketplace upgrade 失敗: $name" >&2; fail=1; continue
      fi
    elif [ -z "$root" ]; then
      if ! codex plugin marketplace add "${OWNER}/${repo}" --ref main >/dev/null 2>&1; then
        echo "[error] marketplace add 失敗: $name" >&2; fail=1; continue
      fi
    else
        # ローカル path 登録は GitHub 登録へ置き換える
      if ! codex plugin marketplace remove "$name" >/dev/null 2>&1 \
        || ! codex plugin marketplace add "${OWNER}/${repo}" --ref main >/dev/null 2>&1; then
        echo "[error] marketplace置換失敗: $name" >&2; fail=1; continue
      fi
    fi
    for p in $plugins; do
      if ! out=$(codex plugin add "${p}@${name}" 2>&1); then
        echo "[error] ${p}@${name} install失敗: $out" >&2; fail=1
      fi
    done
  done <<<"$MARKETPLACES"
  local marketplaces installed expected_root expected_source catalog source_path expected_version expected_plugin_path
  if ! marketplaces=$(codex plugin marketplace list --json 2>/dev/null) \
    || ! installed=$(codex plugin list --json 2>/dev/null); then
    echo "[error] Codex導入状態の取得に失敗" >&2; fail=1; return
  fi
  if ! validate_codex_marketplaces "$marketplaces" || ! validate_codex_plugins "$installed"; then
    echo "[error] Codex導入後状態のschemaが不正" >&2; fail=1; return
  fi
  while IFS='|' read -r name repo plugins; do
    [ -n "$name" ] || continue
    expected_root="$CODEX_HOME/.tmp/marketplaces/$name"
    expected_source="https://github.com/${OWNER}/${repo}.git"
    if ! printf '%s' "$marketplaces" | jq -e --arg name "$name" --arg root "$expected_root" --arg source "$expected_source" \
      'any(.marketplaces[]; .name == $name and .root == $root and .marketplaceSource.sourceType == "git" and .marketplaceSource.source == $source)' >/dev/null; then
      echo "[error] Codex marketplace source不一致: $name" >&2; fail=1; continue
    fi
    catalog="$expected_root/.agents/plugins/marketplace.json"
    for p in $plugins; do
      expected_version=$(jq -er --arg p "$p" '.plugins[] | select(.name == $p) | .version' "$catalog" 2>/dev/null) \
        || { echo "[error] Codex marketplace catalog identity不明: ${p}@${name}" >&2; fail=1; continue; }
      source_path=$(jq -er --arg p "$p" '.plugins[] | select(.name == $p) | .source.path' "$catalog" 2>/dev/null) \
        || { echo "[error] Codex marketplace source path不明: ${p}@${name}" >&2; fail=1; continue; }
      expected_plugin_path="$expected_root/${source_path#./}"
      if printf '%s' "$installed" | jq -e --arg id "${p}@${name}" --arg version "$expected_version" --arg path "$expected_plugin_path" --arg source "$expected_source" \
        'any(.installed[]; .pluginId == $id and .version == $version and .installed == true and .enabled == true and .source.source == "local" and .source.path == $path and .marketplaceSource.sourceType == "git" and .marketplaceSource.source == $source)' >/dev/null; then
        note "  ${p}@${name}: verified version=${expected_version} source=${expected_source}"
      else
        echo "[error] Codex plugin登録状態不一致: ${p}@${name}" >&2; fail=1
      fi
    done
  done <<<"$MARKETPLACES"
}

case "$RUNTIME" in
  claude) install_claude ;;
  codex) install_codex ;;
  both) install_claude; install_codex ;;
esac
[ "$fail" = 0 ] && note "== done" || { echo "== 失敗あり" >&2; exit 1; }
