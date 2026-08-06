# kits/playwright/versions/1.0.1/playwright.py  (Etna edition)
#
# ARCHITECTURE
# ─────────────────────────────────────────────────────────────────────────────
# Chrome runs on the host. "etna browser start" launches it with CDP on
# localhost:9222. This kit calls that command via subprocess when browser_start
# is invoked and Chrome isn't already reachable — no broker, no relay, no socat.
#
# TAB MODEL
# ─────────────────────────────────────────────────────────────────────────────
# Tabs are identified by their CDP target ID — a stable UUID assigned by
# Chrome (e.g. "4B3A2F1E..."). No manual registry, no reaper, no drift.
# _context.pages is always the live source of truth.
#
# THREAD BRIDGE
# ─────────────────────────────────────────────────────────────────────────────
# Tools are sync def wrappers that submit coroutines to a dedicated browser
# event loop via threading.Event. This avoids the future.result() OS scheduling
# hang that can occur under uvicorn.

kit_name        = "Playwright"
kit_description = "Browser automation via Chrome CDP — drives the user's visible Chrome instance"
requirements    = ["playwright"]
config          = {}

import asyncio
import atexit
import base64
import concurrent.futures
import json
import os
import random
import subprocess
import time
import threading
import urllib.request
from utils import tool

# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

_KIT_DIR      = os.path.dirname(os.path.abspath(__file__))
_HISTORY_PATH = os.path.join(_KIT_DIR, "browser_history.json")

# Etna runs on the host — Chrome CDP is plain localhost:9222, no relay needed.
_CDP_HOST        = "localhost"
_CDP_PORT        = 9222
_CDP_VERSION_URL = f"http://{_CDP_HOST}:{_CDP_PORT}/json/version"

_POLL_INTERVAL_S = 0.10
_POLL_TIMEOUT_S  = 15.0

_HUMAN_DELAY_RANGES = {
    "nav":    (0.35, 1.00),
    "action": (0.45, 1.20),
}

_TIMEOUT_MIN_MS = 4000
_TIMEOUT_MAX_MS = 7000

def _default_timeout_ms() -> int:
    return random.randint(_TIMEOUT_MIN_MS, _TIMEOUT_MAX_MS)

# ─────────────────────────────────────────────────────────────────────────────
# Browser history
# ─────────────────────────────────────────────────────────────────────────────

def _load_history() -> list:
    try:
        with open(_HISTORY_PATH) as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception:
        return []

def _save_history(urls: list):
    try:
        with open(_HISTORY_PATH, "w") as f:
            json.dump(urls[-500:], f)
    except Exception:
        pass

# ─────────────────────────────────────────────────────────────────────────────
# Module-level state — minimal, no manual tab registry
# ─────────────────────────────────────────────────────────────────────────────

_pw           = None
_browser      = None
_context      = None
_engine       = "unknown"
_browser_loop = None

# ─────────────────────────────────────────────────────────────────────────────
# CDP liveness check
# ─────────────────────────────────────────────────────────────────────────────

def _cdp_reachable() -> bool:
    # Etna runs on the host — localhost:9222, no Host header spoofing needed.
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(_CDP_VERSION_URL, timeout=2) as r:
            data = json.loads(r.read())
            return bool(data.get("webSocketDebuggerUrl") or data.get("Browser"))
    except Exception:
        return False

def _get_ws_url() -> str:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(_CDP_VERSION_URL, timeout=3) as r:
        # WebSocket URL already points at localhost:9222 — no rewrite needed.
        return json.loads(r.read())["webSocketDebuggerUrl"]

# ─────────────────────────────────────────────────────────────────────────────
# Browser launch via etna CLI
# ─────────────────────────────────────────────────────────────────────────────

