# -*- coding: utf-8 -*-
"""本機 HTTP 伺服器。只用 Python 標準函式庫，不需要 pip install 任何東西。

python -m app.server        （或直接跑 start.bat）
"""
import datetime as dt
import hashlib
import io
import json
import mimetypes
import os
import re
import secrets
import smtplib
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from email.message import EmailMessage
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, quote

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from app import core, db, docs_scan, ics_export, report, report_html, xls_export, schedule as sch
else:
    from . import core, db, docs_scan, ics_export, report, report_html, xls_export, schedule as sch

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

# 這支服務行程真正啟動的時間，只有你重開 start.bat 才會變——是「畫面上跑的是不是
# 今天改過的新程式碼」唯一可信的證據。version.json 的版號是手動維護的，你改了程式碼
# 卻忘記重啟服務，版號照樣顯示新的，會讓人誤以為新程式碼已經生效；啟動時間不會騙人。
_PROCESS_STARTED_AT = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

TASK_FIELDS = ("wbs_no", "parent_wbs", "level", "stage_code", "name", "owner",
               "planned_start", "planned_end", "actual_start", "actual_finish",
               "hard_deadline", "predecessors", "status", "progress", "note", "risk")
PROJ_FIELDS = ("code", "name", "owner", "start_date", "end_date",
               "docs_subdir", "color", "archived", "memo")
# baseline_start/baseline_end 刻意不開放 /api/tasks 寫入——只能透過 core.freeze_baseline()
# 改，這是「凍結後唯讀」的唯一防線；project.baseline_end 同理不在 PROJ_FIELDS 裡。

LINK_TOKEN_RE = re.compile(r"^(FS|SS|FF)?([+-]\d+)?$", re.I)


def _dl_headers(filename_ascii, filename_utf8, disposition="attachment"):
    """中文檔名不能直接塞進 HTTP header（send_header 內部是 latin-1 strict，塞了就
    UnicodeEncodeError、整個下載必炸）。RFC 5987 filename* 讓支援的瀏覽器顯示中文檔名，
    filename 留一份 ASCII 後備給不支援的用戶端。disposition="inline" 讓瀏覽器能就地
    開啟的檔案（PDF、圖片）直接在分頁預覽，不強制跳下載視窗；Word/Excel 瀏覽器反正
    不會就地渲染，帶 inline 也只是照樣下載，無害。"""
    return {"Content-Disposition":
            f"{disposition}; filename=\"{filename_ascii}\"; filename*=UTF-8''{quote(filename_utf8)}"}


def _validate_predecessors(val, project_id, exclude_id=None):
    """相依語法錯誤／指到不存在的項次，過去是靜默丟掉或降級成 FS+0——這裡改成直接擋掉，
    讓打錯的人看到訊息，而不是浮時默默算錯。"""
    if not val:
        return
    known = {r["wbs_no"] for r in db.rows(
        "SELECT wbs_no FROM task WHERE project_id=?" +
        (" AND id<>?" if exclude_id else ""),
        (project_id, exclude_id) if exclude_id else (project_id,))}
    for tok in val.replace("，", ",").replace(" ", "").split(","):
        if not tok:
            continue
        code, _, spec = tok.partition(":")
        if code not in known:
            raise ValueError(f"「{tok}」看不懂：找不到項次「{code}」")
        if spec and not LINK_TOKEN_RE.match(spec):
            raise ValueError(f"「{tok}」的關係寫法看不懂，要用 FS/SS/FF 加可選的 +N/-N，"
                             "例如 A3:FS+5")


MAX_UPLOAD_BYTES = 25 * 1024 * 1024

SESSION_DAYS = 14
PBKDF2_ITERS = 200_000
MIN_PASSWORD_LEN = 8
LOGIN_MAX_FAILURES = 5
LOGIN_LOCKOUT_MINUTES = 15

# 登入失敗次數記在記憶體裡，不用另外開資料表——重開機就重算是可接受的，這只是
# 防暴力猜密碼的緩解，不是正式的稽核紀錄。key 是姓名，value 是失敗時間的 list。
_login_failures = {}
_login_failures_lock = threading.Lock()


def _check_login_rate_limit(name):
    """回傳鎖定還剩幾分鐘（>0 代表被鎖），沒被鎖回傳 0。視窗內失敗次數到門檻
    才鎖，鎖定時間過了自動解鎖，不用人工介入解鎖。"""
    with _login_failures_lock:
        now = dt.datetime.now()
        window_start = now - dt.timedelta(minutes=LOGIN_LOCKOUT_MINUTES)
        fails = [t for t in _login_failures.get(name, []) if t > window_start]
        _login_failures[name] = fails
        if len(fails) < LOGIN_MAX_FAILURES:
            return 0
        remaining = fails[0] + dt.timedelta(minutes=LOGIN_LOCKOUT_MINUTES) - now
        return max(1, int(remaining.total_seconds() // 60) + 1)


def _record_login_failure(name):
    with _login_failures_lock:
        _login_failures.setdefault(name, []).append(dt.datetime.now())


def _clear_login_failures(name):
    with _login_failures_lock:
        _login_failures.pop(name, None)


def _auth_required():
    """只有「Server 模式」（config.json 的 bind_host 不是 127.0.0.1，代表對外聽、
    區網其他人打得到）才強制登入。桌機單機版預設 127.0.0.1，外面本來就連不到，
    不用逼使用者自己也要每天登入——這是刻意的設計，不是漏掉。"""
    cfg = db.load_config()
    return (cfg.get("bind_host") or "127.0.0.1") != "127.0.0.1"


def _hash_password(password, salt=None):
    salt = salt or secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), PBKDF2_ITERS)
    return h.hex(), salt


def _verify_password(password, salt, expected_hash):
    if not salt or not expected_hash:
        return False
    h, _ = _hash_password(password, salt)
    return secrets.compare_digest(h, expected_hash)


def _create_session(person_id):
    token = secrets.token_urlsafe(32)
    now = dt.datetime.now()
    db.run("INSERT INTO session(token, person_id, created_at, expires_at) VALUES (?,?,?,?)",
           (token, person_id, now.isoformat(timespec="seconds"),
            (now + dt.timedelta(days=SESSION_DAYS)).isoformat(timespec="seconds")))
    return token


def _session_person(token):
    """token 換回登入者的 person row，過期或不存在都回 None。過期的順便清掉，
    不用另外排一個清理排程——session 表本來就不大，每次查詢時順手清很夠用。"""
    if not token:
        return None
    now = dt.datetime.now().isoformat(timespec="seconds")
    db.run("DELETE FROM session WHERE expires_at < ?", (now,))
    row = db.one("SELECT person_id FROM session WHERE token=? AND expires_at >= ?", (token, now))
    if not row:
        return None
    return db.one("SELECT id, name, role FROM person WHERE id=?", (row["person_id"],))


def _parse_multipart(body, content_type):
    """手刻的最小 multipart/form-data 解析器——Python 3.13 拿掉了 cgi 模組，stdlib
    已經沒有現成的高階解析器可用。只解析上傳文件真正需要的東西：每個欄位的
    name／filename／內容位元組，其餘 multipart 規格的細節（巢狀 multipart、
    Content-Transfer-Encoding 這類）這個用途用不到，不處理。"""
    m = re.search(r"boundary=([^;]+)", content_type)
    if not m:
        return {}
    boundary = m.group(1).strip('"').encode("utf-8")
    delim = b"--" + boundary
    fields = {}
    for chunk in body.split(delim):
        chunk = chunk.strip(b"\r\n")
        if not chunk or chunk == b"--":
            continue
        if b"\r\n\r\n" not in chunk:
            continue
        head, data = chunk.split(b"\r\n\r\n", 1)
        data = data[:-2] if data.endswith(b"\r\n") else data
        headers = {}
        for line in head.split(b"\r\n"):
            if b":" in line:
                k, v = line.split(b":", 1)
                headers[k.strip().lower()] = v.strip()
        cd = headers.get(b"content-disposition", b"").decode("utf-8", "replace")
        nm = re.search(r'name="([^"]*)"', cd)
        if not nm:
            continue
        fnm = re.search(r'filename="([^"]*)"', cd)
        fields[nm.group(1)] = {
            "filename": fnm.group(1) if fnm else None,
            "data": data,
        }
    return fields


def _reorder_stages_cascade(project_id, order, old_code_by_id):
    """拖拉調整階段順序時，把代號（S01/S02/...）也跟著改到跟新順序一致。

    使用者發現的問題：seq（顯示順序）跟 code（S01 這種代號）本來各自獨立，拖一拖之後
    「S03 排在 S02 前面」——代號本身就是數字，天生會被當成順序看，兩者不一致就是一種
    誤導。與其加警語，不如讓代號永遠等於順序：拖完 S02 自然還是叫 S02。

    代號被 doc_req / doc_file / task 三張表當文字外鍵在用，且 (project_id, code) 有唯一
    索引，不能直接互換（會撞已存在的代號）——所以先全部改成跟任何真代號都不會撞的暫存
    代號，等這批都改完，再一次改成最終代號。"""
    n = len(order)
    new_code_by_id = {sid: f"S{i:02d}" for i, sid in enumerate(order, start=1)}
    changed = {sid: (old_code_by_id[sid], new_code_by_id[sid])
               for sid in order if old_code_by_id[sid] != new_code_by_id[sid]}
    if not changed:
        with db.conn() as c:
            for i, sid in enumerate(order, start=1):
                c.execute("UPDATE stage SET seq=? WHERE id=?", (i, sid))
        return 0
    with db.conn() as c:
        for sid, (old, _new) in changed.items():
            tmp = f"__TMP{sid}__"
            for tbl in ("task", "doc_req", "doc_file"):
                c.execute(f"UPDATE {tbl} SET stage_code=? WHERE project_id=? AND stage_code=?",
                          (tmp, project_id, old))
            c.execute("UPDATE stage SET code=? WHERE id=?", (tmp, sid))
        for sid, (_old, new) in changed.items():
            tmp = f"__TMP{sid}__"
            for tbl in ("task", "doc_req", "doc_file"):
                c.execute(f"UPDATE {tbl} SET stage_code=? WHERE project_id=? AND stage_code=?",
                          (new, project_id, tmp))
            c.execute("UPDATE stage SET code=? WHERE id=?", (new, sid))
        for i, sid in enumerate(order, start=1):
            c.execute("UPDATE stage SET seq=? WHERE id=?", (i, sid))
    return len(changed)


