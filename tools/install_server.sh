#!/usr/bin/env bash
# 一鍵在 Linux 上把 CL_WBS 建成「Server 模式」（對外聽、給多台機器/多人連）。
# 跟桌機單機版是同一份程式碼，差別只在 config.json 的 bind_host。
#
# 用法：
#   1. 把整個 CL_WBS 專案資料夾（含這支腳本）複製到目標 Linux 機器上任何位置
#   2. cd 進那個資料夾，執行：sudo bash tools/install_server.sh
#   3. 可用環境變數覆蓋預設值，例如：
#        sudo PORT=3001 INSTALL_DIR=/opt/cl_wbs2 bash tools/install_server.sh
#
# 這支腳本會：建專用系統帳號 → 複製程式碼 → 產生 config.json（bind_host=0.0.0.0）
# → 建 systemd 服務並開機自動啟動 → 開防火牆（firewalld 或 ufw，兩個都沒有就跳過並提醒）
# → 打 /api/version 驗證。全部動作都是這支腳本在跑，不是 Claude Code 逐條下指令
# ——這樣以後要在新機器上開一份 Server，你自己一行指令就能搞定，不用每次都要
# 我一步步申請系統層級權限。
#
# 重跑這支腳本是安全的（idempotent）：config.json 已存在就不會覆蓋掉、使用者/
# 目錄已存在就跳過建立，只有程式碼跟 systemd unit 每次都會用最新的覆蓋。

set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "[X] 要用 root 執行（或 sudo）——要建系統帳號、寫 systemd、開防火牆，都需要 root 權限。"
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"   # 專案根目錄（tools/ 的上一層）
SERVICE_USER="${SERVICE_USER:-wbs}"
INSTALL_DIR="${INSTALL_DIR:-/opt/cl_wbs}"
PORT="${PORT:-8765}"
SERVICE_NAME="${SERVICE_NAME:-cl-wbs}"

echo "=== CL_WBS Server 模式安裝 ==="
echo "來源：$SCRIPT_DIR"
echo "安裝到：$INSTALL_DIR（帳號 $SERVICE_USER，port $PORT，服務名稱 $SERVICE_NAME）"
echo

# 1) 專用系統帳號 —— 不跟這台機器上其他服務共用帳號，權限互相隔離
if id -u "$SERVICE_USER" >/dev/null 2>&1; then
  echo "[i] 帳號 $SERVICE_USER 已存在，跳過建立"
else
  useradd -m -d "/home/$SERVICE_USER" -s /bin/bash "$SERVICE_USER"
  echo "[OK] 已建立帳號 $SERVICE_USER"
fi

# 2) 複製程式碼——只拿 app/ tests/ tools/ version.json，不拿 data/（真實資料）跟
#    這台機器自己的 config.json（如果目標目錄已經有的話，不要蓋掉）
mkdir -p "$INSTALL_DIR"
for d in app tests tools; do
  rsync -a --delete "$SCRIPT_DIR/$d/" "$INSTALL_DIR/$d/" 2>/dev/null \
    || cp -a "$SCRIPT_DIR/$d" "$INSTALL_DIR/"
done
cp -a "$SCRIPT_DIR/version.json" "$INSTALL_DIR/version.json"
find "$INSTALL_DIR" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"
echo "[OK] 程式碼已複製到 $INSTALL_DIR"

# 3) config.json —— 已存在就不動（不要把使用者調整過的設定蓋掉），第一次安裝才產生
CONFIG_PATH="$INSTALL_DIR/config.json"
if [ -f "$CONFIG_PATH" ]; then
  echo "[i] $CONFIG_PATH 已存在，不覆蓋。若要改 port/bind_host 請自己編輯後重啟服務。"
else
  SYNC_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))' 2>/dev/null || echo "")"
  mkdir -p "$INSTALL_DIR/docs_placeholder"
  cat > "$CONFIG_PATH" <<EOF
{
  "docs_root": "$INSTALL_DIR/docs_placeholder",
  "db_path": "data/wbs.db",
  "port": $PORT,
  "bind_host": "0.0.0.0",
  "sync_token": "$SYNC_TOKEN"
}
EOF
  chown "$SERVICE_USER:$SERVICE_USER" "$CONFIG_PATH"
  echo "[OK] 已產生 $CONFIG_PATH"
  echo "     sync_token（要跟另一台做資料同步的話，兩邊要填一樣的值）："
  echo "     $SYNC_TOKEN"
fi

# 4) systemd 服務——每次都用最新版覆蓋 unit 檔本身，保證行為跟這支腳本說明的一致
cat > "/etc/systemd/system/${SERVICE_NAME}.service" <<EOF
[Unit]
Description=CL_WBS project tracker (Server mode)
After=network.target

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_USER
WorkingDirectory=$INSTALL_DIR
ExecStart=/usr/bin/env python3 -m app.server
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now "${SERVICE_NAME}.service"
echo "[OK] systemd 服務 ${SERVICE_NAME}.service 已啟動並設為開機自動啟動"

# 5) 防火牆——firewalld 或 ufw 挑一個有的用，兩個都沒有就提醒使用者自己處理
if command -v firewall-cmd >/dev/null 2>&1; then
  firewall-cmd --add-port="${PORT}/tcp" --permanent
  firewall-cmd --reload
  echo "[OK] firewalld 已開放 ${PORT}/tcp"
elif command -v ufw >/dev/null 2>&1; then
  ufw allow "${PORT}/tcp"
  echo "[OK] ufw 已開放 ${PORT}/tcp"
else
  echo "[!] 沒偵測到 firewalld 或 ufw，這台機器可能沒有防火牆、或用別的工具管理——"
  echo "    自己確認 ${PORT}/tcp 對區網開放，否則其他機器連不進來。"
fi

# 6) 驗證
echo
echo "等服務啟動..."
sleep 2
if curl -fsS "http://127.0.0.1:${PORT}/api/version" >/dev/null 2>&1; then
  echo "[OK] 服務正常回應：$(curl -fsS "http://127.0.0.1:${PORT}/api/version")"
  echo
  echo "=== 完成 === 從其他機器打 http://<這台機器的IP>:${PORT}/ 應該打得到。"
  echo "第一次開啟畫面要求登入，人員名單是空的話用「註冊新帳號」建立第一個帳號，"
  echo "系統裡還沒有管理者時，第一個註冊的人自動變管理者。"
else
  echo "[X] /api/version 沒有正常回應，執行 journalctl -u ${SERVICE_NAME}.service -n 50 查看錯誤"
  exit 1
fi
