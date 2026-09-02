# -*- coding: utf-8 -*-
"""救援用：真的被鎖在系統外面（忘記密碼、帳號被鎖、或資料庫是空的）時，
不透過網頁登入，直接在資料庫裡建立/重設一個管理者帳號。

只用標準函式庫，跟主程式一樣零外部相依。密碼雜湊邏輯直接沿用
app/server.py 的 _hash_password，不重寫一份——避免兩邊參數（PBKDF2 迭代次數等）
分岔造成「這裡設的密碼，網頁登入驗不過」這種詭異狀況。

用法：
    python tools/reset_admin.py            互動選單，從現有人員名單挑一個，或新增
    python tools/reset_admin.py <姓名>      跳過選單，直接指定姓名（原本的用法）

會互動式輸入密碼（不會顯示在畫面上、不會留在 shell history）。
如果這個姓名已經存在，就重設密碼並把 role 強制改成 admin；
不存在就新建一個 admin 帳號。
"""
import getpass
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import db  # noqa: E402
from app.server import _hash_password  # noqa: E402


def pick_name_via_menu():
    people = db.rows("SELECT name, role FROM person ORDER BY name")
    print("現有人員名單：")
    if not people:
        print("  （目前一個人都沒有）")
    for i, p in enumerate(people, 1):
        tag = f"（{p['role']}）" if p["role"] and p["role"] != "user" else ""
        print(f"  {i}) {p['name']} {tag}")
    print("  0) 新增一個新帳號")
    choice = input("要重設密碼／設成 admin 的是哪一個？輸入編號：").strip()
    if choice == "0" or not people:
        name = input("新帳號的姓名：").strip()
        return name
    try:
        idx = int(choice)
        if not (1 <= idx <= len(people)):
            raise ValueError
    except ValueError:
        print("輸入無效", file=sys.stderr)
        return None
    return people[idx - 1]["name"]


def main():
    db.init_db()

    if len(sys.argv) == 2:
        name = sys.argv[1].strip()
    elif len(sys.argv) == 1:
        name = pick_name_via_menu()
    else:
        print(f"用法：python {sys.argv[0]} [姓名或登入帳號]", file=sys.stderr)
        return 1

    if not name:
        print("姓名不能是空的", file=sys.stderr)
        return 1

    pw1 = getpass.getpass("新密碼：")
    if not pw1:
        print("密碼不能是空的", file=sys.stderr)
        return 1
    pw2 = getpass.getpass("再輸入一次：")
    if pw1 != pw2:
        print("兩次輸入不一致", file=sys.stderr)
        return 1

    h, salt = _hash_password(pw1)
    existing = db.one("SELECT id FROM person WHERE name=?", (name,))
    if existing:
        db.run(
            "UPDATE person SET password_hash=?, password_salt=?, role='admin', "
            "is_admin=1 WHERE id=?",
            (h, salt, existing["id"]),
        )
        print(f"已重設「{name}」的密碼，並設為 admin。")
    else:
        db.run(
            "INSERT INTO person(name, password_hash, password_salt, role, is_admin) "
            "VALUES (?,?,?,'admin',1)",
            (name, h, salt),
        )
        print(f"已新增管理者帳號「{name}」。")

    print("登入時「姓名或登入帳號」欄位填這個名字即可（除非這個人另外設了 username）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
