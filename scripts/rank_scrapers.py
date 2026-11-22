#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rank_scrapers.py — 公開ランキング／IPOスケジュールの取得＋解析（標準ライブラリのみ）

対象（いずれもログイン不要の公開ページ）:
  1. Amazon.co.jp 売れ筋ランキング（ビューティー系カテゴリ）… r.jina.ai 経由
     - 素のアクセスは captcha になるため、既存の X 取得と同じく r.jina.ai の描画結果を読む。
     - ?language=ja_JP を付けると商品名が日本語になる（価格は取得元が米ドル建てで返す）。
  2. 楽天市場ランキング（デイリー／週間）… r.jina.ai 経由（ranking.rakuten.co.jp は直接403）
  3. 東京IPO「IPOスケジュール一覧」… 直接 POST（EUC-JP）

すべて read-only の公開情報。認証情報は一切使わない。
"""
import gzip
import html as htmlmod
import io
import random
import re
import threading
import time
import urllib.parse
import urllib.request
from collections import deque
from html.parser import HTMLParser

JINA = "https://r.jina.ai/"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")
JP_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
         "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")

TOKYOIPO_URL = "https://www.tokyoipo.com/ipo/schedule.php"


# ---------------------------------------------------------------------------
# レート制御（r.jina.ai は無認証だと概ね 20req/60s。余裕を見て 18req/60s・最小3.3秒）
# ---------------------------------------------------------------------------
_RATE_LOCK = threading.Lock()
_RATE_TIMES = deque(maxlen=60)
RATE_PER_WINDOW = 18
RATE_WINDOW_SEC = 60.0
RATE_MIN_INTERVAL = 3.3


def _throttle():
    with _RATE_LOCK:
        while True:
            now = time.time()
            while _RATE_TIMES and now - _RATE_TIMES[0] > RATE_WINDOW_SEC:
                _RATE_TIMES.popleft()
            if len(_RATE_TIMES) < RATE_PER_WINDOW:
                gap = now - _RATE_TIMES[-1] if _RATE_TIMES else RATE_MIN_INTERVAL
                if gap >= RATE_MIN_INTERVAL:
                    _RATE_TIMES.append(now)
                    return
                wait = RATE_MIN_INTERVAL - gap
            else:
                wait = RATE_WINDOW_SEC - (now - _RATE_TIMES[0]) + 0.4
            time.sleep(max(0.2, wait))


def http_get(url, data=None, headers=None, timeout=45, retries=3):
    """GET/POST して bytes を返す（gzip/deflate 対応）。"""
    hdrs = {"User-Agent": JP_UA, "Accept-Language": "ja-JP,ja;q=0.9",
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            "Accept-Encoding": "gzip, deflate"}
    if headers:
        hdrs.update(headers)
    body = urllib.parse.urlencode(data).encode() if isinstance(data, dict) else data
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=body, headers=hdrs)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                enc = (r.headers.get("Content-Encoding") or "").lower()
            if enc == "gzip":
                raw = gzip.decompress(raw)
            elif enc == "deflate":
                try:
                    raw = gzip.decompress(raw)
                except OSError:
                    import zlib
                    raw = zlib.decompress(raw)
            return raw
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.5 + attempt * 2 + random.uniform(0, 1))
    raise RuntimeError(f"GET failed {url}: {last}")


def jina_html(target_url, timeout=100, retries=4, wait_selector=None, no_cache=False):
    """r.jina.ai 経由で描画済み HTML を取得。"""
    headers = {"X-Return-Format": "html", "Accept": "text/html,*/*"}
    if wait_selector:
        headers["X-Wait-For-Selector"] = wait_selector
    if no_cache:
        headers["X-No-Cache"] = "true"
    last = None
    for attempt in range(retries):
        _throttle()
        try:
            req = urllib.request.Request(JINA + target_url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = r.read()
                code = r.status
            if code == 200 and len(data) > 2000:
                return data.decode("utf-8", "ignore")
            last = f"HTTP {code} size={len(data)}"
        except Exception as e:  # noqa: BLE001
            last = str(e)
        time.sleep(3 + attempt * 4 + random.uniform(0, 2))
    raise RuntimeError(f"jina fetch failed {target_url}: {last}")


# ---------------------------------------------------------------------------
# Amazon 売れ筋ランキング
# ---------------------------------------------------------------------------
def fetch_amazon(url):
    sep = "&" if "?" in url else "?"
    page = jina_html(f"{url}{sep}language=ja_JP", wait_selector="#gridItemRoot")
    return parse_amazon(page)


_TAG = re.compile(r"<[^>]+>")


def _text(fragment):
    return htmlmod.unescape(_TAG.sub(" ", fragment or "")).replace("\u00a0", " ").strip()


def _clean_spaces(s):
    return re.sub(r"[ \t\u3000]+", " ", s or "").strip()


def _usd(s):
    m = re.search(r"(?:USD|US\$|\$)\s*([\d,]+(?:\.\d+)?)", str(s or ""))
    return float(m.group(1).replace(",", "")) if m else None


def parse_amazon(page_html):
    """Amazon 売れ筋ランキング HTML → [{rank, asin, title, price, rating, reviews, img, url}]"""
    parts = re.split(r'id="p13n-asin-index-(\d+)"', page_html)
    items, seen = [], set()
    for i in range(1, len(parts) - 1, 2):
        try:
            rank = int(parts[i])
        except ValueError:
            continue
        seg = parts[i + 1]
        if rank in seen:
            continue

        m_asin = re.search(r'data-asin="([A-Z0-9]{10})"', seg)
        asin = m_asin.group(1) if m_asin else ""

        title = ""
        for pat in (r'class="[^"]*p13n-sc-css-line-clamp[^"]*">(.*?)</div>',
                    r'<div class="[^"]*line-clamp[^"]*"[^>]*>(.*?)</div>',
                    r'aria-label="([^"]{15,}?)"[^>]*class="a-link-normal"'):
            m = re.search(pat, seg, re.S)
            if m:
                title = _clean_spaces(_text(m.group(1)))
                if title:
                    break
        if not title:
            # 画像 alt は商品名になっていることが多い
            m = re.search(r'<img alt="([^"]{10,})"', seg)
            if m:
                title = _clean_spaces(_text(m.group(1)))
        if not title:
            continue

        price, price_num, currency = "", None, ""
        # 新レイアウト: a-price（整数部/小数部が別span）
        sym = re.search(r'<span class="a-price-symbol">([^<]+)</span>', seg)
        whole = re.search(r'<span class="a-price-whole">([\d,]+)', seg)
        frac = re.search(r'<span class="a-price-fraction">(\d+)</span>', seg)
        if sym and whole:
            currency = "USD" if ("USD" in sym.group(1) or "$" in sym.group(1)) else (
                "JPY" if ("￥" in sym.group(1) or "¥" in sym.group(1)) else sym.group(1).strip())
            w = whole.group(1).replace(",", "")
            f = frac.group(1) if frac else ""
            price_num = float(f"{w}.{f}") if f else float(w)
            if currency == "USD":
                price = "US$" + f"{price_num:,.2f}"
            elif currency == "JPY":
                price = "￥" + f"{int(price_num):,}"
            else:
                price = sym.group(1).strip() + w
            tail = seg[whole.start():whole.start() + 600]
            if re.search(r'</span>\s*から', tail):
                price += "〜"
        else:
            # 旧レイアウト: p13n-sc-price
            m = re.search(r'class="[^"]*p13n-sc-price[^"]*">(.*?)</span>', seg, re.S)
            if not m:
                m = re.search(r'class="a-size-base a-color-price">(.*?)</span>', seg, re.S)
            if m:
                price = _clean_spaces(_text(m.group(1)))
                usd = _usd(price)
                if usd:
                    currency, price_num = "USD", usd
                    price = "US$" + f"{usd:,.2f}" + ("〜" if "から" in price else "")
                else:
                    yen = re.search(r"([\d,]+)\s*円", price)
                    if yen:
                        currency, price_num = "JPY", float(yen.group(1).replace(",", ""))
                        price = "￥" + f"{int(price_num):,}" + ("〜" if "から" in price or "～" in price else "")

        rating, reviews = None, None
        m = re.search(r"5つ星のうち([\d.]+)、([\d,]+)件", seg)
        if not m:
            m = re.search(r"([\d.]+) out of 5 stars[,、]\s*([\d,]+)", seg)
        if m:
            try:
                rating = float(m.group(1))
                reviews = int(m.group(2).replace(",", ""))
            except ValueError:
                pass

        img = ""
        m = re.search(r'<img[^^>]*?src="(https://m\.media-amazon\.com/images/[^"]+|https://images-fe\.ssl-images-amazon\.com/images/[^"]+)"', seg)
        if not m:
            m = re.search(r'src="(https://[^"]*images-amazon\.com/images/[^"]+)"', seg)
        if m:
            img = htmlmod.unescape(m.group(1)).split("},{")[0]

        url = f"https://www.amazon.co.jp/dp/{asin}" if asin else ""
        if not url:
            m = re.search(r'href="(/[^"]*?/dp/[A-Z0-9]{10}[^"]*)"', seg)
            if m:
                url = "https://www.amazon.co.jp" + htmlmod.unescape(m.group(1)).split("?")[0]
        if not url:
            continue

        seen.add(rank)
        items.append({"rank": rank, "asin": asin, "title": title, "price": price,
                      "price_num": price_num, "currency": currency,
                      "rating": rating, "reviews": reviews, "img": img, "url": url})

    items.sort(key=lambda x: x["rank"])
    # Amazon の index は 0 始まりのことがあるので 1..N に振り直す
    for i, it in enumerate(items, 1):
        it["rank"] = i
    return items


# ---------------------------------------------------------------------------
# 楽天市場ランキング
# ---------------------------------------------------------------------------
MOVE_LABEL = {"Up!": "↑", "Down!": "↓", "Stay!": "→", "New!": "NEW"}


def fetch_rakuten(period, genre_id):
    url = f"https://ranking.rakuten.co.jp/{period}/{genre_id}/" if genre_id else \
          f"https://ranking.rakuten.co.jp/{period}/"
    return parse_rakuten(jina_html(url, wait_selector=".rnkRanking_itemName"))


def parse_rakuten(page_html):
    """楽天ランキング HTML → (更新日時文字列, items)"""
    blocks = re.split(r'<div class="rnkRanking_(?:top3box|after4box)', page_html)
    items, seen = [], set()
    for seg in blocks[1:]:
        m = re.search(r'rnkRanking_rankIcon[^>]*>\s*<img[^>]*alt="(\d+)位"', seg)
        if not m:
            m = re.search(r'rnkRanking_dispRank">\s*(\d+)', seg)
        if not m:
            continue
        rank = int(m.group(1))
        if rank in seen:
            continue

        m = re.search(r'rnkRanking_itemName"><a href="([^"]+)"[^>]*>(.*?)</a>', seg, re.S)
        if not m:
            continue
        url = htmlmod.unescape(m.group(1))
        name = _clean_spaces(_text(m.group(2)))
        if not name:
            continue

        m = re.search(r'rnkRanking_shop"><a[^>]*>(.*?)</a>', seg, re.S)
        shop = _clean_spaces(_text(m.group(1))) if m else ""
        m = re.search(r'rnkRanking_price">(.*?)</div>', seg, re.S)
        price = _clean_spaces(_text(m.group(1))) if m else ""
        m = re.search(r"レビュー\(([\d,]+)件\)", seg)
        reviews = int(m.group(1).replace(",", "")) if m else None
        stars = seg.count('class="rnkRanking_starON') + 0.5 * seg.count('class="rnkRanking_starHALF')
        rating = stars if stars else None
        m = re.search(r'rnkRanking_imageBox">\s*<a[^>]*>.*?<img[^>]*src="([^"]+)"', seg, re.S)
        if not m:
            m = re.search(r'src="(https://tshop\.r10s\.jp/[^"]+)"', seg)
        img = htmlmod.unescape(m.group(1)) if m else ""
        if img.startswith("//"):
            img = "https:" + img
        m = re.search(r'rnkRanking_preRank.*?alt="(Up!|Down!|Stay!|New!)"', seg, re.S)
        movement = MOVE_LABEL.get(m.group(1), "") if m else ""

        seen.add(rank)
        items.append({"rank": rank, "title": name, "price": price, "shop": shop,
                      "rating": round(rating, 1) if rating else None,
                      "reviews": reviews, "img": img, "url": re.sub(r"[?#].*$", "", url),
                      "movement": movement})

    items.sort(key=lambda x: x["rank"])
    m = re.search(r"(20\d{2}年\d{1,2}月\d{1,2}日\(.\)更新)", page_html)
    updated = m.group(1) if m else ""
    return updated, items


# ---------------------------------------------------------------------------
# 東京IPO IPOスケジュール
# ---------------------------------------------------------------------------
class _IpoTableParser(HTMLParser):
    """table.iposchedulelist を行×セルで取り出す（EUC-JPデコード済みテキスト用）。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth_table = 0
        self.in_table = False
        self.rows = []       # [{"attrs": {...}, "cells": [{tag, attrs, text}]}]
        self.cur_row = None
        self.cur_cell = None

    def handle_starttag(self, tag, attrs):
        ad = dict(attrs)
        if tag == "table":
            if not self.in_table and "iposchedulelist" in (ad.get("class") or ""):
                self.in_table = True
                self.depth_table = 1
            elif self.in_table:
                self.depth_table += 1
            return
        if not self.in_table:
            return
        if tag == "tr":
            self.cur_row = {"attrs": ad, "cells": []}
        elif tag in ("td", "th") and self.cur_row is not None:
            self.cur_cell = {"tag": tag, "attrs": ad, "text": ""}
        elif tag in ("br",) and self.cur_cell is not None:
            self.cur_cell["text"] += "\n"

    def handle_endtag(self, tag):
        if tag == "table":
            if self.in_table:
                self.depth_table -= 1
                if self.depth_table <= 0:
                    self.in_table = False
            return
        if not self.in_table:
            return
        if tag in ("td", "th") and self.cur_cell is not None and self.cur_row is not None:
            self.cur_row["cells"].append(self.cur_cell)
            self.cur_cell = None
        elif tag == "tr" and self.cur_row is not None:
            if self.cur_row["cells"]:
                self.rows.append(self.cur_row)
            self.cur_row = None

    def handle_data(self, data):
        if self.cur_cell is not None:
            self.cur_cell["text"] += data


