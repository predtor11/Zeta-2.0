"""Plugin system.

A plugin is a Python module (built-in under `app/tools/<name>/` or external
under `<project>/plugins/<name>/`) that exposes a `PLUGIN` object:

    PLUGIN = Plugin(
        name="filesystem",
        description="Search, open and manage files",
        permissions=["filesystem"],
        tools=[SearchFilesTool(), ...]   # or a factory: tools=lambda settings: [...]
        configuration={"FS_ALLOWED_ROOTS": "..."},
        health_check=async_fn,
    )
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Union

from app.tools.base import Tool, ToolRegistry

log = logging.getLogger(__name__)

ToolsSpec = Union[List[Tool], Callable[[Any], List[Tool]]]


@dataclass
class Plugin:
    name: str
    description: str
    permissions: List[str] = field(default_factory=list)
    tools: ToolsSpec = field(default_factory=list)
    configuration: Dict[str, str] = field(default_factory=dict)  # env var -> description
    health_check: Optional[Callable[[], Awaitable[Dict[str, Any]]]] = None
    version: str = "0.1.0"
    builtin: bool = True
    _loaded_tools: List[Tool] = field(default_factory=list, repr=False)

    def build_tools(self, settings: Any) -> List[Tool]:
        tools = self.tools(settings) if callable(self.tools) else list(self.tools)
        self._loaded_tools = tools
        return tools

    async def health(self) -> Dict[str, Any]:
        if self.health_check is None:
            return {"ok": True, "detail": "no health check"}
        try:
            return await self.health_check()
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "detail": f"{e.__class__.__name__}: {e}"}

    def info(self) -> Dict[str, Any]:
        return {
            "name": self.name, "description": self.description, "permissions": self.permissions,
            "tools": [t.name for t in self._loaded_tools], "configuration": self.configuration,
            "version": self.version, "builtin": self.builtin,
        }


BUILTIN_PLUGIN_MODULES = [
    "app.tools.agent_tools",
    "app.tools.filesystem",
    "app.tools.computer",
    "app.tools.terminal",
    "app.tools.browser",
    "app.tools.developer",
    "app.tools.memory",
    "app.tools.messaging",
    "app.tools.email",
    "app.tools.screen",
    "app.tools.scheduler",
]


class PluginManager:
    def __init__(self, registry: ToolRegistry, settings: Any, external_dir: Optional[Path] = None):
        self.registry = registry
        self.settings = settings
        self.external_dir = external_dir
        self.plugins: Dict[str, Plugin] = {}

    def load_builtin(self) -> None:
        for mod_name in BUILTIN_PLUGIN_MODULES:
            try:
                mod = importlib.import_module(mod_name)
            except Exception as e:  # noqa: BLE001
                log.error("Failed to import builtin plugin %s: %s", mod_name, e, exc_info=True)
                continue
            plugin = getattr(mod, "PLUGIN", None)
            if plugin is None:
                log.warning("Module %s has no PLUGIN object", mod_name)
                continue
            self._register(plugin)

    def load_external(self) -> None:
        if not self.external_dir or not self.external_dir.exists():
            return
        for entry in sorted(self.external_dir.iterdir()):
            init = entry / "__init__.py"
            if not entry.is_dir() or not init.exists():
                continue
            mod_name = f"zeta_plugin_{entry.name}"
            try:
                spec = importlib.util.spec_from_file_location(mod_name, init, submodule_search_locations=[str(entry)])
                if spec is None or spec.loader is None:
                    continue
                mod = importlib.util.module_from_spec(spec)
                sys.modules[mod_name] = mod
                spec.loader.exec_module(mod)
            except Exception as e:  # noqa: BLE001
                log.error("Failed to load external plugin %s: %s", entry.name, e, exc_info=True)
                continue
            plugin = getattr(mod, "PLUGIN", None)
            if plugin is None:
                log.warning("External plugin %s has no PLUGIN object", entry.name)
                continue
            plugin.builtin = False
            self._register(plugin)

    def _register(self, plugin: Plugin) -> None:
        try:
            tools = plugin.build_tools(self.settings)
        except Exception as e:  # noqa: BLE001
            log.error("Plugin %s failed to build tools: %s", plugin.name, e, exc_info=True)
            return
        for t in tools:
            if not t.category or t.category == "general":
                t.category = plugin.name
            self.registry.register(t)
        self.plugins[plugin.name] = plugin
        log.info("Plugin '%s' loaded with %d tools", plugin.name, len(tools))

    async def health(self) -> List[Dict[str, Any]]:
        out = []
        for p in self.plugins.values():
            h = await p.health()
            out.append({**p.info(), "health": h})
        return out
