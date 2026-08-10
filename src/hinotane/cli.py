"""コマンドラインインターフェース。

よく使う順:
    hinotane init                     初期化（DB 作成）
    hinotane doctor                   設定の健康診断（まずこれ）
    hinotane line-test                LINE にテスト送信
    hinotane backfill --years 2       過去データの一括取得（初回のみ・数十分）
    hinotane fetch                    日次の株価更新
    hinotane screen --dry-run         スクリーニング結果を画面に表示（LINE に送らない）
    hinotane screen                   スクリーニング → LINE に配信
    hinotane execute                  承認済みシグナルを発注
    hinotane mark                     建玉の損切り・利確・時間切れを判定
    hinotane backtest                 バックテスト
    hinotane walkforward              イン／アウトオブサンプル検証
    hinotane serve                    LINE Webhook サーバーを起動
    hinotane doctor                   設定の健康診断
"""

from __future__ import annotations

import argparse
import logging
import sys

from .config import load_config, today
from .db import Database
from .strategies import available


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )


def cmd_init(args, cfg, db) -> int:
    db.init_schema()
    print(f"✅ データベースを初期化しました: {cfg.db_path}")
    return 0


def cmd_doctor(args, cfg, db) -> int:
    """設定が正しいかを一通りチェックする。最初に必ずこれを実行する。

    既定では実際に J-Quants API を 1 回叩いて、鍵が本当に通るかまで確かめる。
    「設定してあるのに動かない」の大半はここで判明する。
    """
    ok = True
    print("=== 設定の健康診断 ===\n")

    # --- J-Quants -----------------------------------------------------
    if not cfg.jquants.configured:
        print("❌ J-Quants: 未設定")
        print("     .env に JQUANTS_API_KEY（V2）を設定してください。")
        print("     V1 の場合は JQUANTS_MAIL_ADDRESS + JQUANTS_PASSWORD。")
        ok = False
    else:
        mode = "APIキー (V2)" if cfg.jquants.auth_mode == "apikey" else "トークン (V1)"
        if args.offline:
            print(f"✅ J-Quants: 設定あり（認証方式: {mode}）※接続確認はスキップ")
        else:
            from .datasource.jquants import (
                JQuantsAuthError,
                JQuantsClient,
                JQuantsNetworkError,
            )

            try:
                listed = JQuantsClient(cfg.jquants).listed_info()
                print(f"✅ J-Quants: 接続成功（認証方式: {mode} / {len(listed):,} 銘柄）")
            except JQuantsAuthError as exc:
                # 設定の問題。「アカウントは作ったのに動かない」の大半はプラン未選択。
                print(f"❌ J-Quants: 認証に失敗しました（認証方式: {mode}）")
                print(f"     {exc}")
                print("     考えられる原因を可能性の高い順に:")
                print("     1. ダッシュボードでプラン選択が未完了")
                print("        （Free プランでも「選択」の操作が必要です）")
                print("     2. API キーの貼り間違い・コピー漏れ")
                print("     3. キーを再発行して古いものが残っている")
                ok = False
            except JQuantsNetworkError as exc:
                # 到達性の問題。設定を疑わせない。
                print("❌ J-Quants: サーバーに接続できませんでした")
                print(f"     {exc}")
                print("     設定ではなくネットワーク側の問題です:")
                print("     ・インターネットに繋がっているか")
                print("     ・社内プロキシ / VPN / ファイアウォールで遮断されていないか")
                print("     ・J-Quants 側が一時的に落ちていないか")
                ok = False
            except Exception as exc:
                print(f"❌ J-Quants: 予期しないエラー（認証方式: {mode}）")
                print(f"     {exc}")
                ok = False

    # --- LINE ---------------------------------------------------------
    if not cfg.line.configured:
        print("⚠️  LINE: 未設定。通知は画面出力のみになります")
        print("     LINE_CHANNEL_ACCESS_TOKEN と LINE_CHANNEL_SECRET を設定してください。")
    elif cfg.line.setup_mode:
        print("🔧 LINE: セットアップモード（宛先 userId が未登録）")
        print("     LINE Developers コンソール →「チャネル基本設定」タブ →")
        print("     『あなたのユーザーID』をコピーして、.env の")
        print("     LINE_ALLOWED_USER_IDS に貼り付けてください。")
        print("     （分からなければ `hinotane serve` を起動して Bot に話しかけると返信で教えます）")
    else:
        print(f"✅ LINE: 設定済み（宛先 {len(cfg.line.allowed_user_ids)} 件）")
        print("     → `hinotane line-test` で実際にメッセージが届くか試せます")

    try:
        db.init_schema()
        latest = db.latest_quote_date()
        quotes = db.query("SELECT count(*) AS n FROM daily_quotes").iloc[0]["n"]
        listed = db.query("SELECT count(*) AS n FROM listed").iloc[0]["n"]
        print(f"✅ DB: {cfg.db_path}")
        print(f"    銘柄マスタ {int(listed):,} 件 / 日足 {int(quotes):,} 件")
        if latest is not None:
            age = (today() - latest.date()).days
            flag = "⚠️ " if age > cfg.stale_data_warn_days else "   "
            print(f"{flag}   最新の株価データ: {latest.date()}（{age} 日前）")
            if age > 60:
                print("     → J-Quants 無料プランは 12 週間遅延です。")
                print("       当日判断には Light プラン（月1,650円）以上が必要です。")
        else:
            print("⚠️    株価データが空です。`hinotane backfill` を実行してください")
    except Exception as exc:
        print(f"❌ DB: {exc}")
        ok = False

    print(f"\n    戦略: {', '.join(cfg.screener.strategies)}（利用可能: {', '.join(available())}）")
    print(f"    運用資金: {cfg.risk.equity_jpy:,.0f} 円")
    print(f"    1トレードのリスク: {cfg.risk.risk_per_trade:.1%}"
          f" = {cfg.risk.equity_jpy * cfg.risk.risk_per_trade:,.0f} 円")
    print(f"    最大保有銘柄数: {cfg.risk.max_open_positions}")
    print(f"    1日の最大通知数: {cfg.risk.max_signals_per_day}")

    if cfg.execution.live_trading:
        print("\n🚨 LIVE_TRADING=true — 実際の注文が発注されます")
        if cfg.execution.auto_execute_without_approval:
            print("🚨 AUTO_EXECUTE_WITHOUT_APPROVAL=true — 承認なしで発注されます")
    else:
        print("\n🛡️  擬似発注モード（実際のお金は動きません）")

    return 0 if ok else 1


