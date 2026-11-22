#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
xlsx_lite.py — 標準ライブラリだけで .xlsx を書き出す軽量ライター（外部pip不要）

GitHub Actions の素の Python（openpyxl なし）でも Excel ブックを生成できるようにする。
zipfile で OOXML を直接組み立てる。インライン文字列方式なので sharedStrings 不要。

使い方:
    from xlsx_lite import write_xlsx
    write_xlsx("out.xlsx", [
        {
            "name": "一覧",
            "columns": [{"header": "順位", "width": 6, "num": int},
                        {"header": "商品名", "width": 50},
                        {"header": "URL", "width": 40, "link": True}],
            "rows": [[1, "テスト商品", "https://example.com"]],
            "freeze": True,          # 1行目を固定（既定 True）
            "autofilter": True,      # オートフィルタ（既定 True）
        },
    ], title="タイトル")
"""
import re
import zipfile
from xml.sax.saxutils import escape

# セル種別 -> style index
ST_DEFAULT = 0
ST_HEADER = 1
ST_INT = 2       # #,##0
ST_FLOAT = 3     # #,##0.0
ST_LINK = 4      # 青字下線
ST_PCT = 5       # 0.0%
ST_WRAP = 6      # 折り返し

_STYLES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<numFmts count="4">
<numFmt numFmtId="164" formatCode="#,##0"/>
<numFmt numFmtId="165" formatCode="#,##0.0"/>
<numFmt numFmtId="166" formatCode="0.0%"/>
<numFmt numFmtId="167" formatCode="yyyy/m/d"/>
</numFmts>
<fonts count="3">
<font><sz val="10"/><name val="Yu Gothic"/></font>
<font><b/><sz val="10"/><color rgb="FFFFFFFF"/><name val="Yu Gothic"/></font>
<font><u/><sz val="10"/><color rgb="FF0563C1"/><name val="Yu Gothic"/></font>
</fonts>
<fills count="3">
<fill><patternFill patternType="none"/></fill>
<fill><patternFill patternType="gray125"/></fill>
<fill><patternFill patternType="solid"><fgColor rgb="FF145343"/><bgColor indexed="64"/></patternFill></fill>
</fills>
<borders count="2">
<border><left/><right/><top/><bottom/><diagonal/></border>
<border><left style="thin"><color rgb="FFD1D5DB"/></left><right style="thin"><color rgb="FFD1D5DB"/></right><top style="thin"><color rgb="FFD1D5DB"/></top><bottom style="thin"><color rgb="FFD1D5DB"/></bottom><diagonal/></border>
</borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="7">
<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
<xf numFmtId="0" fontId="1" fillId="2" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center" wrapText="1"/></xf>
<xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>
<xf numFmtId="165" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>
<xf numFmtId="0" fontId="2" fillId="0" borderId="0" xfId="0" applyFont="1"/>
<xf numFmtId="166" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>
<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf>
</cellXfs>
<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>"""

_CT = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
{sheets}
<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>
</Types>"""

_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
</Relationships>"""

_NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_NS_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def _clean(s):
    """XML で不正になる制御文字を落とす。"""
    if s is None:
        return ""
    s = str(s)
    s = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", s)
    return escape(s)


def col_letter(n):
    """1 -> A, 27 -> AA"""
    out = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        out = chr(65 + rem) + out
    return out


def _num(v):
    if isinstance(v, bool):
        return 1 if v else 0
    return v


def _cell(ref, value, style):
    if value is None or value == "":
        if style:
            return f'<c r="{ref}" s="{style}"/>'
        return ""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f'<c r="{ref}" s="{style}"><v>{value}</v></c>'
    text = _clean(value)
    return f'<c r="{ref}" s="{style}" t="inlineStr"><is><t xml:space="preserve">{text}</t></is></c>'


def _sheet_xml(sheet, links):
    columns = sheet.get("columns") or []
    rows = sheet.get("rows") or []
    freeze = sheet.get("freeze", True)
    autofilter = sheet.get("autofilter", True)
    ncols = max([len(columns)] + [len(r) for r in rows] + [1])

    parts = [f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
             f'<worksheet xmlns="{_NS_MAIN}" xmlns:r="{_NS_REL}">']
    if freeze:
        parts.append('<sheetViews><sheetView workbookViewId="0" tabSelected="0">'
                     '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
                     '</sheetView></sheetViews>')
    if columns:
        cols = []
        for i, c in enumerate(columns, 1):
            w = c.get("width") or 14
            cols.append(f'<col min="{i}" max="{i}" width="{w}" customWidth="1"/>')
        parts.append("<cols>" + "".join(cols) + "</cols>")

    parts.append("<sheetData>")
    # ヘッダー
    if columns:
        cells = []
        for i, c in enumerate(columns, 1):
            cells.append(_cell(f"{col_letter(i)}1", c.get("header", ""), ST_HEADER))
        parts.append('<row r="1" ht="24" customHeight="1">' + "".join(cells) + "</row>")

    start = 2 if columns else 1
    for ri, row in enumerate(rows, start):
        cells = []
        for ci, value in enumerate(row, 1):
            col = columns[ci - 1] if ci - 1 < len(columns) else {}
            style = ST_DEFAULT
            v = _num(value)
            if col.get("link") and value:
                style = ST_LINK
                links.append((f"{col_letter(ci)}{ri}", str(value)))
            elif col.get("pct") and isinstance(v, (int, float)) and not isinstance(v, bool):
                style = ST_PCT
            elif col.get("num") is int and isinstance(v, int) and not isinstance(v, bool):
                style = ST_INT
            elif col.get("num") is float and isinstance(v, (int, float)) and not isinstance(v, bool):
                style = ST_FLOAT
            elif col.get("wrap"):
                style = ST_WRAP
            cells.append(_cell(f"{col_letter(ci)}{ri}", v, style))
        parts.append(f'<row r="{ri}">' + "".join(cells) + "</row>")
    parts.append("</sheetData>")

    if autofilter and columns and rows:
        parts.append(f'<autoFilter ref="A1:{col_letter(ncols)}{len(rows) + 1}"/>')
    parts.append(f'<pageMargins left="0.5" right="0.5" top="0.6" bottom="0.6" header="0.3" footer="0.3"/>')
    parts.append("</worksheet>")
    return "".join(parts)


