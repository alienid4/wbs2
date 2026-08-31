# -*- coding: utf-8 -*-
"""排程引擎：工作日計算、CPM 前推後推、浮時、關鍵路徑、延遲判定。

這裡是整個系統回答「哪些沒做完就會 delay」的唯一地方。
判定依據是浮時（total float），不是感覺，也不是進度條。
"""
import datetime as dt
import re

DATE_FMT = "%Y-%m-%d"
LINK_RE = re.compile(r"^(FS|SS|FF)?([+-]\d+)?$")


def d(s):
    if not s:
        return None
    if isinstance(s, dt.date):
        return s
    s = str(s).strip().replace("/", "-")
    parts = s.split("-")
    if len(parts) != 3:
        return None
    try:
        return dt.date(int(parts[0]), int(parts[1]), int(parts[2]))
    except ValueError:
        return None


def s(x):
    return x.strftime(DATE_FMT) if isinstance(x, dt.date) else (x or "")


class Calendar:
    def __init__(self, workweek=None, holidays=None):
        self.workweek = set(workweek or [1, 2, 3, 4, 5])   # 1=Mon .. 7=Sun
        self.holidays = set(d(h) for h in (holidays or []) if d(h))

    def is_workday(self, day):
        return day.isoweekday() in self.workweek and day not in self.holidays

    def next_workday(self, day):
        x = day
        for _ in range(400):
            x += dt.timedelta(days=1)
            if self.is_workday(x):
                return x
        return x

    def prev_workday(self, day):
        x = day
        for _ in range(400):
            x -= dt.timedelta(days=1)
            if self.is_workday(x):
                return x
        return x

    def snap_forward(self, day):
        x = day
        for _ in range(400):
            if self.is_workday(x):
                return x
            x += dt.timedelta(days=1)
        return day

    def add_workdays(self, day, n):
        """day 起算的第 n 個工作日（n=0 回傳 day 本身，先對齊到工作日）。"""
        x = self.snap_forward(day)
        step = 1 if n >= 0 else -1
        for _ in range(abs(int(n))):
            x = self.next_workday(x) if step > 0 else self.prev_workday(x)
        return x

    def workdays_between(self, a, b):
        """含頭含尾的工作日數。b < a 時回傳負值。"""
        if a is None or b is None:
            return 0
        sign = 1
        if b < a:
            a, b, sign = b, a, -1
        n, x = 0, a
        while x <= b and n < 3000:
            if self.is_workday(x):
                n += 1
            x += dt.timedelta(days=1)
        return sign * n


