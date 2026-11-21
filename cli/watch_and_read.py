"""拔插后自动跑一次完整回读（最短路径），结果写入 gpt.json / read_result.log。"""

import json
import os
import sys
import time

import qedl_core as core

LOADER = os.path.join(ROOT, "assets", "loaders", "HuaweiCommon_680_devprg.elf")
LOG = os.path.join(ROOT, "read_result.log")
JSON = os.path.join(ROOT, "gpt.json")

logf = open(LOG, "a", encoding="utf-8")


def log(msg):
    line = time.strftime("[%H:%M:%S] ") + str(msg)
    print(line, flush=True)
    logf.write(line + "\n")
    logf.flush()


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
    for i in range(6):
        log("状态机复位 (0x13) 第 %d 次" % (i + 1))
        hello = sah.reset_state(4.0)
        if hello:
            log("收到 HELLO version=%s mode=%s" % (hello["version"], hello["mode"]))
            return hello
        time.sleep(0.4)
    raise core.EdlError("目标无响应，需要拔插 USB 重新枚举")


def wait_for_replug():
    if core.list_edl_ports():
        log("设备仍占用端口，等待拔掉数据线 ...")
        end = time.time() + 600
        while time.time() < end and core.list_edl_ports():
            time.sleep(0.2)
        if core.list_edl_ports():
            raise core.EdlError("超时：设备一直没拔掉")
        log("已拔出，等待插回 ...")
    else:
        log("等待 9008 端口出现 ...")

    end = time.time() + 1200
    while time.time() < end:
        ports = core.list_edl_ports()
        if ports:
            log("检测到 %s" % ports[0][0])
            return ports[0][0]
        time.sleep(0.2)
    raise core.EdlError("超时：未等到 9008 端口")


def main():
    try:
        port = wait_for_replug()
        time.sleep(1.5)
        link = core.SerialLink(port, log)
    except core.EdlError as exc:
        log(str(exc))
        return 1
    try:
        sah = core.Sahara(link, log)
        for attempt in range(3):
            try:
                recover(sah)
                log("序列号 / MSM ID 需要命令模式，本次跳过；直接上传引导")
                sah.upload_loader(LOADER)
                break
            except core.EdlError as exc:
                log("上传失败: %s" % exc)
                if attempt == 2:
                    raise
                time.sleep(0.6)

        fh = core.Firehose(link, log)
        attrs = fh.configure("emmc")
        log("configure: MemoryName=%s MaxPayload=%s"
            % (attrs.get("MemoryName"), attrs.get("MaxPayloadSizeToTargetInBytes")))
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
        with open(JSON, "w", encoding="utf-8") as wf:
            json.dump(gpt, wf, ensure_ascii=False, indent=2)
        log("已保存 %s" % JSON)
        log("=== 回读成功 ===")
        return 0
    except Exception as exc:  # noqa: BLE001
        log("失败: %s: %s" % (type(exc).__name__, exc))
        return 2
    finally:
        link.close()


if __name__ == "__main__":
    sys.exit(main())
