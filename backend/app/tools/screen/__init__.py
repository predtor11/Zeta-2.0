"""Screen interaction (optional): analyse a screenshot with a vision model,
click/type/scroll/hotkeys via pyautogui.  Structured tools are always preferred;
these are SENSITIVE by default so they ask for confirmation.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any, Dict, List

from pydantic import BaseModel, Field

from app.plugins.loader import Plugin
from app.providers.llm.base import image_content_part
from app.security.permissions import RiskLevel
from app.tools.base import Tool, ToolContext, ToolResult, run_sync


def _pyautogui():
    try:
        import pyautogui  # type: ignore

        pyautogui.FAILSAFE = True
        pyautogui.PAUSE = 0.05
        return pyautogui
    except ImportError:
        return None


class AnalyzeArgs(BaseModel):
    question: str = Field(default="Describe what is on the screen and list the main UI elements with approximate positions.",
                          description="What to look for / answer about the screen")


class AnalyzeScreenTool(Tool):
    name = "analyze_screen"
    description = ("Take a screenshot and ask the vision-capable model a question about it (what's on screen, where a button is, "
                   "what an error says). Requires a vision model (LLM_VISION_MODEL or a multimodal LLM_MODEL).")
    category = "screen"
    risk_level = RiskLevel.READ_ONLY
    args_model = AnalyzeArgs
    available_in_cloud = False
    timeout_seconds = 240

    async def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            from PIL import ImageGrab
        except ImportError:
            return ToolResult.fail("Pillow is not installed.")
        import io

        def _grab():
            img = ImageGrab.grab()
            w, h = img.size
            img.thumbnail((1600, 1000))
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return (w, h), img.size, base64.b64encode(buf.getvalue()).decode()

        (w, h), (sw, sh), b64 = await run_sync(_grab)
        llm = ctx.service("llm")
        model = ctx.settings.llm_vision_model or None
        if not model and not getattr(llm, "supports_vision", False):
            return ToolResult.fail("No vision-capable model is configured. Set LLM_VISION_MODEL (e.g. llava, moondream, gpt-4o-mini).")
        prompt = (f"{args['question']}\nThe screenshot is {sw}x{sh} pixels (scaled from the real {w}x{h} screen). "
                  "When giving positions, give x,y in the scaled image coordinates.")
        try:
            resp = await llm.chat([{"role": "user", "content": [{"type": "text", "text": prompt}, image_content_part(b64)]}], None, model=model)
        except Exception as e:  # noqa: BLE001
            return ToolResult.fail(f"Vision analysis failed: {getattr(e, 'user_message', str(e))}")
        return ToolResult.ok({"analysis": resp.content, "screen": {"width": w, "height": h}, "image": {"width": sw, "height": sh},
                              "scale_x": w / sw, "scale_y": h / sh}, summary="Screen analysed", untrusted=True, source="screen",
                             artifacts={"image_b64": b64})


class ClickArgs(BaseModel):
    x: int = Field(ge=0)
    y: int = Field(ge=0)
    button: str = Field(default="left", pattern="^(left|right|middle)$")
    double: bool = False
    scaled: bool = Field(default=False, description="True if x,y come from analyze_screen's scaled image")
    scale_x: float = 1.0
    scale_y: float = 1.0


class ClickTool(Tool):
    name = "screen_click"
    description = "Click at screen coordinates. Prefer structured tools; use only when necessary."
    category = "screen"
    risk_level = RiskLevel.SENSITIVE
    args_model = ClickArgs
    available_in_cloud = False

    def describe(self, args: Dict[str, Any]) -> str:
        return f"{'Double-' if args.get('double') else ''}{args.get('button', 'left')} click at ({args.get('x')}, {args.get('y')})"

    async def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        pg = _pyautogui()
        if pg is None:
            return ToolResult.fail("pyautogui is not installed (pip install pyautogui).")
        x, y = args["x"], args["y"]
        if args.get("scaled"):
            x, y = int(x * args.get("scale_x", 1.0)), int(y * args.get("scale_y", 1.0))
        try:
            if args.get("double"):
                await run_sync(pg.doubleClick, x, y, button=args.get("button", "left"))
            else:
                await run_sync(pg.click, x, y, button=args.get("button", "left"))
        except Exception as e:  # noqa: BLE001
            return ToolResult.fail(f"Click failed: {e}")
        return ToolResult.ok({"clicked": [x, y]}, summary=f"Clicked ({x}, {y})")


class TypeArgs(BaseModel):
    text: str = Field(max_length=5000)
    press_enter: bool = False


class TypeTool(Tool):
    name = "screen_type"
    description = "Type text into the currently focused window (keyboard input)."
    category = "screen"
    risk_level = RiskLevel.SENSITIVE
    args_model = TypeArgs
    available_in_cloud = False
    log_arguments = False

    def describe(self, args: Dict[str, Any]) -> str:
        return f"Type {len(args.get('text', ''))} characters" + (" and press Enter" if args.get("press_enter") else "")

    async def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        pg = _pyautogui()
        if pg is None:
            return ToolResult.fail("pyautogui is not installed (pip install pyautogui).")
        try:
            await run_sync(pg.write, args["text"], interval=0.01)
            if args.get("press_enter"):
                await run_sync(pg.press, "enter")
        except Exception as e:  # noqa: BLE001
            return ToolResult.fail(f"Typing failed: {e}")
        return ToolResult.ok({"typed_chars": len(args["text"])}, summary="Typed text")


class HotkeyArgs(BaseModel):
    keys: List[str] = Field(description="Keys to press together, e.g. ['ctrl','s'] or ['alt','tab']", min_length=1, max_length=4)


class HotkeyTool(Tool):
    name = "screen_hotkey"
    description = "Press a keyboard shortcut (e.g. ctrl+s, alt+tab, win+d)."
    category = "screen"
    risk_level = RiskLevel.SENSITIVE
    args_model = HotkeyArgs
    available_in_cloud = False

    def classify(self, args: Dict[str, Any]) -> RiskLevel:
        keys = {k.lower() for k in args.get("keys", [])}
        if keys & {"delete", "f4"} and keys & {"alt", "ctrl", "shift"}:
            return RiskLevel.DANGEROUS
        return RiskLevel.SENSITIVE

    def describe(self, args: Dict[str, Any]) -> str:
        return "Press " + "+".join(args.get("keys", []))

    async def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        pg = _pyautogui()
        if pg is None:
            return ToolResult.fail("pyautogui is not installed (pip install pyautogui).")
        try:
            await run_sync(pg.hotkey, *[k.lower() for k in args["keys"]])
        except Exception as e:  # noqa: BLE001
            return ToolResult.fail(f"Hotkey failed: {e}")
        return ToolResult.ok({"pressed": args["keys"]}, summary="Pressed " + "+".join(args["keys"]))


class ScrollArgs(BaseModel):
    amount: int = Field(description="Positive scrolls up, negative scrolls down (units of ~1 line)", ge=-100, le=100)
    x: int = Field(default=-1)
    y: int = Field(default=-1)


class ScrollTool(Tool):
    name = "screen_scroll"
    description = "Scroll the mouse wheel at the current (or given) position."
    category = "screen"
    risk_level = RiskLevel.SAFE
    args_model = ScrollArgs
    available_in_cloud = False

    async def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        pg = _pyautogui()
        if pg is None:
            return ToolResult.fail("pyautogui is not installed (pip install pyautogui).")
        kw = {}
        if args.get("x", -1) >= 0 and args.get("y", -1) >= 0:
            kw = {"x": args["x"], "y": args["y"]}
        try:
            await run_sync(pg.scroll, args["amount"] * 100, **kw)
        except Exception as e:  # noqa: BLE001
            return ToolResult.fail(f"Scroll failed: {e}")
        return ToolResult.ok({"scrolled": args["amount"]}, summary="Scrolled")


async def _health() -> Dict[str, Any]:
    return {"ok": _pyautogui() is not None, "detail": "pyautogui available" if _pyautogui() else "pyautogui not installed (optional)"}


PLUGIN = Plugin(
    name="screen",
    description="Optional screen awareness: vision analysis, click, type, hotkeys, scroll",
    permissions=["screen"],
    tools=lambda s: [AnalyzeScreenTool(), ClickTool(), TypeTool(), HotkeyTool(), ScrollTool()],
    configuration={"LLM_VISION_MODEL": "vision-capable model for analyze_screen"},
    health_check=_health,
)
