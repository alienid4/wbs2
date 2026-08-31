# -*- coding: utf-8 -*-
"""單機 <-> Server 資料同步。只用標準函式庫，跟主程式一樣零外部相依。

設計原則：單一權威版本、明確動作觸發，不是即時雙向合併。任何時刻只有一邊是
「最新」，push/pull 都是使用者主動按下才發生，不會有背景執行緒偷偷同步——
這樣不用處理「兩邊同時被改」的即時合併衝突，衝突只會在你主動同步的那一刻
被發現、也只在那一刻需要你決定聽誰的。

用法：
    python tools/sync_server.py status  --local http://127.0.0.1:3001 --remote http://192.168.1.221:8765
    python tools/sync_server.py push    --local http://127.0.0.1:3001 --remote http://192.168.1.221:8765
    python tools/sync_server.py pull    --local http://127.0.0.1:3001 --remote http://192.168.1.221:8765

第一次同步（雙方都還沒有共同基準點）必須明確指定 --init-from：push 只接受
--init-from local（本機蓋過 Server），pull 只接受 --init-from remote（Server
蓋過本機）——不自動猜方向，猜錯會覆蓋掉另一邊所有資料且救不回來（覆蓋前雖然
雙方都會各自留一份 presync 備份，但正確做法是一開始就選對方向，不要靠備份
補救）。

之後的每一次 push/pull，都會先比對「本機記得的上次同步版本」跟「雙方現在
的版本」：
  - 只有一邊變了 -> 照著做（push 或 pull）
  - 兩邊都沒變 -> 什麼都不做
  - 兩邊都變了 -> 拒絕執行，印出兩邊內容不同，請你自己決定要留哪一邊
    （用 --force-local 或 --force-remote 明確覆蓋，或先手動比對兩份備份）
"""
import argparse
import json
import os
import sys
import urllib.request
import urllib.error

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_PATH = os.path.join(ROOT, "data", ".sync_state.json")
CONFIG_PATH = os.path.join(ROOT, "config.json")


def _say(msg=""):
    """Windows 主控台可能不是 UTF-8（cp950 等），✓/✗ 這類符號會讓 print 直接
    UnicodeEncodeError 炸掉，跟 app/server.py 的 _say 用同一招吞掉重編碼。"""
    try:
        print(msg)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "ascii"
        print(msg.encode(enc, "replace").decode(enc, "replace"))


def _local_sync_token():
    """讀本機 config.json 的 sync_token——這是機器對機器的共用密鑰，不是任何人
    的登入密碼，跟兩台的 config.json 各自手動填一樣的值配對。Server 模式的
    /api/admin/* 同步端點只認這個，不吃一般使用者的登入 session。"""
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return (json.load(f).get("sync_token") or "").strip()
    except (OSError, ValueError):
        return ""


def _get(url, path):
    req = urllib.request.Request(url.rstrip("/") + path,
                                  headers={"X-Sync-Token": _local_sync_token()})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read(), dict(r.headers)


def _post_bytes(url, path, data):
    req = urllib.request.Request(
        url.rstrip("/") + path, data=data, method="POST",
        headers={"Content-Type": "application/octet-stream",
                "X-Sync-Token": _local_sync_token()})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def fingerprint(base_url):
    raw, _ = _get(base_url, "/api/admin/sync-status")
    return json.loads(raw.decode("utf-8"))


