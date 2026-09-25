# harness-tools

harness-pluginsv2 の全 plugin repository が共有する保守 tool を、一か所に置く repository である。各 repository は兄弟 checkout `../harness-tools/` の tool を呼び、複製を持たない。plugin package ではないので、marketplace も runtime manifest も持たず、導入の対象にも root の検査の対象にもならない。

前提の CLI は、`python3`（3.10 以上）、`bash`、`jq`、`rg`（ripgrep）、mikefarah 版の `yq` v4 である。

## tool

すべての tool は、対象の repository を絶対パスの引数で受け取り、自分の置き場やカレントディレクトリから対象を推測しない。兄弟の checkout が無ければ止まり、fixture で代わりにしない。

| path | 役割 |
|---|---|
| `tools/validate-plugin-repository.py` | package の配置と manifest の検査。`--self-test` で合成 fixture の正例と反例を確かめる |
| `tools/validate-workspace.sh` | workspace の規約の置き場と、明示した repository への配置の検査。workspace の `scripts/validate.sh` が委譲する |
| `tools/release.py` | 両 marketplace と両 runtime manifest の version を同時に上げる |
| `tools/test-hardening.py` | release.py の回帰検査。`--repository` を渡すと、その repository の CI の action が SHA で固定されているかも見る |
| `tools/install-plugins.sh` | GitHub の marketplace 経由で全 plugin を Claude Code と Codex へ導入し、導入後の version と source を照合する |
| `tools/test-install-plugins.sh` | install-plugins.sh の照合を stub の CLI で検査する |
| `ci/validate.sh` | CI の共通の step。配置の検査と、その repository の `scripts/validate.sh` を実行する |
| `ci/install-requirements.sh` | GitHub Actions の runner へ yq、jq、ripgrep を入れる |
| `scripts/validate.sh` | harness-tools 自身の自己検査 |

各 tool が何を合否にするかの宣言は、その tool の docstring か冒頭のコメントにある。

## 各 repository からの呼び方

各 repository の `scripts/validate.sh` は、`../harness-tools/` があることを確かめてから tool を呼ぶ。

```bash
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
TOOLS="$ROOT/../harness-tools/tools"
[ -d "$TOOLS" ] || { echo "[error] 兄弟 checkout harness-tools が無い: $TOOLS" >&2; exit 2; }
python3 "$TOOLS/validate-plugin-repository.py" "$ROOT" || status=1
python3 "$TOOLS/test-hardening.py" --repository "$ROOT" || status=1
```

## CI の兄弟 checkout

各 repository の `.github/workflows/validate.yml` は、自分を `<workspace>/<自分の名前>` に、harness-tools と、自分の検査が実配布物を要する repository を `<workspace>/<repository 名>` に checkout し、`bash harness-tools/ci/validate.sh "$GITHUB_WORKSPACE/<自分の名前>"` を呼ぶ。兄弟の repository は、常に検証済みの `main` を取る。PR の ref を兄弟の checkout へ渡すと、未検証のコードを実行する経路ができるからである。複数の repository を同時に変えるときは、harness-tools、使われる側、使う側の順にマージする。action は commit SHA で固定する。

## 自己検査

```bash
bash scripts/validate.sh
```
