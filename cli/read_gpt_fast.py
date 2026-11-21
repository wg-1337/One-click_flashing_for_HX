"""最短路径：复位 -> 上传引导 -> configure -> 读 GPT（跳过命令模式识别）。"""

import os
import sys
import time

import qedl_core as core

LOADER = os.path.join(ROOT, "assets", "loaders", "HuaweiCommon_680_devprg.elf")


def log(msg):
    print(time.strftime("[%H:%M:%S] ") + str(msg), flush=True)


def human(n):
    step = 1024.0
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < step or unit == "TB":
            return ("%d B" % n) if unit == "B" else ("%.2f %s" % (n, unit))
        n /= step


def recover(sah):
    hello = sah.try_hello(1.5)
    if hello:
        log("HELLO 已就绪 version=%s" % hello["version"])
        return hello
    for i in range(3):
        log("状态机复位 (0x13) 第 %d 次" % (i + 1))
        hello = sah.reset_state(5.0)
        if hello:
            log("收到 HELLO version=%s model=%s" % (hello["version"], hello["mode"]))
            return hello
        time.sleep(0.5)
    raise core.EdlError("目标无响应，需要拔插 USB 重新枚举")


def main():
    link = core.SerialLink("COM3", log)
    try:
        sah = core.Sahara(link, log)
        for attempt in range(3):
            try:
                recover(sah)
                log("上传引导 (第 %d 次) ..." % (attempt + 1))
                sah.upload_loader(LOADER, progress=lambda p, t: None)
                break
            except core.EdlError as exc:
                log("上传失败: %s" % exc)
                if attempt == 2:
                    raise
                time.sleep(0.6)
        log("引导已运行，进入 Firehose")
        fh = core.Firehose(link, log)
        attrs = fh.configure("emmc")
        log("configure: %s" % attrs)
        storage = fh.get_storage_info()
        if storage:
            log("storage_info: %s" % storage)
        gpt = core.parse_gpt(fh)
        log("磁盘 GUID: %s" % gpt["disk_guid"])
        log("容量: %s (%d 扇区)" % (human(gpt["total_bytes"]), gpt["total_sectors"]))
        log("分区数: %d" % len(gpt["partitions"]))
        for p in gpt["partitions"]:
            log("  #%-3d %-24s LBA %-11d %-11d %-10s %s"
                % (p["index"], p["name"], p["start_lba"], p["sectors"],
                   human(p["size_bytes"]), p["type_guid"]))
        import json
        with open(os.path.join(ROOT, "gpt.json"), "w", encoding="utf-8") as wf:
            json.dump(gpt, wf, ensure_ascii=False, indent=2)
        log("已保存 gpt.json")
        log("=== 成功 ===")
        return 0
    finally:
        link.close()


if __name__ == "__main__":
    sys.exit(main())
