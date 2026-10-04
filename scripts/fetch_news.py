"""クレジット・決済代行・コード決済・キャッシュレスのニュースとプレスリリースを集めて docs/data.json に保存する。"""
import calendar
import csv
import hashlib
import html
import json
import os
import re
import sys
import unicodedata
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feedparser

ROOT = Path(__file__).resolve().parent.parent
CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
DATA_PATH = ROOT / "docs" / "data.json"
STATS_PATH = ROOT / "stats" / "feed_log.csv"   # 収集元ごとの件数の記録（件数の推移を追うため）
JST = timezone(timedelta(hours=9))
UA = "Mozilla/5.0 (compatible; EnergyNewsBot/1.0)"
BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
EXCLUDE = [re.compile(p) for p in CONFIG.get("exclude_patterns", [])]
# 1回の実行で企業名を調べるリリースの上限（Googleニュースへの問い合わせを抑える）
COMPANY_LOOKUP_LIMIT = 80


def clean(text):
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def norm_title(title):
    # 全角/半角の違いを吸収し、同じ記事を重複登録しない
    title = unicodedata.normalize("NFKC", title)
    # 末尾の「(朝日新聞)」「[京都府]」「(2026年10月2日掲載)」などは配信先ごとの付記なので無視
    title = re.sub(r"(\s*[(\[【][^()\[\]【】]*[)\]】])+\s*$", "", title) or title
    return re.sub(r"[\s「」『』【】\[\]()・、。,.!?:\-–—|]", "", title).lower()


def has_any(text, words):
    for w in words:
        if w.startswith("re:"):
            # 「re:」で始まる語句は正規表現（例：容量の「10MW」「9.8MWh」）
            if re.search(w[3:], text):
                return True
        elif re.fullmatch(r"[A-Za-z]+", w):
            # 英字だけのキーワード（BESS、FITなど）は単語として一致した場合のみ
            if re.search(rf"(?<![A-Za-z]){w}(?![A-Za-z])", text):
                return True
        elif w in text:
            return True
    return False


def categorize(text):
    return [cat for cat, words in CONFIG["keywords"].items() if has_any(text, words)]


def pick_topic(text, topics):
    """細分類。config の並び順が優先順位で、最初に当たった1つを返す。"""
    text = unicodedata.normalize("NFKC", text)
    for topic, words in topics.items():
        if has_any(text, words):
            return topic
    return "その他"


def entry_time(entry):
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            return datetime.fromtimestamp(calendar.timegm(t), tz=timezone.utc)
    return datetime.now(timezone.utc)


# ── プレスリリースの企業名 ──────────────────────────────

# ページの発行者がこれらなら転載元なので、企業名としては使わない
MEDIA_NAMES = re.compile(r"PR TIMES|ニュース|新聞|デジタル|放送|テレビ|NEWS|News|Yahoo|Excite|エキサイト|Infoseek|@Press|アットプレス|さんデジ")


def http_get(url, data=None, headers=None):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": BROWSER_UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.read().decode("utf-8", "replace")


def decode_google_news(url):
    """Googleニュースの記事リンクを配信元のURLに戻す。"""
    aid = urllib.parse.urlparse(url).path.rstrip("/").split("/")[-1]
    page = http_get(f"https://news.google.com/rss/articles/{aid}")
    sg = re.search(r'data-n-a-sg="([^"]+)"', page)
    ts = re.search(r'data-n-a-ts="([^"]+)"', page)
    if not (sg and ts):
        return None
    inner = json.dumps(["garturlreq", [["X", "X", ["X", "X"], None, None, 1, 1, "US:en", None, 1, None, None, None, None, None, 0, 1],
                                       "X", "X", 1, [1, 1, 1], 1, 1, None, 0, 0, None, 0], aid, int(ts.group(1)), sg.group(1)])
    body = urllib.parse.urlencode({"f.req": json.dumps([[["Fbv4je", inner]]])}).encode()
    res = http_get("https://news.google.com/_/DotsSplashUi/data/batchexecute", body,
                   {"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"})
    m = re.search(r'\[\\"garturlres\\",\\"(.*?)\\"', res)
    return m.group(1).encode().decode("unicode_escape") if m else None


def company_from_page(url, depth=0):
    # エキサイトの PR TIMES 転載（Prtimes_日付-企業ID-リリース番号）は PR TIMES の元ページへ
    m = re.search(r"Prtimes_\d{4}-\d{2}-\d{2}-(\d+)-(\d+)", url)
    if m:
        url = f"https://prtimes.jp/main/html/rd/p/{int(m.group(2)):09d}.{int(m.group(1)):09d}.html"
    page = http_get(url)
    if "prtimes.jp" in url:
        t = re.search(r"<title>[^<]*\|\s*([^<|]+?)のプレスリリース", page)
        if t:
            return html.unescape(t.group(1)).strip()
    a = re.search(r'"author"\s*:\s*\{[^}]*?"name"\s*:\s*"([^"]+)"', page)
    if a:
        name = html.unescape(a.group(1)).strip()
        if not MEDIA_NAMES.search(name):
            return name
    # 転載ページなら、本文中の PR TIMES 元記事へのリンクをたどる
    p = re.search(r"https?://prtimes\.jp/main/html/rd/p/\d+\.\d+\.html", page)
    if p and depth == 0:
        return company_from_page(p.group(0), depth + 1)
    return None


