"""The hardware monitor: real readings, and honest gaps.

The rule this module is held to is that it never invents a number. On Windows the CPU
temperature is usually unreadable (`psutil.sensors_temperatures` does not exist there, and the
WMI thermal zone answers "Access denied" to a normal process), so the panel has to be able to
say so rather than show a plausible-looking figure.
"""

from __future__ import annotations

import pytest

from app.core import metrics


@pytest.fixture(autouse=True)
def _clear_caches():
    metrics._cache.clear()
    metrics._last_io.clear()
    yield
    metrics._cache.clear()
    metrics._last_io.clear()


# ------------------------------------------------------------------ parsing
@pytest.mark.parametrize("text,expected", [
    ("42", 42.0), (" 63 ", 63.0), ("22.12 W", 22.12),
    ("[N/A]", None), ("[Not Supported]", None), ("", None), ("N/A", None), ("banana", None),
])
def test_nvidia_smi_placeholders_are_not_mistaken_for_readings(text, expected):
    """`[N/A]` is what a laptop prints for fan speed and power limit. It is not zero."""
    assert metrics._num(text) == expected


def test_a_missing_gpu_is_reported_not_faked(monkeypatch):
    monkeypatch.setattr(metrics.shutil, "which", lambda _: None)
    g = metrics.gpu()
    assert g["present"] is False and "NVIDIA" in g["detail"]


def test_gpu_fields_are_parsed_in_order(monkeypatch):
    row = "NVIDIA GeForce RTX 4070 Laptop GPU, 21, 4, 3501, 8188, 58, 22.12, [N/A], 1980, 3105, [N/A]"
    monkeypatch.setattr(metrics.shutil, "which", lambda _: "nvidia-smi")
    monkeypatch.setattr(metrics, "_run", lambda *a, **k: row)
    monkeypatch.setattr(metrics, "gpu_processes", lambda *a, **k: [])
    g = metrics.gpu()
    assert g["name"] == "NVIDIA GeForce RTX 4070 Laptop GPU"
    assert g["utilization"] == 21 and g["memory_used_mb"] == 3501 and g["temperature_c"] == 58
    assert g["memory_percent"] == pytest.approx(42.8, abs=0.1)
    assert g["power_limit_w"] is None and g["fan_percent"] is None    # not exposed on this laptop


def test_gpu_processes_survive_missing_per_process_vram(monkeypatch):
    """Under WDDM the memory column is `[N/A]`; the process names still matter."""
    monkeypatch.setattr(metrics.shutil, "which", lambda _: "nvidia-smi")
    monkeypatch.setattr(metrics, "_run",
                        lambda *a, **k: "50680, C:\\Games\\VALORANT-Win64-Shipping.exe, [N/A]\n"
                                        "15816, C:\\Ollama\\llama-server.exe, 5900")
    rows = metrics.gpu_processes()
    assert [r["name"] for r in rows] == ["VALORANT-Win64-Shipping.exe", "llama-server.exe"]
    assert rows[0]["memory_mb"] is None and rows[1]["memory_mb"] == 5900


# ------------------------------------------------------------------ honesty about temperature
def test_an_unreadable_cpu_temperature_says_why(monkeypatch):
    monkeypatch.setattr(metrics, "_WINDOWS", True)
    monkeypatch.setattr(metrics, "_cpu_temp_source", None)
    monkeypatch.setattr(metrics, "_cpu_temp_reason", "")
    monkeypatch.setattr(metrics, "_windows_cpu_temp", lambda: None)
    temp, reason = metrics.cpu_temperature()
    assert temp is None
    assert "LibreHardwareMonitor" in reason      # tells the user what would fix it


def test_the_temperature_probe_is_not_repeated_once_it_is_known_to_fail(monkeypatch):
    """Spawning PowerShell costs a second; a panel polling every two seconds must not pay it."""
    monkeypatch.setattr(metrics, "_WINDOWS", True)
    monkeypatch.setattr(metrics, "_cpu_temp_source", None)
    monkeypatch.setattr(metrics, "_cpu_temp_reason", "")
    calls = []
    monkeypatch.setattr(metrics, "_windows_cpu_temp", lambda: calls.append(1))
    for _ in range(5):
        metrics.cpu_temperature()
    assert len(calls) == 1


def test_a_working_sensor_is_used(monkeypatch):
    monkeypatch.setattr(metrics, "_WINDOWS", True)
    monkeypatch.setattr(metrics, "_cpu_temp_source", "lhm")
    monkeypatch.setattr(metrics, "_windows_cpu_temp", lambda: 61.5)
    assert metrics.cpu_temperature() == (61.5, "")


