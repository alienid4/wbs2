# -*- coding: utf-8 -*-
"""煙霧測試——單檔、純 stdlib unittest，不需要 pytest。

每個測試各自用一個全新的暫存 DB／config（setUp 每次重建），不共用狀態、不動你真正的
data/wbs.db。涵蓋這次改動最怕「看起來對、其實錯」的地方：latin-1 檔名、baseline 天花板、
wbs_no 改名連動、輸入驗證、里程碑、循環相依不能讓整頁崩潰、報表匯出。跑法：

    py tests/smoke.py
"""
import datetime as dt
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from app import db  # noqa: E402
from app import seed  # noqa: E402
from app import core, docs_scan, schedule, server, report, report_html, xls_export  # noqa: E402


class SmokeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="clwbs_smoke_")
        cfg = {
            "docs_root": self.tmp, "db_path": os.path.join(self.tmp, "wbs.db"),
            "port": 8765, "workweek": [1, 2, 3, 4, 5], "holidays": [],
            "report_day": 5, "report_time": "14:00", "daily_checkin_time": "17:20",
            "blocks": {}, "amber_float_days": 2,
        }
        cfg_path = os.path.join(self.tmp, "config.json")
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False)
        db.CONFIG_PATH = cfg_path  # 隔離：不碰真正的 config.json / data/wbs.db
        db.init_db()
        seed.run()
        self.p01 = db.one("SELECT * FROM project WHERE code='P01'")
        self.p02 = db.one("SELECT * FROM project WHERE code='P02'")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- 基礎：能種資料、能算 state ----
    def test_seed_and_project_state(self):
        st = core.project_state(self.p01["id"])
        self.assertEqual(st["summary"]["total"], 11)
        self.assertFalse(st["summary"]["baseline_set"])

    # ---- latin-1 中文檔名 bug（b4）：真的修了 ----
    def test_dl_headers_no_unicode_crash(self):
        h = server._dl_headers("WBS_weekly_2026-08-28.md", "WBS_週報_2026-08-28.md")
        v = h["Content-Disposition"]
        v.encode("latin-1")  # 不炸就是修好了
        self.assertIn("filename*=UTF-8''", v)

    # ---- baseline：天花板而非地板 ----
    def test_baseline_ceiling_caps_finish_not_extends(self):
        pid = self.p01["id"]
        db.run("UPDATE project SET end_date=? WHERE id=?", ("2027-07-30", pid))  # 週五
        r = core.freeze_baseline(pid)
        self.assertTrue(r.get("ok"))
        before = core.project_state(pid)["summary"]
        self.assertTrue(before["baseline_set"])
        self.assertEqual(before["baseline_finish"], "2027-07-30")

        # 把最後一項往後推 3 個月，重現「過去會被吃掉」的條件
        last = db.one("SELECT * FROM task WHERE project_id=? AND wbs_no='A11'", (pid,))
        db.run("UPDATE task SET planned_start=?, planned_end=? WHERE id=?",
              ("2027-08-02", "2027-10-29", last["id"]))
        after = core.project_state(pid)["summary"]
        # 承諾日必須維持不動（天花板），forecast 必須真的往後飄，variance 必須是正的
        self.assertEqual(after["baseline_finish"], "2027-07-30")
        self.assertGreater(after["finish_variance_days"], 0)
        tasks = core.project_state(pid)["tasks"]
        self.assertTrue(any(t["flag"] in ("will_slip", "overdue", "critical")
                            for t in tasks if t["wbs_no"] in ("A10", "A11")))

    # ---- 相依驗證：亂打的 token 要被擋 ----
    def test_predecessor_validation_rejects_garbage(self):
        pid = self.p02["id"]
        with self.assertRaises(ValueError):
            server._validate_predecessors("B1:FS5", pid)  # 少了 +
        with self.assertRaises(ValueError):
            server._validate_predecessors("Z99", pid)     # 不存在的項次
        server._validate_predecessors("B1:FS+5", pid)     # 合法，不該丟例外

    # ---- wbs_no 改名要連動改掉別人的 predecessors ----
    def test_rename_cascade(self):
        pid = self.p02["id"]
        db.run("UPDATE task SET wbs_no='B1X' WHERE project_id=? AND wbs_no='B1'", (pid,))
        n = server._rename_wbs_cascade(pid, "B1", "B1X")
        self.assertGreaterEqual(n, 1)
        b2 = db.one("SELECT * FROM task WHERE project_id=? AND wbs_no='B2'", (pid,))
        self.assertEqual(b2["predecessors"], "B1X")

    # ---- 里程碑：level='M' 不需要碰 CPM 不變量也能算 ----
    def test_milestone_zero_special_case(self):
        pid = self.p01["id"]
        db.run(
            "INSERT INTO task(project_id,wbs_no,level,name,planned_start,planned_end,"
            "predecessors,status,progress) VALUES (?,?,?,?,?,?,?,?,?)",
            (pid, "MS1", "M", "決標", "2026-10-31", "2026-10-31", "A3", "未開始", 0))
        st = core.project_state(pid)
        self.assertEqual(len(st["milestones"]), 1)
        self.assertEqual(st["milestones"][0]["wbs_no"], "MS1")
        self.assertEqual(st["summary"]["total"], 12)

    # ---- 拖拉調整專案順序：core.projects() 要照 sort_order 排，不是永遠照代號 ----
    def test_project_sort_order_overrides_code_order(self):
        p02 = db.one("SELECT * FROM project WHERE code='P02'")
        db.run("UPDATE project SET sort_order=1 WHERE id=?", (p02["id"],))
        db.run("UPDATE project SET sort_order=2 WHERE id=?", (self.p01["id"],))
        ordered = core.projects()
        self.assertEqual([p["code"] for p in ordered], ["P02", "P01"])

    # ---- 專案協同負責人：多對多，全量覆蓋不是增量新增，順序不影響比對 ----
    def test_project_co_owners_roundtrip(self):
        pid = self.p01["id"]
        by_id = {p["id"]: p for p in core.projects(include_archived=True)}
        self.assertEqual(by_id[pid]["co_owners"], [])
        db.run("INSERT INTO project_owner(project_id, person_name) VALUES (?,?),(?,?)",
              (pid, "測試協同甲", pid, "測試協同乙"))
        by_id = {p["id"]: p for p in core.projects(include_archived=True)}
        self.assertEqual(sorted(by_id[pid]["co_owners"]), ["測試協同乙", "測試協同甲"])
        # 全量覆蓋：換一批人，舊的要真的消失，不是疊加
        with db.conn() as c:
            c.execute("DELETE FROM project_owner WHERE project_id=?", (pid,))
            c.execute("INSERT INTO project_owner(project_id, person_name) VALUES (?,?)",
                     (pid, "測試協同丙"))
        by_id = {p["id"]: p for p in core.projects(include_archived=True)}
        self.assertEqual(by_id[pid]["co_owners"], ["測試協同丙"])
        # project_state()（單一專案頁用的那支）也要看得到協同負責人，不是只有
        # core.projects()（列表用的那支）——這兩支各自組資料，容易漏改一支
        st = core.project_state(pid)
        self.assertEqual(st["project"]["co_owners"], ["測試協同丙"])

    # ---- 資安基本款：Server header 不能洩漏 Python 版本、回應要帶基本安全 header、
    # 500 錯誤不能把內部例外訊息直接回給瀏覽器（弱掃會抓這幾項） ----
    def test_response_headers_and_error_body_are_hardened(self):
        from http.server import ThreadingHTTPServer
        srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        port = srv.server_address[1]
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/version", timeout=5) as resp:
                headers = resp.headers
                self.assertNotIn("Python", headers.get("Server", ""))
                self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")
                self.assertEqual(headers.get("X-Frame-Options"), "DENY")
            # 故意送壞掉的 JSON body，讓 self._body() 在 do_POST 的 try 區塊裡炸出
            # json.JSONDecodeError，確認 500 回應是通用訊息，不是把 Python 例外
            # 原始訊息（例如 "JSONDecodeError: Expecting value: line 1 column 1"）吐回去。
            req = urllib.request.Request(
                "http://127.0.0.1:{}/api/tasks".format(port),
                data=b"{not valid json",
                headers={"Content-Type": "application/json"}, method="POST")
            try:
                urllib.request.urlopen(req, timeout=5)
            except urllib.error.HTTPError as e:
                body = json.loads(e.read().decode("utf-8"))
                self.assertNotIn("Error", body.get("error", ""))  # 不含 "XxxError" 這種例外類別名
                self.assertNotIn("Traceback", body.get("error", ""))
        finally:
            srv.shutdown()
            srv.server_close()

    # ---- 登入 session cookie 要有 SameSite，降低 CSRF 風險 ----
    def test_login_cookie_has_samesite(self):
        from http.server import ThreadingHTTPServer
        srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        port = srv.server_address[1]
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            cfg = db.load_config()
            cfg["bind_host"] = "0.0.0.0"
            db.save_config(cfg)
            db.run("INSERT INTO person(name, role) VALUES (?,?)", ("測試SameSite者", "user"))
            body = json.dumps({"name": "測試SameSite者", "password": "samesite12345"}).encode("utf-8")
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/auth/login", data=body,
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=5) as resp:
                self.assertIn("SameSite=Lax", resp.headers.get("Set-Cookie", ""))
        finally:
            cfg["bind_host"] = "127.0.0.1"
            db.save_config(cfg)
            srv.shutdown()
            srv.server_close()

    # ---- 前端只送 {id, co_owners}（勾選協同負責人時的實際送法）不能整支 API 炸掉：
    # PROJ_FIELDS 過濾後 fields 會是空字典，UPDATE project SET  WHERE id=? 這種空
    # SET子句是無效 SQL，會丟例外、co_owners 也就永遠寫不進去——即使前端顯示「已儲存」。
    def test_project_co_owners_only_body_persists_via_real_api(self):
        from http.server import ThreadingHTTPServer
        srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        port = srv.server_address[1]
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            pid = self.p01["id"]
            body = json.dumps({"id": pid, "co_owners": ["測試協同甲", "測試協同乙"]}).encode("utf-8")
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/projects", data=body,
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=5) as resp:
                self.assertEqual(resp.status, 200)
            co = db.rows(
                "SELECT person_name FROM project_owner WHERE project_id=? ORDER BY person_name", (pid,))
            self.assertEqual([r["person_name"] for r in co], ["測試協同乙", "測試協同甲"])
        finally:
            srv.shutdown()
            srv.server_close()

    # ---- 人員名單：負責人是下拉選單，來源是共用名單，不是自由打字 ----
    # ---- 階段日期是任務日期彙總出來的，任務改了要自動跟著重算，不能停在舊數字 ----
    def test_roll_up_stage_dates_reflects_task_changes(self):
        pid = self.p01["id"]
        stage = db.one("SELECT * FROM stage WHERE project_id=? AND code='S01'", (pid,))
        # A1、A2 都在 S01，先確認彙總後的範圍涵蓋兩者
        core.roll_up_stage_dates(pid)
        stage = db.one("SELECT * FROM stage WHERE id=?", (stage["id"],))
        a1 = db.one("SELECT * FROM task WHERE project_id=? AND wbs_no='A1'", (pid,))
        a2 = db.one("SELECT * FROM task WHERE project_id=? AND wbs_no='A2'", (pid,))
        self.assertEqual(stage["planned_start"], min(a1["planned_start"], a2["planned_start"]))
        self.assertEqual(stage["planned_end"], max(a1["planned_end"], a2["planned_end"]))

        # 把 A2 的結束日往後拉，重算後階段的結束日要跟著變晚
        db.run("UPDATE task SET planned_end='2027-12-31' WHERE id=?", (a2["id"],))
        core.roll_up_stage_dates(pid)
        stage2 = db.one("SELECT * FROM stage WHERE id=?", (stage["id"],))
        self.assertEqual(stage2["planned_end"], "2027-12-31")
        # 刻意不動 status——使用者可能手動選了狀態，重算日期不該把它蓋掉
        self.assertEqual(stage2["status"], stage["status"])

    def test_person_list_and_doc_req_owner_assignment(self):
        pid = db.run("INSERT INTO person(name) VALUES (?)", ("測試甲君",))
        self.assertIsNotNone(db.one("SELECT * FROM person WHERE id=?", (pid,)))
        with self.assertRaises(Exception):
            db.run("INSERT INTO person(name) VALUES (?)", ("測試甲君",))  # 名字不能重複

        req = db.one("SELECT * FROM doc_req WHERE project_id=? AND doc_code='REQ-SPEC'", (self.p01["id"],))
        db.run("UPDATE doc_req SET owner=? WHERE id=?", ("測試甲君", req["id"]))
        item = next(i for i in docs_scan.stage_docs(self.p01["id"], "S01")["items"]
                   if i["doc_code"] == "REQ-SPEC")
        self.assertEqual(item["owner"], "測試甲君")

        # 刪掉名單上的人，不會動到已經填在文件上的負責人文字（純文字比對，不是外鍵）
        db.run("DELETE FROM person WHERE id=?", (pid,))
        item2 = next(i for i in docs_scan.stage_docs(self.p01["id"], "S01")["items"]
                    if i["doc_code"] == "REQ-SPEC")
        self.assertEqual(item2["owner"], "測試甲君")

    # ---- 改名（不是刪除）：現有指派這個人的地方要一起改過去，不能變孤兒字串 ----
    # 跟上面「刪除不動歷史資料」刻意不同調——改名是同一個人換寫法，語意不一樣。
    def test_rename_person_cascades_to_all_owner_fields(self):
        pid = self.p01["id"]
        person_id = db.run("INSERT INTO person(name) VALUES (?)", ("測試改名前",))
        db.run("UPDATE task SET owner=? WHERE project_id=? AND wbs_no='A1'", ("測試改名前", pid))
        db.run("UPDATE project SET owner=? WHERE id=?", ("測試改名前", pid))
        req = db.one("SELECT id FROM doc_req WHERE project_id=? AND doc_code='REQ-SPEC'", (pid,))
        db.run("UPDATE doc_req SET owner=? WHERE id=?", ("測試改名前", req["id"]))
        db.run("INSERT INTO project_owner(project_id, person_name) VALUES (?,?)",
              (pid, "測試改名前"))

        server._rename_person_cascade(person_id, "測試改名前", "測試改名後")

        self.assertEqual(db.one("SELECT name FROM person WHERE id=?", (person_id,))["name"],
                         "測試改名後")
        self.assertEqual(db.one("SELECT owner FROM task WHERE project_id=? AND wbs_no='A1'",
                                (pid,))["owner"], "測試改名後")
        self.assertEqual(db.one("SELECT owner FROM project WHERE id=?", (pid,))["owner"],
                         "測試改名後")
        self.assertEqual(db.one("SELECT owner FROM doc_req WHERE id=?", (req["id"],))["owner"],
                         "測試改名後")
        co = [r["person_name"] for r in db.rows(
            "SELECT person_name FROM project_owner WHERE project_id=?", (pid,))]
        self.assertIn("測試改名後", co)
        self.assertNotIn("測試改名前", co)

    # ---- 拖拉調整階段順序：代號要跟著改，不然「S03 排在 S02 前面」會被誤讀成順序 ----
    def test_stage_reorder_renames_codes_and_cascades_references(self):
        pid = self.p01["id"]
        stages = db.rows("SELECT id, code FROM stage WHERE project_id=? ORDER BY seq", (pid,))
        by_id = {r["id"]: r["code"] for r in stages}
        old_s04_task_ids = {r["id"] for r in
                            db.rows("SELECT id FROM task WHERE project_id=? AND stage_code='S04'", (pid,))}
        self.assertTrue(old_s04_task_ids, "測試前提：S04 底下至少要有一項任務")
        # 把 S02、S04 對調（挪到第二位）
        rest = [s["id"] for s in stages if s["code"] not in ("S02", "S04")]
        order = [rest[0], stages[3]["id"], stages[1]["id"]] + rest[1:]
        n = server._reorder_stages_cascade(pid, order, by_id)
        self.assertGreater(n, 0)
        new_stages = db.rows("SELECT id, code, seq FROM stage WHERE project_id=? ORDER BY seq", (pid,))
        # 代號永遠等於順序：排第二的一定叫 S02，不管它原本是哪個階段
        self.assertEqual(new_stages[1]["code"], "S02")
        self.assertEqual(new_stages[1]["id"], stages[3]["id"], "原本的 S04 現在應該排第二")
        # 原本掛在舊 S04 底下的任務要跟著改成新代號 S02，不能無聲斷 link
        new_s02_task_ids = {r["id"] for r in
                            db.rows("SELECT id FROM task WHERE project_id=? AND stage_code='S02'", (pid,))}
        self.assertEqual(old_s04_task_ids, new_s02_task_ids)

    # ---- 文件進度：0/50 不算已交，100 才算，且會反映在 all_ready 百分比裡 ----
    def test_doc_progress_only_100_counts_as_ready(self):
        pid = self.p01["id"]
        req = db.one("SELECT * FROM doc_req WHERE project_id=? AND doc_code='REQ-SPEC'", (pid,))
        data = docs_scan.stage_docs(pid, "S01")
        item = next(i for i in data["items"] if i["doc_code"] == "REQ-SPEC")
        self.assertFalse(item["ready"])

        db.run("UPDATE doc_req SET progress=50 WHERE id=?", (req["id"],))
        data = docs_scan.stage_docs(pid, "S01")
        item = next(i for i in data["items"] if i["doc_code"] == "REQ-SPEC")
        self.assertFalse(item["ready"], "50% 只是進度提示，不該被算成已交")

        db.run("UPDATE doc_req SET progress=100 WHERE id=?", (req["id"],))
        gate = docs_scan.gate_status(pid, "S01")
        self.assertEqual(gate["all_ready"], 1)
        self.assertTrue(next(i for i in docs_scan.stage_docs(pid, "S01")["items"]
                             if i["doc_code"] == "REQ-SPEC")["ready"])

    def test_avg_progress_reflects_partial_completion(self):
        # 兩項標 50%、其餘 0% 時，avg_progress 要反映平均值，不能卡在 0%
        # （all_ready 二元計數答的是另一個問題，兩個指標刻意分開）
        pid = self.p01["id"]
        reqs = db.rows("SELECT * FROM doc_req WHERE project_id=? AND stage_code='S01'", (pid,))
        for r in reqs[:2]:
            db.run("UPDATE doc_req SET progress=50 WHERE id=?", (r["id"],))
        gate = docs_scan.gate_status(pid, "S01")
        self.assertEqual(gate["all_ready"], 0, "50% 不算已交")
        expected = round(sum(50 if i < 2 else 0 for i in range(len(reqs))) / len(reqs))
        self.assertEqual(gate["avg_progress"], expected)
        self.assertGreater(gate["avg_progress"], 0, "有進度就不該顯示 0%")

    # ---- 週報：md／html／xls 三種輸出都要真的能產生、不炸 ----
    def test_report_outputs(self):
        md = report.generate("2026-08-28")
        self.assertIn("較原訂承諾", md)
        html = report_html.build("2026-08-28")
        self.assertIn("<!doctype html>", html)
        self.assertIn("WBS 進度週報", html)
        xls = xls_export.build()
        self.assertIn("<Workbook", xls)
        saved = report.save("2026-08-28")
        self.assertTrue(saved["content_html"])

    # ---- 循環相依：不能讓整個 compute() 掛掉，且要標記出來、不能悄悄綠燈 ----
    def test_cyclic_dependency_does_not_crash(self):
        pid = self.p02["id"]
        db.run("UPDATE task SET predecessors='B3' WHERE project_id=? AND wbs_no='B2'", (pid,))
        db.run("UPDATE task SET predecessors='B2' WHERE project_id=? AND wbs_no='B3'", (pid,))
        st = core.project_state(pid)  # 不該丟例外
        b2 = next(t for t in st["tasks"] if t["wbs_no"] == "B2")
        b3 = next(t for t in st["tasks"] if t["wbs_no"] == "B3")
        self.assertEqual(b2["flag"], "cyclic")
        self.assertEqual(b3["flag"], "cyclic")

    # ---- 版本徽章：version.json 存在且是合法 JSON，啟動時間有算出來 ----
    def test_version_file_and_started_at(self):
        vpath = os.path.join(db.ROOT, "version.json")
        self.assertTrue(os.path.isfile(vpath), "version.json 應該存在於專案根目錄")
        with open(vpath, encoding="utf-8") as f:
            v = json.load(f)
        self.assertIn("version", v)
        self.assertRegex(server._PROCESS_STARTED_AT, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")

    # ---- ics 時間進位：跨小時/跨午夜不該產生不存在的時刻 ----
    def test_ics_time_rollover(self):
        from app import ics_export
        cfg = db.load_config()
        cfg["daily_checkin_time"] = "23:55"
        cfg["report_time"] = "23:30"
        db.save_config(cfg)
        ics = ics_export.build()  # 不該丟例外
        self.assertIn("BEGIN:VCALENDAR", ics)
        self.assertNotIn("T176500", ics)
        self.assertNotIn("T246500", ics)

    # ---- 文件上傳：自動命名、版次遞增、寫完立刻重掃比對得到 ----
    def test_upload_doc_auto_names_and_matches(self):
        pid = self.p01["id"]
        core.apply_stage_template(pid)  # seed 已套過，這裡確認冪等不出錯
        req = db.one("SELECT * FROM doc_req WHERE project_id=? AND doc_code='REQ-SPEC'", (pid,))
        r = docs_scan.upload_doc(req["id"], "我的規格.docx", b"fake docx bytes")
        self.assertTrue(r.get("ok"), r)
        self.assertTrue(r["filename"].startswith("P01_01_REQ-SPEC_"))
        self.assertTrue(r["gate"]["required_ready"] >= 1)

        full = os.path.join(self.tmp, r["rel_path"])
        self.assertTrue(os.path.isfile(full))

        # 再傳一次版次要自動 +1，不是覆蓋或報錯
        r2 = docs_scan.upload_doc(req["id"], "改版.docx", b"v2 bytes")
        self.assertTrue(r2.get("ok"), r2)
        self.assertIn("_v2_", r2["filename"])

    # ---- 傳錯檔要能刪掉重傳，不是卡死在錯的版本 ----
    def test_delete_file_removes_disk_file_and_db_row(self):
        pid = self.p01["id"]
        req = db.one("SELECT * FROM doc_req WHERE project_id=? AND doc_code='REQ-SPEC'", (pid,))
        r = docs_scan.upload_doc(req["id"], "傳錯的檔案.docx", b"wrong file")
        self.assertTrue(r.get("ok"), r)
        row = db.one("SELECT * FROM doc_file WHERE project_id=? AND rel_path=?", (pid, r["rel_path"]))
        self.assertIsNotNone(row)
        full = os.path.join(self.tmp, r["rel_path"])
        self.assertTrue(os.path.isfile(full))

        d = docs_scan.delete_file(row["id"])
        self.assertTrue(d.get("ok"), d)
        self.assertFalse(os.path.isfile(full), "磁碟上的檔案要真的被刪掉")
        self.assertIsNone(db.one("SELECT * FROM doc_file WHERE id=?", (row["id"],)),
                          "doc_file 的資料庫紀錄也要跟著刪掉")
        # 刪完這個項目要變回「尚未在目錄中找到對應檔案」，可以重新上傳
        data = docs_scan.stage_docs(pid, "S01")
        item = next(i for i in data["items"] if i["doc_code"] == "REQ-SPEC")
        self.assertFalse(item["ready"])

    def test_upload_doc_rejects_bad_extension(self):
        pid = self.p01["id"]
        req = db.one("SELECT * FROM doc_req WHERE project_id=? AND doc_code='REQ-SPEC'", (pid,))
        r = docs_scan.upload_doc(req["id"], "malware.exe", b"x")
        self.assertIn("error", r)

    def test_resolve_file_blocks_path_traversal(self):
        # 就算 rel_path 被塞進奇怪的值，resolve_file 也不能讓它逃出 docs_root
        pid = self.p01["id"]
        db.run("INSERT INTO doc_file(project_id,rel_path,filename,scanned_at) "
              "VALUES (?,?,?,?)", (pid, "../../../../etc/passwd", "passwd", "now"))
        rid = db.rows("SELECT id FROM doc_file ORDER BY id DESC LIMIT 1")[0]["id"]
        self.assertIsNone(docs_scan.resolve_file(rid))

    # ---- 現在階段：用既有 stage.status 算，不是新欄位、也不是編的 ----
    def test_current_stage_and_status_sentence(self):
        pid = self.p01["id"]
        st = core.project_state(pid)
        self.assertEqual(st["current_stage"]["code"], "S01")  # 全部未開始 → 第一站
        self.assertIn("S01", st["status_sentence"])
        self.assertIn("尚未凍結基準線", st["status_sentence"])

        db.run("UPDATE stage SET status='已完成' WHERE project_id=? AND code IN "
              "('S01','S02','S03')", (pid,))
        st2 = core.project_state(pid)
        self.assertEqual(st2["current_stage"]["code"], "S04")

        db.run("UPDATE stage SET status='已完成' WHERE project_id=?", (pid,))
        st3 = core.project_state(pid)
        self.assertIsNone(st3["current_stage"])
        self.assertIn("皆已完成", st3["status_sentence"])

    # ---- 里程碑卡片內容：從真實任務/缺件算，沒掛 stage_code 就老實回 None，不編內容 ----
    def test_milestone_segment_is_real_not_fabricated(self):
        pid = self.p01["id"]
        db.run(
            "INSERT INTO task(project_id,wbs_no,level,stage_code,name,planned_start,"
            "planned_end,status,progress) VALUES (?,?,?,?,?,?,?,?,?)",
            (pid, "MS1", "M", "S01", "需求規格簽准", "2026-09-30", "2026-09-30", "未開始", 0))
        db.run(
            "INSERT INTO task(project_id,wbs_no,level,stage_code,name,planned_start,"
            "planned_end,status,progress) VALUES (?,?,?,?,?,?,?,?,?)",
            (pid, "MS2", "M", "", "沒掛階段的里程碑", "2026-10-01", "2026-10-01", "未開始", 0))
        st = core.project_state(pid)
        ms1 = next(m for m in st["milestones"] if m["wbs_no"] == "MS1")
        ms2 = next(m for m in st["milestones"] if m["wbs_no"] == "MS2")
        self.assertIsNotNone(ms1["segment"])
        self.assertIn("產出需求規格與採購規範", ms1["segment"]["open"])  # A2 真的在 S01
        self.assertTrue(len(ms1["segment"]["missing_docs"]) > 0)  # S01 真的缺件
        self.assertIsNone(ms2["segment"])  # 沒掛階段，老實回 None，不能編內容

    # ---- multipart 解析器：真的餵一段 HTTP body 進去，不只測邏輯 ----
    def test_multipart_parser_roundtrip(self):
        boundary = "TestBoundary123"
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="測試文件.docx"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n"
        ).encode("utf-8") + b"\x00\x01hello\xff" + f"\r\n--{boundary}--\r\n".encode("utf-8")
        fields = server._parse_multipart(body, f"multipart/form-data; boundary={boundary}")
        self.assertIn("file", fields)
        self.assertEqual(fields["file"]["filename"], "測試文件.docx")
        self.assertEqual(fields["file"]["data"], b"\x00\x01hello\xff")

    # ---- 延誤因果鏈：純規則沿「前置」欄位往回找根本原因，不叫 AI ----
    def test_delay_chain_traces_root_cause_through_predecessors(self):
        pid = self.p01["id"]
        overdue_end = (dt.date.today() - dt.timedelta(days=5)).isoformat()
        overdue_start = (dt.date.today() - dt.timedelta(days=10)).isoformat()
        db.run(
            "INSERT INTO task(project_id,wbs_no,level,stage_code,name,planned_start,"
            "planned_end,status,progress) VALUES (?,?,?,?,?,?,?,?,?)",
            (pid, "DCA", "L0", "S01", "上游拖累的來源", overdue_start, overdue_end, "進行中", 30))
        db.run(
            "INSERT INTO task(project_id,wbs_no,level,stage_code,name,planned_start,"
            "planned_end,status,progress,predecessors) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (pid, "DCB", "L0", "S01", "被拖累的中間項", overdue_end, overdue_end, "未開始", 0, "DCA"))
        db.run(
            "INSERT INTO task(project_id,wbs_no,level,stage_code,name,planned_start,"
            "planned_end,status,progress,predecessors) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (pid, "DCM", "M", "S01", "受影響的里程碑", overdue_end, overdue_end, "未開始", 0, "DCB"))
        st = core.project_state(pid)
        ms = next(m for m in st["milestones"] if m["wbs_no"] == "DCM")
        self.assertIn("上游拖累的來源", ms["delay_chain"])
        self.assertIn("受影響的里程碑", ms["delay_chain"])
        self.assertIn("已逾期", ms["delay_chain"])

        # 沒有落後的里程碑不該生出因果鏈文字，不能無中生有
        for m in st["milestones"]:
            if m["wbs_no"] != "DCM" and m.get("flag") in ("done", "ok"):
                self.assertEqual(m["delay_chain"], "")

    # ---- 負荷評估：跨專案依負責人彙整、日期重疊判定 ----
    def test_workload_summary_groups_by_owner_and_flags_overlap(self):
        pid = self.p01["id"]
        db.run(
            "INSERT INTO task(project_id,wbs_no,level,stage_code,name,owner,planned_start,"
            "planned_end,status,progress) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (pid, "WLA", "L0", "S01", "重疊測試甲", "測試甲君", "2026-09-01", "2026-09-10", "未開始", 0))
        db.run(
            "INSERT INTO task(project_id,wbs_no,level,stage_code,name,owner,planned_start,"
            "planned_end,status,progress) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (pid, "WLB", "L0", "S01", "重疊測試乙", "測試甲君", "2026-09-05", "2026-09-15", "未開始", 0))
        db.run(
            "INSERT INTO task(project_id,wbs_no,level,stage_code,name,owner,planned_start,"
            "planned_end,status,progress) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (pid, "WLC", "L0", "S01", "不重疊測試", "測試乙君", "2026-11-01", "2026-11-05", "未開始", 0))
        # 已完成的不該佔負荷
        db.run(
            "INSERT INTO task(project_id,wbs_no,level,stage_code,name,owner,planned_start,"
            "planned_end,status,progress) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (pid, "WLD", "L0", "S01", "已完成不算負荷", "測試甲君", "2026-09-01", "2026-09-10", "已完成", 100))
        result = core.workload_summary()
        wang = next(p for p in result["people"] if p["name"] == "測試甲君")
        names = {t["wbs_no"] for t in wang["tasks"]}
        self.assertIn("WLA", names)
        self.assertIn("WLB", names)
        self.assertNotIn("WLD", names)  # 已完成不進清單
        wla = next(t for t in wang["tasks"] if t["wbs_no"] == "WLA")
        self.assertTrue(wla["overlap"])  # 跟 WLB 日期有交集

        chen = next(p for p in result["people"] if p["name"] == "測試乙君")
        wlc = next(t for t in chen["tasks"] if t["wbs_no"] == "WLC")
        self.assertFalse(wlc["overlap"])  # 沒有其他項目可重疊

    # ---- 桌面提醒：預設關閉，設定值要能存能讀 ----
    def test_notify_config_defaults_off_and_roundtrips(self):
        cfg = db.load_config()
        self.assertFalse(cfg["notify_enabled"])  # 新功能預設不打擾使用者
        self.assertFalse(cfg["notify_email_enabled"])
        self.assertEqual(cfg["notify_time"], "09:00")
        self.assertEqual(cfg["notify_email_to"], "")
        cfg["notify_enabled"] = True
        cfg["notify_email_enabled"] = True
        cfg["notify_time"] = "10:30"
        cfg["notify_email_to"] = "test.user@example.com"
        db.save_config(cfg)
        cfg2 = db.load_config()
        self.assertTrue(cfg2["notify_enabled"])
        self.assertTrue(cfg2["notify_email_enabled"])
        self.assertEqual(cfg2["notify_time"], "10:30")
        self.assertEqual(cfg2["notify_email_to"], "test.user@example.com")
        self.assertEqual(cfg["notify_email_mode"], "outlook")  # 預設走 Outlook，不強迫填 SMTP
        cfg2["notify_email_mode"] = "smtp"
        cfg2["smtp_host"] = "smtp.gmail.com"
        cfg2["smtp_user"] = "test.user@example.com"
        cfg2["smtp_pass"] = "test-app-password"
        db.save_config(cfg2)
        cfg3 = db.load_config()
        self.assertEqual(cfg3["notify_email_mode"], "smtp")
        self.assertEqual(cfg3["smtp_host"], "smtp.gmail.com")
        self.assertEqual(cfg3["smtp_port"], 465)  # 預設埠沒特別改就維持 465

    # ---- 多人模式：密碼雜湊/驗證正確、鹽值不同雜湊就不同 ----
    def test_password_hash_roundtrip_and_salted(self):
        h1, salt1 = server._hash_password("hunter2")
        h2, salt2 = server._hash_password("hunter2")
        self.assertNotEqual(salt1, salt2)  # 每次自動生成新鹽值
        self.assertNotEqual(h1, h2)        # 同密碼、不同鹽值 -> 雜湊不同
        self.assertTrue(server._verify_password("hunter2", salt1, h1))
        self.assertFalse(server._verify_password("wrong", salt1, h1))
        self.assertFalse(server._verify_password("hunter2", salt1, h2))  # 鹽值對不上

    # ---- 多人模式：session 建立、查詢、過期即失效 ----
    def test_session_create_and_expiry(self):
        pid = db.run("INSERT INTO person(name) VALUES (?)", ("測試登入者",))
        token = server._create_session(pid)
        person = server._session_person(token)
        self.assertIsNotNone(person)
        self.assertEqual(person["name"], "測試登入者")
        self.assertIsNone(server._session_person("不存在的token"))
        # 手動把過期時間改到過去，確認真的會失效（不是只看 token 存不存在）
        db.run("UPDATE session SET expires_at=? WHERE token=?", ("2000-01-01T00:00:00", token))
        self.assertIsNone(server._session_person(token))

    # ---- 多人模式：auth_required 跟著 bind_host 走，不是獨立開關 ----
    def test_auth_required_follows_bind_host(self):
        cfg = db.load_config()
        self.assertEqual(cfg["bind_host"], "127.0.0.1")  # 測試環境沒特別設，走預設值
        self.assertFalse(server._auth_required())  # 127.0.0.1 -> 不強制登入
        cfg["bind_host"] = "0.0.0.0"
        db.save_config(cfg)
        self.assertTrue(server._auth_required())
        cfg["bind_host"] = "127.0.0.1"
        db.save_config(cfg)
        self.assertFalse(server._auth_required())

    # ---- 三級權限：user 不能編別人的，manager/admin 可以 ----
    def test_can_edit_others_by_role_tier(self):
        self.assertFalse(server.Handler._can_edit_others(None, {"role": "user"}))
        self.assertTrue(server.Handler._can_edit_others(None, {"role": "manager"}))
        self.assertTrue(server.Handler._can_edit_others(None, {"role": "admin"}))
        self.assertFalse(server.Handler._can_edit_others(None, None))

    # ---- 專案的主要／協同負責人不用先升級成 manager 就能改這個專案裡任何人的項目 ----
    def test_can_edit_project_covers_owner_and_co_owner(self):
        pid = self.p01["id"]
        owner_name = self.p01["owner"]
        db.run("INSERT INTO project_owner(project_id, person_name) VALUES (?,?)", (pid, "測試副手"))
        self.assertTrue(server.Handler._can_edit_project(None, {"name": owner_name}, pid))
        self.assertTrue(server.Handler._can_edit_project(None, {"name": "測試副手"}, pid))
        self.assertFalse(server.Handler._can_edit_project(None, {"name": "毫不相干的人"}, pid))
        self.assertFalse(server.Handler._can_edit_project(None, None, pid))

    # ---- 舊資料庫升級：is_admin=1 的既有資料要搬進 role='admin'，不能權限倒退 ----
    def test_migration_backfills_role_from_legacy_is_admin(self):
        legacy_path = os.path.join(self.tmp, "legacy.db")
        conn = sqlite3.connect(legacy_path)
        try:
            # 刻意只建「role 欄位加入前」的舊版 person 表結構
            conn.execute("""CREATE TABLE person (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                password_hash TEXT, password_salt TEXT, is_admin INTEGER DEFAULT 0)""")
            conn.execute("INSERT INTO person(name, is_admin) VALUES (?,?)", ("舊管理者", 1))
            conn.execute("INSERT INTO person(name, is_admin) VALUES (?,?)", ("舊一般人", 0))
            conn.commit()
        finally:
            conn.close()
        cfg = db.load_config()
        cfg["db_path"] = legacy_path
        db.save_config(cfg)
        try:
            db.init_db()  # 觸發 _migrate：對這個「已存在但缺 role/username 欄」的 db 補欄位+搬資料
            rows = {r["name"]: r["role"] for r in db.rows("SELECT name, role FROM person")}
            self.assertEqual(rows["舊管理者"], "admin")
            self.assertEqual(rows["舊一般人"], "user")
            # username 欄位跟唯一性 INDEX 也要補上，且不能因為這步而炸掉——這是這次
            # migration 裡唯一一個「欄位層級 UNIQUE 沒辦法用 ALTER TABLE 直接補」的
            # 特殊案例，用 CREATE UNIQUE INDEX 代替，這裡驗證兩邊都補了：
            # (1) 欄位存在、預設 NULL；(2) 唯一性真的有強制。
            with db.conn() as c:
                col_names = {r[1] for r in c.execute("PRAGMA table_info(person)").fetchall()}
                self.assertIn("username", col_names)
                c.execute("UPDATE person SET username=? WHERE name=?", ("legacyadmin", "舊管理者"))
                with self.assertRaises(sqlite3.IntegrityError):
                    c.execute("UPDATE person SET username=? WHERE name=?", ("legacyadmin", "舊一般人"))
        finally:
            cfg["db_path"] = os.path.join(self.tmp, "wbs.db")
            db.save_config(cfg)

    # ---- 多人模式：登入用中文姓名或英文帳號都要能找到同一個人 ----
    def test_login_lookup_matches_username_or_name(self):
        pid = db.run("INSERT INTO person(name, username) VALUES (?,?)", ("測試雙帳號者", "aliya_test"))
        by_username = db.one("SELECT * FROM person WHERE username=? OR name=?", ("aliya_test", "aliya_test"))
        by_name = db.one("SELECT * FROM person WHERE username=? OR name=?", ("測試雙帳號者", "測試雙帳號者"))
        self.assertEqual(by_username["id"], pid)
        self.assertEqual(by_name["id"], pid)

    # ---- 多人模式：登入失敗次數限制，鎖定後就算密碼對了也擋下 ----
    def test_login_rate_limit_locks_out_after_max_failures(self):
        name = "測試鎖定者_" + str(id(self))  # 每個測試獨立姓名，避免跟其他測試共用全域失敗計數互相汙染
        for _ in range(server.LOGIN_MAX_FAILURES):
            self.assertEqual(server._check_login_rate_limit(name), 0)
            server._record_login_failure(name)
        locked_min = server._check_login_rate_limit(name)
        self.assertGreater(locked_min, 0)
        server._clear_login_failures(name)
        self.assertEqual(server._check_login_rate_limit(name), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
