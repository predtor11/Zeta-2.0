from pathlib import Path

import pytest

from app.security.permissions import DEFAULT_POLICY, Decision, PermissionManager, RiskLevel


def test_default_policy_conservative():
    pm = PermissionManager()
    assert pm.check("x", "unknown_category", RiskLevel.READ_ONLY).decision == Decision.ALLOW
    assert pm.check("x", "unknown_category", RiskLevel.SENSITIVE).decision == Decision.CONFIRM
    assert pm.check("x", "unknown_category", RiskLevel.DANGEROUS).decision == Decision.CONFIRM
    assert pm.check("x", "unknown_category", RiskLevel.BLOCKED).decision == Decision.DENY


def test_terminal_defaults():
    pm = PermissionManager()
    assert pm.check("execute_command", "terminal", RiskLevel.SAFE).decision == Decision.ALLOW
    assert pm.check("execute_command", "terminal", RiskLevel.SENSITIVE).decision == Decision.CONFIRM
    assert pm.check("execute_command", "terminal", RiskLevel.DANGEROUS).decision == Decision.DENY


def test_spec_style_aliases():
    pm = PermissionManager({"filesystem": {"read": "allowed", "write": "allowed", "delete": "confirmation"},
                            "terminal": {"safe_commands": "allowed", "sensitive_commands": "confirmation", "dangerous_commands": "blocked"}})
    assert pm.check("t", "filesystem", RiskLevel.READ_ONLY).decision == Decision.ALLOW
    assert pm.check("t", "filesystem", RiskLevel.SENSITIVE).decision == Decision.ALLOW
    assert pm.check("t", "filesystem", RiskLevel.DANGEROUS).decision == Decision.CONFIRM
    assert pm.check("t", "terminal", RiskLevel.DANGEROUS).decision == Decision.DENY


def test_requires_confirmation_upgrades_allow():
    pm = PermissionManager()
    r = pm.check("delete_files", "filesystem", RiskLevel.SAFE, requires_confirmation=True)
    assert r.decision == Decision.CONFIRM


def test_disabled_category_and_tool():
    pm = PermissionManager({"enabled": {"browser": False}, "disabled_tools": ["kill_process"]})
    assert pm.check("open_website", "browser", RiskLevel.SAFE).decision == Decision.DENY
    assert pm.check("kill_process", "terminal", RiskLevel.SAFE).decision == Decision.DENY
    pm.set_category_enabled("browser", True)
    assert pm.check("open_website", "browser", RiskLevel.SAFE).decision == Decision.ALLOW


def test_session_remember():
    pm = PermissionManager()
    key = "send_whatsapp_message:{}"
    assert pm.check("send_whatsapp_message", "messaging", RiskLevel.SENSITIVE, action_key=key).decision == Decision.CONFIRM
    pm.remember_allow(key)
    assert pm.check("send_whatsapp_message", "messaging", RiskLevel.SENSITIVE, action_key=key).decision == Decision.ALLOW


def test_blocked_never_allowed_even_if_policy_says_allow():
    pm = PermissionManager({"terminal": {"BLOCKED": "allow"}})
    assert pm.check("x", "terminal", RiskLevel.BLOCKED).decision == Decision.DENY


def test_yaml_roundtrip(tmp_path: Path):
    pm = PermissionManager({"browser": {"SENSITIVE": "deny"}, "enabled": {"email": False}})
    p = tmp_path / "perm.yaml"
    pm.save(p)
    pm2 = PermissionManager.from_files(p)
    assert pm2.check("x", "browser", RiskLevel.SENSITIVE).decision == Decision.DENY
    assert not pm2.is_category_enabled("email")


def test_default_policy_covers_all_categories():
    for cat, levels in DEFAULT_POLICY.items():
        assert set(levels) == {l.value for l in RiskLevel}
