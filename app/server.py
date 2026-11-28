#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一键刷机 · 高通 9008 WebUI 后端

- 启动时检查高通 9008 驱动，缺失时调用官方安装包（提权）
- 9008 阶段：连接 -> 校验设备 -> 擦除 userdata/version -> 重启
- 引导用户完成激活 / 开发者选项 / USB 调试
- ADB 连上后冻结华为管控相关包
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import qedl_core as core

FROZEN = bool(getattr(sys, "frozen", False))
# 只读资源：打包后在 _MEIPASS，源码运行时是项目根目录（app/ 的上一级）
BASE = getattr(sys, "_MEIPASS", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# 可写目录：打包后是 exe 所在目录
APP_DIR = os.path.dirname(sys.executable) if FROZEN else BASE

WEB = os.path.join(BASE, "web")
LOG_DIR = os.path.join(APP_DIR, "logs")

LOADER = os.path.join(BASE, "assets", "loaders", "HuaweiCommon_680_devprg.elf")
VERSION_IMG = os.path.join(BASE, "assets", "images", "version.img.gz")
QUD_INSTALLER = os.path.join(BASE, "assets", "drivers", "QUD.WIN.1.1_Installer_10037.3_Setup.exe")
ADB_DRIVER_INSTALLER = os.path.join(BASE, "assets", "drivers", "一键安装安卓驱动.exe")
ADB_EXE = os.path.join(BASE, "tools", "platform-tools", "adb.exe")

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

APP_VERSION = "0.0.4"

REQUIRED_PARTITIONS = ["oeminfo", "super", "userdata", "version", "boot", "vbmeta"]
ERASE_TARGETS = ["userdata"]
FREEZE_PACKAGES = [
    "com.huawei.hwid",
    "com.huawei.android.hwouc",
    "com.huawei.hms.dupdateengine",
    "com.huawei.android.hsf",
]


# --------------------------------------------------------------------------
# 全局状态 / 事件
# --------------------------------------------------------------------------
class Store:
    def __init__(self):
        self.lock = threading.Lock()
        self.events = []
        self.next_id = 1
        self.phase = "idle"
        self.device = {}
        self.progress = {"value": 0, "total": 0, "label": ""}
        self.prompt = None
        self._pending = {}
        self._prompt_seq = 0
        self.running = False
        os.makedirs(LOG_DIR, exist_ok=True)
        self.logfile = os.path.join(LOG_DIR, time.strftime("run-%Y%m%d-%H%M%S.log"))
        self.adb_serial = None

    # -- events ----------------------------------------------------------
    def log(self, text, level="info"):
        with self.lock:
            ev = {
                "id": self.next_id,
                "ts": time.strftime("%H:%M:%S"),
                "level": level,
                "text": str(text),
            }
            self.next_id += 1
            self.events.append(ev)
            if len(self.events) > 800:
                self.events = self.events[-800:]
        print("[%s] %s" % (ev["ts"], text), flush=True)
        try:
            with open(self.logfile, "a", encoding="utf-8") as wf:
                wf.write("[%s] %s %s\n" % (ev["ts"], level.upper(), text))
        except Exception:
            pass

    def set_phase(self, phase, **kw):
        with self.lock:
            self.phase = phase
            if kw:
                self.device.update(kw)

    def set_progress(self, value, total, label=""):
        with self.lock:
            self.progress = {"value": value, "total": total, "label": label}

    def snapshot(self, since=0):
        with self.lock:
            return {
                "events": [e for e in self.events if e["id"] > since],
                "phase": self.phase,
                "device": self.device,
                "progress": self.progress,
                "prompt": self.prompt,
                "running": self.running,
                "adb_serial": self.adb_serial,
                "version": APP_VERSION,
                "driver_ok": self.device.get("driver_ok"),
                "edl_ports": [p[0] for p in core.list_edl_ports()],
            }

    # -- prompts ---------------------------------------------------------
    def ask(self, kind, title, text, timeout=1800):
        with self.lock:
            self._prompt_seq += 1
            pid = self._prompt_seq
            ev = threading.Event()
            self._pending[pid] = {"event": ev, "answer": None}
            self.prompt = {"id": pid, "kind": kind, "title": title, "text": text}
        self.log("等待用户确认：%s" % title, "warn")
        ev.wait(timeout)
        with self.lock:
            entry = self._pending.pop(pid, None)
            self.prompt = None
        answer = entry["answer"] if entry else None
        self.log("用户选择：%s" % ("是" if answer else "否"), "muted")
        return bool(answer)

    def answer(self, pid, value):
        with self.lock:
            entry = self._pending.get(pid)
        if not entry:
            return False
        entry["answer"] = value
        entry["event"].set()
        return True


ST = Store()


# --------------------------------------------------------------------------
# 驱动
# --------------------------------------------------------------------------
def qcom_driver_installed():
    try:
        out = subprocess.run(["pnputil", "/enum-drivers"], capture_output=True,
                             text=True, encoding="gbk", errors="replace",
                             creationflags=CREATE_NO_WINDOW, timeout=40).stdout.lower()
        return "qcser.inf" in out
    except Exception:
        return False


def install_qcom_driver():
    if not os.path.exists(QUD_INSTALLER):
        ST.log("缺少驱动安装包：%s" % QUD_INSTALLER, "error")
        return False
    ST.log("正在提权运行 Qualcomm USB 驱动安装包（可能会弹出 UAC，请点“是”）", "warn")
    ps = ("Start-Process -FilePath '%s' -ArgumentList '/s','/v/qn' -Verb RunAs -Wait"
          % QUD_INSTALLER)
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       capture_output=True, text=True, timeout=600,
                       creationflags=CREATE_NO_WINDOW)
    except Exception as exc:
        ST.log("驱动安装调用失败：%s" % exc, "error")
        return False
    time.sleep(2)
    ok = qcom_driver_installed()
    ST.log("驱动安装结果：%s" % ("成功" if ok else "未检测到 qcser.inf"), "info" if ok else "error")
    return ok


