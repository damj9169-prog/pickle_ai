#!/usr/bin/env python3
"""
GitHub Actions 自己完結ビルドスクリプト（外部pip依存ゼロ）
dashboards/*.json を全自動スキャンして各HTMLと index.html を生成する。
"""
import concurrent.futures
import datetime
import glob
import html
import json
import os
import random
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DASHBOARDS_DIR = os.path.join(ROOT_DIR, "dashboards")
CACHE_FILE = os.path.join(ROOT_DIR, "scripts", "trans_cache.json")
JST = datetime.timezone(datetime.timedelta(hours=9))


def is_foreign(text):
    if not text:
        return False
    kana = len(re.findall(r"[\u3040-\u309F\u30A0-\u30FF]", text))
    clean = re.sub(r"\s+", "", text)
    if not clean:
        return False
    if kana / len(clean) < 0.08:
        if re.search(r"[A-Za-z]{3,}", text) or re.search(r"[\u4e00-\u9fa5]{4,}", text):
            return True
    return False


def translate_to_ja(text):
    if not text or not text.strip():
        return ""
    try:
        url = "https://translate.googleapis.com/translate_a/single?client=dict-chrome-ex&sl=auto&tl=ja&dt=t&q=" + urllib.parse.quote(text)
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=6) as res:
            data = json.loads(res.read().decode("utf-8"))
            return "".join([part[0] for part in data[0] if part[0]])
    except Exception:
        return ""


def load_cache():
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_cache(cache):
    if len(cache) > 2500:
        keys = list(cache.keys())[-1800:]
        cache = {k: cache[k] for k in keys}
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


def fetch_x_posts(handle, limit):
    url = f"https://r.jina.ai/https://x.com/{handle}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    content = ""
    for attempt in range(2):
        try:
            with urllib.request.urlopen(req, timeout=8) as r:
                content = r.read().decode("utf-8", errors="ignore")
                break
        except Exception:
            time.sleep(0.8)
    if not content:
        return []
    parts = re.split(r"## .*?posts", content, flags=re.IGNORECASE)
    post_block = parts[1] if len(parts) > 1 else content
    raw_items = re.split(r"\n\*\s+", post_block)
    items = []
    for raw in raw_items:
        clean = re.sub(r"\[!\[.*?\]\(.*?\)\]\(.*?\)", "", raw)
        clean = re.sub(r"!\[.*?\]\(.*?\)", "", clean)
        s_parts = re.split(r"\[(?:\d+[smhd]|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d+)\]\(https://x\.com/[^)]+\)", clean)
        body = s_parts[-1] if len(s_parts) > 1 else clean
        body = re.sub(r"\[(.*?)\]\(.*?\)", r"\1", body)
        body = re.sub(r"(Fan account|Show more|Pinned)", "", body)
        body = re.sub(r"[\d\.,\sKMB]+$", "", body).strip()
        if len(body) > 15 and "Log in or sign up" not in body and "Terms of Service" not in body:
            items.append({"title": body[:280], "link": f"https://x.com/{handle}", "pub": "X Post"})
            if len(items) >= limit:
                break
    return items


