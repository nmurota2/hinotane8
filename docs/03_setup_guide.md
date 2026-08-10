# セットアップ手順書（クリック単位）

このドキュメントは、`README.md` の「必要なもの 1〜3」を実際に取得するための手順書です。
**画面の文言はサービス側の更新で多少変わることがあります。**
迷ったら、そこで止めて画面のスクリーンショットを送ってください。その場で読み解きます。

---

## 🚀 最短ルート：VPS もドメインも要りません（所要 20 分）

いきなり VPS を借りる必要はありません。**通知を受け取るだけなら、お手元の PC で動きます。**

| やりたいこと | 必要なもの |
|---|---|
| **A. 毎晩の候補を LINE で受け取る** | J-Quants ＋ LINEトークン だけ（**PC で手動実行**） |
| B. 承認ボタンを押して発注まで | A ＋ VPS ＋ ドメイン（HTTPS の受け口が要るため） |

**まず A をやってください。** LINE に候補が届くようになれば、システムの価値の大半は手に入ります。
B は「通知の中身に納得できたら」で構いません。

> なぜ B にドメインが必要か: LINE の「買う」ボタンは、LINE のサーバーから
> あなたのサーバーへ HTTPS でリクエストが飛ぶ仕組みです。受け口となる
> 固定の HTTPS アドレスが要るので、VPS とドメイン（年1,000〜1,500円程度）が必要になります。
> 一方、こちらから送る通知（push）は受け口が不要なので、PC だけで動きます。

---

## 1. J-Quants（株価データ）— 所要 5分・無料

### 1-1. アカウントを作る

1. https://jpx-jquants.com/auth/signin を開く
2. **新規登録**（Sign up）を選ぶ
3. メールアドレスとパスワードを入力して送信
4. 登録したメールアドレスに確認メールが届くので、**本文の URL をクリック**して本登録を完了

### 1-2. ⚠️ プランを選択する（ここを飛ばすと API が動きません）

**最頻出のつまずきポイントです。** アカウントを作っただけでは API は使えません。

1. ログインしてダッシュボードを開く
2. プラン選択の画面で **Free（無料）** を選んで確定する
3. 「契約中のプラン」に Free と表示されれば OK

> 後から Light（月1,650円）に上げれば当日データが使えるようになります。
> **まずは Free で構いません。** バックテストは Free のデータで十分回ります。

### 1-3. API キーを発行する

1. ダッシュボードのメニューから **［設定］→［API キー］** を開く
2. **［発行］**（Create / Generate）を押す
3. 表示された文字列を**すべてコピー**

### 1-4. `.env` に貼る

**セットアップスクリプトを使うのが一番楽です**（キーは画面に表示されません）。

```bash
bash scripts/setup.sh
```

手動でやる場合は `.env` を開いて直接書きます。

```dotenv
JQUANTS_API_KEY=ここに貼り付け
```

> 📌 もしダッシュボードに「API キー」の項目が見当たらず、代わりに
> リフレッシュトークンの案内が出ている場合は、V1 のアカウントです。その場合は
> `JQUANTS_API_KEY` を空のままにして、以下を埋めてください（どちらでも動きます）。
> ```dotenv
> JQUANTS_MAIL_ADDRESS=登録したメールアドレス
> JQUANTS_PASSWORD=登録したパスワード
> ```

### 1-5. 動作確認

```bash
source .venv/bin/activate    # 毎回、作業前にこれを実行します
hinotane doctor
```

> `hinotane: command not found` と出たら、上の `source` を忘れています。
> 面倒なら `.venv/bin/hinotane doctor` とフルパスで書いても同じです。

```
✅ J-Quants: 接続成功（認証方式: APIキー (V2) / 4,412 銘柄）
```

こう出れば完了です。`❌ 接続失敗` の場合は、**1-2 のプラン選択が終わっているか**を最初に疑ってください。

---

## 2. LINE Messaging API（通知）— 所要 15分・無料

> LINE Notify は 2025年3月31日に終了しています。ネット上の「LINE Notify で通知」系の
> 記事はすべて使えません。現在は Messaging API を使います。

### 2-1. コンソールにログイン

1. https://developers.line.biz/console/ を開く
2. **普段お使いの LINE アカウントでログイン**（スマホで2段階認証を求められます）
3. 初回は開発者名とメールアドレスの登録を求められるので入力

### 2-2. プロバイダーを作る