def install_adb_driver():
    if not os.path.exists(ADB_DRIVER_INSTALLER):
        ST.log("缺少 ADB 驱动安装包：%s" % ADB_DRIVER_INSTALLER, "error")
        return False
    ST.log("正在提权运行 ADB 驱动安装包（请在弹出的窗口里完成安装）", "warn")
    ps = "Start-Process -FilePath '%s' -Verb RunAs -Wait" % ADB_DRIVER_INSTALLER
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       capture_output=True, text=True, timeout=1800,
                       creationflags=CREATE_NO_WINDOW)
        return True
    except Exception as exc:
        ST.log("ADB 驱动安装调用失败：%s" % exc, "error")
        return False


# --------------------------------------------------------------------------
# ADB
# --------------------------------------------------------------------------
def adb_run(args, timeout=60, serial=None):
    cmd = [ADB_EXE] if os.path.exists(ADB_EXE) else ["adb"]
    if serial:
        cmd += ["-s", serial]
    cmd += args
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout,
                           creationflags=CREATE_NO_WINDOW)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except FileNotFoundError:
        return 127, "未找到 adb"
    except subprocess.TimeoutExpired:
        return 124, "adb 超时"


def mask_serial(serial):
    """序列号只保留前 4 位，避免日志/界面泄露。"""
    if not serial:
        return "****"
    return serial[:4] + "****"


def adb_devices():
    rc, out = adb_run(["devices"], timeout=30)
    devs = []
    for line in out.splitlines()[1:]:
        line = line.strip()
        if not line or "\t" not in line:
            continue
        serial, state = line.split("\t", 1)
        devs.append((serial.strip(), state.strip()))
    return devs


def adb_ensure_server():
    adb_run(["start-server"], timeout=60)


