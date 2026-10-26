"""Windows tray monitor for battery levels reported by Logitech HID++ devices."""

from __future__ import annotations

import logging
import os
import queue
import base64
import json
import subprocess
import threading
import time
import ctypes
import sys
import winreg
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
import tkinter as tk

import hid
import pystray
from PIL import Image, ImageDraw


LOG = logging.getLogger("battery_monitor")
LOGITECH_VID = 0x046D
RECEIVER_PIDS = {
    0xC548,  # Logi Bolt
    0xC52B, 0xC532, 0xC534, 0xC537,  # Unifying / Nano
    0xC539, 0xC53A, 0xC53F, 0xC541, 0xC547, 0xC54D,  # Lightspeed
}
BATTERY_STATUS = 0x1000
BATTERY_VOLTAGE = 0x1001
UNIFIED_BATTERY = 0x1004
DEVICE_NAME = 0x0005
POLL_SECONDS = 180
LOW_BATTERY_THRESHOLD = 20
LOW_BATTERY_RECOVERY_DELTA = 5
SETTINGS_FILE = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "LogiBattery" / "settings.json"
AUTOSTART_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
AUTOSTART_VALUE = "LogiBattery"
AUTOSTART_APPROVED_RUN_KEY = (
    r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"
)
AUTOSTART_APPROVED_FOLDER_KEY = (
    r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\StartupFolder"
)
AUTOSTART_SHORTCUT_NAME = "LogiBattery.lnk"
START_MENU_SHORTCUT_NAME = "LogiBattery.lnk"
TOAST_APP_ID = "LogiBattery.Desktop"

# Generic single-cell Li-ion/Li-Po discharge curve. Voltage-only devices do
# not report state of charge, so these values are estimates, rounded to 5%.
LIPO_VOLTAGE_CURVE = (
    (3300, 0), (3500, 5), (3600, 10), (3650, 15), (3700, 20),
    (3730, 25), (3760, 30), (3780, 35), (3800, 40), (3820, 45),
    (3850, 50), (3880, 55), (3910, 60), (3940, 65), (3970, 70),
    (4000, 75), (4040, 80), (4080, 85), (4120, 90), (4160, 95),
    (4200, 100),
)


def _load_low_battery_threshold() -> int:
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        value = int(data.get("low_battery_threshold", LOW_BATTERY_THRESHOLD))
        return min(100, max(5, int(round(value / 5) * 5)))
    except FileNotFoundError:
        return LOW_BATTERY_THRESHOLD
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        LOG.exception("Could not read battery notification settings")
        return LOW_BATTERY_THRESHOLD


def _save_low_battery_threshold(value: int) -> None:
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {"low_battery_threshold": value}
    SETTINGS_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


@dataclass(frozen=True)
class DeviceBattery:
    name: str
    level: int | None
    state: str
    category: str = "设备"
    transport: str = "无线接收器"
    estimated: bool = False
    voltage_mv: int | None = None
    connected: bool = True


class HidppChannel:
    """Read-only HID++ channel using the receiver's vendor-defined collections."""

    def __init__(self, devices: list[tuple[int, hid.device]]) -> None:
        self.devices = devices
        self._lock = threading.Lock()
        self._software_id = 7

    @classmethod
    def open_hidpp_device(cls, vid: int, pid: int) -> "HidppChannel | None":
        infos = hid.enumerate(vid, pid)
        paths: dict[bytes, int] = {}
        for info in infos:
            path = info.get("path")
            if not path or info.get("usage_page") != 0xFF00:
                continue
            # HID++ devices expose linked short/long-report collections. A
            # reply may arrive on either collection, regardless of transport.
            paths[path] = int(info.get("usage", 0))

        opened: list[tuple[int, hid.device]] = []
        for path, _usage in sorted(paths.items(), key=lambda entry: entry[1]):
            dev = hid.device()
            try:
                dev.open_path(path)
                dev.set_nonblocking(True)
                opened.append((_usage, dev))
            except OSError:
                try:
                    dev.close()
                except Exception:
                    pass
        return cls(opened) if opened else None

    def close(self) -> None:
        for _usage, dev in self.devices:
            try:
                dev.close()
            except Exception:
                pass

    def request(
        self,
        report_id: int,
        device_index: int,
        feature_index: int,
        function: int,
        params: bytes = b"",
        *,
        timeout_ms: int = 260,
    ) -> bytes | None:
        report_size = 7 if report_id == 0x10 else 20

        with self._lock:
            # Prefer usage 2 for long HID++ messages and usage 1 for short
            # register requests. If a receiver variant routes differently, try
            # the remaining collection; these are all read-only requests.
            preferred_usage = 1 if report_id == 0x10 else 2
            ordered = sorted(self.devices, key=lambda item: item[0] != preferred_usage)
            for attempt in range(2):
                request_function = function
                if report_id == 0x11 and device_index != 0xFF:
                    # Replies echo the low nibble. Rotate it so a delayed answer
                    # from an earlier request cannot be mistaken for this one.
                    self._software_id = 8 if self._software_id >= 15 else self._software_id + 1
                    request_function = (function & 0xF0) | self._software_id
                frame = bytes((report_id, device_index, feature_index, request_function)) + params
                frame = frame[:report_size].ljust(report_size, b"\0")
                expected = (device_index, feature_index, request_function)
                for _usage, sender in ordered:
                    try:
                        written = sender.write(frame)
                    except OSError:
                        continue
                    if written < 0:
                        continue

                    deadline = time.monotonic() + timeout_ms / 1000
                    while time.monotonic() < deadline:
                        for _reader_usage, reader in self.devices:
                            try:
                                packet = bytes(reader.read(64, 12))
                            except OSError:
                                packet = b""
                            if (
                                len(packet) >= 4
                                and packet[0] in (0x10, 0x11)
                                and packet[1:4] == bytes(expected)
                            ):
                                return packet
                            if (
                                len(packet) >= 5
                                and packet[0] in (0x10, 0x11)
                                and packet[1] == device_index
                                and packet[2] == 0x8F
                                and packet[3] == feature_index
                                and packet[4] == request_function
                            ):
                                return None
        return None


