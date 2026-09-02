# -*- coding: utf-8 -*-
"""SQLite 存取層。

設計原則：資料庫只存「狀態與路徑」，不存文件本體。
整個 data/ 目錄刪掉後可用 seed.py 重建，移植時只需帶走 app/ 與 config.json。
"""
import json
import os
import sqlite3
from contextlib import contextmanager

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_DIR = os.path.join(ROOT, "app")
CONFIG_PATH = os.path.join(ROOT, "config.json")

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS project (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    code          TEXT NOT NULL UNIQUE,        -- P01 / P02
    name          TEXT NOT NULL,
    owner         TEXT,
    start_date    TEXT,
    end_date      TEXT,                        -- 目前目標完工日，隨時可改（凍結前的暫定天花板）
    baseline_end  TEXT,                        -- 對主管承諾的完工日。凍結後唯讀，只能靠「重新基準化」改
    baseline_set_at TEXT,                      -- 最近一次凍結/重新基準化的時間
    docs_subdir   TEXT,                        -- 相對於 config.docs_root
    color         TEXT DEFAULT '#2563eb',
    archived      INTEGER DEFAULT 0,
    sort_order    INTEGER DEFAULT 0             -- 專案分頁／總覽的顯示順序，使用者可拖拉調整
);

-- 專案可以不只一個人負責——owner 欄位保留當「主要負責人」（既有畫面/邏輯都繼續
-- 認這欄，不用大改），這張表是額外的「協同負責人」，多對多、person_name 一樣是
-- 純文字比對不是外鍵，跟 owner 欄位同一套「改名/刪除不動歷史資料」哲學。
CREATE TABLE IF NOT EXISTS project_owner (
    project_id    INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    person_name   TEXT NOT NULL,
    PRIMARY KEY (project_id, person_name)
);

CREATE TABLE IF NOT EXISTS baseline_revision (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id    INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    revision_no   INTEGER NOT NULL,            -- 第幾次凍結，1 為首次
    baseline_end  TEXT NOT NULL,               -- 這一版承諾的完工日
    reason        TEXT,                        -- 重新基準化的理由（首次凍結可空）
    created_at    TEXT
);

CREATE TABLE IF NOT EXISTS stage (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id    INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    code          TEXT NOT NULL,               -- S01..S09
    seq           INTEGER NOT NULL,
    name          TEXT NOT NULL,
    purpose       TEXT,
    exit_gate     TEXT,
    planned_start TEXT,
    planned_end   TEXT,
    status        TEXT DEFAULT '未開始',        -- 未開始/進行中/已完成
    UNIQUE(project_id, code)
);

CREATE TABLE IF NOT EXISTS doc_req (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id    INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    stage_code    TEXT NOT NULL,
    doc_code      TEXT NOT NULL,               -- REQ-SPEC / POC-PLAN ...
    name          TEXT NOT NULL,
    required      INTEGER DEFAULT 1,
    note          TEXT,
    manual_done   INTEGER DEFAULT 0,           -- 舊欄位，保留相容，新畫面已改用 progress
    manual_note   TEXT,
    progress      INTEGER DEFAULT 0,           -- 0 / 50 / 100，這份文件寫到哪了；
                                                -- 100 才算「已交」、會影響階段關卡
    owner         TEXT,                        -- 這份文件由誰承辦，不是每次都是專案負責人
    UNIQUE(project_id, stage_code, doc_code)
);

CREATE TABLE IF NOT EXISTS person (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL UNIQUE         -- 人員名單，給「負責人」下拉選單用，
                                                -- 純文字比對，不是外鍵——改名/刪除不會動到
                                                -- 既有任務/文件上已經填的負責人字串
                                                -- （帳號登入也是同一個 name，見下面三欄）
    , password_hash TEXT                       -- PBKDF2-HMAC-SHA256 雜湊，NULL＝這個人
                                                -- 還沒設過密碼，下次登入時輸入的密碼會
                                                -- 直接變成他的密碼（自助認領帳號，見
                                                -- server.py 的 auth_login）
    , password_salt TEXT
    , is_admin      INTEGER DEFAULT 0          -- 舊欄位，保留相容（migration 會把 1 值
                                                -- 搬進 role='admin'）——新程式碼一律看
                                                -- role，不要再新增看 is_admin 的邏輯
    , role          TEXT DEFAULT 'user'        -- user（一般使用者，只能編自己負責的項目）
                                                -- / manager（主管，能編所有人的工作項目，
                                                -- 但不能做系統管理動作）/ admin（管理者，
                                                -- manager 的權限 + 重設密碼、清空資料等
                                                -- 系統管理動作）。多機同步/Server 模式才
                                                -- 需要登入，單機版預設不強制
    , username      TEXT                        -- 登入帳號，跟「負責人」顯示用的中文姓名
                                                -- （name 欄）分開——「負責人」欄位、WBS/
                                                -- 文件的擁有者比對永遠看 name，不會因為
                                                -- 使用者習慣用英文帳號登入而受影響。NULL
                                                -- 代表這個人沒有另外設帳號，登入時繼續用
                                                -- name 本身（向下相容，不強迫每個人都要
                                                -- 設英文帳號）。唯一性用下面的 INDEX 強制
                                                -- （不能直接寫 UNIQUE 欄位限制——SQLite 的
                                                -- ALTER TABLE ADD COLUMN 不支援替既有資料庫
                                                -- 補一個帶 UNIQUE 的欄位，只能新建資料庫時
                                                -- 用；分開寫 INDEX 兩種情況都適用）
);

