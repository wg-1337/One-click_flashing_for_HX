#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Qualcomm EDL (9008) core for the one-click flasher.

Implements the Sahara handshake and the Firehose XML protocol on top of a
plain COM port (Qualcomm QDLoader driver), so it works with the driver that
ships in drivers\\ without libusb/WinUSB.

Only the parts needed by the GUI live here: identify the device, upload a
firehose programmer, read the GPT, read/program raw partitions.
"""

from __future__ import annotations

import os
import re
import struct
import time

import serial
import serial.tools.list_ports

VID_QUALCOMM = 0x05C6
PID_EDL = (0x9008, 0x9006)

SECTOR = 512


class EdlError(Exception):
    pass


# --------------------------------------------------------------------------
# port discovery
# --------------------------------------------------------------------------
def list_edl_ports():
    """Return [(device, description, vid, pid), ...] for 9008-ish COM ports."""
    found = []
    for p in serial.tools.list_ports.comports():
        desc = p.description or ""
        is_qcom = (p.vid == VID_QUALCOMM and p.pid in PID_EDL)
        looks_edl = ("9008" in desc) or ("9006" in desc) or ("QDLoader" in desc) \
            or ("QDLOADER" in desc.upper())
        if is_qcom or looks_edl:
            found.append((p.device, desc, p.vid, p.pid))
    return found


def wait_edl_port(timeout=20.0, prefer=None):
    end = time.time() + timeout
    last = []
    while time.time() < end:
        ports = list_edl_ports()
        last = ports
        if ports:
            if prefer:
                for dev, desc, vid, pid in ports:
                    if dev == prefer:
                        return dev
            return ports[0][0]
        time.sleep(0.3)
    raise EdlError("no 9008 port found (tried %d times); last=%r" % (int(timeout / 0.3), last))


# --------------------------------------------------------------------------
# serial transport
# --------------------------------------------------------------------------
class SerialLink:
    def __init__(self, port, log=None):
        self.log = log or (lambda *_: None)
        self.port = port
        self.buf = b""
        self.ser = serial.Serial()
        self.ser.port = port
        self.ser.baudrate = 115200
        self.ser.bytesize = serial.EIGHTBITS
        self.ser.parity = serial.PARITY_NONE
        self.ser.stopbits = serial.STOPBITS_ONE
        self.ser.timeout = 0.05
        self.ser.write_timeout = 30
        self.ser.xonxoff = False
        self.ser.rtscts = False
        self.ser.dsrdtr = False
        self.ser.open()
        try:
            self.ser.dtr = True
            self.ser.rts = True
        except Exception:
            pass

    def close(self):
        try:
            if self.ser.is_open:
                self.ser.close()
        except Exception:
            pass

    def write(self, data: bytes):
        if not data:
            return
        self.ser.write(data)
        self.ser.flush()

    def read_exact(self, n: int, timeout: float = 15.0) -> bytes:
        end = time.time() + timeout
        while len(self.buf) < n:
            remain = end - time.time()
            if remain <= 0:
                raise EdlError("read timeout: wanted %d bytes, got %d" % (n, len(self.buf)))
            self.ser.timeout = min(0.2, max(0.01, remain))
            chunk = self.ser.read(n - len(self.buf))
            if chunk:
                self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def read_until(self, marker: bytes, timeout: float = 15.0) -> bytes:
        end = time.time() + timeout
        while marker not in self.buf:
            remain = end - time.time()
            if remain <= 0:
                raise EdlError("read timeout waiting for %r (have %d bytes)" % (marker, len(self.buf)))
            self.ser.timeout = min(0.2, max(0.01, remain))
            chunk = self.ser.read(4096)
            if chunk:
                self.buf += chunk
        idx = self.buf.index(marker) + len(marker)
        out, self.buf = self.buf[:idx], self.buf[idx:]
        return out

    def discard_input(self, wait: float = 0.25) -> int:
        """Throw away stale packets left over from an aborted Sahara run."""
        dropped = len(self.buf)
        self.buf = b""
        end = time.time() + wait
        while time.time() < end:
            self.ser.timeout = min(0.05, max(0.01, end - time.time()))
            chunk = self.ser.read(4096)
            if chunk:
                dropped += len(chunk)
            else:
                break
        return dropped


# --------------------------------------------------------------------------
# Sahara
# --------------------------------------------------------------------------
class Sahara:
    HELLO_REQ = 0x01
    HELLO_RSP = 0x02
    READ_DATA = 0x03
    END_TRANSFER = 0x04
    DONE_REQ = 0x05
    DONE_RSP = 0x06
    EXECUTE_REQ = 0x0D
    EXECUTE_RSP = 0x0E
    EXECUTE_DATA = 0x0F

    MODE_IMAGE_TX_PENDING = 0x00
    MODE_IMAGE_TX_COMPLETE = 0x01
    MODE_MEMORY_DEBUG = 0x02
    MODE_COMMAND = 0x03

    EXEC_SERIAL = 0x01
    EXEC_MSM_HW_ID = 0x02
    EXEC_PKHASH = 0x03

    def __init__(self, link: SerialLink, log=None):
        self.link = link
        self.log = log or (lambda *_: None)
        self.version = 2

    # -- handshake -------------------------------------------------------
    def wait_hello(self, timeout: float = 20.0):
        hdr = self.link.read_exact(8, timeout)
        cmd, length = struct.unpack("<II", hdr)
        if cmd != self.HELLO_REQ:
            raise EdlError("expected Sahara HELLO, got packet 0x%x" % cmd)
        body = self.link.read_exact(length - 8, timeout)
        version, versup, pktlen, mode = struct.unpack("<IIII", body[:16])
        self.version = min(version, 2) if version else 2
        return {
            "version": version,
            "version_supported": versup,
            "cmd_packet_length": pktlen,
            "mode": mode,
        }

    def try_hello(self, timeout: float = 2.0):
        """Read the target's HELLO if it is still buffered; None if silent.

        A PBL only announces itself once per USB attach, so after a driver
        rebind (or a previous aborted run) the port stays silent and the host
        has to send HELLO_RSP anyway.
        """
        try:
            return self.wait_hello(timeout)
        except EdlError:
            return None

    def wait_packet(self, timeout: float = 3.0):
        """Read one Sahara packet, return (cmd, body)."""
        hdr = self.link.read_exact(8, timeout)
        cmd, length = struct.unpack("<II", hdr)
        body = self.link.read_exact(length - 8, timeout) if length > 8 else b""
        return cmd, body

    def enter_command_mode(self, timeout: float = 3.0):
        """Handshake with MODE_COMMAND so the identifiers can be queried."""
        self.send_hello(self.MODE_COMMAND)
        try:
            cmd, body = self.wait_packet(timeout)
        except EdlError:
            return False
        if cmd == self.HELLO_REQ:          # target re-announced itself
            self.send_hello(self.MODE_COMMAND)
            try:
                cmd, body = self.wait_packet(timeout)
            except EdlError:
                return False
        return cmd == 0x0B  # SAHARA_CMD_READY

    def reset_state(self, timeout: float = 3.0):
        """SAHARA_RESET_STATE_MACHINE_ID (0x13).

        The only reliable way back from a NAK/END_TRANSFER state: the PBL
        restarts Sahara and re-broadcasts its HELLO packet.
        """
        self.link.write(struct.pack("<II", 0x13, 8))
        try:
            return self.wait_hello(timeout)
        except EdlError:
            return None

    def send_hello(self, mode, version=2, min_version=1):
        # same field layout edlclient uses; the target only checks mode/version
        pkt = struct.pack("<12I", self.HELLO_RSP, 0x30, version, min_version, 0,
                          mode, 1, 2, 3, 4, 5, 6)
        self.link.write(pkt)

    # -- extended commands ----------------------------------------------
    def exec_cmd(self, mcmd: int, timeout: float = 5.0):
        self.link.write(struct.pack("<III", self.EXECUTE_REQ, 0x0C, mcmd))
        hdr = self.link.read_exact(8, timeout)
        cmd, length = struct.unpack("<II", hdr)
        body = self.link.read_exact(length - 8, timeout) if length > 8 else b""
        if cmd != self.EXECUTE_RSP:
            return None
        data_len = struct.unpack("<I", body[4:8])[0] if len(body) >= 8 else 0
        if data_len:
            self.link.write(struct.pack("<III", self.EXECUTE_DATA, 0x0C, mcmd))
            return self.link.read_exact(data_len, timeout)
        return b""

    def get_serial(self):
        res = self.exec_cmd(self.EXEC_SERIAL)
        if not res:
            return None
        return int.from_bytes(res, "little")

    def get_msm_hwid(self):
        res = self.exec_cmd(self.EXEC_MSM_HW_ID)
        if not res:
            return None
        return int.from_bytes(res[:8], "little")

    def get_pkhash(self):
        res = self.exec_cmd(self.EXEC_PKHASH)
        if not res:
            return None
        # the payload repeats the first dword right after the real hash
        idx = res[4:].find(res[:4])
        if idx != -1:
            res = res[:4 + idx]
        return res.hex()

    # -- programmer upload ----------------------------------------------
    def upload_loader(self, path, progress=None, timeout=30.0):
        with open(path, "rb") as rf:
            image = rf.read()
        # stale READ_DATA/END_TRANSFER packets from an aborted transfer would
        # be answered with the wrong payload size (NAK 0x8), so start clean
        dropped = self.link.discard_input(0.25)
        if dropped:
            self.log("丢弃 %d 字节残留数据" % dropped)
        self.send_hello(self.MODE_IMAGE_TX_PENDING)
        total = len(image)
        while True:
            hdr = self.link.read_exact(8, timeout)
            cmd, length = struct.unpack("<II", hdr)
            body = self.link.read_exact(length - 8, timeout) if length > 8 else b""
            if cmd == self.HELLO_REQ:
                # target re-announced itself; answer once more and keep going
                self.send_hello(self.MODE_IMAGE_TX_PENDING)
                continue
            if cmd == self.READ_DATA:
                image_id, offset, dlen = struct.unpack("<III", body[:12])
            elif cmd == 0x12:  # SAHARA_64BIT_MEMORY_READ_DATA (image_id is a qword)
                image_id, offset, dlen = struct.unpack("<QQQ", body[:24])
            elif cmd == self.END_TRANSFER:
                image_id, status = struct.unpack("<II", body[:8])
                if status != 0:
                    raise EdlError("loader rejected by target, status 0x%x" % status)
                break
            elif cmd == 0:
                raise EdlError("target sent an empty packet during upload")
            else:
                raise EdlError("unexpected Sahara packet 0x%x during upload" % cmd)

            chunk = image[offset:offset + dlen]
            if offset > len(image) or dlen > len(image) + 0x10000:
                raise EdlError("sanity: bad read request image_id=%s offset=%s len=%s "
                               "(packet body=%s)" % (image_id, offset, dlen, body[:24].hex()))
            if len(chunk) < dlen:
                chunk += b"\xff" * (dlen - len(chunk))
            self.link.write(chunk)
            if progress:
                progress(min(offset + dlen, total), total)

        # DONE handshake, then the target jumps into the programmer
        self.link.write(struct.pack("<II", self.DONE_REQ, 8))
        hdr = self.link.read_exact(8, timeout)
        cmd, length = struct.unpack("<II", hdr)
        if length > 8:
            self.link.read_exact(length - 8, timeout)
        if cmd not in (self.DONE_RSP, self.END_TRANSFER):
            raise EdlError("unexpected packet 0x%x after upload" % cmd)
        return True


# --------------------------------------------------------------------------
# Firehose
# --------------------------------------------------------------------------
RESP_RE = re.compile(r"<response\s+([^/>]*)/?>", re.I)
LOG_RE = re.compile(r'<log\s+value="([^"]*)"', re.I)
ATTR_RE = re.compile(r'(\w+)="([^"]*)"')


class Firehose:
    def __init__(self, link: SerialLink, log=None):
        self.link = link
        self.log = log or (lambda *_: None)
        self.max_payload = 1048576
        self.max_xml = 4096
        self.sector_size = SECTOR
        self.memory = "emmc"
        self.target = ""
        self.info = {}

    # -- low level -------------------------------------------------------
    def _read_xml(self, timeout=30.0) -> str:
        end = time.time() + timeout
        while time.time() < end:
            block = self.link.read_until(b"</data>", timeout=end - time.time())
            if b"<response" in block:
                return block.decode("utf-8", "replace")
        raise EdlError("firehose response timeout")

    def send_xml(self, xml: str, timeout=30.0) -> dict:
        self.link.write(xml.encode("utf-8"))
        txt = self._read_xml(timeout)
        m = RESP_RE.search(txt)
        attrs = dict(ATTR_RE.findall(m.group(1))) if m else {}
        logs = LOG_RE.findall(txt)
        return {"attrs": attrs, "logs": logs, "raw": txt}

    def check(self, resp, what):
        val = resp["attrs"].get("value", "")
        if val.upper() != "ACK":
            detail = "; ".join(resp["logs"][:3]) or resp["raw"][:200]
            raise EdlError("%s failed: %s (%s)" % (what, val or "no response", detail))
        return resp

    # -- commands --------------------------------------------------------
    def configure(self, memory="emmc"):
        xml = ('<?xml version="1.0" encoding="UTF-8" ?><data>'
               '<configure MemoryName="%s" Verbose="0" AlwaysValidate="0" '
               'MaxDigestTableSizeInBytes="2048" MaxPayloadSizeToTargetInBytes="1048576" '
               'ZLPAwareHost="1" SkipStorageInit="0" SkipWrite="0"/></data>' % memory)
        resp = self.send_xml(xml)
        attrs = resp["attrs"]
        if attrs.get("value", "").upper() != "ACK":
            detail = "; ".join(resp["logs"][:3]) or resp["raw"][:200]
            raise EdlError("configure failed: %s" % detail)
        self.info.update(attrs)
        self.memory = attrs.get("MemoryName", memory)
        self.target = attrs.get("TargetName", "")
        for key in ("MaxPayloadSizeToTargetInBytes",):
            if key in attrs:
                try:
                    self.max_payload = int(attrs[key])
                except ValueError:
                    pass
        if "MaxXMLSizeInBytes" in attrs:
            try:
                self.max_xml = int(attrs["MaxXMLSizeInBytes"])
            except ValueError:
                pass
        if self.memory.lower() in ("ufs",):
            self.sector_size = 4096
        return attrs

    def nop(self):
        return self.send_xml('<?xml version="1.0" ?><data><nop/></data>')

    def get_storage_info(self):
        try:
            resp = self.send_xml('<?xml version="1.0" ?><data><getstorageinfo/></data>')
        except EdlError:
            return None
        text = resp["raw"] + " ".join(resp["logs"])
        info = {}
        idx = text.find("storage_info")
        if idx != -1:
            start = text.rfind("{", 0, idx)
            end = text.find("}", idx)
            import json
            while start != -1 and end != -1:
                try:
                    info = json.loads(text[start:end + 1]).get("storage_info", {})
                    break
                except Exception:
                    end = text.find("}", end + 1)
        return info or None

    def read(self, lun, start_sector, num_sectors, timeout=120.0) -> bytes:
        nbytes = num_sectors * self.sector_size
        xml = ('<?xml version="1.0" ?><data><read SECTOR_SIZE_IN_BYTES="%d" '
               'num_partition_sectors="%d" physical_partition_number="%d" '
               'start_sector="%d"/></data>'
               % (self.sector_size, num_sectors, lun, start_sector))
        self.link.write(xml.encode("utf-8"))
        ack = self._read_xml(timeout=30)
        if 'value="ACK"' not in ack:
            raise EdlError("read rejected: %s" % ack[:200])
        data = self.link.read_exact(nbytes, timeout)
        # trailing ack (some programmers omit it; do not fail the read)
        try:
            self._read_xml(timeout=10)
        except EdlError:
            pass
        return data

    def program_stream(self, lun, start_sector, num_sectors, filename, chunks,
                       total, progress=None, timeout=600.0):
        """流式 <program>：chunks 产出数据块，边收边写，适合大镜像。"""
        xml = ('<?xml version="1.0" ?><data><program SECTOR_SIZE_IN_BYTES="%d" '
               'num_partition_sectors="%d" physical_partition_number="%d" '
               'start_sector="%d" filename="%s"/></data>'
               % (self.sector_size, num_sectors, lun, start_sector,
                  os.path.basename(filename)))
        resp = self.send_xml(xml)
        self.check(resp, "program start")
        written = 0
        for chunk in chunks:
            if not chunk:
                continue
            if len(chunk) % self.sector_size:
                chunk += b"\x00" * (self.sector_size - len(chunk) % self.sector_size)
            self.link.write(chunk)
            written += len(chunk)
            if progress:
                progress(min(written, total), total)
        resp = self._read_xml(timeout=timeout)
        m = RESP_RE.search(resp)
        attrs = dict(ATTR_RE.findall(m.group(1))) if m else {}
        if attrs.get("value", "").upper() != "ACK":
            raise EdlError("program stream failed: %s" % resp[:200])
        return written

    def erase(self, lun, start_sector, num_sectors, timeout=180.0):
        """<erase> (fast trim); returns True on ACK, False if unsupported."""
        xml = ('<?xml version="1.0" ?><data><erase SECTOR_SIZE_IN_BYTES="%d" '
               'num_partition_sectors="%d" physical_partition_number="%d" '
               'start_sector="%d"/></data>'
               % (self.sector_size, num_sectors, lun, start_sector))
        resp = self.send_xml(xml, timeout=timeout)
        return resp["attrs"].get("value", "").upper() == "ACK"

    def program(self, lun, start_sector, num_sectors, filename, data, progress=None):
        xml = ('<?xml version="1.0" ?><data><program SECTOR_SIZE_IN_BYTES="%d" '
               'num_partition_sectors="%d" physical_partition_number="%d" '
               'start_sector="%d" filename="%s"/></data>'
               % (self.sector_size, num_sectors, lun, start_sector,
                  os.path.basename(filename)))
        resp = self.send_xml(xml)
        self.check(resp, "program start")
        total = len(data)
        pos = 0
        while pos < total:
            chunk = data[pos:pos + self.max_payload]
            if len(chunk) % self.sector_size:
                chunk += b"\x00" * (self.sector_size - len(chunk) % self.sector_size)
            self.link.write(chunk)
            pos += len(chunk)
            if progress:
                progress(min(pos, total), total)
        self.link.write(b"\x00" * 0)
        resp = self._read_xml(timeout=180)
        m = RESP_RE.search(resp)
        attrs = dict(ATTR_RE.findall(m.group(1))) if m else {}
        if attrs.get("value", "").upper() != "ACK":
            raise EdlError("program failed: %s" % resp[:200])


# --------------------------------------------------------------------------
# GPT
# --------------------------------------------------------------------------
def _guid(raw: bytes) -> str:
    a, b, c = struct.unpack("<IHH", raw[:8])
    d = struct.unpack(">H", raw[8:10])[0]
    e = raw[10:16].hex().upper()
    return "%08X-%04X-%04X-%04X-%s" % (a, b, c, d, e)


def parse_gpt(fh: Firehose, lun=0, log=None):
    hdr = fh.read(lun, 1, 1)
    if hdr[:8] != b"EFI PART":
        raise EdlError("no GPT signature on LUN%d (got %r)" % (lun, hdr[:8]))
    (header_size,) = struct.unpack("<I", hdr[12:16])
    current_lba, backup_lba, first_usable, last_usable = struct.unpack("<QQQQ", hdr[24:56])
    disk_guid = _guid(hdr[56:72])
    part_entry_lba, num_entries, entry_size = struct.unpack("<QII", hdr[72:88])
    sectors = (num_entries * entry_size + SECTOR - 1) // SECTOR
    entries = fh.read(lun, part_entry_lba, sectors)

    parts = []
    for i in range(num_entries):
        off = i * entry_size
        raw = entries[off:off + entry_size]
        if len(raw) < 128 or raw[:16] == b"\x00" * 16:
            continue
        type_guid = _guid(raw[0:16])
        uniq = _guid(raw[16:32])
        first_lba, last_lba, flags = struct.unpack("<QQQ", raw[32:56])
        name = raw[56:128].decode("utf-16-le", "ignore").split("\x00")[0]
        parts.append({
            "index": len(parts) + 1,
            "name": name,
            "start_lba": first_lba,
            "end_lba": last_lba,
            "sectors": last_lba - first_lba + 1 if last_lba >= first_lba else 0,
            "size_bytes": (last_lba - first_lba + 1) * SECTOR if last_lba >= first_lba else 0,
            "type_guid": type_guid,
            "uuid": uniq,
            "flags": flags,
        })

    total_sectors = last_usable + 1 + (backup_lba - last_usable)
    return {
        "lun": lun,
        "disk_guid": disk_guid,
        "current_lba": current_lba,
        "backup_lba": backup_lba,
        "first_usable": first_usable,
        "last_usable": last_usable,
        "entry_count": num_entries,
        "entry_size": entry_size,
        "partitions": parts,
        "total_sectors": total_sectors,
        "total_bytes": (backup_lba + 1) * SECTOR,
        "header_size": header_size,
    }


# --------------------------------------------------------------------------
# high level session
# --------------------------------------------------------------------------
class EdlSession:
    """Owns port + Sahara + Firehose for the GUI."""

    def __init__(self, log=None):
        self.log = log or (lambda *_: None)
        self.link = None
        self.port = None
        self.sahara = None
        self.firehose = None
        self.device = {}

    def open(self, port=None, loader=None, timeout=25.0):
        self.port = port or wait_edl_port(timeout)
        self.log("打开端口 %s" % self.port)
        self.link = SerialLink(self.port)
        self.sahara = Sahara(self.link, self.log)
        hello = self.sahara.try_hello(1.5)
        if hello is None:
            self.log("端口静默，发送状态机复位(0x13)触发重新握手")
            hello = self.sahara.reset_state()
            if hello is None:
                time.sleep(0.8)
                hello = self.sahara.reset_state()
        if hello is None:
            self.log("复位后仍无 HELLO，稍后由主机盲发握手")
        else:
            self.log("Sahara 握手成功: version=%s mode=%s"
                     % (hello["version"], hello["mode"]))
            self.device["sahara"] = hello
        return hello

    def identify(self):
        """Sahara-level identifiers (serial / MSM hw id / pkhash)."""
        dev = self.device
        ok = self.sahara.enter_command_mode()
        if not ok:
            self.log("未进入命令模式，状态机复位后重试")
            if self.sahara.reset_state() is not None:
                ok = self.sahara.enter_command_mode()
        if not ok:
            self.log("目标未进入命令模式，跳过序列号 / MSM ID 读取")
            return dev
        self.log("已进入 Sahara 命令模式")
        try:
            dev["serial"] = self.sahara.get_serial()
        except EdlError as exc:
            self.log("读取序列号失败: %s" % exc)
            dev["serial"] = None
        try:
            dev["msm_hwid"] = self.sahara.get_msm_hwid()
        except EdlError as exc:
            self.log("读取 MSM HW ID 失败: %s" % exc)
            dev["msm_hwid"] = None
        return dev

    def enter_firehose(self, loader, progress=None):
        # Command mode and image transfer cannot coexist: ask the PBL to
        # restart Sahara, then answer its fresh HELLO with MODE_IMAGE_TX.
        last = None
        for attempt in range(3):
            try:
                self.log("状态机复位，准备镜像传输（第 %d 次）" % (attempt + 1))
                if self.sahara.reset_state() is None:
                    self.log("复位无响应，改为盲发握手")
                self.log("上传引导: %s" % loader)
                self.sahara.upload_loader(loader, progress)
                last = None
                break
            except EdlError as exc:
                last = exc
                self.log("上传失败: %s" % exc)
                time.sleep(0.6)
        if last is not None:
            raise last
        self.log("引导上传完成，切换到 Firehose")

        # Usually the programmer keeps the same COM port; only if it went away
        # (USB re-enumeration) do we reopen. Closing a live port can reset the
        # target, so try the existing handle first.
        old = self.port
        attrs = None
        try:
            self.firehose = Firehose(self.link, self.log)
            attrs = self.firehose.configure("emmc")
        except EdlError as exc:
            self.log("原端口无响应 (%s)，等待重新枚举 ..." % exc)
            try:
                self.link.close()
            except Exception:
                pass
            time.sleep(1.5)
            new_port = wait_edl_port(25.0, prefer=old)
            self.port = new_port
            self.log("重新打开端口 %s" % new_port)
            self.link = SerialLink(new_port)
            self.firehose = Firehose(self.link, self.log)
            attrs = self.firehose.configure("emmc")

        self.log("配置完成: MemoryName=%s TargetName=%s"
                 % (attrs.get("MemoryName"), attrs.get("TargetName")))
        self.device["firehose"] = attrs
        return attrs

    def read_gpt(self, lun=0):
        self.log("读取 LUN%d GPT ..." % lun)
        return parse_gpt(self.firehose, lun)

    def write_self_test(self, lun=0, start_sector=296, sectors=16, progress=None):
        """Non-destructive write check: read, write back identical, verify."""
        fh = self.firehose
        self.log("写入自检: LUN%d LBA%d 共 %d 扇区" % (lun, start_sector, sectors))
        before = fh.read(lun, start_sector, sectors)
        self.log("已回读 %d 字节" % len(before))
        fh.program(lun, start_sector, sectors, "selftest.bin", before, progress=progress)
        self.log("原样写回完成")
        after = fh.read(lun, start_sector, sectors)
        ok = before == after
        self.log("校验结果: %s" % ("一致" if ok else "不一致"))
        return ok, before, after

    def dump_partition(self, lun, start_sector, sectors, path, progress=None,
                       chunk_sectors=8192):
        """Read a partition out to a raw .bin file."""
        fh = self.firehose
        done = 0
        with open(path, "wb") as wf:
            while done < sectors:
                n = min(chunk_sectors, sectors - done)
                wf.write(fh.read(lun, start_sector + done, n))
                done += n
                if progress:
                    progress(done * fh.sector_size, sectors * fh.sector_size)
        return path

    def close(self):
        if self.link is not None:
            self.link.close()