def company_from_title(title):
    """見出しの「【〇〇株式会社】…」「〇〇、…」から企業名を推定する。"""
    m = re.match(r"^【([^】]{2,30}?(?:株式会社|合同会社|有限会社|グループ|ホールディングス)[^】]{0,10})】", title)
    if m:
        return m.group(1)
    m = re.match(r"^([^、。「」『』【】\s]{2,20}?)(?:が|は)?、", title)
    if m:
        name = m.group(1)
        if not re.match(r"^\d", name) and not re.search(r"(年|月|日|以降|向け|ため|中|後|前)$", name):
            return name
    return None


GOV_SOURCES = {"meti.go.jp": "経済産業省", "enecho.meti.go.jp": "資源エネルギー庁", "env.go.jp": "環境省"}


def lookup_company(it):
    if it["source"] in GOV_SOURCES:
        return GOV_SOURCES[it["source"]]
    try:
        url = decode_google_news(it["link"]) if "news.google.com" in it["link"] else it["link"]
        name = company_from_page(url) if url else None
    except Exception:
        name = None
        recent = datetime.now(JST) - timedelta(days=3)
        # 通信エラーで見出しからも分からないときは、新しい記事に限り次回に再挑戦
        if not company_from_title(it["title"]) and datetime.fromisoformat(it["published"]) >= recent:
            return None
    return name or company_from_title(it["title"]) or ""


def fill_companies(items):
    todo = [it for it in items if it["type"] == "release" and "company" not in it][:COMPANY_LOOKUP_LIMIT]
    with ThreadPoolExecutor(max_workers=6) as ex:
        for it, name in zip(todo, ex.map(lookup_company, todo)):
            if name is not None:
                it["company"] = re.sub(r"\s+", " ", name.replace("\xa0", " ")).strip()
    print(f"  企業名の確認: {len(todo)}件")


# ── 新着の通知（ntfy） ─────────────────────────────

def notify(new_items):
    """新着があれば ntfy に1通だけ送る。トピック名は GitHub の Secret（NTFY_TOPIC）で渡す。"""
    topic = os.environ.get("NTFY_TOPIC")
    if not topic or not new_items:
        return
    heads = "\n".join(f"・{it['title'][:40]}" for it in new_items[:3])
    more = f"\nほか{len(new_items) - 3}件" if len(new_items) > 3 else ""
    body = json.dumps({
        "topic": topic,
        "title": f"{CONFIG.get('site_name', 'News')} 新着{len(new_items)}件",
        "message": heads + more,
        "click": os.environ.get("SITE_URL", CONFIG.get("site_url", "")),
        "tags": [CONFIG.get("notify_tag", "newspaper")],
    }, ensure_ascii=False).encode()
    try:
        req = urllib.request.Request("https://ntfy.sh/", data=body, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=15).close()
        print(f"  通知を送信: {len(new_items)}件")
    except Exception as e:
        print(f"  ! 通知の送信に失敗: {e}")


# ── 収集 ─────────────────────────────────────────

def fetch_feed(feed):
    parsed = feedparser.parse(feed["url"], agent=UA)
    if parsed.bozo and not parsed.entries:
        print(f"  ! 取得失敗: {feed['name']} ({parsed.get('bozo_exception')})")
        return []

    items = []
    for e in parsed.entries:
        title = clean(e.get("title"))
        link = e.get("link", "")
        if not title or not link:
            continue

        source = ""
        src = e.get("source")
        if isinstance(src, dict):
            source = src.get("title", "")
        # Googleニュースは「タイトル - 媒体名」形式なので媒体名を分離
        if "news.google.com" in feed["url"]:
            m = re.match(r"^(.*)\s+-\s+([^-]+)$", title)
            if source and title.endswith(f" - {source}"):
                title = title[: -len(source) - 3].strip()
            elif m:
                title, source = m.group(1).strip(), source or m.group(2).strip()
        source = source or feed["name"]
        # 「… - ニュース - メガソーラービジネス plus」のような媒体内の区分表記や末尾の記号を外す
        title = re.sub(r"\s+-\s+(ニュース|特集|コラム|インタビュー)\s+-\s+.*$", "", title)
        title = re.sub(r"\s*[-–—|｜]+\s*$", "", title)
        # 写真ページ（「写真：」付き）は本記事と同じ内容なので接頭辞を外して重複扱いにする
        title = re.sub(r"^写真[：:]\s*", "", title)
        title = re.sub(r"^写真・図版（\d+枚目）\s*[|｜]\s*", "", title)
        if any(p.search(title) for p in EXCLUDE) or source in CONFIG.get("exclude_sources", []):
            continue

        summary = clean(e.get("summary", ""))[:200]
        cats = categorize(f"{title} {summary}")
        if not cats:
            if feed.get("require_keyword"):
                continue
            cats = [feed["default_category"]]

        is_release = feed["type"] == "release" or "prtimes.jp" in link or "PR TIMES" in source
        item = {
            "id": hashlib.sha1(norm_title(title).encode()).hexdigest()[:16],
            "title": title,
            "link": link,
            "source": source,
            "summary": summary if summary != title else "",
            "categories": sorted(set(cats)),
            "type": "release" if is_release else "news",
            "published": entry_time(e).astimezone(JST).isoformat(timespec="minutes"),
        }
        # PR TIMES の公式RSSは企業名（dc:corp）を持っている
        if e.get("dc_corp"):
            item["company"] = e["dc_corp"].strip()
        items.append(item)
    print(f"  {feed['name']}: {len(items)}件")
    return items


