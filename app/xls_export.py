# -*- coding: utf-8 -*-
"""Excel 對照表匯出——不裝任何第三方套件（沒有 openpyxl），用 Excel 原生支援的
SpreadsheetML 2003 XML 格式手刻。副檔名 .xls，雙擊會用 Excel 開，開了之後
排序／篩選／凍結窗格都是 Excel 本身的功能，不需要我們自己實作。
"""
import html as _html

from . import core

COLS = [
    ("專案", 16), ("項次", 10), ("工作項目", 34), ("原訂完成", 12),
    ("目前計畫完成", 12), ("實際完成", 12), ("較原訂(工作日)", 14),
    ("總浮時", 8), ("剩餘浮時", 8), ("狀態", 10), ("原因", 30), ("備註", 30),
]

STYLE_HEAD = 'ss:StyleID="head"'
STYLE_BAD = 'ss:StyleID="bad"'
STYLE_GOOD = 'ss:StyleID="good"'


def _e(x):
    return _html.escape(str(x if x is not None else ""))


def _cell(val, style=None, kind="String"):
    s = f' {style}' if style else ""
    if kind == "Number" and val not in (None, ""):
        return f'<Cell{s}><Data ss:Type="Number">{val}</Data></Cell>'
    return f'<Cell{s}><Data ss:Type="String">{_e(val)}</Data></Cell>'


def build():
    rows_xml = []
    for st in core.all_states():
        if not st:
            continue
        cal = core.calendar()
        p = st["project"]
        for t in st["tasks"]:
            late = core.late_workdays(cal, t)
            style = STYLE_BAD if (late or 0) > 0 else (STYLE_GOOD if (late or 0) < 0 else None)
            cells = [
                _cell(p["name"]), _cell(t["wbs_no"]), _cell(t["name"]),
                _cell(t.get("baseline_end") or ""), _cell(t.get("planned_end") or ""),
                _cell(t.get("actual_finish") or ""),
                _cell(late if late is not None else "", style, "Number"),
                _cell(t.get("total_float"), None, "Number"),
                _cell(t.get("live_float"), None, "Number"),
                _cell(t.get("status")), _cell(t.get("flag_reason")),
                _cell((t.get("note") or "").replace("\n", " ")),
            ]
            rows_xml.append(f'<Row>{"".join(cells)}</Row>')

    header = "".join(f'<Cell {STYLE_HEAD}><Data ss:Type="String">{_e(name)}</Data></Cell>'
                     for name, _w in COLS)
    cols_def = "".join(f'<Column ss:Width="{w*6}"/>' for _n, w in COLS)

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<?mso-application progid="Excel.Sheet"?>
<Workbook xmlns="urn:schemas-microsoft-com:office:spreadsheet"
 xmlns:o="urn:schemas-microsoft-com:office:office"
 xmlns:x="urn:schemas-microsoft-com:office:excel"
 xmlns:ss="urn:schemas-microsoft-com:office:spreadsheet">
<Styles>
 <Style ss:ID="head"><Font ss:Bold="1" ss:Color="#FFFFFF"/>
   <Interior ss:Color="#1F3864" ss:Pattern="Solid"/></Style>
 <Style ss:ID="bad"><Font ss:Color="#DC2626" ss:Bold="1"/></Style>
 <Style ss:ID="good"><Font ss:Color="#059669" ss:Bold="1"/></Style>
</Styles>
<Worksheet ss:Name="WBS對照表">
<Table>{cols_def}
<Row>{header}</Row>
{"".join(rows_xml)}
</Table>
<WorksheetOptions xmlns="urn:schemas-microsoft-com:office:excel">
 <FreezePanes/><FrozenNoSplit/><SplitHorizontal>1</SplitHorizontal>
 <TopRowBottomPane>1</TopRowBottomPane><ActivePane>2</ActivePane>
</WorksheetOptions>
</Worksheet>
</Workbook>
"""
