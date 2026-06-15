#!/bin/bash
# 從 SDK 工作目錄重新產生 F37 RTSP patch 並 push 到 GitHub。
# 只收錄下面 FILES 清單的檔案 —— 不會把 QR Wi-Fi 等其他功能混進來。
# 用法：改完 ameba-rtos-pro2 裡 F37 相關的檔後，執行 ./update-patch.sh
set -e

SDK="$HOME/Projects/FW/RTL8735B/ameba-rtos-pro2"
REPO="$HOME/Projects/FW/RTL8735B/amb82-mini-rtsp-f37"
PATCH="patches/amb82-mini-f37-rtsp.patch"

# 只屬於「F37 RTSP 修正」的檔案（要新增功能就加進這個清單；QR 等其他功能不要列）
FILES=(
  "project/realtek_amebapro2_v0_example/inc/sensor.h"
  "component/video/driver/RTL8735B/video_api.c"
  "component/video/driver/RTL8735B/video_user_boot.c"
)

# 1. 只對清單內的檔產生 patch（git diff -- <檔> 會把範圍鎖在這些檔）
cd "$SDK"
git diff -- "${FILES[@]}" > "$REPO/$PATCH"
echo "已更新 $PATCH（$(grep -c '^diff' "$REPO/$PATCH") 個檔）"
echo "收錄的檔案："
grep '^diff' "$REPO/$PATCH" | sed -E 's#^diff --git a/##; s/ b\/.*//' | sed 's/^/  - /'

# 2. commit + push（-f 因為全域 gitignore 擋 *.patch）
cd "$REPO"
git add -f "$PATCH"
if git diff --cached --quiet; then
  echo "patch 沒有變化，不需要 push"
  exit 0
fi
git -c user.name="SheldonChangL" -c user.email="sheldon.chang@jet-opto.com.tw" \
    commit -m "update patch ($(date +%Y-%m-%d))"

TOKEN=$(grep -E "GITHUB_PERSONAL_ACCESS_TOKEN" ~/.zshrc | head -1 | sed -E 's/.*=//; s/^["'"'"']//; s/["'"'"']$//')
GH_TOKEN=$TOKEN git push origin main
echo "已 push 到 https://github.com/SheldonChangL/amb82-mini-rtsp-f37"
