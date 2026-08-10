"""立花証券 e支店 API アダプタ（Phase 4 / 未実装）。

⚠️ **意図的に未実装のまま置いてあります。**

理由: 実発注のリクエスト形式を推測で書くと、誤った銘柄・数量・売買区分で
本物の注文が飛ぶ危険があります。口座開設後に配布される公式のAPI仕様書を
確認してから実装します。仕様書が手元に来たら、このファイルを埋めるだけで
他のコードは一切変更不要です（BrokerAdapter で抽象化済みのため）。

--------------------------------------------------------------------------
調査済みの前提（docs/01_research.md 参照）
--------------------------------------------------------------------------
* 口座は必ず「**e支店**」で開設すること。「ストックハウス」で開設すると
  API が使えず、一度解約しないと e支店側に口座を作れない。
* 通信は全て HTTPS の GET。クエリを JSON 化して URL エンコードし、
  URL 末尾に付ける方式。Linux + Python だけで完結する。
* 認証フロー:
      1. `接続先URL + auth/ + ? + データ部` にログイン要求
      2. レスポンスで「**仮想URL**」が返る = そのセッション限りのトークン
      3. 以降のコマンドは `仮想URL + ? + データ部` に送る
* 仮想URLの失効条件（いずれか最初に起きたとき）:
      - ログアウト
      - **同一ユーザーIDでの再ログイン**（多重起動厳禁）
      - **APIサーバーの閉局（03:30）**
  → 毎朝の自動再ログイン処理が必須。プロセスの多重起動を防ぐこと。
* 取引可能商品は国内株式の現物・信用のみ。
* 2026/6/27 に v4r8 が廃止され、以降は API ログイン時の電話認証が不要。
* 🚨 **2026年12月初旬より、取引・出金にパスキー認証の設定が必須**。
  API 発注への影響は口座開設時に必ず確認すること。

--------------------------------------------------------------------------
実装時のチェックリスト
--------------------------------------------------------------------------
[ ] ログイン → 仮想URL 取得 → セッション保持（03:30 の閉局をまたいだら再ログイン）
[ ] プロセス多重起動の防止（ファイルロック）。二重ログインでセッションが飛ぶため
[ ] 発注前の残高・買付余力チェック
[ ] 発注前の価格乖離チェック（シグナル時の価格から大きく離れていたら中止して通知）
[ ] 注文受付 → 約定確認のポーリング（約定するまで status を確定させない）
[ ] 失敗時は必ず LINE に通知（黙って失敗するのが一番危ない）
[ ] まず 1 単元・1 銘柄だけの手動実行で疎通確認してから自動化に載せる
"""

from __future__ import annotations

from ..config import ExecutionConfig
from ..db import Database
from .base import BrokerAdapter, OrderRequest, OrderResult

_NOT_READY = (
    "立花証券 e支店 API アダプタは未実装です。\n"
    "口座開設後に公式のAPI仕様書を確認してから実装します。\n"
    "それまでは BROKER=paper（擬似発注）で運用してください。"
)


class TachibanaBroker(BrokerAdapter):
    name = "tachibana"
    is_paper = False

    def __init__(self, cfg: ExecutionConfig, db: Database):
        self.cfg = cfg
        self.db = db

    def healthcheck(self) -> tuple[bool, str]:
        return False, _NOT_READY

    def buy(self, req: OrderRequest) -> OrderResult:
        raise NotImplementedError(_NOT_READY)

    def sell(self, req: OrderRequest) -> OrderResult:
        raise NotImplementedError(_NOT_READY)