def compute(tasks, cal, project_finish=None, today=None):
    """對一組任務做 CPM。tasks 為 dict 清單，需含 wbs_no/planned_start/planned_end/
    predecessors/hard_deadline/status/progress。

    project_finish：承諾完工日（baseline_end 凍結後）。凍結前傳 None，退回「以目前計畫
    自然算出的終點」；凍結後這個日期是**天花板**，不會再被任務排程往後拉——排到承諾日
    之後的任務，浮時會算成負值並觸發 will_slip，而不是讓終點跟著任務一起往後漂。

    回傳 (metrics, meta)：
      metrics：{wbs_no: 逐項 CPM 指標}
      meta：{"forecast_finish": 依目前計畫實際算出的完工日,
             "baseline_finish": project_finish（未凍結為空字串）,
             "finish_variance_days": forecast 比 baseline 晚幾個工作日（未凍結為 None）}
    """
    today = today or dt.date.today()
    by_no = {}
    for t in tasks:
        by_no[t["wbs_no"]] = t

    # 相依關係支援 FS(預設)/SS/FF 與正負延時：  A3 · A3:SS · A3:FS+5 · A3:FF-2
    succ = {k: [] for k in by_no}
    preds = {}
    for no, t in by_no.items():
        raw = (t.get("predecessors") or "").replace("，", ",").replace(" ", "")
        plist = []
        for tok in raw.split(","):
            if not tok:
                continue
            body, _, spec = tok.partition(":")
            if body not in by_no or body == no:
                continue
            kind, lag = "FS", 0
            if spec:
                m = LINK_RE.match(spec.upper())
                if m:
                    kind = m.group(1) or "FS"
                    lag = int(m.group(2) or 0)
            link = (body, kind, lag)
            plist.append(link)
            succ[body].append((no, kind, lag))
        preds[no] = plist

    # 拓樸排序（有環時退化為原順序，並標記）
    order, temp, perm, cyclic = [], set(), set(), set()

    def visit(n):
        if n in perm:
            return
        if n in temp:
            # temp 此刻就是目前這條 DFS 路徑上的所有節點，也正好是這個環本身——
            # 只標 n 會讓環上其他節點看起來「沒事」，使用者會看到同一個環裡
            # 一項標警示、另一項卻是綠燈，比不標示更誤導。整條路徑一起標。
            cyclic.update(temp)
            return
        temp.add(n)
        for p, _k, _l in preds.get(n, []):
            visit(p)
        temp.discard(n)
        perm.add(n)
        order.append(n)

    for n in by_no:
        visit(n)

    m = {}
    for no, t in by_no.items():
        ps, pe = d(t.get("planned_start")), d(t.get("planned_end"))
        if ps and pe and pe < ps:
            ps, pe = pe, ps
        dur = cal.workdays_between(ps, pe) if (ps and pe) else 1
        m[no] = {
            "wbs_no": no, "dur": max(dur, 1),
            "planned_start": ps, "planned_end": pe,
            "hard_deadline": d(t.get("hard_deadline")),
            "preds": preds.get(no, []), "succs": succ.get(no, []),
            "cyclic": no in cyclic,
        }

    # ---- forward pass ----
    # 已排定的計畫日期視為基準（不改寫使用者的計畫）；沒填日期才由相依關係推導。
    conflicts = {}
    for no in order:
        x = m[no]
        earliest = None
        for p, kind, lag in x["preds"]:
            pm = m[p]
            if not pm.get("ef"):
                continue
            if kind == "FS":
                cand = cal.add_workdays(cal.next_workday(pm["ef"]), lag)
            elif kind == "SS":
                cand = cal.add_workdays(pm["es"], lag)
            else:  # FF：本項完成不得早於前置完成
                cand = cal.add_workdays(pm["ef"], lag - x["dur"] + 1)
            if earliest is None or cand > earliest:
                earliest = cand
        if x["planned_start"]:
            es = cal.snap_forward(x["planned_start"])
            if earliest and earliest > es:
                conflicts[no] = s(earliest)
        else:
            es = earliest or cal.snap_forward(today)
        x["es"] = es
        # 完成日落在假日時往前貼齊（週六結束＝實際週五結束），往後貼齊會製造假的相依衝突
        if x["planned_end"]:
            pe = x["planned_end"]
            x["ef"] = pe if cal.is_workday(pe) else cal.prev_workday(pe)
        else:
            x["ef"] = cal.add_workdays(es, x["dur"] - 1)
        if x["ef"] < x["es"]:
            x["ef"] = cal.add_workdays(x["es"], x["dur"] - 1)

    # ---- 專案終點 ----
    # forecast：不管有沒有凍結，都是「照目前計畫實際會落在哪天」——純粹由任務算出來，
    # 從不被承諾日限制，這是主管要問的「現在預測幾號完工」那個數字。
    ends = [x["ef"] for x in m.values() if x.get("ef")]
    forecast_finish = max(ends) if ends else today
    if not cal.is_workday(forecast_finish):
        forecast_finish = cal.prev_workday(forecast_finish)

    # baseline：承諾完工日，凍結後是天花板，不會被任務往後拉。backward pass 一律以它為準，
    # 這樣排到承諾日之後的任務，浮時才會算成負值、觸發 will_slip——而不是終點跟著漂、
    # 紅燈跟著熄。凍結前沒有承諾日，退回舊行為（用 forecast 自己當終點，不然任何任務都會
    # 因為「沒有終點可比」而拿不到浮時）。
    pf = d(project_finish)
    if pf:
        pf = pf if cal.is_workday(pf) else cal.prev_workday(pf)
        finish = pf
    else:
        finish = forecast_finish
    if not cal.is_workday(finish):
        finish = cal.prev_workday(finish)

    # ---- backward pass ----
    for no in reversed(order):
        x = m[no]
        lf = None
        for sc, kind, lag in x["succs"]:
            sm = m[sc]
            if "ls" not in sm:
                # 只在有循環相依時發生：reversed(order) 對環上的節點不保證真的是逆拓樸
                # 順序，這個 successor 可能還沒被算過。忽略它（等於這條邊暫時不參與
                # 限制）好過整頁 500——環本來就會被標成 cyclic、flag_reason 會講清楚
                # 這個節點的浮時不可信，不需要靠崩潰來提醒使用者。
                continue
            if kind == "FS":
                cand = cal.add_workdays(cal.prev_workday(sm["ls"]), -lag)
            elif kind == "SS":
                cand = cal.add_workdays(sm["ls"], -lag + x["dur"] - 1)
            else:  # FF
                cand = cal.add_workdays(sm["lf"], -lag)
            lf = cand if lf is None else min(lf, cand)
        if lf is None:
            lf = finish
        if x["hard_deadline"]:
            lf = min(lf, cal.snap_forward(x["hard_deadline"])
                     if cal.is_workday(x["hard_deadline"]) else
                     cal.prev_workday(x["hard_deadline"]))
        x["lf"] = lf
        x["ls"] = cal.add_workdays(lf, -(x["dur"] - 1))

    # ---- 浮時與旗標 ----
    for no, x in m.items():
        t = by_no[no]
        x["total_float"] = cal.workdays_between(x["ef"], x["lf"]) - 1
        x["critical"] = x["total_float"] <= 0
        x["preds"] = [p for p, _k, _l in x["preds"]]
        x["succs"] = [sc for sc, _k, _l in x["succs"]]
        x["plan_conflict"] = conflicts.get(no, "")
        status = (t.get("status") or "未開始").strip()
        prog = int(t.get("progress") or 0)
        done = status == "已完成" or prog >= 100
        x["done"] = done

        overdue = 0
        if not done and x["planned_end"] and today > x["planned_end"]:
            overdue = cal.workdays_between(cal.next_workday(x["planned_end"]), today)
        x["overdue_days"] = max(overdue, 0)

        # 剩餘可用天 vs 剩餘工作量
        remain_work = max(1, round(x["dur"] * (100 - prog) / 100.0)) if not done else 0
        # 起算點：已開工或已到期的用今天，尚未到期的用計畫開工日 —— 否則未來任務會
        # 因為「離死線還很久」而算出一個沒有意義的大浮時。
        origin = cal.snap_forward(today)
        if x["planned_start"] and x["planned_start"] > origin:
            origin = cal.snap_forward(x["planned_start"])
        remain_cap = cal.workdays_between(origin, x["lf"]) if x["lf"] else 0
        x["remain_work"] = remain_work
        x["live_float"] = (remain_cap - remain_work) if not done else None

        # 最後可動工日：還沒開工的任務，過了這天就吃掉浮時
        x["last_start"] = x["ls"]
        x["must_start_today"] = (
            not done and status == "未開始" and x["ls"] and cal.snap_forward(today) >= x["ls"]
        )

        if done:
            flag, reason = "done", "已完成"
        elif x["cyclic"]:
            # 環狀相依：拓樸排序退化成任意順序，這一項（跟環上其他項）算出來的浮時
            # 沒有意義——過去這個狀態只寫進 dict、從沒被讀出來，紅綠燈照樣正常顯示，
            # 是這系統唯一「算錯了但看起來一切正常」的地方，優先權必須最高。
            flag, reason = "cyclic", "前置關係形成循環，浮時計算不可信，請檢查「前置」欄"
        elif x["overdue_days"] > 0:
            flag, reason = "overdue", f"已逾期 {x['overdue_days']} 個工作日"
        elif x["live_float"] is not None and x["live_float"] < 0:
            flag, reason = "will_slip", f"剩餘工作量已超出可用時間 {abs(x['live_float'])} 天"
        elif x["must_start_today"]:
            flag, reason = "must_start", "已到最後可動工日，今天不開工即延遲"
        elif x["critical"]:
            flag, reason = "critical", "零浮時，任何落後直接推遲全案"
        elif x["total_float"] <= 2:
            flag, reason = "tight", f"浮時僅 {x['total_float']} 個工作日"
        else:
            flag, reason = "ok", f"浮時 {x['total_float']} 個工作日"
        if x["plan_conflict"] and not done:
            reason += f"（計畫衝突：依相依關係最早只能 {x['plan_conflict']} 開工）"
        x["flag"] = flag
        x["flag_reason"] = reason
        x["mark"] = {"overdue": "🛑", "will_slip": "🔴", "must_start": "🔴",
                     "critical": "🔴", "tight": "🟡", "ok": "⚪", "done": "✅",
                     "cyclic": "🌀"}[flag]

        for k in ("es", "ef", "ls", "lf", "planned_start", "planned_end",
                  "hard_deadline", "last_start"):
            x[k] = s(x[k]) if x.get(k) else ""

    # 正值＝比承諾晚幾個工作日，負值＝提前，0＝準時。刻意不做下限——「提前」是真的訊息，
    # 夾成 0 等於把好消息也藏起來。workdays_between 是頭尾皆含的計數，跟 total_float 同一種
    # 算法必須各自照方向減 1，不能對負值直接套同一條公式（會多扣一天）。
    if pf is None:
        variance = None
    elif forecast_finish >= pf:
        variance = cal.workdays_between(pf, forecast_finish) - 1
    else:
        variance = -(cal.workdays_between(forecast_finish, pf) - 1)
    meta = {
        "forecast_finish": s(forecast_finish),
        "baseline_finish": s(pf) if pf else "",
        "finish_variance_days": variance,
    }
    return m, meta


def week_bounds(day=None, report_day=5):
    """回傳該週的 (週一, 報告日)。report_day: 1=Mon..7=Sun，預設 5=Fri。"""
    day = day or dt.date.today()
    monday = day - dt.timedelta(days=day.isoweekday() - 1)
    return monday, monday + dt.timedelta(days=report_day - 1)