def _rename_wbs_cascade(project_id, old_no, new_no):
    """項次改名後，把其他任務「前置」欄裡指向舊編號的 token 同步改掉，不然那些相依關係
    會無聲斷掉——浮時默默膨脹、紅燈默默熄滅，是這套系統過去最陰的一個坑。"""
    if old_no == new_no:
        return 0
    n = 0
    for r in db.rows("SELECT id, predecessors FROM task WHERE project_id=? AND "
                     "predecessors LIKE ?", (project_id, f"%{old_no}%")):
        toks, changed = [], False
        for tok in (r["predecessors"] or "").split(","):
            code, sep, spec = tok.partition(":")
            if code == old_no:
                tok, changed = new_no + (sep + spec if sep else ""), True
            toks.append(tok)
        if changed:
            db.run("UPDATE task SET predecessors=? WHERE id=?", (",".join(toks), r["id"]))
            n += 1
    return n


def _rename_person_cascade(pid, old_name, new_name):
    """幫人員改名，同時把所有現有指派這個人的地方一起改過去——task/project/doc_req
    的 owner 欄位都是純文字比對（不是外鍵），改名不連動的話，改完名字那些欄位裡
    存的還是舊名字，變成查無此人的孤兒字串，比原本打錯字更糟。跟「刪除」刻意不
    同調：刪除本來就是要讓這個人從名單消失，不該去動歷史紀錄；改名是同一個人
    換了個寫法，兩者語意不同。"""
    with db.conn() as c:
        c.execute("UPDATE person SET name=? WHERE id=?", (new_name, pid))
        for table in ("task", "project", "doc_req"):
            c.execute(f"UPDATE {table} SET owner=? WHERE owner=?", (new_name, old_name))
        c.execute("UPDATE project_owner SET person_name=? WHERE person_name=?",
                 (new_name, old_name))


