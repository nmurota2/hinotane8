#!/usr/bin/env bash
# 追加データの取り込みをまとめて実行する。
#
# 3 つのコマンドを順に走らせるだけだが、1 つずつ手で打つと
# 「どこまで終わったか分からない」「途中のエラーを見落とす」が起きる。
# ここでまとめて、各段階の結果を必ず画面に出す。
#
# 途中で止めても安全。取得済みの日付はスキップするので、
# もう一度実行すれば続きから再開する。

set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

step() { printf '\n\033[1;36m── %s ──\033[0m\n' "$*"; }
ok()   { printf '\033[1;32m✅ %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m⚠️  %s\033[0m\n' "$*"; }
fail() { printf '\033[1;31m❌ %s\033[0m\n' "$*"; }

if [ ! -x "./hinotane.sh" ]; then
    fail "hinotane.sh が見つかりません。リポジトリの中で実行してください。"
    exit 1
fi

started=$(date +%s)

step "1/4 いまの検証範囲を確認します（数秒）"
if ! ./hinotane.sh universe; then
    fail "universe に失敗しました。ここで止めます。"
    exit 1
fi

step "2/4 過去時点の上場銘柄一覧を取得します（数分）"
echo "   いま検証から黙って外れている「上場廃止銘柄」を数えます。"
if ! ./hinotane.sh backfill-listed; then
    warn "backfill-listed に失敗しました。次に進みます。"
fi

step "3/4 決算・財務情報を取得します（20〜30分）"
echo "   Light プラン以上が必要です。Free だと空で返り続けます。"
echo "   ここが最も時間がかかります。席を外して大丈夫です。"
if ! ./hinotane.sh backfill-fins; then
    warn "backfill-fins に失敗しました。次に進みます。"
fi

step "4/4 取り込み結果を確認します"
./hinotane.sh universe 2>&1 | sed -n '/生存者バイアス/,/^$/p'

elapsed=$(( ($(date +%s) - started) / 60 ))
printf '\n────────────────────────────────────────\n'
ok "取り込み完了（所要 ${elapsed} 分）"
cat <<'MSG'

  次にやること:

    ./hinotane.sh doctor          設定の確認
    ./hinotane.sh forward         紙トレードの経過（まだ開始していなければ案内が出ます）

  LINE の設定は docs/03_setup_guide.md のセクション 2 を見てください。
MSG
printf '────────────────────────────────────────\n'
