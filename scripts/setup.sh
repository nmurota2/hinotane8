#!/usr/bin/env bash
#
# hinotane セットアップスクリプト（macOS / Linux / VPS 用）
#
#   bash scripts/setup.sh
#
# やること:
#   1. Python 3.11 以上があるか確認
#   2. 仮想環境 .venv を作る
#   3. 依存パッケージを入れる
#   4. .env を作り、API キーなどを聞いて書き込む（画面には表示しません）
#   5. 設定の健康診断を実行
#
# 何度実行しても壊れません。設定を変えたいときは再実行してください。

set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$(pwd)"

BOLD=$'\033[1m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RED=$'\033[31m'; RESET=$'\033[0m'

say()  { printf "%s\n" "$*"; }
ok()   { printf "%s✅ %s%s\n" "$GREEN" "$*" "$RESET"; }
warn() { printf "%s⚠️  %s%s\n" "$YELLOW" "$*" "$RESET"; }
err()  { printf "%s❌ %s%s\n" "$RED" "$*" "$RESET"; }
step() { printf "\n%s── %s ──%s\n" "$BOLD" "$*" "$RESET"; }

# --------------------------------------------------------------- 1. Python
step "1/5 Python を確認します"

PYTHON=""
for candidate in python3.13 python3.12 python3.11 python3; do
    if command -v "$candidate" >/dev/null 2>&1; then
        if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
            PYTHON="$candidate"
            break
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    err "Python 3.11 以上が見つかりませんでした。"
    say ""
    if [ "$(uname -s)" = "Darwin" ]; then
        say "  ${BOLD}macOS に最初から入っている Python は 3.9 系で、この先の処理には足りません。${RESET}"
        say ""
        say "  ${BOLD}いちばん簡単な入れ方（ターミナル不要）:${RESET}"
        say "    1. https://www.python.org/downloads/macos/ を開く"
        say "    2. 「Latest Python 3 Release」の macOS 64-bit universal2 installer を取得"
        say "    3. ダウンロードした .pkg をダブルクリックして、指示どおり進めるだけ"
        say "    4. 終わったらターミナルを一度閉じて開き直し、このコマンドを再実行"
        say ""
        say "  （Homebrew を使い慣れている場合は brew install python@3.12 でも可）"
    else
        say "  Ubuntu / Debian : sudo apt update && sudo apt install -y python3.12 python3.12-venv"
        say "  その他          : https://www.python.org/downloads/"
    fi
    say ""
    exit 1
fi
ok "$($PYTHON -V) を使います"

# --------------------------------------------------------- 2. 仮想環境
step "2/5 仮想環境を用意します"

if [ ! -d .venv ]; then
    "$PYTHON" -m venv .venv
    ok "仮想環境 .venv を作成しました"
else
    ok "既存の .venv を使います"
fi

VENV_PY="$ROOT/.venv/bin/python"
HINOTANE="$ROOT/.venv/bin/hinotane"

# ------------------------------------------------------- 3. パッケージ
step "3/5 必要なパッケージを入れます（初回は数分かかります）"

"$VENV_PY" -m pip install --quiet --upgrade pip
"$VENV_PY" -m pip install --quiet -e ".[dev]"
ok "インストール完了"

# --------------------------------------------------------------- 4. .env
step "4/5 設定ファイル (.env) を作ります"

if [ ! -f .env ]; then
    cp .env.example .env
    ok ".env を作成しました"
else
    ok "既存の .env に追記・更新します"
fi

# .env の値を安全に書き換える。キーに記号が入っていても壊れないよう Python で処理する。
set_env() {
    local key="$1" value="$2"
    KEY="$key" VALUE="$value" "$VENV_PY" - <<'PY'
import os
import pathlib

key = os.environ["KEY"]
value = os.environ["VALUE"]
path = pathlib.Path(".env")
lines = path.read_text().splitlines()

replaced = False
for i, line in enumerate(lines):
    stripped = line.lstrip()
    if stripped.startswith(f"{key}=") or stripped.startswith(f"#{key}="):
        lines[i] = f"{key}={value}"
        replaced = True
        break
if not replaced:
    lines.append(f"{key}={value}")

path.write_text("\n".join(lines) + "\n")
PY
}

current_env() {
    grep -E "^$1=" .env 2>/dev/null | head -1 | cut -d= -f2- || true
}

say ""
say "${BOLD}J-Quants の API キー${RESET}"
say "  ダッシュボード →［設定］→［API キー］で発行したものを貼り付けてください。"
say "  ${YELLOW}入力中は画面に表示されません（そういう仕様です）。${RESET}"
if [ -n "$(current_env JQUANTS_API_KEY)" ]; then
    say "  ${GREEN}設定済みです。Enter だけ押せば変更しません。${RESET}"
fi
printf "  APIキー: "
read -rs JQ_KEY || true
printf "\n"

if [ -n "${JQ_KEY:-}" ]; then
    set_env JQUANTS_API_KEY "$JQ_KEY"
    ok "APIキーを .env に保存しました（${#JQ_KEY} 文字）"
    unset JQ_KEY
else
    if [ -z "$(current_env JQUANTS_API_KEY)" ]; then
        warn "APIキーが未設定のままです。あとで .env を直接編集してください。"
    fi
fi

say ""
say "${BOLD}運用資金（円）${RESET}"
say "  ポジションサイズの計算基準です。実際に株に回す予定の金額を入れてください。"
CURRENT_EQUITY="$(current_env RISK_EQUITY_JPY)"
say "  現在の設定: ${CURRENT_EQUITY:-未設定}（Enter で変更しません）"
printf "  金額: "
read -r EQUITY || true

if [ -n "${EQUITY:-}" ]; then
    if printf "%s" "$EQUITY" | grep -Eq '^[0-9]+$'; then
        set_env RISK_EQUITY_JPY "$EQUITY"
        ok "運用資金を ${EQUITY} 円に設定しました"
        RISK=$(( EQUITY / 100 ))
        say "     → 1トレードあたりの想定最大損失: 約 ${RISK} 円（資金の1%）"
    else
        warn "数字のみで入力してください。今回は変更しませんでした。"
    fi
fi

chmod 600 .env 2>/dev/null || true

# ------------------------------------------------------------- 5. 診断
step "5/5 設定を確認します"
say ""

set +e
"$HINOTANE" doctor
DOCTOR_STATUS=$?
set -e

say ""
printf "%s────────────────────────────────────────%s\n" "$BOLD" "$RESET"
if [ $DOCTOR_STATUS -eq 0 ]; then
    ok "セットアップ完了です"
    say ""
    say "${BOLD}次にやること${RESET}"
    say ""
    say "  1) 過去データを取り込む（数十分かかります。放っておいてOK）"
    say "     ${BOLD}.venv/bin/hinotane backfill --years 2${RESET}"
    say ""
    say "  2) スクリーニングを試す（LINE には送りません）"
    say "     ${BOLD}.venv/bin/hinotane screen --dry-run${RESET}"
    say ""
    say "  3) バックテストで戦略を検証する"
    say "     ${BOLD}.venv/bin/hinotane walkforward${RESET}"
    say ""
    say "  LINE 通知を使うときは docs/03_setup_guide.md の「2. LINE」へ。"
else
    warn "設定に問題があります。${BOLD}上に出ている原因の候補${RESET}${YELLOW}を確認してください。${RESET}"
    say ""
    say "  直したら、もう一度この確認だけ実行できます:"
    say "     ${BOLD}.venv/bin/hinotane doctor${RESET}"
    say ""
    say "  解決しなければ、上の出力をそのまま伝えてください。"
fi
printf "%s────────────────────────────────────────%s\n" "$BOLD" "$RESET"

exit $DOCTOR_STATUS
