"""Peekaboo integration for macOS UI automation.

This module provides a Python wrapper around the Peekaboo CLI for macOS
UI automation, screen capture, and mouse/keyboard control.
"""

import asyncio
import json
import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("pyclawops.utils.peekaboo")


class PeekabooConfig:
    """Peekaboo configuration."""
    
    def __init__(
        self,
        capture_path: str = "/tmp/peekaboo",
        json_output: bool = True,
        verbose: bool = False,
    ):
        self.capture_path = capture_path
        self.json_output = json_output
        self.verbose = verbose
        self._logger = logging.getLogger("pyclawops.peekaboo")
    
    @property
    def peekaboo_bin(self) -> str:
        """Path to peekaboo binary."""
        return shutil.which("peekaboo") or "peekaboo"


@dataclass
class PeekabooResult:
    """Result of a Peekaboo operation."""
    success: bool
    data: Optional[Dict[str, Any]] = None
    stdout: str = ""
    stderr: str = ""
    error: Optional[str] = None


class Peekaboo:
    """Peekaboo CLI wrapper for macOS UI automation."""
    
    def __init__(self, config: Optional[PeekabooConfig] = None):
        self.config = config or PeekabooConfig()
        self._logger = logging.getLogger("pyclawops.peekaboo")
    
    async def _run(self, args: List[str]) -> PeekabooResult:
        """Run a peekaboo command."""
        cmd = [self.config.peekaboo_bin] + args
        
        if self.config.json_output:
            cmd.append("--json")
        
        if self.config.verbose:
            cmd.append("--verbose")
        
        self._logger.debug(f"Running: {' '.join(cmd)}")
        
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            
            stdout, stderr = await process.communicate()
            stdout_str = stdout.decode("utf-8", errors="replace")
            stderr_str = stderr.decode("utf-8", errors="replace")
            
            if process.returncode != 0:
                return PeekabooResult(
                    success=False,
                    stdout=stdout_str,
                    stderr=stderr_str,
                    error=stderr_str or f"Command failed with code {process.returncode}"
                )
            
            # Parse JSON output if available
            data = None
            if self.config.json_output and stdout_str.strip():
                try:
                    data = json.loads(stdout_str)
                except json.JSONDecodeError:
                    data = {"raw": stdout_str}
            
            return PeekabooResult(
                success=True,
                data=data,
                stdout=stdout_str,
                stderr=stderr_str,
            )
            
        except FileNotFoundError:
            return PeekabooResult(
                success=False,
                error="Peekaboo not found. Install with: brew install steipete/tap/peekaboo"
            )
        except Exception as e:
            return PeekabooResult(success=False, error=str(e))
    
    def _run_sync(self, args: List[str]) -> PeekabooResult:
        """Run a peekaboo command synchronously."""
        return asyncio.get_event_loop().run_until_complete(self._run(args))
    
    # Permission checks
    async def check_permissions(self) -> PeekabooResult:
        """Check Screen Recording and Accessibility permissions."""
        return await self._run(["permissions"])
    
    def check_permissions_sync(self) -> PeekabooResult:
        """Check permissions synchronously."""
        return self._run_sync(["permissions"])
    
    # Listing
    async def list_apps(self) -> PeekabooResult:
        """List running applications."""
        return await self._run(["list", "apps"])
    
    async def list_windows(self, app: Optional[str] = None) -> PeekabooResult:
        """List windows, optionally filtered by app."""
        args = ["list", "windows"]
        if app:
            args.extend(["--app", app])
        return await self._run(args)
    
    async def list_screens(self) -> PeekabooResult:
        """List available screens."""
        return await self._run(["list", "screens"])
    
    # Screen capture
    async def capture_screen(
        self,
        screen_index: int = 0,
        path: Optional[str] = None,
        retina: bool = True,
        annotate: bool = False,
    ) -> PeekabooResult:
        """Capture a screenshot."""
        args = ["image", "--mode", "screen", "--screen-index", str(screen_index)]
        
        if path:
            args.extend(["--path", path])
        if retina:
            args.append("--retina")
        if annotate:
            args.append("--annotate")
        
        return await self._run(args)
    
    async def capture_window(
        self,
        window_title: Optional[str] = None,
        window_id: Optional[int] = None,
        path: Optional[str] = None,
        annotate: bool = False,
    ) -> PeekabooResult:
        """Capture a window screenshot."""
        args = ["image", "--mode", "window"]
        
        if window_title:
            args.extend(["--window-title", window_title])
        if window_id:
            args.extend(["--window-id", str(window_id)])
        if path:
            args.extend(["--path", path])
        if annotate:
            args.append("--annotate")
        
        return await self._run(args)
    
    async def capture_app(
        self,
        app: str,
        path: Optional[str] = None,
        annotate: bool = False,
    ) -> PeekabooResult:
        """Capture the frontmost window of an app."""
        args = ["image", "--mode", "app", "--app", app]
        
        if path:
            args.extend(["--path", path])
        if annotate:
            args.append("--annotate")
        
        return await self._run(args)
    
    # UI inspection
    async def see(
        self,
        app: Optional[str] = None,
        window_title: Optional[str] = None,
        annotate: bool = True,
        path: Optional[str] = None,
    ) -> PeekabooResult:
        """Get UI map with element IDs."""
        args = ["see"]
        
        if app:
            args.extend(["--app", app])
        if window_title:
            args.extend(["--window-title", window_title])
        if annotate:
            args.append("--annotate")
        if path:
            args.extend(["--path", path])
        
        return await self._run(args)
    
    # Mouse/keyboard interaction
    async def click(
        self,
        on: Optional[str] = None,
        coords: Optional[str] = None,
        app: Optional[str] = None,
    ) -> PeekabooResult:
        """Click an element or coordinates."""
        args = ["click"]
        
        if on:
            args.extend(["--on", on])
        if coords:
            args.extend(["--coords", coords])
        if app:
            args.extend(["--app", app])
        
        return await self._run(args)
    
    async def double_click(
        self,
        on: Optional[str] = None,
        coords: Optional[str] = None,
    ) -> PeekabooResult:
        """Double-click an element or coordinates."""
        args = ["click", "--double"]
        
        if on:
            args.extend(["--on", on])
        if coords:
            args.extend(["--coords", coords])
        
        return await self._run(args)
    
    async def right_click(
        self,
        on: Optional[str] = None,
        coords: Optional[str] = None,
    ) -> PeekabooResult:
        """Right-click an element or coordinates."""
        args = ["click", "--right"]
        
        if on:
            args.extend(["--on", on])
        if coords:
            args.extend(["--coords", coords])
        
        return await self._run(args)
    
    async def type_text(
        self,
        text: str,
        app: Optional[str] = None,
        clear: bool = False,
        return_key: bool = False,
    ) -> PeekabooResult:
        """Type text."""
        args = ["type", text]
        
        if app:
            args.extend(["--app", app])
        if clear:
            args.append("--clear")
        if return_key:
            args.append("--return")
        
        return await self._run(args)
    
    async def press_key(self, key: str, count: int = 1) -> PeekabooResult:
        """Press a special key."""
        args = ["press", key, "--count", str(count)]
        return await self._run(args)
    
    async def hotkey(self, keys: str) -> PeekabooResult:
        """Press a hotkey combination (e.g., 'cmd,shift,t')."""
        return await self._run(["hotkey", "--keys", keys])
    
    async def move_mouse(self, coords: str, smooth: bool = False) -> PeekabooResult:
        """Move mouse to coordinates."""
        args = ["move", coords]
        
        if smooth:
            args.append("--smooth")
        
        return await self._run(args)
    
    async def drag(
        self,
        from_el: Optional[str] = None,
        to_el: Optional[str] = None,
        from_coords: Optional[str] = None,
        to_coords: Optional[str] = None,
        duration: int = 500,
    ) -> PeekabooResult:
        """Drag from one element/coordinates to another."""
        args = ["drag"]
        
        if from_el:
            args.extend(["--from", from_el])
        if to_el:
            args.extend(["--to", to_el])
        if from_coords:
            args.extend(["--from-coords", from_coords])
        if to_coords:
            args.extend(["--to-coords", to_coords])
        
        args.extend(["--duration", str(duration)])
        
        return await self._run(args)
    
    async def scroll(self, direction: str, amount: int = 3, smooth: bool = True) -> PeekabooResult:
        """Scroll in a direction."""
        args = ["scroll", "--direction", direction, "--amount", str(amount)]
        
        if smooth:
            args.append("--smooth")
        
        return await self._run(args)
    
    # App management
    async def launch_app(self, app: str, url: Optional[str] = None) -> PeekabooResult:
        """Launch an application."""
        args = ["app", "launch", app]
        
        if url:
            args.extend(["--open", url])
        
        return await self._run(args)
    
    async def quit_app(self, app: str) -> PeekabooResult:
        """Quit an application."""
        return await self._run(["app", "quit", "--app", app])
    
    async def switch_app(self, app: str) -> PeekabooResult:
        """Switch to an application."""
        return await self._run(["app", "switch", "--app", app])
    
    # Window management
    async def focus_window(
        self,
        app: Optional[str] = None,
        window_title: Optional[str] = None,
        window_id: Optional[int] = None,
    ) -> PeekabooResult:
        """Focus a window."""
        args = ["window", "focus"]
        
        if app:
            args.extend(["--app", app])
        if window_title:
            args.extend(["--window-title", window_title])
        if window_id:
            args.extend(["--window-id", str(window_id)])
        
        return await self._run(args)
    
    async def close_window(
        self,
        app: Optional[str] = None,
        window_title: Optional[str] = None,
    ) -> PeekabooResult:
        """Close a window."""
        args = ["window", "close"]
        
        if app:
            args.extend(["--app", app])
        if window_title:
            args.extend(["--window-title", window_title])
        
        return await self._run(args)
    
    async def set_window_bounds(
        self,
        x: int,
        y: int,
        width: int,
        height: int,
        app: Optional[str] = None,
        window_title: Optional[str] = None,
    ) -> PeekabooResult:
        """Set window position and size."""
        args = ["window", "set-bounds", "--x", str(x), "--y", str(y),
                "--width", str(width), "--height", str(height)]
        
        if app:
            args.extend(["--app", app])
        if window_title:
            args.extend(["--window-title", window_title])
        
        return await self._run(args)
    
    # Clipboard
    async def get_clipboard(self) -> PeekabooResult:
        """Get clipboard contents."""
        return await self._run(["clipboard", "read"])
    
    async def set_clipboard(self, text: str) -> PeekabooResult:
        """Set clipboard contents."""
        return await self._run(["clipboard", "write", text])
    
    # Dialogs
    async def click_dialog_button(self, button: str = "OK") -> PeekabooResult:
        """Click a button in a system dialog."""
        return await self._run(["dialog", "click", "--button", button])
    
    # Menu
    async def click_menu(self, app: str, item: str) -> PeekabooResult:
        """Click a menu item."""
        return await self._run(["menu", "click", "--app", app, "--item", item])
    
    async def click_menu_path(self, app: str, path: str) -> PeekabooResult:
        """Click a menu item by path (e.g., 'Format > Font > Bold')."""
        return await self._run(["menu", "click", "--app", app, "--path", path])
    
    # Sleep/wait
    async def sleep(self, seconds: float) -> PeekabooResult:
        """Pause execution for a duration."""
        return await self._run(["sleep", str(seconds)])


# Synchronous wrapper
class PeekabooSync:
    """Synchronous wrapper for Peekaboo."""
    
    def __init__(self, config: Optional[PeekabooConfig] = None):
        self._peekaboo = Peekaboo(config)
    
    def __getattr__(self, name):
        """Delegate to async methods with sync wrapper."""
        attr = getattr(self._peekaboo, name)
        if asyncio.iscoroutinefunction(attr):
            def sync_wrapper(*args, **kwargs):
                return asyncio.get_event_loop().run_until_complete(attr(*args, **kwargs))
            return sync_wrapper
        return attr


# Factory function
def create_peekaboo(config: Optional[PeekabooConfig] = None) -> Peekaboo:
    """Create a Peekaboo instance."""
    return Peekaboo(config)
