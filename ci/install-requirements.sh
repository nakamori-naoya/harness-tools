#!/usr/bin/env bash
# GitHub Actions runner（ubuntu / macos）へ、検査が明示依存する CLI を入れる。CI の共通 step。
#
#   bash ../harness-tools/ci/install-requirements.sh
#
# 前提: actions/setup-python（python3）と actions/setup-go（go）が先に済んでいる。
# 入れるのは mikefarah/yq v4（go install）、jq、ripgrep。GITHUB_PATH があれば GOPATH/bin を後続 step の PATH へ足す。
set -euo pipefail

YQ_VERSION=v4.44.3

command -v go >/dev/null 2>&1 || { echo '[error] go が無い（actions/setup-go を先に置く）' >&2; exit 2; }
go install "github.com/mikefarah/yq/v4@${YQ_VERSION}"
GOBIN_DIR="$(go env GOPATH)/bin"
if [ -n "${GITHUB_PATH:-}" ]; then
  echo "$GOBIN_DIR" >> "$GITHUB_PATH"
fi
export PATH="$GOBIN_DIR:$PATH"

case "${RUNNER_OS:-$(uname -s)}" in
  Linux)
    sudo apt-get update
    sudo apt-get install -y jq ripgrep
    ;;
  macOS|Darwin)
    brew list jq >/dev/null 2>&1 || brew install jq
    brew list ripgrep >/dev/null 2>&1 || brew install ripgrep
    ;;
  *)
    echo "[error] 未対応の runner OS: ${RUNNER_OS:-$(uname -s)}" >&2; exit 2
    ;;
esac

yq --version | grep -q 'mikefarah' || { echo '[error] yq が mikefarah 版ではない' >&2; exit 2; }
jq --version
rg --version | head -1
