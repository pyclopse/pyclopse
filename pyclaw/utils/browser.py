"""Browser automation using Playwright."""

import asyncio
import base64
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from pyclaw.config.schema import BaseModel

logger = logging.getLogger("pyclaw.utils.browser")


class BrowserConfig(BaseModel):
    """Browser automation configuration."""
    headless: bool = True
    slow_mo: int = 0  # ms between actions
    timeout: int = 30000  # ms
    viewport_width: int = 1280
    viewport_height: int = 720
    downloads_path: Optional[str] = None
    args: List[str] = []  # Additional chromium args


@dataclass
class BrowserResult:
    """Result of browser operation."""
    success: bool
    data: Any = None
    error: Optional[str] = None


class PlaywrightBrowser:
    """Playwright-based browser automation."""
    
    def __init__(self, config: Optional[BrowserConfig] = None):
        self.config = config or BrowserConfig()
        self._browser = None
        self._context = None
        self._page = None
        self._playwright = None
        self._logger = logging.getLogger("pyclaw.browser")
    
    async def start(self) -> BrowserResult:
        """Start the browser."""
        try:
            from playwright.async_api import async_playwright
            
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=self.config.headless,
                slow_mo=self.config.slow_mo,
                args=self.config.args or None,
            )
            
            self._context = await self._browser.new_context(
                viewport={
                    "width": self.config.viewport_width,
                    "height": self.config.viewport_height,
                },
                downloads_path=self.config.downloads_path,
            )
            
            self._page = await self._context.new_page()
            self._page.set_default_timeout(self.config.timeout)
            
            self._logger.info("Browser started successfully")
            return BrowserResult(success=True)
            
        except ImportError:
            return BrowserResult(
                success=False,
                error="Playwright not installed. Run: pip install playwright && playwright install chromium"
            )
        except Exception as e:
            self._logger.error(f"Failed to start browser: {e}")
            return BrowserResult(success=False, error=str(e))
    
    async def stop(self) -> None:
        """Stop the browser."""
        try:
            if self._page:
                await self._page.close()
            if self._context:
                await self._context.close()
            if self._browser:
                await self._browser.close()
            if self._playwright:
                await self._playwright.stop()
            self._logger.info("Browser stopped")
        except Exception as e:
            self._logger.error(f"Error stopping browser: {e}")
    
    async def navigate(self, url: str) -> BrowserResult:
        """Navigate to a URL."""
        if not self._page:
            return BrowserResult(success=False, error="Browser not started")
        
        try:
            await self._page.goto(url, wait_until="domcontentloaded")
            title = await self._page.title()
            self._logger.info(f"Navigated to {url}: {title}")
            return BrowserResult(success=True, data={"title": title, "url": url})
        except Exception as e:
            return BrowserResult(success=False, error=str(e))
    
    async def screenshot(self, path: Optional[str] = None, full_page: bool = False) -> BrowserResult:
        """Take a screenshot."""
        if not self._page:
            return BrowserResult(success=False, error="Browser not started")
        
        try:
            if path:
                await self._page.screenshot(path=path, full_page=full_page)
                return BrowserResult(success=True, data={"path": path})
            else:
                # Return base64 encoded screenshot
                data = await self._page.screenshot(full_page=full_page)
                b64 = base64.b64encode(data).decode()
                return BrowserResult(success=True, data={"base64": b64})
        except Exception as e:
            return BrowserResult(success=False, error=str(e))
    
    async def click(self, selector: str) -> BrowserResult:
        """Click an element."""
        if not self._page:
            return BrowserResult(success=False, error="Browser not started")
        
        try:
            await self._page.click(selector)
            self._logger.info(f"Clicked: {selector}")
            return BrowserResult(success=True)
        except Exception as e:
            return BrowserResult(success=False, error=str(e))
    
    async def type_text(self, selector: str, text: str, delay: int = 0) -> BrowserResult:
        """Type text into an element."""
        if not self._page:
            return BrowserResult(success=False, error="Browser not started")
        
        try:
            await self._page.fill(selector, text)
            self._logger.info(f"Typed into {selector}: {text[:20]}...")
            return BrowserResult(success=True)
        except Exception as e:
            return BrowserResult(success=False, error=str(e))
    
    async def fill_form(self, fields: Dict[str, str]) -> BrowserResult:
        """Fill multiple form fields."""
        if not self._page:
            return BrowserResult(success=False, error="Browser not started")
        
        try:
            for selector, value in fields.items():
                await self._page.fill(selector, value)
            self._logger.info(f"Filled form with {len(fields)} fields")
            return BrowserResult(success=True, data={"fields_filled": len(fields)})
        except Exception as e:
            return BrowserResult(success=False, error=str(e))
    
    async def submit(self, selector: str = "form") -> BrowserResult:
        """Submit a form."""
        if not self._page:
            return BrowserResult(success=False, error="Browser not started")
        
        try:
            await self._page.click(selector)
            await self._page.wait_for_load_state("networkidle")
            return BrowserResult(success=True)
        except Exception as e:
            return BrowserResult(success=False, error=str(e))
    
    async def get_text(self, selector: str) -> BrowserResult:
        """Get text content of an element."""
        if not self._page:
            return BrowserResult(success=False, error="Browser not started")
        
        try:
            text = await self._page.text_content(selector)
            return BrowserResult(success=True, data={"text": text})
        except Exception as e:
            return BrowserResult(success=False, error=str(e))
    
    async def get_html(self, selector: Optional[str] = None) -> BrowserResult:
        """Get HTML content."""
        if not self._page:
            return BrowserResult(success=False, error="Browser not started")
        
        try:
            if selector:
                html = await self._page.inner_html(selector)
            else:
                html = await self._page.content()
            return BrowserResult(success=True, data={"html": html})
        except Exception as e:
            return BrowserResult(success=False, error=str(e))
    
    async def evaluate(self, script: str) -> BrowserResult:
        """Evaluate JavaScript."""
        if not self._page:
            return BrowserResult(success=False, error="Browser not started")
        
        try:
            result = await self._page.evaluate(script)
            return BrowserResult(success=True, data=result)
        except Exception as e:
            return BrowserResult(success=False, error=str(e))
    
    async def wait_for_selector(self, selector: str, timeout: Optional[int] = None) -> BrowserResult:
        """Wait for a selector to appear."""
        if not self._page:
            return BrowserResult(success=False, error="Browser not started")
        
        try:
            await self._page.wait_for_selector(selector, timeout=timeout)
            return BrowserResult(success=True)
        except Exception as e:
            return BrowserResult(success=False, error=str(e))
    
    async def wait_for_navigation(self, timeout: Optional[int] = None) -> BrowserResult:
        """Wait for navigation to complete."""
        if not self._page:
            return BrowserResult(success=False, error="Browser not started")
        
        try:
            await self._page.wait_for_load_state("networkidle", timeout=timeout)
            return BrowserResult(success=True)
        except Exception as e:
            return BrowserResult(success=False, error=str(e))
    
    async def get_cookies(self) -> BrowserResult:
        """Get all cookies."""
        if not self._context:
            return BrowserResult(success=False, error="Browser not started")
        
        try:
            cookies = await self._context.cookies()
            return BrowserResult(success=True, data={"cookies": cookies})
        except Exception as e:
            return BrowserResult(success=False, error=str(e))
    
    async def set_cookies(self, cookies: List[Dict[str, Any]]) -> BrowserResult:
        """Set cookies."""
        if not self._context:
            return BrowserResult(success=False, error="Browser not started")
        
        try:
            await self._context.add_cookies(cookies)
            return BrowserResult(success=True)
        except Exception as e:
            return BrowserResult(success=False, error=str(e))


class Browser:
    """Synchronous wrapper for PlaywrightBrowser."""
    
    def __init__(self, config: Optional[BrowserConfig] = None):
        self._browser = PlaywrightBrowser(config)
    
    def __enter__(self):
        asyncio.get_event_loop().run_until_complete(self._browser.start())
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        asyncio.get_event_loop().run_until_complete(self._browser.stop())
    
    def __getattr__(self, name):
        """Delegate async methods to sync."""
        attr = getattr(self._browser, name)
        if asyncio.iscoroutinefunction(attr):
            def sync_wrapper(*args, **kwargs):
                return asyncio.get_event_loop().run_until_complete(attr(*args, **kwargs))
            return sync_wrapper
        return attr


# Factory function
def create_browser(config: Optional[BrowserConfig] = None) -> PlaywrightBrowser:
    """Create a Playwright browser instance."""
    return PlaywrightBrowser(config)
