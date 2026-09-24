# harness-tools

harness-pluginsv2 の全 plugin repository が共有する保守 tool の基準資料である。各 repository は兄弟 checkout `../harness-tools/` の tool を呼び、複製を持たない。同期機構は無い。plugin package ではないので、marketplace も runtime manifest も持たず、`install-plugins.sh` の導入対象にも root validator の検査対象にもならない。

前提 CLI: `python3`（3.10 以上）、`bash`、`jq`、`rg`（ripgrep）、mikefarah 版 `yq` v4。

## tool 一覧

| path | 役割 | 呼び方（`<repo>` は plugin repository の絶対 path） |
|---|---|---|
| `tools/validate-plugin-repository.py` | root 契約（配置・manifest・隣接 playbook.yml・禁止参照形）を 1 repository へ fail-closed で適用する構造検査 | `python3 ../harness-tools/tools/validate-plugin-repository.py <repo>`、`--self-test` で合成 fixture の正例・反例・境界例 |
| `tools/validate-workspace.sh` | workspace root の規約入口検査と、明示 repository への root 契約適用。root の `scripts/validate.sh` が委譲する本体 | `bash tools/validate-workspace.sh --workspace <workspace root> <repo>...` |
| `tools/doctor.py` | CLI の有無、両 runtime の公開入口、全公開入口の外部依存を兄弟 checkout の実配布物に対して解決する読み取り専用診断（JSON） | `python3 ../harness-tools/tools/doctor.py --repository <repo> [--repo <対象 project>] [--distribution-only]` |
| `tools/lint-consumer-contract.py` | 消費側の文書・script・設定に外部依存の内部名（依存先の manifest から作る内部 plugin 名・内部 skill 名と、名指しの構文で書かれた工程 id）が漏れていないかの静的 lint | `HARNESS_PLUGIN_DEV_ROOTS=<契約ID→package root の JSON> python3 ../harness-tools/tools/lint-consumer-contract.py --repo <repo> --runtime claude\|codex [--json]` |
| `tools/resolve-dependency.py` | 依存 1 件の解決、束縛 snapshot（`--create-lock`）、steps の突き合わせ（`--check-steps`）。doctor と lint が使う | 直接呼ぶのは doctor / lint / test-hardening。使い方は file 先頭の docstring |
| `tools/release.py` | 両 marketplace と両 runtime manifest の version を同時に更新し、`releases/<plugin>-<version>.json` を書く | `python3 ../harness-tools/tools/release.py --repo <repo> --plugin <名> --version <semver> --notes … --breaking … --migration … --checks <JSON> [--apply]` |
| `tools/evaluate-skills.py` | `evals/scenarios.json` を生成 model と独立 judge へ渡して記録する。criterion の真偽は記録であり合否ではない | `scripts/run-evals.sh` から呼ぶ（下記） |
| `tools/claude-eval-adapter.py` | `claude --print` を tool 無しで呼び、要求した model が実際に使われた証拠を返す adapter | `evaluate-skills.py --model-command '["python3","<絶対path>/tools/claude-eval-adapter.py"]'` |
| `tools/test-hardening.py` | 上の tool の回帰検査（unittest）。`--repository` を渡すと、その repository の CI workflow の SHA 固定、公開入口の一意性、doctor の読み取り専用性も検査する | `python3 ../harness-tools/tools/test-hardening.py [--repository <repo>]` |
| `tools/install-plugins.sh` | GitHub marketplace 経由で全 plugin を Claude Code / Codex へ導入・更新する。root の `install-plugins.sh` が委譲する本体 | `bash tools/install-plugins.sh [--runtime claude\|codex\|both] [--owner <owner>]` |
| `tools/test-install-plugins.sh` | `install-plugins.sh` の契約（fail-closed、mutation 後の identity 照合）を stub CLI で検査 | `bash tools/test-install-plugins.sh` |
| `ci/validate.sh` | CI の共通 step。前提 CLI → root 契約 → その repository の `scripts/validate.sh` | `bash ../harness-tools/ci/validate.sh <repo>` |
| `ci/install-requirements.sh` | GitHub Actions runner へ yq / jq / ripgrep を入れる | `bash ../harness-tools/ci/install-requirements.sh` |
| `scripts/run-evals.sh` | eval runner（下記） | `bash scripts/run-evals.sh --model … --judge-model … <repo>...` |
| `scripts/validate.sh` | harness-tools 自身の自己検査 | `bash scripts/validate.sh` |