1. **［Create］/［作成］** から新しいプロバイダーを作成
2. 名前は何でも構いません（例: `hinotane`）

> プロバイダー＝「アプリの提供者」の器です。1つ作れば以降使い回せます。

### 2-3. Messaging API チャネルを作る

1. 作ったプロバイダーを開き、**［Create a Messaging API channel］**（Messaging APIチャネルを作成）
2. 入力項目:

| 項目 | 入れるもの | 注意 |
|---|---|---|
| チャネル名 | 例: `株シグナル通知` | ⚠️ **名前に「LINE」の文字は使えません**。これがそのまま LINE 公式アカウント名になります |
| チャネル説明 | 例: `個人用の株式シグナル通知` | 任意の文で OK |
| 大業種 / 小業種 | 「個人」→「個人（その他）」など | 個人利用なら何でも構いません |
| メールアドレス | ご自身のもの | |

3. 利用規約に同意して作成

### 2-4. 🔑 必要な3つの値を集める

作成したチャネルの画面から、**3つの値**をコピーします。

#### ① チャネルシークレット

**［チャネル基本設定］**（Basic settings）タブ → **「チャネルシークレット」**

```dotenv
LINE_CHANNEL_SECRET=ここに貼り付け
```

#### ② あなたのユーザーID ← ここが分かりにくい

**同じ［チャネル基本設定］タブを一番下までスクロール** → **「あなたのユーザーID」**（Your user ID）

`U` で始まる 33 文字の文字列です（例: `U8189cf6745fc0d808977bdb0b9f22995`）。

```dotenv
LINE_ALLOWED_USER_IDS=ここに貼り付け
```

> **この ID に登録された人だけが承認できます。**セキュリティ上とても重要な設定です。
>
> 見つからない場合の代替手段: `.env` に ①③ だけ入れて `hinotane serve` を起動し、
> Bot に何か話しかけてください。**「あなたの userId はこれです」と返信で教えてくれます**
> （セットアップモード）。この状態では承認操作は一切受け付けないので安全です。

#### ③ チャネルアクセストークン

**［Messaging API設定］**（Messaging API）タブ → **一番下**の「チャネルアクセストークン（長期）」→ **［発行］**

```dotenv
LINE_CHANNEL_ACCESS_TOKEN=ここに貼り付け
```

> ⚠️ **［再発行］を押すと古いトークンは即座に無効になります。**通知が急に止まったら、
> 誰かが再発行していないか確認してください。

### 2-5. Bot を友だち追加する

**［Messaging API設定］**タブに **QRコード** が表示されています。
スマホの LINE で読み取って**友だち追加**してください。

**これを忘れると通知は届きません。**（友だちでない相手には送れない仕様です）

### 2-6. 自動応答を切る

初期状態だと、こちらが何か送るたびに定型文が自動で返ってきて邪魔になります。

1. **［Messaging API設定］**タブの「応答設定」から、**LINE Official Account Manager** へのリンクを開く
2. **応答メッセージ → オフ**
3. **あいさつメッセージ → お好みで（オフ推奨）**
4. **Webhook → オン**

### 2-7. 動作確認

```bash
hinotane line-test
```

LINE に以下が届けば完了です。

```
✅ hinotane の接続テストです。
このメッセージが届いていれば通知の設定は完了しています。

試しに「状況」と送ってみてください。
```

---

## 3. ここまでで動くこと（VPS なし）

```bash
hinotane init                 # DB を作る
hinotane backfill --years 2   # 過去データ取り込み（数十分。放っておいてOK）
hinotane screen               # スクリーニング → LINE に配信
```

**`hinotane screen` を毎晩実行すれば、それだけで候補が LINE に届きます。**
発注は自分の証券口座で手動、という運用がここで完成します。

### Mac で自動実行したい場合

`crontab -e` で以下を追加（PC がスリープしていると動きません）。

```cron
0 16 * * 1-5 cd ~/hinotane && .venv/bin/hinotane fetch
0 21 * * 1-5 cd ~/hinotane && .venv/bin/hinotane screen
```

---

## 4. VPS ＋ ドメイン（承認ボタンを使うなら）

承認ボタンから発注まで自動化する段階で必要になります。

### 4-1. VPS を借りる

