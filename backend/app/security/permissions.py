"""PermissionManager - every tool invocation passes through here.

    User Request -> Agent -> Tool Selection -> PermissionManager
        -> Risk Classification -> ALLOW / CONFIRM / DENY -> Execute -> Audit

Policy comes from `backend/config/permissions.yaml` (overridable by
`permissions.local.yaml`, which the setup wizard writes).  Each tool category
maps risk levels to a decision:

    filesystem:
      READ_ONLY: allow
      SAFE: allow
      SENSITIVE: allow
      DANGEROUS: confirm
      BLOCKED: deny
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

log = logging.getLogger(__name__)


class RiskLevel(str, Enum):
    READ_ONLY = "READ_ONLY"
    SAFE = "SAFE"
    SENSITIVE = "SENSITIVE"
    DANGEROUS = "DANGEROUS"
    BLOCKED = "BLOCKED"

    @property
    def rank(self) -> int:
        return _RANK[self]


_RANK = {RiskLevel.READ_ONLY: 0, RiskLevel.SAFE: 1, RiskLevel.SENSITIVE: 2, RiskLevel.DANGEROUS: 3, RiskLevel.BLOCKED: 4}


class Decision(str, Enum):
    ALLOW = "allow"
    CONFIRM = "confirm"
    DENY = "deny"


@dataclass
class PermissionResult:
    decision: Decision
    risk: RiskLevel
    reason: str
    category: str


# Conservative defaults. Anything not listed inherits from "default".
DEFAULT_POLICY: Dict[str, Dict[str, str]] = {
    "default": {"READ_ONLY": "allow", "SAFE": "allow", "SENSITIVE": "confirm", "DANGEROUS": "confirm", "BLOCKED": "deny"},
    "filesystem": {"READ_ONLY": "allow", "SAFE": "allow", "SENSITIVE": "allow", "DANGEROUS": "confirm", "BLOCKED": "deny"},
    "computer": {"READ_ONLY": "allow", "SAFE": "allow", "SENSITIVE": "allow", "DANGEROUS": "confirm", "BLOCKED": "deny"},
    "terminal": {"READ_ONLY": "allow", "SAFE": "allow", "SENSITIVE": "confirm", "DANGEROUS": "deny", "BLOCKED": "deny"},
    "browser": {"READ_ONLY": "allow", "SAFE": "allow", "SENSITIVE": "confirm", "DANGEROUS": "confirm", "BLOCKED": "deny"},
    "messaging": {"READ_ONLY": "allow", "SAFE": "allow", "SENSITIVE": "confirm", "DANGEROUS": "confirm", "BLOCKED": "deny"},
    "email": {"READ_ONLY": "allow", "SAFE": "allow", "SENSITIVE": "confirm", "DANGEROUS": "confirm", "BLOCKED": "deny"},
    "developer": {"READ_ONLY": "allow", "SAFE": "allow", "SENSITIVE": "confirm", "DANGEROUS": "deny", "BLOCKED": "deny"},
    "memory": {"READ_ONLY": "allow", "SAFE": "allow", "SENSITIVE": "allow", "DANGEROUS": "confirm", "BLOCKED": "deny"},
    "screen": {"READ_ONLY": "allow", "SAFE": "confirm", "SENSITIVE": "confirm", "DANGEROUS": "confirm", "BLOCKED": "deny"},
    "scheduler": {"READ_ONLY": "allow", "SAFE": "allow", "SENSITIVE": "allow", "DANGEROUS": "confirm", "BLOCKED": "deny"},
    "agent": {"READ_ONLY": "allow", "SAFE": "allow", "SENSITIVE": "allow", "DANGEROUS": "confirm", "BLOCKED": "deny"},
}

# Spec-style aliases (filesystem: {read: allowed, write: allowed, delete: confirmation})
_ALIAS_MAP = {
    "read": "READ_ONLY", "read_only": "READ_ONLY", "readonly": "READ_ONLY",
    "safe": "SAFE", "safe_commands": "SAFE", "write": "SENSITIVE",
    "sensitive": "SENSITIVE", "sensitive_commands": "SENSITIVE", "send": "SENSITIVE",
    "delete": "DANGEROUS", "dangerous": "DANGEROUS", "dangerous_commands": "DANGEROUS",
    "blocked": "BLOCKED",
}
_DECISION_ALIAS = {"allowed": "allow", "allow": "allow", "confirmation": "confirm", "confirm": "confirm",
                   "ask": "confirm", "deny": "deny", "denied": "deny", "blocked": "deny", "block": "deny"}


def _normalise(policy: Dict[str, Any]) -> Dict[str, Dict[str, str]]:
    out: Dict[str, Dict[str, str]] = {}
    for category, levels in (policy or {}).items():
        if not isinstance(levels, dict):
            continue
        cat: Dict[str, str] = {}
        for k, v in levels.items():
            level = _ALIAS_MAP.get(str(k).lower(), str(k).upper())
            if level not in RiskLevel.__members__:
                log.warning("Unknown risk level '%s' in permissions for %s", k, category)
                continue
            decision = _DECISION_ALIAS.get(str(v).lower())
            if decision is None:
                log.warning("Unknown decision '%s' in permissions for %s.%s", v, category, k)
                continue
            cat[level] = decision
        out[str(category).lower()] = cat
    return out


class PermissionManager:
    def __init__(self, policy: Optional[Dict[str, Any]] = None, disabled_categories: Optional[set] = None,
                 disabled_tools: Optional[set] = None):
        self.policy: Dict[str, Dict[str, str]] = {k: dict(v) for k, v in DEFAULT_POLICY.items()}
        self.disabled_categories: set = set(disabled_categories or ())
        self.disabled_tools: set = set(disabled_tools or ())
        self._session_allow: set = set()  # "remember this decision" cache (tool+args hash)
        if policy:
            self.apply(policy)

    # ---- policy loading ------------------------------------------------
    def apply(self, policy: Dict[str, Any]) -> None:
        norm = _normalise(policy.get("permissions", policy) if isinstance(policy, dict) else {})
        for cat, levels in norm.items():
            self.policy.setdefault(cat, dict(self.policy["default"])).update(levels)
        enabled = policy.get("enabled") if isinstance(policy, dict) else None
        if isinstance(enabled, dict):
            for cat, flag in enabled.items():
                if flag is False:
                    self.disabled_categories.add(str(cat).lower())
                else:
                    self.disabled_categories.discard(str(cat).lower())
        disabled_tools = policy.get("disabled_tools") if isinstance(policy, dict) else None
        if isinstance(disabled_tools, list):
            self.disabled_tools.update(str(t) for t in disabled_tools)

    @classmethod
    def from_files(cls, *paths: Path) -> "PermissionManager":
        pm = cls()
        for p in paths:
            if p and p.exists():
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        pm.apply(yaml.safe_load(f) or {})
                    log.info("Loaded permissions from %s", p)
                except Exception as e:  # noqa: BLE001
                    log.error("Failed to load permissions from %s: %s", p, e)
        return pm

    def to_dict(self) -> Dict[str, Any]:
        return {
            "permissions": self.policy,
            "enabled": {cat: (cat not in self.disabled_categories) for cat in self.policy if cat != "default"},
            "disabled_tools": sorted(self.disabled_tools),
        }

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            yaml.safe_dump(self.to_dict(), f, sort_keys=True)

    # ---- decisions -------------------------------------------------------
    def decision_for(self, category: str, risk: RiskLevel) -> Decision:
        cat = self.policy.get(category.lower(), self.policy["default"])
        raw = cat.get(risk.value) or self.policy["default"].get(risk.value, "confirm")
        return Decision(raw)

    def check(self, tool_name: str, category: str, risk: RiskLevel, *, requires_confirmation: bool = False,
              action_key: Optional[str] = None) -> PermissionResult:
        category = category.lower()
        if tool_name in self.disabled_tools:
            return PermissionResult(Decision.DENY, risk, f"Tool '{tool_name}' is disabled by configuration", category)
        if category in self.disabled_categories:
            return PermissionResult(Decision.DENY, risk, f"'{category}' capability is disabled in permissions", category)
        if risk == RiskLevel.BLOCKED:
            return PermissionResult(Decision.DENY, risk, "This action is blocked", category)
        decision = self.decision_for(category, risk)
        if decision == Decision.ALLOW and requires_confirmation:
            decision = Decision.CONFIRM
        if decision == Decision.CONFIRM and action_key and action_key in self._session_allow:
            return PermissionResult(Decision.ALLOW, risk, "previously confirmed in this session", category)
        reason = {
            Decision.ALLOW: f"{risk.value} action in '{category}' is allowed",
            Decision.CONFIRM: f"{risk.value} action in '{category}' requires confirmation",
            Decision.DENY: f"{risk.value} action in '{category}' is denied by policy",
        }[decision]
        return PermissionResult(decision, risk, reason, category)

    def remember_allow(self, action_key: str) -> None:
        self._session_allow.add(action_key)

    def set_category_enabled(self, category: str, enabled: bool) -> None:
        if enabled:
            self.disabled_categories.discard(category.lower())
        else:
            self.disabled_categories.add(category.lower())

    def is_category_enabled(self, category: str) -> bool:
        return category.lower() not in self.disabled_categories
