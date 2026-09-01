# -*- coding: utf-8 -*-
"""服務層：把排程引擎與文件關卡兜起來，供 API 與週報共用。"""
import datetime as dt
import re

from . import db, docs_scan, schedule as sch


def _natkey(s):
    """A10 要排在 A2 後面，不是 A1 後面。"""
    return [int(p) if p.isdigit() else p.lower()
            for p in re.split(r"(\d+)", str(s or ""))]


def calendar():
    cfg = db.load_config()
    return sch.Calendar(cfg.get("workweek"), cfg.get("holidays"))


def roll_up_stage_dates(project_id):
    """階段圈圈底下顯示的日期＝底下工作項目日期的最早～最晚，每次任務日期／階段歸屬
    異動後都要重算一次，不然畫面停在舊數字，跟實際任務兜不起來（2026-08-28 使用者
    抓到：拖拉調完階段順序、想順手改日期，才發現這個彙總其實沒有跟著任務自動更新）。

    只動 planned_start/planned_end，刻意不動 status——階段狀態使用者可以手動下拉選
    （未開始/進行中/已完成），自動重算會蓋掉使用者自己選的狀態，兩件事分開處理。"""
    for st in db.rows("SELECT id, code FROM stage WHERE project_id=?", (project_id,)):
        ts = db.rows(
            "SELECT planned_start, planned_end FROM task WHERE project_id=? AND "
            "stage_code=? AND planned_start<>'' AND planned_end<>''",
            (project_id, st["code"]))
        if not ts:
            continue
        s0 = min(t["planned_start"] for t in ts)
        s1 = max(t["planned_end"] for t in ts)
        db.run("UPDATE stage SET planned_start=?, planned_end=? WHERE id=?", (s0, s1, st["id"]))


def projects(include_archived=False):
    q = "SELECT * FROM project"
    if not include_archived:
        q += " WHERE archived=0"
    rows = db.rows(q + " ORDER BY sort_order, code")
    co = {}
    for r in db.rows("SELECT project_id, person_name FROM project_owner ORDER BY person_name"):
        co.setdefault(r["project_id"], []).append(r["person_name"])
    for p in rows:
        p["co_owners"] = co.get(p["id"], [])
    return rows


_DELAY_FLAGS = ("overdue", "will_slip", "must_start", "critical", "tight", "cyclic")


def delay_chain_text(task, by_wbs, max_hops=5):
    """里程碑／任務落後時，沿著「前置」欄位往回找是哪個上游項目造成的，組一句
    給人看的因果鏈說明（不是畫依賴圖）——純規則判斷，不叫用任何 AI。

    每一步在前置項目裡挑「最像瓶頸」的那個（已逾期優先，其次是浮時最少的），
    一路往回追到找不到還在落後的前置項目為止；那一項就是根本原因。"""
    if task.get("done") or task.get("flag") not in _DELAY_FLAGS:
        return ""
    chain = [task]
    seen = {task["wbs_no"]}
    cur = task
    for _ in range(max_hops):
        best = None
        for p_no in (cur.get("preds") or []):
            p = by_wbs.get(p_no)
            if not p or p["wbs_no"] in seen or p.get("done"):
                continue
            if p.get("flag") not in _DELAY_FLAGS:
                continue
            key = (p.get("overdue_days", 0), -(p.get("total_float") if p.get("total_float") is not None else 999))
            if best is None or key > best[0]:
                best = (key, p)
        if not best:
            break
        cur = best[1]
        chain.append(cur)
        seen.add(cur["wbs_no"])
    root = chain[-1]
    if len(chain) == 1:
        return f"「{task['name']}」{task.get('flag_reason', '')}，不是被上游工作項目拖累。"
    path = "→".join(f"「{t['name']}」" for t in reversed(chain))
    return f"延誤源頭：{path}。根本原因：「{root['name']}」{root.get('flag_reason', '')}。"


