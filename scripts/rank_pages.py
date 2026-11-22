#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rank_pages.py — ランキング・調査データを「HTML一覧＋CSV/Excelダウンロード」として
GitHub Pages に自動生成する（GitHub Actions 上で外部pipなしに動く＝完全0トークン）。

対象（すべて公開データ・認証不要）:
  amazon    : Amazon.co.jp 売れ筋ランキング（ビューティー系61カテゴリ・上位30）
  rakuten   : 楽天市場ランキング（美容/スキンケア系44カテゴリ × デイリー/週間・上位30）
  tokyoipo  : 東京IPO IPOスケジュール（今年・前年・全件）

更新頻度は状態ファイル scripts/rank_state.json で制御し、前回更新から一定時間
経過したときだけ取得する（毎時起動の build_all.py から呼ばれても普段は即終了）。

CLI:
  python3 scripts/rank_pages.py --run                 # 期限が来たものだけ更新
  python3 scripts/rank_pages.py --run --force         # 全部いますぐ更新
  python3 scripts/rank_pages.py --only amazon,tokyoipo --force
  python3 scripts/rank_pages.py --status              # 状態表示

出力:
  <root>/amazon-beauty.html, rakuten-skincare.html, tokyoipo.html
  <root>/downloads/amazon-beauty.{xlsx,csv} ほか
  <root>/scripts/rank_data/{amazon,rakuten,tokyoipo}.json （スナップショット）
