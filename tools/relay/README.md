# 公司筆電更新用中繼站

**Public repo（純程式碼，無真實資料）**：https://github.com/alienid4/wbs2

## 第一次在公司筆電上取得程式碼

1. 瀏覽器打開 <https://github.com/alienid4/wbs2>
2. 點綠色「Code」按鈕 → **Download ZIP**（不要用 `git clone`，公司網路多半擋 SSH／git 協定）
3. 解壓縮到你要放的資料夾，例如 `D:\CL_WBS`
4. 執行 `start.bat`，第一次會自動產生 `config.json`（本機設定，不會被之後的更新覆蓋）

## 之後每次要更新

在 `D:\CL_WBS\tools\relay\` 底下執行 `update.bat`——用 `curl` 直接下載最新的 zip 並套用，不需要
安裝 git。只會更新 `app/`、`tests/`、`start.bat`、`version.json`，不會動到 `config.json` 跟
`data/`（本機設定跟真實資料一律留著）。

## 在家用機（來源端）要推新版時

在 `C:\AiProject\CL_WBS\tools\relay\` 底下執行 `push_public_patch.bat`：
1. 第一次執行會自動把 public repo clone 到 `%USERPROFILE%\Desktop\CL_WBS_public_relay`
2. 把乾淨的程式碼（`app/`、`tests/`、`README.md`、`start.bat`、`version.json`、`.gitignore`）鏡像複製過去
3. 印出改了哪些檔案，**要你自己手動打 YES 確認**才會 commit + push

**這一步的 `git push` 是使用者要自己按下去的**——Claude Code 的安全機制會擋下 AI 直接推到
public 目的地的動作，這是刻意設計，不是漏洞。

## 安全性

`push_public_patch.bat` 只會複製 `app/`、`tests/` 跟幾個頂層檔案，不會把 `config.json`（含本機
路徑）或 `data/`（真實營運資料）帶進 public repo。`app/seed.py` 已經清過，裡面只有示範用的假資料，
放心。
