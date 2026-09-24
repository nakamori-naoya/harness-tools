> 作業を始める前に、workspace規約入口 `/Users/naoya-nakamoriq/Documents/Github/harness-pluginsv2/AGENTS.md` を読み、そこから指定される共通規約（`/Users/naoya-nakamoriq/Documents/Github/harness-pluginsv2/.agents/rules/harness-principles.md`、`/Users/naoya-nakamoriq/Documents/Github/harness-pluginsv2/.agents/rules/deterministic-validation.md`、`/Users/naoya-nakamoriq/Documents/Github/harness-pluginsv2/.agents/rules/plugin-package-contract.md`）とこのrepository固有の規則を適用する。

# harness-tools

harness-pluginsv2 の全 plugin repository が共有する保守 tool の基準資料である。plugin package ではない（marketplace も runtime manifest も持たず、install 対象にならない）。各 repository は兄弟 checkout `../harness-tools/` の tool を呼び、複製を持たない。同期機構（`sync-runtime.py`、`runtime-manifest.json`）は置かない。

- `tools/` は各 repository の `scripts/validate.sh`、workspace root の入口、CI が呼ぶ tool。すべて絶対 path の引数で対象 repository を受け取り、自分の置き場から対象を推測しない。
- `ci/` は GitHub Actions の共通 step。各 repository の workflow は harness-tools を兄弟 checkout してここを呼ぶ。
- `scripts/` は harness-tools 自身の自己検査（`validate.sh`）と eval runner（`run-evals.sh`）。
- 機械検査に入れるのは、閉じた入力と基準資料から真偽が一意に決まる述語だけである。tool へ条件を足すときは、`deterministic-validation.md` の宣言（基準資料・入力・正規化・合格述語・診断・正例・反例・境界例・意味評価として残す範囲）を先に書く。意味評価（文章の妥当性、eval 記録の読み比べ）を script の合否にしない。
- 後方互換の alias / fallback / 基準資料の重複を作らない。tool の引数を変えたら、呼び手（各 repository の `scripts/validate.sh`、README の呼び方）を同じ変更で直す。

変更後は `bash scripts/validate.sh` を実行する。plugin repository 側の検査は、その repository で `bash scripts/validate.sh` を、workspace root で `bash scripts/validate.sh <repository の絶対 path>` を実行する。

## 検査スクリプトは、意味が一意に決まることだけを判定する

このrepositoryの検査スクリプト（validate、lint、verify、checkなど、名前を問わない）が判定してよいのは、ファイルや見出しの有無、識別子や版の一致、宣言と配置の対応、禁止された書き方の有無のように、入力と基準資料から意味が決定論的に一意に決まることだけである。読んで解釈しないと決まらないことや、件数や語の出現のような品質の代わりの指標は判定せず、エージェントが読んで評価する（意味評価）。判定が一意に決まることを宣言できない検査は作らず、詳しい条件は `/Users/naoya-nakamoriq/Documents/Github/harness-pluginsv2/.agents/rules/deterministic-validation.md` に従う。