def workload_summary():
    """跨所有專案、依負責人彙整未完成工作項目——「能力負荷」（誰手上有什麼）跟
    「時間負荷」（哪天/哪週最壅塞）共用這份底層資料，前端各自算自己要的呈現方式，
    不是兩套各自維護的資料。owner 空白的項目歸在 "" 這個桶，前端顯示為「未指派」。"""
    projs = {p["id"]: p for p in projects()}
    by_owner = {}
    for pid, p in projs.items():
        stage_names = {s["code"]: s["name"] for s in db.rows(
            "SELECT code, name FROM stage WHERE project_id=?", (pid,))}
        for t in db.rows(
                "SELECT wbs_no, name, owner, planned_start, planned_end, status, progress, "
                "level, stage_code FROM task WHERE project_id=?", (pid,)):
            if t.get("level") == "M":
                continue
            done = (t.get("status") == "已完成") or (int(t.get("progress") or 0) >= 100)
            if done or not t.get("planned_start") or not t.get("planned_end"):
                continue
            owner = t.get("owner") or ""
            stage_code = t.get("stage_code") or ""
            by_owner.setdefault(owner, []).append({
                "project_code": p["code"], "project_name": p["name"],
                "project_color": p.get("color") or "#2563eb",
                "wbs_no": t["wbs_no"], "name": t["name"],
                "planned_start": t["planned_start"], "planned_end": t["planned_end"],
                "stage_code": stage_code,
                "stage_name": stage_names.get(stage_code, "未分類"),
            })

    def overlaps(a, b):
        return a["planned_start"] <= b["planned_end"] and b["planned_start"] <= a["planned_end"]

    people = []
    for owner, tasks in by_owner.items():
        tasks.sort(key=lambda t: t["planned_start"])
        for i, t in enumerate(tasks):
            t["overlap"] = any(j != i and overlaps(t, tasks[j]) for j in range(len(tasks)))
        people.append({"name": owner, "tasks": tasks})
    people.sort(key=lambda p: (p["name"] == "", p["name"]))
    return {"people": people}


def project_state(project_id, today=None):
    """單一專案：任務 + CPM 指標 + 階段文件關卡。"""
    cal = calendar()
    today = today or dt.date.today()
    proj = db.one("SELECT * FROM project WHERE id=?", (project_id,))
    if not proj:
        return None
    proj["co_owners"] = [r["person_name"] for r in db.rows(
        "SELECT person_name FROM project_owner WHERE project_id=? ORDER BY person_name", (project_id,))]
    tasks = db.rows("SELECT * FROM task WHERE project_id=?", (project_id,))
    tasks.sort(key=lambda t: (t.get("planned_start") or "9999", _natkey(t["wbs_no"])))
    # 承諾日的天花板：凍結後用 baseline_end（唯讀、只能靠重新基準化改），
    # 還沒凍結就退回可隨手編輯的 end_date——讓還沒 onboarding 完的專案不會被空承諾日卡死。
    ceiling = proj.get("baseline_end") or proj.get("end_date")
    metrics, fmeta = sch.compute(tasks, cal, ceiling, today)
    merged = []
    for t in tasks:
        m = metrics.get(t["wbs_no"], {})
        merged.append({**t, **{k: v for k, v in m.items() if k != "wbs_no"}})

    stages = db.rows("SELECT * FROM stage WHERE project_id=? ORDER BY seq", (project_id,))
    for st in stages:
        st["gate"] = docs_scan.gate_status(project_id, st["code"])
        st["tasks"] = [t["wbs_no"] for t in merged if t.get("stage_code") == st["code"]]

    open_tasks = [t for t in merged if not t["done"]]

    # 現在在哪一階段：不新增欄位，直接用 stage.status（既有、使用者本來就會維護）——
    # seq 最小、狀態不是「已完成」的那個就是現在。全部完成就沒有 current，前端顯示全案結案。
    current_stage = next((s for s in stages if s["status"] != "已完成"), None)

    milestones = sorted(
        [t for t in merged if t.get("level") == "M"],
        key=lambda t: t.get("baseline_end") or t.get("planned_end") or "9999")
    # 里程碑卡片的內容一律從既有資料算，不生新資料：掛了 stage_code 才有 segment，
    # 沒掛就是 None——前端要老實顯示「這個里程碑沒連到任何階段」，不能編內容湊版面。
    stage_by_code = {s["code"]: s for s in stages}
    by_wbs = {t["wbs_no"]: t for t in merged}
    for ms in milestones:
        sc = ms.get("stage_code")
        st = stage_by_code.get(sc)
        if not st:
            ms["segment"] = None
        else:
            seg_tasks = [t for t in merged if t.get("stage_code") == sc and t.get("level") != "M"]
            ms["segment"] = {
                "stage_code": sc, "stage_name": st["name"],
                "done": [t["name"] for t in seg_tasks if t["done"]],
                "open": [t["name"] for t in seg_tasks if not t["done"]],
                "missing_docs": st["gate"]["missing"],
            }
        ms["delay_chain"] = delay_chain_text(ms, by_wbs)

    summary = {
        "total": len(merged),
        "done": sum(1 for t in merged if t["done"]),
        "overdue": sum(1 for t in merged if t["flag"] == "overdue"),
        "at_risk": sum(1 for t in merged
                      if t["flag"] in ("will_slip", "must_start", "cyclic")),
        "critical_open": sum(1 for t in open_tasks if t["critical"]),
        "min_float": min([t["total_float"] for t in open_tasks], default=None),
        "gates_failed": sum(1 for st in stages
                            if st["status"] == "已完成" and not st["gate"]["passed"]),
        "docs_missing": sum(len(st["gate"]["missing"]) for st in stages),
        "forecast_finish": fmeta["forecast_finish"],
        "baseline_finish": fmeta["baseline_finish"],
        "finish_variance_days": fmeta["finish_variance_days"],
        "baseline_set": bool(proj.get("baseline_end")),
    }

    return {
        "project": proj,
        "tasks": merged,
        "stages": stages,
        "current_stage": current_stage,
        "milestones": milestones,
        "summary": summary,
        "status_sentence": _status_sentence(proj, stages, current_stage, summary),
    }


