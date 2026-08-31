# -*- coding: utf-8 -*-
"""匯出 .ics —— 給還是想在 Google 日曆上看一眼的時候用。

只匯出「需要出現在日曆上的東西」：
  · 進行中／未開始任務的區間（全天事件）
  · 關鍵與吃緊任務的「最後可動工日」提醒
  · 每日雙專案工作 block、每日收尾檢核、週五週報（重複事件）
已完成的任務不匯出，日曆不是歷史檔案庫。
"""
import datetime as dt

from . import core, db, schedule as sch

CRLF = "\r\n"


def _esc(t):
    return (str(t or "").replace("\\", "\\\\").replace(";", "\\;")
            .replace(",", "\\,").replace("\n", "\\n"))


def _fold(line):
    b = line.encode("utf-8")
    if len(b) <= 73:
        return line
    out, cur = [], b
    out.append(cur[:73].decode("utf-8", "ignore"))
    rest = cur[len(out[0].encode("utf-8")):]
    while rest:
        chunk = rest[:72].decode("utf-8", "ignore")
        out.append(" " + chunk)
        rest = rest[len(chunk.encode("utf-8")):]
    return CRLF.join(out)


def _uid(*parts):
    return "-".join(str(p) for p in parts).replace(" ", "") + "@cl-wbs.local"


def build(tzid="Asia/Taipei"):
    cfg = db.load_config()
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    L = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//CL_WBS//TW//ZH",
         "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
         "X-WR-CALNAME:專案 WBS", f"X-WR-TIMEZONE:{tzid}"]

    def ev(uid, summary, lines, desc=""):
        L.extend(["BEGIN:VEVENT", f"UID:{uid}", f"DTSTAMP:{stamp}",
                  _fold(f"SUMMARY:{_esc(summary)}")])
        L.extend(lines)
        if desc:
            L.append(_fold(f"DESCRIPTION:{_esc(desc)}"))
        L.append("END:VEVENT")

    for st in core.all_states():
        if not st:
            continue
        p = st["project"]
        for t in st["tasks"]:
            if t["done"] or not t["planned_start"] or not t["planned_end"]:
                continue
            s0 = sch.d(t["planned_start"])
            s1 = sch.d(t["planned_end"]) + dt.timedelta(days=1)   # DTEND 為排除端
            title = (f"{t['mark']} {p['code']}｜{t['wbs_no']} {t['name']} "
                     f"⟨浮時{t['total_float']}d⟩")
            desc = (f"專案：{p['name']}\n階段：{t.get('stage_code') or '—'}\n"
                    f"原因：{t['flag_reason']}\n最晚完成：{t['lf']}\n"
                    f"最後可動工日：{t['last_start']}\n備註：{t.get('note') or '—'}")
            ev(_uid("task", p["code"], t["wbs_no"]), title,
               [f"DTSTART;VALUE=DATE:{s0.strftime('%Y%m%d')}",
                f"DTEND;VALUE=DATE:{s1.strftime('%Y%m%d')}",
                "TRANSP:TRANSPARENT",
                f"CATEGORIES:{p['code']},{t['flag']}"], desc)

            if t["flag"] in ("critical", "tight", "must_start") and t["last_start"]:
                ls = sch.d(t["last_start"])
                ev(_uid("ls", p["code"], t["wbs_no"]),
                   f"⛔ 最後可動工日：{p['code']}｜{t['wbs_no']} {t['name']}",
                   [f"DTSTART;VALUE=DATE:{ls.strftime('%Y%m%d')}",
                    f"DTEND;VALUE=DATE:{(ls + dt.timedelta(days=1)).strftime('%Y%m%d')}",
                    "BEGIN:VALARM", "TRIGGER:-P1D", "ACTION:DISPLAY",
                    "DESCRIPTION:明天是最後可動工日", "END:VALARM"],
                   f"過了今天未開工，{p['name']} 完工日將順延。")

    # ---- 固定節奏 ----
    monday = dt.date.today() - dt.timedelta(days=dt.date.today().isoweekday() - 1)
    ymd = monday.strftime("%Y%m%d")

    def hhmm(x):
        h, m = x.split(":")
        return f"{int(h):02d}{int(m):02d}00"

    def add_minutes(hm, delta):
        """字串拼接做時間加法在跨小時/跨午夜時會產生 176500 這種不存在的時刻——
        一律走 datetime 運算：加完直接讀 .hour/.minute，datetime 自己處理進位。"""
        h, m = (int(x) for x in hm.split(":"))
        t = dt.datetime(2000, 1, 1, h, m) + dt.timedelta(minutes=delta)
        return f"{t.hour:02d}:{t.minute:02d}"

    for code, span in (cfg.get("blocks") or {}).items():
        pr = db.one("SELECT * FROM project WHERE code=?", (code,))
        if not pr or "-" not in span:
            continue
        a, b = span.split("-")
        ev(_uid("block", code),
           f"🔵 {pr['name']} 深度工作",
           [f"DTSTART;TZID={tzid}:{ymd}T{hhmm(a)}",
            f"DTEND;TZID={tzid}:{ymd}T{hhmm(b)}",
            "RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR"],
           "只做這個專案。開始前先看 App 的『今日』頁。")

    ci = cfg.get("daily_checkin_time", "17:20")
    ev(_uid("checkin"), "📝 每日收尾檢核（10 分鐘）",
       [f"DTSTART;TZID={tzid}:{ymd}T{hhmm(ci)}",
        f"DTEND;TZID={tzid}:{ymd}T{hhmm(add_minutes(ci, 10))}",
        "RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR",
        "BEGIN:VALARM", "TRIGGER:-PT5M", "ACTION:DISPLAY",
        "DESCRIPTION:更新今日進度", "END:VALARM"],
       "在 App 更新今天每一項的進度與狀態。只做這件事，不做別的。這是週報的唯一資料來源。")

    thu = monday + dt.timedelta(days=3)
    ev(_uid("buffer"), "🧯 落後追補時段（不排新工作）",
       [f"DTSTART;TZID={tzid}:{thu.strftime('%Y%m%d')}T140000",
        f"DTEND;TZID={tzid}:{thu.strftime('%Y%m%d')}T170000",
        "RRULE:FREQ=WEEKLY;BYDAY=TH"],
       "專吃本週落後項目。這段時間若沒事做，代表本週狀況良好。")

    rt = cfg.get("report_time", "14:00")
    fri = monday + dt.timedelta(days=cfg.get("report_day", 5) - 1)
    ev(_uid("report"), "📊 WBS 進度週報產出",
       [f"DTSTART;TZID={tzid}:{fri.strftime('%Y%m%d')}T{hhmm(rt)}",
        f"DTEND;TZID={tzid}:{fri.strftime('%Y%m%d')}T{hhmm(add_minutes(rt, 60))}",
        "RRULE:FREQ=WEEKLY;BYDAY=FR",
        "BEGIN:VALARM", "TRIGGER:-PT30M", "ACTION:DISPLAY",
        "DESCRIPTION:準備週報", "END:VALARM"],
       "在 App 的『週報』頁按產生，檢查四段內容後匯出。")

    L.append("END:VCALENDAR")
    return CRLF.join(L) + CRLF
