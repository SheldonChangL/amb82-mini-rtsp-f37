#!/bin/bash
# 重新從 SDK 工作目錄產生 patch 並 push 到 GitHub。
# 用法：改完 ameba-rtos-pro2 裡的東西後，執行 ./update-patch.sh
set -e

SDK="$HOME/Projects/FW/RTL8735B/ameba-rtos-pro2"
REPO="$HOME/Projects/FW/RTL8735B/amb82-mini-rtsp-f37"
PATCH="amb82-mini-f37-rtsp.patch"

# 1. 從 SDK 目前未 commit 的改動產生 patch
cd "$SDK"
git diff > "$REPO/$PATCH"
echo "已更新 $PATCH（$(grep -c '^diff' "$REPO/$PATCH") 個檔有改動）"

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