def fetch_feed(feed):
    limit = int(feed.get("limit", 15))
    if feed.get("type") == "x":
        handle = (feed.get("handle") or feed.get("url", "")).lstrip("@")
        return feed, fetch_x_posts(handle, limit)

    items = []
    for attempt in range(2):
        try:
            time.sleep(1.2 + random.uniform(0, 0.8) if attempt > 0 else random.uniform(0.05, 0.25))
            req = urllib.request.Request(feed["url"], headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"})
            with urllib.request.urlopen(req, timeout=8) as r:
                data = r.read()
            root = ET.fromstring(data)
            entries = root.findall(".//item") or root.findall(".//{http://www.w3.org/2005/Atom}entry")
            for item in entries[:limit]:
                title = (item.findtext("title") or item.findtext("{http://www.w3.org/2005/Atom}title") or "").strip()
                link = (item.findtext("link") or "").strip()
                if not link:
                    link_el = item.find("{http://www.w3.org/2005/Atom}link")
                    if link_el is not None:
                        link = (link_el.attrib.get("href") or "").strip()
                pub = (item.findtext("pubDate") or item.findtext("{http://www.w3.org/2005/Atom}updated") or "").strip()
                if title and link:
                    items.append({"title": title, "link": link, "pub": pub})
            return feed, items
        except Exception as e:
            if attempt == 0 and "429" in str(e):
                continue
            print(f"[warn] {feed.get('name')}: {e}", file=sys.stderr)
    return feed, items


def build_one(cfg_path, trans_cache):
    with open(cfg_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    title = cfg.get("title", "📰 ニュース監視ダッシュボード")
    gh_file = cfg.get("github_file") or os.path.basename(cfg.get("output_html", "news.html"))
    out_path = os.path.join(ROOT_DIR, gh_file)
    feeds = cfg.get("feeds", [])

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        for res in [ex.submit(fetch_feed, fd) for fd in feeds]:
            results.append(res.result())

    order_map = {fd["url"]: i for i, fd in enumerate(feeds)}
    results.sort(key=lambda x: order_map.get(x[0]["url"], 999))

    to_trans = {it["title"] for _, items in results for it in items if is_foreign(it["title"]) and it["title"] not in trans_cache}
    if to_trans:
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
            fmap = {ex.submit(translate_to_ja, t): t for t in to_trans}
            for fut in concurrent.futures.as_completed(fmap):
                ja = fut.result()
                if ja:
                    trans_cache[fmap[fut]] = ja

    media_tabs = []
    for fd in feeds:
        m = fd.get("media", fd.get("name", "その他"))
        if m not in media_tabs:
            media_tabs.append(m)

    sections = []
    total_articles = 0
    active_sources = 0
    for feed, items in results:
        if not items:
            continue
        active_sources += 1
        total_articles += len(items)
        media = feed.get("media", feed.get("name", "その他"))
        lang = feed.get("lang", "JP")
        domain = feed.get("domain") or urllib.parse.urlparse(feed["url"]).netloc
        rows = []
        for it in items:
            orig = it["title"]
            ja = trans_cache.get(orig, "") if is_foreign(orig) else ""
            badge = '<span class="badge badge-en">EN</span>' if (lang != "JP" or is_foreign(orig)) else '<span class="badge badge-jp">JP</span>'
            ja_html = f'<div class="ja-line">🇯🇵 <strong>【和訳】</strong>{html.escape(ja)}</div>' if ja else ""
            pub_html = f'<span class="pub">{html.escape(it["pub"][:22])}</span>' if it["pub"] else ""
            rows.append(f'''
            <a class="item" href="{html.escape(it["link"])}" target="_blank" rel="noopener">
              <div class="item-title">{badge}{html.escape(orig)}</div>
              {ja_html}
              <div class="item-meta"><span>{html.escape(domain)}</span>{pub_html}</div>
            </a>''')
        sections.append(f'''
        <section class="source-card" data-media="{html.escape(media)}" data-lang="{html.escape(lang)}">
          <div class="source-header">
            <span class="source-name">{html.escape(feed["name"])}</span>
            <span class="source-count">{len(items)}件</span>
          </div>
          <div class="items">{''.join(rows)}</div>
        </section>''')

    tab_buttons = ['<button class="tab active" onclick="filterTab(\'ALL\', this)">すべて</button>']
    for m in media_tabs:
        tab_buttons.append(f'<button class="tab" onclick="filterTab(\'{html.escape(m)}\', this)">{html.escape(m)}</button>')
    tab_buttons.append('<button class="tab" onclick="filterTab(\'EN_ONLY\', this)">外国語のみ</button>')

    now_str = datetime.datetime.now(JST).strftime("%Y-%m-%d %H:%M")
    page = f'''<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Hiragino Sans", sans-serif; background: #f4f6f5; color: #1f2937; margin: 0; padding: 16px; }}
  .container {{ max-width: 820px; margin: 0 auto; }}
  h1 {{ font-size: 20px; margin: 0 0 4px; }}
  .sub {{ font-size: 12px; color: #6b7280; margin-bottom: 12px; }}
  .tabs {{ display: flex; gap: 8px; overflow-x: auto; padding-bottom: 8px; margin-bottom: 12px; }}
  .tab {{ border: 1px solid #d1d5db; background: #fff; border-radius: 999px; padding: 6px 14px; font-size: 13px; cursor: pointer; white-space: nowrap; color: #374151; }}
  .tab.active {{ background: #145343; color: #fff; border-color: #145343; font-weight: 600; }}
  .source-card {{ background: #fff; border: 1px solid #e5e7eb; border-radius: 12px; padding: 14px 16px; margin-bottom: 14px; box-shadow: 0 1px 2px rgba(0,0,0,0.03); }}
  .source-header {{ display: flex; justify-content: space-between; align-items: center; border-bottom: 2px solid #145343; padding-bottom: 8px; margin-bottom: 8px; }}
  .source-name {{ font-weight: 700; font-size: 15px; }}
  .source-count {{ font-size: 12px; color: #6b7280; }}
  .item {{ display: block; padding: 10px 0; border-bottom: 1px solid #f3f4f6; text-decoration: none; color: inherit; }}
  .item:last-child {{ border-bottom: none; }}
  .item:hover .item-title {{ color: #145343; text-decoration: underline; }}
  .item-title {{ font-size: 14px; font-weight: 600; line-height: 1.45; }}
  .ja-line {{ font-size: 13px; color: #065f46; background: #ecfdf5; border-left: 3px solid #10b981; padding: 5px 8px; margin-top: 5px; border-radius: 0 6px 6px 0; line-height: 1.45; }}
  .item-meta {{ display: flex; justify-content: space-between; font-size: 11px; color: #9ca3af; margin-top: 5px; }}
  .badge {{ display: inline-block; font-size: 10px; font-weight: 700; padding: 1px 6px; border-radius: 4px; margin-right: 6px; vertical-align: middle; }}
  .badge-en {{ background: #e0f2fe; color: #0369a1; }}
  .badge-jp {{ background: #fef3c7; color: #92400e; }}
</style>
</head>
<body>
<div class="container">
  <h1>{html.escape(title)}</h1>
  <div class="sub">更新: {now_str} JST ・ {active_sources}フィード / 計{total_articles}件（外国語は原文＋インライン和訳）</div>
  <div class="tabs">{''.join(tab_buttons)}</div>
  {''.join(sections)}
</div>
<script>
function filterTab(key, btn) {{
  document.querySelectorAll('.tab').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  document.querySelectorAll('.source-card').forEach(card => {{
    if (key === 'ALL') card.style.display = 'block';
    else if (key === 'EN_ONLY') card.style.display = (card.dataset.lang !== 'JP') ? 'block' : 'none';
    else card.style.display = (card.dataset.media === key) ? 'block' : 'none';
  }});
}}
</script>
</body>
</html>'''
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(page)
    print(f"Built {gh_file} ({active_sources} feeds, {total_articles} articles)")
    return {"file": gh_file, "title": title, "feeds": feeds}


def rebuild_index(items):
    cards = []
    for it in items:
        names = []
        for fd in it["feeds"]:
            m = fd.get("media", fd.get("name", ""))
            if m and m not in names:
                names.append(m)
        desc = " / ".join(names) + f"（{len(it['feeds'])}フィード）"
        cards.append(f'''  <a class="card" href="./{html.escape(it['file'])}">
    <div class="title">{html.escape(it['title'])}</div>
    <div class="desc">{html.escape(desc)}</div>
  </a>''')
    now_str = datetime.datetime.now(JST).strftime("%Y-%m-%d %H:%M")
    idx = f'''<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ニュース監視ダッシュボード一覧</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #f4f6f5; color: #1f2937; margin: 0; padding: 24px 16px; }}
  .wrap {{ max-width: 580px; margin: 0 auto; }}
  h1 {{ font-size: 22px; margin: 0 0 6px; }}
  p {{ font-size: 13px; color: #6b7280; margin: 0 0 20px; }}
  .card {{ display: block; background: #fff; border: 1px solid #e5e7eb; border-left: 5px solid #145343; border-radius: 12px; padding: 18px 20px; margin-bottom: 14px; text-decoration: none; color: inherit; box-shadow: 0 1px 3px rgba(0,0,0,0.04); }}
  .card:hover {{ border-color: #145343; background: #f9fafb; }}
  .title {{ font-size: 16px; font-weight: 700; margin-bottom: 4px; }}
  .desc {{ font-size: 13px; color: #4b5563; line-height: 1.45; }}
</style>
</head>
<body>
<div class="wrap">
  <h1>📰 ニュース監視ダッシュボード</h1>
  <p>最終更新: {now_str} JST（リロードで常に最新）</p>
{''.join(cards)}
</div>
</body>
</html>'''
    with open(os.path.join(ROOT_DIR, "index.html"), "w", encoding="utf-8") as f:
        f.write(idx)
    print(f"Rebuilt index.html ({len(cards)} dashboards)")


def main():
    cache = load_cache()
    built = []
    for path in sorted(glob.glob(os.path.join(DASHBOARDS_DIR, "*.json"))):
        built.append(build_one(path, cache))
    save_cache(cache)
    rebuild_index(built)


if __name__ == "__main__":
    main()