def _status_sentence(proj, stages, current_stage, summary):
    """回報用的白話狀態句——只有這裡算一次，前端跟 HTML 週報都讀這個欄位，
    不要各自在 JS／report_html.py 裡重寫一次同樣的邏輯，兩邊會慢慢講法不一樣。"""
    if current_stage:
        idx = next(i for i, s in enumerate(stages) if s["code"] == current_stage["code"]) + 1
        gate = current_stage["gate"]
        stage_txt = f"目前處於 {current_stage['code']} {current_stage['name']} 階段（{idx}/{len(stages)} 站）。"
        if gate["missing"]:
            stage_txt += f"本階段必繳文件還缺 {len(gate['missing'])} 份，未齊備前無法往下一階段推進。"
        else:
            stage_txt += "本階段必繳文件已齊備。"
    elif stages:
        stage_txt = "全案 9 個階段皆已完成。"
    else:
        stage_txt = "尚未套用階段範本。"

    if summary["baseline_set"]:
        fv = summary["finish_variance_days"]
        if fv > 0:
            base_txt = (f"較原訂承諾（{summary['baseline_finish']}）晚 {fv} 個工作日，"
                       f"現在預測 {summary['forecast_finish']} 完工。")
        elif fv < 0:
            base_txt = f"較原訂承諾提前 {abs(fv)} 個工作日。"
        else:
            base_txt = "準時，符合原訂承諾完工日。"
    else:
        base_txt = f"尚未凍結基準線，目前預測完工 {summary.get('forecast_finish') or '—'}（僅供參考，還沒有承諾日可比較）。"
    return stage_txt + base_txt


def all_states(today=None):
    return [project_state(p["id"], today) for p in projects()]


def _in_window(t, a, b):
    ps, pe = sch.d(t.get("planned_start")), sch.d(t.get("planned_end"))
    if not ps or not pe:
        return False
    return ps <= b and pe >= a


def late_workdays(cal, t):
    """實際完成日比原訂完成日晚幾個工作日。原訂日缺（新增的臨時任務）就不算。"""
    pe, af = sch.d(t.get("planned_end")), sch.d(t.get("actual_finish"))
    if not pe or not af:
        return None
    if af <= pe:
        return 0
    return cal.workdays_between(cal.next_workday(pe), af)


def today_view(today=None):
    """今天要做什麼、什麼不能拖。"""
    today = today or dt.date.today()
    cal = calendar()
    cfg = db.load_config()
    out = {"date": sch.s(today), "is_workday": cal.is_workday(today),
           "must": [], "should": [], "may": [], "overdue": [], "blocked_gates": []}
    for st in all_states(today):
        if not st:
            continue
        p = st["project"]
        block = (cfg.get("blocks") or {}).get(p["code"], "")
        for t in st["tasks"]:
            if t["done"]:
                continue
            row = {"project": p["name"], "project_code": p["code"],
                   "color": p["color"], "block": block, **t}
            if t["flag"] == "overdue":
                out["overdue"].append(row)
            elif _in_window(t, today, today):
                if t["flag"] in ("will_slip", "must_start", "critical", "cyclic"):
                    out["must"].append(row)
                elif t["flag"] == "tight":
                    out["should"].append(row)
                else:
                    out["may"].append(row)
            elif t["flag"] in ("must_start", "cyclic"):
                out["must"].append(row)
        for stg in st["stages"]:
            g = stg["gate"]
            if stg["status"] == "進行中" and g["missing"]:
                out["blocked_gates"].append({
                    "project": p["name"], "stage": f"{stg['code']} {stg['name']}",
                    "missing": g["missing"], "exit_gate": stg["exit_gate"]})
    for k in ("must", "should", "may", "overdue"):
        out[k].sort(key=lambda r: (r.get("total_float", 99), r.get("planned_end", "")))
    return out


