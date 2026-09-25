> 共通の規約は /Users/naoya-nakamoriq/Documents/Github/harness-pluginsv2/AGENTS.md にある。ここには、この repository だけの規則を置く。

# harness-tools

harness-pluginsv2 の全 plugin repository が共有する保守 tool を置く repository である。plugin package ではない（marketplace も runtime manifest も持たず、導入の対象にならない）。各 repository は兄弟 checkout `../harness-tools/` の tool を呼び、複製を持たない。

- `tools/` は、各 repository の `scripts/validate.sh`、workspace の入口、CI が呼ぶ tool である。対象の repository は絶対パスの引数で受け取り、自分の置き場から推測しない。
- `ci/` は GitHub Actions の共通の step である。
- `scripts/validate.sh` は harness-tools 自身の自己検査である。
- tool の引数を変えたら、呼び手（各 repository の `scripts/validate.sh`、README の呼び方）を同じ変更で直す。

変更後は `bash scripts/validate.sh` を実行する。