すべての tool は対象 repository を**絶対 path の引数**で受け取る。自分の置き場や cwd から対象を推測しない。

## 各 repository からの呼び方

各 repository の `scripts/validate.sh` は、`../harness-tools/` の実在を確認してから tool を呼ぶ。無ければ FAIL で止める（fixture で代用しない）。

```bash
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
TOOLS="$ROOT/../harness-tools/tools"
[ -d "$TOOLS" ] || { echo "[error] 兄弟 checkout harness-tools が無い: $TOOLS" >&2; exit 2; }

python3 "$TOOLS/validate-plugin-repository.py" "$ROOT" || status=1
python3 "$TOOLS/test-hardening.py" --repository "$ROOT" || status=1
# 外部依存を持つ repository: 兄弟 checkout の実配布物から dev-map を作って lint を両 runtime で実行する
jq -n --arg g "$(cd "$ROOT/../grill-plugins/plugins/grill" && pwd -P)" \
      --arg w "$(cd "$ROOT/../write-doc-plugins/plugins/write-doc" && pwd -P)" \
  '{schema:1,dependencies:{"grill/grill":$g,"write-doc/write-doc":$w}}' > "$TMP_ROOT/dev-map.json"
for runtime in claude codex; do
  HARNESS_PLUGIN_DEV_ROOTS="$TMP_ROOT/dev-map.json" python3 "$TOOLS/lint-consumer-contract.py" --repo "$ROOT" --runtime "$runtime" || status=1
done
```

- `validate-plugin-repository.py` は repository 固有の validator（`validate_repository.py`、`validate-structure.sh` 等）を置き換えない。root 契約は共通、固有の契約は各 repository が持つ。
- `doctor.py` は外部依存を `<repo>/../<marketplace>-plugins/plugins/<package>` の実配布物に対して解く。兄弟 checkout が無ければ NG を返す。契約 ID → package root の JSON を `HARNESS_PLUGIN_REAL_ROOTS` で渡すこともできる。
- リリースは `release.py --apply` で版と記録を更新し、PR を main へ merge してから root の `install-plugins.sh` で更新する。

workspace root（`harness-pluginsv2/`）では `bash scripts/validate.sh <repo の絶対 path>...` が `tools/validate-workspace.sh` へ委譲し、規約入口の検査と root 契約を掛ける。

## CI の兄弟 checkout

各 repository の `.github/workflows/validate.yml` は、自 repository を `<workspace>/<自分の名前>` に、harness-tools と依存 provider を `<workspace>/<repo 名>` に checkout し、`bash ../harness-tools/ci/validate.sh "$GITHUB_WORKSPACE/<自分の名前>"` を呼ぶ。`ci/validate.sh` が実行するのは local の `scripts/validate.sh` と同じ command である。

兄弟 repository は常に検証済みの `main` を取得する。PR 由来の ref を依存 checkout へ渡さない（未信頼のコードを実行する経路を作らない）。複数 repository を同時に更新するときは、提供側（harness-tools → provider → consumer）から順に merge する。