class Handler(BaseHTTPRequestHandler):
    server_version = "CL_WBS/1.0"

    def version_string(self):
        # 預設會把 Python 版本附加在 Server header 裡（例如 "CL_WBS/1.0 Python/3.9.25"），
        # 等於白送弱掃系統一個可以挑對應已知漏洞的版本號，只回自己的版本字串。
        return self.server_version

    def log_message(self, fmt, *args):
        pass

    # ------------------------------------------------------------ helpers
    def _send(self, code, body=b"", ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # 弱掃基本款安全 header——這個系統沒有第三方會嵌 iframe、沒有跨站資源
        # 需求，全部關掉是安全的預設值，不影響現有功能。
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code=200, extra=None):
        self._send(code, json.dumps(obj, ensure_ascii=False, default=str), extra=extra)

    def _err(self, msg, code=400):
        self._json({"error": msg}, code)

    def _require_admin(self):
        """回傳 True 代表已經擋下（呼叫端直接 return）。沒開登入強制（單機版）時
        一律放行——沒有「登入的人」這個概念可管，管理者限制無從談起。只認
        role='admin'，manager 不算——manager 能編所有人的工作項目，但清空資料/
        重設密碼/系統管理這類動作只有 admin 能做。"""
        if not _auth_required():
            return False
        actor = getattr(self, "_person", None)
        if not actor or actor["role"] != "admin":
            self._err("只有管理者能執行這個操作", 403)
            return True
        return False

    def _require_manager_or_admin(self):
        """跟 _require_admin 同樣的「回傳 True 代表已擋下」慣例，但門檻是
        manager 或 admin 都算過——給「系統設定/專案基本資料/人員管理」這類
        影響全體的動作用，一般使用者這類設定頁面在前端就看不到，這裡是
        後端最後一道防線，不能只靠前端藏起來。"""
        if not _auth_required():
            return False
        actor = getattr(self, "_person", None)
        if not actor or actor["role"] not in ("manager", "admin"):
            self._err("只有主管或管理者能執行這個操作", 403)
            return True
        return False

    def _can_edit_others(self, actor):
        """manager 或 admin 都能編輯別人負責的工作項目，一般使用者不行。"""
        return bool(actor) and actor["role"] in ("manager", "admin")

    def _can_edit_project(self, actor, project_id):
        """一般使用者原則上只能改自己名下的項目，但專案的主要／協同負責人是
        例外——副手既然掛名一起扛這個專案，就該能改這個專案裡的任何工作項目，
        不用先升級成 manager。"""
        if not actor or not project_id:
            return False
        row = db.one("SELECT owner FROM project WHERE id=?", (int(project_id),))
        if row and (row["owner"] or "") == actor["name"]:
            return True
        co = db.one("SELECT 1 FROM project_owner WHERE project_id=? AND person_name=?",
                    (int(project_id), actor["name"]))
        return bool(co)

    def _login_response(self, person):
        token = _create_session(person["id"])
        cookie = SimpleCookie()
        cookie["wbs_session"] = token
        cookie["wbs_session"]["path"] = "/"
        cookie["wbs_session"]["max-age"] = SESSION_DAYS * 86400
        cookie["wbs_session"]["httponly"] = True
        cookie["wbs_session"]["samesite"] = "Lax"
        return self._json(
            {"ok": True, "person": {"id": person["id"], "name": person["name"],
                                    "role": person["role"]}},
            extra={"Set-Cookie": cookie["wbs_session"].OutputString()})

    def _current_person(self):
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        c = SimpleCookie()
        c.load(raw)
        m = c.get("wbs_session")
        return _session_person(m.value) if m else None

    # 不強制登入時放行；強制時，這幾條路徑本來就是「登入本身」跟不含資料的靜態檔，
    # 需要在還沒登入前就能打到，其餘一律要求先登入。
    _AUTH_EXEMPT_PREFIXES = ("/api/auth/login", "/api/auth/register", "/api/auth/me",
                             "/api/auth/login-names", "/api/version",
                             "/api/admin/sync-status", "/api/admin/db-snapshot")
    # 上面兩條同步端點不是對人類開放的「不用登入」，是改用下面 _sync_auth_gate
    # 的機器對機器 token 驗證——一般使用者的 session cookie 在這兩條路上不管用。

    def _sync_auth_gate(self):
        """回傳 True 代表已經擋下。單機版（不強制登入）一律放行，跟其他端點的
        寬鬆行為一致；Server 模式下改認 X-Sync-Token，不是人員帳密——同步是
        機器對機器的事，不該綁在某個真人的登入密碼上，也不該讓每個能登入
        系統的人都能整份資料庫搬走。"""
        if not _auth_required():
            return False
        token = (db.load_config().get("sync_token") or "").strip()
        given = self.headers.get("X-Sync-Token", "")
        if not token or not secrets.compare_digest(given, token):
            self._err("同步 token 錯誤或未設定（Server 模式下同步端點不吃一般登入）", 403)
            return True
        return False

    def _auth_gate(self, path):
        if not path.startswith("/api/"):
            return None  # 靜態檔一律放行，前端本身要能載入才顯示得出登入畫面
        if not _auth_required():
            return None
        if any(path.startswith(p) for p in self._AUTH_EXEMPT_PREFIXES):
            return None
        person = self._current_person()
        if not person:
            self._err("尚未登入", 401)
            return True
        self._person = person
        return None

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}

    def _static(self, path):
        rel = path.lstrip("/") or "index.html"
        if rel.startswith("static/"):
            rel = rel[len("static/"):]
        full = os.path.normpath(os.path.join(STATIC, rel))
        if not full.startswith(STATIC) or not os.path.isfile(full):
            return self._send(404, "not found", "text/plain; charset=utf-8")
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript",):
            ctype += "; charset=utf-8"
        with open(full, "rb") as f:
            self._send(200, f.read(), ctype)

    # ------------------------------------------------------------ routing
    def do_GET(self):
        u = urlparse(self.path)
        p, q = u.path, parse_qs(u.query)
        try:
            if self._auth_gate(p):
                return None
            if not p.startswith("/api/"):
                return self._static(p)
            return self._api_get(p, q)
        except Exception as e:  # noqa: BLE001
            _say(f"[!] GET {p} 發生未預期錯誤：{type(e).__name__}: {e}")
            return self._err("系統發生錯誤，請稍後再試", 500)

    def do_POST(self):
        u = urlparse(self.path)
        ctype = self.headers.get("Content-Type", "")
        try:
            if self._auth_gate(u.path):
                return None
            if ctype.startswith("multipart/form-data"):
                return self._api_upload(u.path, ctype)
            if u.path == "/api/admin/db-snapshot" and ctype == "application/octet-stream":
                return self._api_db_snapshot_upload()
            return self._api_post(u.path, parse_qs(u.query), self._body())
        except Exception as e:  # noqa: BLE001
            _say(f"[!] POST {u.path} 發生未預期錯誤：{type(e).__name__}: {e}")
            return self._err("系統發生錯誤，請稍後再試", 500)

    def _api_upload(self, path, ctype):
        m = re.match(r"^/api/docreq/(\d+)/upload$", path)
        is_restore = path == "/api/admin/backups/upload-restore"
        if not m and not is_restore:
            return self._err("not found", 404)
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_UPLOAD_BYTES:
            # 超過上限：讀完丟掉才回應，不然瀏覽器那端會看到連線中斷而不是明確的錯誤訊息
            remain = n
            while remain > 0:
                remain -= len(self.rfile.read(min(remain, 65536)))
            return self._err(
                f"檔案超過 {MAX_UPLOAD_BYTES // (1024*1024)}MB 上限", 413)
        body = self.rfile.read(n)
        fields = _parse_multipart(body, ctype)
        f = fields.get("file")
        if not f or not f.get("filename"):
            return self._err("沒有收到檔案", 400)
        if is_restore:
            if self._require_admin():
                return None
            # 使用者自己電腦上挑一份 .db 檔上傳並直接還原——跟 sync_server.py 那條
            # 機器對機器的 db-snapshot 端點共用同一套「驗檔頭→先備份現況→原子換檔
            # →補 migration」邏輯，只是這裡走瀏覽器 multipart 上傳、admin 登入驗證，
            # 不是 sync_token。
            data = f["data"]
            if not data.startswith(b"SQLite format 3\x00"):
                return self._err("上傳的內容不是有效的 SQLite 資料庫檔，已拒絕覆蓋", 400)
            _backup_db(prefix="preupload")
            dst = db.db_path()
            tmp = dst + ".incoming"
            with open(tmp, "wb") as fh:
                fh.write(data)
            os.replace(tmp, dst)
            db.init_db()
            return self._json({"ok": True, "filename": f["filename"]})
        r = docs_scan.upload_doc(int(m.group(1)), f["filename"], f["data"])
        return self._json(r, 400 if r.get("error") else 200)

    def _api_db_snapshot_upload(self):
        # 單機／伺服器同步的接收端：對方整份 wbs.db 傳過來，直接覆蓋這台的正式資料庫。
        # 這是「單一權威版本」模式，不是即時雙向合併——覆蓋前一定先備份現有的，
        # 出事還救得回來；上傳內容驗證 SQLite 檔頭，不是隨便一包位元組就收。
        n = int(self.headers.get("Content-Length") or 0)
        if _auth_required():
            token = (db.load_config().get("sync_token") or "").strip()
            given = self.headers.get("X-Sync-Token", "")
            if not token or not secrets.compare_digest(given, token):
                remain = n
                while remain > 0:
                    remain -= len(self.rfile.read(min(remain, 65536)))
                return self._err("同步 token 錯誤或未設定（Server 模式下同步端點不吃一般登入）", 403)
        if n > MAX_UPLOAD_BYTES:
            remain = n
            while remain > 0:
                remain -= len(self.rfile.read(min(remain, 65536)))
            return self._err(f"檔案超過 {MAX_UPLOAD_BYTES // (1024*1024)}MB 上限", 413)
        data = self.rfile.read(n)
        if not data.startswith(b"SQLite format 3\x00"):
            return self._err("上傳的內容不是有效的 SQLite 資料庫檔，已拒絕覆蓋", 400)
        _backup_db(prefix="presync")  # 覆蓋前一定先留一份現況快照
        dst = db.db_path()
        tmp = dst + ".incoming"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, dst)  # 同一個檔案系統上是原子操作，不會留半寫的殘檔
        db.init_db()  # 換進來的資料庫可能是舊版本存的，立刻補齊欄位，不用等重啟服務
        return self._json(_db_fingerprint())

    do_PUT = do_POST

    def do_DELETE(self):
        u = urlparse(self.path)
        if self._auth_gate(u.path):
            return None
        m = re.match(r"^/api/tasks/(\d+)$", u.path)
        if m:
            tid = int(m.group(1))
            cur = db.one("SELECT project_id, owner FROM task WHERE id=?", (tid,))
            actor = getattr(self, "_person", None)
            if _auth_required() and actor and not self._can_edit_others(actor) and cur \
                    and (cur["owner"] or "") != actor["name"] \
                    and not self._can_edit_project(actor, cur["project_id"]):
                return self._err("這不是你負責的項目，只有主管/管理者/專案負責人能刪除別人負責的項目", 403)
            db.run("DELETE FROM task WHERE id=?", (tid,))
            if cur:
                core.roll_up_stage_dates(cur["project_id"])
            return self._json({"ok": True})
        m = re.match(r"^/api/projects/(\d+)$", u.path)
        if m:
            db.run("DELETE FROM project WHERE id=?", (int(m.group(1)),))
            return self._json({"ok": True})
        m = re.match(r"^/api/docreq/(\d+)$", u.path)
        if m:
            # 只刪這筆要求，掃到的實體檔案不動——刪掉的是「要交這份文件」的規則，
            # 不是文件本身；檔案下次掃描會變成孤兒檔（orphans），使用者看得到、不會憑空消失。
            db.run("DELETE FROM doc_req WHERE id=?", (int(m.group(1)),))
            return self._json({"ok": True})
        m = re.match(r"^/api/people/(\d+)$", u.path)
        if m:
            if self._require_admin():
                return None
            # 只刪名單裡的這一筆，不會動到任務/文件上已經填的負責人文字
            # （owner 存的是純文字，不是外鍵）——刪掉的人名還留在舊紀錄裡，只是下拉選單裡不再列出。
            db.run("DELETE FROM person WHERE id=?", (int(m.group(1)),))
            return self._json({"ok": True})
        m = re.match(r"^/api/docs/file/(\d+)$", u.path)
        if m:
            # 這個相反：刪的是實體檔案本身（例如手滑傳錯檔案），doc_req 的要求不動，
            # 刪完那個項目就變回「尚未在目錄中找到對應檔案」，可以重新上傳。
            r = docs_scan.delete_file(int(m.group(1)))
            return self._json(r, 400 if r.get("error") else 200)
        m = re.match(r"^/api/stage-template/doc/([^/]+)/([^/]+)$", u.path)
        if m:
            stage_code, code = m.group(1), m.group(2)
            tpl = db.load_stage_template()
            stage = next((s for s in tpl["stages"] if s["code"] == stage_code), None)
            if not stage:
                return self._err(f"範本裡找不到階段「{stage_code}」", 400)
            before = len(stage["docs"])
            stage["docs"] = [d for d in stage["docs"] if d["code"] != code]
            if len(stage["docs"]) == before:
                return self._err("範本裡找不到這份文件", 400)
            db.save_stage_template(tpl)
            return self._json({"ok": True})
        return self._err("not found", 404)

    # ------------------------------------------------------------ GET api
    def _api_get(self, p, q):
        if p == "/api/version":
            info = {"version": "?", "name": "", "started_at": _PROCESS_STARTED_AT,
                    "data_modified_at": None}
            try:
                with open(os.path.join(db.ROOT, "version.json"), "r", encoding="utf-8") as f:
                    info.update(json.load(f))
            except OSError:
                pass
            try:
                # 「上次啟動」只回答「程式碼有沒有換新」，不代表「有沒有人在動資料」——
                # 使用者要的是後者：db 檔最後一次真的被寫入（任何一筆存檔）的時間，
                # 用檔案的 mtime 就夠準，不用額外開一張表記時間戳。
                info["data_modified_at"] = dt.datetime.fromtimestamp(
                    os.path.getmtime(db.db_path())).strftime("%Y-%m-%d %H:%M:%S")
            except OSError:
                pass
            return self._json(info)
        if p == "/api/config":
            return self._json(db.load_config())
        if p == "/api/stage-template":
            return self._json(db.load_stage_template())
        if p == "/api/people":
            # 故意不 SELECT *——person 表現在多了 password_hash/password_salt，
            # 這兩欄絕對不能送到前端，就算是自己的雜湊值也不用讓瀏覽器拿到。
            return self._json(db.rows(
                "SELECT id, name, role, username, "
                "(password_hash IS NOT NULL) AS has_password "
                "FROM person ORDER BY name"))
        if p == "/api/admin/backups":
            if self._require_admin():
                return None
            bdir = os.path.join(os.path.dirname(db.db_path()), "backups")
            files = _sorted_backup_files(bdir)
            out = []
            for f in files:
                stamp = _backup_stamp(f)
                label = f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]} {stamp[9:11]}:{stamp[11:13]}:{stamp[13:15]}" if stamp else f
                kind = "清空前手動備份" if f.startswith("preclear_") else "開機自動備份"
                try:
                    size = os.path.getsize(os.path.join(bdir, f))
                except OSError:
                    size = 0
                out.append({"filename": f, "label": label, "kind": kind, "size": size})
            return self._json(out)
        m = re.match(r"^/api/admin/backups/([^/]+)/download$", p)
        if m:
            if self._require_admin():
                return None
            bdir = os.path.join(os.path.dirname(db.db_path()), "backups")
            # 檔名只能是白名單裡列出的（_sorted_backup_files 只回傳 bdir 底下真的
            # 存在的 .db 檔），不能拿網址裡的字串直接接路徑——擋路徑穿越。
            fname = m.group(1)
            if fname not in _sorted_backup_files(bdir):
                return self._err("找不到這份備份檔", 404)
            full = os.path.join(bdir, fname)
            with open(full, "rb") as f:
                return self._send(200, f.read(), "application/octet-stream",
                                  _dl_headers(fname, fname))
        if p == "/api/auth/me":
            person = self._current_person()
            return self._json({
                "auth_required": _auth_required(),
                "person": {"id": person["id"], "name": person["name"],
                          "role": person["role"]} if person else None,
            })
        if p == "/api/auth/login-names":
            # 給登入頁的姓名下拉選單用——刻意不用 /api/people（那支登入前打不到，
            # 而且會多帶 role/has_password 這些不需要曝光的欄位）。只回名字，
            # 內部小工具，曝光員工姓名清單給還沒登入的人是可接受的取捨。
            # 有設英文帳號的人，登入頁的建議清單顯示帳號（他們習慣打的就是這個）；
            # 沒設的人繼續顯示姓名（向下相容，沒有登入帳號一樣能用姓名登入）。
            return self._json([r["username"] or r["name"]
                               for r in db.rows("SELECT name, username FROM person ORDER BY name")])
        if p == "/api/projects":
            return self._json(core.projects(include_archived=True))
        if p == "/api/workload":
            return self._json(core.workload_summary())
        if p == "/api/today":
            return self._json(core.today_view(sch.d(q.get("date", [None])[0])))
        if p == "/api/week":
            return self._json(core.week_view(q.get("end", [None])[0]))
        if p == "/api/reports":
            return self._json(db.rows(
                "SELECT week_end, created_at FROM report ORDER BY week_end DESC"))

        m = re.match(r"^/api/report/([0-9\-]+)$", p)
        if m:
            r = db.one("SELECT * FROM report WHERE week_end=?", (m.group(1),))
            return self._json(r or {"error": "查無此週報"})

        m = re.match(r"^/api/docs/file/(\d+)$", p)
        if m:
            r = docs_scan.resolve_file(int(m.group(1)))
            if not r:
                return self._err("找不到這份文件（可能已被搬移或刪除，按「掃描文件目錄」更新）", 404)
            full, filename = r
            ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
            ascii_name = re.sub(r"[^\x20-\x7e]", "_", filename) or "file"
            with open(full, "rb") as f:
                return self._send(200, f.read(), ctype,
                                  _dl_headers(ascii_name, filename, "inline"))

        m = re.match(r"^/api/projects/(\d+)/state$", p)
        if m:
            st = core.project_state(int(m.group(1)))
            return self._json(st or {"error": "查無此專案"})

        m = re.match(r"^/api/projects/(\d+)/docs$", p)
        if m:
            pid = int(m.group(1))
            out = []
            for st in db.rows("SELECT * FROM stage WHERE project_id=? ORDER BY seq", (pid,)):
                data = docs_scan.stage_docs(pid, st["code"])
                out.append({**st, "gate": docs_scan.gate_status(pid, st["code"]),
                            "docs": data["items"], "orphans": data["orphans"]})
            return self._json({"stages": out, "docs_root": docs_scan.docs_root()})

        if p == "/api/export/ics":
            return self._send(200, ics_export.build(), "text/calendar; charset=utf-8",
                              {"Content-Disposition": 'attachment; filename="cl_wbs.ics"'})

        if p == "/api/export/csv":
            buf = io.StringIO()
            buf.write("﻿專案,項次,層級,階段,工作項目,負責人,開始,結束,"
                      "最晚完成,總浮時,剩餘浮時,狀態,進度,原因,備註\n")
            for st in core.all_states():
                if not st:
                    continue
                for t in st["tasks"]:
                    row = [st["project"]["name"], t["wbs_no"], t["level"],
                           t.get("stage_code") or "", t["name"], t.get("owner") or "",
                           t["planned_start"], t["planned_end"], t["lf"],
                           t["total_float"],
                           t["live_float"] if t["live_float"] is not None else "",
                           t["status"], t.get("progress") or 0, t["flag_reason"],
                           (t.get("note") or "").replace("\n", " ")]
                    buf.write(",".join('"' + str(x).replace('"', '""') + '"'
                                       for x in row) + "\n")
            return self._send(200, buf.getvalue(), "text/csv; charset=utf-8",
                              {"Content-Disposition": 'attachment; filename="wbs.csv"'})

        if p == "/api/export/my-tasks":
            # 給一般使用者的「備份自己的東西」——不是整份資料庫快照（那是管理者的
            # /api/admin/backup），是只含自己負責項目的 CSV 匯出，存起來當個人紀錄。
            # 單機版沒有登入概念，沒有「自己」可言，這條路只在 Server 模式、真的
            # 登入的情況下才有意義。
            actor = getattr(self, "_person", None)
            if _auth_required() and not actor:
                return self._err("尚未登入", 401)
            my_name = actor["name"] if actor else None
            buf = io.StringIO()
            buf.write("﻿專案,項次,層級,階段,工作項目,開始,結束,"
                      "最晚完成,總浮時,剩餘浮時,狀態,進度,原因,備註\n")
            for st in core.all_states():
                if not st:
                    continue
                for t in st["tasks"]:
                    if my_name is not None and (t.get("owner") or "") != my_name:
                        continue
                    row = [st["project"]["name"], t["wbs_no"], t["level"],
                           t.get("stage_code") or "", t["name"],
                           t["planned_start"], t["planned_end"], t["lf"],
                           t["total_float"],
                           t["live_float"] if t["live_float"] is not None else "",
                           t["status"], t.get("progress") or 0, t["flag_reason"],
                           (t.get("note") or "").replace("\n", " ")]
                    buf.write(",".join('"' + str(x).replace('"', '""') + '"'
                                       for x in row) + "\n")
            fname = f"我的工作項目_{my_name}.csv" if my_name else "my_tasks.csv"
            return self._send(200, buf.getvalue(), "text/csv; charset=utf-8",
                              _dl_headers("my_tasks.csv", fname))

        m = re.match(r"^/api/report/([0-9\-]+)/md$", p)
        if m:
            r = db.one("SELECT * FROM report WHERE week_end=?", (m.group(1),))
            md = r["content_md"] if r else report.generate(m.group(1))
            return self._send(200, md, "text/markdown; charset=utf-8",
                              _dl_headers(f"WBS_weekly_{m.group(1)}.md",
                                         f"WBS_週報_{m.group(1)}.md"))

        m = re.match(r"^/api/report/([0-9\-]+)/html$", p)
        if m:
            r = db.one("SELECT * FROM report WHERE week_end=?", (m.group(1),))
            html = (r["content_html"] if r and r.get("content_html")
                   else report.save(m.group(1))["content_html"])
            return self._send(200, html, "text/html; charset=utf-8",
                              _dl_headers(f"WBS_weekly_{m.group(1)}.html",
                                         f"WBS_週報_{m.group(1)}.html"))

        if p == "/api/export/xls":
            return self._send(200, xls_export.build(), "application/vnd.ms-excel",
                              _dl_headers("WBS_compare.xls", "WBS_對照表.xls"))

        m = re.match(r"^/api/projects/(\d+)/baseline$", p)
        if m:
            return self._json({"revisions": core.baseline_revisions(int(m.group(1)))})

        if p == "/api/admin/sync-status":
            if self._sync_auth_gate():
                return None
            return self._json(_db_fingerprint())

        if p == "/api/admin/db-snapshot":
            if self._sync_auth_gate():
                return None
            # 給單機／伺服器互相同步用：抓「目前這份 db 的完整內容」下載回去，不是
            # 匯出資料，是整個 SQLite 檔案本身。刻意先跑一次跟本機備份同一套邏輯
            # 產生時間戳快照再讀那份，不是直接讀正在被寫入的 db.wbs——避免讀到
            # SQLite 正在寫一半、還沒 commit 的頁面（狀態不一致）。
            bin_path, _, _ = _backup_db(prefix="syncsnap")
            if not bin_path:
                return self._err("沒有現有資料可同步", 400)
            with open(bin_path, "rb") as f:
                data = f.read()
            fp = _db_fingerprint()
            return self._send(200, data, "application/octet-stream",
                              {"Content-Disposition": 'attachment; filename="wbs_snapshot.db"',
                               "X-Db-Sha256": fp["sha256"], "X-Db-Mtime": fp["mtime"]})

        return self._err("not found", 404)

    # ------------------------------------------------------------ POST api
    def _api_post(self, p, q, body):
        if p == "/api/auth/login":
            name = (body.get("name") or "").strip()
            password = body.get("password") or ""
            if not name or not password:
                return self._err("帳號密碼不能空白", 400)
            locked_min = _check_login_rate_limit(name)
            if locked_min:
                return self._err(f"這個帳號登入失敗太多次，請等 {locked_min} 分鐘後再試", 429)
            # 輸入值可能是中文姓名，也可能是英文登入帳號（username）——兩邊都比對，
            # 對使用者來說「打哪個都能登入」，不用強迫他們改用英文帳號登入。
            person = db.one("SELECT * FROM person WHERE username=? OR name=?", (name, name))
            if not person:
                _record_login_failure(name)
                return self._err("查無此人，請先請管理者把你加進人員名單", 400)
            if not person["password_hash"]:
                # 這個人從來沒設過密碼——自助認領帳號：這次輸入的密碼直接變成他的密碼。
                # 系統裡目前一個 admin 都沒有時，第一個認領帳號的人自動變 admin，
                # 不然全新上線時沒有人有權限去指定第一個 admin，會卡死。
                if len(password) < MIN_PASSWORD_LEN:
                    return self._err(f"第一次登入要設定密碼，至少 {MIN_PASSWORD_LEN} 個字元", 400)
                h, salt = _hash_password(password)
                no_admin_yet = not db.one("SELECT id FROM person WHERE role='admin'")
                db.run("UPDATE person SET password_hash=?, password_salt=?, role=? WHERE id=?",
                       (h, salt, "admin" if no_admin_yet else person["role"], person["id"]))
                person = db.one("SELECT * FROM person WHERE id=?", (person["id"],))
            elif not _verify_password(password, person["password_salt"], person["password_hash"]):
                _record_login_failure(name)
                return self._err("密碼錯誤", 400)
            _clear_login_failures(name)
            return self._login_response(person)

        if p == "/api/auth/register":
            # 給還沒被 admin 加進 person 名單、AD 又還沒接上的人自己開帳號用——
            # 跟 login 的「自助認領」不一樣：這裡是連 person 這筆資料都不存在，
            # 一步建立姓名+密碼。刻意不讓兩個同名的人重複註冊（用 UNIQUE 擋），
            # 姓名重複時請對方改用登入頁（代表已經有人幫他建過名單）。
            name = (body.get("name") or "").strip()
            password = body.get("password") or ""
            if not name or not password:
                return self._err("帳號密碼不能空白", 400)
            if len(password) < MIN_PASSWORD_LEN:
                return self._err(f"密碼至少要 {MIN_PASSWORD_LEN} 個字元", 400)
            h, salt = _hash_password(password)
            no_admin_yet = not db.one("SELECT id FROM person WHERE role='admin'")
            try:
                pid = db.run(
                    "INSERT INTO person(name, password_hash, password_salt, role) VALUES (?,?,?,?)",
                    (name, h, salt, "admin" if no_admin_yet else "user"))
            except sqlite3.IntegrityError:
                return self._err(f"「{name}」已經存在，如果是你，請直接用登入", 400)
            person = db.one("SELECT * FROM person WHERE id=?", (pid,))
            return self._login_response(person)

        if p == "/api/auth/logout":
            raw = self.headers.get("Cookie")
            if raw:
                c = SimpleCookie()
                c.load(raw)
                m = c.get("wbs_session")
                if m:
                    db.run("DELETE FROM session WHERE token=?", (m.value,))
            cookie = SimpleCookie()
            cookie["wbs_session"] = ""
            cookie["wbs_session"]["path"] = "/"
            cookie["wbs_session"]["max-age"] = 0
            cookie["wbs_session"]["samesite"] = "Lax"
            return self._json({"ok": True}, extra={"Set-Cookie": cookie["wbs_session"].OutputString()})

        if p == "/api/auth/change-password":
            # 自己改自己的密碼，任何角色都能用（不用管理者），跟「重設密碼」不一樣：
            # 重設是管理者清空 hash 讓本人下次登入自助認領，這裡是已經登入的狀態下
            # 直接換一組新密碼，要先驗證舊密碼才准改，不是誰有 session 誰就能改。
            actor = getattr(self, "_person", None)
            if not actor:
                return self._err("尚未登入", 401)
            person = db.one("SELECT * FROM person WHERE id=?", (actor["id"],))
            old_pw = body.get("old_password") or ""
            new_pw = body.get("new_password") or ""
            if not _verify_password(old_pw, person["password_salt"], person["password_hash"]):
                return self._err("目前的密碼不對", 400)
            if len(new_pw) < MIN_PASSWORD_LEN:
                return self._err(f"新密碼至少要 {MIN_PASSWORD_LEN} 個字元", 400)
            h, salt = _hash_password(new_pw)
            db.run("UPDATE person SET password_hash=?, password_salt=? WHERE id=?",
                   (h, salt, actor["id"]))
            return self._json({"ok": True})

        if p == "/api/people/reset-password":
            # 忘記密碼沒有 IT 部門能重設——admin 把 hash 清空，等於幫他退回「還沒設過
            # 密碼」的狀態，本人下次登入輸入什麼就變成新密碼，不用 admin 知道新密碼是什麼。
            actor = getattr(self, "_person", None)
            if not actor or actor["role"] != "admin":
                return self._err("只有管理者能重設密碼", 403)
            pid = body.get("id")
            if not pid:
                return self._err("缺少 id", 400)
            db.run("UPDATE person SET password_hash=NULL, password_salt=NULL WHERE id=?", (int(pid),))
            return self._json({"ok": True})

        if p == "/api/people/rename":
            # 改的是「這個人叫什麼」（例如打錯字要修正），不是換一個人——所以刻意跟
            # 「刪除」不同調：刪除不動既有任務/文件上已經填的負責人字串（那是真的換
            # 掉一個人），改名要把所有現有指派這個人的地方一起改過去，不然改完名字
            # WBS 表上一堆工作項目的負責人會變成「查無此人」的孤兒字串，比原本的
            # 打錯字更糟。只有管理者能改，一般人打錯字自己看得到但改不動，避免亂改。
            if self._require_admin():
                return None
            pid = body.get("id")
            new_name = (body.get("name") or "").strip()
            if not pid or not new_name:
                return self._err("id、name 都要填", 400)
            old = db.one("SELECT name FROM person WHERE id=?", (int(pid),))
            if not old:
                return self._err("查無此人", 400)
            old_name = old["name"]
            if old_name == new_name:
                return self._json({"ok": True, "renamed": 0})
            try:
                _rename_person_cascade(int(pid), old_name, new_name)
            except sqlite3.IntegrityError:
                return self._err(f"「{new_name}」已經在名單裡了", 400)
            return self._json({"ok": True})

        if p == "/api/people/set-role":
            # 指定誰是主管/管理者，只有管理者能做這件事——三級權限：user（只能編
            # 自己負責的項目）／manager（能編所有人的工作項目，不能做系統管理動作）／
            # admin（manager 的權限 + 重設密碼、清空資料等系統管理動作）。
            actor = getattr(self, "_person", None)
            if not actor or actor["role"] != "admin":
                return self._err("只有管理者能指定角色", 403)
            pid = body.get("id")
            role = body.get("role")
            if role not in ("user", "manager", "admin"):
                return self._err("role 只能是 user / manager / admin", 400)
            if not pid:
                return self._err("缺少 id", 400)
            if int(pid) == actor["id"] and role != "admin":
                return self._err("不能把自己降級，請先指定另一個管理者再改自己", 400)
            db.run("UPDATE person SET role=? WHERE id=?", (role, int(pid)))
            return self._json({"ok": True})

        if p == "/api/people/set-username":
            # 給習慣用英文帳號登入的人設一個登入用的帳號，跟「負責人」顯示用的
            # 中文姓名分開——name 欄位不動，WBS/文件的擁有者比對完全不受影響。
            # 只有管理者能設，避免一般使用者亂改別人的登入方式。
            if self._require_admin():
                return None
            pid = body.get("id")
            if not pid:
                return self._err("缺少 id", 400)
            username = (body.get("username") or "").strip() or None
            try:
                db.run("UPDATE person SET username=? WHERE id=?", (username, int(pid)))
            except sqlite3.IntegrityError:
                return self._err(f"帳號「{username}」已經有別人在用了", 400)
            return self._json({"ok": True})

        if p == "/api/config":
            if self._require_manager_or_admin():
                return None
            cfg = db.load_config()
            cfg.update(body or {})
            db.save_config(cfg)
            return self._json(cfg)

        if p == "/api/notify/test-email":
            cfg = db.load_config()
            to_addr = (cfg.get("notify_email_to") or "").strip()
            if not to_addr:
                return self._err("尚未填寫收件信箱", 400)
            subject = "專案 WBS 追蹤 — 測試信"
            text = "這是一封測試信，收到就代表 Email 提醒設定成功。"
            if cfg.get("notify_email_mode") == "smtp":
                ok, err = _send_smtp_mail(subject, text, to_addr, cfg)
                return self._json({"ok": ok, "error": err})
            else:
                _send_outlook_mail(subject, text, to_addr)
                return self._json({"ok": True,
                    "error": None,
                    "note": "已交給 Outlook 寄送，Outlook 沒開/沒登入/自動化被鎖時會靜默失敗，"
                            "請自行檢查 Outlook 的寄件備份，或 %TEMP%\\clwbs_mail_error.log"})

        if p == "/api/people":
            if self._require_manager_or_admin():
                return None
            name = (body.get("name") or "").strip()
            if not name:
                return self._err("姓名不能空白", 400)
            try:
                pid = db.run("INSERT INTO person(name) VALUES (?)", (name,))
            except sqlite3.IntegrityError:
                return self._err(f"「{name}」已經在名單裡了", 400)
            return self._json({"ok": True, "id": pid})

        if p == "/api/admin/clear-data":
            if self._require_admin():
                return None
            # 上 public repo 前的安全動作：先備份一份真實資料，再把工作中的資料庫清空、
            # 重新種上通用示範資料——確認畫面上不會再看到任何真實內容，才推程式碼出去。
            # 一定先備份才清空，順序不能反，不然沒東西可以還原。BIN 一定會存（還原要
            # 靠它)；文字版（SQL dump）是否額外輸出由使用者勾選決定，方便肉眼核對。
            bin_path, text_path, _ = _backup_db(prefix="preclear", also_text=bool(body.get("also_text")))
            if not bin_path:
                return self._err("沒有現有資料可備份，或備份失敗，已取消清空", 400)
            with db.conn() as c:
                c.execute("DELETE FROM project")  # CASCADE 一併清掉 stage/task/doc_req/doc_file
                c.execute("DELETE FROM person")
            if __package__ in (None, ""):
                from app import seed
            else:
                from . import seed
            seed.run()
            return self._json({
                "ok": True, "backup": os.path.basename(bin_path),
                "backup_text": os.path.basename(text_path) if text_path else None,
            })

        if p == "/api/stage-template/doc":
            if self._require_manager_or_admin():
                return None
            # 改的是共用範本本身（stage_template.json），影響「以後每個新專案」預設
            # 拿到什麼文件清單——跟改某個專案自己的 doc_req 是兩件事，不要混在一起。
            tpl = db.load_stage_template()
            stage_code = (body.get("stage_code") or "").strip()
            code = (body.get("code") or "").strip()
            name = (body.get("name") or "").strip()
            if not (stage_code and code and name):
                return self._err("stage_code、code、name 都要填", 400)
            stage = next((s for s in tpl["stages"] if s["code"] == stage_code), None)
            if not stage:
                return self._err(f"範本裡找不到階段「{stage_code}」", 400)
            if any(d["code"] == code for d in stage["docs"]):
                return self._err(f"這個階段的範本已經有代碼「{code}」了", 400)
            stage["docs"].append({
                "code": code, "name": name,
                "required": bool(body.get("required", True)), "note": body.get("note") or "",
            })
            db.save_stage_template(tpl)
            return self._json({"ok": True})

        if p == "/api/stage-template/doc/edit":
            if self._require_manager_or_admin():
                return None
            tpl = db.load_stage_template()
            stage_code = (body.get("stage_code") or "").strip()
            code = (body.get("code") or "").strip()
            stage = next((s for s in tpl["stages"] if s["code"] == stage_code), None)
            doc = next((d for d in stage["docs"] if d["code"] == code), None) if stage else None
            if not doc:
                return self._err("範本裡找不到這份文件", 400)
            if "name" in body and (body["name"] or "").strip():
                doc["name"] = body["name"].strip()
            if "required" in body:
                doc["required"] = bool(body["required"])
            if "note" in body:
                doc["note"] = body["note"] or ""
            if "new_code" in body and (body["new_code"] or "").strip():
                new_code = body["new_code"].strip()
                if new_code != code:
                    if any(d["code"] == new_code for d in stage["docs"]):
                        return self._err(f"這個階段的範本已經有代碼「{new_code}」了", 400)
                    doc["code"] = new_code
            db.save_stage_template(tpl)
            return self._json({"ok": True})

        if p == "/api/admin/backup":
            # 存的是整份資料庫（全部專案、全部人的資料），只有管理者能做——一般
            # 使用者要的「備份自己的東西」走下面的 /api/export/my-tasks（匯出成
            # CSV，只有自己負責的項目），語意不一樣：這支是系統層級的完整快照，
            # 那支是個人層級的資料匯出，不是同一件事的兩種權限，是兩件不同的事。
            if self._require_admin():
                return None
            also_enc = bool(body.get("also_encrypted"))
            password = body.get("password") or ""
            if also_enc and not password:
                return self._err("要存密檔備份，密碼不能空白", 400)
            bin_path, text_path, enc_path = _backup_db(
                prefix="manual", also_text=bool(body.get("also_text")),
                also_encrypted=also_enc, password=password)
            if not bin_path:
                return self._err("沒有現有資料可備份", 400)
            return self._json({
                "ok": True, "backup": os.path.basename(bin_path),
                "backup_text": os.path.basename(text_path) if text_path else None,
                "backup_enc": os.path.basename(enc_path) if enc_path else None,
            })

        if p == "/api/admin/restore-backup":
            if self._require_admin():
                return None
            # 不能用檔名字串排序——備份檔名有兩種前綴（開機自動備份的 wbs_、清空前
            # 手動備份的 preclear_），字母 w 排在 p 後面，純排檔名會選到「字母比較
            # 大」但時間比較舊的那份。也不能用檔案的作業系統修改時間排——
            # shutil.copy2 會把來源檔（正式資料庫）的 mtime 一起複製過去，備份檔的
            # mtime 顯示的是「資料庫最後一次被寫入的時間」，不是「這次備份的時間」，
            # 兩者常常對不上（2026-08-29 實測就選錯過）。唯一可信的是檔名裡自己寫的
            # 時間戳，用正規表達式抓出來比大小，不管前綴是什麼。
            bdir = os.path.join(os.path.dirname(db.db_path()), "backups")
            files = _sorted_backup_files(bdir)
            if not files:
                return self._err("找不到任何備份檔", 400)
            # 可以指定還原哪一份（畫面上的下拉選單）；不指定就沿用舊行為還原最新一份。
            # 檔名只能是 basename、必須在備份清單裡才准用——擋路徑穿越，不能拿使用者
            # 傳來的字串直接接路徑去讀任意檔案。
            wanted = (body.get("filename") or "").strip()
            if wanted:
                if wanted not in files:
                    return self._err("指定的備份檔不存在", 400)
                target = wanted
            else:
                target = files[0]
            latest = os.path.join(bdir, target)
            import shutil
            shutil.copy2(latest, db.db_path())
            # 還原完立刻重跑一次 migration，不要等下次重啟服務才補欄位——還原的備份
            # 檔通常是舊版本存的，缺新版程式碼才加的欄位；不馬上補的話，還原後、
            # 重啟前這段時間，只要程式一碰到新欄位就會直接噴 SQLite「no such
            # column」錯誤，使用者會以為是還原失敗，其實只是欄位還沒補上。
            db.init_db()
            return self._json({"ok": True, "restored": target})

        if p == "/api/projects":
            if self._require_manager_or_admin():
                return None
            pid = body.get("id")
            fields = {k: body.get(k) for k in PROJ_FIELDS if k in body}
            for k in ("start_date", "end_date"):
                if fields.get(k) and sch.d(fields[k]) is None:
                    return self._err(f"「{fields[k]}」不是有效日期，{k} 沒有存入", 400)
            if pid:
                if fields:
                    sets = ",".join(f"{k}=?" for k in fields)
                    try:
                        db.run(f"UPDATE project SET {sets} WHERE id=?",
                               (*fields.values(), int(pid)))
                    except sqlite3.IntegrityError:
                        return self._err(f"專案代號「{fields.get('code')}」已經有別的專案在用，改別的代號", 400)
            else:
                cols = ",".join(fields)
                qs = ",".join("?" * len(fields))
                pid = db.run(f"INSERT INTO project({cols}) VALUES ({qs})",
                             tuple(fields.values()))
                core.apply_stage_template(pid)
            if "co_owners" in body:
                # 全量覆蓋這個專案的協同負責人清單，不是增量新增——前端每次都送完整
                # 清單，簡單、不用另外做「新增一個/移除一個」兩支 API。
                names = [n.strip() for n in (body.get("co_owners") or []) if (n or "").strip()]
                with db.conn() as c:
                    c.execute("DELETE FROM project_owner WHERE project_id=?", (int(pid),))
                    c.executemany("INSERT INTO project_owner(project_id, person_name) VALUES (?,?)",
                                 [(int(pid), n) for n in names])
            return self._json({"ok": True, "id": pid})

        m = re.match(r"^/api/projects/(\d+)/scan$", p)
        if m:
            return self._json(docs_scan.scan_project(int(m.group(1))))

        m = re.match(r"^/api/projects/(\d+)/build-folders$", p)
        if m:
            return self._json(docs_scan.build_folders(int(m.group(1))))

        m = re.match(r"^/api/projects/(\d+)/apply-template$", p)
        if m:
            return self._json(core.apply_stage_template(
                int(m.group(1)), bool(body.get("overwrite"))))

        if p == "/api/tasks":
            items = body if isinstance(body, list) else [body]
            # 先驗證全部、再寫入——一個 batch 裡有一項打錯，不該有一半已經寫進 DB
            # 才發現最後一項壞掉。單筆編輯（今日/WBS 頁的常態）本來就只有一項。
            prepared = []
            for it in items:
                fields = {k: it.get(k) for k in TASK_FIELDS if k in it}
                tid = int(it["id"]) if it.get("id") else None
                cur = db.one("SELECT project_id, wbs_no, status, actual_finish, owner FROM task "
                            "WHERE id=?", (tid,)) if tid else None
                project_id = cur["project_id"] if cur else it.get("project_id")
                if not project_id:
                    return self._err("project_id 缺漏", 400)
                # Server 模式、一般使用者（非 manager/admin）：只能改自己負責的既有
                # 項目，新建的項目也只能掛在自己名下——不是資料庫層級的隔離，是
                # 「登入的人只能動自己那份」這個約定的唯一防線，manager/admin 不受此限。
                actor = getattr(self, "_person", None)
                if _auth_required() and actor and not self._can_edit_others(actor) \
                        and not self._can_edit_project(actor, project_id):
                    if cur and (cur["owner"] or "") != actor["name"]:
                        return self._err(
                            f"「{cur['wbs_no']}」的負責人不是你，只有主管/管理者/專案負責人能改別人負責的項目", 403)
                    if not cur and fields.get("owner") not in (None, "", actor["name"]):
                        return self._err("不能新增指派給別人的工作項目，負責人要是你自己", 403)
                try:
                    for k in ("planned_start", "planned_end", "hard_deadline",
                             "actual_start", "actual_finish"):
                        if k in fields and fields[k] and sch.d(fields[k]) is None:
                            raise ValueError(f"「{fields[k]}」不是有效日期，{k} 沒有存入")
                    if "predecessors" in fields:
                        _validate_predecessors(fields["predecessors"], project_id,
                                               exclude_id=tid)
                except ValueError as e:
                    return self._err(str(e), 400)
                # 標「已完成」且沒人手動填實際完成日 → 自動蓋今天，事後仍可在格子改。
                # 這是唯一每天必用的動作，不能加彈窗——錯了要看得見才會被改，不是靠多一步擋。
                if (fields.get("status") == "已完成" and not fields.get("actual_finish")
                        and cur and not cur.get("actual_finish")):
                    fields["actual_finish"] = dt.date.today().isoformat()
                prepared.append((tid, cur, project_id, fields))

            out = []
            for tid, cur, project_id, fields in prepared:
                if tid:
                    if not fields:
                        out.append(tid)
                        continue
                    sets = ",".join(f"{k}=?" for k in fields)
                    try:
                        db.run(f"UPDATE task SET {sets} WHERE id=?",
                               (*fields.values(), tid))
                    except sqlite3.IntegrityError:
                        return self._err(f"項次「{fields.get('wbs_no')}」已經有別的工作項目"
                                         "在用，改別的編號", 400)
                    if "wbs_no" in fields and cur and fields["wbs_no"] != cur["wbs_no"]:
                        _rename_wbs_cascade(project_id, cur["wbs_no"], fields["wbs_no"])
                    out.append(tid)
                else:
                    fields["project_id"] = project_id
                    cols = ",".join(fields)
                    qs = ",".join("?" * len(fields))
                    try:
                        out.append(db.run(
                            f"INSERT INTO task({cols}) VALUES ({qs})", tuple(fields.values())))
                    except sqlite3.IntegrityError:
                        return self._err(f"項次「{fields.get('wbs_no')}」已經存在", 400)
            # 階段圈圈底下的日期是「底下任務日期最早～最晚」算出來的，任務異動完
            # 要重算，不然畫面停在舊數字——批次可能跨好幾個專案，逐一重算，不猜只有一個。
            for pid in {project_id for _, _, project_id, _ in prepared}:
                core.roll_up_stage_dates(pid)
            return self._json({"ok": True, "ids": out})

        if p == "/api/docreq":
            project_id = body.get("project_id")
            stage_code = (body.get("stage_code") or "").strip()
            doc_code = (body.get("doc_code") or "").strip()
            name = (body.get("name") or "").strip()
            if not (project_id and stage_code and doc_code and name):
                return self._err("project_id、stage_code、doc_code、name 都要填", 400)
            try:
                rid = db.run(
                    "INSERT INTO doc_req(project_id,stage_code,doc_code,name,required,note) "
                    "VALUES (?,?,?,?,?,?)",
                    (int(project_id), stage_code, doc_code, name,
                     1 if body.get("required", True) else 0, body.get("note")))
            except sqlite3.IntegrityError:
                return self._err(f"這個階段已經有代碼「{doc_code}」的文件項目了", 400)
            return self._json({"ok": True, "id": rid})

        if p == "/api/projects/reorder":
            order = body.get("order") or []
            ids = {r["id"] for r in db.rows("SELECT id FROM project")}
            if set(order) != ids:
                return self._err("順序清單跟目前的專案對不起來，沒有更新", 400)
            with db.conn() as c:
                for i, pid in enumerate(order, start=1):
                    c.execute("UPDATE project SET sort_order=? WHERE id=?", (i, pid))
            return self._json({"ok": True})

        m = re.match(r"^/api/projects/(\d+)/stages/reorder$", p)
        if m:
            pid = int(m.group(1))
            order = body.get("order") or []
            rows = db.rows("SELECT id, code FROM stage WHERE project_id=?", (pid,))
            by_id = {r["id"]: r["code"] for r in rows}
            if set(order) != set(by_id):
                return self._err("順序清單跟這個專案的階段對不起來，沒有更新", 400)
            n = _reorder_stages_cascade(pid, order, by_id)
            return self._json({"ok": True, "renamed": n})

        m = re.match(r"^/api/projects/(\d+)/baseline$", p)
        if m:
            r = core.freeze_baseline(int(m.group(1)), body.get("reason"))
            return self._json(r, 400 if r.get("error") else 200)

        m = re.match(r"^/api/tasks/(\d+)/checkin$", p)
        if m:
            tid = int(m.group(1))
            day = body.get("log_date") or dt.date.today().isoformat()
            db.run("INSERT INTO checkin(task_id,log_date,progress,status,note,created_at) "
                   "VALUES (?,?,?,?,?,?)",
                   (tid, day, body.get("progress"), body.get("status"),
                    body.get("note"), dt.datetime.now().isoformat(timespec="seconds")))
            sets, vals = [], []
            if body.get("progress") is not None:
                sets.append("progress=?"); vals.append(int(body["progress"]))
            if body.get("status"):
                sets.append("status=?"); vals.append(body["status"])
                # 今日頁「記錄」是每天最常用的路徑——標完成同樣要蓋實際完成日，
                # 不然只有 WBS 表格改狀態才會蓋到，checkin 這條最常走的路反而漏掉。
                if body["status"] == "已完成":
                    cur = db.one("SELECT actual_finish FROM task WHERE id=?", (tid,))
                    if cur and not cur.get("actual_finish"):
                        sets.append("actual_finish=?")
                        vals.append(body.get("actual_finish") or dt.date.today().isoformat())
            if sets:
                db.run(f"UPDATE task SET {','.join(sets)} WHERE id=?", (*vals, tid))
            return self._json({"ok": True})

        m = re.match(r"^/api/stages/(\d+)$", p)
        if m:
            sid = int(m.group(1))
            fields = {k: body[k] for k in ("status", "planned_start", "planned_end",
                                           "exit_gate", "name", "purpose") if k in body}
            if fields:
                st = db.one("SELECT * FROM stage WHERE id=?", (sid,))
                if body.get("status") == "已完成" and not body.get("force"):
                    g = docs_scan.gate_status(st["project_id"], st["code"])
                    if not g["passed"]:
                        return self._json({"blocked": True, "gate": g}, 409)
                sets = ",".join(f"{k}=?" for k in fields)
                db.run(f"UPDATE stage SET {sets} WHERE id=?", (*fields.values(), sid))
            return self._json({"ok": True})

        m = re.match(r"^/api/docreq/(\d+)$", p)
        if m:
            rid = int(m.group(1))
            fields = {k: body[k] for k in ("required", "manual_done", "manual_note",
                                           "name", "note", "progress", "owner") if k in body}
            if "progress" in fields and int(fields["progress"]) not in (0, 50, 100):
                return self._err("進度只能是 0、50、100", 400)
            if fields:
                sets = ",".join(f"{k}=?" for k in fields)
                db.run(f"UPDATE doc_req SET {sets} WHERE id=?", (*fields.values(), rid))
            return self._json({"ok": True})

        if p == "/api/report":
            return self._json(report.save(body.get("week_end")))

        return self._err("not found", 404)


