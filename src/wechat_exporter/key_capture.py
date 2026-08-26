from __future__ import annotations

import bisect
import os
import queue
import re
import struct
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from .crypto import (
    DatabaseKeys,
    DatabaseTarget,
    keys_from_master_password,
)
from .windows import bring_wechat_to_front


_RIP_RELATIVE_LEA = re.compile(
    rb"[HL]\x8d[\x05\x0d\x15\x1d\x25\x2d\x35\x3d].{4}", re.DOTALL
)


@dataclass(frozen=True, slots=True)
class _PeSection:
    name: str
    virtual_address: int
    virtual_size: int
    raw_offset: int
    raw_size: int


@dataclass(frozen=True, slots=True)
class KeyCapturePreparation:
    dll_path: Path
    dll_size: int
    dll_mtime_ns: int
    codec_rva: int

    def matches(self, executable: Path) -> bool:
        try:
            current = locate_weixin_dll(executable)
            stat = current.stat()
        except OSError:
            return False
        return (
            current.resolve() == self.dll_path.resolve()
            and stat.st_size == self.dll_size
            and stat.st_mtime_ns == self.dll_mtime_ns
        )


def _read_sections(data: bytes) -> list[_PeSection]:
    if len(data) < 0x40:
        raise ValueError("Weixin.dll 不是有效的 PE 文件")
    pe_offset = struct.unpack_from("<I", data, 0x3C)[0]
    if data[pe_offset : pe_offset + 4] != b"PE\0\0":
        raise ValueError("Weixin.dll 不是有效的 PE 文件")
    coff = pe_offset + 4
    section_count = struct.unpack_from("<H", data, coff + 2)[0]
    optional_size = struct.unpack_from("<H", data, coff + 16)[0]
    section_table = coff + 20 + optional_size
    sections = []
    for index in range(section_count):
        offset = section_table + index * 40
        name = data[offset : offset + 8].rstrip(b"\0").decode("ascii", errors="replace")
        virtual_size, virtual_address, raw_size, raw_offset = struct.unpack_from(
            "<IIII", data, offset + 8
        )
        sections.append(
            _PeSection(name, virtual_address, virtual_size, raw_offset, raw_size)
        )
    return sections


def find_codec_config_rva(weixin_dll: Path) -> int:
    """Derive the codec setup function from its unique MMV1 code reference."""
    data = weixin_dll.read_bytes()
    sections = _read_sections(data)
    text = next((section for section in sections if section.name == ".text"), None)
    pdata = next((section for section in sections if section.name == ".pdata"), None)
    if text is None or pdata is None:
        raise ValueError("Weixin.dll 缺少 .text/.pdata 节")

    targets: set[int] = set()
    for section in sections:
        raw = data[section.raw_offset : section.raw_offset + section.raw_size]
        cursor = 0
        while True:
            cursor = raw.find(b"MMV1", cursor)
            if cursor < 0:
                break
            targets.add(section.virtual_address + cursor)
            cursor += 4
    if not targets:
        raise ValueError("当前微信版本中没有找到数据库 codec 标记")

    ranges: list[tuple[int, int]] = []
    pdata_raw = data[pdata.raw_offset : pdata.raw_offset + pdata.raw_size]
    for offset in range(0, len(pdata_raw) - 11, 12):
        begin, end, _ = struct.unpack_from("<III", pdata_raw, offset)
        if begin < end:
            ranges.append((begin, end))
    ranges.sort()
    starts = [begin for begin, _ in ranges]

    candidates: set[int] = set()
    text_raw = data[text.raw_offset : text.raw_offset + text.raw_size]
    for match in _RIP_RELATIVE_LEA.finditer(text_raw):
        instruction_rva = text.virtual_address + match.start()
        displacement = struct.unpack("<i", match.group(0)[3:7])[0]
        if instruction_rva + 7 + displacement not in targets:
            continue
        range_index = bisect.bisect_right(starts, instruction_rva) - 1
        if range_index < 0:
            continue
        begin, end = ranges[range_index]
        if begin <= instruction_rva < end:
            candidates.add(begin)
    if len(candidates) != 1:
        raise ValueError(
            f"当前微信版本的 codec 函数签名不唯一（候选 {len(candidates)} 个），为避免误挂钩已停止。"
        )
    return candidates.pop()