def freeze_packages(serial):
    ST.log("开始停用华为管控相关包（设备 %s）" % mask_serial(serial), "info")
    ok_all = True
    for pkg in FREEZE_PACKAGES:
        rc, out = adb_run(["shell", "pm", "disable-user", "--user", "0", pkg],
                          timeout=60, serial=serial)
        out = out.strip()
        good = "new state: disabled-user" in out.lower() or "disabled-user" in out.lower()
        if good:
            ST.log("已冻结 %s" % pkg, "success")
        else:
            ok_all = False
            ST.log("冻结 %s 失败：%s" % (pkg, out or "无输出"), "error")
    rc, out = adb_run(["shell", "pm", "list", "packages", "-d"], timeout=60, serial=serial)
    still = [p for p in FREEZE_PACKAGES if p in out]
    ST.log("已禁用列表校验：%s" % (", ".join(still) if still else "全部不在禁用列表？"), "muted")
    return ok_all


def watch_adb_and_freeze(timeout=1800):
    adb_ensure_server()
    ST.log("等待设备连接 ADB（请完成激活并打开 USB 调试）", "info")
    deadline = time.time() + timeout
    last_state = None
    while time.time() < deadline and ST.phase in ("guide", "adb_wait"):
        devs = adb_devices()
        if devs:
            serial, state = devs[0]
            if state != last_state:
                ST.log("检测到设备 %s（状态 %s）" % (mask_serial(serial), state), "info")
                last_state = state
            if state == "device":
                with ST.lock:
                    ST.adb_serial = serial
                ST.set_phase("freezing")
                ok = freeze_packages(serial)
                ST.set_phase("done")
                ST.log("冻结流程结束：%s" % ("成功" if ok else "存在失败项"), "success" if ok else "error")
                return ok
            if state == "unauthorized":
                ST.log("设备未授权：请在平板上点“允许 USB 调试”", "warn")
        time.sleep(2)
    if ST.phase == "guide":
        ST.log("等待 ADB 超时，可稍后点击“检测 ADB 并冻结”重试", "warn")
    return False


# --------------------------------------------------------------------------
# 9008 刷机流程
# --------------------------------------------------------------------------
def wait_for_edl(timeout=180):
    ST.log("请把平板进入 9008 模式并连接电脑，正在等待端口 ...", "info")
    deadline = time.time() + timeout
    while time.time() < deadline:
        ports = core.list_edl_ports()
        if ports:
            ST.log("检测到 9008 端口：%s" % ports[0][0], "success")
            return ports[0][0]
        time.sleep(0.5)
    return None


def open_session(port):
    session = core.EdlSession(lambda m: ST.log(m))
    session.open(port=port)
    session.identify()
    session.enter_firehose(LOADER)
    return session


def verify_device(session, gpt):
    dev = session.device
    fh = session.firehose
    problems = []
    if fh.memory.lower() != "emmc":
        problems.append("存储类型不是 eMMC（%s）" % fh.memory)
    names = {p["name"] for p in gpt["partitions"]}
    missing = [n for n in REQUIRED_PARTITIONS if n not in names]
    if missing:
        problems.append("缺少关键分区：%s" % ", ".join(missing))
    return problems


def erase_partition(session, part):
    fh = session.firehose
    ST.log("擦除 %s（LBA %d，%d 扇区）..." % (part["name"], part["start_lba"], part["sectors"]))
    t0 = time.time()
    ok = fh.erase(0, part["start_lba"], part["sectors"], timeout=600)
    if not ok:
        ST.log("loader 不支持 <erase>，改为写零（可能很慢）", "warn")
        zeros = b"\x00" * (1024 * 1024)
        done = 0
        total = part["sectors"] * fh.sector_size
        chunk_sectors = 2048
        while done < part["sectors"]:
            n = min(chunk_sectors, part["sectors"] - done)
            fh.program(0, part["start_lba"] + done, n, part["name"],
                       zeros[:n * fh.sector_size])
            done += n
            ST.set_progress(done * fh.sector_size, total, "写零 %s" % part["name"])
        ok = True
    ST.log("擦除完成：%s，用时 %.1fs" % (part["name"], time.time() - t0),
           "success" if ok else "error")
    return ok