def _say(msg=""):
    """Windows 主控台可能不是 UTF-8，印不出來也不該讓服務掛掉。"""
    try:
        print(msg)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "ascii"
        print(msg.encode(enc, "replace").decode(enc, "replace"))


def _xor_obfuscate(data, password):
    """用密碼混淆位元組，不是正式加密（標準函式庫沒有 AES 這類工具，這是能做到
    最好的程度）——擋得住用文字編輯器/DB 瀏覽器不小心打開看到內容，擋不住懂程式
    刻意破解的人。XOR 是對稱運算，同一個函式可以拿來解密：用同一組密碼再跑一次
    就還原了。金鑰用 hashlib（標準函式庫）算 SHA-256 出來，不用另外裝套件。"""
    import hashlib
    key = hashlib.sha256(password.encode("utf-8")).digest()
    return bytes(b ^ key[i % len(key)] for i, b in enumerate(data))


def _backup_stamp(filename):
    m = re.search(r"(\d{8}_\d{6})\.db$", filename)
    return m.group(1) if m else ""


def _sorted_backup_files(bdir):
    """備份檔名（新到舊）。跟 restore-backup 共用同一份排序邏輯——只有一處判斷
    「哪個新哪個舊」，不要分開各寫一次然後兩邊標準不小心兜不起來。"""
    if not os.path.isdir(bdir):
        return []
    return sorted((f for f in os.listdir(bdir) if f.endswith(".db")),
                  key=_backup_stamp, reverse=True)