class LogitechReceiver:
    def __init__(self, vid: int, pid: int, channel: HidppChannel) -> None:
        self.vid = vid
        self.pid = pid
        self.channel = channel

    def close(self) -> None:
        self.channel.close()

    def _feature_index(self, slot: int, feature_id: int) -> int | None:
        response = self.channel.request(
            0x11, slot, 0x00, 0x08, feature_id.to_bytes(2, "big")
        )
        if response is None or len(response) < 5:
            return None
        index = response[4]
        return index or None

    def _feature_request(
        self, slot: int, feature_index: int, function: int = 0, params: bytes = b""
    ) -> bytes | None:
        # HID++ software ID 8 distinguishes our reply from unsolicited events.
        # For HID++ 2.0 the high nibble is function and low nibble is software ID.
        command = (function << 4) | 0x08
        return self.channel.request(0x11, slot, feature_index, command, params)

    def _name_and_kind(self, slot: int) -> tuple[str | None, int | None]:
        index = self._feature_index(slot, DEVICE_NAME)
        if index is None:
            return None, None
        kind_reply = self._feature_request(slot, index, 2)
        kind = kind_reply[4] if kind_reply and len(kind_reply) > 4 else None
        length_reply = self._feature_request(slot, index)
        if not length_reply or len(length_reply) < 5:
            return None, kind
        length = min(length_reply[4], 32)
        name = bytearray()
        while len(name) < length:
            fragment = self._feature_request(slot, index, 1, bytes((len(name),)))
            if not fragment or len(fragment) < 5:
                break
            name.extend(fragment[4 : 4 + min(16, length - len(name))])
        try:
            decoded = bytes(name[:length]).decode("utf-8", errors="replace").strip("\0 ")
        except Exception:
            decoded = ""
        return decoded or None, kind

    def _slot_is_paired(self, slot: int) -> bool:
        # Bolt uses 0x50 + slot; Unifying-compatible receivers use the even
        # subregisters beginning at 0x20. Reading these registers cannot alter
        # receiver pairing state.
        subregister = 0x50 + slot if self.pid == 0xC548 else 0x20 + 2 * (slot - 1)
        response = self.channel.request(
            0x10, 0xFF, 0x83, 0xB5, bytes((subregister, 0, 0)), timeout_ms=140
        )
        return response is not None and len(response) >= 7

    def _battery(self, slot: int) -> tuple[int | None, str, bool, int | None] | None:
        status_only: tuple[int | None, str, bool, int | None] | None = None
        for feature_id in (UNIFIED_BATTERY, BATTERY_STATUS, BATTERY_VOLTAGE):
            index = self._feature_index(slot, feature_id)
            if index is None:
                continue
            if feature_id == UNIFIED_BATTERY:
                reply = self._feature_request(slot, index, 1)
                if reply and len(reply) > 7:
                    level, status = reply[4], reply[6]
                    state = _charge_state(status)
                    if level <= 100:
                        return level, state, False, None
            else:
                reply = self._feature_request(slot, index)
                if reply and len(reply) > 6:
                    if feature_id == BATTERY_STATUS:
                        level, status = reply[4], reply[6]
                        if 0 < level <= 100:
                            return level, _charge_state(status), False, None
                        if level == 0:
                            status_only = (None, _charge_state(status), False, None)
                    else:
                        voltage = (reply[4] << 8) | reply[5]
                        flags = reply[6]
                        if 2500 <= voltage <= 4500:
                            return (
                                _voltage_to_percent(voltage),
                                _voltage_charge_state(flags),
                                True,
                                voltage,
                            )
        return status_only

    def read_devices(self) -> list[DeviceBattery]:
        found: list[DeviceBattery] = []
        # HID++ device indices 1..6 are used by Logitech Bolt/Unifying and
        # Lightspeed receivers. Empty slots simply time out.
        for slot in range(1, 7):
            try:
                if not self._slot_is_paired(slot):
                    # A missed receiver reply used to hide this slot for the
                    # whole refresh. Retry once before treating it as empty.
                    time.sleep(0.08)
                    if not self._slot_is_paired(slot):
                        continue
                # Bolt devices can answer the receiver's pairing-info register
                # before their HID++ feature channel is ready. Retry the read-only
                # feature queries so a cold start does not silently omit a device.
                name, kind = None, None
                battery = None
                for attempt in range(3):
                    if name is None:
                        candidate_name, candidate_kind = self._name_and_kind(slot)
                        name = candidate_name or name
                        if candidate_kind is not None:
                            kind = candidate_kind
                    if battery is None:
                        battery = self._battery(slot)
                    if name is not None and battery is not None:
                        break
                    if attempt < 2:
                        time.sleep(0.12)
                if name is None and battery is None:
                    continue
                role = _device_category(kind)
                label = name or f"罗技{role}（槽位 {slot}）"
                result = battery
                if result is None:
                    found.append(DeviceBattery(label, None, "设备未返回电量", role, "无线接收器"))
                else:
                    found.append(
                        DeviceBattery(label, result[0], result[1], role, "无线接收器", result[2], result[3])
                    )
            except Exception:
                LOG.exception(
                    "Could not read receiver slot %d (%04X:%04X)",
                    slot,
                    self.vid,
                    self.pid,
                )
        return found

    def read_direct_device(self, default_name: str = "Logitech USB 设备") -> DeviceBattery | None:
        """Read the local device over HID++ instead of a receiver slot."""
        name: str | None = None
        kind: int | None = None
        battery: tuple[int | None, str] | None = None
        for device_index in (0xFF, 0x00):
            for attempt in range(3):
                if name is None:
                    candidate_name, candidate_kind = self._name_and_kind(device_index)
                    name = candidate_name or name
                    if candidate_kind is not None:
                        kind = candidate_kind
                if battery is None:
                    battery = self._battery(device_index)
                if name is not None and battery is not None:
                    break
                if attempt < 2:
                    time.sleep(0.12)
            if name is not None and battery is not None:
                break
        if name is None and battery is None:
            return None

        category = _device_category(kind)
        device_name = name or default_name
        if battery is None:
            return DeviceBattery(device_name, None, "设备未返回电量", category, "USB 直连")
        return DeviceBattery(
            device_name, battery[0], battery[1], category, "USB 直连", battery[2], battery[3]
        )