def write_version_image(session, gpt):
    """将内置 version.img.gz 流式写入 version 分区（不足补零）。"""
    fh = session.firehose
    part = next((p for p in gpt["partitions"] if p["name"] == "version"), None)
    if part is None:
        raise core.EdlError("分区表中没有 version 分区")
    expected = part["sectors"] * fh.sector_size
    if not os.path.exists(VERSION_IMG):
        raise core.EdlError("内置 version 镜像缺失：%s" % VERSION_IMG)

    import gzip
    t0 = time.time()

    def chunks():
        with gzip.open(VERSION_IMG, "rb") as gz:
            remaining = expected
            while remaining > 0:
                data = gz.read(min(fh.max_payload, remaining))
                if not data:
                    yield b"\x00" * remaining
                    return
                yield data
                remaining -= len(data)

    written = fh.program_stream(0, part["start_lba"], part["sectors"], "version.img",
                                chunks(), expected,
                                progress=lambda done, total: ST.set_progress(done, total, "写入 version"))
    ST.log("version 回写完成：%d 字节，用时 %.1fs" % (written, time.time() - t0), "success")


def flash_worker():
    ST.running = True
    session = None
    try:
        # 1. 驱动
        ST.set_phase("checking_driver")
        if not qcom_driver_installed():
            ST.set_phase("driver_missing")
            if not ST.ask("driver", "缺少高通 9008 驱动",
                          "系统里没有检测到 Qualcomm QDLoader 驱动（qcser.inf）。是否现在安装？"):
                ST.log("用户取消驱动安装", "warn")
                ST.set_phase("idle")
                return
            if not install_qcom_driver():
                ST.set_phase("error")
                return
        with ST.lock:
            ST.device["driver_ok"] = True

        # 2. 连接
        ST.set_phase("waiting_device")
        port = wait_for_edl()
        if not port:
            ST.log("未检测到 9008 设备", "error")
            ST.set_phase("error")
            return

        ST.set_phase("connecting")
        session = open_session(port)
        dev = session.device
        fh = session.firehose
        with ST.lock:
            ST.device.update({
                "serial": ("0x%016X" % dev["serial"]) if dev.get("serial") else "N/A",
                "hwid": ("0x%016X" % dev["msm_hwid"]) if dev.get("msm_hwid") else "N/A",
                "memory": fh.memory,
                "target": fh.target or "SM6225",
            })

        # 3. 分区表 + 校验
        ST.set_phase("reading_gpt")
        gpt = session.read_gpt(0)
        with ST.lock:
            ST.device.update({
                "partitions": len(gpt["partitions"]),
                "capacity": "%.2f GB" % (gpt["total_bytes"] / (1024 ** 3)),
                "disk_guid": gpt["disk_guid"],
            })
        problems = verify_device(session, gpt)
        if problems:
            for p in problems:
                ST.log("校验失败：%s" % p, "error")
            ST.set_phase("error")
            return
        ST.log("设备校验通过：eMMC / %d 个分区 / 磁盘 GUID %s"
               % (len(gpt["partitions"]), gpt["disk_guid"]), "success")

        # 4. 擦除确认
        parts = {p["name"]: p for p in gpt["partitions"]}
        targets = [parts[n] for n in ERASE_TARGETS if n in parts]
        part_version = parts.get("version")
        ST.set_phase("erase_prompt")
        prompt = ("将擦除：userdata（%.2f GB）\n"
                  "并将 version 分区回写为内置镜像（%.1f MB）\n\n擦除后设备数据不可恢复。"
                  % (targets[0]["size_bytes"] / (1024 ** 3),
                     part_version["size_bytes"] / 1048576 if part_version else 0))
        if not ST.ask("erase", "是否擦除 userdata 并回写 version？", prompt):
            ST.log("跳过擦除与回写，直接重启设备", "warn")
        else:
            if not ST.ask("erase2", "二次确认",
                          "请再次确认：userdata 擦除后无法找回，version 将被内置镜像覆盖。"):
                ST.log("二次确认被取消，跳过擦除", "warn")
            else:
                ST.set_phase("erasing")
                for p in targets:
                    if not erase_partition(session, p):
                        ST.log("%s 擦除失败，建议进 recovery 恢复出厂设置" % p["name"], "error")
                if part_version is not None:
                    ST.set_phase("writing_version")
                    ST.log("回写内置 version 镜像 ...")
                    write_version_image(session, gpt)
                ST.set_phase("erased")

        # 5. 重启
        ST.set_phase("rebooting")
        try:
            fh.send_xml('<?xml version="1.0" ?><data><power value="reset"/></data>', timeout=5)
            ST.log("已发送重启指令，设备将断开 9008", "success")
        except Exception as exc:
            ST.log("重启指令发送失败：%s" % exc, "warn")
        session.close()
        session = None

        # 6. 进入引导阶段，后台等 ADB
        ST.set_phase("guide")
        ST.log("进入激活引导阶段：请按右侧步骤操作，完成后设备连上 ADB 即可自动冻结", "info")
        watch_adb_and_freeze()

    except core.EdlError as exc:
        ST.log("9008 阶段出错：%s" % exc, "error")
        ST.set_phase("error")
    except Exception as exc:  # noqa: BLE001
        ST.log("未预期错误：%s" % exc, "error")
        ST.log(traceback.format_exc(limit=3), "muted")
        ST.set_phase("error")
    finally:
        if session is not None:
            session.close()
        ST.running = False
        ST.set_progress(0, 0)