def cmd_line_test(args, cfg, db) -> int:
    """LINE に実際にテストメッセージを送って、届くかを確かめる。"""
    from .notify.line import LineNotifier

    if not cfg.line.configured:
        print("❌ LINE_CHANNEL_ACCESS_TOKEN / LINE_CHANNEL_SECRET が未設定です")
        return 1
    if cfg.line.setup_mode:
        print("❌ LINE_ALLOWED_USER_IDS が未設定のため送信先がありません")
        print("   LINE Developers →「チャネル基本設定」→『あなたのユーザーID』を")
        print("   .env の LINE_ALLOWED_USER_IDS に設定してください。")
        return 1

    ok = LineNotifier(cfg.line).push_text(
        "✅ hinotane の接続テストです。\n"
        "このメッセージが届いていれば通知の設定は完了しています。\n\n"
        "試しに「状況」と送ってみてください。"
    )
    if ok:
        print(f"✅ 送信しました（宛先 {len(cfg.line.allowed_user_ids)} 件）。LINE を確認してください。")
        return 0
    print("❌ 送信に失敗しました。よくある原因:")
    print("   ・アクセストークンが間違っている（発行し直すと古いトークンは無効になります）")
    print("   ・userId が別のチャネルのもの")
    print("   ・Bot を友だち追加していない")
    return 1


def cmd_fetch(args, cfg, db) -> int:
    from .pipeline import fetch_listed, fetch_quotes

    db.init_schema()
    fetch_listed(cfg, db)
    fetch_quotes(cfg, db, days=args.days)
    return 0


def cmd_backfill(args, cfg, db) -> int:
    from .pipeline import backfill, fetch_listed

    db.init_schema()
    fetch_listed(cfg, db)
    backfill(cfg, db, years=args.years)
    return 0


def cmd_screen(args, cfg, db) -> int:
    from .pipeline import run_screen_and_notify

    db.init_schema()
    run_screen_and_notify(cfg, db, dry_run=args.dry_run)
    return 0


def cmd_execute(args, cfg, db) -> int:
    from .pipeline import run_execute

    db.init_schema()
    n = run_execute(cfg, db)
    print(f"{n} 件を執行しました")
    return 0


def cmd_mark(args, cfg, db) -> int:
    from .pipeline import run_mark

    db.init_schema()
    n = run_mark(cfg, db)
    print(f"{n} 件を決済しました")
    return 0