CREATE TABLE IF NOT EXISTS session (
    token         TEXT PRIMARY KEY,
    person_id     INTEGER NOT NULL REFERENCES person(id) ON DELETE CASCADE,
    created_at    TEXT,
    expires_at    TEXT
);

CREATE TABLE IF NOT EXISTS doc_file (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id    INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    stage_code    TEXT,
    doc_code      TEXT,
    rel_path      TEXT NOT NULL,               -- 相對 docs_root，不存絕對路徑（可移植）
    filename      TEXT NOT NULL,
    size          INTEGER,
    mtime         TEXT,
    version       TEXT,
    matched_by    TEXT,                        -- code / folder / unmatched
    scanned_at    TEXT
);
CREATE INDEX IF NOT EXISTS ix_docfile ON doc_file(project_id, stage_code, doc_code);

CREATE TABLE IF NOT EXISTS task (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id     INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    wbs_no         TEXT NOT NULL,              -- A1 / A1.1
    parent_wbs     TEXT,                       -- 週交付掛在里程碑底下
    level          TEXT DEFAULT 'L0',          -- L0 工作項目 / L1 週交付 / M 里程碑（零工期事件）
    stage_code     TEXT,
    name           TEXT NOT NULL,
    owner          TEXT,
    planned_start  TEXT,
    planned_end    TEXT,
    baseline_start TEXT,                       -- 凍結時的計畫起日，之後 UI 不可再改
    baseline_end   TEXT,                       -- 凍結時的計畫迄日
    actual_start   TEXT,                       -- 實際開工日
    actual_finish  TEXT,                       -- 實際完成日：標「已完成」時自動蓋今天，可事後修正
    hard_deadline  TEXT,                       -- 外部死線，優先於推算
    predecessors   TEXT DEFAULT '',            -- 逗號分隔的 wbs_no
    status         TEXT DEFAULT '未開始',       -- 未開始/進行中/已完成/延遲
    progress       INTEGER DEFAULT 0,          -- 0 / 50 / 100
    note           TEXT,
    risk           TEXT,
    UNIQUE(project_id, wbs_no)
);

CREATE TABLE IF NOT EXISTS checkin (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id       INTEGER NOT NULL REFERENCES task(id) ON DELETE CASCADE,
    log_date      TEXT NOT NULL,
    progress      INTEGER,
    status        TEXT,
    note          TEXT,
    created_at    TEXT
);
CREATE INDEX IF NOT EXISTS ix_checkin ON checkin(log_date);

CREATE TABLE IF NOT EXISTS report (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    week_end      TEXT NOT NULL UNIQUE,        -- 該週週五
    content_md    TEXT,
    content_html  TEXT,                        -- 單檔自足 HTML，可直接寄給主管
    metrics_json  TEXT,                        -- 產出當下的預測完工/落後天數快照，供下週對照「較上週」
    created_at    TEXT
);