def adb_disable_only(wait_seconds=15):
    """仅走 ADB：探测设备并停用管控相关应用，返回结构化结果。"""
    if not os.path.exists(ADB_EXE):
        return {"ok": False, "reason": "adb_missing", "detail": ADB_EXE}
    ST.log("仅 ADB 模式：开始探测设备", "info")
    adb_ensure_server()
    deadline = time.time() + wait_seconds
    serial = state = None
    while time.time() < deadline:
        devs = adb_devices()
        if devs:
            serial, state = devs[0]
            if state == "device":
                break
        time.sleep(1)

    if not serial:
        ST.log("未检测到 ADB 设备", "warn")
        return {"ok": False, "reason": "no_device",
                "detail": "adb devices 中没有设备"}
    if state != "device":
        ST.log("设备状态为 %s，需要先在平板上允许 USB 调试" % state, "warn")
        return {"ok": False, "reason": "unauthorized" if state == "unauthorized" else state,
                "serial": serial}

    with ST.lock:
        ST.adb_serial = serial
    ST.log("检测到设备 %s，开始停用应用" % mask_serial(serial), "info")

    results = []
    for pkg in FREEZE_PACKAGES:
        rc, out = adb_run(["shell", "pm", "disable-user", "--user", "0", pkg],
                          timeout=60, serial=serial)
        text = (out or "").strip()
        ok = "disabled-user" in text.lower()
        line = text.splitlines()[-1] if text else ""
        results.append({"pkg": pkg, "ok": ok, "msg": line})
        ST.log(("已停用 " if ok else "停用失败 ") + pkg, "success" if ok else "error")

    return {"ok": all(r["ok"] for r in results), "serial": mask_serial(serial), "results": results}


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "FlashUI/1.0"

    def log_message(self, fmt, *args):  # 静音默认访问日志
        return

    # -- helpers ---------------------------------------------------------
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _json(self, data, code=200):
        self._send(code, json.dumps(data, ensure_ascii=False))

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    # -- routing ---------------------------------------------------------
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path == "/api/state":
            qs = urllib.parse.parse_qs(parsed.query)
            since = int(qs.get("since", ["0"])[0] or 0)
            self._json(ST.snapshot(since))
            return
        if path in ("/", "/index.html"):
            return self._serve("index.html")
        return self._serve(path.lstrip("/"))

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/check":
            ok = qcom_driver_installed()
            with ST.lock:
                ST.device["driver_ok"] = ok
            ST.log("驱动检查：%s" % ("已安装" if ok else "未安装"), "info" if ok else "warn")
            return self._json({"ok": ok, "ports": [p[0] for p in core.list_edl_ports()]})
        if path == "/api/install-driver":
            target = self._body().get("target", "qcom")
            ok = install_adb_driver() if target == "adb" else install_qcom_driver()
            return self._json({"ok": ok})
        if path == "/api/flash/start":
            if ST.running:
                return self._json({"ok": False, "error": "任务进行中"})
            ST.log("========== 开始一键刷机 ==========", "info")
            ST.set_phase("starting")
            threading.Thread(target=flash_worker, daemon=True).start()
            return self._json({"ok": True})
        if path == "/api/adb/disable":
            if ST.running:
                return self._json({"ok": False, "reason": "busy", "detail": "任务进行中"})
            ST.log("========== 仅 ADB 禁用 ==========", "info")
            return self._json(adb_disable_only())
        if path == "/api/answer":
            data = self._body()
            ok = ST.answer(int(data.get("id", 0)), bool(data.get("answer")))
            return self._json({"ok": ok})
        if path == "/api/reset":
            if ST.running:
                return self._json({"ok": False, "error": "任务进行中"})
            ST.set_phase("idle")
            with ST.lock:
                ST.device = {"driver_ok": qcom_driver_installed()}
            ST.set_progress(0, 0)
            ST.log("状态已重置", "muted")
            return self._json({"ok": True})
        return self._json({"ok": False, "error": "unknown api"}, 404)

    # -- static ----------------------------------------------------------
    def _serve(self, rel):
        rel = rel.replace("..", "")
        full = os.path.join(WEB, rel)
        if not os.path.exists(full) or not os.path.isfile(full):
            return self._send(404, "not found", "text/plain; charset=utf-8")
        ctypes = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".svg": "image/svg+xml",
            ".json": "application/json; charset=utf-8",
            ".png": "image/png",
            ".woff2": "font/woff2",
        }
        ctype = ctypes.get(os.path.splitext(full)[1].lower(), "application/octet-stream")
        with open(full, "rb") as rf:
            self._send(200, rf.read(), ctype)


