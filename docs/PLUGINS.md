# Writing a plugin

A plugin bundles tools for one capability (Spotify, Calendar, Notion, smart home, another
computer…). Built-in plugins live in `backend/app/tools/<name>/`; external ones in
`plugins/<name>/`. Both look the same.

## 1. Create the folder

```
plugins/spotify/__init__.py
```

## 2. Define tools

```python
from typing import Any, Dict
from pydantic import BaseModel, Field
from app.plugins.loader import Plugin
from app.security.permissions import RiskLevel
from app.tools.base import Tool, ToolContext, ToolResult


class PlayArgs(BaseModel):
    query: str = Field(description="Song, album or playlist to play")


class PlayTool(Tool):
    name = "spotify_play"                       # unique, snake_case; the LLM sees this
    description = "Play a song, album or playlist on Spotify."
    category = "spotify"                        # permission category (defaults to plugin name)
    risk_level = RiskLevel.SAFE                 # READ_ONLY | SAFE | SENSITIVE | DANGEROUS
    requires_confirmation = False               # True forces a confirmation even when policy says allow
    args_model = PlayArgs                       # validated before run(); schema auto-derived
    available_in_cloud = False                  # needs the host? hide in cloud mode
    log_arguments = True                        # False if args may contain private text

    def classify(self, args: Dict[str, Any]) -> RiskLevel:   # optional per-call refinement
        return self.risk_level

    def describe(self, args: Dict[str, Any]) -> str:         # shown in confirmations / activity
        return f"Play '{args['query']}' on Spotify"

    async def preview(self, args, ctx) -> Dict[str, Any]:    # optional extra details for the modal
        return {}

    async def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        token = ctx.secrets.get("spotify_token")             # secrets never reach the model
        if not token:
            return ToolResult.fail("Spotify is not configured. Set SPOTIFY_TOKEN.")
        ctx.activity("Contacting Spotify…")                  # activity panel line
        ...
        return ToolResult.ok({"now_playing": "..."}, summary="Playing")
```

`ToolResult` fields: `success`, `output` (JSON-serialisable, shown to the model), `error`,
`summary` (one line for the UI), `untrusted=True` + `source` for external content (web, email,
documents), `artifacts` (e.g. `image_b64`, `path` for the UI).

`ToolContext` gives you `settings`, `secrets`, `permissions`, `task_id`, `conversation_id`,
`activity()`, and shared services via `ctx.service("browser" | "file_index" | "llm" |
"long_term_memory" | "contacts" | "scheduler" | "tasks" | ...)`.

## 3. Export the plugin

```python
async def health() -> Dict[str, Any]:
    return {"ok": True, "detail": "token configured"}

PLUGIN = Plugin(
    name="spotify",
    description="Control Spotify playback",
    permissions=["spotify"],
    tools=[PlayTool()],                     # or a factory: tools=lambda settings: [...]
    configuration={"SPOTIFY_TOKEN": "OAuth token"},
    health_check=health,
)
```

## 4. Permissions

Add a block to `backend/config/permissions.yaml` (or rely on `default`):

```yaml
permissions:
  spotify:
    READ_ONLY: allow
    SAFE: allow
    SENSITIVE: confirm
    DANGEROUS: confirm
    BLOCKED: deny
enabled:
  spotify: true
```

## 5. Restart Zeta

The loader logs `Plugin 'spotify' loaded with N tools`; the tools appear in the Tools panel and
in `GET /api/tools`, and the model can use them immediately.

## Guidelines

- Validate everything with the `args_model`; never pass model-provided strings to a shell.
- Prefer APIs over UI automation; prefer read-only tools for discovery and a separate
  SENSITIVE/DANGEROUS tool for changes.
- Mark output that comes from outside (APIs returning user-generated content, web pages) as `untrusted`.
- Return `ToolResult.fail(...)` with a clear human explanation rather than raising for expected failures.
- Return `ToolResult.not_implemented("...")` for scaffolded capabilities instead of faking success.
- Keep secrets in `.env`/keyring and fetch them with `ctx.secrets.get(name)` (add the name to `SecretStore._from_env`).
