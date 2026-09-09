"""What the machine is actually doing: CPU, memory, GPU, disks, network, battery.

Zeta runs its own models on the user's hardware, so "why is it slow" is usually a question
about the machine rather than about the code. This module answers it with measurements, and -
just as importantly - says plainly when a reading is not available rather than inventing one.

Two things are worth knowing about Windows:

* **CPU temperature is usually not readable.** `psutil.sensors_temperatures` does not exist on
  Windows at all, and the WMI thermal zone (`MSAcpi_ThermalZoneTemperature`) answers "Access
  denied" unless the process is elevated. Many laptops do not expose it even then. Zeta reports
  the reason instead of guessing; `LibreHardwareMonitor` running in the background exposes a WMI
  namespace that does work, and Zeta picks that up automatically if it is there.
* **Per-process VRAM is usually not readable either** under the WDDM driver model - `nvidia-smi`
  prints `N/A` for it. The process *names* still come through, which is the part that matters:
  it is how you discover a game is using the card Zeta wanted.

Rates (disk and network throughput, CPU percent) are differences between calls, so the first
snapshot after start-up reports zero for those.

Readings are taken by a background `Sampler` rather than on the request path. Spawning
nvidia-smi is normally 60 ms but occasionally seconds when the machine is under load - which is
exactly when someone is watching the monitor - and a panel that stalls while reporting that the
machine is busy is worse than useless.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import time
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

_WINDOWS = os.name == "nt"
_NO_WINDOW = 0x08000000 if _WINDOWS else 0        # keep console windows from flashing up

# nvidia-smi fields, in the order they are parsed below.
_GPU_FIELDS = ("name", "utilization.gpu", "utilization.memory", "memory.used", "memory.total",
               "temperature.gpu", "power.draw", "power.limit", "clocks.sm", "clocks.max.sm", "fan.speed")

_last_io: Dict[str, Tuple[float, Any, Any]] = {}   # previous counters, for per-second rates
_cache: Dict[str, Tuple[float, Any]] = {}          # slow readings, refreshed on their own schedule
_cpu_temp_source: Optional[str] = None             # None = not probed yet, "" = known unavailable
_cpu_temp_reason = ""


def _cached(key: str, ttl: float, produce):
    """Re-use a slow reading for `ttl` seconds.

    Walking every process costs about 0.8 s on this machine, which is far too much for a panel
    that polls every couple of seconds - and the answer barely changes in that time. The cheap
    readings (CPU, memory, GPU counters) are always taken fresh.
    """
    now = time.monotonic()
    hit = _cache.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    value = produce()
    _cache[key] = (now, value)
    return value


def _run(cmd: List[str], timeout: float = 4.0) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=True,
                              creationflags=_NO_WINDOW).stdout
    except Exception as e:  # noqa: BLE001
        log.debug("%s failed: %s", cmd[0], e)
        return ""


def _num(text: str) -> Optional[float]:
    """nvidia-smi writes '[N/A]' and '[Not Supported]' for fields a laptop does not expose."""
    text = text.strip()
    if not text or text.startswith("[") or text.lower() in ("n/a", "not supported"):
        return None
    try:
        return float(text.split()[0])
    except ValueError:
        return None


# --------------------------------------------------------------------------- CPU
def cpu() -> Dict[str, Any]:
    import psutil

    freq = None
    try:
        f = psutil.cpu_freq()
        freq = {"current_mhz": round(f.current), "max_mhz": round(f.max) or None} if f else None
    except Exception:  # noqa: BLE001
        pass
    temp, reason = cpu_temperature()
    return {
        "percent": psutil.cpu_percent(interval=None),
        "per_core": psutil.cpu_percent(interval=None, percpu=True),
        "cores_logical": psutil.cpu_count(logical=True),
        "cores_physical": psutil.cpu_count(logical=False),
        "frequency": freq,
        "temperature_c": temp,
        "temperature_detail": reason,
    }


def cpu_temperature() -> Tuple[Optional[float], str]:
    """Core temperature, or None with a plain-English reason it cannot be read."""
    global _cpu_temp_source, _cpu_temp_reason

    import psutil

    sensors = getattr(psutil, "sensors_temperatures", None)
    if sensors:                                   # Linux, macOS with the right kext
        try:
            for readings in (sensors() or {}).values():
                for r in readings:
                    if r.current:
                        return round(r.current, 1), ""
        except Exception:  # noqa: BLE001
            pass
    if not _WINDOWS:
        return None, "no temperature sensor is exposed to this process"
    if _cpu_temp_source == "":
        return None, _cpu_temp_reason
    temp = _cached("cpu_temp", 5.0, _windows_cpu_temp)
    if temp is None and _cpu_temp_source is None:
        _cpu_temp_source = ""
        _cpu_temp_reason = ("Windows does not expose CPU temperature to a normal process. Running "
                            "LibreHardwareMonitor in the background makes it readable, and Zeta will "
                            "pick it up automatically.")
    return temp, "" if temp is not None else _cpu_temp_reason


# Both Windows sources in one shot: starting PowerShell costs about a second, and asking it two
# questions costs no more than asking it one. Prints "lhm <celsius>" or "acpi <decikelvin>".
_TEMP_PROBE = """
$s = Get-CimInstance -Namespace root/LibreHardwareMonitor -ClassName Sensor -ErrorAction SilentlyContinue |
     Where-Object { $_.SensorType -eq 'Temperature' -and $_.Name -like '*CPU*' } |
     Measure-Object -Property Value -Maximum