CREATE TABLE IF NOT EXISTS setting (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

DEFAULT_CONFIG = {
    "docs_root": "D:/專案文件",
    "db_path": "data/wbs.db",
    "port": 8765,
    "bind_host": "127.0.0.1",  # 只聽本機；要當 Server 給其他機器連才改成 0.0.0.0，不是預設行為
    "workweek": [1, 2, 3, 4, 5],
    "holidays": [],
    "report_day": 5,
    "report_time": "14:00",
    "daily_checkin_time": "17:20",
    "blocks": {"P01": "09:00-12:00", "P02": "14:00-17:00"},
    "amber_float_days": 2,
    "notify_enabled": False,   # 桌面提醒預設關閉，使用者要自己開——不強推通知
    "notify_time": "09:00",    # 每天最早檢查一次的時間點；程式沒開著就不會提醒
    "notify_email_enabled": False,  # 額外寄信提醒，同樣預設關閉
    "notify_email_to": "",          # 收件地址，空白時不寄
    "notify_email_mode": "outlook", # outlook（借用本機已登入 Outlook）/ smtp（自行填寄件帳號密碼）
    "smtp_host": "",                # 例如 smtp.gmail.com
    "smtp_port": 465,               # 465=SSL（本系統固定用 SSL，不支援 STARTTLS）
    "smtp_user": "",                # 寄件帳號，通常跟 smtp_from 相同
    "smtp_pass": "",                # 明碼存在本機 config.json，Gmail 等服務請用「應用程式密碼」，不要用登入密碼
    "sync_token": "",               # 單機/Server 同步用的機器對機器共用密鑰，不是任何人的登入密碼；
                                     # 兩邊 config.json 要填一樣的值，Server 模式下同步端點才會認
    "tls_enabled": False,           # 開了才會多開一個 HTTPS 埠（tls_port），不影響原本的 HTTP 埠——
                                     # 測試期間兩個並存，確認沒問題後再把 HTTP 那個關掉
    "tls_port": 3443,               # HTTPS 監聽埠，跟 port（HTTP）分開，不互相影響
    "tls_cert_file": "",            # 憑證檔路徑（.pem/.crt），測試期間用自簽憑證即可
    "tls_key_file": "",             # 私鑰檔路徑（.pem/.key）
}


def load_config():
    if not os.path.exists(CONFIG_PATH):
        save_config(DEFAULT_CONFIG)
        return dict(DEFAULT_CONFIG)
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    merged = dict(DEFAULT_CONFIG)
    merged.update(cfg)
    return merged


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def db_path():
    cfg = load_config()
    p = cfg.get("db_path", "data/wbs.db")
    if not os.path.isabs(p):
        p = os.path.join(ROOT, p)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    return p


@contextmanager
def conn():
    c = sqlite3.connect(db_path())
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    try:
        yield c
        c.commit()
    finally:
        c.close()


# 新增欄位一律走這裡加，不改上面 CREATE TABLE 的既有欄位順序——
# 舊 db.wbs 檔案（已有資料）用 ALTER TABLE 補欄位；全新建立的 db 由 SCHEMA 一次到位，這裡跑過即空轉。
MIGRATIONS = [
    ("project", "baseline_end", "TEXT"),
    ("project", "baseline_set_at", "TEXT"),
    ("task", "baseline_start", "TEXT"),
    ("task", "baseline_end", "TEXT"),
    ("task", "actual_start", "TEXT"),
    ("task", "actual_finish", "TEXT"),
    ("report", "content_html", "TEXT"),
    ("report", "metrics_json", "TEXT"),
    ("doc_req", "progress", "INTEGER DEFAULT 0"),
    ("project", "sort_order", "INTEGER DEFAULT 0"),
    ("doc_req", "owner", "TEXT"),
    ("person", "password_hash", "TEXT"),
    ("person", "password_salt", "TEXT"),
    ("person", "is_admin", "INTEGER DEFAULT 0"),
    ("person", "role", "TEXT DEFAULT 'user'"),
    ("person", "username", "TEXT"),
    # 自由格式備忘欄（主機 IP、待申請防火牆規則之類雜項筆記）——跟 WBS/文件無關，
    # 不進排程或關卡判斷，純粹隨手記，欄位獨立不影響任何既有邏輯。
    ("project", "memo", "TEXT"),
]


def _migrate(c):
    added_role = False
    for table, col, decl in MIGRATIONS:
        existing = {r[1] for r in c.execute(f"PRAGMA table_info({table})").fetchall()}
        if col not in existing:
            c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
            if (table, col) == ("person", "role"):
                added_role = True
    if added_role:
        # 剛加這欄的既有資料庫：把舊的 is_admin=1 搬進 role='admin'，不然升級後
        # 原本的管理者一夜之間變回一般使用者，權限判斷全部改看 role 之後這步
        # 不做的話會是真的權限倒退，不是無害的欄位新增。
        c.execute("UPDATE person SET role='admin' WHERE is_admin=1")
    # username 的唯一性用 INDEX 強制，不是欄位層級的 UNIQUE——SQLite 的
    # ALTER TABLE ADD COLUMN 不支援直接補一個帶 UNIQUE 的欄位，只能另外建
    # INDEX；放在這裡（migrate 尾端、ALTER TABLE 都跑完之後）而不是 SCHEMA
    # 字串裡，是因為 SCHEMA 對「已存在的舊資料庫」不會重新跑 CREATE TABLE，
    # 這時候欄位還沒補上，這裡先建 INDEX 會直接噴「no such column」。
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_person_username ON person(username)")


def init_db():
    with conn() as c:
        c.executescript(SCHEMA)
        _migrate(c)


def rows(sql, params=()):
    with conn() as c:
        return [dict(r) for r in c.execute(sql, params).fetchall()]


def one(sql, params=()):
    r = rows(sql, params)
    return r[0] if r else None


def run(sql, params=()):
    with conn() as c:
        cur = c.execute(sql, params)
        return cur.lastrowid


def _stage_template_path():
    return os.path.join(APP_DIR, "templates", "stage_template.json")


def load_stage_template():
    with open(_stage_template_path(), "r", encoding="utf-8") as f:
        return json.load(f)


def save_stage_template(data):
    # 縮排＋不轉義中文，改完的檔案人眼看得懂、git diff 也乾淨，不會變成一整行亂碼。
    with open(_stage_template_path(), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