"""
import argparse
import concurrent.futures
import datetime
import html
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rank_scrapers as rs  # noqa: E402
from xlsx_lite import write_xlsx, write_csv  # noqa: E402

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT_DIR, "scripts", "rank_data")
DL_DIR = os.path.join(ROOT_DIR, "downloads")
STATE_PATH = os.path.join(ROOT_DIR, "scripts", "rank_state.json")
JST = datetime.timezone(datetime.timedelta(hours=9))

AMAZON_TOP = 30
RAKUTEN_TOP = 20
WORKERS = 4

# 前回更新から何時間で再取得するか（Actionsは毎時0分に走る）
INTERVALS = {"amazon": 12, "rakuten": 24, "tokyoipo": 6}

RAKUTEN_CATEGORIES = [
    ("all", "楽天総合", None),
    ("beauty", "美容・コスメ・香水", "100939"),
    ("skincare", "スキンケア", "100944"),
    ("base_makeup", "ベースメイク・メイクアップ", "204233"),
    ("sunscreen", "日焼け止め・UVケア", "216492"),
    ("makeup_tools", "メイク道具・ケアグッズ", "111787"),
    ("haircare", "ヘアケア・スタイリング", "100940"),
    ("fragrance", "香水・フレグランス", "111120"),
    ("aroma", "アロマ・お香", "100878"),
    ("bodycare", "ボディケア", "100960"),
    ("nail", "ネイル", "201454"),
    ("shaving", "シェービング・脱毛・脱色", "210462"),
    ("beauty_device", "美顔器・スチーマー", "204245"),
    ("korean_cosme", "韓国コスメ", "564517"),
    ("ingredient", "化粧品原料", "567431"),
    ("cleansing", "クレンジング", "210450"),
    ("face_wash", "洗顔料", "216301"),
    ("lotion", "化粧水・ローション", "216307"),
    ("mist", "保湿ミスト", "567441"),
    ("serum", "美容液", "216348"),
    ("emulsion", "乳液・ミルク", "216387"),
    ("face_cream", "フェイスクリーム", "216424"),
    ("face_oil", "フェイスオイル・バーム", "216461"),
    ("gel", "保湿ジェル", "567440"),
    ("all_in_one", "オールインワン化粧品", "563728"),
    ("gel_pack", "ジェルパック・クリーム", "216467"),
    ("sheet_mask", "シートマスク・フェイスパック", "503020"),
    ("peeling", "ピーリング・ゴマージュ", "503044"),
    ("eye_serum", "目元美容液", "509822"),
    ("eye_cream", "アイクリーム・ジェル", "509843"),
    ("eye_patch", "目元・口元ケアパッチ", "569360"),
    ("lip_cream", "リップクリーム", "405206"),
    ("lip_plumper", "リッププランパー", "568658"),
    ("set", "セット", "567442"),
    ("diet_health", "ダイエット・健康", "100938"),
    ("diet", "ダイエット", "100984"),
    ("supplement", "サプリメント", "563727"),
    ("health_food", "健康食品", "100987"),
    ("dental", "デンタルケア", "204745"),
    ("baby_lotion", "ベビーローション・オイル", "205205"),
    ("baby_powder", "ベビーパウダー", "205206"),
    ("baby_cream", "ベビークリーム", "401164"),
    ("baby_lip", "ベビーリップ", "401165"),
    ("tv_game", "テレビゲーム", "101205"),
]


# ---------------------------------------------------------------------------
# 共通ユーティリティ
# ---------------------------------------------------------------------------
def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))


def now_jst():
    return datetime.datetime.now(JST)


def iso_now():
    return now_jst().strftime("%Y-%m-%dT%H:%M:%S+09:00")


def ymd(iso):
    return (iso or "").replace("T", " ")[:16]


def amazon_categories():
    path = os.path.join(DATA_DIR, "amazon_categories.json")
    cats = load_json(path, None)
    if cats:
        return cats
    return [{"name": "ビューティー総合", "url": "https://www.amazon.co.jp/gp/bestsellers/beauty",
             "group": "スキンケア"}]


def price_to_int(s):
    """'1,980円' / '¥1,980' / 'USD 6.94' から数値を取り出す。"""
    if not s:
        return None
    s = str(s)
    if "USD" in s or "$" in s:
        m = re.search(r"([\d,]+(?:\.\d+)?)", s.replace(",", ""))
        return None if not m else None  # ドルは別途換算
    m = re.search(r"([\d,]+)\s*円", s)
    if m:
        return int(m.group(1).replace(",", ""))
    return None


def usd_amount(s):
    m = re.search(r"USD\s*([\d,]+(?:\.\d+)?)", str(s or ""))
    return float(m.group(1).replace(",", "")) if m else None


def fetch_usd_jpy():
    try:
        raw = rs.http_get("https://open.er-api.com/v6/latest/USD", timeout=20, retries=2)
        data = json.loads(raw.decode("utf-8"))
        rate = float(data.get("rates", {}).get("JPY") or 0)
        if 80 < rate < 300:
            return rate
    except Exception as e:  # noqa: BLE001
        print(f"[warn] FX rate fetch failed: {e}", file=sys.stderr)
    return None


# ---------------------------------------------------------------------------
# 取得
# ---------------------------------------------------------------------------
def gather_amazon(prev_rows, rate):
    cats = amazon_categories()
    prev_rank = {}
    for r in prev_rows:
        if r.get("asin"):
            prev_rank[(r.get("c"), r.get("asin"))] = r.get("r")
    rows, ok_cats, failed = [], 0, []

    def work(cat):
        return cat, rs.fetch_amazon(cat["url"])

    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(work, c): c for c in cats}
        for fut in concurrent.futures.as_completed(futs):
            cat = futs[fut]
            try:
                _, items = fut.result()
            except Exception as e:  # noqa: BLE001
                failed.append(f"{cat['name']}: {e}")
                print(f"[warn] amazon {cat['name']}: {e}", file=sys.stderr)
                continue
            items = items[:AMAZON_TOP]
            if not items:
                failed.append(f"{cat['name']}: 0件")
                continue
            ok_cats += 1
            for it in items:
                p = it.get("price") or ""
                cur = it.get("currency") or ""
                num = it.get("price_num")
                pj = None
                if num is not None:
                    pj = int(round(num)) if cur == "JPY" else (
                        int(round(num * rate)) if (cur == "USD" and rate) else None)
                old = prev_rank.get((cat["name"], it["asin"]))
                if old is None:
                    delta = "new"
                elif old == it["rank"]:
                    delta = 0
                else:
                    delta = old - it["rank"]  # プラス＝順位上昇
                rows.append({"g": "amazon", "c": cat["name"], "cg": cat.get("group", ""),
                             "r": it["rank"], "t": it["title"], "p": p, "pj": pj,
                             "d": delta, "rt": it.get("rating"), "rv": it.get("reviews"),
                             "i": it.get("img") or "", "u": it.get("url") or "",
                             "id": it.get("asin") or ""})
    meta = {"ok_categories": ok_cats, "total_categories": len(cats), "failed": failed[:10],
            "usd_jpy": rate}
    if ok_cats == 0:
        raise RuntimeError("amazon: 全カテゴリ取得失敗")
    if ok_cats < len(cats) * 0.5:
        raise RuntimeError(f"amazon: 取得成功 {ok_cats}/{len(cats)} が半数未満のため中止")
    return rows, meta


def gather_rakuten(prev_rows, _rate):
    prev_rank = {}
    for r in prev_rows:
        prev_rank[(r.get("g"), r.get("c"), r.get("u"))] = r.get("r")
    rows, ok_cats, failed = [], 0, []

    def work(item):
        period, name, gid = item
        updated, items = rs.fetch_rakuten(period, gid)
        return item, updated, items

    tasks = [(p, name, gid) for (p, label) in (("daily", "デイリー"), ("weekly", "週間"))
             for (_, name, gid) in RAKUTEN_CATEGORIES]
    labels = {"daily": "デイリー", "weekly": "週間"}
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(work, t): t for t in tasks}
        for fut in concurrent.futures.as_completed(futs):
            t = futs[fut]
            period, name, _gid = t
            try:
                _, updated, items = fut.result()
            except Exception as e:  # noqa: BLE001
                failed.append(f"{labels[period]}/{name}: {e}")
                print(f"[warn] rakuten {period} {name}: {e}", file=sys.stderr)
                continue
            items = items[:RAKUTEN_TOP]
            if not items:
                failed.append(f"{labels[period]}/{name}: 0件")
                continue
            ok_cats += 1
            for it in items:
                old = prev_rank.get((period, name, it.get("url")))
                if old is None:
                    delta = "new" if it.get("movement") == "NEW" else None
                else:
                    delta = old - it["rank"]
                rows.append({"g": period, "c": name, "r": it["rank"], "t": it["title"],
                             "p": it.get("price") or "",
                             "pj": price_to_int(it.get("price") or ""), "d": delta,
                             "s": it.get("shop") or "", "rt": it.get("rating"),
                             "rv": it.get("reviews"), "i": it.get("img") or "",
                             "u": it.get("url") or "", "mv": it.get("movement") or "",
                             "up": updated})
    meta = {"ok_categories": ok_cats, "total_categories": len(tasks), "failed": failed[:10]}
    try:
        meta["source_updated"] = rows[0].get("up") if rows else ""
    except Exception:  # noqa: BLE001
        pass
    if ok_cats == 0:
        raise RuntimeError("rakuten: 全カテゴリ取得失敗")
    if ok_cats < len(tasks) * 0.5:
        raise RuntimeError(f"rakuten: 取得成功 {ok_cats}/{len(tasks)} が半数未満のため中止")
    return rows, meta


WEEKDAY_JA = "月火水木金土日"


def gather_tokyoipo(prev_rows, _rate):
    years = [now_jst().year, now_jst().year - 1]
    rows, meta_errors = [], []
    for y in years:
        try:
            txt = rs.fetch_tokyoipo(y)
            entries = rs.parse_tokyoipo(txt, y)
        except Exception as e:  # noqa: BLE001
            meta_errors.append(f"{y}: {e}")
            print(f"[warn] tokyoipo {y}: {e}", file=sys.stderr)
            continue
        for e in entries:
            f = e["fields"]
            code_cell = f.get("コード", "")
            m = re.match(r"^\s*([0-9A-Z]{3,5})\s*(?:[（(]([^）)]*)[）)])?", code_cell)
            code = m.group(1) if m else code_cell.split("\n")[0]
            market = (m.group(2) if (m and m.group(2)) else "")
            if not market:
                mm = re.search(r"[（(]([^）)]+)[）)]", code_cell)
                market = mm.group(1) if mm else ""
            date_iso, weekday = "", ""
            dm = re.match(r"^(\d{1,2})/(\d{1,2})$", e.get("date_md", ""))
            if dm:
                try:
                    d = datetime.date(y, int(dm.group(1)), int(dm.group(2)))
                    date_iso = d.isoformat()
                    weekday = WEEKDAY_JA[d.weekday()]
                except ValueError:
                    pass
            price = f.get("公開価格", "")
            pm = re.match(r"^([\d,]+)\s*円$", price)
            initial = f.get("初値", "")
            im = re.match(r"^([\d,]+)\s*円$", initial)
            chg = f.get("騰落率", "")
            cm = re.match(r"^(-?[\d.]+)\s*%$", chg)
            href = e.get("href", "")
            if href.startswith("/"):
                href = "https://www.tokyoipo.com" + href
            rows.append({
                "g": str(y), "c": market or "その他", "r": None,
                "t": e["name"], "code": code, "market": market,
                "date": date_iso, "date_md": e.get("date_md", ""), "wd": weekday,
                "shares": f.get("公開株数", ""), "kari": f.get("仮条件", ""),
                "p": price, "pj": int(pm.group(1).replace(",", "")) if pm else None,
                "bb": f.get("BB期間", ""), "initial": initial,
                "pj2": int(im.group(1).replace(",", "")) if im else None,
                "chg": chg, "chgp": float(cm.group(1)) / 100 if cm else None,
                "lead": f.get("主幹事", ""), "biz": f.get("事業内容", ""),
                "status": "上場済み" if initial else "上場予定",
                "u": href, "i": "",
            })
    if not rows:
        raise RuntimeError("tokyoipo: 取得0件")
    # 年内の上場日順に並べる（日付なしは末尾）
    rows.sort(key=lambda r: (r["g"], r.get("date") or "9999"))
    return rows, {"errors": meta_errors}


# ---------------------------------------------------------------------------
# 出力（HTML / CSV / XLSX）
# ---------------------------------------------------------------------------
SOURCES = {
    "amazon": {
        "title": "🛒 Amazon 美容・スキンケア 売れ筋ランキング",
        "page": "amazon-beauty.html",
        "xlsx": "amazon-beauty.xlsx",
        "csv": "amazon-beauty.csv",
        "note": "Amazon.co.jp 売れ筋ランキング（ビューティー系61カテゴリ・各上位30件）",
        "group_label": "カテゴリ",
        "default_sort": "rank",
        "cols": [
            {"k": "r", "l": "順位", "t": "rank", "w": 56},
            {"k": "d", "l": "前回比", "t": "delta", "w": 62},
            {"k": "i", "l": "", "t": "img", "w": 52},
            {"k": "t", "l": "商品名", "t": "title"},
            {"k": "pj", "l": "価格(円)", "t": "yen", "w": 84},
            {"k": "rt", "l": "評価", "t": "rating", "w": 64},
            {"k": "rv", "l": "レビュー", "t": "int", "w": 72},
        ],
    },
    "rakuten": {
        "title": "🛍 楽天市場 美容・スキンケア ランキング",
        "page": "rakuten-skincare.html",
        "xlsx": "rakuten-skincare.xlsx",
        "csv": "rakuten-skincare.csv",
        "note": "楽天市場ランキング（美容・スキンケア系44カテゴリ × デイリー/週間・各上位20件）",
        "group_label": "期間",
        "default_sort": "rank",
        "cols": [
            {"k": "r", "l": "順位", "t": "rank", "w": 56},
            {"k": "d", "l": "前回比", "t": "delta", "w": 62},
            {"k": "i", "l": "", "t": "img", "w": 52},
            {"k": "t", "l": "商品名", "t": "title"},
            {"k": "p", "l": "価格", "t": "text", "w": 88},
            {"k": "s", "l": "ショップ", "t": "text", "w": 140},
            {"k": "rt", "l": "評価", "t": "rating", "w": 56},
            {"k": "rv", "l": "レビュー", "t": "int", "w": 72},
        ],
    },
    "tokyoipo": {
        "title": "📈 東京IPO 新規上場スケジュール",
        "page": "tokyoipo.html",
        "xlsx": "tokyoipo-ipo-schedule.xlsx",
        "csv": "tokyoipo-ipo-schedule.csv",
        "note": "東京IPO「IPOスケジュール一覧」今年・前年の全件（公開価格・初値・騰落率・主幹事）",
        "group_label": "年",
        "default_sort": "date",
        "cols": [
            {"k": "date", "l": "上場日", "t": "date", "w": 96},
            {"k": "code", "l": "コード", "t": "text", "w": 64},
            {"k": "t", "l": "銘柄名", "t": "title"},
            {"k": "market", "l": "市場", "t": "text", "w": 120},
            {"k": "pj", "l": "公開価格", "t": "yen", "w": 84},
            {"k": "pj2", "l": "初値", "t": "yen", "w": 84},
            {"k": "chgp", "l": "騰落率", "t": "pct", "w": 72},
            {"k": "lead", "l": "主幹事", "t": "text", "w": 150},
        ],
    },
}

XLSX_HEADERS = {
    "amazon": ["カテゴリ", "区分", "順位", "前回比", "ASIN", "商品名", "価格(取得元表示)",
               "価格(円)", "評価", "レビュー数", "商品URL", "画像URL"],
    "rakuten": ["期間", "カテゴリ", "順位", "前回比", "商品名", "価格", "ショップ", "評価",
                "レビュー数", "ランキング更新", "商品URL", "画像URL"],
    "tokyoipo": ["年", "上場日", "曜日", "コード", "銘柄名", "市場", "公開株数", "仮条件",
                 "公開価格", "BB期間", "初値", "騰落率", "主幹事", "状態", "事業内容", "詳細URL"],
}

GROUP_LABELS = {"amazon": {"amazon": "全カテゴリ"}, "rakuten": {"daily": "デイリー", "weekly": "週間"}}


def delta_text(d, mv=""):
    if d == "new":
        return "NEW"
    if isinstance(d, int):
        if d > 0:
            return f"↑{d}"
        if d < 0:
            return f"↓{-d}"
        return "→"
    return mv or ""


def xlsx_rows(key, rows):
    out = []
    if key == "amazon":
        for r in rows:
            out.append([r["c"], r.get("cg", ""), r["r"], delta_text(r.get("d")),
                        r.get("id", ""), r["t"], r.get("p", ""), r.get("pj"),
                        r.get("rt"), r.get("rv"), r.get("u", ""), r.get("i", "")])
    elif key == "rakuten":
        for r in rows:
            out.append([GROUP_LABELS["rakuten"].get(r["g"], r["g"]), r["c"], r["r"],
                        delta_text(r.get("d"), r.get("mv", "")), r["t"], r.get("p", ""),
                        r.get("s", ""), r.get("rt"), r.get("rv"), r.get("up", ""),
                        r.get("u", ""), r.get("i", "")])
    else:
        for r in rows:
            out.append([r["g"], r.get("date", ""), r.get("wd", ""), r.get("code", ""),
                        r["t"], r.get("market", ""), r.get("shares", ""), r.get("kari", ""),
                        r.get("p", ""), r.get("bb", ""), r.get("initial", ""),
                        r.get("chg", ""), r.get("lead", ""), r.get("status", ""),
                        r.get("biz", ""), r.get("u", "")])
    return out


def write_outputs(key, snap):
    cfg = SOURCES[key]
    rows = snap["rows"]
    os.makedirs(DL_DIR, exist_ok=True)

    # CSV
    write_csv(os.path.join(DL_DIR, cfg["csv"]), XLSX_HEADERS[key], xlsx_rows(key, rows))

    # XLSX（一覧＋サマリー）
    widths = {"amazon": [12, 10, 6, 8, 13, 52, 16, 10, 7, 10, 34, 34],
              "rakuten": [8, 18, 6, 8, 52, 12, 24, 7, 10, 18, 34, 34],
              "tokyoipo": [7, 11, 4, 8, 26, 16, 18, 14, 10, 16, 10, 8, 18, 9, 50, 34]}
    link_idx = {"amazon": [10], "rakuten": [10], "tokyoipo": [15]}[key]
    cols = []
    for i, h in enumerate(XLSX_HEADERS[key]):
        c = {"header": h, "width": widths[key][i]}
        if i in link_idx:
            c["link"] = True
        elif h in ("順位",):
            c["num"] = int
        elif h == "価格(円)" or h == "レビュー数":
            c["num"] = int
        elif h == "騰落率":
            c["pct"] = True
        elif h in ("評価",):
            c["num"] = float
        elif h == "事業内容":
            c["wrap"] = True
        cols.append(c)
    summary = [
        ["生成日時", ymd(snap.get("generated_at"))],
        ["対象", cfg["note"]],
        ["件数", len(rows)],
    ]
    for k, v in (snap.get("meta") or {}).items():
        if isinstance(v, (str, int, float)) and k != "failed":
            summary.append([k, v])
    write_xlsx(os.path.join(DL_DIR, cfg["xlsx"]),
               [{"name": "一覧", "columns": cols, "rows": xlsx_rows(key, rows)},
                {"name": "サマリー", "columns": [{"header": "項目", "width": 26},
                                              {"header": "値", "width": 60}],
                 "rows": summary, "autofilter": False}],
               title=cfg["title"])

    # HTML
    html_text = render_html(key, snap)
    with open(os.path.join(ROOT_DIR, cfg["page"]), "w", encoding="utf-8") as f:
        f.write(html_text)
    return os.path.join(ROOT_DIR, cfg["page"])


def _js_rows(key, rows):
    """HTML 埋め込み用に余計なキーを落とす。"""
    keep = {"amazon": ("g", "c", "cg", "r", "t", "p", "pj", "d", "rt", "rv", "i", "u", "id"),
            "rakuten": ("g", "c", "r", "t", "p", "pj", "d", "s", "rt", "rv", "i", "u", "mv"),
            "tokyoipo": ("g", "c", "r", "t", "code", "market", "date", "wd", "p", "pj",
                         "pj2", "chg", "chgp", "lead", "status", "u", "shares", "kari",
                         "bb", "initial", "biz", "date_md")}[key]
    out = []
    for r in rows:
        d = {k: r.get(k) for k in keep if r.get(k) not in (None, "")}
        if d.get("r") is not None:
            d["r"] = r["r"]
        out.append(d)
    return out


def render_html(key, snap):
    cfg = SOURCES[key]
    rows = snap["rows"]
    groups = []
    for r in rows:
        g = str(r.get("g"))
        if g not in groups:
            groups.append(g)
    if key == "amazon":
        group_opts = [{"k": g, "l": GROUP_LABELS["amazon"].get(g, g)} for g in groups]
    elif key == "rakuten":
        order = ["daily", "weekly"]
        group_opts = [{"k": g, "l": GROUP_LABELS["rakuten"].get(g, g)} for g in order if g in groups]
    else:
        group_opts = [{"k": g, "l": f"{g}年（{sum(1 for r in rows if str(r.get('g')) == g)}件）"}
                      for g in sorted(groups, reverse=True)]

    cats = []
    for r in rows:
        c = r.get("c") or "その他"
        if c not in cats:
            cats.append(c)
    cats.sort()

    updated = ymd(snap.get("generated_at"))
    meta = snap.get("meta") or {}
    add_note = ""
    if key == "amazon" and meta.get("usd_jpy"):
        add_note = (f"※Amazonが価格を米ドル建てで返すため、円は 1USD={meta['usd_jpy']:.1f}円 で"
                    f"換算した参考値（取得元表示も併記）。")
    data = {"groups": group_opts, "cats": cats, "rows": _js_rows(key, rows),
            "updated": updated, "note": cfg["note"],
            "defaultSort": cfg.get("default_sort", "rank")}
    cols = cfg["cols"]
    js_data = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    js_cols = json.dumps([{k: v for k, v in c.items() if k != "t2"} for c in cols],
                         ensure_ascii=False)

    return _PAGE_TEMPLATE \
        .replace("__TITLE__", html.escape(cfg["title"])) \
        .replace("__NOTE__", html.escape(cfg["note"])) \
        .replace("__ADDN__", html.escape(add_note)) \
        .replace("__UPDATED__", html.escape(updated)) \
        .replace("__COUNT__", str(len(rows))) \
        .replace("__GROUP_LABEL__", html.escape(cfg["group_label"])) \
        .replace("__XLSX__", cfg["xlsx"]) \
        .replace("__CSV__", cfg["csv"]) \
        .replace("__DATA__", js_data) \
        .replace("__COLS__", js_cols)


_PAGE_TEMPLATE = r"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
  :root { --main:#145343; --line:#e5e7eb; }
  * { box-sizing: border-box; }
  body { font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Hiragino Sans",sans-serif;
         background:#f4f6f5; color:#1f2937; margin:0; padding:14px; }
  .wrap { max-width: 980px; margin:0 auto; }
  h1 { font-size:19px; margin:0 0 4px; }
  .sub { font-size:12px; color:#6b7280; margin-bottom:2px; }
  .sub2 { font-size:11px; color:#9ca3af; margin-bottom:10px; }
  .dl { display:flex; gap:8px; flex-wrap:wrap; margin:10px 0 12px; }
  .dl a { display:inline-block; font-size:13px; font-weight:600; text-decoration:none;
          color:#fff; background:var(--main); padding:8px 14px; border-radius:8px; }
  .dl a.alt { background:#fff; color:var(--main); border:1.5px solid var(--main); }
  .bar { display:flex; gap:8px; flex-wrap:wrap; align-items:center; margin-bottom:10px; }
  .tabs { display:flex; gap:6px; overflow-x:auto; }
  .tab { border:1px solid #d1d5db; background:#fff; border-radius:999px; padding:6px 13px;
         font-size:13px; cursor:pointer; white-space:nowrap; color:#374151; }
  .tab.active { background:var(--main); color:#fff; border-color:var(--main); font-weight:700; }
  input[type=search], select { font-size:13px; padding:7px 10px; border:1px solid #d1d5db;
         border-radius:8px; background:#fff; color:#1f2937; }
  input[type=search] { flex:1 1 180px; min-width:150px; }
  .count { font-size:12px; color:#6b7280; margin:2px 0 8px; }
  .tblwrap { background:#fff; border:1px solid var(--line); border-radius:12px; overflow:auto;
             max-height:78vh; }
  table { border-collapse:collapse; width:100%; font-size:13px; }
  thead th { position:sticky; top:0; background:#eef3f1; color:#0f3d31; text-align:left;
             font-size:12px; padding:8px 8px; border-bottom:2px solid var(--main); white-space:nowrap; z-index:2; }
  tbody td { padding:7px 8px; border-bottom:1px solid #f3f4f6; vertical-align:middle; }
  tbody tr:hover { background:#f9fafb; }
  td.rank { font-weight:700; color:var(--main); text-align:right; white-space:nowrap; }
  td.num, td.yen, td.pct { text-align:right; white-space:nowrap; }
  td.title { min-width:220px; line-height:1.4; }
  td.title a { color:#111827; text-decoration:none; }
  td.title a:hover { color:var(--main); text-decoration:underline; }
  td.img img { width:40px; height:40px; object-fit:contain; background:#fff; border-radius:4px; }
  .up { color:#059669; font-weight:700; font-size:12px; white-space:nowrap; }
  .down { color:#dc2626; font-weight:700; font-size:12px; white-space:nowrap; }
  .flat { color:#9ca3af; font-weight:700; font-size:12px; }
  .new { color:#7c3aed; font-weight:700; font-size:12px; }
  .chip { display:inline-block; font-size:11px; padding:1px 7px; border-radius:999px;
          background:#eef3f1; color:#145343; margin-right:4px; white-space:nowrap; }
  .chip.mid { background:#fef3c7; color:#92400e; }
  .biz { color:#6b7280; font-size:12px; }
  .more { display:block; margin:10px auto; padding:9px 18px; font-size:13px; cursor:pointer;
          border:1px solid #d1d5db; background:#fff; border-radius:8px; }
  footer { font-size:11px; color:#9ca3af; margin:14px 0 30px; line-height:1.6; }
</style>
</head>
<body>
<div class="wrap">
  <h1>__TITLE__</h1>
  <div class="sub">更新: __UPDATED__ JST ・ 計 __COUNT__ 件</div>
  <div class="sub2">__NOTE__</div>
  <div class="dl">
    <a href="./downloads/__XLSX__">⬇ Excel (.xlsx)</a>
    <a class="alt" href="./downloads/__CSV__">⬇ CSV</a>
    <a class="alt" href="./index.html">← ダッシュボード一覧</a>
  </div>
  <div class="bar">
    <div class="tabs" id="tabs"></div>
  </div>
  <div class="bar">
    <select id="cat"></select>
    <input type="search" id="q" placeholder="商品名・銘柄名で検索">
    <select id="sort">
      <option value="rank">並び: 順位順</option>
      <option value="-rt">並び: 評価が高い順</option>
      <option value="-rv">並び: レビューが多い順</option>
      <option value="pj">並び: 価格が安い順</option>
      <option value="-pj">並び: 価格が高い順</option>
      <option value="date">並び: 日付順</option>
    </select>
  </div>
  <div class="count" id="count"></div>
  <div class="tblwrap">
    <table>
      <thead><tr id="thead"></tr></thead>
      <tbody id="tbody"></tbody>
    </table>
  </div>
  <button class="more" id="more" hidden>さらに表示</button>
  <footer>
    出典: __NOTE__（公開ページをGitHub Actionsが定期取得。無料・0トークンで自動更新）<br>
    __ADDN__
  </footer>
</div>
<script>
const DATA = __DATA__;
const COLS = __COLS__;
const GROUPS = DATA.groups.map(g => g.k);
const PAGE = 120;
let S = { g: GROUPS[0], c: "", q: "", sort: DATA.defaultSort || "rank", shown: PAGE };

function esc(s){ return String(s == null ? "" : s).replace(/[&<>"]/g, m => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[m])); }
function fmtYen(v){ return v == null ? "" : "¥" + Number(v).toLocaleString("ja-JP"); }
function fmtPct(v){ return v == null ? "" : (v * 100).toFixed(1) + "%"; }

function rowsOf(){
  let rows = DATA.rows.filter(r => String(r.g) === String(S.g));
  if (S.c) rows = rows.filter(r => (r.c || "その他") === S.c);
  if (S.q) {
    const q = S.q.toLowerCase();
    rows = rows.filter(r => [r.t, r.s, r.code, r.market, r.lead].some(v => v && String(v).toLowerCase().includes(q)));
  }
  const s = S.sort;
  if (s === "rank") rows.sort((a,b) => (a.r == null ? 9e9 : a.r) - (b.r == null ? 9e9 : b.r) || String(a.c).localeCompare(String(b.c), "ja"));
  else if (s === "date") rows.sort((a,b) => String(a.date || "9999").localeCompare(String(b.date || "9999")));
  else if (s === "-rt") rows.sort((a,b) => (b.rt||0) - (a.rt||0));
  else if (s === "-rv") rows.sort((a,b) => (b.rv||0) - (a.rv||0));
  else if (s === "pj") rows.sort((a,b) => (a.pj == null ? 9e12 : a.pj) - (b.pj == null ? 9e12 : b.pj));
  else if (s === "-pj") rows.sort((a,b) => (b.pj||0) - (a.pj||0));
  return rows;
}

function deltaCell(d, mv){
  if (d === "new") return '<span class="new">NEW</span>';
  if (typeof d === "number") {
    if (d > 0) return '<span class="up">↑' + d + '</span>';
    if (d < 0) return '<span class="down">↓' + (-d) + '</span>';
    return '<span class="flat">→</span>';
  }
  if (mv === "NEW") return '<span class="new">NEW</span>';
  return mv ? '<span class="flat">' + esc(mv) + '</span>' : '';
}

function cell(col, r){
  switch (col.t) {
    case "rank": return r.r == null ? "" : '<td class="rank">' + r.r + '</td>';
    case "delta": return '<td>' + deltaCell(r.d, r.mv) + '</td>';
    case "img": return '<td class="img">' + (r.i ? '<img loading="lazy" src="' + esc(r.i) + '" alt="">' : '') + '</td>';
    case "title": {
      let extra = "";
      if (r.status) extra += '<span class="chip' + (r.status === "上場予定" ? " mid" : "") + '">' + esc(r.status) + '</span>';
      if (r.c && r.c !== "他の商品" && !r.date) extra = '<span class="chip">' + esc(r.c) + '</span>' + extra;
      const link = r.u ? '<a href="' + esc(r.u) + '" target="_blank" rel="noopener">' + esc(r.t) + '</a>' : esc(r.t);
      return '<td class="title">' + extra + link + '</td>';
    }
    case "yen": {
      if (col.k === "p" && r.pj == null) return '<td class="yen">' + esc(r.p || "") + '</td>';
      return '<td class="yen">' + fmtYen(r[col.k]) + '</td>';
    }
    case "pct": return '<td class="pct">' + fmtPct(r[col.k]) + '</td>';
    case "rating": return '<td class="num">' + (r.rt == null ? "" : "★" + Number(r.rt).toFixed(1)) + '</td>';
    case "int": return '<td class="num">' + (r[col.k] == null ? "" : Number(r[col.k]).toLocaleString("ja-JP")) + '</td>';
    case "date": return '<td>' + esc((r.date || r.date_md || "").replace(/-/g, "/")) + (r.wd ? "(" + esc(r.wd) + ")" : "") + '</td>';
    default: return '<td>' + esc(r[col.k]) + '</td>';
  }
}

function render(){
  const thead = document.getElementById("thead");
  thead.innerHTML = COLS.map(c => '<th' + (c.w ? ' style="width:' + c.w + 'px"' : '') + '>' + esc(c.l) + '</th>').join("");
  const rows = rowsOf();
  const shown = rows.slice(0, S.shown);
  document.getElementById("tbody").innerHTML = shown.map(r =>
    "<tr>" + COLS.map(c => cell(c, r)).join("") + "</tr>").join("");
  document.getElementById("count").textContent =
    rows.length + "件中 " + shown.length + "件表示" + (S.q ? "（検索: " + S.q + "）" : "");
  const more = document.getElementById("more");
  more.hidden = rows.length <= S.shown;
}

function setup(){
  document.getElementById("tabs").innerHTML = DATA.groups.map(g =>
    '<button class="tab' + (g.k === S.g ? " active" : "") + '" data-k="' + esc(g.k) + '">' + esc(g.l) + '</button>').join("");
  document.querySelectorAll("#tabs .tab").forEach(b => b.onclick = () => {
    S.g = b.dataset.k; S.shown = PAGE;
    document.querySelectorAll("#tabs .tab").forEach(x => x.classList.toggle("active", x === b));
    render();
  });
  const cats = DATA.cats.filter(c => DATA.rows.some(r => String(r.g) === String(S.g) && (r.c || "その他") === c));
  const sel = document.getElementById("cat");
  sel.innerHTML = '<option value="">すべてのカテゴリ</option>' +
    cats.map(c => '<option value="' + esc(c) + '">' + esc(c) + '</option>').join("");
  sel.onchange = () => { S.c = sel.value; S.shown = PAGE; render(); };
  document.getElementById("q").oninput = e => { S.q = e.target.value.trim(); S.shown = PAGE; render(); };
  document.getElementById("sort").onchange = e => { S.sort = e.target.value; render(); };
  const sortSel = document.getElementById("sort");
  sortSel.value = S.sort;
  if (!sortSel.value) sortSel.value = "rank";
  document.getElementById("more").onclick = () => { S.shown += PAGE; render(); };
  render();
}
setup();
</script>
</body>
</html>"""



