"""LINE Webhook 受信サーバー（FastAPI）。

LINE の「買う / 見送る」ボタン（postback）と、テキストでの問い合わせを受ける。
テキストへの返信は reply API なので **課金対象外**。何回聞いても無料。

セキュリティ上、以下を必ず通す:
  1. X-Line-Signature の署名検証（偽の承認リクエストを弾く）
  2. 送信者 userId のホワイトリスト照合（他人が承認できないようにする）
"""

from __future__ import annotations

import logging
from datetime import datetime
from urllib.parse import parse_qs

from fastapi import APIRouter, FastAPI, Header, HTTPException, Request

from .config import AppConfig, load_config, now
from .datasource.jquants import display_code
from .db import Database
from .notify.line import LineNotifier, verify_signature

log = logging.getLogger(__name__)

router = APIRouter()


def _state() -> tuple[AppConfig, Database, LineNotifier]:
    cfg = load_config()
    return cfg, Database(cfg.db_path), LineNotifier(cfg.line)


@router.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.post("/line/webhook")
async def line_webhook(request: Request, x_line_signature: str = Header(default="")) -> dict:
    cfg, db, notifier = _state()

    if not cfg.line.channel_secret:
        raise HTTPException(status_code=503, detail="LINE_CHANNEL_SECRET が未設定です")

    body = await request.body()
    if not verify_signature(cfg.line.channel_secret, body, x_line_signature):
        log.warning("署名検証に失敗したリクエストを拒否しました")
        raise HTTPException(status_code=403, detail="invalid signature")

    payload = await request.json()
    for event in payload.get("events", []):
        try:
            _handle_event(cfg, db, notifier, event)
        except Exception:
            log.exception("イベント処理に失敗しました")

    return {"ok": True}


def _handle_event(cfg: AppConfig, db: Database, notifier: LineNotifier, event: dict) -> None:
    user_id = (event.get("source") or {}).get("userId", "")
    reply_token = event.get("replyToken", "")

    # --- セットアップモード ------------------------------------------------
    # 宛先 userId が未登録のうちは、userId を教え返すことしかしない。
    # 承認などの操作は一切受け付けないので、この状態で第三者に
    # 勝手に発注されることはない。
    if cfg.line.setup_mode:
        log.warning("【セットアップ】あなたの userId: %s", user_id)
        notifier.reply_text(
            reply_token,
            "🔧 セットアップモードです\n\n"
            "あなたの userId は以下です。これを .env の\n"
            "LINE_ALLOWED_USER_IDS に貼り付けて再起動してください。\n\n"
            f"{user_id}\n\n"
            "登録が済むと、毎晩の候補通知と承認ボタンが有効になります。",
        )
        return

    if user_id not in cfg.line.allowed_user_ids:
        log.warning("許可されていない userId からの操作を無視: %s", user_id[:8])
        return

    if event.get("type") == "postback":
        _handle_postback(cfg, db, notifier, event, user_id, reply_token)
    elif event.get("type") == "message" and event.get("message", {}).get("type") == "text":
        _handle_text(cfg, db, notifier, event["message"]["text"].strip(), reply_token)


def _handle_postback(
    cfg: AppConfig,
    db: Database,
    notifier: LineNotifier,
    event: dict,
    user_id: str,
    reply_token: str,
) -> None:
    data = parse_qs((event.get("postback") or {}).get("data", ""))
    action = (data.get("a") or [""])[0]
    sid = (data.get("sid") or [""])[0]

    if action not in {"approve", "reject"} or not sid:
        return

    rows = db.query(
        "SELECT code, name, quantity, entry_price, status, expires_at FROM signals WHERE id = ?",
        [sid],
    )
    if rows.empty:
        notifier.reply_text(reply_token, "そのシグナルは見つかりませんでした。")
        return

    row = rows.iloc[0]
    code = display_code(str(row["code"]))

    if row["status"] != "pending":
        notifier.reply_text(
            reply_token, f"{code} {row['name']} は既に「{row['status']}」の状態です。"
        )
        return

    expires = row["expires_at"]
    if expires is not None and now() > _as_datetime(expires):
        db.execute("UPDATE signals SET status = 'expired' WHERE id = ?", [sid])
        notifier.reply_text(reply_token, f"{code} {row['name']} は承認期限が切れています。")
        return

    new_status = "approved" if action == "approve" else "rejected"
    db.execute("UPDATE signals SET status = ? WHERE id = ?", [new_status, sid])
    db.execute(
        "INSERT INTO approvals (signal_id, decision, decided_by) VALUES (?, ?, ?)",
        [sid, action, user_id],
    )

    if action == "approve":
        cost = float(row["entry_price"]) * int(row["quantity"])
        mode = "⚠️ 実発注" if cfg.execution.live_trading else "擬似発注"
        notifier.reply_text(
            reply_token,
            f"✅ 承認しました\n{code} {row['name']}\n"
            f"{int(row['quantity']):,}株 / 約{cost:,.0f}円\n"
            f"翌営業日の寄り付きに{mode}します。",
        )
    else:
        notifier.reply_text(reply_token, f"👋 {code} {row['name']} を見送りました。")