if ($s -and $s.Maximum) { "lhm " + $s.Maximum; exit }
$z = Get-CimInstance -Namespace root/WMI -ClassName MSAcpi_ThermalZoneTemperature -ErrorAction SilentlyContinue |
     Select-Object -First 1
if ($z -and $z.CurrentTemperature) { "acpi " + $z.CurrentTemperature }
"""


def _windows_cpu_temp() -> Optional[float]:
    """LibreHardwareMonitor if it is running, otherwise the ACPI thermal zone (usually blocked)."""
    global _cpu_temp_source

    out = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", _TEMP_PROBE], timeout=8.0).split()
    if len(out) < 2:
        return None
    source, value = out[0], _num(out[1])
    if value is None:
        return None
    _cpu_temp_source = source
    if source == "acpi":                          # tenths of a kelvin
        return round(value / 10.0 - 273.15, 1)
    return round(value, 1)


# --------------------------------------------------------------------------- memory
def memory() -> Dict[str, Any]:
    import psutil

    m = psutil.virtual_memory()
    s = psutil.swap_memory()
    return {"used_mb": round(m.used / 2 ** 20), "total_mb": round(m.total / 2 ** 20), "percent": m.percent,
            "available_mb": round(m.available / 2 ** 20),
            "swap_used_mb": round(s.used / 2 ** 20), "swap_total_mb": round(s.total / 2 ** 20)}


# --------------------------------------------------------------------------- GPU
def gpu() -> Dict[str, Any]:
    """The NVIDIA card, if there is one. `present: False` on machines without."""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return {"present": False, "detail": "no NVIDIA GPU detected (nvidia-smi is not installed)"}
    out = _run([exe, f"--query-gpu={','.join(_GPU_FIELDS)}", "--format=csv,noheader,nounits"])
    line = out.strip().splitlines()[0] if out.strip() else ""
    if not line:
        return {"present": False, "detail": "nvidia-smi did not answer"}
    parts = [p.strip() for p in line.split(",")]
    parts += [""] * (len(_GPU_FIELDS) - len(parts))
    used, total = _num(parts[3]), _num(parts[4])
    return {
        "present": True,
        "name": parts[0],
        "utilization": _num(parts[1]),
        "memory_utilization": _num(parts[2]),
        "memory_used_mb": used,
        "memory_total_mb": total,
        "memory_percent": round(used / total * 100, 1) if used is not None and total else None,
        "temperature_c": _num(parts[5]),
        "power_w": _num(parts[6]),
        "power_limit_w": _num(parts[7]),
        "clock_mhz": _num(parts[8]),
        "clock_max_mhz": _num(parts[9]),
        "fan_percent": _num(parts[10]),
        "processes": _cached("gpu_procs", 10.0, lambda: gpu_processes(exe)),
    }


def gpu_processes(exe: str = "") -> List[Dict[str, Any]]:
    """Who else is on the card. Per-process VRAM is usually `N/A` on Windows; names are not."""
    exe = exe or shutil.which("nvidia-smi") or ""
    if not exe:
        return []
    out = _run([exe, "--query-compute-apps=pid,process_name,used_memory", "--format=csv,noheader,nounits"])
    rows: List[Dict[str, Any]] = []
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 2:
            continue
        rows.append({"pid": int(_num(parts[0]) or 0), "name": os.path.basename(parts[1]),
                     "memory_mb": _num(parts[2]) if len(parts) > 2 else None})
    return rows


# --------------------------------------------------------------------------- disks, network
def disks() -> List[Dict[str, Any]]:
    import psutil

    out: List[Dict[str, Any]] = []
    for part in psutil.disk_partitions(all=False):
        if _WINDOWS and "cdrom" in part.opts:
            continue
        try:
            u = psutil.disk_usage(part.mountpoint)
        except OSError:                            # an empty card reader, a disconnected drive
            continue
        out.append({"mount": part.mountpoint, "used_gb": round(u.used / 2 ** 30, 1),
                    "total_gb": round(u.total / 2 ** 30, 1), "percent": u.percent})
    return out


def _rate(key: str, read: float, write: float) -> Dict[str, float]:
    """Per-second throughput from the difference since the last snapshot."""
    now = time.monotonic()
    previous = _last_io.get(key)
    _last_io[key] = (now, read, write)
    if not previous:
        return {"read_mb_s": 0.0, "write_mb_s": 0.0}
    elapsed = now - previous[0]
    if elapsed <= 0:
        return {"read_mb_s": 0.0, "write_mb_s": 0.0}
    return {"read_mb_s": round(max(0.0, read - previous[1]) / elapsed / 2 ** 20, 2),
            "write_mb_s": round(max(0.0, write - previous[2]) / elapsed / 2 ** 20, 2)}


def io_rates() -> Dict[str, Any]:
    import psutil

    out: Dict[str, Any] = {}
    try:
        d = psutil.disk_io_counters()
        out["disk"] = _rate("disk", d.read_bytes, d.write_bytes) if d else None
    except Exception:  # noqa: BLE001
        out["disk"] = None
    try:
        n = psutil.net_io_counters()
        r = _rate("net", n.bytes_recv, n.bytes_sent)
        out["network"] = {"down_mb_s": r["read_mb_s"], "up_mb_s": r["write_mb_s"]}
    except Exception:  # noqa: BLE001
        out["network"] = None
    return out


# --------------------------------------------------------------------------- the rest
def battery() -> Optional[Dict[str, Any]]:
    import psutil

    try:
        b = psutil.sensors_battery()
    except Exception:  # noqa: BLE001
        return None
    if not b:
        return None
    minutes = None
    if isinstance(b.secsleft, int) and b.secsleft >= 0:
        minutes = round(b.secsleft / 60)
    return {"percent": round(b.percent), "plugged": bool(b.power_plugged), "minutes_left": minutes}


def top_processes(limit: int = 6) -> List[Dict[str, Any]]:
    """The heaviest processes by memory - the usual answer to "what is eating this machine"."""
    import psutil

    rows: List[Dict[str, Any]] = []
    for p in psutil.process_iter(["name", "memory_info"]):
        try:
            info = p.info
            rss = info["memory_info"].rss if info.get("memory_info") else 0
        except (psutil.NoSuchProcess, psutil.AccessDenied, AttributeError, KeyError):
            continue
        if rss:
            rows.append({"pid": p.pid, "name": info.get("name") or "?", "memory_mb": round(rss / 2 ** 20)})
    rows.sort(key=lambda r: r["memory_mb"], reverse=True)
    return rows[:limit]


def snapshot(*, processes: bool = True) -> Dict[str, Any]:
    """One reading of everything. Call it from a thread: nvidia-smi takes a few tens of ms."""
    import psutil

    return {
        "at": time.time(),
        "uptime_s": round(time.time() - psutil.boot_time()),
        "cpu": cpu(),
        "memory": memory(),
        "gpu": gpu(),
        "disks": _cached("disks", 20.0, disks),
        **io_rates(),
        "battery": _cached("battery", 10.0, battery),
        "processes": _cached("top", 30.0, top_processes) if processes else [],
    }


class Sampler:
    """Keeps a fresh reading available, but only while someone is looking at it.

    Polling the hardware forever in the background would be its own small tax on the machine, so
    the loop starts on the first request and stops once nothing has asked for `idle_stop`
    seconds. Callers always get an answer immediately: the first one waits for a reading, and
    everyone after that gets the most recent one.
    """

    def __init__(self, interval: float = 3.0, idle_stop: float = 20.0):
        self.interval = interval
        self.idle_stop = idle_stop
        self._latest: Optional[Dict[str, Any]] = None
        self._task: Optional[asyncio.Task] = None
        self._last_asked = 0.0

    async def get(self) -> Dict[str, Any]:
        self._last_asked = time.monotonic()
        self._ensure_running()
        if self._latest is None:
            self._latest = await self._take()
        return self._latest

    @staticmethod
    async def _take() -> Dict[str, Any]:
        return await asyncio.get_running_loop().run_in_executor(None, snapshot)

    def _ensure_running(self) -> None:
        if self._task and not self._task.done():
            return
        try:
            self._task = asyncio.get_running_loop().create_task(self._loop())
        except RuntimeError:
            self._task = None          # no loop (a synchronous caller): get() samples directly

    async def _loop(self) -> None:
        try:
            while time.monotonic() - self._last_asked < self.idle_stop:
                await asyncio.sleep(self.interval)
                try:
                    self._latest = await self._take()
                except Exception as e:  # noqa: BLE001
                    log.debug("metrics sampling failed: %s", e)
        except asyncio.CancelledError:
            raise

    def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None


sampler = Sampler()