# ---------------------------------------------------------------------------
# 状態・スケジューリング
# ---------------------------------------------------------------------------
def load_state():
    return load_json(STATE_PATH, {})


def save_state(st):
    save_json(STATE_PATH, st)


def is_due(key, state, force=False):
    if force:
        return True
    rec = (state or {}).get(key) or {}
    last = rec.get("built_at")
    if not last:
        return True
    try:
        t = datetime.datetime.strptime(last[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=JST)
    except Exception:  # noqa: BLE001
        return True
    return (now_jst() - t).total_seconds() >= INTERVALS[key] * 3600


def run_source(key, state, force=False):
    cfg = SOURCES[key]
    snap_path = os.path.join(DATA_DIR, f"{key}.json")
    prev = load_json(snap_path, {})
    prev_rows = prev.get("rows") or []
    rate = fetch_usd_jpy() if key == "amazon" else None
    t0 = time.time()
    if key == "amazon":
        rows, meta = gather_amazon(prev_rows, rate)
    elif key == "rakuten":
        rows, meta = gather_rakuten(prev_rows, rate)
    else:
        rows, meta = gather_tokyoipo(prev_rows, rate)
    snap = {"source": key, "generated_at": iso_now(), "rows": rows, "meta": meta}
    save_json(snap_path, snap)
    page = write_outputs(key, snap)
    dt = time.time() - t0
    state[key] = {"built_at": iso_now(), "rows": len(rows), "ok": True,
                  "seconds": round(dt, 1)}
    print(f"[rank] {key}: {len(rows)} rows → {os.path.basename(page)} ({dt:.0f}s)")
    return {"file": cfg["page"], "title": cfg["title"],
            "desc": f"{cfg['note']} ・ Excel/CSV付き ・ {ymd(snap['generated_at'])} 更新",
            "kind": "rank"}


def build_due(force=False, only=None):
    """build_all.py から呼ばれる。期限が来たソースだけ更新して index 用カードを返す。"""
    state = load_state()
    cards = []
    keys = [k for k in SOURCES if (not only or k in only)]
    for key in keys:
        cfg = SOURCES[key]
        try:
            if is_due(key, state, force):
                cards.append(run_source(key, state, force))
            else:
                snap = load_json(os.path.join(DATA_DIR, f"{key}.json"), {})
                rows = snap.get("rows") or []
                cards.append({"file": cfg["page"], "title": cfg["title"],
                              "desc": f"{cfg['note']} ・ Excel/CSV付き ・ "
                                      f"{ymd(snap.get('generated_at'))} 更新（{len(rows)}件）",
                              "kind": "rank"})
        except Exception as e:  # noqa: BLE001
            print(f"[error] rank_pages {key}: {e}", file=sys.stderr)
            rec = state.get(key) or {}
            rec["last_error"] = f"{iso_now()} {e}"[:300]
            state[key] = rec
            # 既存ページがあればカードは残す（中身は前回のまま）
            if os.path.exists(os.path.join(ROOT_DIR, cfg["page"])):
                snap = load_json(os.path.join(DATA_DIR, f"{key}.json"), {})
                cards.append({"file": cfg["page"], "title": cfg["title"],
                              "desc": f"{cfg['note']} ・ Excel/CSV付き ・ "
                                      f"{ymd(snap.get('generated_at'))} 更新（前回分）",
                              "kind": "rank"})
    save_state(state)
    return cards


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--only", default="")
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args()
    only = [s.strip() for s in args.only.split(",") if s.strip()] or None
    if only:
        bad = [s for s in only if s not in SOURCES]
        if bad:
            print(f"unknown source: {bad}", file=sys.stderr)
            return 2
    if args.status:
        st = load_state()
        for k in SOURCES:
            print(k, st.get(k) or "(未生成)")
        return 0
    if not args.run:
        ap.print_help()
        return 1
    cards = build_due(force=args.force, only=only)
    for c in cards:
        print("card:", c["file"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
