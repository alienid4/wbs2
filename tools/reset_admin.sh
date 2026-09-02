#!/usr/bin/env bash
# 薄殼層，跟 patch.sh／install_server.sh 同一套呼叫習慣：
#   sudo bash tools/reset_admin.sh              互動選單，從人員名單挑一個
#   sudo bash tools/reset_admin.sh <姓名或帳號>  跳過選單，直接指定
# 實際邏輯在 reset_admin.py（跟主程式共用密碼雜湊邏輯，避免重寫一份分岔）。
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_ROOT="${CLWBS_APP:-/opt/cl_wbs}"
SVC_USER="${CLWBS_USER:-wbs}"

if [ $# -gt 1 ]; then
  echo "用法： sudo bash $0 [姓名或登入帳號]" >&2
  exit 1
fi

PY="$HERE/reset_admin.py"
[ -f "$PY" ] || PY="$APP_ROOT/tools/reset_admin.py"
[ -f "$PY" ] || { echo "找不到 reset_admin.py" >&2; exit 1; }

if [ "$(id -u)" = "0" ]; then
  # 用 root 執行本腳本時，改用服務帳號跑 python，維持「不用 root 碰程式碼」的原則。
  exec sudo -u "$SVC_USER" python3 "$PY" "$@"
else
  exec python3 "$PY" "$@"
fi