def _device_category(kind: int | None) -> str:
    return {
        0: "键盘",
        1: "遥控器",
        2: "数字键盘",
        3: "鼠标",
        4: "触控板",
        5: "轨迹球",
        6: "演示器",
        7: "接收器",
        8: "轨迹球",
        9: "触控板",
    }.get(kind, "设备")


def _charge_state(status: int) -> str:
    return {
        0: "正在使用",
        1: "正在充电",
        2: "接近充满",
        3: "已充满",
        4: "慢速充电",
        5: "电池类型不支持",
        6: "充电温度异常",
    }.get(status, "状态未知")


def _voltage_to_percent(voltage_mv: int) -> int:
    if voltage_mv <= LIPO_VOLTAGE_CURVE[0][0]:
        return 0
    if voltage_mv >= LIPO_VOLTAGE_CURVE[-1][0]:
        return 100

    for (low_mv, low_pct), (high_mv, high_pct) in zip(
        LIPO_VOLTAGE_CURVE, LIPO_VOLTAGE_CURVE[1:]
    ):
        if voltage_mv <= high_mv:
            fraction = (voltage_mv - low_mv) / (high_mv - low_mv)
            estimate = low_pct + fraction * (high_pct - low_pct)
            return min(100, max(0, int(round(estimate / 5) * 5)))
    return 100


def _voltage_charge_state(flags: int) -> str:
    if flags & 0x80:
        status = flags & 0x07
        if status in (1, 3):
            return "已充满"
        if status == 2:
            return "已接电源，暂停充电"
        if status == 0:
            if flags & 0x08:
                return "快速充电中"
            if flags & 0x10:
                return "慢速充电中"
            return "正在充电"
        return "充电状态未知"
    if flags & 0x10:
        return "正在无线充电"
    return "正在使用"


def _format_level(device: DeviceBattery) -> str:
    if device.level is None:
        return device.state
    prefix = "约 " if device.estimated else ""
    return f"{prefix}{device.level}%"


def _format_device_status(device: DeviceBattery) -> str:
    details = [device.transport]
    if device.estimated:
        if device.voltage_mv is not None:
            details.append(f"电压估算 {device.voltage_mv / 1000:.2f} V")
        else:
            details.append("电压估算")
    if device.connected:
        details.append(device.state)
    else:
        details.append("暂时未响应 · 显示上次电量")
    return "  ·  ".join(details)


def _autostart_folder() -> Path:
    appdata = Path(os.environ.get("APPDATA", Path.home()))
    return appdata / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def _start_menu_folder() -> Path:
    appdata = Path(os.environ.get("APPDATA", Path.home()))
    return appdata / "Microsoft" / "Windows" / "Start Menu" / "Programs"


def _autostart_target() -> tuple[Path, str]:
    if getattr(sys, "frozen", False):
        return Path(sys.executable), ""

    executable = Path(sys.executable)
    windowless_python = executable.with_name("pythonw.exe")
    if windowless_python.exists():
        executable = windowless_python
    return executable, f'"{Path(__file__).resolve()}"'


def _startup_approval_enabled(key_path: str, value_name: str) -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            approval, _value_type = winreg.QueryValueEx(key, value_name)
        return not (approval and (approval[0] & 1))
    except FileNotFoundError:
        return True
    except OSError:
        LOG.exception("Could not read Windows startup approval state")
        return True


def _autostart_enabled() -> bool:
    shortcut = _autostart_folder() / AUTOSTART_SHORTCUT_NAME
    if shortcut.exists():
        return _startup_approval_enabled(
            AUTOSTART_APPROVED_FOLDER_KEY, AUTOSTART_SHORTCUT_NAME
        )

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_KEY) as key:
            command, _value_type = winreg.QueryValueEx(key, AUTOSTART_VALUE)
    except FileNotFoundError:
        return False
    except OSError:
        LOG.exception("Could not read Windows logon startup setting")
        return False
    if not command:
        return False
    return _startup_approval_enabled(AUTOSTART_APPROVED_RUN_KEY, AUTOSTART_VALUE)


def _delete_registry_value(key_path: str, value_name: str) -> None:
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.DeleteValue(key, value_name)
    except FileNotFoundError:
        pass