| サービス | 目安 | 備考 |
|---|---|---|
| [ConoHa VPS](https://www.conoha.jp/vps/) | 約700円/月 | 1GB プラン、Ubuntu 24.04。管理画面が分かりやすい |
| [さくらのVPS](https://vps.sakura.ad.jp/) | 約700円/月 | 1GB プラン、Ubuntu 24.04 |

**Ubuntu 24.04、メモリ1GBで十分です。**

### 4-2. ドメインを取る

お名前.com / ムームードメイン / Cloudflare Registrar などで、`.com` や `.net` を年1,000〜1,500円程度で取得。

取得したら **DNS の A レコードを VPS の IP アドレスに向けます**。
（VPS 事業者が提供する無料ドメインでも構いません）

### 4-3. デプロイ

```bash
# VPS に SSH でログインしてから
curl -fsSL https://get.docker.com | sh

git clone <このリポジトリのURL> hinotane && cd hinotane
cp .env.example .env
vi .env                              # 1・2 で集めた値を貼る
echo "DOMAIN=取得したドメイン" >> .env

docker compose up -d --build
docker compose exec app hinotane backfill --years 2
```

HTTPS 証明書は Caddy が Let's Encrypt から自動取得します。設定は不要です。

### 4-4. LINE 側に Webhook URL を登録

**［Messaging API設定］**タブ → 「Webhook URL」に以下を入力して **［検証］**

```
https://取得したドメイン/line/webhook
```

「成功」と出れば完了です。以降、LINE のボタンが機能します。

---

## つまずいたときの早見表

| 症状 | 原因として多いもの |
|---|---|
| `doctor` が「認証に失敗しました」 | **プラン選択（1-2）が未完了**が最多。次にキーの貼り間違い |
| `doctor` が「サーバーに接続できませんでした」 | 設定ではなくネットワーク側。VPN / 社内プロキシ / 回線を確認 |
| `hinotane: command not found` | 仮想環境が有効になっていない。`source .venv/bin/activate` するか `.venv/bin/hinotane …` と書く |
| 「必須カラムを特定できませんでした」 | J-Quants のレスポンス形式が想定と違う。**エラーに実際のカラム一覧が出るので、それを送ってください**。対応表に1行足せば直ります |
| `line-test` が ❌ | ①Bot を友だち追加していない ②トークンを再発行して古いのを貼っている ③userId が別チャネルのもの |
| LINE に通知が来ない | `LINE_ALLOWED_USER_IDS` が空（未設定だと送信されません） |
| 話しかけると定型文が返ってくる | 応答メッセージがオン（2-6） |
| Webhook の「検証」が失敗する | ①DNS が VPS を向いていない ②`docker compose up` していない ③証明書の取得中（数分待つ） |
| 候補が 0 件 | 異常とは限りません。3戦略とも条件は厳しめです。`SCREENER_MIN_TURNOVER_JPY` を下げるか、グロース（0113）を対象に足してみてください |

---

## 集める値のチェックリスト

`.env` に埋めるのは、最終的にこの4つだけです。

```dotenv
JQUANTS_API_KEY=            # 1-3 で発行
LINE_CHANNEL_SECRET=        # 2-4 ① チャネル基本設定タブ
LINE_ALLOWED_USER_IDS=      # 2-4 ② チャネル基本設定タブの一番下
LINE_CHANNEL_ACCESS_TOKEN=  # 2-4 ③ Messaging API設定タブの一番下
```

- [ ] J-Quants でプランを選択した（Free で可）
- [ ] `hinotane doctor` が J-Quants ✅ になった
- [ ] LINE Bot を友だち追加した
- [ ] 応答メッセージをオフにした
- [ ] `hinotane line-test` が LINE に届いた
- [ ] `hinotane backfill --years 2` を流した
- [ ] `hinotane screen` で LINE に候補が届いた

ここまで来れば **Phase 1（毎晩の通知）は完成**です。

---

## 参考

- [J-Quants サインイン](https://jpx-jquants.com/auth/signin) / [クイックスタート](https://jpx-jquants.com/ja/spec/quickstart) / [V1→V2 変更点](https://jpx-jquants.com/ja/spec/migration-v1-v2)
- [LINE Developers コンソール](https://developers.line.biz/console/) / [Messaging APIを始めよう](https://developers.line.biz/ja/docs/messaging-api/getting-started/) / [ユーザーIDを取得する](https://developers.line.biz/ja/docs/messaging-api/getting-user-ids/)
- [LINE Notify のサービス終了告知](https://notify-bot.line.me/closing-announce)
