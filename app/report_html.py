# -*- coding: utf-8 -*-
"""單檔自足 HTML 週報——給主管的那一份。

設計原則跟 report.py（Markdown 版）不同：Markdown 是給你自己複製貼上用的操作紀錄，
這份是**利害關係人視角**：不需要登入、不需要開這個 App，雙擊就開、可以直接列印成 PDF、
CSS 全部內嵌不依賴任何外部資源。

第一句話永遠是主管真正想問的那句：「現在預測幾號完工，比原訂晚幾天」。
"""
import datetime as dt
import html as _html

from . import core

MARK = {"overdue": "🛑", "will_slip": "🔴", "must_start": "🔴", "cyclic": "🌀",
        "critical": "🔴", "tight": "🟡", "ok": "⚪", "done": "✅"}


def _e(x):
    return _html.escape(str(x if x is not None else ""))


def _variance_line(s):
    """把 finish_variance_days 轉成一句人話。"""
    fv = s["finish_variance_days"]
    if not s.get("baseline_set"):
        return ("尚未凍結基準線 — 目前預測完工："
                f"<b>{_e(s['forecast_finish']) or '—'}</b>（此數字僅供參考，"
                "還沒有承諾日可比較）")
    if fv is None:
        return "—"
    base = f"原訂 <b>{_e(s['baseline_finish'])}</b>　現在預測 <b>{_e(s['forecast_finish'])}</b>　"
    if fv > 0:
        return base + f'<span class="tag bad">落後 {fv} 個工作日</span>'
    if fv < 0:
        return base + f'<span class="tag good">提前 {abs(fv)} 個工作日</span>'
    return base + '<span class="tag ok">準時</span>'


def _bar(s):
    """baseline vs forecast 對照條：灰段＝承諾工期，色段＝目前預測，超出部分標紅。"""
    if not s.get("baseline_set") or not s.get("forecast_finish"):
        return ""
    fv = s["finish_variance_days"] or 0
    base_w = 70
    over_w = min(max(fv, 0) * 3, 120)
    ahead_w = min(max(-fv, 0) * 3, 40)
    inner = f'<div class="seg base" style="width:{base_w}%"></div>'
    if fv > 0:
        inner += f'<div class="seg over" style="width:{min(over_w,30)}%"></div>'
    elif fv < 0:
        inner += f'<div class="seg ahead" style="width:{min(ahead_w,15)}%"></div>'
    return f'<div class="track">{inner}</div>'