def _norm(s):
    if s is None:
        return ""
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = s.replace("\xa0", " ").replace("\u200e", "").replace("\u200f", "").replace("\u200b", "")
    s = s.replace("\ufffd", "")
    s = re.sub(r"[ \t\u3000]*\n[ \t\u3000]*", "\n", s)
    s = re.sub(r"[ \t\u3000]+", " ", s)
    return s.strip()


def fetch_tokyoipo(year):
    raw = http_get(f"{TOKYOIPO_URL}?j_e=J", data={"yr": str(year)},
                   headers={"Referer": TOKYOIPO_URL}, timeout=50, retries=3)
    text = raw.decode("euc-jp", "replace")
    if "iposchedulelist" not in text:
        raise RuntimeError(f"tokyoipo: schedule table not found ({year})")
    return text


def parse_tokyoipo(page_text, year):
    parser = _IpoTableParser()
    parser.feed(page_text)
    entries, cur = [], None
    tops = []
    for row in parser.rows:
        cells = row["cells"]
        cls = row["attrs"].get("class") or ""
        if "iposchedulelist_tr_top" in cls:
            cur = {"year": year, "name": "", "href": "", "date_md": "", "fields": {}}
            entries.append(cur)
            tops.append(row)
            continue
        if cur is None:
            continue
        ths = [c for c in cells if c["tag"] == "th"]
        tds = [c for c in cells if c["tag"] == "td"]
        if not ths:
            continue
        if tds and tds[0]["attrs"].get("rowspan") and not cur["date_md"]:
            cur["date_md"] = _norm(tds[0]["text"])
            tds = tds[1:]
        for i, th in enumerate(ths):
            label = _norm(th["text"])
            if not label:
                continue
            cur["fields"][label] = _norm(tds[i]["text"]) if i < len(tds) else ""

    # 銘柄名・詳細URLは tr_top 行の h2>a から（行内テキストには隣接リンクが混ざるため使わない）
    segs = re.findall(r'<tr class="iposchedulelist_tr_top">(.*?)</tr>', page_text, re.S)
    for e, seg in zip(entries, segs):
        m = re.search(r'<h2[^>]*>\s*<a href="([^"]+)"[^>]*>(.*?)</a>', seg, re.S)
        if m:
            e["href"] = htmlmod.unescape(m.group(1))
            e["name"] = _clean_spaces(_text(re.sub(r"<img[^>]*>", "", m.group(2))))
    return [e for e in entries if e["name"]]
