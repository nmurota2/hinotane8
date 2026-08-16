#!/usr/bin/env bash
#
# 最新版を取り込んだあとに実行するスクリプト
#
#   bash scripts/update.sh
#
# GitHub Desktop で「Pull origin」した直後は、必ずこれを実行してください。
#
# なぜ必要か:
#   コードを取り込んでも、Python 側の「どこにパッケージがあるか」という
#   登録情報は自動更新されません。依存パッケージが増えたときも同様です。
#   その状態で実行すると `ModuleNotFoundError: No module named 'hinotane'`
#   のようなエラーになります。このスクリプトが登録し直します。
#
# .env は触りません。API キーなどの設定はそのまま残ります。

set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$(pwd)"

BOLD=$'\033[1m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RED=$'\033[31m'; RESET=$'\033[0m'

ok()   { printf "%s✅ %s%s\n" "$GREEN" "$*" "$RESET"; }
warn() { printf "%s⚠️  %s%s\n" "$YELLOW" "$*" "$RESET"; }
err()  { printf "%s❌ %s%s\n" "$RED" "$*" "$RESET"; }
step() { printf "\n%s── %s ──%s\n" "$BOLD" "$*" "$RESET"; }

if [ ! -x .venv/bin/python ]; then
    err "仮想環境 (.venv) が見つかりません。"
    printf "\n  最初のセットアップがまだのようです。こちらを実行してください:\n"
    printf "    %sbash scripts/setup.sh%s\n\n" "$BOLD" "$RESET"
    exit 1
fi

step "1/2 パッケージを登録し直します"
.venv/bin/python -m pip install --quiet --upgrade pip
.venv/bin/python -m pip install --quiet -e ".[dev]"
ok "完了"

step "2/2 設定を確認します"
printf "\n"

set +e
.venv/bin/hinotane doctor
DOCTOR_STATUS=$?
set -e

printf "\n%s────────────────────────────────────────%s\n" "$BOLD" "$RESET"
if [ $DOCTOR_STATUS -eq 0 ]; then
    ok "最新版で正常に動いています"
    printf "\n  そのまま次の作業に進めます。よく使うコマンド:\n\n"
    printf "    %s.venv/bin/hinotane backfill --years 2%s   過去データの取り込み\n" "$BOLD" "$RESET"
    printf "    %s.venv/bin/hinotane screen --dry-run%s     候補を画面に表示\n" "$BOLD" "$RESET"
    printf "    %s.venv/bin/hinotane walkforward%s          戦略の検証\n" "$BOLD" "$RESET"
else
    warn "設定に問題があります。上に出ている原因の候補を確認してください。"
    printf "\n  解決しなければ、上の出力をそのまま伝えてください。\n"
fi
printf "%s────────────────────────────────────────%s\n" "$BOLD" "$RESET"

exit $DOCTOR_STATUS
