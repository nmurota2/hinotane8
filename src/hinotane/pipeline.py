"""バッチ処理の本体。CLI から呼ばれる各ステップ。

1 日の流れ:
    16:00  fetch    引け後の日足を取得
    21:00  screen   スクリーニング → LINE に候補を配信
     (随時) 承認     LINE のボタンを押す（webhook が受ける）
    08:50  execute  承認済みシグナルを発注
    16:10  mark     建玉の損切り・利確・時間切れを判定して決済
"""

from __future__ import annotations

import logging
import uuid
from datetime import date, datetime, timedelta

import pandas as pd

from .broker import OrderRequest, get_broker
from .config import AppConfig, now, today
from .datasource.jquants import JQuantsClient, display_code
from .db import Database
from .notify import console
from .notify.line import LineNotifier
from .screener import screen_and_size
from .strategies.base import get_strategy

log = logging.getLogger(__name__)


def _log_run(db: Database, kind: str, started: datetime, ok: bool, detail: str) -> None:
    db.execute(
        "INSERT INTO runs (id, kind, started_at, finished_at, ok, detail) VALUES (?,?,?,?,?,?)",
        [uuid.uuid4().hex[:16], kind, started, now(), ok, detail[:2000]],
    )


# --------------------------------------------------------------------- fetch


def fetch_listed(cfg: AppConfig, db: Database) -> int:
    client = JQuantsClient(cfg.jquants)
    df = client.listed_info()
    n = db.upsert_listed(df)
    log.info("上場銘柄マスタを更新: %d 件", n)
    return n


def fetch_quotes(cfg: AppConfig, db: Database, days: int = 10) -> int:
    """直近 N 日ぶんの日足を取得する（日次運用用）。

    J-Quants は「日付指定で全銘柄」を 1 リクエストで返すので、
    銘柄ごとに叩くより圧倒的に速い。
    """
    client = JQuantsClient(cfg.jquants)
    total = 0
    run_date = today()

    for offset in range(days, -1, -1):
        target = run_date - timedelta(days=offset)
        if target.weekday() >= 5:      # 土日はスキップ
            continue
        try:
            df = client.daily_quotes_by_date(target)
        except Exception as exc:
            log.warning("%s の取得に失敗（スキップ）: %s", target, exc)
            continue
        if df.empty:
            continue
        total += db.upsert_quotes(df)
        log.info("%s: %d 件", target, len(df))

    log.info("日足を %d 件更新しました", total)
    return total