def week_view(week_end=None):
    """一週的完成/未完成/浮時消耗，週報的資料來源。"""
    cfg = db.load_config()
    cal = calendar()
    ref = sch.d(week_end) or dt.date.today()
    monday, friday = sch.week_bounds(ref, cfg.get("report_day", 5))
    sunday = monday + dt.timedelta(days=6)
    data = {"week_start": sch.s(monday), "week_end": sch.s(friday), "projects": []}
    for st in all_states(friday):
        if not st:
            continue
        p = st["project"]
        inwin = [t for t in st["tasks"] if _in_window(t, monday, sunday)]
        # 「本週完成」看的是實際完成日落在這週，不是計畫區間——遲交的任務結案那天理應
        # 出現在「它真正完成的那一週」，而不是從此在所有週報裡人間蒸發。
        done = [t for t in st["tasks"]
                if t["done"] and sch.d(t.get("actual_finish")) and
                monday <= sch.d(t["actual_finish"]) <= sunday]
        for t in done:
            t["late_days"] = late_workdays(cal, t)
        undone = [t for t in inwin if not t["done"]]
        nextwin = [t for t in st["tasks"]
                   if not t["done"] and _in_window(t, monday + dt.timedelta(days=7),
                                                   sunday + dt.timedelta(days=7))]
        logs = db.rows(
            "SELECT c.*, t.wbs_no, t.name FROM checkin c JOIN task t ON t.id=c.task_id "
            "WHERE t.project_id=? AND c.log_date BETWEEN ? AND ? ORDER BY c.log_date",
            (p["id"], sch.s(monday), sch.s(sunday)))
        data["projects"].append({
            "project": p, "summary": st["summary"],
            "done": done, "undone": undone,
            "overdue": [t for t in st["tasks"] if t["flag"] == "overdue"],
            "at_risk": [t for t in st["tasks"]
                        if t["flag"] in ("will_slip", "must_start", "critical", "cyclic")
                        and not t["done"]],
            "next_week": sorted(nextwin, key=lambda t: t.get("total_float", 99)),
            "stages": st["stages"], "checkins": logs,
        })
    return data


def apply_stage_template(project_id, overwrite=False):
    """把階段範本套進專案（建立 stage 與 doc_req）。"""
    tpl = db.load_stage_template()
    with db.conn() as c:
        if overwrite:
            c.execute("DELETE FROM doc_req WHERE project_id=?", (project_id,))
            c.execute("DELETE FROM stage WHERE project_id=?", (project_id,))
        for st in tpl["stages"]:
            c.execute(
                "INSERT OR IGNORE INTO stage(project_id,code,seq,name,purpose,exit_gate) "
                "VALUES (?,?,?,?,?,?)",
                (project_id, st["code"], st["seq"], st["name"],
                 st.get("purpose"), st.get("exit_gate")))
            for dc in st["docs"]:
                c.execute(
                    "INSERT OR IGNORE INTO doc_req(project_id,stage_code,doc_code,name,"
                    "required,note) VALUES (?,?,?,?,?,?)",
                    (project_id, st["code"], dc["code"], dc["name"],
                     1 if dc["required"] else 0, dc.get("note")))
    return {"ok": True, "stages": len(tpl["stages"])}


def baseline_revisions(project_id):
    return db.rows(
        "SELECT * FROM baseline_revision WHERE project_id=? ORDER BY revision_no",
        (project_id,))


def freeze_baseline(project_id, reason=None):
    """凍結（或重新基準化）承諾完工日：把目前 end_date 定為天花板，任務的目前計畫日
    同步存一份 baseline_start/baseline_end 快照，並在 baseline_revision 留一筆軌跡。
    之後 UI 對 baseline_* 欄位唯讀；要改只能再呼叫這個函式（等於「重新基準化」）。"""
    proj = db.one("SELECT * FROM project WHERE id=?", (project_id,))
    if not proj:
        return {"error": "查無此專案"}
    end_date = proj.get("end_date")
    if not end_date:
        return {"error": "尚未填目前目標完工日（end_date），無法凍結"}
    now = dt.datetime.now().isoformat(timespec="seconds")
    prev = db.rows("SELECT revision_no FROM baseline_revision WHERE project_id=? "
                   "ORDER BY revision_no DESC LIMIT 1", (project_id,))
    rev_no = (prev[0]["revision_no"] + 1) if prev else 1
    with db.conn() as c:
        c.execute("UPDATE project SET baseline_end=?, baseline_set_at=? WHERE id=?",
                  (end_date, now, project_id))
        c.execute(
            "INSERT INTO baseline_revision(project_id,revision_no,baseline_end,reason,"
            "created_at) VALUES (?,?,?,?,?)",
            (project_id, rev_no, end_date, reason, now))
        for t in db.rows("SELECT id,planned_start,planned_end FROM task WHERE project_id=?",
                         (project_id,)):
            c.execute("UPDATE task SET baseline_start=?, baseline_end=? WHERE id=?",
                      (t["planned_start"], t["planned_end"], t["id"]))
    return {"ok": True, "revision_no": rev_no, "baseline_end": end_date}
