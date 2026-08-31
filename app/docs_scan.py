# -*- coding: utf-8 -*-
"""文件目錄掃描與應繳文件比對。

刻意的設計限制：
  1. 只讀取檔名與 mtime，不開啟、不複製、不搬移任何文件。
  2. 資料庫只存相對於 docs_root 的路徑，換機器改 config.json 的 docs_root 即可。
  3. 掃不到不代表沒有 —— 可在介面手動勾選「已備妥」並註明原因。
"""
import datetime as dt
import os
import re

from . import db

_SAFE_CHARS = re.compile(r"[^0-9A-Za-z一-鿿_\-]+")

SKIP_DIRS = {".git", "__pycache__", "node_modules", ".idea", ".vs", "$RECYCLE.BIN"}
SKIP_PREFIX = ("~$", ".")
DOC_EXT = {".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".pdf",
           ".msg", ".eml", ".txt", ".md", ".png", ".jpg", ".jpeg", ".zip"}

VER_RE = re.compile(r"[_\-]v(\d+(?:\.\d+)?)", re.I)


def docs_root():
    cfg = db.load_config()
    return cfg.get("docs_root") or ""


def _norm(x):
    return re.sub(r"[\s_\-]+", "", (x or "").upper())


def scan_project(project_id):
    """掃描單一專案的文件目錄，重建 doc_file 表。回傳統計。"""
    proj = db.one("SELECT * FROM project WHERE id=?", (project_id,))
    if not proj:
        return {"error": "查無此專案"}
    root = docs_root()
    sub = proj.get("docs_subdir") or ""
    base = os.path.join(root, sub) if sub else root
    if not root or not os.path.isdir(base):
        return {"error": f"文件目錄不存在：{base}", "base": base, "files": 0}

    reqs = db.rows("SELECT * FROM doc_req WHERE project_id=?", (project_id,))
    code_map = {_norm(r["doc_code"]): r for r in reqs}
    # 較長的代碼優先比對，避免 RFP 誤吃 RFP-FINAL
    codes_sorted = sorted(code_map.keys(), key=len, reverse=True)

    stages = {st["code"]: st for st in
              db.rows("SELECT * FROM stage WHERE project_id=?", (project_id,))}

    now = dt.datetime.now().isoformat(timespec="seconds")
    found = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [x for x in dirnames if x not in SKIP_DIRS and not x.startswith(".")]
        for fn in filenames:
            if fn.startswith(SKIP_PREFIX):
                continue
            ext = os.path.splitext(fn)[1].lower()
            if ext and ext not in DOC_EXT:
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root).replace("\\", "/")
            nfn = _norm(fn)
            nrel = _norm(rel)

            doc_code, stage_code, matched_by = None, None, "unmatched"
            for c in codes_sorted:
                if c and c in nfn:
                    doc_code = code_map[c]["doc_code"]
                    stage_code = code_map[c]["stage_code"]
                    matched_by = "code"
                    break
            if not doc_code:
                # 退而求其次：用所在資料夾對應階段
                for sc, st in stages.items():
                    if _norm(sc) in nrel or _norm(st["name"]) in nrel:
                        stage_code, matched_by = sc, "folder"
                        break
            try:
                stt = os.stat(full)
                size, mtime = stt.st_size, dt.datetime.fromtimestamp(
                    stt.st_mtime).isoformat(timespec="seconds")
            except OSError:
                size, mtime = None, None
            mv = VER_RE.search(fn)
            found.append((project_id, stage_code, doc_code, rel, fn, size, mtime,
                          mv.group(1) if mv else "", matched_by, now))

    with db.conn() as c:
        c.execute("DELETE FROM doc_file WHERE project_id=?", (project_id,))
        c.executemany(
            "INSERT INTO doc_file(project_id,stage_code,doc_code,rel_path,filename,"
            "size,mtime,version,matched_by,scanned_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            found)
    return {
        "base": base, "files": len(found),
        "matched": sum(1 for f in found if f[8] == "code"),
        "by_folder": sum(1 for f in found if f[8] == "folder"),
        "unmatched": sum(1 for f in found if f[8] == "unmatched"),
        "scanned_at": now,
    }


def stage_docs(project_id, stage_code):
    """回傳該階段的應繳清單 + 對應到的實體檔案。"""
    reqs = db.rows(
        "SELECT * FROM doc_req WHERE project_id=? AND stage_code=? ORDER BY required DESC, id",
        (project_id, stage_code))
    files = db.rows(
        "SELECT * FROM doc_file WHERE project_id=? AND stage_code=?",
        (project_id, stage_code))
    by_code = {}
    for f in files:
        by_code.setdefault(f["doc_code"] or "_none", []).append(f)
    out = []
    for r in reqs:
        fl = sorted(by_code.get(r["doc_code"], []),
                    key=lambda x: (x.get("version") or "", x.get("mtime") or ""))
        # 有掃到檔案就視為 100%，沒掃到就用使用者自己標的 0/50/100。
        pct = 100 if fl else (r.get("progress") or 0)
        out.append({
            **r,
            "files": fl,
            "has_file": bool(fl),
            "progress_pct": pct,
            # ready＝真的掃到檔案，或使用者自己把進度標成 100%——50% 只是給自己看的
            # 進度提示，不算「已交」，不會讓階段關卡通過，避免「寫了一半」被當成交了。
            "ready": pct >= 100,
            "latest": fl[-1] if fl else None,
        })
    return {"items": out, "orphans": by_code.get("_none", [])}


def gate_status(project_id, stage_code):
    """階段出場關卡：必繳文件是否齊備。

    三個數字刻意分開算，回答的是三個不同問題：
      required_ready/required_total → 「能不能過關」（出場條件只看必繳，二元）
      all_ready/all_total           → 「幾項已經真的交了」（必繳+選繳都算，二元計數）
      avg_progress                  → 「整體平均寫到哪了」（0/50/100 取平均，會反映
                                        「兩項 50%」這種還沒交但有進度的狀態，不是卡在
                                        0% 不動——這是使用者實測回饋抓到的落差：只看
                                        二元的「交了幾項」，會讓「大家都做了一半」跟
                                        「完全沒人動」在畫面上長得一樣）
    """
    data = stage_docs(project_id, stage_code)
    req = [i for i in data["items"] if i["required"]]
    missing = [i for i in req if not i["ready"]]
    opt_missing = [i for i in data["items"] if not i["required"] and not i["ready"]]
    all_items = data["items"]
    avg_progress = (round(sum(i["progress_pct"] for i in all_items) / len(all_items))
                    if all_items else 0)
    return {
        "stage_code": stage_code,
        "required_total": len(req),
        "required_ready": len(req) - len(missing),
        "all_total": len(all_items),
        "all_ready": sum(1 for i in all_items if i["ready"]),
        "avg_progress": avg_progress,
        "missing": [{"doc_code": i["doc_code"], "name": i["name"]} for i in missing],
        "optional_missing": [{"doc_code": i["doc_code"], "name": i["name"]} for i in opt_missing],
        "passed": len(missing) == 0 and len(req) > 0,
        "orphan_count": len(data["orphans"]),
    }


def build_folders(project_id):
    """依階段範本建立文件目錄骨架（只建資料夾，不動既有檔案）。"""
    proj = db.one("SELECT * FROM project WHERE id=?", (project_id,))
    root = docs_root()
    if not root:
        return {"error": "config.json 的 docs_root 尚未設定"}
    base = os.path.join(root, proj.get("docs_subdir") or proj["code"])
    stages = db.rows("SELECT * FROM stage WHERE project_id=? ORDER BY seq", (project_id,))
    created = []
    for st in stages:
        p = os.path.join(base, f"{st['seq']:02d}_{st['code']}_{st['name']}")
        if not os.path.isdir(p):
            os.makedirs(p, exist_ok=True)
            created.append(os.path.relpath(p, root).replace("\\", "/"))
    for extra in ("00_專案總覽", "99_往來信件與會議紀錄"):
        p = os.path.join(base, extra)
        if not os.path.isdir(p):
            os.makedirs(p, exist_ok=True)
            created.append(os.path.relpath(p, root).replace("\\", "/"))

    readme = os.path.join(base, "00_專案總覽", "檔名規則.txt")
    if not os.path.exists(readme):
        tpl = db.load_stage_template()
        lines = [
            "檔名規則（App 靠這個自動比對應繳文件，請務必遵守）",
            "",
            f"  {tpl['filename_rule']}",
            f"  例：{tpl['filename_example']}",
            "",
            "關鍵是「文件代碼」那一段，其餘欄位可自由發揮。",
            "各階段文件代碼一覽：",
            "",
        ]
        for st in tpl["stages"]:
            lines.append(f"[{st['code']}] {st['name']}")
            for dc in st["docs"]:
                mark = "必繳" if dc["required"] else "選繳"
                lines.append(f"    {dc['code']:<16}{dc['name']}（{mark}）")
            lines.append("")
        with open(readme, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        created.append(os.path.relpath(readme, root).replace("\\", "/"))
    return {"base": base, "created": created}


def _stage_folder(root, base, stage):
    """跟 build_folders 用同一套命名——上傳時資料夾不存在就直接建，不強迫使用者
    先手動按過一次「建立目錄骨架」才能上傳。"""
    p = os.path.join(base, f"{stage['seq']:02d}_{stage['code']}_{stage['name']}")
    os.makedirs(p, exist_ok=True)
    return p


def resolve_file(doc_file_id):
    """下載/開啟用：doc_file.id → 安全的絕對路徑。回傳 None 代表查無此檔或檔案
    已經不在磁碟上（被使用者手動搬走/刪掉，掃描前資料庫還沒更新）。防護跟
    server.py 的 _static() 同一招：算出絕對路徑後檢查真的落在 docs_root 底下，
    不能因為 rel_path 藏了 `..` 就逃出目錄。"""
    row = db.one("SELECT * FROM doc_file WHERE id=?", (doc_file_id,))
    if not row:
        return None
    root = docs_root()
    if not root:
        return None
    full = os.path.normpath(os.path.join(root, row["rel_path"]))
    root_norm = os.path.normpath(root)
    if not (full == root_norm or full.startswith(root_norm + os.sep)):
        return None
    if not os.path.isfile(full):
        return None
    return full, row["filename"]


def delete_file(doc_file_id):
    """刪掉一份上傳錯的檔案：磁碟上的實體檔＋doc_file 的資料庫紀錄一起清掉。
    只刪這一個版次，不影響同一應繳項目底下其他版次的檔案。跟 resolve_file() 同一套
    路徑防護——算出絕對路徑後要落在 docs_root 底下才動手，不能被 rel_path 裡的 `..` 誘拐。"""
    row = db.one("SELECT * FROM doc_file WHERE id=?", (doc_file_id,))
    if not row:
        return {"error": "查無此檔案紀錄"}
    root = docs_root()
    if not root:
        return {"error": "文件根目錄未設定"}
    full = os.path.normpath(os.path.join(root, row["rel_path"]))
    root_norm = os.path.normpath(root)
    if not (full == root_norm or full.startswith(root_norm + os.sep)):
        return {"error": "檔案路徑不合法，拒絕刪除"}
    if os.path.isfile(full):
        os.remove(full)
    db.run("DELETE FROM doc_file WHERE id=?", (doc_file_id,))
    return {"ok": True, "filename": row["filename"]}


def upload_doc(doc_req_id, filename, data):
    """上傳一份文件滿足指定的 doc_req：驗副檔名、自動照命名規則存進正確的階段
    資料夾、版次自動抓現有最大版次 +1，寫完立刻重掃這個專案讓比對結果馬上更新。
    「說明」欄位刻意不讓使用者手打——直接用 doc_req.name（例如「需求規格書」），
    命名規則正不正確不該取決於使用者當下有沒有照著打對。
    project_id 不當參數收——doc_req_id 本身就唯一決定它，多一個參數只是多一個
    「兩個值對不上」的錯誤來源。"""
    req = db.one("SELECT * FROM doc_req WHERE id=?", (doc_req_id,))
    if not req:
        return {"error": "查無此應繳項目"}
    project_id = req["project_id"]
    proj = db.one("SELECT * FROM project WHERE id=?", (project_id,))
    stage = db.one("SELECT * FROM stage WHERE project_id=? AND code=?",
                   (project_id, req["stage_code"]))
    if not stage:
        return {"error": "查無對應階段"}
    root = docs_root()
    if not root or not os.path.isdir(root):
        return {"error": f"文件根目錄不存在或未設定：{root or '（空）'}"}

    ext = os.path.splitext(filename or "")[1].lower()
    if ext not in DOC_EXT:
        return {"error": f"不支援的副檔名「{ext or '（無）'}」，可用：" +
                         "、".join(sorted(DOC_EXT))}
    if len(data) > 25 * 1024 * 1024:
        return {"error": "檔案超過 25MB，上傳限制內請改用共用網路空間並手動標記已備妥"}

    existing = db.rows(
        "SELECT version FROM doc_file WHERE project_id=? AND doc_code=?",
        (project_id, req["doc_code"]))
    versions = [int(float(v["version"])) for v in existing
               if v.get("version") and str(v["version"]).replace(".", "").isdigit()]
    next_v = max(versions, default=0) + 1

    desc = _SAFE_CHARS.sub("", req["name"])[:30] or req["doc_code"]
    today = dt.date.today().strftime("%Y%m%d")
    out_name = f"{proj['code']}_{stage['seq']:02d}_{req['doc_code']}_{desc}_v{next_v}_{today}{ext}"

    base = os.path.join(root, proj.get("docs_subdir") or proj["code"])
    folder = _stage_folder(root, base, stage)
    dest = os.path.join(folder, out_name)
    if os.path.exists(dest):
        return {"error": f"檔案「{out_name}」已存在，請稍後再試一次（版次會自動往後推）"}
    with open(dest, "wb") as f:
        f.write(data)

    scan_project(project_id)
    return {"ok": True, "filename": out_name,
           "rel_path": os.path.relpath(dest, root).replace("\\", "/"),
           "gate": gate_status(project_id, req["stage_code"])}