def build(week_end=None):
    data = core.week_view(week_end)
    ws, we = data["week_start"], data["week_end"]

    parts = []
    add = parts.append

    add(f"""<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>WBS 進度週報 {_e(we)}</title>
<style>
{_CSS}
</style></head><body>
<div class="page">
<header>
  <div class="ttl">WBS 進度週報</div>
  <div class="range">{_e(ws)} ～ {_e(we)}</div>
</header>
""")

    worst = []
    for p in data["projects"]:
        s = p["summary"]
        if s.get("baseline_set") and (s["finish_variance_days"] or 0) > 0:
            worst.append(f"{_e(p['project']['name'])} 落後 {s['finish_variance_days']} 個工作日")
        elif p["overdue"]:
            worst.append(f"{_e(p['project']['name'])} 有 {len(p['overdue'])} 項已逾期")
    add(f'<div class="banner {"bad" if worst else "good"}">'
        f'{"；".join(worst) + "。需要決策或資源介入。" if worst else "兩案本週均在承諾範圍內，無延遲風險。"}'
        f'</div>')

    for p in data["projects"]:
        pr, s = p["project"], p["summary"]
        add(f'<section class="proj">')
        add(f'<h2><span class="dot" style="background:{_e(pr.get("color") or "#2563eb")}">'
            f'</span>{_e(pr["code"])}　{_e(pr["name"])}</h2>')
        add(f'<div class="headline">{_variance_line(s)}</div>')
        add(_bar(s))

        add('<div class="kpis">')
        for cls, val, lbl in [
            ("", s["total"], "工作項目"), ("g", s["done"], "已完成"),
            ("r", s["overdue"], "逾期"), ("r", s["at_risk"], "高風險"),
            ("a", s["min_float"] if s["min_float"] is not None else "—", "最小浮時(d)"),
            ("r", s["docs_missing"], "文件缺件"),
        ]:
            add(f'<div class="kpi {cls}"><b>{_e(val)}</b><span>{lbl}</span></div>')
        add('</div>')

        if pr.get("baseline_set_at"):
            revs = core.baseline_revisions(pr["id"])
            if len(revs) > 1:
                add(f'<p class="sub">本案已重新基準化 {len(revs)-1} 次，'
                    f'最初承諾完工日：{_e(revs[0]["baseline_end"])}</p>')

        add(_milestone_table(pr["id"]))

        add('<h3>本週完成</h3>')
        if p["done"]:
            add('<table><tr><th>項次</th><th>工作項目</th><th>原訂完成</th>'
                '<th>實際完成</th><th>較原訂</th></tr>')
            for t in p["done"]:
                late = t.get("late_days")
                if late is None:
                    late_html = '<span class="tag">—</span>'
                elif late > 0:
                    late_html = f'<span class="tag bad">遲 {late} 天</span>'
                elif late < 0:
                    late_html = f'<span class="tag good">早 {abs(late)} 天</span>'
                else:
                    late_html = '<span class="tag ok">準時</span>'
                add(f'<tr><td>{_e(t["wbs_no"])}</td><td>{_e(t["name"])}</td>'
                    f'<td>{_e(t["planned_end"])}</td>'
                    f'<td>{_e(t.get("actual_finish") or "—")}</td><td>{late_html}</td></tr>')
            add('</table>')
        else:
            add('<p class="sub">本週無項目結案。</p>')

        add('<h3>未完成項目與原因</h3>')
        if p["undone"]:
            add('<table><tr><th>項次</th><th>工作項目</th><th>進度</th>'
                '<th>原訂完成</th><th>原因</th><th>備註</th></tr>')
            for t in p["undone"]:
                add(f'<tr><td>{_e(t["wbs_no"])}</td><td>{_e(t["name"])}</td>'
                    f'<td class="num">{_e(t.get("progress",0))}%</td>'
                    f'<td>{_e(t["planned_end"])}</td>'
                    f'<td>{MARK.get(t["flag"],"")} {_e(t["flag_reason"])}</td>'
                    f'<td>{_e((t.get("note") or "—"))}</td></tr>')
            add('</table>')
        else:
            add('<p class="sub">無。</p>')

        risk = p["at_risk"] + [t for t in p["overdue"] if t not in p["at_risk"]]
        add('<h3>浮時消耗與延遲預警</h3>')
        if risk:
            add('<table><tr><th>項次</th><th>工作項目</th><th>總浮時</th>'
                '<th>剩餘浮時</th><th>最後可動工日</th><th>原因</th></tr>')
            for t in sorted(risk, key=lambda x: x.get("total_float", 99)):
                lf = t.get("live_float")
                add(f'<tr><td>{_e(t["wbs_no"])}</td><td>{_e(t["name"])}</td>'
                    f'<td class="num">{_e(t["total_float"])}</td>'
                    f'<td class="num">{_e(lf if lf is not None else "—")}</td>'
                    f'<td>{_e(t.get("last_start",""))}</td>'
                    f'<td>{MARK.get(t["flag"],"")} {_e(t["flag_reason"])}</td></tr>')
            add('</table>')
        else:
            add('<p class="sub">目前所有進行中項目浮時皆大於 2 個工作日，無立即延遲風險。</p>')

        gaps = [st for st in p["stages"] if st["gate"]["missing"]
               and st["status"] in ("進行中", "已完成")]
        if gaps:
            add('<h3>階段文件關卡</h3><table><tr><th>階段</th><th>狀態</th>'
                '<th>必繳齊備</th><th>尚缺文件</th></tr>')
            for st in gaps:
                g = st["gate"]
                add(f'<tr><td>{_e(st["code"])} {_e(st["name"])}</td><td>{_e(st["status"])}</td>'
                    f'<td class="num">{g["required_ready"]}/{g["required_total"]}</td>'
                    f'<td>{"、".join(_e(m["name"])+"（"+_e(m["doc_code"])+"）" for m in g["missing"])}'
                    '</td></tr>')
            add('</table>')

        add('</section>')

    decisions = []
    for p in data["projects"]:
        for t in p["overdue"]:
            decisions.append(f"{_e(p['project']['name'])}／{_e(t['wbs_no'])} {_e(t['name'])}："
                             f"已逾期 {t['overdue_days']} 個工作日，請裁示是否調整時程或追加資源。")
        for st in p["stages"]:
            if st["status"] == "進行中" and st["gate"]["missing"]:
                decisions.append(f"{_e(p['project']['name'])}／{_e(st['code'])} {_e(st['name'])}："
                                 + "、".join(_e(m["name"]) for m in st["gate"]["missing"])
                                 + " 尚未提供，階段無法結案。")
    add('<section class="proj"><h2>需要主管決策事項</h2>')
    if decisions:
        add('<ul>' + "".join(f"<li>{d}</li>" for d in decisions) + '</ul>')
    else:
        add('<p class="sub">無。</p>')
    add('</section>')

    add(f'<footer>產出時間：{_e(dt.datetime.now().strftime("%Y-%m-%d %H:%M"))}'
        '　·　本檔為單一自足 HTML，離線可開、可直接列印成 PDF</footer>')
    add('</div></body></html>')
    return "".join(parts)


