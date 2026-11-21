#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Headless smoke test: identify the 9008 device and dump its GPT."""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))

from qedl_core import EdlSession, EdlError

LOADER = os.path.join(ROOT, "assets", "loaders", "HuaweiCommon_680_devprg.elf")


def log(msg):
    print(msg, flush=True)


def human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return "%.2f %s" % (n, unit) if unit != "B" else "%d B" % n
        n /= 1024.0


def main():
    if not os.path.exists(LOADER):
        log("找不到引导文件: %s" % LOADER)
        return 1
    session = EdlSession(log)
    try:
        session.open()
        session.identify()
        dev = session.device
        log("序列号: %s" % (("0x%016X" % dev["serial"]) if dev.get("serial") else "N/A"))
        if dev.get("msm_hwid"):
            log("MSM HW ID: 0x%016X" % dev["msm_hwid"])
        else:
            log("MSM HW ID: N/A")

        session.enter_firehose(LOADER)
        fh = session.firehose
        storage = fh.get_storage_info()
        if storage:
            log("存储信息: %s" % storage)
        else:
            log("存储信息: (loader 不返回 getstorageinfo)")

        gpt = session.read_gpt(0)
        log("磁盘 GUID: %s" % gpt["disk_guid"])
        log("总容量(按备份 GPT): %s (%d 扇区)" % (human(gpt["total_bytes"]), gpt["total_sectors"]))
        log("分区数: %d" % len(gpt["partitions"]))
        print()
        print("%-4s %-24s %-12s %-12s %-10s %s" % ("#", "名称", "起始LBA", "结束LBA", "大小", "类型GUID"))
        for p in gpt["partitions"]:
            print("%-4d %-24s %-12d %-12d %-10s %s"
                  % (p["index"], p["name"], p["start_lba"], p["end_lba"],
                     human(p["size_bytes"]), p["type_guid"]))
        return 0
    except EdlError as exc:
        log("错误: %s" % exc)
        return 2
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
