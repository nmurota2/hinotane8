# 株取引の自動化／半自動化システム — 事前リサーチメモ

作成日: 2026-08-10
調査方法: Web検索（記事本文の直接取得は実行環境のプロキシ制限により不可。検索スニペット＋周辺記事から再構成）

---

## 1. 提示された3記事の中身

### ① note / dolphin415「毎晩『明日の有望株』がLINEに届くアプリを、非エンジニアがClaude Codeで1日で作った話」
- 公開: 2026-04-25
- **作ったもの**: 毎晩22時に「明日の有望株」がLINEに届くアプリ
- **データ**: J-Quants API（無料プラン、株価・出来高）
- **開発**: Claude Code。非エンジニアが1日で構築
- **実行環境**: macOS の `launchd`（タスクスケジューラ）。ネット接続確認を挟んでから実行する回避策を入れている
- **自動化範囲**: 現時点は **通知のみ（＝半自動）**
- **ロードマップ**: Phase3 = 信用残・需給データ（J-Quants有料）、Phase4 = バックテスト、Phase5 = kabuステーションAPIで自動発注
- **コスト**: Claude Code Pro $20/月 ＋ J-Quants Light 1,650円/月（Phase4以降）
- 👉 **3記事の中で今回のご要望に一番近い**。実質これの上位互換を作るのがゴールになる

### ② LinkedIn / Ben Harden "How I Built a Complete Stock Trading Analysis App in Under 10 Hours"
- 公開: 2025-03-02
- **作ったもの**: 株式の**分析**アプリ。AIツール（Claude等）で10時間以内に構築
- **自動化範囲**: **分析・学習用途に限定。実際の発注は行わない**と明言
- 米国株前提。日本の証券口座事情には触れていない
- 👉 「AIでここまで作れる」という実証系の記事。**発注部分の参考にはならない**

### ③ genai-ai.co.jp / Claude Code系ブログ
- 同社は Claude Code Max 20x（月3万円前後）で全社業務自動化（営業資料・広告運用・記帳・ブログ・秘書業務、月160時間削減）という文脈のブログを多数展開
- 関連記事:「【2026年8月最新】AI自動株取引の仕組みと始め方｜個人投資家が今すぐ実践できる方法」
- 👉 **入門/概論寄り**。具体的な実装の詳細度は①に劣る

### ⚠️ 3記事に共通する読み方の注意
3記事とも「**作れた**」話であって「**継続的に儲かった**」話ではありません。特に検索で並んで出てくる「勝率82%・2日で+10%」系の記事は、検証期間が極端に短く、統計的な意味はほぼありません。**ツールの実現可能性と、戦略の収益性は完全に別問題**として扱います。

---

## 2. 追加で見つけた、より有用な情報源

### YouTube
| 動画 | URL | 内容 |
|---|---|---|
| 【衝撃】コード書けない素人がAIにトレード判断させるbot作ってみた【Claude Code】 | https://www.youtube.com/watch?v=dO-O_Kaqlsg | 非エンジニアがAIに売買判断させるbotを構築 |
| 【実験】Claude Codeに投資をさせたら利益は出せるのか実験してみた！ | https://www.youtube.com/watch?v=9mSfs7wGkrU | 仮想資金での検証実験 |
| 日本株自動売買 今からやるならCLAUDEとどう作る？ | https://www.youtube.com/watch?v=dFSVVGh465U | 2026年時点の構成論 |