def _request_chrome_start():
    """
    Ask Etna to launch Chrome. "etna browser start" is non-blocking — it spawns
    Chrome and writes a PID file. We poll for CDP readiness after calling it.
    """
    try:
        subprocess.Popen(
            ["etna", "browser", "start"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass

def _poll_cdp_ready(timeout_s: float = _POLL_TIMEOUT_S) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if _cdp_reachable():
            return True
        time.sleep(_POLL_INTERVAL_S)
    return False

# ─────────────────────────────────────────────────────────────────────────────
# Context management
# ─────────────────────────────────────────────────────────────────────────────

_CHROME_NOT_AVAILABLE = {
    "error": "chrome_not_available",
    "message": "Chrome is not running. Call browser_start first, then retry.",
}

async def _ensure_context() -> str | None:
    """Connect to Chrome if not already connected. Returns None on success."""
    global _pw, _browser, _context, _engine

    if _context is not None:
        try:
            if _browser is not None and not _browser.is_connected():
                raise RuntimeError("browser disconnected")
            _ = _context.pages
            return None
        except Exception:
            _pw = _browser = _context = None

    loop = asyncio.get_event_loop()
    if not await loop.run_in_executor(None, _cdp_reachable):
        return "chrome_not_available"

    try:
        from patchright.async_api import async_playwright
        _engine = "patchright"
    except ImportError:
        from playwright.async_api import async_playwright
        _engine = "playwright"

    if _pw is None:
        _pw = await async_playwright().start()

    ws_url = await loop.run_in_executor(None, _get_ws_url)
    _browser = await _pw.chromium.connect_over_cdp(ws_url)
    _context = _browser.contexts[0] if _browser.contexts else await _browser.new_context()
    return None

# ─────────────────────────────────────────────────────────────────────────────
# CDP-native tab helpers — no manual registry
# ─────────────────────────────────────────────────────────────────────────────

async def _get_target_id(page) -> str:
    """Get Chrome's stable CDP target ID for a page."""
    try:
        session = await page.context.new_cdp_session(page)
        info = await session.send("Target.getTargetInfo")
        await session.detach()
        return info["targetInfo"]["targetId"]
    except Exception:
        return getattr(getattr(page, "_impl_obj", page), "_guid", "unknown")

async def _find_page(target_id: str):
    """Find a live page by CDP target ID. Returns None if not found."""
    for page in _context.pages:
        try:
            if await _get_target_id(page) == target_id:
                return page
        except Exception:
            continue
    return None

async def _resolve_tab(target_id: str):
    """Returns (page, None) or (None, error_dict)."""
    err = await _ensure_context()
    if err:
        return None, _CHROME_NOT_AVAILABLE
    page = await _find_page(target_id)
    if page is None:
        return None, {
            "error": f"Tab '{target_id}' not found. It may have been closed.",
            "hint": "Call browser_list_tabs to see currently open tabs.",
        }
    return page, None

# ─────────────────────────────────────────────────────────────────────────────
# Thread bridge
# ─────────────────────────────────────────────────────────────────────────────

def _start_browser_thread():
    global _browser_loop, _pw, _engine
    ready = threading.Event()

    def _thread_main():
        global _browser_loop, _pw, _engine
        loop = asyncio.new_event_loop()
        _browser_loop = loop
        ready.set()

        async def _eager_start():
            global _pw, _engine
            try:
                from patchright.async_api import async_playwright
                _engine = "patchright"
            except ImportError:
                from playwright.async_api import async_playwright
                _engine = "playwright"
            _pw = await async_playwright().start()

        loop.run_until_complete(_eager_start())
        loop.run_forever()

    threading.Thread(target=_thread_main, daemon=True).start()
    ready.wait()

def _run(coro):
    done = threading.Event()
    result_box = [None]
    exc_box    = [None]

    async def _wrapper():
        try:
            result_box[0] = await coro
        except Exception as e:
            exc_box[0] = e
        finally:
            done.set()

    _browser_loop.call_soon_threadsafe(asyncio.ensure_future, _wrapper())
    if not done.wait(timeout=120):
        raise TimeoutError("Browser tool timed out after 120 seconds.")
    if exc_box[0] is not None:
        raise exc_box[0]
    return result_box[0]

# ─────────────────────────────────────────────────────────────────────────────
# Page helpers
# ─────────────────────────────────────────────────────────────────────────────

_STEALTH_SCRIPT = """
    const _getParam = WebGLRenderingContext.prototype.getParameter;
    WebGLRenderingContext.prototype.getParameter = function(p) {
        if (p === 37445) return 'Intel Inc.';
        if (p === 37446) return 'Intel Iris OpenGL Engine';
        return _getParam.call(this, p);
    };
    Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
    Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
    window.chrome = { runtime: {}, loadTimes: function() {}, csi: function() {}, app: {} };
"""

async def _new_page():
    page = await _context.new_page()
    await page.add_init_script(_STEALTH_SCRIPT)
    return page

async def _goto(page, url: str, wait_until: str, timeout: int):
    try:
        await page.goto(url, wait_until=wait_until, timeout=timeout)
    except Exception as e:
        if "Timeout" in type(e).__name__ and wait_until == "networkidle":
            pass
        else:
            raise
    await asyncio.sleep(random.uniform(*_HUMAN_DELAY_RANGES["nav"]))

# ─────────────────────────────────────────────────────────────────────────────
# Shutdown
# ─────────────────────────────────────────────────────────────────────────────

def _shutdown_browser():
    if _browser_loop and _pw:
        async def _stop():
            if _browser:
                try:
                    await _browser.close()
                except Exception:
                    pass
            await _pw.stop()
        future = concurrent.futures.Future()
        async def _wrapper():
            try:
                future.set_result(await _stop())
            except Exception as e:
                future.set_exception(e)
        _browser_loop.call_soon_threadsafe(asyncio.ensure_future, _wrapper())
        try:
            future.result(timeout=5)
        except Exception:
            pass

_start_browser_thread()
atexit.register(_shutdown_browser)

# ─────────────────────────────────────────────────────────────────────────────
# Tools
# ─────────────────────────────────────────────────────────────────────────────

@tool
def browser_start() -> dict:
    """
    Launch Chrome and establish the CDP connection. Call this before any other
    browser tool. If Chrome is already running, connects immediately.
    If not, calls "etna browser start" to launch it and waits up to 15 seconds.

    Returns {"status": "ready", "engine": str} or {"error": str}.
    """
    async def _inner():
        global _pw, _browser, _context, _engine
        loop = asyncio.get_event_loop()

        already_running = await loop.run_in_executor(None, _cdp_reachable)
        if not already_running:
            await loop.run_in_executor(None, _request_chrome_start)
            if not await loop.run_in_executor(None, _poll_cdp_ready):
                return {"error": "Chrome did not become reachable within 15 seconds. Run 'etna browser start' manually on the host."}

        try:
            from patchright.async_api import async_playwright
            _engine = "patchright"
        except ImportError:
            from playwright.async_api import async_playwright
            _engine = "playwright"

        if _pw is None:
            _pw = await async_playwright().start()

        ws_url = await loop.run_in_executor(None, _get_ws_url)
        _browser = await _pw.chromium.connect_over_cdp(ws_url)
        _context = _browser.contexts[0] if _browser.contexts else await _browser.new_context()
        result = {"status": "ready", "engine": _engine}
        if already_running:
            result["note"] = "Chrome was already running"
        return result
    return _run(_inner())


@tool
def browser_stop() -> dict:
    """
    Gracefully close Chrome. Closes the browser and disconnects Playwright.
    The user's Chrome window will close. Call browser_start to reopen it.

    Returns {"status": "stopped", "engine": str}.
    """
    async def _inner():
        global _pw, _browser, _context
        try:
            if _browser:
                await _browser.close()
        except Exception:
            pass
        try:
            if _pw:
                await _pw.stop()
        except Exception:
            pass
        _browser = None
        _context = None
        _pw = None
        return {"status": "stopped", "engine": _engine}
    return _run(_inner())


@tool
def browser_list_tabs() -> dict:
    """
    List all currently open tabs with their CDP target_id, URL, and title.
    target_id is stable across navigations — use it with all other tools.

    Returns {"tabs": [{"target_id": str, "url": str, "title": str}], "engine": str}.
    """
    async def _inner():
        err = await _ensure_context()
        if err:
            return _CHROME_NOT_AVAILABLE
        tabs = []
        for page in _context.pages:
            try:
                tabs.append({
                    "target_id": await _get_target_id(page),
                    "url": page.url,
                    "title": await page.title(),
                })
            except Exception:
                pass
        return {"tabs": tabs, "engine": _engine}
    return _run(_inner())

@tool
def browser_open_tab(url: str) -> dict:
    """
    Open a new tab and navigate to a URL. Returns the CDP target_id.
    Call browser_start first if Chrome is not running.

    Returns {"target_id": str, "url": str, "engine": str}.
    """
    async def _inner():
        err = await _ensure_context()
        if err:
            return _CHROME_NOT_AVAILABLE
        try:
            page = await _new_page()
            await _goto(page, url, wait_until="domcontentloaded", timeout=30000)
            target_id = await _get_target_id(page)
            history = _load_history()
            if page.url not in history:
                history.append(page.url)
                _save_history(history)
            return {"target_id": target_id, "url": page.url, "engine": _engine}
        except Exception as e:
            return {"error": str(e), "engine": _engine}
    return _run(_inner())


@tool
def browser_close_tab(target_id: str) -> dict:
    """
    Close the specified tab.

    target_id: From browser_list_tabs or browser_open_tab.
    Returns {"closed": str, "engine": str}.
    """
    async def _inner():
        page, err = await _resolve_tab(target_id)
        if err:
            return err
        try:
            await page.close()
        except Exception:
            pass
        return {"closed": target_id, "engine": _engine}
    return _run(_inner())


@tool
def browser_navigate(target_id: str, url: str, wait_for: str = "networkidle", timeout: int = _default_timeout_ms()) -> dict:
    """
    Navigate an existing tab to a URL, preserving session and cookies.

    target_id: From browser_list_tabs or browser_open_tab.
    url:       Destination URL.
    wait_for:  "networkidle" (default), "load", "domcontentloaded", or "commit".
    timeout:   Milliseconds.

    Returns {"target_id": str, "url": str, "engine": str}.
    """
    async def _inner():
        page, err = await _resolve_tab(target_id)
        if err:
            return err
        try:
            await _goto(page, url, wait_until=wait_for, timeout=timeout)
            history = _load_history()
            if page.url not in history:
                history.append(page.url)
                _save_history(history)
            return {"target_id": target_id, "url": page.url, "engine": _engine}
        except Exception as e:
            return {"error": str(e), "target_id": target_id, "engine": _engine}
    return _run(_inner())

@tool
def browser_get_text(target_id: str, wait_for: str = "networkidle", timeout: int = _default_timeout_ms()) -> dict:
    """
    Return the visible plain text of the tab's current page.
    Prefer this over browser_get_page when you don't need raw HTML.

    target_id: From browser_list_tabs or browser_open_tab.
    wait_for:  "networkidle" (default), "load", "domcontentloaded", or "commit".
    timeout:   Milliseconds.

    Returns {"target_id": str, "url": str, "text": str, "engine": str}.
    """
    async def _inner():
        page, err = await _resolve_tab(target_id)
        if err:
            return err
        try:
            await page.wait_for_load_state(wait_for, timeout=timeout)
            text = await page.evaluate("() => document.body ? document.body.innerText : ''")
            history = _load_history()
            if page.url not in history:
                history.append(page.url)
                _save_history(history)
            return {"target_id": target_id, "url": page.url, "text": text, "engine": _engine}
        except Exception as e:
            return {"error": str(e), "target_id": target_id, "engine": _engine}
    return _run(_inner())


@tool
def browser_get_page(target_id: str, wait_for: str = "networkidle", timeout: int = _default_timeout_ms()) -> dict:
    """
    Return the raw HTML source of the tab's current page.
    Use when you need to inspect HTML structure, scrape data, or parse the DOM.
    Use browser_get_text when you only need readable content — it's cheaper.
    Use browser_scan_interactive when you need to find clickable/fillable elements.

    target_id: From browser_list_tabs or browser_open_tab.
    wait_for:  "networkidle" (default), "load", "domcontentloaded", or "commit".
    timeout:   Milliseconds.

    Returns {"target_id": str, "url": str, "html": str, "engine": str}.
    """
    async def _inner():
        page, err = await _resolve_tab(target_id)
        if err:
            return err
        try:
            await page.wait_for_load_state(wait_for, timeout=timeout)
            html = await page.content()
            history = _load_history()
            if page.url not in history:
                history.append(page.url)
                _save_history(history)
            return {"target_id": target_id, "url": page.url, "html": html, "engine": _engine}
        except Exception as e:
            return {"error": str(e), "target_id": target_id, "engine": _engine}
    return _run(_inner())

@tool
def browser_click(target_id: str, selector: str, wait_for: str = "networkidle", timeout: int = _default_timeout_ms()) -> dict:
    """
    Click an element and wait for the page to settle.

    target_id: From browser_list_tabs or browser_open_tab.
    selector:  CSS selector of the element to click.
    wait_for:  "networkidle" (default), "load", or "domcontentloaded".
    timeout:   Milliseconds.

    Returns {"target_id": str, "url": str, "clicked": str, "engine": str}.
    To see what changed after the click, follow up with browser_get_text or browser_scan_interactive.
    """
    async def _inner():
        page, err = await _resolve_tab(target_id)
        if err:
            return err
        try:
            await page.click(selector, timeout=timeout)
            await page.wait_for_load_state(wait_for, timeout=timeout)
            await asyncio.sleep(random.uniform(*_HUMAN_DELAY_RANGES["action"]))
            return {"target_id": target_id, "url": page.url, "clicked": selector, "engine": _engine}
        except Exception as e:
            return {"error": str(e), "target_id": target_id, "engine": _engine}
    return _run(_inner())


@tool
def browser_fill_and_submit(target_id: str, fields: str, submit_selector: str, wait_for: str = "networkidle", timeout: int = _default_timeout_ms()) -> dict:
    """
    Fill form fields and submit.

    target_id:       From browser_list_tabs or browser_open_tab.
    fields:          JSON mapping CSS selectors to values,
                     e.g. '{"#username": "me", "#password": "secret"}'.
    submit_selector: CSS selector of the submit button.
    wait_for:        "networkidle" (default), "load", or "domcontentloaded".
    timeout:         Milliseconds.

    Returns {"target_id": str, "url": str, "text": str, "engine": str}.
    """
    async def _inner():
        page, err = await _resolve_tab(target_id)
        if err:
            return err
        try:
            try:
                field_map = json.loads(fields)
            except Exception:
                raise ValueError(f"'fields' must be valid JSON, got: {fields}")
            for selector, value in field_map.items():
                await page.fill(selector, str(value), timeout=timeout)
            await page.click(submit_selector, timeout=timeout)
            await page.wait_for_load_state(wait_for, timeout=timeout)
            await asyncio.sleep(random.uniform(*_HUMAN_DELAY_RANGES["action"]))
            text = await page.evaluate("() => document.body ? document.body.innerText : ''")
            return {"target_id": target_id, "url": page.url, "text": text, "engine": _engine}
        except Exception as e:
            return {"error": str(e), "target_id": target_id, "engine": _engine}
    return _run(_inner())

@tool
def browser_screenshot(target_id: str, save_path: str = "/tmp/screenshot.png", return_base64: bool = False) -> dict:
    """
    Take a full-page screenshot of a tab.

    target_id:     From browser_list_tabs or browser_open_tab.
    save_path:     Path to write the PNG.
    return_base64: If true, include the PNG as base64 in the response.

    Returns {"target_id": str, "url": str, "saved_to": str, "engine": str, "png_base64"?: str}.
    """
    async def _inner():
        page, err = await _resolve_tab(target_id)
        if err:
            return err
        try:
            await page.screenshot(path=save_path, full_page=True)
            result = {"target_id": target_id, "url": page.url, "saved_to": save_path, "engine": _engine}
            if return_base64:
                try:
                    with open(save_path, "rb") as f:
                        result["png_base64"] = base64.b64encode(f.read()).decode("ascii")
                except Exception as e:
                    result["png_base64_error"] = str(e)
            return result
        except Exception as e:
            return {"error": str(e), "target_id": target_id, "engine": _engine}
    return _run(_inner())


@tool
def browser_evaluate(target_id: str, script: str, timeout: int = _default_timeout_ms()) -> dict:
    """
    Execute JavaScript in a tab and return the result.

    target_id: From browser_list_tabs or browser_open_tab.
    script:    JS expression or arrow function, e.g.:
               "document.title"
               "() => [...document.querySelectorAll('a')].map(a => a.href)"
    timeout:   Milliseconds.

    Returns {"target_id": str, "url": str, "result": any, "engine": str}.
    """
    async def _inner():
        page, err = await _resolve_tab(target_id)
        if err:
            return err
        try:
            result = await page.evaluate(script)
            return {"target_id": target_id, "url": page.url, "result": result, "engine": _engine}
        except Exception as e:
            return {"error": str(e), "target_id": target_id, "engine": _engine}
    return _run(_inner())

@tool
def browser_scan_interactive(target_id: str, wait_for: str = "networkidle", timeout: int = 5000) -> dict:
    """
    Scan the current page for visible, interactive elements — buttons, links, inputs,
    selects, textareas, and ARIA roles. Hidden elements are filtered out.

    Use this to see what's actionable on a page before clicking or filling anything.
    Much cheaper than browser_get_page. Good first call after any navigation.

    target_id: From browser_list_tabs or browser_open_tab.
    wait_for:  "networkidle" (default), "load", "domcontentloaded", or "commit".
    timeout:   Milliseconds.

    Returns {"target_id": str, "url": str, "elements": list, "engine": str}.
    """
    _JS = """() => {
        function siblingContext(el) {
            const parent = el.parentElement;
            if (!parent) return null;
            const parts = [];
            parent.childNodes.forEach(node => {
                if (node === el) return;
                const t = node.textContent && node.textContent.trim();
                if (t) parts.push(t);
            });
            const ctx = parts.join(' ').trim().replace(/\\s+/g, ' ');
            return ctx.length ? ctx.substring(0, 60) : null;
        }

        const els = document.querySelectorAll(
            'button, a, input, select, textarea, [role="button"], [role="tab"], [role="checkbox"], [role="radio"]'
        );
        const results = [];
        els.forEach(el => {
            const style = window.getComputedStyle(el);
            if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return;
            if (el.offsetWidth <= 0 || el.offsetHeight <= 0) return;
            const text = (el.textContent || el.getAttribute('aria-label') || '').trim();
            const value = el.value || '';
            results.push({
                tag:     el.tagName.toLowerCase(),
                id:      el.id || null,
                name:    el.name || null,
                type:    el.type || el.getAttribute('role') || el.tagName.toLowerCase(),
                text:    text.substring(0, 60),
                context: siblingContext(el),
                value:   value.length < 100 ? value : null,
            });
        });
        return results;
    }"""

    async def _inner():
        page, err = await _resolve_tab(target_id)
        if err:
            return err
        try:
            await page.wait_for_load_state(wait_for, timeout=timeout)
            elements = await page.evaluate(_JS)
            return {"target_id": target_id, "url": page.url, "elements": elements, "engine": _engine}
        except Exception as e:
            return {"error": str(e), "target_id": target_id, "engine": _engine}
    return _run(_inner())

@tool
def browser_get_element_context(target_id: str, selector: str, depth: int = 1) -> dict:
    """
    Get the local DOM context around an element to disambiguate it.
    Use this when browser_scan_interactive returns elements that are unclear
    without context — e.g. multiple buttons with text "+2" or "Submit".

    Pass the same selector string from the scan output.

    depth controls how much of the surrounding tree is returned:
      1 — parent element only (tag, attributes, direct text)
      2 — parent + all siblings (what's next to the element?)
      3 — parent + siblings + siblings' children (deepest; includes nested content)
    Each level includes everything from the prior level.
    Depth 3 is the maximum.

    target_id: From browser_list_tabs or browser_open_tab.
    selector:  CSS selector of the element to inspect.
    depth:     1, 2, or 3 (default 1).

    Returns {"target_id": str, "selector": str, "context": dict, "engine": str}.
    """
    _JS = """([selector, depth]) => {
        function elSummary(el, includeChildren) {
            if (!el) return null;
            const attrs = {};
            for (const a of el.attributes) attrs[a.name] = a.value;
            const out = {
                tag:   el.tagName.toLowerCase(),
                attrs: attrs,
                text:  (el.innerText || '').trim().substring(0, 80),
            };
            if (includeChildren) {
                out.children = Array.from(el.children).map(c => ({
                    tag:  c.tagName.toLowerCase(),
                    text: (c.innerText || '').trim().substring(0, 60),
                }));
            }
            return out;
        }

        const el = document.querySelector(selector);
        if (!el) return {error: 'Element not found: ' + selector};

        const parent = el.parentElement;
        if (!parent) return {error: 'Element has no parent'};

        const result = { parent: elSummary(parent, false) };

        if (depth >= 2) {
            result.siblings = Array.from(parent.children)
                .filter(c => c !== el)
                .map(c => elSummary(c, depth >= 3));
        }

        return result;
    }"""

    async def _inner():
        page, err = await _resolve_tab(target_id)
        if err:
            return err
        try:
            clamped = max(1, min(3, depth))
            context = await page.evaluate(_JS, [selector, clamped])
            return {"target_id": target_id, "selector": selector, "context": context, "engine": _engine}
        except Exception as e:
            return {"error": str(e), "target_id": target_id, "engine": _engine}
    return _run(_inner())

# ─────────────────────────────────────────────────────────────────────────────
# End of playwright_kit_v2.py
# ─────────────────────────────────────────────────────────────────────────────