def _milestone_table(project_id):
    st = core.project_state(project_id)
    ms = st.get("milestones") if st else []
    if not ms:
        return ""
    rows = []
    for t in ms:
        base = t.get("baseline_end") or ""
        cur = t.get("actual_finish") or t.get("planned_end") or ""
        rows.append((t, base, cur))
    out = ['<h3>里程碑</h3><table><tr><th>里程碑</th><th>原訂日</th>'
           '<th>目前預測／實際</th><th>狀態</th></tr>']
    for t, base, cur in rows:
        out.append(f'<tr><td>{_e(t["name"])}</td><td>{_e(base or "—")}</td>'
                   f'<td>{_e(cur or "—")}</td>'
                   f'<td>{MARK.get(t["flag"],"")} {_e(t["flag_reason"])}</td></tr>')
    out.append('</table>')
    return "".join(out)


_CSS = """
:root{--ink:#1a1d21;--sub:#6b7280;--line:#e3e6ea;--bg:#f6f7f9;--panel:#fff;
  --accent:#1f3864;--accent2:#2563eb;--red:#dc2626;--redbg:#fef2f2;
  --green:#059669;--greenbg:#ecfdf5;--amber:#d97706;--amberbg:#fffbeb}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font:14px/1.6 -apple-system,"Segoe UI","Microsoft JhengHei","PingFang TC",sans-serif}
.page{max-width:880px;margin:0 auto;padding:28px 24px 60px}
header{border-bottom:3px solid var(--accent);padding-bottom:14px;margin-bottom:16px}
.ttl{font-size:24px;font-weight:800;color:var(--accent)}
.range{color:var(--sub);font-size:13px;margin-top:2px}
.banner{padding:12px 16px;border-radius:8px;font-weight:600;margin-bottom:20px}
.banner.bad{background:var(--redbg);color:var(--red)}
.banner.good{background:var(--greenbg);color:var(--green)}
.proj{background:var(--panel);border:1px solid var(--line);border-radius:10px;
  padding:18px 22px;margin-bottom:18px}
h2{font-size:17px;margin:0 0 10px;display:flex;align-items:center;gap:8px}
.dot{width:10px;height:10px;border-radius:50%;display:inline-block}
h3{font-size:14px;margin:18px 0 8px;color:var(--accent2);border-bottom:1px solid var(--line);
  padding-bottom:4px}
.headline{font-size:15px;margin-bottom:8px}
.sub{color:var(--sub);font-size:12.5px}
.tag{font-size:12px;padding:2px 9px;border-radius:6px;font-weight:600}
.tag.bad{background:var(--redbg);color:var(--red)}
.tag.good{background:var(--greenbg);color:var(--green)}
.tag.ok{background:rgba(127,127,127,.12);color:var(--sub)}
.track{height:14px;border-radius:99px;background:rgba(127,127,127,.14);overflow:hidden;
  display:flex;margin:10px 0}
.track .seg.base{background:#9ca3af}
.track .seg.over{background:var(--red)}
.track .seg.ahead{background:var(--green)}
.kpis{display:flex;gap:8px;flex-wrap:wrap;margin:10px 0}
.kpi{border:1px solid var(--line);border-radius:8px;padding:7px 13px;min-width:76px}
.kpi b{display:block;font-size:18px}
.kpi span{font-size:10.5px;color:var(--sub)}
.kpi.r b{color:var(--red)}.kpi.g b{color:var(--green)}.kpi.a b{color:var(--amber)}
table{border-collapse:collapse;width:100%;font-size:12.5px;margin:6px 0 4px}
th{background:rgba(127,127,127,.12);text-align:left;padding:6px 8px;font-weight:600}
td{padding:6px 8px;border-bottom:1px solid var(--line)}
td.num{text-align:right;font-variant-numeric:tabular-nums}
ul{margin:6px 0;padding-left:20px}
footer{color:var(--sub);font-size:11.5px;text-align:center;margin-top:24px}
@media print{body{background:#fff}.proj{break-inside:avoid;box-shadow:none}}
"""