def pick_port(start=8765):
    for port in range(start, start + 40):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError("没有可用端口")


def probe_existing():
    """已有实例在跑就直接打开浏览器。"""
    import urllib.request
    for port in range(8765, 8805):
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/api/state?since=0" % port,
                                        timeout=0.35) as resp:
                if b'"phase"' in resp.read(240):
                    return port
        except Exception:
            continue
    return None


def main():
    if FROZEN:
        running = probe_existing()
        if running:
            webbrowser.open("http://127.0.0.1:%d/" % running)
            return

    with ST.lock:
        ST.device["driver_ok"] = qcom_driver_installed()

    port = pick_port()
    url = "http://127.0.0.1:%d/" % port
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    srv.daemon_threads = True
    ST.log("服务已启动：%s" % url, "success")
    ST.log("日志文件：%s" % ST.logfile, "muted")
    if ST.device.get("driver_ok"):
        ST.log("高通 9008 驱动：已安装", "success")
    else:
        ST.log("高通 9008 驱动：未安装，开始自动安装（会弹出 UAC，请点“是”）", "warn")

        def _auto_install():
            ok = install_qcom_driver()
            with ST.lock:
                ST.device["driver_ok"] = ok

        threading.Thread(target=_auto_install, daemon=True).start()
    if not os.environ.get("FLASH_NO_BROWSER"):
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        ST.log("收到中断，退出", "muted")
    finally:
        srv.server_close()


def _fatal(exc):
    import traceback
    text = "".join(traceback.format_exception(exc))
    try:
        with open(os.path.join(APP_DIR, "启动错误.log"), "w", encoding="utf-8") as wf:
            wf.write(text)
    except Exception:
        pass
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                None, "启动失败：\n\n" + text[-800:], "一键刷机", 0x10)
        except Exception:
            pass


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        _fatal(exc)
        raise
