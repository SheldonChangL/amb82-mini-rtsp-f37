#!/usr/bin/env python3
"""
靜音 prebuilt libnn.a 的逐幀 debug 噪音(handgesture 模式的 ">>> rotate tick %f")。

那行 printf 在 Realtek 的 prebuilt NN 函式庫 libnn.a 裡,不在原始碼 → 無法註解。
這支把該格式字串就地清成空字串(同長度,不破壞 .a 結構),printf 變印空字串 = 不輸出。
跑完要重新 build(relink 才會帶到 patch 後的 libnn.a)。會先備份 libnn.a.bak。

用法: python3 silence_libnn.py
注意: 這是修改本機 prebuilt 的 hack,重新 clone SDK 後需再跑一次(可重複執行,冪等)。
"""
import os
import sys

HOME = os.path.expanduser("~")
LIBNN = os.path.join(
    HOME,
    "Projects/FW/RTL8735B/ameba-rtos-pro2/project/realtek_amebapro2_v0_example"
    "/GCC-RELEASE/application/output/libnn.a",
)
# 要清掉的格式字串(出現在 handgesture 旋轉處理,逐幀印)
TARGETS = [
    b">>>>>>>>>>>>>>>>>>>>>>>>> rotate tick %f",
]


def main():
    if not os.path.exists(LIBNN):
        sys.exit(f"✗ 找不到 {LIBNN}")
    data = bytearray(open(LIBNN, "rb").read())
    total = 0
    for pat in TARGETS:
        start = 0
        while True:
            i = data.find(pat, start)
            if i < 0:
                break
            # 整段就地清為 0x00(同長度;printf 讀到開頭 0x00 = 空字串,不輸出)
            for j in range(i, i + len(pat)):
                data[j] = 0
            total += 1
            start = i + len(pat)
    if total == 0:
        print("已經乾淨(找不到目標字串,可能先前已 patch)。")
        return
    bak = LIBNN + ".bak"
    if not os.path.exists(bak):
        open(bak, "wb").write(open(LIBNN, "rb").read())
        print(f"已備份: {bak}")
    open(LIBNN, "wb").write(data)
    print(f"✓ 已清掉 {total} 處 'rotate tick' 格式字串 → 重新 build 即生效")
    print("  (build_firmware.py gesture 會 relink 帶到 patch 後的 libnn.a)")


if __name__ == "__main__":
    main()