def locate_weixin_dll(executable: Path) -> Path:
    executable = executable.resolve()
    version = _windows_file_version(executable)
    candidates = []
    if version:
        candidates.append(executable.parent / version / "Weixin.dll")
    candidates.extend(executable.parent.glob("*.*.*.*\\Weixin.dll"))
    existing = [path for path in candidates if path.is_file()]
    if not existing:
        raise FileNotFoundError("没有在微信安装目录找到 Weixin.dll")
    return max(existing, key=lambda path: path.stat().st_mtime_ns)


def _windows_file_version(path: Path) -> str:
    if os.name != "nt":
        return ""
    import ctypes
    from ctypes import wintypes

    version = ctypes.WinDLL("version", use_last_error=True)
    version.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
    version.GetFileVersionInfoSizeW.restype = wintypes.DWORD
    version.GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
    version.GetFileVersionInfoW.restype = wintypes.BOOL
    version.VerQueryValueW.argtypes = [
        ctypes.c_void_p,
        wintypes.LPCWSTR,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.UINT),
    ]
    version.VerQueryValueW.restype = wintypes.BOOL
    size = version.GetFileVersionInfoSizeW(str(path), None)
    if not size:
        return ""
    buffer = ctypes.create_string_buffer(size)
    if not version.GetFileVersionInfoW(str(path), 0, size, buffer):
        return ""
    value = ctypes.c_void_p()
    length = wintypes.UINT()
    if not version.VerQueryValueW(buffer, "\\", ctypes.byref(value), ctypes.byref(length)):
        return ""
    fixed = ctypes.cast(value, ctypes.POINTER(wintypes.DWORD * 13)).contents
    ms, ls = fixed[2], fixed[3]
    return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"


def prepare_key_capture(
    executable: Path,
    *,
    progress: Callable[[str], None] | None = None,
) -> KeyCapturePreparation:
    """Load the capture runtime and scan the installed build before consent."""
    try:
        import frida  # type: ignore[import-not-found]
    except ImportError as error:
        raise RuntimeError("缺少 frida 运行组件，请重新安装完整版本的导出工具。") from error

    if progress:
        progress("正在预加载微信连接组件...")
    frida.get_local_device()
    dll = locate_weixin_dll(executable)
    stat = dll.stat()
    codec_rva = find_codec_config_rva(dll)
    if progress:
        progress("微信启动准备已完成")
    return KeyCapturePreparation(
        dll_path=dll.resolve(),
        dll_size=stat.st_size,
        dll_mtime_ns=stat.st_mtime_ns,
        codec_rva=codec_rva,
    )