def write_xlsx(path, sheets, title=None):
    """sheets: [{"name","columns","rows","freeze","autofilter"}] を .xlsx に書き出す。"""
    used = set()
    names = []
    for i, sh in enumerate(sheets, 1):
        raw = re.sub(r"[\[\]\*\?/\\:]", "", str(sh.get("name") or f"Sheet{i}"))[:31] or f"Sheet{i}"
        name = raw
        k = 2
        while name in used:
            name = raw[:28] + f"_{k}"
            k += 1
        used.add(name)
        names.append((name, i))

    ct_sheets = "\n".join(
        f'<Override PartName="/xl/worksheets/sheet{i}.xml" '
        f'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        for _, i in names)

    wb_sheets = "".join(f'<sheet name="{_clean(n)}" sheetId="{i}" r:id="rId{i}"/>' for n, i in names)
    workbook = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                f'<workbook xmlns="{_NS_MAIN}" xmlns:r="{_NS_REL}">'
                f'<sheets>{wb_sheets}</sheets></workbook>')

    rels = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">']
    for n, i in names:
        rels.append(f'<Relationship Id="rId{i}" Type="{_NS_REL}/worksheet" Target="worksheets/sheet{i}.xml"/>')
    rels.append(f'<Relationship Id="rId{len(names) + 1}" Type="{_NS_REL}/styles" Target="styles.xml"/>')
    rels.append("</Relationships>")

    app_xml = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
               '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" '
               'xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">'
               '<Application>xlsx_lite</Application></Properties>')
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    core_xml = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
                'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
                'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
                f'<dc:title>{_clean(title or "")}</dc:title>'
                f'<dcterms:created xsi:type="dcterms:W3CDTF">{now}</dcterms:created>'
                f'<dcterms:modified xsi:type="dcterms:W3CDTF">{now}</dcterms:modified>'
                '</cp:coreProperties>')

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _CT.format(sheets=ct_sheets))
        z.writestr("_rels/.rels", _RELS)
        z.writestr("docProps/core.xml", core_xml)
        z.writestr("docProps/app.xml", app_xml)
        z.writestr("xl/workbook.xml", workbook)
        z.writestr("xl/_rels/workbook.xml.rels", "".join(rels))
        z.writestr("xl/styles.xml", _STYLES)
        for n, i in names:
            links = []
            xml = _sheet_xml(sheets[i - 1], links)
            if links:
                # ハイパーリンクは sheet XML 内に r:id 参照を足し、sheet 単位 rels を作る
                rel_xml = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
                           '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">']
                hl = []
                for k, (ref, url) in enumerate(links, 1):
                    rel_xml.append(f'<Relationship Id="rId{k}" '
                                   f'Type="{_NS_REL}/hyperlink" Target="{_clean(url)}" TargetMode="External"/>')
                    hl.append(f'<hyperlink ref="{ref}" r:id="rId{k}"/>')
                rel_xml.append("</Relationships>")
                xml = xml.replace("</worksheet>", "<hyperlinks>" + "".join(hl) + "</hyperlinks></worksheet>")
                z.writestr(f"xl/worksheets/_rels/sheet{i}.xml.rels", "".join(rel_xml))
            z.writestr(f"xl/worksheets/sheet{i}.xml", xml)


def write_csv(path, header, rows):
    """Excel でそのまま開ける UTF-8 BOM 付き CSV。"""
    import csv
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, quoting=csv.QUOTE_MINIMAL)
        w.writerow(header)
        for r in rows:
            w.writerow(["" if v is None else v for v in r])


if __name__ == "__main__":
    write_xlsx("/tmp/_xlsx_test.xlsx", [
        {"name": "テスト", "columns": [
            {"header": "順位", "width": 6, "num": int},
            {"header": "商品名", "width": 40},
            {"header": "価格", "width": 12, "num": int},
            {"header": "評価", "width": 8, "num": float},
            {"header": "URL", "width": 30, "link": True},
        ], "rows": [[1, "テスト商品 & <tag>", 1980, 4.5, "https://example.com/?a=1&b=2"],
                    [2, None, 2980, 4.0, "https://example.com/2"]]},
    ], title="テスト")
    print("ok")