# ------------------------------------------------------------------ rates and caching
def test_the_first_reading_reports_no_throughput():
    """There is nothing to subtract from yet, so zero is the truthful answer."""
    assert metrics._rate("disk", 1_000_000, 500_000) == {"read_mb_s": 0.0, "write_mb_s": 0.0}


def test_throughput_is_the_difference_between_calls(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(metrics.time, "monotonic", lambda: clock[0])
    metrics._rate("disk", 0, 0)
    clock[0] = 102.0                                    # two seconds later
    rate = metrics._rate("disk", 2 * 2 ** 20 * 2, 0)    # 4 MB read in 2 s
    assert rate["read_mb_s"] == pytest.approx(2.0, abs=0.01)


def test_a_counter_that_went_backwards_never_reports_a_negative_rate(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(metrics.time, "monotonic", lambda: clock[0])
    metrics._rate("net", 5_000_000, 5_000_000)
    clock[0] = 101.0
    assert metrics._rate("net", 10, 10) == {"read_mb_s": 0.0, "write_mb_s": 0.0}


def test_expensive_readings_are_reused_within_their_window(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(metrics.time, "monotonic", lambda: clock[0])
    calls = []
    produce = lambda: calls.append(1) or "value"       # noqa: E731
    assert metrics._cached("k", 8.0, produce) == "value"
    clock[0] = 5.0
    metrics._cached("k", 8.0, produce)
    assert len(calls) == 1
    clock[0] = 9.0
    metrics._cached("k", 8.0, produce)
    assert len(calls) == 2


# ------------------------------------------------------------------ the whole snapshot
def test_a_snapshot_has_everything_the_panel_draws():
    snap = metrics.snapshot(processes=False)
    for key in ("at", "uptime_s", "cpu", "memory", "gpu", "disks", "disk", "network", "battery", "processes"):
        assert key in snap, key
    assert 0 <= snap["cpu"]["percent"] <= 100
    assert snap["memory"]["total_mb"] > 0
    assert len(snap["cpu"]["per_core"]) == snap["cpu"]["cores_logical"]
    assert snap["processes"] == []                     # asked not to walk the process table


# ------------------------------------------------------------------ the background sampler
@pytest.mark.asyncio
async def test_the_first_caller_gets_a_reading_not_an_empty_frame():
    s = metrics.Sampler(interval=0.05, idle_stop=0.4)
    try:
        snap = await s.get()
        assert snap["memory"]["total_mb"] > 0
    finally:
        s.stop()


@pytest.mark.asyncio
async def test_later_callers_are_served_the_latest_reading_without_waiting(monkeypatch):
    """The point of the sampler: nvidia-smi can stall for seconds, and the panel must not."""
    import asyncio

    taken = []

    async def slow_take():
        taken.append(1)
        await asyncio.sleep(0.02)
        return {"n": len(taken)}

    s = metrics.Sampler(interval=0.05, idle_stop=1.0)
    monkeypatch.setattr(s, "_take", slow_take)
    try:
        first = await s.get()
        assert first == {"n": 1}
        await asyncio.sleep(0.18)          # the loop keeps sampling underneath
        again = await s.get()
        assert again["n"] > 1              # a newer reading, and it did not have to measure
    finally:
        s.stop()


@pytest.mark.asyncio
async def test_sampling_stops_when_nobody_is_watching():
    """Left running, this would poll the hardware forever for a panel nobody has open."""
    import asyncio

    # Generous margins: a real reading takes real time (nvidia-smi, WMI), and under a loaded
    # test run a 0.15 s idle window could elapse before the assertion below even ran.
    s = metrics.Sampler(interval=0.2, idle_stop=1.5)
    try:
        await s.get()
        assert s._task is not None and not s._task.done()
        await asyncio.sleep(2.5)
        assert s._task.done()
    finally:
        s.stop()


@pytest.mark.asyncio
async def test_a_failed_reading_does_not_kill_the_loop(monkeypatch):
    import asyncio

    calls = []

    async def flaky():
        calls.append(1)
        if len(calls) == 2:
            raise OSError("nvidia-smi went away")
        return {"n": len(calls)}

    s = metrics.Sampler(interval=0.05, idle_stop=1.0)
    monkeypatch.setattr(s, "_take", flaky)
    try:
        await s.get()
        await asyncio.sleep(0.25)
        assert len(calls) > 2               # kept going after the failure
        assert not s._task.done()
    finally:
        s.stop()
