# -*- coding: utf-8 -*-
"""初始資料——通用示範資料，不含任何真實廠商/人名/專案內容。

這份資料只在「第一次啟動、資料庫還沒有任何專案」時才會種進去，讓系統一開機就有
東西可以操作、示範怎麼用。真實的專案資料一律直接在系統裡輸入，不寫進原始碼——
這支檔案會隨程式碼一起流通（包含推到 public repo 當更新中繼站），寫真實資料進來
等於把公司內部資訊直接曝露在外，2026-08-28 已經因為這個理由整支清過一次。
本檔可重複執行；已存在的專案不會被覆寫（除非 run(force=True)）。
"""
import datetime as dt

from . import core, db

P01 = dict(
    code="P01", name="示範專案一（系統轉換）", owner="王小明",
    start_date="2026-08-01", end_date="2027-07-31",
    docs_subdir="P01_示範專案一", color="#2563eb")

P02 = dict(
    code="P02", name="示範專案二（平台建置）", owner="陳小華",
    start_date="2026-07-01", end_date="2027-10-31",
    docs_subdir="P02_示範專案二", color="#ea580c")

T01 = [
    # wbs, stage, name, start, end, preds, status, progress, owner, note
    ("A1", "S01", "需求盤點與規劃", "2026-08-03", "2026-09-18", "",
     "進行中", 50, "王小明", "示範用備註：需求訪談與範圍確認進行中"),
    ("A2", "S01", "產出需求規格與採購規範", "2026-09-21", "2026-09-30", "A1",
     "未開始", 0, "王小明", "完成需求規格及採購項目審核"),
    ("A3", "S04", "完成廠商評選及啟動", "2026-10-01", "2026-10-31", "A2",
     "未開始", 0, "王小明", "選出正式廠商及啟動公文簽核"),
    ("A4", "S06", "Phase 1 環境建置", "2026-10-01", "2026-10-31", "A2",
     "未開始", 0, "王小明", "前導作業，將系統實裝並建置"),
    ("A5", "S05", "完成建置及合約簽訂", "2026-11-01", "2026-11-30", "A3",
     "未開始", 0, "王小明", "完成公文簽核及廠商程序"),
    ("A6", "S06", "Phase 1 系統轉換導入", "2026-11-01", "2027-02-26", "A4",
     "未開始", 0, "王小明", "少量導入並完成驗證"),
    ("A7", "S06", "Phase 2 正式系統建置", "2027-01-01", "2027-02-28", "A5,A6",
     "未開始", 0, "王小明", "pilot 轉正式環境"),
    ("A8", "S06", "功能建置及權限轉換納管", "2027-03-01", "2027-05-31", "A7",
     "未開始", 0, "王小明", "完成系統納管作業"),
    ("A9", "S07", "整合及測試", "2027-03-01", "2027-05-31", "A7",
     "未開始", 0, "王小明", "完成整合測試、功能驗證等問題修正"),
    ("A10", "S08", "Phase 2 正式上線", "2027-04-01", "2027-06-30", "A8:FF,A9:FF",
     "未開始", 0, "王小明", "正式切換並取代原有系統"),
    ("A11", "S09", "全案驗收", "2027-07-01", "2027-07-31", "A10",
     "未開始", 0, "王小明", "系統穩定運作一段時間後完成驗收及結案"),
]

T02 = [
    ("B1", "S01", "專案需求盤點及規劃", "2026-07-01", "2026-07-28", "",
     "已完成", 100, "陳小華", "示範用備註：已完成需求盤點與架構規劃"),
    ("B2", "S01", "跨部門需求整合", "2026-08-01", "2026-08-31", "B1",
     "進行中", 50, "陳小華", "示範用備註：跨部門需求確認討論進行中"),
    ("B3", "S03", "導入 Pilot 驗證", "2026-09-01", "2026-09-30", "B2",
     "未開始", 0, "陳小華", "啟動 pilot 系統驗證測試"),
    ("B4", "S04", "採購與招標作業", "2026-10-01", "2026-11-30", "B3",
     "未開始", 0, "陳小華", "啟動專案採購及採購流程"),
    ("B5", "S06", "系統環境建置", "2027-01-01", "2027-01-31", "B4",
     "未開始", 0, "陳小華", "開始系統環境建置"),
    ("B6", "S06", "系統功能建置與驗證", "2027-02-15", "2027-04-30", "B5",
     "未開始", 0, "陳小華", "完成功能建置、驗證及測試"),
    ("B7", "S07", "整合及測試", "2027-05-01", "2027-08-31", "B6",
     "未開始", 0, "陳小華", "完成跨系統整合測試"),
    ("B8", "S08", "正式上線", "2027-09-01", "2027-09-30", "B7",
     "未開始", 0, "陳小華", "完成系統切換並正式提供服務"),
    ("B9", "S09", "全案驗收", "2027-10-01", "2027-10-31", "B8",
     "未開始", 0, "陳小華", "系統穩定運作一個月後完成驗收及結案"),
]


def _insert(proj, tasks):
    pid = db.run(
        "INSERT INTO project(code,name,owner,start_date,end_date,docs_subdir,color) "
        "VALUES (?,?,?,?,?,?,?)",
        (proj["code"], proj["name"], proj["owner"], proj["start_date"],
         proj["end_date"], proj["docs_subdir"], proj["color"]))
    core.apply_stage_template(pid)
    for wbs, stage, name, s0, s1, preds, status, prog, owner, note in tasks:
        db.run(
            "INSERT INTO task(project_id,wbs_no,level,stage_code,name,owner,"
            "planned_start,planned_end,predecessors,status,progress,note) "
            "VALUES (?,?,'L0',?,?,?,?,?,?,?,?,?)",
            (pid, wbs, stage, name, owner, s0, s1, preds, status, prog, note))
    _roll_up_stages(pid)
    return pid


def _roll_up_stages(pid):
    today = dt.date.today().isoformat()
    for st in db.rows("SELECT * FROM stage WHERE project_id=?", (pid,)):
        ts = db.rows("SELECT * FROM task WHERE project_id=? AND stage_code=? "
                     "AND planned_start<>''", (pid, st["code"]))
        if not ts:
            continue
        s0 = min(t["planned_start"] for t in ts)
        s1 = max(t["planned_end"] for t in ts)
        if all(t["status"] == "已完成" for t in ts):
            status = "已完成"
        elif any(t["status"] in ("進行中", "延遲") for t in ts) or s0 <= today <= s1:
            status = "進行中"
        else:
            status = "未開始"
        db.run("UPDATE stage SET planned_start=?,planned_end=?,status=? WHERE id=?",
               (s0, s1, status, st["id"]))


def run(force=False):
    db.init_db()
    if force:
        db.run("DELETE FROM project")
    if not db.rows("SELECT id FROM project LIMIT 1"):
        _insert(P01, T01)
        _insert(P02, T02)
    return True


if __name__ == "__main__":
    import sys
    run(force="--force" in sys.argv)
    print("seed done")