def log_stats(mode, rows):
    """実行ごとに、収集元ごとの取得件数・新着件数を CSV に追記する。"""
    STATS_PATH.parent.mkdir(parents=True, exist_ok=True)
    first = not STATS_PATH.exists()
    now = datetime.now(JST).isoformat(timespec="minutes")
    with STATS_PATH.open("a", encoding="utf-8", newline="") as fp:
        w = csv.writer(fp)
        if first:
            w.writerow(["run_at", "mode", "feed", "fetched", "new"])
        for name, fetched, new in rows:
            w.writerow([now, mode, name, fetched, new])


def main():
    # --hourly：1時間ごとの実行。hourly 指定のフィード（PR TIMES）だけを取得し、通知はしない
    hourly = "--hourly" in sys.argv
    feeds = [f for f in CONFIG["feeds"] if f.get("hourly")] if hourly else CONFIG["feeds"]

    existing, since = [], None
    if DATA_PATH.exists():
        try:
            prev = json.loads(DATA_PATH.read_text(encoding="utf-8"))
            existing, since = prev.get("items", []), prev.get("collected_since")
        except json.JSONDecodeError:
            pass

    merged = {it["id"]: it for it in existing}
    new_ids, stats = [], []
    for feed in feeds:
        got = fetch_feed(feed)
        new_here = 0
        for it in got:
            if it["id"] in merged:
                old = merged[it["id"]]
                old["categories"] = sorted(set(old["categories"]) | set(it["categories"]))
                if it["type"] == "release":
                    old["type"] = "release"
                if it.get("company") and not old.get("company"):
                    old["company"] = it["company"]
            else:
                it["via"] = feed["name"]          # 最初に見つけた収集元
                if hourly:
                    it["pending_notify"] = True   # 次の定期更新でまとめて通知する
                merged[it["id"]] = it
                new_ids.append(it["id"])
                new_here += 1
        stats.append((feed["name"], len(got), new_here))
    log_stats("hourly" if hourly else "full", stats)

    if hourly and not new_ids:
        print("新着なし（data.json は更新しない）")
        return

    cutoff = datetime.now(JST) - timedelta(days=CONFIG["keep_days"])
    items = [it for it in merged.values() if datetime.fromisoformat(it["published"]) >= cutoff]
    items.sort(key=lambda it: it["published"], reverse=True)
    items = items[: CONFIG["max_items"]]

    # 細分類（config.json の topics に分野ごとの分類語を書いた場合だけ）。毎回付け直すので過去記事にも反映される
    topics_conf = CONFIG.get("topics", {})
    for it in items:
        text = f"{it['title']} {it['summary']}"
        t = {cat: pick_topic(text, topics_conf[cat]) for cat in it["categories"] if topics_conf.get(cat)}
        if t:
            it["topics"] = t
        else:
            it.pop("topics", None)

    fill_companies(items)

    fresh = []
    if not hourly:
        # 通知は直近24時間に配信された新着だけ（1時間ごとの取得で見つけた分も含める）
        recent = datetime.now(JST) - timedelta(hours=24)
        pend = [it["id"] for it in items if it.pop("pending_notify", False)]
        fresh = [merged[i] for i in dict.fromkeys(new_ids + pend) if datetime.fromisoformat(merged[i]["published"]) >= recent]
        fresh.sort(key=lambda it: it["published"], reverse=True)

    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    DATA_PATH.write_text(json.dumps({
        "updated_at": datetime.now(JST).isoformat(timespec="minutes"),
        # 収集を始めた日時（前週比は、前の週のデータがそろってから出すため）
        "collected_since": since or datetime.now(JST).isoformat(timespec="minutes"),
        "categories": CONFIG["categories"],
        "topics": {cat: [*words, "その他"] for cat, words in CONFIG.get("topics", {}).items()},
        "items": items,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"新着 {len(new_ids)}件 / 合計 {len(items)}件")
    notify(fresh)


if __name__ == "__main__":
    main()