```yaml
name: Validate
on:
  push:
  pull_request:
permissions:
  contents: read
jobs:
  validate:
    name: validate (${{ matrix.os }})
    strategy:
      fail-fast: false
      matrix:
        os: [ubuntu-latest, macos-latest]
    runs-on: ${{ matrix.os }}
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          path: ${{ github.event.repository.name }}
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          repository: ${{ github.repository_owner }}/harness-tools
          ref: main
          path: harness-tools
      # 依存 provider（自分の scripts/validate.sh が実配布物を必要とするものだけ）。自分自身は checkout しない。
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        if: github.event.repository.name != 'grill-plugins'
        with:
          repository: ${{ github.repository_owner }}/grill-plugins
          ref: main
          path: grill-plugins
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        if: github.event.repository.name != 'write-doc-plugins'
        with:
          repository: ${{ github.repository_owner }}/write-doc-plugins
          ref: main
          path: write-doc-plugins
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        if: github.event.repository.name != 'agent-work-policy-plugins'
        with:
          repository: ${{ github.repository_owner }}/agent-work-policy-plugins
          ref: main
          path: agent-work-policy-plugins
      - uses: actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97 # v7.0.0
        with:
          python-version: '3.12'
      - uses: actions/setup-go@b7ad1dad31e06c5925ef5d2fc7ad053ef454303e # v7.0.0
        with:
          go-version: '1.24.x'
      - name: Install CLI requirements
        shell: bash
        run: bash harness-tools/ci/install-requirements.sh
      - name: Validate
        shell: bash
        run: bash harness-tools/ci/validate.sh "$GITHUB_WORKSPACE/${{ github.event.repository.name }}"
```

動く条件: (1) GitHub に同 owner の `harness-tools` repository があり `main` に `ci/` と `tools/` がある、(2) 依存 provider の `main` が検証済みで、consumer の validate.sh が期待する配布物 path（`plugins/<package>`）を持つ、(3) action は commit SHA で固定する（`test-hardening.py --repository` が検査する）。

## eval runner

```bash
bash scripts/run-evals.sh --model claude-opus-5 --judge-model claude-sonnet-5 --effort high \
  /absolute/path/to/go-convention-plugins /absolute/path/to/pull-request-plugins
```

各 repository の `evals/scenarios.json` を `tools/evaluate-skills.py` で実行し、`<repo>/evals/runs/<YYYY-MM-DD>/<label>.json`（`--label` 省略時は `evaluation`）へ記録する。生成と judge は `tools/claude-eval-adapter.py`（`claude --print`、tool 無し）で呼ぶので、実行者の環境の資格情報を使う。**local で手動実行する。CI には置かない。**

- exit 0 は「全 repository で全 case の記録が書けた」ことだけを示す。criterion の pass/fail は記録の中の意味証拠であり、runner は合否 gate ではない。
- **モデルを更新したときに実行し、前回の記録（同じ `evals/runs/` の過去日付）と読み比べる。読み比べは agent の意味評価**（応答の逐語、judge の理由、criterion の妥当性を読み、根拠付きで所見を `<label>.md` に残す）であり、点数や pass 数の差で判定しない。
- 記録は上書きしない。同じ日に再実行するときは `--label` で別名を付ける。
- repository が無い、`evals/scenarios.json` が無い、adapter が失敗した（記録に `error` が残る）、いずれも非 0 で終了する。自己検査は `scripts/test-run-evals.sh`（stub の `claude` で実 model を呼ばない）。

## 自己検査

```bash
bash scripts/validate.sh
```

各 tool の self-test / unittest（`validate-plugin-repository.py --self-test`、`test-hardening.py`、`test-install-plugins.sh`、`test-run-evals.sh`）、構文、入口 script の引数契約の負例、`ci/validate.sh` の正例を実行する。plugin repository は検査しない。

## 規則

- tool へ条件を足すときは、[機械検査と意味評価の境界](/Users/naoya-nakamoriq/Documents/Github/harness-pluginsv2/.agents/rules/deterministic-validation.md)の宣言（基準資料・入力・正規化・合格述語・診断・正例・反例・境界例・意味評価として残す範囲）を先に書く。root 契約の宣言は [plugin package の公開境界と自己完結](/Users/naoya-nakamoriq/Documents/Github/harness-pluginsv2/.agents/rules/plugin-package-contract.md) にある。
- 後方互換の alias / fallback / 基準資料の重複を作らない。tool の引数を変えたら、呼び手（各 repository の `scripts/validate.sh`、この README）を同じ変更で直す。