def capture_keys_during_wechat_start(
    executable: Path,
    targets: Iterable[DatabaseTarget],
    *,
    timeout_seconds: int = 75,
    progress: Callable[[str], None] | None = None,
    preparation: KeyCapturePreparation | None = None,
) -> DatabaseKeys:
    """Spawn WeChat under a short-lived Frida hook and return validated keys.

    Callers must require the user to fully exit WeChat first. The hook sends a
    32-byte candidate to this process, validates it against database HMACs, and
    never writes the candidate to disk.
    """
    target_list = list(targets)
    try:
        import frida  # type: ignore[import-not-found]
    except ImportError as error:
        raise RuntimeError("缺少 frida 运行组件，请重新安装完整版本的导出工具。") from error

    if preparation is None or not preparation.matches(executable):
        preparation = prepare_key_capture(executable, progress=progress)
    codec_rva = preparation.codec_rva
    if progress:
        progress("启动准备已完成，正在创建微信进程...")

    event = threading.Event()
    stop_validator = threading.Event()
    result: dict[str, DatabaseKeys | BaseException] = {}
    candidate_queue: queue.Queue[bytes] = queue.Queue(maxsize=64)
    seen_candidates: set[bytes] = set()
    candidate_lock = threading.Lock()
    javascript = f"""
    'use strict';
    var installed = false;
    var tries = 0;
    var seenCandidates = Object.create(null);
    var uniqueCandidates = 0;
    function install() {{
      if (installed) return;
      tries += 1;
      try {{
        var module = Process.getModuleByName('Weixin.dll');
        var address = module.base.add(ptr('{codec_rva:#x}'));
        Interceptor.attach(address, {{
          onEnter: function(args) {{
            try {{
              var bytes = this.context.rcx.readByteArray(32);
              var view = new Uint8Array(bytes);
              var signature = '';
              for (var i = 0; i < view.length; i++) {{
                signature += ('0' + view[i].toString(16)).slice(-2);
              }}
              if (seenCandidates[signature]) return;
              seenCandidates[signature] = true;
              uniqueCandidates += 1;
              if (uniqueCandidates > 64) return;
              send({{type: 'candidate'}}, bytes);
            }} catch (error) {{
              send({{type: 'read-error', message: String(error)}});
            }}
          }}
        }});
        installed = true;
        send({{type: 'installed'}});
      }} catch (error) {{
        if (tries < 3000) setTimeout(install, 10);
        else send({{type: 'install-error', message: String(error)}});
      }}
    }}
    install();
    """

    def validate_candidates() -> None:
        checked = 0
        while not stop_validator.is_set():
            try:
                candidate = candidate_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            checked += 1
            if progress:
                progress(f"正在快速验证登录密钥（候选 {checked}）...")
            try:
                candidate_keys = keys_from_master_password(candidate, target_list)
            except ValueError:
                continue
            required = {"contact\\contact.db", "session\\session.db"}
            available = set(candidate_keys.paths())
            has_messages = any(path.startswith("message\\message_") for path in available)
            if required.issubset(available) and has_messages:
                result["keys"] = candidate_keys
                event.set()
                stop_validator.set()
                return

    def on_message(message: dict[str, object], data: bytes | None) -> None:
        payload = message.get("payload")
        if message.get("type") == "error":
            result["error"] = RuntimeError(str(message.get("stack") or message))
            event.set()
            return
        if not isinstance(payload, dict):
            return
        message_type = payload.get("type")
        if message_type == "installed" and progress:
            progress("读取组件已就绪，正在等待微信打开数据库...")
        elif message_type == "candidate" and data:
            candidate = bytes(data[:32])
            if len(candidate) != 32:
                return
            with candidate_lock:
                if candidate in seen_candidates:
                    return
                seen_candidates.add(candidate)
            try:
                candidate_queue.put_nowait(candidate)
            except queue.Full:
                result["error"] = RuntimeError(
                    "微信返回了过多不同的密钥候选，已停止以避免长时间占用处理器。"
                )
                event.set()
        elif message_type in {"install-error", "read-error"}:
            result["error"] = RuntimeError(str(payload.get("message") or "微信读取组件失败"))
            event.set()

    device = frida.get_local_device()
    pid: int | None = None
    session = None
    script = None
    resumed = False
    validator = threading.Thread(target=validate_candidates, daemon=True)
    validator.start()
    try:
        pid = device.spawn([str(executable), "--scene=desktop"])
        if progress:
            progress("微信进程已创建，正在加载读取组件...")
        session = device.attach(pid)
        script = session.create_script(javascript)
        script.on("message", on_message)
        script.load()
        if progress:
            progress("读取组件已加载，正在显示微信窗口...")
        device.resume(pid)
        resumed = True
        threading.Thread(
            target=bring_wechat_to_front,
            args=(pid,),
            kwargs={"timeout_seconds": 20.0, "progress": progress},
            daemon=True,
        ).start()
        if progress:
            progress("微信已启动；如出现登录界面，请完成登录")

        deadline = time.monotonic() + timeout_seconds
        while not event.wait(0.25) and time.monotonic() < deadline:
            pass
        if "keys" in result:
            keys = result["keys"]
            assert isinstance(keys, DatabaseKeys)
            if progress:
                progress(f"已在内存中验证 {len(keys)} 个数据库密钥（不会落盘）")
            return keys
        if "error" in result:
            raise result["error"]  # type: ignore[misc]
        raise TimeoutError("等待微信数据库打开超时。请确认已完成登录，并重新尝试。")
    finally:
        stop_validator.set()
        if script is not None:
            try:
                script.unload()
            except Exception:
                pass
        if session is not None:
            try:
                session.detach()
            except Exception:
                pass
        if pid is not None and not resumed:
            try:
                device.resume(pid)
            except Exception:
                pass
        validator.join(timeout=1.5)
