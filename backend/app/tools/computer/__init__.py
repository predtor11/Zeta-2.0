"""Computer control tools (Windows-first): applications, clipboard, screenshots,
system information, network status, lock.  All need the host OS, so they are
unavailable in cloud mode.
"""

from __future__ import annotations

import base64
import ctypes
import os
import platform
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import psutil
from pydantic import BaseModel, Field

from app.plugins.loader import Plugin
from app.security.permissions import RiskLevel
from app.security.validators import resolve_path
from app.tools.base import Tool, ToolContext, ToolResult, run_sync

IS_WIN = sys.platform == "win32"

# Friendly names -> candidate executables / commands.  Extend via config/apps.yaml later.
KNOWN_APPS: Dict[str, List[str]] = {
    "chrome": ["chrome", r"%ProgramFiles%\Google\Chrome\Application\chrome.exe", r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe", r"%LocalAppData%\Google\Chrome\Application\chrome.exe"],
    "google chrome": ["chrome"],
    "edge": ["msedge", r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"],
    "firefox": ["firefox", r"%ProgramFiles%\Mozilla Firefox\firefox.exe"],
    "vs code": ["code", r"%LocalAppData%\Programs\Microsoft VS Code\Code.exe", r"%ProgramFiles%\Microsoft VS Code\Code.exe"],
    "vscode": ["vs code"], "code": ["vs code"], "visual studio code": ["vs code"],
    "notepad": ["notepad"], "notepad++": ["notepad++", r"%ProgramFiles%\Notepad++\notepad++.exe"],
    "explorer": ["explorer"], "file explorer": ["explorer"], "calculator": ["calc"], "calc": ["calc"],
    "paint": ["mspaint"], "cmd": ["cmd"], "command prompt": ["cmd"], "powershell": ["powershell"],
    "terminal": ["wt", "powershell"], "windows terminal": ["wt"], "task manager": ["taskmgr"], "settings": ["ms-settings:"],
    "spotify": ["spotify", r"%AppData%\Spotify\Spotify.exe"], "discord": [r"%LocalAppData%\Discord\Update.exe --processStart Discord.exe"],
    "slack": [r"%LocalAppData%\slack\slack.exe"], "teams": ["ms-teams", r"%LocalAppData%\Microsoft\Teams\current\Teams.exe"],
    "word": ["winword"], "excel": ["excel"], "powerpoint": ["powerpnt"], "outlook": ["outlook"], "onenote": ["onenote"],
    "whatsapp": ["whatsapp:"], "telegram": [r"%AppData%\Telegram Desktop\Telegram.exe"], "zoom": [r"%AppData%\Zoom\bin\Zoom.exe"],
    "docker": ["docker desktop", r"%ProgramFiles%\Docker\Docker\Docker Desktop.exe"], "docker desktop": [r"%ProgramFiles%\Docker\Docker\Docker Desktop.exe"],
    "vlc": ["vlc", r"%ProgramFiles%\VideoLAN\VLC\vlc.exe"], "steam": [r"%ProgramFiles(x86)%\Steam\steam.exe"],
    "obsidian": [r"%LocalAppData%\Obsidian\Obsidian.exe"], "postman": [r"%LocalAppData%\Postman\Postman.exe"],
    "snipping tool": ["snippingtool"], "control panel": ["control"], "registry editor": ["regedit"],
}

# Process names we refuse to kill (system stability)
PROTECTED_PROCESSES = {"system", "system idle process", "csrss.exe", "wininit.exe", "winlogon.exe", "services.exe", "lsass.exe",
                       "smss.exe", "svchost.exe", "dwm.exe", "explorer.exe", "fontdrvhost.exe", "registry", "memory compression"}


def _expand(s: str) -> str:
    return os.path.expandvars(s)


def _resolve_app(name: str) -> Optional[str]:
    """Return an executable path/command for a friendly app name, or None."""
    key = name.strip().lower()
    seen = set()
    candidates = list(KNOWN_APPS.get(key, [name]))
    while candidates:
        c = candidates.pop(0)
        if c in seen:
            continue
        seen.add(c)
        if c.lower() in KNOWN_APPS and c.lower() != key:
            candidates.extend(KNOWN_APPS[c.lower()])
            continue
        if c.endswith(":"):  # URI scheme (ms-settings:, whatsapp:)
            return c
        expanded = _expand(c)
        if os.path.isfile(expanded):
            return expanded
        exe = shutil.which(expanded) or shutil.which(expanded + ".exe")
        if exe:
            return exe
        # "Update.exe --processStart X" style
        head = expanded.split(" --")[0]
        if os.path.isfile(head):
            return expanded
    # Search Start Menu shortcuts
    if IS_WIN:
        for root in (_expand(r"%AppData%\Microsoft\Windows\Start Menu\Programs"), _expand(r"%ProgramData%\Microsoft\Windows\Start Menu\Programs")):
            for dirpath, _, files in os.walk(root):
                for f in files:
                    if f.lower().endswith(".lnk") and key in f.lower():
                        return os.path.join(dirpath, f)
    return None


def _window_titles() -> Dict[int, str]:
    """pid -> main window title (Windows only, via user32)."""
    titles: Dict[int, str] = {}
    if not IS_WIN:
        return titles
    user32 = ctypes.windll.user32
    EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def cb(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            length = user32.GetWindowTextLengthW(hwnd)
            if length:
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                pid = ctypes.c_ulong()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                titles.setdefault(pid.value, buf.value)
        return True

    user32.EnumWindows(EnumWindowsProc(cb), 0)
    return titles


def _focus_window(match: str) -> Optional[str]:
    if not IS_WIN:
        return None
    user32 = ctypes.windll.user32
    found: List[tuple] = []
    EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    m = match.lower()

    def cb(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            length = user32.GetWindowTextLengthW(hwnd)
            if length:
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                pid = ctypes.c_ulong()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                try:
                    pname = psutil.Process(pid.value).name().lower()
                except Exception:  # noqa: BLE001
                    pname = ""
                if m in buf.value.lower() or m in pname or m.replace(" ", "") in pname:
                    found.append((hwnd, buf.value))
        return True

    user32.EnumWindows(EnumWindowsProc(cb), 0)
    if not found:
        return None
    hwnd, title = found[0]
    SW_RESTORE = 9
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    # Trick to bypass foreground lock: simulate ALT key press
    try:
        ctypes.windll.user32.keybd_event(0x12, 0, 0, 0)
        ctypes.windll.user32.keybd_event(0x12, 0, 2, 0)
    except Exception:  # noqa: BLE001
        pass
    user32.SetForegroundWindow(hwnd)
    return title


# ---------------------------------------------------------------- tools
class NoArgs(BaseModel):
    pass


class ListAppsArgs(BaseModel):
    only_with_windows: bool = Field(default=True, description="Only list apps that have a visible window")
    limit: int = Field(default=60, ge=1, le=500)


class ListApplicationsTool(Tool):
    name = "list_running_applications"
    description = "List running applications (process name, PID, window title, memory)."
    category = "computer"
    risk_level = RiskLevel.READ_ONLY
    args_model = ListAppsArgs
    available_in_cloud = False

    async def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        def _list():
            titles = _window_titles()
            out = []
            for p in psutil.process_iter(["pid", "name", "memory_info", "create_time"]):
                try:
                    title = titles.get(p.info["pid"])
                    if args.get("only_with_windows", True) and not title:
                        continue
                    out.append({"pid": p.info["pid"], "name": p.info["name"], "title": title,
                                "memory_mb": round((p.info["memory_info"].rss if p.info["memory_info"] else 0) / 1048576, 1)})
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
            out.sort(key=lambda x: -x["memory_mb"])
            return out

        apps = await run_sync(_list)
        return ToolResult.ok({"count": len(apps), "applications": apps[: args.get("limit", 60)]}, summary=f"{len(apps)} running apps")


class AppNameArgs(BaseModel):
    name: str = Field(description="Application name (e.g. 'Chrome', 'VS Code', 'Spotify') or executable path")
    arguments: str = Field(default="", description="Optional command-line arguments (e.g. a URL or file to open)")


class LaunchApplicationTool(Tool):
    name = "launch_application"
    description = "Launch an application by name (Chrome, VS Code, Notepad, Spotify, Explorer...) or by executable path, optionally with arguments."
    category = "computer"
    risk_level = RiskLevel.SAFE
    args_model = AppNameArgs
    available_in_cloud = False

    def classify(self, args: Dict[str, Any]) -> RiskLevel:
        name = args.get("name", "").lower()
        if name in ("regedit", "registry editor", "diskpart", "cmd", "powershell", "terminal", "windows terminal", "command prompt"):
            return RiskLevel.SENSITIVE
        return RiskLevel.SAFE

    def describe(self, args: Dict[str, Any]) -> str:
        return f"Launch {args.get('name')}" + (f" with '{args.get('arguments')}'" if args.get("arguments") else "")

    async def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        name = args["name"]
        target = await run_sync(_resolve_app, name)
        if target is None:
            return ToolResult.fail(f"I couldn't find an application called '{name}' on this computer.")
        extra = args.get("arguments", "").strip()
        try:
            if target.endswith(":") or target.lower().endswith(".lnk"):
                os.startfile(target + (extra if target.endswith(":") else ""))  # type: ignore[attr-defined]
            elif " --" in target and not os.path.isfile(target):
                subprocess.Popen(target + (" " + extra if extra else ""), shell=False)
            else:
                cmd = [target] + (extra.split() if extra else [])
                subprocess.Popen(cmd, creationflags=getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        except OSError as e:
            return ToolResult.fail(f"Windows couldn't start '{name}': {e.strerror or e}")
        ctx.activity(f"Launched {name}")
        return ToolResult.ok({"launched": name, "target": target}, summary=f"Launched {name}")


class CloseAppArgs(BaseModel):
    name: str = Field(description="Application/process name or window title fragment, e.g. 'Spotify', 'notepad'")
    force: bool = Field(default=False, description="Kill immediately instead of asking the app to close")


class CloseApplicationTool(Tool):
    name = "close_application"
    description = "Close a running application by name. Asks the app to close gracefully; force=true kills it."
    category = "computer"
    risk_level = RiskLevel.SENSITIVE
    args_model = CloseAppArgs
    available_in_cloud = False

    def classify(self, args: Dict[str, Any]) -> RiskLevel:
        return RiskLevel.DANGEROUS if args.get("force") else RiskLevel.SENSITIVE

    def describe(self, args: Dict[str, Any]) -> str:
        return f"{'Force kill' if args.get('force') else 'Close'} {args.get('name')}"

    async def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        name = args["name"].lower().strip()
        key = name.replace(" ", "")
        procs = []
        for p in psutil.process_iter(["pid", "name"]):
            pname = (p.info["name"] or "").lower()
            if pname in PROTECTED_PROCESSES:
                continue
            if key in pname.replace(" ", "") or key == pname.replace(".exe", ""):
                procs.append(p)
        if not procs:
            titles = _window_titles()
            pids = [pid for pid, t in titles.items() if name in t.lower()]
            procs = [psutil.Process(pid) for pid in pids if psutil.pid_exists(pid)]
        if not procs:
            return ToolResult.fail(f"'{args['name']}' doesn't appear to be running.")
        closed = []
        for p in procs:
            try:
                if args.get("force"):
                    p.kill()
                else:
                    p.terminate()
                closed.append(p.pid)
            except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
                return ToolResult.fail(f"Couldn't close {p.name()} (PID {p.pid}): {e.__class__.__name__}")
        await run_sync(psutil.wait_procs, procs, 5)
        still = [p.pid for p in procs if p.is_running()]
        if still and not args.get("force"):
            return ToolResult.fail(f"Asked {args['name']} to close but {len(still)} process(es) are still running. Use force=true to kill.")
        return ToolResult.ok({"closed_pids": closed}, summary=f"Closed {args['name']}")


class FocusArgs(BaseModel):
    name: str = Field(description="Application name or window title fragment")


class FocusApplicationTool(Tool):
    name = "focus_application"
    description = "Bring an application's window to the foreground ('switch to Chrome')."
    category = "computer"
    risk_level = RiskLevel.SAFE
    args_model = FocusArgs
    available_in_cloud = False

    async def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        title = await run_sync(_focus_window, args["name"])
        if title is None:
            return ToolResult.fail(f"No window matching '{args['name']}' was found.")
        return ToolResult.ok({"focused": title}, summary=f"Focused '{title}'")


class IsRunningTool(Tool):
    name = "is_application_running"
    description = "Check whether an application/process is running."
    category = "computer"
    risk_level = RiskLevel.READ_ONLY
    args_model = FocusArgs
    available_in_cloud = False

    async def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        key = args["name"].lower().replace(" ", "")
        hits = [{"pid": p.info["pid"], "name": p.info["name"]} for p in psutil.process_iter(["pid", "name"])
                if key in (p.info["name"] or "").lower().replace(" ", "")]
        return ToolResult.ok({"running": bool(hits), "processes": hits[:20]}, summary=f"{args['name']} {'is' if hits else 'is not'} running")


# ------------------------------------------------------------- clipboard
class ClipboardReadTool(Tool):
    name = "read_clipboard"
    description = "Read the current text content of the clipboard."
    category = "computer"
    risk_level = RiskLevel.READ_ONLY
    args_model = NoArgs
    available_in_cloud = False

    async def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            import pyperclip

            text = await run_sync(pyperclip.paste)
        except Exception as e:  # noqa: BLE001
            return ToolResult.fail(f"Clipboard read failed: {e}")
        return ToolResult.ok({"text": text[:20000], "length": len(text)}, summary="Read clipboard", untrusted=True, source="clipboard")


class ClipboardWriteArgs(BaseModel):
    text: str = Field(max_length=100000)


class ClipboardWriteTool(Tool):
    name = "write_clipboard"
    description = "Copy text to the clipboard."
    category = "computer"
    risk_level = RiskLevel.SAFE
    args_model = ClipboardWriteArgs
    available_in_cloud = False
    log_arguments = False

    async def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            import pyperclip

            await run_sync(pyperclip.copy, args["text"])
        except Exception as e:  # noqa: BLE001
            return ToolResult.fail(f"Clipboard write failed: {e}")
        return ToolResult.ok({"copied_chars": len(args["text"])}, summary="Copied to clipboard")


# ------------------------------------------------------------ screenshot
class ScreenshotArgs(BaseModel):
    save_to: str = Field(default="", description="Optional file path to save the PNG (default: data/screenshots/)")


class ScreenshotTool(Tool):
    name = "take_screenshot"
    description = "Capture the screen to a PNG file and return its path (and a thumbnail for the UI)."
    category = "computer"
    risk_level = RiskLevel.READ_ONLY
    args_model = ScreenshotArgs
    available_in_cloud = False

    async def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            from PIL import ImageGrab
        except ImportError:
            return ToolResult.fail("Pillow is not installed (pip install pillow).")
        if args.get("save_to"):
            out = resolve_path(args["save_to"], ctx.settings.allowed_roots, for_write=True)
        else:
            d = ctx.settings.data_dir / "screenshots"
            d.mkdir(parents=True, exist_ok=True)
            out = d / f"screenshot_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"

        def _grab():
            img = ImageGrab.grab(all_screens=True)
            img.save(out)
            thumb = img.copy()
            thumb.thumbnail((640, 400))
            import io

            buf = io.BytesIO()
            thumb.save(buf, format="PNG")
            return img.size, base64.b64encode(buf.getvalue()).decode()

        try:
            size, thumb_b64 = await run_sync(_grab)
        except Exception as e:  # noqa: BLE001
            return ToolResult.fail(f"Screenshot failed: {e}")
        return ToolResult.ok({"path": str(out), "width": size[0], "height": size[1]}, summary=f"Screenshot saved to {out.name}",
                             artifacts={"image_b64": thumb_b64, "path": str(out)})


# -------------------------------------------------------------- system
class SystemInfoTool(Tool):
    name = "get_system_info"
    description = "Get system information: OS, CPU, RAM, disks, uptime, battery, current user."
    category = "computer"
    risk_level = RiskLevel.READ_ONLY
    args_model = NoArgs

    async def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        def _info():
            vm = psutil.virtual_memory()
            disks = []
            for part in psutil.disk_partitions(all=False):
                try:
                    u = psutil.disk_usage(part.mountpoint)
                    disks.append({"drive": part.device, "fs": part.fstype, "total_gb": round(u.total / 2**30, 1),
                                  "used_gb": round(u.used / 2**30, 1), "free_gb": round(u.free / 2**30, 1), "percent": u.percent})
                except OSError:
                    continue
            bat = None
            try:
                b = psutil.sensors_battery()
                if b:
                    bat = {"percent": b.percent, "plugged_in": b.power_plugged,
                           "minutes_left": None if b.secsleft in (psutil.POWER_TIME_UNLIMITED, psutil.POWER_TIME_UNKNOWN) else b.secsleft // 60}
            except Exception:  # noqa: BLE001
                pass
            return {
                "os": f"{platform.system()} {platform.release()} ({platform.version()})", "machine": platform.machine(),
                "hostname": socket.gethostname(), "user": os.environ.get("USERNAME") or os.environ.get("USER"),
                "python": platform.python_version(),
                "cpu": {"model": platform.processor(), "cores_physical": psutil.cpu_count(logical=False), "cores_logical": psutil.cpu_count(),
                        "usage_percent": psutil.cpu_percent(interval=0.5), "frequency_mhz": round(psutil.cpu_freq().current) if psutil.cpu_freq() else None},
                "memory": {"total_gb": round(vm.total / 2**30, 1), "used_gb": round(vm.used / 2**30, 1), "available_gb": round(vm.available / 2**30, 1), "percent": vm.percent},
                "disks": disks, "battery": bat,
                "uptime_hours": round((time.time() - psutil.boot_time()) / 3600, 1),
                "boot_time": datetime.fromtimestamp(psutil.boot_time()).isoformat(timespec="seconds"),
            }

        return ToolResult.ok(await run_sync(_info), summary="System info collected")


class ResourceUsageTool(Tool):
    name = "get_resource_usage"
    description = "Current CPU, RAM and disk usage plus the top processes by CPU and memory."
    category = "computer"
    risk_level = RiskLevel.READ_ONLY
    args_model = NoArgs

    async def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        def _usage():
            psutil.cpu_percent(interval=None)
            procs = []
            for p in psutil.process_iter(["pid", "name", "memory_info"]):
                try:
                    p.cpu_percent(None)
                    procs.append(p)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            time.sleep(0.8)
            rows = []
            for p in procs:
                try:
                    rows.append({"pid": p.pid, "name": p.info["name"], "cpu_percent": p.cpu_percent(None),
                                 "memory_mb": round(p.info["memory_info"].rss / 1048576, 1) if p.info["memory_info"] else 0})
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            vm = psutil.virtual_memory()
            return {"cpu_percent": psutil.cpu_percent(interval=None), "memory_percent": vm.percent,
                    "memory_used_gb": round(vm.used / 2**30, 1), "memory_total_gb": round(vm.total / 2**30, 1),
                    "top_cpu": sorted(rows, key=lambda r: -r["cpu_percent"])[:8],
                    "top_memory": sorted(rows, key=lambda r: -r["memory_mb"])[:8]}

        return ToolResult.ok(await run_sync(_usage), summary="Resource usage collected")


class NetworkStatusTool(Tool):
    name = "get_network_status"
    description = "Check internet connectivity, local IP addresses, and active network interfaces."
    category = "computer"
    risk_level = RiskLevel.READ_ONLY
    args_model = NoArgs

    async def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        def _net():
            online = False
            latency_ms = None
            try:
                t = time.monotonic()
                with socket.create_connection(("1.1.1.1", 53), timeout=3):
                    online = True
                latency_ms = round((time.monotonic() - t) * 1000)
            except OSError:
                pass
            dns_ok = False
            try:
                socket.gethostbyname("example.com")
                dns_ok = True
            except OSError:
                pass
            ifaces = []
            stats = psutil.net_if_stats()
            for name, addrs in psutil.net_if_addrs().items():
                st = stats.get(name)
                if st and not st.isup:
                    continue
                ips = [a.address for a in addrs if a.family == socket.AF_INET and not a.address.startswith("127.")]
                if ips:
                    ifaces.append({"interface": name, "ipv4": ips, "speed_mbps": st.speed if st else None})
            io = psutil.net_io_counters()
            return {"internet": online, "dns": dns_ok, "latency_ms": latency_ms, "interfaces": ifaces,
                    "bytes_sent_mb": round(io.bytes_sent / 1048576, 1), "bytes_recv_mb": round(io.bytes_recv / 1048576, 1)}

        r = await run_sync(_net)
        return ToolResult.ok(r, summary="Online" if r["internet"] else "Offline")


class LockComputerTool(Tool):
    name = "lock_computer"
    description = "Lock the Windows session (like Win+L)."
    category = "computer"
    risk_level = RiskLevel.SENSITIVE
    args_model = NoArgs
    available_in_cloud = False

    async def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        if not IS_WIN:
            return ToolResult.not_implemented("lock_computer on non-Windows platforms")
        ok = ctypes.windll.user32.LockWorkStation()
        return ToolResult.ok({"locked": bool(ok)}, summary="Locked") if ok else ToolResult.fail("Windows refused to lock the workstation.")


class PowerArgs(BaseModel):
    action: str = Field(description="sleep | shutdown | restart | hibernate", pattern="^(sleep|shutdown|restart|hibernate)$")
    delay_seconds: int = Field(default=10, ge=0, le=3600)


class PowerTool(Tool):
    name = "power_action"
    description = "Sleep, hibernate, shut down or restart the computer (always requires confirmation)."
    category = "computer"
    risk_level = RiskLevel.DANGEROUS
    requires_confirmation = True
    args_model = PowerArgs
    available_in_cloud = False

    def describe(self, args: Dict[str, Any]) -> str:
        return f"{args.get('action', '').capitalize()} the computer in {args.get('delay_seconds', 10)}s"

    async def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        if not IS_WIN:
            return ToolResult.not_implemented("power_action on non-Windows platforms")
        action, delay = args["action"], args.get("delay_seconds", 10)
        if action == "sleep":
            cmd = ["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"]
        elif action == "hibernate":
            cmd = ["shutdown", "/h"]
        elif action == "restart":
            cmd = ["shutdown", "/r", "/t", str(delay)]
        else:
            cmd = ["shutdown", "/s", "/t", str(delay)]
        try:
            subprocess.Popen(cmd)
        except OSError as e:
            return ToolResult.fail(f"Could not {action}: {e}")
        return ToolResult.ok({"action": action, "delay_seconds": delay}, summary=f"{action} scheduled")


def _build(settings) -> List[Tool]:
    return [ListApplicationsTool(), LaunchApplicationTool(), CloseApplicationTool(), FocusApplicationTool(), IsRunningTool(),
            ClipboardReadTool(), ClipboardWriteTool(), ScreenshotTool(), SystemInfoTool(), ResourceUsageTool(),
            NetworkStatusTool(), LockComputerTool(), PowerTool()]


PLUGIN = Plugin(
    name="computer",
    description="Applications, clipboard, screenshots, system information, power",
    permissions=["computer"],
    tools=_build,
)
