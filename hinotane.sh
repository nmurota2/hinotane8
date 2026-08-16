#!/usr/bin/env bash
#
# hinotane 起動スクリプト（インストール状態に依存しません）
#
#   ./hinotane.sh doctor
#   ./hinotane.sh backfill --years 2
#   ./hinotane.sh screen --dry-run
#
# なぜこれがあるか:
#   `pip install -e .` による「パッケージの場所の登録」は、コードを更新したり
#   環境をいじったりすると壊れることがあり、その場合
#   `ModuleNotFoundError: No module named 'hinotane'` になります。
#   このスクリプトはソースの場所を直接指定して起動するので、
#   登録が壊れていても動きます。困ったらこちらを使ってください。
#
# どこから実行しても動きます（スクリプト自身の場所を基準にするため）。

set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"

if [ ! -x "$ROOT/.venv/bin/python" ]; then
    printf '\033[31m❌ 仮想環境 (.venv) が見つかりません。\033[0m\n' >&2
    printf '\n  最初のセットアップがまだのようです:\n' >&2
    printf '    \033[1mbash scripts/setup.sh\033[0m\n\n' >&2
    exit 1
fi

# ソースの場所を直接指定する。pip の登録が壊れていても解決できる。
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"

# .env とデータベースの相対パスがずれないよう、必ずリポジトリ直下で動かす
cd "$ROOT"

exec "$ROOT/.venv/bin/python" -m hinotane "$@"