def load_state():
    if not os.path.isfile(STATE_PATH):
        return {}
    with open(STATE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_state(state):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def cmd_status(args):
    local_fp = fingerprint(args.local)
    remote_fp = fingerprint(args.remote)
    state = load_state()
    _say(f"本機   {args.local}")
    _say(f"       sha256={local_fp['sha256']}  size={local_fp['size']}  mtime={local_fp['mtime']}")
    _say(f"Server {args.remote}")
    _say(f"       sha256={remote_fp['sha256']}  size={remote_fp['size']}  mtime={remote_fp['mtime']}")
    baseline = state.get("last_synced_sha256")
    _say(f"上次同步基準  {baseline or '（從未同步過）'}")
    if not baseline:
        _say("=> 尚未建立基準，第一次同步請用 push/pull 加 --init-from local 或 --init-from remote")
    elif local_fp["sha256"] == remote_fp["sha256"]:
        _say("=> 兩邊一致，不需要同步")
    elif local_fp["sha256"] == baseline and remote_fp["sha256"] != baseline:
        _say("=> Server 那邊有新資料，建議 pull")
    elif remote_fp["sha256"] == baseline and local_fp["sha256"] != baseline:
        _say("=> 本機有新資料，建議 push")
    else:
        _say("=> 衝突：本機跟 Server 自上次同步後都各自變了，push/pull 都會被拒絕，"
              "需要人工決定、加 --force-local 或 --force-remote")


def _resolve_direction(local_fp, remote_fp, state, want_push, force_local, force_remote):
    baseline = state.get("last_synced_sha256")
    if not baseline:
        return None  # 呼叫端要求走 --init-from 流程
    both_changed = local_fp["sha256"] != baseline and remote_fp["sha256"] != baseline \
        and local_fp["sha256"] != remote_fp["sha256"]
    if both_changed and not (force_local or force_remote):
        _say("✗ 衝突：本機跟 Server 自上次同步後都各自變了，拒絕自動覆蓋。")
        _say(f"  本機   sha256={local_fp['sha256']}  mtime={local_fp['mtime']}")
        _say(f"  Server sha256={remote_fp['sha256']}  mtime={remote_fp['mtime']}")
        _say("  確定要留哪一邊，用 --force-local（以本機為準）或 --force-remote（以 Server 為準）重跑。")
        sys.exit(1)
    return True


def cmd_push(args):
    if args.init_from == "remote":
        _say("✗ push 只接受 --init-from local。你要的是拿 Server 資料蓋過本機，"
              "請改用：python tools/sync_server.py pull --init-from remote ...")
        sys.exit(1)
    state = load_state()
    if not state.get("last_synced_sha256") and not args.init_from and not args.force_local:
        _say("✗ 從未同步過，第一次請明確指定 --init-from local，確認要用本機資料覆蓋 Server。")
        sys.exit(1)
    local_fp = fingerprint(args.local)
    remote_fp = fingerprint(args.remote)
    if state.get("last_synced_sha256"):
        _resolve_direction(local_fp, remote_fp, state, True, args.force_local, args.force_remote)
    if local_fp["sha256"] == remote_fp["sha256"]:
        _say("· 兩邊已經一致，不需要 push")
        state["last_synced_sha256"] = local_fp["sha256"]
        save_state(state)
        return
    data, _ = _get(args.local, "/api/admin/db-snapshot")
    result = _post_bytes(args.remote, "/api/admin/db-snapshot", data)
    state["last_synced_sha256"] = result["sha256"]
    state["last_synced_at"] = result["mtime"]
    save_state(state)
    _say(f"✓ 已 push 本機資料到 Server（{len(data)} bytes，sha256={result['sha256'][:12]}…）")


def cmd_pull(args):
    if args.init_from == "local":
        _say("✗ pull 只接受 --init-from remote。你要的是拿本機資料蓋過 Server，"
              "請改用：python tools/sync_server.py push --init-from local ...")
        sys.exit(1)
    state = load_state()
    if not state.get("last_synced_sha256") and not args.init_from and not args.force_remote:
        _say("✗ 從未同步過，第一次請明確指定 --init-from remote，確認要用 Server 資料覆蓋本機。")
        sys.exit(1)
    local_fp = fingerprint(args.local)
    remote_fp = fingerprint(args.remote)
    if state.get("last_synced_sha256"):
        _resolve_direction(local_fp, remote_fp, state, False, args.force_local, args.force_remote)
    if local_fp["sha256"] == remote_fp["sha256"]:
        _say("· 兩邊已經一致，不需要 pull")
        state["last_synced_sha256"] = local_fp["sha256"]
        save_state(state)
        return
    data, _ = _get(args.remote, "/api/admin/db-snapshot")
    result = _post_bytes(args.local, "/api/admin/db-snapshot", data)
    state["last_synced_sha256"] = result["sha256"]
    state["last_synced_at"] = result["mtime"]
    save_state(state)
    _say(f"✓ 已把 Server 資料 pull 回本機（{len(data)} bytes，sha256={result['sha256'][:12]}…）")


def main():
    # --local/--remote 這些選項掛在共用的 parent parser 上、每個子命令各自繼承一份，
    # 這樣 flags 放子命令前後都能用（單純掛在最外層 parser 會变成「flags 一定要放
    # 在 status/push/pull 前面」，不直覺、也跟上面文件字串寫的用法對不起來）。
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--local", required=True, help="本機 CL_WBS 網址，例如 http://127.0.0.1:3001")
    common.add_argument("--remote", required=True, help="Server CL_WBS 網址，例如 http://192.168.1.221:8765")
    common.add_argument("--init-from", choices=["local", "remote"], help="第一次同步，指定以哪邊為準")
    common.add_argument("--force-local", action="store_true", help="有衝突時強制以本機為準")
    common.add_argument("--force-remote", action="store_true", help="有衝突時強制以 Server 為準")

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", parents=[common])
    sub.add_parser("push", parents=[common])
    sub.add_parser("pull", parents=[common])
    args = ap.parse_args()

    if args.init_from == "local":
        args.force_local = True
    elif args.init_from == "remote":
        args.force_remote = True

    try:
        {"status": cmd_status, "push": cmd_push, "pull": cmd_pull}[args.cmd](args)
    except urllib.error.URLError as e:
        _say(f"✗ 連線失敗：{e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
