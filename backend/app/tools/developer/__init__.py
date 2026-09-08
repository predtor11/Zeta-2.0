"""Developer tools: git, GitHub, Docker and project diagnostics.

These wrap the terminal machinery with structured, read-mostly helpers so the
model can inspect a repository without composing raw commands.  Anything that
mutates state (commit, push, docker restart) routes through `execute_command`
so it is classified and confirmed like any other command.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
from pydantic import BaseModel, Field

from app.plugins.loader import Plugin
from app.security.permissions import RiskLevel
from app.security.validators import resolve_path
from app.tools.base import Tool, ToolContext, ToolResult


async def _run(argv: List[str], cwd: Optional[str] = None, timeout: int = 60) -> Dict[str, Any]:
    try:
        proc = await asyncio.create_subprocess_exec(*argv, cwd=cwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                                                    stdin=asyncio.subprocess.DEVNULL)
    except FileNotFoundError:
        return {"returncode": 127, "stdout": "", "stderr": f"'{argv[0]}' is not installed or not on PATH"}
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return {"returncode": -1, "stdout": "", "stderr": "timed out"}
    return {"returncode": proc.returncode, "stdout": out.decode("utf-8", errors="replace")[-15000:],
            "stderr": err.decode("utf-8", errors="replace")[-4000:]}


def _repo(ctx: ToolContext, raw: str) -> str:
    p = resolve_path(raw or ctx.settings.terminal_cwd or str(Path.cwd()), ctx.settings.allowed_roots, must_exist=True)
    return str(p)


class RepoArgs(BaseModel):
    path: str = Field(default="", description="Repository folder (default: configured working directory)")


class GitStatusTool(Tool):
    name = "git_status"
    description = "Show the current branch, uncommitted changes, and recent commits of a git repository."
    category = "developer"
    risk_level = RiskLevel.READ_ONLY
    args_model = RepoArgs

    async def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        cwd = _repo(ctx, args.get("path", ""))
        inside = await _run(["git", "rev-parse", "--is-inside-work-tree"], cwd)
        if inside["returncode"] != 0:
            return ToolResult.fail(f"'{cwd}' is not a git repository ({inside['stderr'].strip()[:200]})")
        branch = await _run(["git", "symbolic-ref", "--short", "-q", "HEAD"], cwd)
        if branch["returncode"] != 0:
            branch = await _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd)
            if branch["returncode"] != 0:
                branch = {"stdout": "(detached / no commits)"}
        status = await _run(["git", "status", "--porcelain=v1", "--branch"], cwd)
        log = await _run(["git", "log", "--oneline", "-n", "10"], cwd)
        remote = await _run(["git", "remote", "-v"], cwd)
        lines = status["stdout"].splitlines()
        changes = [l for l in lines[1:] if l.strip()]
        return ToolResult.ok({"path": cwd, "branch": branch["stdout"].strip(), "branch_line": lines[0] if lines else "",
                              "changed_files": changes, "change_count": len(changes), "recent_commits": log["stdout"].splitlines(),
                              "remotes": remote["stdout"].splitlines()}, summary=f"{branch['stdout'].strip()}: {len(changes)} changes",
                             untrusted=True, source="git")


class GitDiffArgs(BaseModel):
    path: str = ""
    staged: bool = False
    file: str = Field(default="", description="Limit to one file")
    max_chars: int = Field(default=12000, ge=500, le=60000)


class GitDiffTool(Tool):
    name = "git_diff"
    description = "Show uncommitted changes (diff) in a repository, optionally staged only or for a single file."
    category = "developer"
    risk_level = RiskLevel.READ_ONLY
    args_model = GitDiffArgs

    async def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        cwd = _repo(ctx, args.get("path", ""))
        argv = ["git", "diff", "--stat"] + (["--cached"] if args.get("staged") else [])
        stat = await _run(argv, cwd)
        argv = ["git", "diff"] + (["--cached"] if args.get("staged") else []) + (["--", args["file"]] if args.get("file") else [])
        diff = await _run(argv, cwd)
        if diff["returncode"] != 0:
            return ToolResult.fail(diff["stderr"].strip() or "git diff failed")
        text = diff["stdout"][: args.get("max_chars", 12000)]
        return ToolResult.ok({"stat": stat["stdout"], "diff": text, "truncated": len(diff["stdout"]) > len(text)},
                             summary="Diff collected", untrusted=True, source="git")


class GitLogArgs(BaseModel):
    path: str = ""
    count: int = Field(default=20, ge=1, le=200)
    author: str = ""
    since: str = Field(default="", description="e.g. '2 weeks ago'")


class GitLogTool(Tool):
    name = "git_log"
    description = "Show commit history with author and date."
    category = "developer"
    risk_level = RiskLevel.READ_ONLY
    args_model = GitLogArgs

    async def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        cwd = _repo(ctx, args.get("path", ""))
        argv = ["git", "log", f"-n{args.get('count', 20)}", "--date=short", "--pretty=format:%h|%an|%ad|%s"]
        if args.get("author"):
            argv.append(f"--author={args['author']}")
        if args.get("since"):
            argv.append(f"--since={args['since']}")
        r = await _run(argv, cwd)
        if r["returncode"] != 0:
            return ToolResult.fail(r["stderr"].strip() or "git log failed")
        commits = [dict(zip(("hash", "author", "date", "message"), l.split("|", 3))) for l in r["stdout"].splitlines() if l]
        return ToolResult.ok({"commits": commits}, summary=f"{len(commits)} commits", untrusted=True, source="git")


class DockerStatusTool(Tool):
    name = "docker_status"
    description = "Check whether Docker is running and list containers (running and stopped) with status and ports."
    category = "developer"
    risk_level = RiskLevel.READ_ONLY
    args_model = RepoArgs

    async def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        if not shutil.which("docker"):
            return ToolResult.fail("Docker CLI is not installed or not on PATH.")
        ver = await _run(["docker", "version", "--format", "{{.Server.Version}}"], timeout=20)
        if ver["returncode"] != 0:
            return ToolResult.fail(f"Docker daemon is not reachable: {ver['stderr'].strip()[:300]} (is Docker Desktop running?)")
        ps = await _run(["docker", "ps", "-a", "--format", "{{json .}}"], timeout=30)
        containers = []
        for line in ps["stdout"].splitlines():
            try:
                c = json.loads(line)
                containers.append({"name": c.get("Names"), "image": c.get("Image"), "status": c.get("Status"), "state": c.get("State"), "ports": c.get("Ports")})
            except json.JSONDecodeError:
                continue
        compose = None
        cwd = args.get("path")
        if cwd:
            cwd = _repo(ctx, cwd)
            if any((Path(cwd) / f).exists() for f in ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml")):
                compose = (await _run(["docker", "compose", "ps", "--format", "json"], cwd, timeout=30))["stdout"][:5000]
        return ToolResult.ok({"server_version": ver["stdout"].strip(), "containers": containers, "compose_ps": compose},
                             summary=f"Docker OK, {len(containers)} containers", untrusted=True, source="docker")


class DockerLogsArgs(BaseModel):
    container: str
    tail: int = Field(default=200, ge=10, le=5000)


class DockerLogsTool(Tool):
    name = "docker_logs"
    description = "Read the last lines of a container's logs."
    category = "developer"
    risk_level = RiskLevel.READ_ONLY
    args_model = DockerLogsArgs

    async def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        r = await _run(["docker", "logs", "--tail", str(args.get("tail", 200)), args["container"]], timeout=30)
        if r["returncode"] != 0:
            return ToolResult.fail(r["stderr"].strip()[:500] or "docker logs failed")
        return ToolResult.ok({"logs": (r["stdout"] + r["stderr"])[-15000:]}, summary="Logs collected", untrusted=True, source="docker")


class ProjectInfoTool(Tool):
    name = "inspect_project"
    description = ("Inspect a project folder: detect language/framework (Python, Node, Docker...), entry points, "
                   "package manifests, tests, env files, and running processes/ports that look related. Useful for 'why isn't my backend running?'.")
    category = "developer"
    risk_level = RiskLevel.READ_ONLY
    args_model = RepoArgs

    async def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        cwd = Path(_repo(ctx, args.get("path", "")))
        markers = {
            "python": ["requirements.txt", "pyproject.toml", "setup.py", "Pipfile", "manage.py", "app/main.py", "main.py"],
            "node": ["package.json", "pnpm-lock.yaml", "yarn.lock", "package-lock.json"],
            "docker": ["Dockerfile", "docker-compose.yml", "docker-compose.yaml", "compose.yml"],
            "dotnet": ["*.csproj", "*.sln"], "java": ["pom.xml", "build.gradle"], "go": ["go.mod"], "rust": ["Cargo.toml"],
        }
        found: Dict[str, List[str]] = {}
        for lang, files in markers.items():
            hits = []
            for f in files:
                if "*" in f:
                    hits += [p.name for p in cwd.glob(f)]
                elif (cwd / f).exists():
                    hits.append(f)
            if hits:
                found[lang] = hits
        scripts = {}
        pkg = cwd / "package.json"
        if pkg.exists():
            try:
                scripts = json.loads(pkg.read_text(encoding="utf-8")).get("scripts", {})
            except Exception:  # noqa: BLE001
                pass
        env_files = [p.name for p in cwd.glob(".env*")]
        venv = any((cwd / v).exists() for v in (".venv", "venv"))
        tests = [p.name for p in cwd.iterdir() if p.is_dir() and p.name.lower() in ("tests", "test", "__tests__", "spec")]
        top = sorted([p.name + ("/" if p.is_dir() else "") for p in cwd.iterdir() if not p.name.startswith(".")])[:60]
        # listening ports
        import psutil

        ports = []
        try:
            for c in psutil.net_connections(kind="inet"):
                if c.status == psutil.CONN_LISTEN and c.laddr:
                    try:
                        pname = psutil.Process(c.pid).name() if c.pid else ""
                    except Exception:  # noqa: BLE001
                        pname = ""
                    ports.append({"port": c.laddr.port, "pid": c.pid, "process": pname})
        except (psutil.AccessDenied, RuntimeError):
            pass
        ports = sorted({(p["port"], p["pid"], p["process"]) for p in ports})
        return ToolResult.ok({"path": str(cwd), "detected": found, "npm_scripts": scripts, "env_files": env_files, "has_venv": venv,
                              "test_dirs": tests, "top_level": top,
                              "listening_ports": [{"port": p, "pid": pid, "process": n} for p, pid, n in ports[:60]]},
                             summary=f"Project types: {', '.join(found) or 'unknown'}")


class GitHubArgs(BaseModel):
    repo: str = Field(description="owner/name")
    what: str = Field(default="pulls", description="pulls | issues | repo | commits | actions")
    limit: int = Field(default=10, ge=1, le=50)


class GitHubTool(Tool):
    name = "github_query"
    description = "Query GitHub (public or with GITHUB_TOKEN): repository info, open pull requests, issues, recent commits, workflow runs."
    category = "developer"
    risk_level = RiskLevel.READ_ONLY
    args_model = GitHubArgs

    async def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        token = ctx.secrets.get("github_token")
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "zeta-agent"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        repo = args["repo"].strip("/")
        what = args.get("what", "pulls")
        path = {"pulls": f"/repos/{repo}/pulls", "issues": f"/repos/{repo}/issues", "repo": f"/repos/{repo}",
                "commits": f"/repos/{repo}/commits", "actions": f"/repos/{repo}/actions/runs"}.get(what)
        if not path:
            return ToolResult.fail("what must be one of pulls|issues|repo|commits|actions")
        async with httpx.AsyncClient(base_url="https://api.github.com", headers=headers, timeout=30) as c:
            r = await c.get(path, params={"per_page": args.get("limit", 10)})
        if r.status_code == 404:
            return ToolResult.fail(f"Repository '{repo}' not found (or private without a token).")
        if r.status_code >= 400:
            return ToolResult.fail(f"GitHub API error {r.status_code}: {r.text[:200]}")
        data = r.json()
        if what == "repo":
            out = {k: data.get(k) for k in ("full_name", "description", "default_branch", "stargazers_count", "forks_count", "open_issues_count", "pushed_at", "html_url")}
        elif what == "commits":
            out = [{"sha": d["sha"][:7], "message": d["commit"]["message"].split("\n")[0], "author": d["commit"]["author"]["name"], "date": d["commit"]["author"]["date"]} for d in data]
        elif what == "actions":
            out = [{"name": d.get("name"), "status": d.get("status"), "conclusion": d.get("conclusion"), "branch": d.get("head_branch"), "url": d.get("html_url")} for d in data.get("workflow_runs", [])]
        else:
            out = [{"number": d["number"], "title": d["title"], "state": d["state"], "user": d["user"]["login"], "updated_at": d["updated_at"], "url": d["html_url"]} for d in data]
        return ToolResult.ok(out, summary=f"GitHub {what} for {repo}", untrusted=True, source="github")


def _build(settings) -> List[Tool]:
    return [GitStatusTool(), GitDiffTool(), GitLogTool(), DockerStatusTool(), DockerLogsTool(), ProjectInfoTool(), GitHubTool()]


PLUGIN = Plugin(
    name="developer",
    description="Git, GitHub, Docker and project diagnostics",
    permissions=["developer"],
    tools=_build,
    configuration={"GITHUB_TOKEN": "optional token for private repositories"},
)
