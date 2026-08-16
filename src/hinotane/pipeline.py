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
import math
import uuid
from datetime import date, datetime, timedelta

import pandas as pd

from .broker import OrderRequest, get_broker
from .config import AppConfig, now, today
from .datasource.jquants import (
    JQuantsClient,
    JQuantsOutOfRangeError,
    display_code,
)
from .db import Database
from .indicators import enrich
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
        except JQuantsOutOfRangeError as exc:
            log.warning(
                "%s は契約プランのデータ提供範囲外です（提供範囲: %s 〜 %s）。"
                " 無料プランは 12 週間遅延のため、当日データには上位プランが必要です。",
                target, exc.covered_from, exc.covered_to,
            )
            continue
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

    # 既に DB にある日付は取りに行かない。
    # DuckDB の DATE 列は pandas.Timestamp で返るため、必ず date に揃えてから
    # 集合にする。ここを揃え忘れると比較が常に不一致になり、
    # スキップが黙って効かなくなる（実際に起きた）。
    known = db.query("SELECT DISTINCT date FROM daily_quotes")
    already: set[date] = (
        set(pd.to_datetime(known["date"]).dt.date) if not known.empty else set()
    )

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
        except JQuantsOutOfRangeError as exc:
            # 契約プランのデータ提供範囲を超えた。この先の日付も必ず同じ結果に
            # なるので、待つだけ無駄。打ち切る（実機で 60 日ぶん＝16 分を空費した）。
            log.info(
                "%s は契約プランのデータ提供範囲外です（提供範囲: %s 〜 %s）。"
                " これ以降の日付も同じなので取得を打ち切ります。",
                target, exc.covered_from, exc.covered_to,
            )
            if exc.covered_to:
                log.info(
                    "より新しいデータが必要な場合は上位プランをご検討ください: "
                    "https://jpx-jquants.com/#dataset"
                )
            break
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
        if not result.ok and result.retriable:
            # ⚠️ ここを 'failed' にしてはいけない。
            # 本番のスケジュールでは execute は当日の株価を取り込む前に走るため、
            # 「翌営業日の始値がまだ無い」は毎回起きる。これを失敗として確定
            # させると、'failed' は執行対象のステータスから外れているので、
            # 承認したシグナルが二度と発注されずに黙って消える。
            log.info("執行を保留（%s）: %s", result.message, row["code"])
            continue

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

    バックテスト（backtest.py）と同じ約定モデルを使うこと。片方だけ甘いと、
    検証結果と実運用の成績が食い違う。具体的には次の 3 点を揃えている:
      * **エントリー当日も判定対象に含める**（買った初日に損切りは普通に起きる）
      * **約定は判定した翌営業日の始値**（この関数は引け後に走るので、
        損切り価格ちょうどで約定させるには逆指値注文が要る。まだ無い）
      * **切り上げた損切り価格を DB に保存する**（翌日の判定が同じ前提で走るように）
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
            WHERE code = ? AND date >= ?
            ORDER BY date
            """,
            [pos["code"], pos["entry_date"]],
        )
        if bars.empty:
            continue

        # トレーリングに使う ATR。指標は enrich() で一括計算する。
        atr_by_date: dict = {}
        if len(bars) > 1:
            history = db.bars(str(pos["code"]), limit=400)
            if len(history) >= 20:
                enriched = enrich(history)
                atr_by_date = dict(zip(enriched["date"], enriched["atr14"], strict=True))

        try:
            strategy = get_strategy(str(pos["strategy"]))
            max_days = strategy.max_holding_days
            trailing = strategy.trailing_atr_mult
        except KeyError:
            max_days, trailing = 20, None

        stop = float(pos["stop_price"])
        target = float(pos["target_price"])
        exit_reason = ""
        triggered_at: int | None = None   # 決済条件が成立したバーの位置

        highest = float(pos["entry_price"])
        rows = list(bars.iterrows())
        for held, (_, bar) in enumerate(rows):
            if bar["low"] <= stop:
                exit_reason, triggered_at = "stop", held
                break
            if bar["high"] >= target:
                exit_reason, triggered_at = "target", held
                break
            if held >= max_days:
                exit_reason, triggered_at = "timeout", held
                break

            # 決済しなかった日だけ損切りを切り上げる（バックテストと同じ順序）
            if trailing:
                highest = max(highest, float(bar["high"]))
                atr_now = atr_by_date.get(bar["date"], 0.0)
                if atr_now > 0:
                    stop = max(stop, highest - trailing * atr_now)

        # 切り上がった損切り価格は必ず保存する。保存しないと、翌日この関数が
        # 走り直したときに最初の損切り価格から計算をやり直すことになり、
        # 「昨日は決済条件が成立していたのに今日は成立しない」が起きうる。
        if trailing and stop > float(pos["stop_price"]):
            db.execute(
                "UPDATE positions SET stop_price = ? WHERE id = ?", [stop, pos["id"]]
            )

        if triggered_at is None:
            continue

        # ⚠️ 約定は「条件が成立した翌営業日の始値」。
        # この関数は引け後 16:10 に走るので、判定した時点で場は終わっている。
        # 逆指値注文を市場に置く仕組みがまだ無いため（OrderRequest に逆指値の
        # 欄が無い）、実際に出せる最速の注文は翌朝の寄り成行だけ。
        # バックテスト（backtest.py）も同じモデルにしてある。
        if triggered_at + 1 >= len(rows):
            # まだ翌営業日のバーが無い。明日この関数が走ったときに約定させる。
            log.info(
                "%s は%s条件が成立。翌営業日の寄りで決済します。",
                pos["code"], exit_reason,
            )
            continue
        exit_price = float(rows[triggered_at + 1][1]["open"])
        if not math.isfinite(exit_price) or exit_price <= 0:
            exit_price = float(rows[triggered_at + 1][1]["close"])

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


def backfill_listed_history(cfg: AppConfig, db: Database, *, every_days: int = 30) -> int:
    """過去時点の上場銘柄一覧を取り込む（生存者バイアスを消すため）。

    `listed` は取得時点のスナップショットなので、途中で上場廃止になった銘柄が
    入らない。検証は `listed` と JOIN しているため、**消えた銘柄は黙って対象外**
    になり、成績が実態より良く出る。新興株ほど上場廃止が多いので影響は大きい。

    `/equities/master` は日付を指定できるので、月に 1 回ぶん取り直せば
    「その時点で上場していた銘柄」が分かる。5 年で 60 回程度、数分で終わる。
    """
    started = now()
    client = JQuantsClient(cfg.jquants)

    span = db.query("SELECT min(date) AS lo, max(date) AS hi FROM daily_quotes")
    if span.empty or pd.isna(span.iloc[0]["lo"]):
        log.warning("株価データがありません。先に backfill を実行してください。")
        _log_run(db, "listed_history", started, False, "株価データなし")
        return 0
    lo = pd.Timestamp(span.iloc[0]["lo"]).date()
    hi = pd.Timestamp(span.iloc[0]["hi"]).date()

    known = db.query("SELECT DISTINCT as_of FROM listed_history")
    already: set[date] = (
        set(pd.to_datetime(known["as_of"]).dt.date) if not known.empty else set()
    )

    targets: list[date] = []
    cursor = lo
    while cursor <= hi:
        if cursor not in already:
            targets.append(cursor)
        cursor += timedelta(days=every_days)
    if hi not in already and hi not in targets:
        targets.append(hi)

    if not targets:
        log.info("過去の上場銘柄一覧はすべて取得済みです")
        _log_run(db, "listed_history", started, True, "取得済み")
        return 0

    eta = len(targets) * cfg.jquants.min_request_interval_sec / 60.0
    log.info(
        "%s 〜 %s を %d 日おきに %d 回取得します（およそ %.0f 分）",
        lo, hi, every_days, len(targets), eta,
    )

    total = 0
    for i, target in enumerate(targets, start=1):
        try:
            df = client.listed_info(target)
        except Exception as exc:
            log.warning("%s の上場一覧取得に失敗（スキップ）: %s", target, exc)
            continue
        total += db.upsert_listed_history(target, df)
        if i % max(len(targets) // 10, 1) == 0 or i == len(targets):
            log.info("進捗 %d/%d  %s まで完了 / 累計 %s 件", i, len(targets), target, f"{total:,}")

    # 効果を数字で出す。何件が「いま消えている銘柄」なのかが本題。
    gap = db.query(
        """
        SELECT count(DISTINCT h.code) AS n
        FROM listed_history h
        LEFT JOIN listed l ON l.code = h.code
        WHERE l.code IS NULL
        """
    ).iloc[0]["n"]
    log.info("過去の上場一覧を取得しました: %s 件", f"{total:,}")
    log.info(
        "→ うち %d 銘柄は現在の上場一覧に存在しません（上場廃止など）。"
        " これまでの検証はこの銘柄群を黙って除外していました。",
        int(gap),
    )
    _log_run(db, "listed_history", started, True, f"{total}件 / 消滅 {int(gap)}銘柄")
    return total


def backfill_financials(cfg: AppConfig, db: Database) -> int:
    """財務情報サマリを取り込む（Light プラン以上）。

    決算の開示日単位で取る。1 リクエストでその日の全開示が返るので、
    5 年ぶんでも営業日数ぶんの往復で済む。

    ⚠️ 使うときは必ず **開示日** で時点を合わせること。決算期末で並べると、
    まだ公表されていない数字を見て売買することになる。
    """
    started = now()
    client = JQuantsClient(cfg.jquants)

    span = db.query("SELECT min(date) AS lo, max(date) AS hi FROM daily_quotes")
    if span.empty or pd.isna(span.iloc[0]["lo"]):
        log.warning("株価データがありません。先に backfill を実行してください。")
        _log_run(db, "financials", started, False, "株価データなし")
        return 0
    lo = pd.Timestamp(span.iloc[0]["lo"]).date()
    hi = pd.Timestamp(span.iloc[0]["hi"]).date()

    known = db.query("SELECT DISTINCT disclosed_on FROM financials")
    already: set[date] = (
        set(pd.to_datetime(known["disclosed_on"]).dt.date) if not known.empty else set()
    )

    targets: list[date] = []
    cursor = lo
    while cursor <= hi:
        if cursor.weekday() < 5 and cursor not in already:
            targets.append(cursor)
        cursor += timedelta(days=1)

    if not targets:
        log.info("財務情報はすべて取得済みです")
        _log_run(db, "financials", started, True, "取得済み")
        return 0

    eta = len(targets) * cfg.jquants.min_request_interval_sec / 60.0
    log.info(
        "%s 〜 %s の %d 営業日ぶんの決算開示を取得します（およそ %.0f 分）",
        targets[0], targets[-1], len(targets), eta,
    )
    if cfg.jquants.requests_per_min <= 5:
        log.warning(
            "財務情報は Light プラン以上が必要です。"
            " Free プランのままだと空で返り続けます。"
        )

    total = 0
    report_every = max(len(targets) // 20, 1)
    for i, target in enumerate(targets, start=1):
        try:
            df = client.financials_by_date(target)
        except JQuantsOutOfRangeError as exc:
            log.info(
                "%s は契約プランのデータ提供範囲外です（%s 〜 %s）。取得を打ち切ります。",
                target, exc.covered_from, exc.covered_to,
            )
            break
        except Exception as exc:
            log.warning("%s の財務情報取得に失敗（スキップ）: %s", target, exc)
            continue
        total += db.upsert_financials(df)
        if i % report_every == 0 or i == len(targets):
            log.info(
                "進捗 %d/%d（%.0f%%） %s まで完了 / 累計 %s 件",
                i, len(targets), i / len(targets) * 100, target, f"{total:,}",
            )

    log.info("財務情報の取得完了: %s 件", f"{total:,}")
    _log_run(db, "financials", started, True, f"{total}件")
    return total