def _set_autostart(enabled: bool) -> None:
    if enabled:
        folder = _autostart_folder()
        folder.mkdir(parents=True, exist_ok=True)
        shortcut = folder / AUTOSTART_SHORTCUT_NAME
        target, arguments = _autostart_target()
        _create_windows_shortcut(shortcut, target, arguments, with_toast_app_id=True)
        with winreg.CreateKey(
            winreg.HKEY_CURRENT_USER, AUTOSTART_APPROVED_FOLDER_KEY
        ) as key:
            winreg.SetValueEx(
                key,
                AUTOSTART_SHORTCUT_NAME,
                0,
                winreg.REG_BINARY,
                bytes((0x02, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)),
            )
        _delete_registry_value(AUTOSTART_KEY, AUTOSTART_VALUE)
        _delete_registry_value(AUTOSTART_APPROVED_RUN_KEY, AUTOSTART_VALUE)
        return

    shortcut = _autostart_folder() / AUTOSTART_SHORTCUT_NAME
    shortcut.unlink(missing_ok=True)
    _delete_registry_value(AUTOSTART_KEY, AUTOSTART_VALUE)
    _delete_registry_value(AUTOSTART_APPROVED_RUN_KEY, AUTOSTART_VALUE)
    _delete_registry_value(AUTOSTART_APPROVED_FOLDER_KEY, AUTOSTART_SHORTCUT_NAME)


def _create_windows_shortcut(
    shortcut: Path,
    target: Path,
    arguments: str,
    *,
    with_toast_app_id: bool,
) -> None:
    """Create a shortcut and optionally stamp the AUMID required for desktop toasts."""
    ps_quote = lambda value: "'" + str(value).replace("'", "''") + "'"
    script_lines = [
        "$ErrorActionPreference = 'Stop'",
        "$shell = New-Object -ComObject WScript.Shell",
        f"$shortcut = $shell.CreateShortcut({ps_quote(shortcut)})",
        f"$shortcut.TargetPath = {ps_quote(target)}",
        f"$shortcut.Arguments = {ps_quote(arguments)}",
        f"$shortcut.WorkingDirectory = {ps_quote(target.parent)}",
        f"$shortcut.IconLocation = {ps_quote(str(target) + ',0')}",
        "$shortcut.Description = 'LogiBattery 键鼠电量面板'",
        "$shortcut.Save()",
    ]
    if with_toast_app_id:
        script_lines.extend(
            (
                "Add-Type -TypeDefinition @'",
                "using System;",
                "using System.Runtime.InteropServices;",
                "using System.Runtime.InteropServices.ComTypes;",
                "[StructLayout(LayoutKind.Sequential, Pack=4)]",
                "public struct LogiBatteryPropertyKey { public Guid fmtid; public uint pid; }",
                "[StructLayout(LayoutKind.Explicit, Size=24)]",
                "public struct LogiBatteryPropVariant { [FieldOffset(0)] public ushort vt; [FieldOffset(8)] public IntPtr pwszVal; }",
                "[ComImport, Guid(\"886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99\"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]",
                "public interface LogiBatteryIPropertyStore { [PreserveSig] int GetCount(out uint count); [PreserveSig] int GetAt(uint index, out LogiBatteryPropertyKey key); [PreserveSig] int GetValue(ref LogiBatteryPropertyKey key, out LogiBatteryPropVariant value); [PreserveSig] int SetValue(ref LogiBatteryPropertyKey key, ref LogiBatteryPropVariant value); [PreserveSig] int Commit(); }",
                "public static class LogiBatteryShortcutProperties {",
                "[DllImport(\"ole32.dll\")] static extern int PropVariantClear(ref LogiBatteryPropVariant value);",
                "public static void SetAppId(string path, string appId) {",
                "Type shellLinkType = Type.GetTypeFromCLSID(new Guid(\"00021401-0000-0000-C000-000000000046\"));",
                "object shellLink = Activator.CreateInstance(shellLinkType);",
                "try { ((IPersistFile)shellLink).Load(path, 2); var store = (LogiBatteryIPropertyStore)shellLink;",
                "var key = new LogiBatteryPropertyKey { fmtid = new Guid(\"9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3\"), pid = 5 };",
                "var value = new LogiBatteryPropVariant { vt = 31, pwszVal = Marshal.StringToCoTaskMemUni(appId) };",
                "try { Marshal.ThrowExceptionForHR(store.SetValue(ref key, ref value)); Marshal.ThrowExceptionForHR(store.Commit()); ((IPersistFile)shellLink).Save(path, true); }",
                "finally { PropVariantClear(ref value); } }",
                "finally { Marshal.FinalReleaseComObject(shellLink); } } }",
                "'@",
                f"[LogiBatteryShortcutProperties]::SetAppId({ps_quote(shortcut)}, {ps_quote(TOAST_APP_ID)})",
            )
        )
    script = "\n".join(script_lines)
    encoded_script = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded_script],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _ensure_toast_shortcut() -> None:
    """Register a Start menu shortcut so Windows can attribute desktop toasts."""
    folder = _start_menu_folder()
    folder.mkdir(parents=True, exist_ok=True)
    shortcut = folder / START_MENU_SHORTCUT_NAME
    target, arguments = _autostart_target()
    _create_windows_shortcut(shortcut, target, arguments, with_toast_app_id=True)