def cmd_backtest(args, cfg, db) -> int:
    from .backtest import run_backtest

    strategies = args.strategies.split(",") if args.strategies else None
    result = run_backtest(
        cfg, db, strategy_names=strategies, label=args.strategies or "全戦略",
        max_symbols=args.max_symbols,
    )
    print(result.summary())

    if result.trades:
        by_strategy: dict[str, list] = {}
        for t in result.trades:
            by_strategy.setdefault(t.strategy, []).append(t)
        print("\n--- 戦略別 ---")
        for name, ts in sorted(by_strategy.items()):
            wins = sum(1 for t in ts if t.pnl_jpy > 0)
            total_pnl = sum(t.pnl_jpy for t in ts)
            avg_r = sum(t.r_multiple for t in ts) / len(ts)
            print(
                f"  {name:<10} 取引 {len(ts):>4}  勝率 {wins / len(ts):>5.1%}"
                f"  損益 {total_pnl:>+12,.0f}円  期待値 {avg_r:>+5.2f}R"
            )
    return 0


def cmd_walkforward(args, cfg, db) -> int:
    from .backtest import walk_forward

    strategies = args.strategies.split(",") if args.strategies else None
    in_s, out_s = walk_forward(
        cfg, db, strategy_names=strategies, split=args.split, max_symbols=args.max_symbols
    )
    print(in_s.summary())
    print()
    print(out_s.summary())

    print("\n=== 判定 ===")
    if not out_s.trades:
        print("⚠️  アウトオブサンプルで取引が発生せず、判断できません。")
        return 0
    if out_s.expectancy_r <= 0:
        print("❌ アウトオブサンプルの期待値がマイナスです。この戦略は実運用に載せないでください。")
    elif in_s.expectancy_r > 0 and out_s.expectancy_r < in_s.expectancy_r * 0.5:
        print("⚠️  後半で期待値が半分以下に落ちています。過剰最適化の疑いが濃厚です。")
    else:
        print("✅ 前半・後半で期待値が保たれています。ただしこれは必要条件であって十分条件ではありません。")
    return 0


def cmd_serve(args, cfg, db) -> int:
    import uvicorn

    db.init_schema()
    print(f"LINE Webhook サーバーを起動します: http://{cfg.webhook_host}:{cfg.webhook_port}")
    print("LINE Developers の Webhook URL には https://<あなたのドメイン>/line/webhook を設定してください")
    uvicorn.run(
        "hinotane.webhook:app", host=cfg.webhook_host, port=cfg.webhook_port, log_level="info"
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hinotane", description="日本株スイングトレードの半自動化")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="データベースを初期化").set_defaults(func=cmd_init)

    d = sub.add_parser("doctor", help="設定の健康診断")
    d.add_argument("--offline", action="store_true", help="外部APIへの接続確認をスキップ")
    d.set_defaults(func=cmd_doctor)

    sub.add_parser("line-test", help="LINE にテストメッセージを送る").set_defaults(
        func=cmd_line_test
    )

    f = sub.add_parser("fetch", help="日次の株価更新")
    f.add_argument("--days", type=int, default=10, help="何日前まで取得するか")
    f.set_defaults(func=cmd_fetch)

    b = sub.add_parser("backfill", help="過去データの一括取得（初回のみ）")
    b.add_argument("--years", type=float, default=2.0)
    b.set_defaults(func=cmd_backfill)

    s = sub.add_parser("screen", help="スクリーニングして通知")
    s.add_argument("--dry-run", action="store_true", help="LINE に送らず画面に出すだけ")
    s.set_defaults(func=cmd_screen)

    sub.add_parser("execute", help="承認済みシグナルを発注").set_defaults(func=cmd_execute)
    sub.add_parser("mark", help="建玉の決済判定").set_defaults(func=cmd_mark)

    bt = sub.add_parser("backtest", help="バックテスト")
    bt.add_argument("--strategies", help="カンマ区切り。省略時は設定値")
    bt.add_argument("--max-symbols", type=int, default=600, help="流動性上位から何銘柄を対象にするか")
    bt.set_defaults(func=cmd_backtest)

    wf = sub.add_parser("walkforward", help="イン／アウトオブサンプル検証")
    wf.add_argument("--strategies")
    wf.add_argument("--split", type=float, default=0.6, help="前半（イン・サンプル）の割合")
    wf.add_argument("--max-symbols", type=int, default=600)
    wf.set_defaults(func=cmd_walkforward)

    sub.add_parser("serve", help="LINE Webhook サーバーを起動").set_defaults(func=cmd_serve)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = load_config()
    _setup_logging(cfg.log_level)
    db = Database(cfg.db_path)
    try:
        return args.func(args, cfg, db)
    except KeyboardInterrupt:
        print("\n中断しました")
        return 130
    except Exception as exc:
        logging.getLogger("hinotane").exception("%s", exc)
        print(f"\n❌ エラー: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
