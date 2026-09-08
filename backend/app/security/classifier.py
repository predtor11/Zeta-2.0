"""Command risk classifier for the terminal tool.

Classification is deliberately conservative: unknown commands are SENSITIVE,
known-destructive patterns are DANGEROUS, and a few things are BLOCKED outright.
Shell chaining (`;`, `&&`, `|`, backticks, `$(`) is evaluated per segment and the
overall command takes the highest risk of any segment.
"""

from __future__ import annotations

import re
import shlex
from typing import List, Tuple

from app.security.permissions import RiskLevel

SAFE_COMMANDS = {
    # inspection
    "git status", "git log", "git diff", "git branch", "git show", "git remote", "git stash list", "git fetch",
    "git rev-parse", "git config --get", "git tag", "git describe", "git blame", "git ls-files",
    "python --version", "python -v", "python3 --version", "pip list", "pip show", "pip --version", "pip freeze",
    "node --version", "node -v", "npm --version", "npm -v", "npm ls", "npm list", "npm outdated", "npm run test",
    "npm test", "pytest", "python -m pytest", "docker ps", "docker images", "docker logs", "docker version",
    "docker info", "docker compose ps", "docker compose logs", "docker-compose ps", "docker-compose logs",
    "docker inspect", "docker stats", "dir", "ls", "pwd", "cd", "echo", "cat", "type", "head", "tail", "find",
    "findstr", "grep", "where", "which", "whoami", "hostname", "date", "time", "ver", "uname", "systeminfo",
    "ipconfig", "ifconfig", "ping", "tracert", "traceroute", "nslookup", "netstat", "tasklist", "get-process",
    "get-service", "get-childitem", "get-content", "get-location", "get-date", "get-computerinfo", "get-item",
    "get-command", "select-string", "test-path", "resolve-path", "measure-object", "get-psdrive", "get-nettcpconnection",
    "get-netipaddress", "test-netconnection", "tree", "wc", "sort", "uniq", "diff", "fc", "env", "printenv",
    "set", "ollama list", "ollama ps", "code --version", "gh --version", "gh pr list", "gh issue list", "gh repo view",
    "gh pr view", "gh run list", "gh auth status", "cargo --version", "go version", "java -version", "dotnet --version",
    "ssh -v", "curl --version", "wget --version", "df", "du", "free", "top", "ps", "nvidia-smi", "wmic",
    "git worktree list", "git reflog", "python -c", "conda list", "conda env list", "poetry show",
}

SENSITIVE_PATTERNS = [
    r"^npm (install|i|ci|update|uninstall|remove|run|start|build)\b",
    r"^pip3? (install|uninstall|download)\b", r"^python -m pip (install|uninstall)\b",
    r"^poetry (add|install|remove|update)\b", r"^conda (install|create|remove)\b",
    r"^git (push|pull|merge|rebase|checkout|switch|commit|add|stash|cherry-pick|revert|restore|reset)\b",
    r"^gh (pr|issue) (create|merge|close|comment|edit)\b", r"^gh repo (create|fork|clone)\b",
    r"^docker (run|start|stop|restart|build|pull|push|exec|rm|rmi|compose)\b", r"^docker-compose\b",
    r"^(python|python3|node|deno|bun|ruby|php|java|dotnet|cargo|go|make|gradle|mvn|uvicorn|flask|django-admin)\b",
    r"^(code|notepad|explorer|start|open)\b", r"^ollama (pull|run|serve|create|rm)\b",
    r"^(ssh|scp|rsync|sftp)\b", r"^(curl|wget|invoke-webrequest|iwr|invoke-restmethod|irm)\b",
    r"^(mkdir|md|new-item|copy|cp|copy-item|move|mv|move-item|rename|ren|rename-item|touch)\b",
    r"^(set-content|add-content|out-file|tee)\b", r"^(setx|set-itemproperty)\b",
    r"^(taskkill|stop-process|kill|pkill)\b", r"^(net start|net stop|sc start|sc stop|start-service|stop-service|restart-service)\b",
    r"^(alembic|prisma|sequelize|knex|manage\.py)\b", r"^(terraform plan|aws|az|gcloud|kubectl get|kubectl describe|kubectl logs)\b",
    r"^(winget|choco|scoop|brew|apt|apt-get|yum|dnf)\b",
]

DANGEROUS_PATTERNS = [
    r"\brm\s+(-[a-z]*r[a-z]*f|-[a-z]*f[a-z]*r|-rf|-fr)\b", r"\brm\s+-r\b", r"\brmdir\s+/s\b", r"\brd\s+/s\b",
    r"\bremove-item\b.*(-recurse|-force)", r"\bdel\s+(/[a-z]\s+)*.*(\*|/s|/q)", r"\berase\b",
    r"\bformat(\.com)?\s+[a-z]:", r"\bdiskpart\b", r"\bmkfs\b", r"\bdd\s+if=", r"\bshutdown\b", r"\bstop-computer\b",
    r"\brestart-computer\b", r"\breboot\b", r"\bpoweroff\b", r"\bhalt\b", r"\bbcdedit\b", r"\bbootrec\b",
    r"\bdrop\s+(database|table|schema)\b", r"\btruncate\s+table\b", r"\bdelete\s+from\b", r"\bdelete\s+database\b",
    r"\bgit\s+push\b.*(--force|-f\b)", r"\bgit\s+reset\s+--hard\b", r"\bgit\s+clean\s+-[a-z]*f", r"\bgit\s+branch\s+-D\b",
    r"\bdocker\s+(system|volume|image|container)\s+prune\b", r"\bdocker\s+rm\s+-f\b", r"\bdocker\s+volume\s+rm\b",
    r"\bkubectl\s+(delete|drain)\b", r"\bterraform\s+(destroy|apply)\b", r"\baws\s+.*\b(delete|terminate|rm)\b",
    r"\breg\s+(delete|add)\b", r"\bremove-itemproperty\b", r"\bnet\s+user\b.*(/add|/delete)", r"\bicacls\b", r"\btakeown\b",
    r"\bchmod\s+(-r\s+)?777\b", r"\bchown\s+-r\b", r"\bsudo\b", r"\brunas\b", r"\bschtasks\b.*/(create|delete)",
    r"\bcipher\s+/w\b", r"\bsdelete\b", r"\bwmic\b.*\bdelete\b", r"\bvssadmin\b", r"\bwevtutil\s+cl\b",
    r"\bset-executionpolicy\b", r"\bdisable-", r"\bnetsh\b.*\b(firewall|advfirewall)\b.*\b(set|add|delete)\b",
    r">\s*[a-z]:\\windows", r"\bmove\b.*\\windows\\", r"\bfsutil\b", r"\bchkdsk\s+/f\b",
]