def _backup_db(prefix="wbs", also_text=False, also_encrypted=False, password=None):
    """備份一份 wbs.db——這是全系統唯一的資料來源，沒有任何機制救得回誤刪或寫壞的
    檔案。只留最近 14 份 BIN 備份，不用使用者自己管理。db 還不存在（第一次啟動）就
    跳過，沒東西可備份。回傳 (bin_path, text_path, enc_path)；沒產生的是 None。
    prefix 用來區分「開機自動備份」跟「上 public 前手動清空」這種不同來源的備份，
    方便還原時看檔名就知道是哪一種。

    BIN（.db 原始檔）是唯一拿來「還原」用的格式，永遠都會存，不能只存其他格式——
    文字版（.sql，用 sqlite3 內建的 iterdump 產生 SQL 文字傾印）方便肉眼核對內容
    有沒有殘留敏感字；密檔版（.enc，XOR 混淆）方便要把備份存到不夠信任的地方時
    用，兩者都是額外輸出，純輔助用途。"""
    src = db.db_path()
    if not os.path.isfile(src):
        return None, None, None
    bdir = os.path.join(os.path.dirname(src), "backups")
    os.makedirs(bdir, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    dst = os.path.join(bdir, f"{prefix}_{stamp}.db")
    text_dst = None
    enc_dst = None
    try:
        import shutil
        shutil.copy2(src, dst)
        # 只留「這個 prefix」自己最近 14 份——原本寫死只清 wbs_（開機自動備份），
        # manual_/preclear_ 這些其他 prefix 從來沒被清過；新增 syncsnap_/presync_
        # 之後（每次 push/pull 各多兩個檔）不修就會無上限累積，所以改成每個
        # prefix 各自輪替，不只清 wbs_ 這一種。
        keep = sorted(
            (f for f in os.listdir(bdir) if f.startswith(f"{prefix}_") and f.endswith(".db")),
            reverse=True)
        for old in keep[14:]:
            os.remove(os.path.join(bdir, old))
        if also_text:
            text_dst = os.path.join(bdir, f"{prefix}_{stamp}.sql")
            conn = sqlite3.connect(dst)
            try:
                with open(text_dst, "w", encoding="utf-8") as f:
                    for line in conn.iterdump():
                        f.write(f"{line}\n")
            finally:
                conn.close()
        if also_encrypted and password:
            enc_dst = os.path.join(bdir, f"{prefix}_{stamp}.enc")
            with open(dst, "rb") as f:
                raw = f.read()
            with open(enc_dst, "wb") as f:
                f.write(_xor_obfuscate(raw, password))
        return dst, text_dst, enc_dst
    except OSError as e:  # noqa: BLE001
        _say(f"[!] 備份失敗：{e}")
        return None, None, None


def _db_fingerprint():
    """給單機／伺服器同步判斷「兩邊是不是同一份資料」用：整份 db 檔案的 sha256 加
    上檔案本身的 mtime。用 sha256 而不是 mtime 比較，是因為 mtime 在不同機器間本來
    就對不齊時區/時鐘飄移，內容雜湊才是唯一可信的「內容有沒有變」判斷依據；mtime
    只是輔助顯示用，不拿來做判斷。"""
    src = db.db_path()
    if not os.path.isfile(src):
        return {"sha256": None, "mtime": None, "size": 0}
    h = hashlib.sha256()
    with open(src, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    st = os.stat(src)
    return {"sha256": h.hexdigest(),
            "mtime": dt.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            "size": st.st_size}


def _send_windows_toast(title, message):
    """跳一則 Windows 系統氣泡通知——只呼叫 Windows 內建的 .NET 功能，不寄信、
    不連外部服務、不用另外安裝任何模組。訊息用暫存 .ps1 檔（帶 UTF-8 BOM）傳給
    PowerShell，不是塞進命令列參數——中文字直接塞命令列在這台機器上的殼層組合
    容易被系統內碼重新編碼成亂碼，寫成檔案才穩。這個通知只在本程式還開著時
    才可能觸發，程式沒開著就不會有任何提醒（見 _notify_loop 的說明）。"""
    script = f"""
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$notify = New-Object System.Windows.Forms.NotifyIcon
$notify.Icon = [System.Drawing.SystemIcons]::Information
$notify.Visible = $true
$notify.ShowBalloonTip(10000, "{title}", "{message}", [System.Windows.Forms.ToolTipIcon]::Info)
Start-Sleep -Seconds 11
$notify.Dispose()
"""
    try:
        with tempfile.NamedTemporaryFile(
                mode="w", suffix=".ps1", delete=False, encoding="utf-8-sig") as f:
            f.write(script)
            path = f.name
        subprocess.Popen(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-File", path],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except OSError as e:  # noqa: BLE001
        _say(f"[!] 通知提醒失敗（不影響系統其他功能）：{e}")


def _send_outlook_mail(subject, body, to_addr):
    """借用這台機器上已經登入的 Outlook（COM 自動化）寄一封信——刻意不做這件事：
    不問使用者要 SMTP 帳號密碼、不寄到隨便一個伺服器。用的是本機 Outlook 既有
    的登入身分去「送」信，收件地址則是使用者在設定頁自己填的固定地址（不填就
    不寄）。Outlook 沒開、沒登入、或 COM 自動化被公司政策鎖住時會直接失敗，
    這裡吞掉例外只記一行 log，不能讓寄信失敗拖累其他提醒功能。"""
    escaped_subject = subject.replace("'", "''")
    escaped_body = body.replace("'", "''")
    escaped_to = to_addr.replace("'", "''")
    script = f"""
try {{
  $ol = New-Object -ComObject Outlook.Application
  $mail = $ol.CreateItem(0)
  $mail.To = '{escaped_to}'
  $mail.Subject = '{escaped_subject}'
  $mail.Body = '{escaped_body}'
  $mail.Send()
}} catch {{
  $_.Exception.Message | Out-File -FilePath "$env:TEMP\\clwbs_mail_error.log" -Encoding utf8
}}
"""
    try:
        with tempfile.NamedTemporaryFile(
                mode="w", suffix=".ps1", delete=False, encoding="utf-8-sig") as f:
            f.write(script)
            path = f.name
        subprocess.Popen(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-File", path],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except OSError as e:  # noqa: BLE001
        _say(f"[!] Email 提醒失敗（不影響系統其他功能）：{e}")


def _send_smtp_mail(subject, body, to_addr, cfg):
    """給沒有裝/沒有登入 Outlook 的機器用：自己填 SMTP 帳號密碼寄信，固定走 SSL
    （465 埠），不支援 STARTTLS——夠用就好，不多加一種模式增加維護負擔。帳號密碼
    明碼存在本機 config.json（使用者已確認接受這個風險）。回傳 (ok, error_message)，
    讓「測試寄送」按鈕可以直接把失敗原因顯示在畫面上，不用去翻 log 檔；背景提醒
    迴圈呼叫時則只在失敗時記一行 log，不拖累其他功能。"""
    host = (cfg.get("smtp_host") or "").strip()
    user = (cfg.get("smtp_user") or "").strip()
    pw = cfg.get("smtp_pass") or ""
    port = int(cfg.get("smtp_port") or 465)
    if not (host and user and pw):
        return False, "SMTP 設定不完整（伺服器／帳號／密碼缺一不可）"
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to_addr
    msg.set_content(body)
    try:
        with smtplib.SMTP_SSL(host, port, context=ssl.create_default_context(), timeout=15) as s:
            s.login(user, pw)
            s.send_message(msg)
        return True, None
    except (OSError, smtplib.SMTPException) as e:  # noqa: BLE001
        return False, str(e)


def _notify_loop():
    """背景執行緒，程式開著的時候每分鐘檢查一次：今天有沒有到通知提醒時間、
    今天還沒提醒過、而且真的有逾期/不能拖/階段缺件——三個條件都成立才跳通知，
    不是每天固定吵一次。程式關掉這個執行緒就跟著結束，沒有殘留背景行程。"""
    last_notified = None
    while True:
        try:
            cfg = db.load_config()
            if cfg.get("notify_enabled"):
                now = dt.datetime.now()
                notify_time = cfg.get("notify_time", "09:00")
                if now.strftime("%H:%M") >= notify_time and last_notified != now.date():
                    view = core.today_view()
                    n_overdue, n_must, n_gates = len(view["overdue"]), len(view["must"]), len(view["blocked_gates"])
                    if n_overdue or n_must or n_gates:
                        parts = []
                        if n_overdue:
                            parts.append(f"{n_overdue} 項已逾期")
                        if n_must:
                            parts.append(f"{n_must} 項今天不能拖")
                        if n_gates:
                            parts.append(f"{n_gates} 個階段缺件卡住")
                        summary = "、".join(parts)
                        _send_windows_toast("專案 WBS 追蹤 — 今日提醒", summary)
                        to_addr = (cfg.get("notify_email_to") or "").strip()
                        if cfg.get("notify_email_enabled") and to_addr:
                            body = summary + "\n\n開啟系統查看：http://127.0.0.1:" + str(cfg.get("port", 8765)) + "/"
                            if cfg.get("notify_email_mode") == "smtp":
                                ok, err = _send_smtp_mail("專案 WBS 追蹤 — 今日提醒", body, to_addr, cfg)
                                if not ok:
                                    _say(f"[!] Email 提醒失敗（SMTP，不影響系統其他功能）：{err}")
                            else:
                                _send_outlook_mail("專案 WBS 追蹤 — 今日提醒", body, to_addr)
                    last_notified = now.date()
        except Exception as e:  # noqa: BLE001
            _say(f"[!] 提醒檢查失敗（不影響系統其他功能）：{e}")
        time.sleep(60)


def main():
    _backup_db()
    db.init_db()
    cfg = db.load_config()
    if not db.rows("SELECT id FROM project LIMIT 1"):
        try:
            if __package__ in (None, ""):
                from app import seed
            else:
                from . import seed
            seed.run()
            _say("[i] 首次啟動：已載入兩個專案的初始資料")
        except Exception as e:  # noqa: BLE001
            _say(f"[!] 種子資料載入失敗：{e}")
    port = int(cfg.get("port", 8765))
    # 預設只聽本機——桌機單機版故意這樣設計（不用擔心區網其他人打得到）。
    # 要當 Server 用（讓其他機器連進來）才需要在 config.json 明確加
    # "bind_host": "0.0.0.0"，不是預設行為，得使用者自己選擇要暴露出去。
    bind_host = cfg.get("bind_host", "127.0.0.1")
    url = f"http://127.0.0.1:{port}/"
    try:
        srv = ThreadingHTTPServer((bind_host, port), Handler)
    except OSError as e:
        _say("=" * 56)
        _say(f"[X] 無法啟動：連接埠 {port} 被佔用或無法綁定。")
        _say(f"    詳細訊息：{e}")
        _say(f"    處理方式：先確認是不是已經開著一個視窗；")
        _say(f"    若不是，改 config.json 的 port（例如 8790）再試。")
        _say("=" * 56)
        return
    # HTTPS 是額外多開一個埠，不取代原本的 HTTP 埠——測試期間兩個並存，
    # 確認 HTTPS 那邊沒問題、使用者都改用 https:// 之後，再手動把 HTTP 那個關掉
    # （改 config.json 的 bind_host 或直接不開 tls_enabled 都不會動到這段邏輯，
    # 純粹是「多開一個」，最小風險的過渡做法）。
    tls_url = None
    if cfg.get("tls_enabled"):
        cert_file, key_file = cfg.get("tls_cert_file") or "", cfg.get("tls_key_file") or ""
        if not (os.path.isfile(cert_file) and os.path.isfile(key_file)):
            _say(f"[!] tls_enabled=true 但憑證/私鑰檔案找不到（{cert_file} / {key_file}），"
                 f"跳過 HTTPS，只開 HTTP。")
        else:
            tls_port = int(cfg.get("tls_port", 3443))
            try:
                srv_tls = ThreadingHTTPServer((bind_host, tls_port), Handler)
                ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                ctx.load_cert_chain(cert_file, key_file)
                srv_tls.socket = ctx.wrap_socket(srv_tls.socket, server_side=True)
            except (OSError, ssl.SSLError) as e:
                _say(f"[!] HTTPS 埠 {tls_port} 啟動失敗（{e}），跳過 HTTPS，只開 HTTP。")
            else:
                tls_url = f"https://127.0.0.1:{tls_port}/"
                threading.Thread(target=srv_tls.serve_forever, daemon=True).start()
    _say("=" * 56)
    _say("  專案 WBS 追蹤系統")
    _say(f"  網址　　：{url}")
    if tls_url:
        _say(f"  HTTPS　：{tls_url}（測試用自簽憑證，瀏覽器會先跳警告）")
    _say(f"  文件目錄：{cfg.get('docs_root')}")
    _say("  按 Ctrl+C 結束")
    _say("=" * 56)
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    threading.Thread(target=_notify_loop, daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        _say("\n已關閉。")


if __name__ == "__main__":
    main()