def _send_windows_toast(title: str, message: str) -> None:
    """Send a modern Windows toast, which can be managed in Notifications settings."""
    encoded_title = base64.b64encode(title.encode("utf-16le")).decode("ascii")
    encoded_message = base64.b64encode(message.encode("utf-16le")).decode("ascii")
    app_id = base64.b64encode(TOAST_APP_ID.encode("utf-16le")).decode("ascii")
    script = "\n".join(
        (
            "$ErrorActionPreference = 'Stop'",
            "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null",
            "[Windows.UI.Notifications.ToastNotification, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null",
            "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] > $null",
            f"$title = [Text.Encoding]::Unicode.GetString([Convert]::FromBase64String('{encoded_title}'))",
            f"$message = [Text.Encoding]::Unicode.GetString([Convert]::FromBase64String('{encoded_message}'))",
            f"$appId = [Text.Encoding]::Unicode.GetString([Convert]::FromBase64String('{app_id}'))",
            "$xml = New-Object Windows.Data.Xml.Dom.XmlDocument",
            "$xml.LoadXml(\"<toast><visual><binding template='ToastGeneric'><text></text><text></text></binding></visual><audio silent='true'/></toast>\")",
            "$nodes = $xml.GetElementsByTagName('text')",
            "$nodes.Item(0).AppendChild($xml.CreateTextNode($title)) > $null",
            "$nodes.Item(1).AppendChild($xml.CreateTextNode($message)) > $null",
            "$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)",
            "$notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId)",
            "$notifier.Show($toast)",
        )
    )
    encoded_script = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-EncodedCommand", encoded_script],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