BLOCKED_PATTERNS = [
    r"\brm\s+-rf\s+(/|\*|~|\$home|c:\\?)\s*$", r"\brm\s+-rf\s+/\s", r"\bformat\s+c:", r"\bdel\s+/[sq]\s+c:\\\s*$",
    r"\bremove-item\b.*\b(c:\\|\$env:systemroot|\$env:windir)", r":\(\)\s*\{\s*:\|:&\s*\};:", r"\bcipher\s+/w:c\b",
    r"\bdiskpart\b.*\bclean\b", r"\bbcdedit\s+/deletevalue\b", r"\bdd\s+if=.*of=\\\\\.\\physicaldrive",
    r"\bmkfs\b.*\bsd[a-z]\b", r"\bnet\s+user\s+administrator\b", r"\bvssadmin\s+delete\s+shadows\b",
    r"\bwevtutil\s+cl\b", r"\breg\s+delete\s+hklm\\(software|system)\b", r"\btakeown\s+/f\s+c:\\",
    r"invoke-expression.*(downloadstring|iwr|irm)", r"\biex\s*\(.*(iwr|irm|downloadstring)", r"\bcurl\b.*\|\s*(sh|bash|powershell|iex)\b",
]

_SPLIT_RE = re.compile(r"\s*(?:&&|\|\||;|\||\n)\s*")


def _segments(command: str) -> List[str]:
    parts = [p.strip() for p in _SPLIT_RE.split(command) if p.strip()]
    # Also surface subshell contents: $(...) and `...`
    extra = re.findall(r"\$\(([^)]*)\)|`([^`]*)`", command)
    for a, b in extra:
        seg = (a or b).strip()
        if seg:
            parts.append(seg)
    return parts or [command.strip()]


def _base(segment: str) -> str:
    seg = segment.strip().lstrip("&").strip()
    try:
        tokens = shlex.split(seg, posix=False)
    except ValueError:
        tokens = seg.split()
    if not tokens:
        return ""
    return " ".join(t.strip("\"'").lower() for t in tokens)


def classify_segment(segment: str) -> Tuple[RiskLevel, str]:
    low = segment.strip().lower()
    if not low:
        return RiskLevel.SAFE, "empty"
    for pat in BLOCKED_PATTERNS:
        if re.search(pat, low):
            return RiskLevel.BLOCKED, f"matches blocked pattern: {pat}"
    for pat in DANGEROUS_PATTERNS:
        if re.search(pat, low):
            return RiskLevel.DANGEROUS, f"matches dangerous pattern: {pat}"
    base = _base(segment)
    # Redirections write files -> sensitive
    if re.search(r"(^|\s)>{1,2}\s*\S", low):
        return RiskLevel.SENSITIVE, "writes to a file via redirection"
    # Exact / prefix matches for safe commands
    for safe in sorted(SAFE_COMMANDS, key=len, reverse=True):
        if base == safe or base.startswith(safe + " "):
            # Safe commands with dangerous flags
            if re.search(r"\s(-d|--delete|/f|-f)\b", base) and safe.split()[0] in ("git", "docker", "npm"):
                return RiskLevel.SENSITIVE, f"'{safe}' with a modifying flag"
            return RiskLevel.SAFE, f"known safe command '{safe}'"
    for pat in SENSITIVE_PATTERNS:
        if re.search(pat, base):
            return RiskLevel.SENSITIVE, f"matches sensitive pattern: {pat}"
    return RiskLevel.SENSITIVE, "unknown command (default: sensitive)"


def classify_command(command: str) -> Tuple[RiskLevel, str]:
    """Return the highest risk across all shell segments and the reason."""
    worst = RiskLevel.READ_ONLY
    reason = "no command"
    low = command.lower()
    for pat in BLOCKED_PATTERNS:
        if re.search(pat, low):
            return RiskLevel.BLOCKED, f"matches blocked pattern: {pat}"
    for pat in DANGEROUS_PATTERNS:
        if re.search(pat, low):
            worst, reason = RiskLevel.DANGEROUS, f"matches dangerous pattern: {pat}"
    for seg in _segments(command):
        risk, why = classify_segment(seg)
        if risk.rank > worst.rank:
            worst, reason = risk, why
    if worst == RiskLevel.READ_ONLY:
        worst = RiskLevel.SAFE
    return worst, reason
