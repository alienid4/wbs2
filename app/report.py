# -*- coding: utf-8 -*-
"""週五 WBS 週報產生器。

四段式結構，順序刻意如此：
  1. 本週完成 —— 事實
  2. 未完成與原因 —— 事實 + 歸因
  3. 浮時消耗預警 —— 這段才是真正的預警，也是一般週報最常漏掉的
  4. 下週關鍵路徑 —— 行動
文件關卡另立一節，因為缺文件卡住階段，跟任務沒做完是兩種不同的延遲。
"""
import datetime as dt
import json

from . import core, db, report_html, schedule as sch

MARK = {"overdue": "🛑", "will_slip": "🔴", "must_start": "🔴", "cyclic": "🌀",
        "critical": "🔴", "tight": "🟡", "ok": "⚪", "done": "✅"}


def _t(t):
    return f"{t['wbs_no']} {t['name']}"


def generate(week_end=None):
    data = core.week_view(week_end)
    ws, we = data["week_start"], data["week_end"]
    L = []
    add = L.append

    add(f"# WBS 進度週報　{ws} ～ {we}")
    add("")
    add(f"產出時間：{dt.datetime.now().strftime('%Y-%m-%d %H:%M')}")
    add("")

    # ---- 總覽 ----
    # 第一欄永遠是主管真正要問的那句話：跟原訂承諾比，現在快還是慢——不是浮時、
    # 不是完成率，那些是 PM 語言。沒凍結基準線之前這欄印「未凍結」，逼你去把它凍上。
    add("## 〇、總覽")
    add("")
    add("| 專案 | 較原訂承諾 | 預測完工 | 本週應完成 | 已完成 | 完成率 | 逾期 | 高風險 | 缺件 |")
    add("|---|---|---|---:|---:|---:|---:|---:|---:|")
    for p in data["projects"]:
        s = p["summary"]
        n = len(p["done"]) + len(p["undone"])
        rate = f"{len(p['done'])/n*100:.0f}%" if n else "—"
        if not s.get("baseline_set"):
            vs = "⚠️ 尚未凍結基準線"
        else:
            fv = s["finish_variance_days"]
            vs = (f"落後 {fv} 工作日" if fv > 0 else
                  f"提前 {abs(fv)} 工作日" if fv < 0 else "準時")
        add(f"| {p['project']['name']} | {vs} | {s.get('forecast_finish') or '—'} | "
            f"{n} | {len(p['done'])} | {rate} | "
            f"{len(p['overdue'])} | {len(p['at_risk'])} | {s['docs_missing']} |")
    add("")

    worst = []
    for p in data["projects"]:
        s = p["summary"]
        if s.get("baseline_set") and (s["finish_variance_days"] or 0) > 0:
            worst.append(f"{p['project']['name']} 落後承諾 {s['finish_variance_days']} 個工作日")
        elif p["overdue"]:
            worst.append(f"{p['project']['name']} 有 {len(p['overdue'])} 項已逾期")
        elif p["at_risk"]:
            worst.append(f"{p['project']['name']} 有 {len(p['at_risk'])} 項零浮時待辦")
    add("> **一句話結論：** " + ("；".join(worst) + "。需要決策或資源介入。"
                                if worst else "兩案本週均在承諾範圍內，無延遲風險。"))
    add("")

    for p in data["projects"]:
        pr = p["project"]
        add(f"## {pr['code']}　{pr['name']}")
        add("")
        if pr.get("baseline_set_at"):
            revs = core.baseline_revisions(pr["id"])
            if len(revs) > 1:
                add(f"> 本案已重新基準化 {len(revs)-1} 次，"
                    f"最初承諾完工日：{revs[0]['baseline_end']}")
                add("")

        ms = [t for t in core.project_state(pr["id"])["milestones"]]
        if ms:
            add("### 里程碑")
            add("")
            add("| 里程碑 | 原訂日 | 目前預測／實際 | 狀態 |")
            add("|---|---|---|---|")
            for t in ms:
                cur = t.get("actual_finish") or t.get("planned_end") or "—"
                add(f"| {t['name']} | {t.get('baseline_end') or '—'} | {cur} | "
                    f"{MARK.get(t['flag'],'')} {t['flag_reason']} |")
            add("")

        # 1 本週完成
        add("### 一、本週完成")
        add("")
        if p["done"]:
            for t in p["done"]:
                late = t.get("late_days")
                late_txt = (f"　⚠️ 較原訂遲 {late} 個工作日" if late and late > 0
                           else "　準時" if late == 0 else "")
                add(f"- ✅ **{_t(t)}**　（原訂 {t['planned_end']} ／ 實際 "
                    f"{t.get('actual_finish') or '—'}）{late_txt}"
                    + (f"　{t['note']}" if t.get("note") else ""))
        else:
            add("- 本週無項目結案。")
        add("")

        # 2 未完成與原因
        add("### 二、未完成項目與原因")
        add("")
        if p["undone"]:
            add("| 項次 | 工作項目 | 進度 | 原訂完成 | 狀態 | 原因／說明 |")
            add("|---|---|---:|---|---|---|")
            for t in p["undone"]:
                add(f"| {t['wbs_no']} | {t['name']} | {t.get('progress',0)}% | "
                    f"{t['planned_end']} | {MARK.get(t['flag'],'')} {t['flag_reason']} | "
                    f"{(t.get('note') or '—').replace(chr(10),' ')} |")
        else:
            add("- 無。")
        add("")

        # 3 浮時消耗預警
        add("### 三、浮時消耗與延遲預警")
        add("")
        add("> 浮時＝這項工作最多還能拖幾個工作日而不影響全案完工。浮時歸零＝下一次落後就是全案延遲。")
        add("")
        risk = p["at_risk"] + [t for t in p["overdue"] if t not in p["at_risk"]]
        if risk:
            add("| 項次 | 工作項目 | 總浮時 | 剩餘浮時 | 最後可動工日 | 原因 |")
            add("|---|---|---:|---:|---|---|")
            for t in sorted(risk, key=lambda x: x.get("total_float", 99)):
                lf = t.get("live_float")
                add(f"| {t['wbs_no']} | {t['name']} | {t['total_float']} | "
                    f"{lf if lf is not None else '—'} | {t.get('last_start','')} | "
                    f"{MARK.get(t['flag'],'')} {t['flag_reason']} |")
            add("")
            add("**影響推估：** 上表任一項再延一日，其下游任務同步後推，"
                "本案完工日將由原訂日期順延相同天數。")
        else:
            add("- 目前所有進行中項目浮時皆大於 2 個工作日，無立即延遲風險。")
        add("")

        # 4 下週關鍵路徑
        add("### 四、下週關鍵路徑")
        add("")
        if p["next_week"]:
            for t in p["next_week"][:12]:
                add(f"- {MARK.get(t['flag'],'')} **{_t(t)}**　"
                    f"{t['planned_start']} ～ {t['planned_end']}　"
                    f"（浮時 {t['total_float']}d）")
        else:
            add("- 下週無排定項目。")
        add("")

        # 文件關卡
        add("### 五、階段文件關卡")
        add("")
        gaps = [st for st in p["stages"] if st["gate"]["missing"]
                and st["status"] in ("進行中", "已完成")]
        if gaps:
            add("| 階段 | 狀態 | 必繳齊備 | 尚缺文件 |")
            add("|---|---|---:|---|")
            for st in gaps:
                g = st["gate"]
                add(f"| {st['code']} {st['name']} | {st['status']} | "
                    f"{g['required_ready']}/{g['required_total']} | "
                    + "、".join(f"{m['name']}（{m['doc_code']}）" for m in g["missing"]) + " |")
            add("")
            add("> 缺件會卡住階段出場，屬於行政型延遲——通常比技術問題更容易補救，"
                "但也更常被忽略到最後一刻。")
        else:
            add("- 進行中階段的必繳文件皆已齊備。")
        add("")

        if p["checkins"]:
            add("<details><summary>本週每日檢核紀錄</summary>")
            add("")
            for c in p["checkins"]:
                add(f"- {c['log_date']}　{c['wbs_no']} {c['name']}　"
                    f"{c.get('progress','')}%　{c.get('note') or ''}")
            add("")
            add("</details>")
            add("")

    add("---")
    add("")
    add("### 需要主管決策事項")
    add("")
    decisions = []
    for p in data["projects"]:
        for t in p["overdue"]:
            decisions.append(f"- **{p['project']['name']}／{_t(t)}**：已逾期 "
                             f"{t['overdue_days']} 個工作日，請裁示是否調整時程或追加資源。")
        for st in p["stages"]:
            if st["status"] == "進行中" and st["gate"]["missing"]:
                decisions.append(
                    f"- **{p['project']['name']}／{st['code']} {st['name']}**："
                    + "、".join(m["name"] for m in st["gate"]["missing"])
                    + " 尚未提供，階段無法結案。")
    if decisions:
        L.extend(decisions)
    else:
        add("- 無。")
    add("")
    return "\n".join(L)


def save(week_end=None):
    md = generate(week_end)
    data = core.week_view(week_end)
    we = data["week_end"]
    html = report_html.build(week_end)
    metrics = {p["project"]["code"]: {
        "forecast_finish": p["summary"].get("forecast_finish"),
        "baseline_finish": p["summary"].get("baseline_finish"),
        "finish_variance_days": p["summary"].get("finish_variance_days"),
    } for p in data["projects"]}
    now = dt.datetime.now().isoformat(timespec="seconds")
    with db.conn() as c:
        c.execute(
            "INSERT INTO report(week_end,content_md,content_html,metrics_json,created_at) "
            "VALUES (?,?,?,?,?) ON CONFLICT(week_end) DO UPDATE SET "
            "content_md=excluded.content_md, content_html=excluded.content_html, "
            "metrics_json=excluded.metrics_json, created_at=excluded.created_at",
            (we, md, html, json.dumps(metrics, ensure_ascii=False), now))
    return {"week_end": we, "content_md": md, "content_html": html}
