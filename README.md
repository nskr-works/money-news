# 決済ニュース

クレジット・決済代行・コード決済・キャッシュレスのニュースとプレスリリースを自動で集め、分野別に一覧表示するサイトです。
GitHub Actions と GitHub Pages だけで動き、費用はかかりません。

## 仕組み

- **定期更新**（`.github/workflows/update.yml`）：毎日 6:37・10:37・13:37・16:37（日本時間）に全収集元を取得します。サイト上の表示は「毎日7時・11時・14時・17時更新」です。
- **PR TIMES の直接取得**（`.github/workflows/prtimes.yml`）：PR TIMES の公式RSSは最新200件しか載らないため、毎時37分に取得します（上の4時刻は除く）。
- **新着の通知**：定期更新のたびに、直近24時間の新着があれば ntfy に1通送ります。宛先のトピック名は GitHub の Secret `NTFY_TOPIC` に登録します（未設定なら通知しません）。
- **件数の記録**：収集元ごとの取得件数・新着件数を `stats/feed_log.csv` に追記します。

## 設定（`config.json`）

- `site_name` / `site_url`：サイト名と公開URL（通知のタイトル・リンク先に使います）
- `categories`：分野の表示順・名前・英字ラベル・色。`keywords` のキーと揃えます
- `keywords`：記事をどの分野に振り分けるかの語句（英字だけの語句は単語として一致したときのみ）
- `topics`（任意）：分野ごとの細分類。`{"credit": {"不正利用": ["不正利用", "漏えい"], ...}}` のように書くと、上から順に判定して1つ付けます
- `exclude_patterns` / `exclude_sources`：取り込まない見出しのパターン・配信元
- `feeds`：収集元の一覧。`hourly: true` のものは1時間ごとの取得の対象です

## ローカルでの確認

```bash
python scripts/fetch_news.py          # 全収集元を取得して docs/data.json を更新
python scripts/fetch_news.py --hourly # hourly 指定の収集元だけ取得
python -m http.server 8766 -d docs    # http://localhost:8766 で表示
```

## 補足

- 記事本文は保存せず、タイトルとリンクのみを表示します。
- 収集の多くは Google ニュースの RSS を経由しています。Google ニュースの RSS は個人の非商用利用に限られているため、広告などで収益化する場合は収集元の見直しが必要です。
- ヘッダー写真：Unsplash（Unsplash License、クレジット表記不要）
