"""Controlled terminal tool.

Commands are classified (SAFE / SENSITIVE / DANGEROUS / BLOCKED) before the
permission manager decides.  Output is captured with a timeout; long-running
commands can be started in the background and polled.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import psutil
from pydantic import BaseModel, Field

from app.plugins.loader import Plugin
from app.security.classifier import classify_command
from app.security.permissions import RiskLevel
from app.security.validators import resolve_path, validate_command
from app.tools.base import Tool, ToolContext, ToolResult

IS_WIN = sys.platform == "win32"
MAX_OUTPUT = 20000
PROTECTED = {"system", "csrss.exe", "wininit.exe", "winlogon.exe", "services.exe", "lsass.exe", "smss.exe", "svchost.exe", "dwm.exe"}


def _shell_argv(shell: str, command: str) -> List[str]:
    shell = (shell or "powershell").lower()
    if shell == "powershell":
        exe = "pwsh" if _which("pwsh") else "powershell"
        return [exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", command]
    if shell == "cmd":
        return ["cmd", "/d", "/s", "/c", command]
    return ["bash", "-lc", command]


def _which(name: str) -> Optional[str]:
    import shutil

    return shutil.which(name)


@dataclass
class BackgroundJob:
    id: str
    command: str
    proc: asyncio.subprocess.Process
    output: List[str] = field(default_factory=list)
    done: bool = False
    returncode: Optional[int] = None


class TerminalService:
    """Holds background jobs so they can be polled/killed across tool calls."""

    def __init__(self):
        self.jobs: Dict[str, BackgroundJob] = {}

    async def _pump(self, job: BackgroundJob) -> None:
        assert job.proc.stdout is not None
        async for line in job.proc.stdout:
            job.output.append(line.decode("utf-8", errors="replace"))
            if len(job.output) > 5000:
                del job.output[:1000]
        job.returncode = await job.proc.wait()
        job.done = True

    async def start(self, argv: List[str], cwd: Optional[str], command: str) -> BackgroundJob:
        proc = await asyncio.create_subprocess_exec(*argv, cwd=cwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                                                    creationflags=getattr(asyncio.subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if IS_WIN else 0)
        job = BackgroundJob(id=uuid.uuid4().hex[:8], command=command, proc=proc)
        self.jobs[job.id] = job
        asyncio.ensure_future(self._pump(job))
        return job


class ExecArgs(BaseModel):
    command: str = Field(description="The shell command to run", min_length=1, max_length=4000)
    cwd: str = Field(default="", description="Working directory (must be inside allowed folders)")
    shell: str = Field(default="", description="powershell | cmd | bash (default from config)")
    timeout_seconds: int = Field(default=0, ge=0, le=1800, description="0 = default")
    background: bool = Field(default=False, description="Start and return immediately; poll with get_command_output")


class ExecuteCommandTool(Tool):
    name = "execute_command"
    description = ("Run a shell command (PowerShell by default) and return its output. Safe commands (git status, dir, docker ps...) "
                   "run directly; installing/pushing/running programs asks for confirmation; destructive commands are blocked or "
                   "need explicit confirmation. Use background=true for servers/long jobs.")
    category = "terminal"
    risk_level = RiskLevel.SENSITIVE
    args_model = ExecArgs

    def classify(self, args: Dict[str, Any]) -> RiskLevel:
        risk, _ = classify_command(validate_command(args["command"]))
        return risk

    def describe(self, args: Dict[str, Any]) -> str:
        risk, reason = classify_command(args.get("command", ""))
        return f"Run command [{risk.value}]: {args.get('command', '')[:200]}"

    def confirmation_details(self, args: Dict[str, Any]) -> Dict[str, Any]:
        risk, reason = classify_command(args.get("command", ""))
        return {"command": args.get("command"), "cwd": args.get("cwd") or "(default)", "risk": risk.value, "reason": reason}

    async def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        command = validate_command(args["command"])
        settings = ctx.settings
        cwd = None
        if args.get("cwd"):
            cwd = str(resolve_path(args["cwd"], settings.allowed_roots, must_exist=True))
        elif settings.terminal_cwd:
            cwd = settings.terminal_cwd
        shell = args.get("shell") or settings.terminal_shell
        argv = _shell_argv(shell, command)
        timeout = args.get("timeout_seconds") or settings.terminal_timeout_seconds
        svc: TerminalService = ctx.service("terminal")
        ctx.activity(f"Running: {command[:80]}")

        if args.get("background"):
            try:
                job = await svc.start(argv, cwd, command)
            except OSError as e:
                return ToolResult.fail(f"Could not start command: {e}")
            await asyncio.sleep(1.0)
            return ToolResult.ok({"job_id": job.id, "pid": job.proc.pid, "initial_output": "".join(job.output)[-2000:],
                                  "note": "Running in background. Use get_command_output with job_id."},
                                 summary=f"Started background job {job.id}")
        try:
            proc = await asyncio.create_subprocess_exec(*argv, cwd=cwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                                                        stdin=asyncio.subprocess.DEVNULL)
        except OSError as e:
            return ToolResult.fail(f"Could not start shell '{shell}': {e}")
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            return ToolResult.fail(f"Command timed out after {timeout}s and was killed: {command[:100]}")
        except asyncio.CancelledError:
            proc.kill()
            raise
        stdout = out.decode("utf-8", errors="replace")
        stderr = err.decode("utf-8", errors="replace")
        truncated = len(stdout) > MAX_OUTPUT
        payload = {"command": command, "cwd": cwd, "returncode": proc.returncode, "stdout": stdout[-MAX_OUTPUT:],
                   "stderr": stderr[-4000:], "truncated": truncated}
        ok = proc.returncode == 0
        summary = f"Exit {proc.returncode}: {command[:60]}"
        return ToolResult(success=ok, output=payload, error=None if ok else f"Command exited with code {proc.returncode}",
                          summary=summary, untrusted=True, source="terminal")


class JobArgs(BaseModel):
    job_id: str


class GetCommandOutputTool(Tool):
    name = "get_command_output"
    description = "Get the current output/status of a background command started with execute_command(background=true)."
    category = "terminal"
    risk_level = RiskLevel.READ_ONLY
    args_model = JobArgs

    async def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        svc: TerminalService = ctx.service("terminal")
        job = svc.jobs.get(args["job_id"])
        if job is None:
            return ToolResult.fail(f"No background job '{args['job_id']}'")
        return ToolResult.ok({"job_id": job.id, "command": job.command, "done": job.done, "returncode": job.returncode,
                              "output": "".join(job.output)[-MAX_OUTPUT:]}, summary=f"Job {job.id} {'done' if job.done else 'running'}",
                             untrusted=True, source="terminal")


class ListProcessesArgs(BaseModel):
    filter: str = Field(default="", description="Substring to match on the process name")
    limit: int = Field(default=50, ge=1, le=500)


class ListProcessesTool(Tool):
    name = "list_processes"
    description = "List running processes (pid, name, cpu, memory, command line), optionally filtered by name."
    category = "terminal"
    risk_level = RiskLevel.READ_ONLY
    args_model = ListProcessesArgs

    async def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        f = args.get("filter", "").lower()
        rows = []
        for p in psutil.process_iter(["pid", "name", "memory_info", "cmdline", "status"]):
            try:
                name = p.info["name"] or ""
                if f and f not in name.lower() and f not in " ".join(p.info["cmdline"] or []).lower():
                    continue
                rows.append({"pid": p.info["pid"], "name": name, "status": p.info["status"],
                             "memory_mb": round((p.info["memory_info"].rss if p.info["memory_info"] else 0) / 1048576, 1),
                             "cmdline": " ".join(p.info["cmdline"] or [])[:200]})
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        rows.sort(key=lambda r: -r["memory_mb"])
        return ToolResult.ok({"count": len(rows), "processes": rows[: args.get("limit", 50)]}, summary=f"{len(rows)} processes")


class KillArgs(BaseModel):
    pid: int = Field(default=0, ge=0)
    name: str = Field(default="", description="Process name (kills all matches)")
    job_id: str = Field(default="", description="Background job id from execute_command")


class KillProcessTool(Tool):
    name = "kill_process"
    description = "Terminate a process by PID, by name, or a background job by id. Requires confirmation."
    category = "terminal"
    risk_level = RiskLevel.DANGEROUS
    args_model = KillArgs

    def describe(self, args: Dict[str, Any]) -> str:
        target = args.get("job_id") or args.get("name") or f"PID {args.get('pid')}"
        return f"Kill process {target}"

    async def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        svc: TerminalService = ctx.service("terminal")
        if args.get("job_id"):
            job = svc.jobs.get(args["job_id"])
            if job is None:
                return ToolResult.fail("No such background job")
            try:
                job.proc.kill()
            except ProcessLookupError:
                pass
            return ToolResult.ok({"killed_job": job.id}, summary=f"Killed job {job.id}")
        targets = []
        if args.get("pid"):
            if psutil.pid_exists(args["pid"]):
                targets.append(psutil.Process(args["pid"]))
        elif args.get("name"):
            key = args["name"].lower()
            targets = [p for p in psutil.process_iter(["name"]) if key in (p.info["name"] or "").lower()]
        if not targets:
            return ToolResult.fail("No matching process found.")
        killed, errors = [], []
        for p in targets:
            try:
                if (p.name() or "").lower() in PROTECTED or p.pid == os.getpid():
                    errors.append(f"{p.name()} is protected")
                    continue
                p.terminate()
                killed.append({"pid": p.pid, "name": p.name()})
            except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
                errors.append(f"{p.pid}: {e.__class__.__name__}")
        psutil.wait_procs(targets, timeout=3)
        return ToolResult(success=bool(killed), output={"killed": killed, "errors": errors},
                          error=None if killed else "; ".join(errors), summary=f"Killed {len(killed)} process(es)")


def _build(settings) -> List[Tool]:
    return [ExecuteCommandTool(), GetCommandOutputTool(), ListProcessesTool(), KillProcessTool()]


PLUGIN = Plugin(
    name="terminal",
    description="Controlled shell execution with command risk classification",
    permissions=["terminal"],
    tools=_build,
    configuration={"TERMINAL_SHELL": "powershell|cmd|bash", "TERMINAL_TIMEOUT_SECONDS": "default timeout"},
)