### 記事（①より実装が具体的なもの）
- [「Claude×J-Quants×Webull」でAIによる自動の株取引システムを構築してみた](https://note.com/aisamanogeboku/n/n12f615c2ba82) — データ(J-Quants)と発注(Webull)を分離する構成
- [Claude Code × 株デイトレ自動取引 完全構築ガイド](https://note.com/kaba_iphone/n/n660eea4781b1) — kabuステーションAPIでの実発注まで
- [第0話 kabuステーション API × Pythonで株の自動売買システムを作った話](https://note.com/natsulline1019/n/n3a0870938ea1) — 連載形式で実装詳細
- [非エンジニアの自分が、AIと2ヶ月で日本株3,700銘柄のサイトを作るまで](https://zenn.dev/kabubase/articles/zenn-kabubase-architecture) — アーキテクチャ設計の参考
- [J-Quants API 入門（V2対応）](https://zenn.dev/shimada_ml/articles/6df909a5a96268) — 2025/12のV2リニューアル対応
- [J-Quants APIで取得できるデータ項目まとめ【日本株4,400銘柄の実測データ付き】](https://qiita.com/zundalab/items/56c909c3bc5284ee7ab9)
- [立花証券e支店とKabuステーションどちらで自動売買すべきか](https://zenn.dev/morim34/articles/955e7a6a18fe52) — 発注APIの選定比較
- [Claude APIで株価分析を全自動化した話](https://qiita.com/yosei_ikegami/items/9a185420493a2f99977f) — GitHub Actionsで毎朝分析、月$2以下

---

## 3. 技術的な現実（設計を左右する重要事実）

### 3-1. データ取得（株価・財務・需給）

| 手段 | 費用 | リアルタイム性 | 環境 | 備考 |
|---|---|---|---|---|
| **J-Quants API（JPX公式）** | 無料 / 1,650円〜 | 無料は**12週間遅延**。有料は当日データ | どこでも | 2025/12 V2化、2026/1に分足・Tick・CSV追加（Light以上）。日本株4,400銘柄の株価・財務・信用残を一括取得 |
| **立花証券 e支店 API** | **無料**（口座開設のみ） | **リアルタイム株価・板** | **Linux/Python OK** | 20年分の日足も無料。クラウド常時運用に最適 |
| **kabuステーションAPI** | 無料（Professionalプラン適用時） | リアルタイム・板 | **Windows専用** | 信用口座開設＋1回取引でProfessional適用 |
| **yfinance** | 無料 | 15〜20分遅延・欠損あり | どこでも | 手軽だが本番運用には弱い |
| **JPX 15分遅延API** | 有料 | 15分遅延 | どこでも | 公式 |

### 3-2. 実際に「発注」できるAPI（日本株）

| 証券会社 | 個人向け発注API | 環境制約 | 判定 |
|---|---|---|---|
| **立花証券 e支店** | ✅ 無料の日本株API（現物・信用） | **Linux + Python で完結。クラウド運用可** | ⭐ **本命** |
| **三菱UFJ eスマート証券（旧auカブコム）** | ✅ kabuステーションAPI | ❌ Windows専用・kabuステーション**常時起動必須**・毎営業日AM4-6時にセッション切断・二段階認証で完全自動ログインが困難 | 常時稼働のハードルが高い |
| **楽天証券** | ⚠️ MarketSpeed II RSS（Excelアドイン、COM経由） | ❌ Windows専用 | 公式RESTなし |
| **SBI証券** | ❌ 個人向けRESTなし（2026年時点） | — | 先物・オプションのみ別途 |
| **moomoo証券** | ✅ moomoo OpenAPI（2026/3〜） | 米国株・香港株・中国A株のみ。**日本株非対応** | 米国株なら本命 |

> **重要**: よくある誤情報として「SBI証券APIで自動売買」という記事が多数ありますが、2026年時点で個人向けの公開REST APIは存在しません。

### 3-3. 米国株を狙う場合の新しい選択肢
- **moomoo OpenAPI**（2026年3月開始）— 日本の証券会社として米国株API取引に対応。日本居住者が使える、Python対応、ペーパートレードあり
- **moomoo API Skill**（2026年4月開始）— **Claude Code / Cursor から自然言語で米国株を売買できるSkillパック**。「AAPLを185ドル指値で100株」「RSI<30でウォッチリスト銘柄を自動買い」といった指示が通る。**全注文はOpenD（ローカル接続ソフト）での手動承認が必須**という安全機構つき
- Interactive Brokers / Alpaca も選択肢だが、日本居住者にはmoomooの方が導入が容易

### 3-4. 通知（LINE）
- ❌ **LINE Notify は 2025年3月31日で完全終了**。①の記事以降に書かれた「LINE Notifyで通知」系の情報は全て無効
- ✅ 代替は **LINE Messaging API**（LINE公式アカウント経由）
  - コミュニケーションプラン: **月額0円 / 200通まで**（1日6通程度なら無料で収まる）
  - ライト: 5,000円/月 5,000通 ／ スタンダード: 15,000円/月 30,000通
  - **Reply API（ユーザー発話への返信）は課金対象外** → 「LINEで『今日の候補は？』と聞いたら返す」形にすれば実質無制限
- 代替候補: Discord Webhook / Slack / Telegram（いずれも無料・通数無制限）

### 3-5. 実行基盤
- **GitHub Actions cron**: 無料枠 月2,000分。ただし schedule はベストエフォートで**遅延・ドロップあり**（寄り付き前の配信には不向き、夜間バッチなら可）
- **VPS（さくら/ConoHa 月数百円〜）**: 立花証券API構成なら Linux VPS で完結 ⭐
- **自宅Mac（launchd）**: ①の構成。PCが起動していないと動かない
- **Windows VPS**: kabuステーションAPI構成では必須。月1,500〜3,000円程度

### 3-6. バックテスト
- **Backtesting.py** — 学習コスト低。数十行で検証可能。日本株での実例多数 ⭐ まずはこれ
- **Backtrader** — 多機能・日本語情報多い。複数銘柄/複数戦略
- **vectorbt** — ベクトル化で数千通りのパラメータを高速検証。本格運用向け

---

## 4. 法律・リスクの整理

### 4-1. 金融商品取引法
- **自分専用に作って自分で使う**分には、投資助言・代理業の登録は不要（事業としての「投資判断の提供」に当たらないため）
- **他人に売る/貸す/会員制で配る**と、投資助言・代理業の登録が必要になる可能性が高い
- 「分析機能のみ・パラメータはユーザー任せ・完全売り切り」なら登録不要と解釈される傾向はあるが、グレー。**配布予定があるなら事前に専門家確認が必須**
- 参考: [牛島総合法律事務所](https://www.ushijima-law.gr.jp/topics/20250421investment_advisory/) / [緒方法務事務所](https://www.ogata-legal.com/top/Business-Insights/fx-ai-tool-reg)

### 4-2. 技術的にハマる落とし穴（実装時に必ず対策する）
1. **過剰最適化（カーブフィッティング）** — バックテストだけ勝てて実弾で負ける最大要因。イン/アウトオブサンプル分割は必須
2. **生存者バイアス** — 上場廃止銘柄を除外したデータでバックテストすると成績が過大評価される
3. **ルックアヘッドバイアス** — 「その時点で知り得なかった情報」を使ってしまう（決算発表日と適時開示のタイムスタンプに注意）
4. **注文の暴走** — 1日の最大発注回数・最大ポジション・損切り強制のガードレールをコード側に必ず入れる
5. **データ遅延** — J-Quants無料の12週間遅延は「翌日の売買候補」を出すには使えない（①の記事もここが最大のボトルネックのはず）

---

## 5. 推奨する段階的アプローチ

```
Phase 1【半自動・通知型】← まずここ
  データ取得 → スクリーニング → 毎晩LINEに候補銘柄を配信
  発注は人間が手動。損失リスクゼロで戦略の当たり外れを実データで検証できる

Phase 2【検証基盤】
  バックテスト＋ペーパートレード（仮想売買の成績を毎日記録）
  Phase 1の通知を「実際に買っていたらどうなったか」で自動採点

Phase 3【承認付き半自動発注】
  LINEから「承認」を押したら発注APIが動く
  人間が最終判断を握ったまま、発注操作だけ自動化

Phase 4【条件付き完全自動】
  Phase 2で統計的に有意な成績が出た戦略に限り、少額から自動発注
  ガードレール（最大損失・最大建玉・強制損切り）必須
```

---

## 6. 出典一覧

- [毎晩「明日の有望株」がLINEに届くアプリを、非エンジニアがClaude Codeで1日で作った話｜note dolphin415](https://note.com/dolphin415/n/n96883fcb5db0)
- [How I Built a Complete Stock Trading Analysis App in Under 10 Hours｜LinkedIn Ben Harden](https://jp.linkedin.com/pulse/how-i-built-complete-stock-trading-analysis-app-under-ben-harden-7gewf)
- [genai-ai.co.jp Claude Code 業務自動化ブログ](https://genai-ai.co.jp/ai-kanri/blog/cc-yt-automation-masterclass-88/) / [AI自動株取引の仕組みと始め方](https://genai-ai.co.jp/ai-kanri/blog/cc-ai-stock-trading/)
- [J-Quants API｜日本取引所グループ](https://www.jpx.co.jp/markets/other-data-services/j-quants-api/index.html) / [CSV形式提供開始及び分足・Tick追加のお知らせ](https://www.jpx.co.jp/corporate/news/news-releases/6020/20260119.html)
- [APIサービス｜立花証券e支店](https://www.e-shiten.jp/api/)
- [kabuステーション®API｜三菱UFJ eスマート証券](https://kabu.com/item/kabustation_api/default.html) / [FAQ](https://kabucom.github.io/kabusapi/ptal/faq.html)
- [マーケットスピード II RSS｜楽天証券](https://marketspeed.jp/ms2_rss/)
- [moomoo OpenAPI](https://www.moomoo.com/jp/newsroom/moomoo-openapi) / [Moomoo API Skill](https://www.moomoo.com/jp/newsroom/moomoo-api-skill)
- [End of service for LINE Notify](https://notify-bot.line.me/closing-announce) / [LINE公式アカウント料金プラン【2026年版】](https://blog.socialplus.jp/knowledge/line-account-plan/)
- [【2026最新】kabuステーションAPI vs 楽天RSSを徹底比較](https://kabutech.jp/tools/kabuapi-rss-comparison)
- [投資助言業登録が必要となるか否かの判断と行政対応の留意点｜牛島総合法律事務所](https://www.ushijima-law.gr.jp/topics/20250421investment_advisory/)
- [過剰最適化｜Myforex](https://myforex.com/ja/glossary/curve-fitting.html)