class BatteryTray:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._devices: list[DeviceBattery] = []
        self._refreshing = False
        self._panel_events: queue.Queue[tuple] = queue.Queue()
        self._panel_ready = threading.Event()
        self._panel_root: tk.Tk | None = None
        self._panel_list: tk.Frame | None = None
        self._panel_status: tk.Label | None = None
        self._panel_updated: tk.Label | None = None
        self._autostart_var: tk.BooleanVar | None = None
        self._threshold_var: tk.StringVar | None = None
        self._low_battery_threshold = _load_low_battery_threshold()
        self._low_battery_alerted: set[str] = set()
        self._icon = pystray.Icon(
            "LogiBattery",
            _battery_icon(None),
            "LogiBattery：正在扫描接收器",
            menu=pystray.Menu(
                pystray.MenuItem("打开电量面板", self._show_panel, default=True),
                pystray.MenuItem("正在扫描接收器…", None, enabled=False),
                pystray.MenuItem("立即刷新", self._refresh_clicked),
                pystray.MenuItem("退出", self._exit_clicked),
            ),
        )

    def _show_panel(self, _icon=None, _item=None) -> None:
        self._panel_events.put(("show",))

    def _refresh_clicked(self, _icon, _item) -> None:
        self.refresh_async()

    def _exit_clicked(self, icon, _item) -> None:
        self._panel_events.put(("quit",))
        icon.stop()

    def refresh_async(self) -> None:
        with self._lock:
            if self._refreshing:
                return
            self._refreshing = True
        threading.Thread(target=self._refresh_worker, daemon=True).start()

    def _refresh_worker(self) -> None:
        devices: list[DeviceBattery] = []
        try:
            infos = hid.enumerate(LOGITECH_VID)
            receivers = {
                (int(info["vendor_id"]), int(info["product_id"]))
                for info in infos
                if int(info.get("product_id", 0)) in RECEIVER_PIDS
                and int(info.get("usage_page", 0)) == 0xFF00
            }
            direct_devices: dict[tuple[int, int], str] = {}
            for info in infos:
                pid = int(info.get("product_id", 0))
                if (
                    pid not in RECEIVER_PIDS
                    and int(info.get("usage_page", 0)) == 0xFF00
                    and int(info.get("usage", 0)) in (0x01, 0x02)
                ):
                    key = (int(info["vendor_id"]), pid)
                    direct_devices[key] = str(info.get("product_string") or f"Logitech USB 设备 ({pid:04X})")

            for vid, pid in receivers:
                channel = HidppChannel.open_hidpp_device(vid, pid)
                if channel is None:
                    continue
                receiver = LogitechReceiver(vid, pid, channel)
                try:
                    devices.extend(receiver.read_devices())
                finally:
                    receiver.close()

            for (vid, pid), product_name in direct_devices.items():
                channel = HidppChannel.open_hidpp_device(vid, pid)
                if channel is None:
                    continue
                device = LogitechReceiver(vid, pid, channel)
                try:
                    direct_reading = device.read_direct_device(product_name)
                    if direct_reading is not None:
                        devices.append(direct_reading)
                finally:
                    device.close()
        except Exception:
            LOG.exception("Battery scan failed")

        # Prefer the direct connection if the same device is also visible via
        # its receiver; it reports the active charging state over USB.
        by_name: dict[str, DeviceBattery] = {}
        for device in devices:
            key = " ".join(device.name.casefold().split())
            current = by_name.get(key)
            if current is None or device.transport == "USB 直连":
                by_name[key] = device
        fresh_devices = list(by_name.values())
        with self._lock:
            fresh_names = set(by_name)
            devices = list(fresh_devices)
            # Keep a device visible for one missed scan so a single receiver
            # timeout does not make it disappear from the panel.
            for previous in self._devices:
                key = " ".join(previous.name.casefold().split())
                if previous.connected and key not in fresh_names:
                    devices.append(replace(previous, connected=False))
            self._devices = devices
            self._refreshing = False
        LOG.info(
            "Battery scan found %d responsive device(s), displaying %d: %s",
            len(fresh_devices),
            len(devices),
            devices,
        )
        self._update_tray()
        self._notify_low_battery(fresh_devices)
        threading.Timer(POLL_SECONDS, self.refresh_async).start()

    def _notify_low_battery(self, devices: list[DeviceBattery]) -> None:
        newly_low: list[DeviceBattery] = []
        with self._lock:
            threshold = self._low_battery_threshold
            recovery_level = min(100, threshold + LOW_BATTERY_RECOVERY_DELTA)
            for device in devices:
                key = " ".join((device.category + " " + device.name).casefold().split())
                if device.level is None:
                    continue

                powered = "充电" in device.state or "接电源" in device.state
                if powered or device.level > recovery_level:
                    self._low_battery_alerted.discard(key)

                if (
                    device.level <= threshold
                    and not powered
                    and key not in self._low_battery_alerted
                ):
                    newly_low.append(device)
                    self._low_battery_alerted.add(key)

        if not newly_low:
            return

        message = "\n".join(
            f"{device.category} · {device.name}: {_format_level(device)}"
            for device in newly_low
        )
        try:
            _send_windows_toast("LogiBattery 电量不足", message)
            LOG.info("Sent low-battery notification: %s", message)
        except Exception:
            LOG.exception("Could not show the low-battery notification")

    def _save_threshold_clicked(self) -> None:
        if self._threshold_var is None:
            return
        try:
            requested = int(self._threshold_var.get().strip().rstrip("%"))
        except ValueError:
            self._threshold_var.set(str(self._low_battery_threshold))
            if self._panel_status is not None:
                self._panel_status.config(text="请输入 5 到 100 的整数。", fg="#F0B34D")
            return

        threshold = min(100, max(5, int(round(requested / 5) * 5)))
        try:
            _save_low_battery_threshold(threshold)
        except OSError:
            LOG.exception("Could not save battery notification threshold")
            if self._panel_status is not None:
                self._panel_status.config(text="提醒阈值保存失败。", fg="#F0B34D")
            return

        with self._lock:
            self._low_battery_threshold = threshold
            self._low_battery_alerted.clear()
        self._threshold_var.set(str(threshold))
        if self._panel_status is not None:
            self._panel_status.config(
                text=f"低电量提醒阈值已设为 {threshold}%", fg="#75E2D0"
            )
        self.refresh_async()

    def _test_notification_clicked(self) -> None:
        with self._lock:
            threshold = self._low_battery_threshold
        try:
            _send_windows_toast(
                "LogiBattery 测试通知",
                f"这是测试提醒。低电量阈值：{threshold}%。",
            )
            LOG.info("Sent a test battery notification")
            message = "已发送测试通知；专注模式可能会隐藏横幅，可在通知中心查看。"
            color = "#75E2D0"
        except Exception:
            LOG.exception("Could not show the test battery notification")
            message = "测试通知发送失败，请查看运行日志。"
            color = "#F0B34D"
        if self._panel_status is not None:
            self._panel_status.config(text=message, fg=color)

    def _update_tray(self) -> None:
        with self._lock:
            devices = list(self._devices)
            refreshing = self._refreshing

        if devices:
            tip = "\n".join(
                f"{d.name}: {_format_level(d)}"
                + ("（暂时未响应）" if not d.connected else "")
                for d in devices
            )
            levels = [d.level for d in devices if d.level is not None]
            icon = _battery_icon(min(levels) if levels else None)
            entries = [
                pystray.MenuItem(
                    f"{d.category} · {d.name}  ·  {_format_level(d)}"
                    + (" · 暂时未响应" if not d.connected else ""),
                    None,
                    enabled=False,
                )
                for d in devices
            ]
        else:
            tip = "LogiBattery：未发现已连接的 Logitech HID++ 设备"
            icon = _battery_icon(None)
            entries = [pystray.MenuItem("未发现支持的设备", None, enabled=False)]

        if refreshing:
            tip = "LogiBattery：正在扫描…"
        self._icon.title = tip[:127]
        self._icon.icon = icon
        self._icon.menu = pystray.Menu(
            pystray.MenuItem("打开电量面板", self._show_panel, default=True),
            *entries,
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("立即刷新", self._refresh_clicked),
            pystray.MenuItem("退出", self._exit_clicked),
        )
        self._icon.update_menu()

        self._panel_events.put(("data", devices, refreshing, datetime.now()))

    def _panel_main(self) -> None:
        root = tk.Tk()
        self._panel_root = root
        root.title("LogiBattery · 设备电量")
        root.configure(bg="#0B1118")
        root.geometry("460x440")
        root.resizable(False, False)
        root.withdraw()
        root.protocol("WM_DELETE_WINDOW", root.withdraw)

        shell = tk.Frame(root, bg="#0B1118", padx=24, pady=22)
        shell.pack(fill="both", expand=True)

        heading = tk.Frame(shell, bg="#0B1118")
        heading.pack(fill="x")
        title = tk.Label(
            heading, text="设备电量", bg="#0B1118", fg="#F3F7FB",
            font=("Segoe UI", 20, "bold"),
        )
        title.pack(side="left")
        refresh = tk.Button(
            heading, text="⟳  刷新", command=self._panel_refresh,
            bg="#183A45", fg="#75E2D0", activebackground="#24515D",
            activeforeground="#FFFFFF", relief="flat", bd=0,
            padx=13, pady=7, cursor="hand2", font=("Segoe UI", 10, "bold"),
        )
        refresh.pack(side="right", pady=2)

        subtitle = tk.Label(
            shell, text="无线接收器与 USB 直连设备电量", bg="#0B1118", fg="#8292A4",
            font=("Segoe UI", 10), anchor="w",
        )
        subtitle.pack(fill="x", pady=(3, 17))

        self._panel_list = tk.Frame(shell, bg="#0B1118")
        self._panel_list.pack(fill="both", expand=True)
        self._panel_status = tk.Label(
            shell, text="正在读取接收器…", bg="#0B1118", fg="#8DA0B4",
            font=("Segoe UI", 9), anchor="w",
        )
        self._panel_status.pack(fill="x", pady=(10, 2))
        self._panel_updated = tk.Label(
            shell, text="自动刷新 · 每 3 分钟", bg="#0B1118", fg="#607286",
            font=("Segoe UI", 9), anchor="w",
        )
        self._panel_updated.pack(fill="x")
        self._autostart_var = tk.BooleanVar(value=_autostart_enabled())
        tk.Checkbutton(
            shell,
            text="登录 Windows 时自动启动",
            variable=self._autostart_var,
            command=self._toggle_autostart,
            bg="#0B1118",
            fg="#AFC0D0",
            activebackground="#0B1118",
            activeforeground="#F3F7FB",
            selectcolor="#151E29",
            font=("Segoe UI", 9),
            anchor="w",
            padx=0,
            highlightthickness=0,
        ).pack(fill="x", pady=(5, 0))

        reminder = tk.Frame(shell, bg="#0B1118")
        reminder.pack(fill="x", pady=(9, 0))
        tk.Label(
            reminder,
            text="低电量提醒",
            bg="#0B1118",
            fg="#AFC0D0",
            font=("Segoe UI", 9),
        ).pack(side="left")
        self._threshold_var = tk.StringVar(value=str(self._low_battery_threshold))
        tk.Spinbox(
            reminder,
            from_=5,
            to=100,
            increment=5,
            textvariable=self._threshold_var,
            width=5,
            justify="center",
            bg="#151E29",
            fg="#F3F7FB",
            buttonbackground="#263341",
            insertbackground="#F3F7FB",
            relief="flat",
            bd=0,
            font=("Segoe UI", 9),
        ).pack(side="left", padx=(10, 4))
        tk.Label(
            reminder,
            text="%",
            bg="#0B1118",
            fg="#AFC0D0",
            font=("Segoe UI", 9),
        ).pack(side="left")
        tk.Button(
            reminder,
            text="测试通知",
            command=self._test_notification_clicked,
            bg="#183A45",
            fg="#75E2D0",
            activebackground="#24515D",
            activeforeground="#FFFFFF",
            relief="flat",
            bd=0,
            padx=10,
            pady=5,
            cursor="hand2",
            font=("Segoe UI", 9, "bold"),
        ).pack(side="right")
        tk.Button(
            reminder,
            text="保存",
            command=self._save_threshold_clicked,
            bg="#263341",
            fg="#DCE7F0",
            activebackground="#344657",
            activeforeground="#FFFFFF",
            relief="flat",
            bd=0,
            padx=10,
            pady=5,
            cursor="hand2",
            font=("Segoe UI", 9),
        ).pack(side="right", padx=(0, 8))

        width, height = 460, 500
        x = max(0, (root.winfo_screenwidth() - width) // 2)
        y = max(0, (root.winfo_screenheight() - height) // 2)
        root.geometry(f"{width}x{height}+{x}+{y}")
        self._panel_ready.set()
        root.after(100, self._poll_panel_events)
        root.mainloop()

    def _panel_refresh(self) -> None:
        if self._panel_status is not None:
            self._panel_status.config(text="正在读取接收器…", fg="#75E2D0")
        self.refresh_async()

    def _toggle_autostart(self) -> None:
        if self._autostart_var is None:
            return
        enabled = self._autostart_var.get()
        try:
            _set_autostart(enabled)
        except (OSError, subprocess.SubprocessError):
            LOG.exception("Could not update Windows logon startup setting")
            self._autostart_var.set(not enabled)
            if self._panel_status is not None:
                self._panel_status.config(
                    text="自启动设置失败，请检查当前用户注册表权限。", fg="#F0B34D"
                )
            return

        if self._panel_status is not None:
            text = (
                "已开启登录 Windows 时自动启动"
                if enabled
                else "已关闭登录 Windows 时自动启动"
            )
            self._panel_status.config(text=text, fg="#75E2D0")

    def _render_panel_devices(
        self, devices: list[DeviceBattery], refreshing: bool, updated: datetime
    ) -> None:
        if self._panel_list is None:
            return
        for child in self._panel_list.winfo_children():
            child.destroy()

        if devices:
            for device in devices:
                card = tk.Frame(self._panel_list, bg="#151E29", padx=16, pady=13)
                card.pack(fill="x", pady=5)
                top = tk.Frame(card, bg="#151E29")
                top.pack(fill="x")
                tk.Label(
                    top, text=device.category, bg="#203844", fg="#75E2D0",
                    font=("Segoe UI", 8, "bold"), padx=7, pady=3,
                ).pack(side="left", padx=(0, 8))
                tk.Label(
                    top, text=device.name, bg="#151E29", fg="#E8EEF5",
                    font=("Segoe UI", 11, "bold"), anchor="w",
                ).pack(side="left", fill="x", expand=True)
                level_label = (
                    f"约 {device.level}%" if device.estimated
                    else f"{device.level}%" if device.level is not None
                    else "—"
                )
                tk.Label(
                    top, text=level_label, bg="#151E29",
                    fg=_battery_color(device.level),
                    font=("Segoe UI", 20, "bold"), anchor="e",
                ).pack(side="right")

                status = tk.Label(
                    card, text=_format_device_status(device), bg="#151E29", fg="#91A1B2",
                    font=("Segoe UI", 9), anchor="w",
                )
                status.pack(fill="x", pady=(3, 8))
                bar = tk.Canvas(
                    card, height=7, bg="#263341", highlightthickness=0, bd=0
                )
                bar.pack(fill="x")

                def draw_battery_bar(_event=None, canvas=bar, level=device.level):
                    canvas.delete("battery-level")
                    if level is None:
                        return
                    fill_width = round(canvas.winfo_width() * level / 100)
                    if fill_width > 0:
                        canvas.create_rectangle(
                            0,
                            0,
                            fill_width,
                            canvas.winfo_height(),
                            fill=_battery_color(level),
                            outline="",
                            tags="battery-level",
                        )

                bar.bind("<Configure>", draw_battery_bar)
                bar.after_idle(draw_battery_bar)
            stale = any(not device.connected for device in devices)
            status_text = (
                "正在更新电量…" if refreshing
                else "部分设备暂时未响应，显示上次电量" if stale
                else "设备已连接"
            )
            self._panel_status.config(
                text=status_text,
                fg="#75E2D0" if refreshing else "#F0B34D" if stale else "#8DA0B4",
            )
        else:
            message = "正在读取接收器…" if refreshing else "暂未发现支持的设备"
            tk.Label(
                self._panel_list, text=message, bg="#0B1118", fg="#91A1B2",
                font=("Segoe UI", 11), pady=42,
            ).pack(fill="x")
            self._panel_status.config(text="可点击右上角刷新重试。", fg="#8DA0B4")
            self._panel_updated.config(text=f"上次更新  {updated:%H:%M:%S}  ·  自动刷新每 3 分钟")

    def _poll_panel_events(self) -> None:
        if self._panel_root is None:
            return
        try:
            while True:
                event = self._panel_events.get_nowait()
                if event[0] == "show":
                    self._panel_root.deiconify()
                    self._panel_root.lift()
                    self._panel_root.focus_force()
                elif event[0] == "data":
                    self._render_panel_devices(event[1], event[2], event[3])
                elif event[0] == "quit":
                    self._panel_root.destroy()
                    self._panel_root = None
                    return
        except queue.Empty:
            pass
        self._panel_root.after(100, self._poll_panel_events)

    def run(self) -> None:
        panel_thread = threading.Thread(target=self._panel_main, daemon=True)
        panel_thread.start()
        self._panel_ready.wait(timeout=5)
        self.refresh_async()
        self._panel_events.put(("show",))
        self._icon.run()


def _battery_icon(level: int | None) -> Image.Image:
    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    outline = (220, 226, 232, 255)
    if level is None:
        fill = (107, 121, 138, 255)
    elif level <= 20:
        fill = (230, 75, 75, 255)
    elif level <= 50:
        fill = (240, 177, 53, 255)
    else:
        fill = (72, 190, 122, 255)
    draw.rounded_rectangle((9, 17, 53, 49), radius=6, outline=outline, width=4)
    draw.rounded_rectangle((53, 26, 59, 40), radius=2, fill=outline)
    if level is not None:
        width = max(3, int(34 * max(0, min(level, 100)) / 100))
        draw.rounded_rectangle((15, 23, 15 + width, 43), radius=3, fill=fill)
    else:
        draw.line((18, 40, 44, 24), fill=fill, width=5)
    return image


def _battery_color(level: int | None) -> str:
    if level is None:
        return "#65788D"
    if level <= 20:
        return "#F06C72"
    if level <= 50:
        return "#F0B34D"
    return "#49D0A0"


def _create_single_instance_mutex():
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = (ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
    kernel32.CloseHandle.restype = ctypes.c_bool
    handle = kernel32.CreateMutexW(None, True, "Local\\LogiBattery.Tray.Singleton")
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    already_running = ctypes.get_last_error() == 183  # ERROR_ALREADY_EXISTS
    return kernel32, handle, already_running


if __name__ == "__main__":
    log_dir = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "LogiBattery"
    log_dir.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [
        logging.FileHandler(log_dir / "battery_monitor.log", encoding="utf-8")
    ]
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler(sys.stderr))
    logging.basicConfig(level=logging.INFO, handlers=handlers)
    kernel32, mutex, already_running = _create_single_instance_mutex()
    if already_running:
        kernel32.CloseHandle(mutex)
        if sys.stdout is not None:
            print("LogiBattery 已在运行，请到任务栏右下角的隐藏图标区查看电池图标。")
        else:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            user32.MessageBoxW(None, "LogiBattery 已在运行，请查看任务栏右下角的托盘图标。", "LogiBattery", 0x40)
        if sys.stdin is not None and sys.stdin.isatty():
            input("按回车关闭此窗口…")
        sys.exit(0)
    try:
        try:
            _ensure_toast_shortcut()
        except Exception:
            LOG.exception("Could not register the Start menu shortcut for toast notifications")
        BatteryTray().run()
    except Exception:
        LOG.exception("LogiBattery stopped unexpectedly")
        if sys.stdout is None:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            user32.MessageBoxW(
                None,
                f"LogiBattery 启动失败。日志位置：{log_dir / 'battery_monitor.log'}",
                "LogiBattery",
                0x10,
            )
        else:
            raise
    finally:
        kernel32.CloseHandle(mutex)