def backfill(cfg: AppConfig, db: Database, years: float = 2.0) -> int:
    """初回のヒストリカル一括取得。

    数十分かかる処理なので、**取得済みの日付は飛ばす**。
    途中で止めても、もう一度実行すれば続きから再開できる。

    無料プランは 12 週間遅延なので直近データは空で返る。
    バックテスト用の過去データを貯める目的なら無料プランでも十分機能する。
    """
    client = JQuantsClient(cfg.jquants)
    run_date = today()
    start = run_date - timedelta(days=int(365 * years))

    # 既に DB にある日付は取りに行かない
    known = db.query("SELECT DISTINCT date FROM daily_quotes")
    already = set(known["date"].tolist()) if not known.empty else set()

    targets: list[date] = []
    cursor = start
    while cursor <= run_date:
        if cursor.weekday() < 5 and cursor not in already:  # 土日は取引がない
            targets.append(cursor)
        cursor += timedelta(days=1)

    if already:
        log.info("取得済み %d 日ぶんはスキップします", len(already))
    if not targets:
        log.info("取得すべき日付はありません（すべて取得済み）")
        return 0

    rpm = cfg.jquants.requests_per_min
    interval = cfg.jquants.min_request_interval_sec
    eta_min = len(targets) * interval / 60.0
    log.info(
        "%s 〜 %s の %d 日ぶんを取得します"
        "（%d 回/分の上限に対し %.1f 秒間隔 → およそ %d 分）",
        targets[0], targets[-1], len(targets), rpm, interval, round(eta_min),
    )
    if rpm <= 5 and eta_min > 30:
        log.info(
            "Free プランの上限（5 回/分）に合わせて間隔を空けます。"
            " 短くしたい場合は --years 1 にするか、Light プラン（60 回/分）にして"
            " .env に JQUANTS_REQUESTS_PER_MIN=60 を設定してください。"
        )

    total = 0
    empty_streak = 0
    # 進捗は 20 回程度に抑える。多すぎると読めず、少なすぎると止まって見える。
    report_every = max(len(targets) // 20, 1)

    for i, target in enumerate(targets, start=1):
        try:
            df = client.daily_quotes_by_date(target)
        except Exception as exc:
            log.warning("%s の取得に失敗（スキップ）: %s", target, exc)
            df = pd.DataFrame()

        if df.empty:
            empty_streak += 1
        else:
            empty_streak = 0
            total += db.upsert_quotes(df)

        if i % report_every == 0 or i == len(targets):
            log.info(
                "進捗 %d/%d（%.0f%%） %s まで完了 / 累計 %s 件",
                i, len(targets), i / len(targets) * 100, target, f"{total:,}",
            )

    if empty_streak > 40:
        log.warning(
            "直近 %d 営業日ぶんが空でした。無料プラン（12週間遅延）の可能性があります。"
            " 当日データが必要なら Light プラン以上を検討してください。",
            empty_streak,
        )
    log.info("ヒストリカル取得完了: %s 件", f"{total:,}")
    return total


# --------------------------------------------------------------------- screen


def _staleness_warnings(cfg: AppConfig, db: Database) -> list[str]:
    warnings: list[str] = []
    latest = db.latest_quote_date()
    if latest is None:
        return ["株価データが 1 件もありません。先に fetch を実行してください。"]

    age = (today() - latest.date()).days
    if age > cfg.stale_data_warn_days:
        warnings.append(
            f"株価データが {age} 日前（{latest.date()}）のものです。"
            " J-Quants 無料プランは 12 週間遅延のため、当日判断には使えません。"
            " 実運用には Light プラン（月1,650円）以上が必要です。"
        )
    return warnings


def run_screen_and_notify(cfg: AppConfig, db: Database, *, dry_run: bool = False) -> int:
    started = now()
    warnings = _staleness_warnings(cfg, db)
    accepted, rejections = screen_and_size(cfg, db)

    latest = db.latest_quote_date()
    latest_str = latest.date().isoformat() if latest is not None else "不明"
    header = (
        f"📈 本日の候補 {len(accepted)} 銘柄\n"
        f"基準日: {latest_str} ／ 運用資金 {cfg.risk.equity_jpy:,.0f}円\n"
        f"モード: {'擬似発注' if not cfg.execution.live_trading else '⚠️実発注'}"
    )

    if dry_run or not cfg.line.can_push:
        print(console.render(accepted, header_text=header, warnings=warnings))
    else:
        LineNotifier(cfg.line).push_signals(accepted, header_text=header, warnings=warnings)

    for r in rejections[:20]:
        log.info("除外 %s (%s): %s", r.code, r.strategy, r.reason)

    _log_run(db, "screen", started, True, f"{len(accepted)}件を通知 / {len(rejections)}件を除外")
    return len(accepted)


# --------------------------------------------------------------------- execute


def run_execute(cfg: AppConfig, db: Database) -> int:
    """承認済み（または自動執行設定時は pending）のシグナルを発注する。"""
    started = now()
    broker = get_broker(cfg, db)
    notifier = LineNotifier(cfg.line)

    statuses = ["approved"]
    if cfg.execution.auto_execute_without_approval:
        log.warning("⚠️ AUTO_EXECUTE_WITHOUT_APPROVAL=true: 承認なしで pending も執行します")
        statuses.append("pending")

    placeholders = ",".join("?" for _ in statuses)
    pending = db.query(
        f"""
        SELECT id, code, name, quantity, entry_price, expires_at, status
        FROM signals
        WHERE status IN ({placeholders})
        ORDER BY created_at
        """,
        statuses,
    )
    if pending.empty:
        log.info("執行対象のシグナルはありません")
        _log_run(db, "execute", started, True, "対象なし")
        return 0

    executed = 0
    exec_time = now()

    for _, row in pending.iterrows():
        sid = row["id"]

        # 期限切れの承認は執行しない（寝ぼけて承認 → 数日後に約定、を防ぐ）
        expires = row["expires_at"]
        expired = (
            expires is not None
            and pd.notna(expires)
            and exec_time > pd.Timestamp(expires).to_pydatetime()
        )
        if expired:
            db.execute("UPDATE signals SET status = 'expired' WHERE id = ?", [sid])
            log.info("期限切れのため執行しません: %s", row["code"])
            continue

        result = broker.buy(
            OrderRequest(
                code=str(row["code"]),
                name=str(row["name"]),
                side="buy",
                quantity=int(row["quantity"]),
                signal_id=sid,
            )
        )
        db.execute(
            "UPDATE signals SET status = ? WHERE id = ?",
            ["executed" if result.ok else "failed", sid],
        )

        mark = "✅" if result.ok else "❌"
        text = f"{mark} {display_code(str(row['code']))} {row['name']}\n{result.message}"
        log.info(text.replace("\n", " "))
        notifier.push_text(text)
        if result.ok:
            executed += 1

    _log_run(db, "execute", started, True, f"{executed}件を執行")
    return executed


# --------------------------------------------------------------------- mark


def run_mark(cfg: AppConfig, db: Database) -> int:
    """建玉を最新の株価で評価し、損切り・利確・時間切れを判定して決済する。

    判定は必ず日足の高値・安値で行う。終値だけで判定すると、
    ザラ場中に損切り価格を割っていた事実を見逃して成績が実態より良く出る。
    同じ日に損切りと利確の両方に触れた場合は、保守的に損切り側を採用する。
    """
    started = now()
    broker = get_broker(cfg, db)
    notifier = LineNotifier(cfg.line)

    positions = db.query("SELECT * FROM positions WHERE status = 'open'")
    if positions.empty:
        _log_run(db, "mark", started, True, "建玉なし")
        return 0

    closed = 0
    for _, pos in positions.iterrows():
        bars = db.query(
            """
            SELECT date, open, high, low, close
            FROM daily_quotes
            WHERE code = ? AND date > ?
            ORDER BY date
            """,
            [pos["code"], pos["entry_date"]],
        )
        if bars.empty:
            continue

        try:
            max_days = get_strategy(str(pos["strategy"])).max_holding_days
        except KeyError:
            max_days = 20

        stop = float(pos["stop_price"])
        target = float(pos["target_price"])
        exit_price: float | None = None
        exit_reason = ""

        for held, (_, bar) in enumerate(bars.iterrows(), start=1):
            if bar["low"] <= stop:
                exit_price, exit_reason = stop, "stop"
                break
            if bar["high"] >= target:
                exit_price, exit_reason = target, "target"
                break
            if held >= max_days:
                exit_price, exit_reason = float(bar["close"]), "timeout"
                break

        if exit_price is None:
            continue

        result = broker.sell(
            OrderRequest(
                code=str(pos["code"]),
                name=str(pos["name"]),
                side="sell",
                quantity=int(pos["quantity"]),
                limit_price=exit_price,
                signal_id=str(pos["id"]),
            )
        )
        if not result.ok:
            log.error("決済に失敗: %s %s", pos["code"], result.message)
            continue

        db.execute(
            "UPDATE positions SET exit_reason = ?, exit_date = ? WHERE id = ?",
            [exit_reason, today(), pos["id"]],
        )
        closed += 1

        label = {"stop": "損切り", "target": "利確", "timeout": "時間切れ"}[exit_reason]
        notifier.push_text(
            f"🔚 {display_code(str(pos['code']))} {pos['name']} を{label}で決済\n{result.message}"
        )

    _log_run(db, "mark", started, True, f"{closed}件を決済")
    return closed