def _handle_text(
    cfg: AppConfig, db: Database, notifier: LineNotifier, text: str, reply_token: str
) -> None:
    """テキストでの問い合わせ。reply なので何回でも無料。"""
    if text in {"状況", "ステータス", "status"}:
        notifier.reply_text(reply_token, _status_text(cfg, db))
    elif text in {"保有", "ポジション", "positions"}:
        notifier.reply_text(reply_token, _positions_text(db))
    elif text in {"成績", "せいせき", "pnl"}:
        notifier.reply_text(reply_token, _pnl_text(db))
    elif text in {"停止", "stop", "キル"}:
        db.execute("UPDATE signals SET status = 'rejected' WHERE status = 'pending'")
        notifier.reply_text(reply_token, "🛑 未承認のシグナルをすべて取り消しました。")
    else:
        notifier.reply_text(
            reply_token,
            "使えるコマンド:\n"
            "・状況 … データの鮮度と保有数\n"
            "・保有 … 現在の建玉一覧\n"
            "・成績 … 確定損益のサマリ\n"
            "・停止 … 未承認シグナルを全部取り消す",
        )


def _as_datetime(value) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def _status_text(cfg: AppConfig, db: Database) -> str:
    latest = db.latest_quote_date()
    latest_str = latest.date().isoformat() if latest is not None else "データなし"
    pending = db.query("SELECT count(*) AS n FROM signals WHERE status = 'pending'").iloc[0]["n"]
    mode = "⚠️ 実発注" if cfg.execution.live_trading else "擬似発注"
    return (
        f"📊 状況\n"
        f"株価データ基準日: {latest_str}\n"
        f"保有銘柄: {db.count_open_positions()} / {cfg.risk.max_open_positions}\n"
        f"未承認シグナル: {int(pending)} 件\n"
        f"モード: {mode}"
    )


def _positions_text(db: Database) -> str:
    df = db.query(
        """
        SELECT code, name, quantity, entry_date, entry_price, stop_price, target_price
        FROM positions WHERE status = 'open' ORDER BY entry_date
        """
    )
    if df.empty:
        return "現在、建玉はありません。"
    lines = ["📁 保有中"]
    for _, r in df.iterrows():
        lines.append(
            f"{display_code(str(r['code']))} {r['name']}\n"
            f"  {int(r['quantity']):,}株 @ {r['entry_price']:,.0f}円 ({r['entry_date']})\n"
            f"  損切 {r['stop_price']:,.0f} / 目標 {r['target_price']:,.0f}"
        )
    return "\n".join(lines)


def _pnl_text(db: Database) -> str:
    df = db.query(
        "SELECT pnl_jpy, exit_reason FROM positions WHERE status = 'closed' AND pnl_jpy IS NOT NULL"
    )
    if df.empty:
        return "確定した取引はまだありません。"
    wins = int((df["pnl_jpy"] > 0).sum())
    total = len(df)
    return (
        f"📈 確定成績\n"
        f"取引数: {total}\n"
        f"勝ち: {wins} / 負け: {total - wins}（勝率 {wins / total:.0%}）\n"
        f"合計損益: {df['pnl_jpy'].sum():,.0f} 円\n"
        f"平均損益: {df['pnl_jpy'].mean():,.0f} 円"
    )


def create_app() -> FastAPI:
    app = FastAPI(title="hinotane webhook", docs_url=None, redoc_url=None)
    app.include_router(router)
    return app


app = create_app()
