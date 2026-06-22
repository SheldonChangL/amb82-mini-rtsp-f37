#!/usr/bin/env python3
"""
AMB82-mini 韌體「功能模式」選擇 build 工具。

一個指令切換不同功能韌體,自動處理兩件容易漏的事:
  (1) active 範例(example_mmf2_video_surport 裡呼叫哪個 *_init)
  (2) FWFS NN 模型打包清單(amebapro2_fwfs_nn_models.json 的 "files")
這兩個若不一致,開機會 nbg bad / vipnn not applied / 無畫面。

用法:
    python3 build_firmware.py <mode>            # 設定 + build flash_nn
    python3 build_firmware.py <mode> --no-build # 只改設定不 build(看會改什麼)
    python3 build_firmware.py --list            # 列出所有模式

以後新增功能:在 MODES 加一筆(init 函式 + 要打包的模型)即可。
NN 分區僅 5.88MB,模型清單請確認加總放得下(臉部與手勢無法同時)。
"""
import os
import re
import json
import sys
import argparse
import subprocess

HOME = os.path.expanduser("~")
SDK = os.path.join(HOME, "Projects/FW/RTL8735B/ameba-rtos-pro2")
TOOLCHAIN_BIN = os.path.join(HOME, "Projects/FW/RTL8735B/toolchain/asdk-10.3.0/darwin/newlib/bin")
EX = os.path.join(SDK, "project/realtek_amebapro2_v0_example")
SURPORT = os.path.join(EX, "src/mmfv2_video_example/video_example_media_framework.c")
FWFS = os.path.join(EX, "GCC-RELEASE/mp/amebapro2_fwfs_nn_models.json")
GCC = os.path.join(EX, "GCC-RELEASE")
BUILD = os.path.join(GCC, "build")
PGTOOL = os.path.join(SDK, "tools/Pro2_PG_tool _v1.4.3")

# 每個功能模式 = active 範例 + 要打包的 NN 模型(模型 key 對應 fwfs json 的 alias)
MODES = {
    "office": {
        "init": "mmf2_video_example_face_rtsp_init",
        "models": ["scrfd320p", "mobilefacenet_i8"],
        "desc": "人臉辨識 → 鎖定/解鎖 + 在場自動化",
    },
    "gesture": {
        "init": "mmf2_video_example_vipnn_handgesture_init",
        "models": ["palm_detection_lite_int16", "hand_landmark_lite_int16"],
        "desc": "揮手 → 鍵盤方向鍵",
    },
    "rtsp": {  # 純串流,不用 NN(target 仍可用 flash_nn,FWFS 留空)
        "init": "mmf2_video_example_v1_init",
        "models": [],
        "desc": "單純 1080p RTSP 串流(無 AI)",
    },
}


def set_active_example(init):
    src = open(SURPORT).read()
    pat = r"static void example_mmf2_video_surport\(void\)\s*\{.*?\n\}"
    if not re.search(pat, src, re.S):
        sys.exit("✗ 找不到 example_mmf2_video_surport(),無法設定 active 範例")
    new_func = (
        "static void example_mmf2_video_surport(void)\n{\n"
        f"\t{init}();\n"
        "\tvideo_init_done = 1;\n"
        "}"
    )
    # 無條件替換(可能與原內容相同 → 冪等;不可拿 src2==src 當失敗判斷)
    src2 = re.sub(pat, lambda _m: new_func, src, count=1, flags=re.S)
    open(SURPORT, "w").write(src2)


def set_models(models):
    cfg = json.load(open(FWFS))
    cfg["FWFS"]["files"] = models
    # 確認每個 model key 都有 alias 定義
    for k in models:
        if k not in cfg:
            sys.exit(f"✗ 模型 '{k}' 在 {os.path.basename(FWFS)} 沒有對應 alias 定義")
    json.dump(cfg, open(FWFS, "w"), indent=4, ensure_ascii=False)
    open(FWFS, "a").write("\n")


def build():
    env = dict(os.environ)
    env["PATH"] = TOOLCHAIN_BIN + ":" + env["PATH"]
    os.makedirs(BUILD, exist_ok=True)
    nproc = str(os.cpu_count() or 4)
    subprocess.run(
        ["cmake", "..", "-G", "Unix Makefiles",
         "-DCMAKE_TOOLCHAIN_FILE=../toolchain.cmake", "-DVIDEO_EXAMPLE=ON"],
        cwd=BUILD, env=env, check=True)
    subprocess.run(
        ["cmake", "--build", ".", "--target", "flash_nn", "--", "-j", nproc],
        cwd=BUILD, env=env, check=True)


def main():
    ap = argparse.ArgumentParser(description="AMB82 韌體功能模式選擇 build")
    ap.add_argument("mode", nargs="?", choices=list(MODES), help="要 build 的功能模式")
    ap.add_argument("--no-build", action="store_true", help="只改設定不 build")
    ap.add_argument("--list", action="store_true", help="列出所有模式")
    a = ap.parse_args()

    if a.list or not a.mode:
        print("可用模式:")
        for k, v in MODES.items():
            print(f"  {k:10s} — {v['desc']}  (models: {', '.join(v['models']) or '無'})")
        return

    m = MODES[a.mode]
    print(f"=== 模式: {a.mode} — {m['desc']}")
    set_active_example(m["init"])
    set_models(m["models"])
    print(f"  active 範例: {m['init']}()")
    print(f"  NN 模型清單: {m['models'] or '(空)'}")

    if a.no_build:
        print("(--no-build:只改設定,未 build)")
        return

    print("=== building (flash_nn) ...")
    build()
    binp = os.path.join(BUILD, "flash_ntz.nn.bin")
    print(f"\n✅ 完成: {binp}")
    sz = os.path.getsize(binp) // (1024 * 1024) if os.path.exists(binp) else 0
    print(f"   ({sz} MB)")
    print("\n燒錄(板子先進燒錄模式:左壓 UART_DOWNLOAD → 右點 RESET → 左放;port 換實際的):")
    print(f"  cd '{PGTOOL}'")
    print(f"  ./uartfwburn.darwin -p /dev/cu.usbserial-XXX -f '{binp}' -b 2000000 -U -x 32 -r")


if __name__ == "__main__":
    main()
