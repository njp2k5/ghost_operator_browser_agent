import asyncio
import base64
import functools
import logging
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from playwright.async_api import async_playwright, Browser, BrowserContext, Page, Playwright

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Windows fix: Playwright needs ProactorEventLoop (for subprocess transport)
# but uvicorn uses SelectorEventLoop.  We run ALL Playwright operations on a
# dedicated ProactorEventLoop in a background thread.
# ---------------------------------------------------------------------------

_pw_loop: asyncio.AbstractEventLoop | None = None
_pw_thread_id: int | None = None


def _start_pw_loop() -> None:
    global _pw_loop, _pw_thread_id
    _pw_loop = asyncio.ProactorEventLoop()
    _pw_thread_id = threading.current_thread().ident
    asyncio.set_event_loop(_pw_loop)
    _pw_loop.run_forever()


if sys.platform == "win32":
    _t = threading.Thread(target=_start_pw_loop, daemon=True, name="PlaywrightLoop")
    _t.start()
    while _pw_loop is None:          # wait until the loop is ready
        time.sleep(0.01)
    logger.info("Playwright ProactorEventLoop thread started.")


def _on_pw_loop(fn):
    """Decorator: dispatch an async function to the Playwright ProactorEventLoop."""
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        if _pw_loop is None:
            # Not on Windows — run directly
            return await fn(*args, **kwargs)
        if threading.current_thread().ident == _pw_thread_id:
            # Already on the Playwright thread (nested call) — run directly
            return await fn(*args, **kwargs)
        future = asyncio.run_coroutine_threadsafe(fn(*args, **kwargs), _pw_loop)
        return await asyncio.wrap_future(future)
    return wrapper

# ---------------------------------------------------------------------------
# In-memory store of active browser sessions  { token: BrowserSession }
# ---------------------------------------------------------------------------
_sessions: dict[str, "BrowserSession"] = {}


@dataclass
class BrowserSession:
    token: str
    page: Page
    context: BrowserContext
    browser: Browser
    pw: Playwright          # MUST be kept alive — GC will kill the browser if dropped
    current_step: int = 1
    action_done_event: asyncio.Event = field(default_factory=asyncio.Event)


# ---------------------------------------------------------------------------
# Session lifecycle
# ---------------------------------------------------------------------------

@_on_pw_loop
async def start_session(token: str, target_url: str | None) -> BrowserSession:
    """Launch a Chromium browser for this session and navigate to target_url."""
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
            "--disable-infobars",
            "--disable-extensions",
            "--disable-default-apps",
            "--disable-component-extensions-with-background-pages",
            "--disable-background-networking",
            "--disable-sync",
            "--metrics-recording-only",
            "--no-first-run",
            "--window-size=1280,800",
            "--lang=en-US,en",
            "--disable-features=IsolateOrigins,site-per-process",
        ],
    )
    context = await browser.new_context(
        viewport={"width": 1280, "height": 800},
        screen={"width": 1920, "height": 1080},
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        locale="en-US",
        timezone_id="Asia/Kolkata",
        extra_http_headers={
            "Accept-Language": "en-US,en;q=0.9",
            "sec-ch-ua": '"Chromium";v="131", "Not_A Brand";v="24"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
        },
        ignore_https_errors=True,
        java_script_enabled=True,
    )

    # ---- Comprehensive stealth injection ----
    await context.add_init_script("""
        // 1. Remove navigator.webdriver flag
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        delete navigator.__proto__.webdriver;

        // 2. Fake plugins array (Chrome normally has 5)
        Object.defineProperty(navigator, 'plugins', {
            get: () => {
                const arr = [
                    { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer',
                      description: 'Portable Document Format',
                      length: 1, item: () => null, namedItem: () => null },
                    { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai',
                      description: '', length: 1, item: () => null, namedItem: () => null },
                    { name: 'Native Client', filename: 'internal-nacl-plugin',
                      description: '', length: 2, item: () => null, namedItem: () => null },
                ];
                arr.item = (i) => arr[i] || null;
                arr.namedItem = (n) => arr.find(p => p.name === n) || null;
                arr.refresh = () => {};
                return arr;
            }
        });

        // 3. Languages
        Object.defineProperty(navigator, 'languages', {
            get: () => ['en-US', 'en']
        });

        // 4. Platform
        Object.defineProperty(navigator, 'platform', { get: () => 'Win32' });

        // 5. Hardware concurrency (real CPUs have 4-16)
        Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });

        // 6. Device memory
        Object.defineProperty(navigator, 'deviceMemory', { get: () => 8 });

        // 7. Max touch points (0 for desktop)
        Object.defineProperty(navigator, 'maxTouchPoints', { get: () => 0 });

        // 8. Connection
        if (navigator.connection) {
            Object.defineProperty(navigator.connection, 'rtt', { get: () => 50 });
        }

        // 9. Chrome object
        window.chrome = {
            app: { isInstalled: false, InstallState: { INSTALLED: 'installed', NOT_INSTALLED: 'not_installed' }, RunningState: { RUNNING: 'running', CANNOT_RUN: 'cannot_run', READY_TO_RUN: 'ready_to_run' } },
            runtime: { OnInstalledReason: { INSTALL: 'install', UPDATE: 'update' }, PlatformOs: { WIN: 'win', MAC: 'mac', LINUX: 'linux' }, RequestUpdateCheckStatus: { THROTTLED: 'throttled', NO_UPDATE: 'no_update', UPDATE_AVAILABLE: 'update_available' }, connect: () => {}, sendMessage: () => {} },
            csi: () => ({}),
            loadTimes: () => ({}),
        };

        // 10. Permissions API — make "notifications" return "denied" like a real browser
        const origQuery = window.Notification && Notification.permission;
        if (navigator.permissions) {
            const origPermQuery = navigator.permissions.query;
            navigator.permissions.query = (params) => {
                if (params.name === 'notifications') {
                    return Promise.resolve({ state: Notification.permission || 'denied' });
                }
                return origPermQuery.call(navigator.permissions, params);
            };
        }

        // 11. WebGL — spoof vendor/renderer
        const getParameterOrig = WebGLRenderingContext.prototype.getParameter;
        WebGLRenderingContext.prototype.getParameter = function(param) {
            if (param === 37445) return 'Google Inc. (Intel)';         // UNMASKED_VENDOR_WEBGL
            if (param === 37446) return 'ANGLE (Intel, Intel(R) UHD Graphics 630, OpenGL 4.5)'; // UNMASKED_RENDERER_WEBGL
            return getParameterOrig.call(this, param);
        };
        const getParameterOrig2 = WebGL2RenderingContext.prototype.getParameter;
        WebGL2RenderingContext.prototype.getParameter = function(param) {
            if (param === 37445) return 'Google Inc. (Intel)';
            if (param === 37446) return 'ANGLE (Intel, Intel(R) UHD Graphics 630, OpenGL 4.5)';
            return getParameterOrig2.call(this, param);
        };

        // 12. Prevent iframe-based detection of Playwright
        const origAttachShadow = HTMLElement.prototype.attachShadow;
        HTMLElement.prototype.attachShadow = function(opts) {
            return origAttachShadow.call(this, { ...opts, mode: 'open' });
        };

        // 13. Spoof screen properties
        Object.defineProperty(screen, 'availWidth', { get: () => 1920 });
        Object.defineProperty(screen, 'availHeight', { get: () => 1040 });
        Object.defineProperty(screen, 'colorDepth', { get: () => 24 });
        Object.defineProperty(screen, 'pixelDepth', { get: () => 24 });

        // 14. Fake media devices
        if (navigator.mediaDevices && navigator.mediaDevices.enumerateDevices) {
            navigator.mediaDevices.enumerateDevices = () => Promise.resolve([
                { deviceId: 'default', kind: 'audioinput', label: '', groupId: 'default' },
                { deviceId: 'default', kind: 'audiooutput', label: '', groupId: 'default' },
                { deviceId: 'default', kind: 'videoinput', label: '', groupId: 'default' },
            ]);
        }
    """)

    page = await context.new_page()

    # Human-like: add slight random mouse movement after page load
    if target_url:
        try:
            await page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(2500)
            # Simulate subtle human mouse movement
            await page.mouse.move(400, 300)
            await page.wait_for_timeout(300)
            await page.mouse.move(640, 400)
            await page.wait_for_timeout(200)
        except Exception as e:
            logger.warning(f"[{token}] Could not navigate to {target_url}: {e}")

    session = BrowserSession(
        token=token, page=page, context=context, browser=browser, pw=pw,
    )
    _sessions[token] = session
    logger.info(f"[{token}] Browser session started.")
    return session


@_on_pw_loop
async def end_session(token: str) -> None:
    """Close browser and clean up session."""
    session = _sessions.pop(token, None)
    if session:
        try:
            await session.context.close()
            await session.browser.close()
            await session.pw.stop()
        except Exception as e:
            logger.warning(f"[{token}] Error closing browser: {e}")
        logger.info(f"[{token}] Browser session ended.")


def get_session(token: str) -> Optional[BrowserSession]:
    return _sessions.get(token)


# ---------------------------------------------------------------------------
# Smart element finder — label-based first, CSS fallback
# ---------------------------------------------------------------------------

@_on_pw_loop
async def _find_element(session: BrowserSession, selector: str):
    """
    Find an element using multiple strategies. Returns a Playwright Locator or CSS string.
    The selector from the LLM is now typically a LABEL TEXT (e.g. "Name", "State").
    
    Strategy order:
    1. Try as CSS selector directly (backward compat)
    2. Try Playwright get_by_label(selector) — matches <label> associations
    3. Try finding input/select/textarea near visible text
    4. Try placeholder text
    5. Generic fallback
    """
    page = session.page

    # 1. Try as CSS selector
    try:
        el = await page.query_selector(selector)
        if el:
            logger.info(f"[{session.token}] CSS selector '{selector}' matched directly")
            return selector
    except Exception:
        pass

    # 2. Try get_by_label (handles <label for="..."> and aria-label)
    try:
        locator = page.get_by_label(selector, exact=False)
        count = await locator.count()
        if count > 0:
            # Verify it's visible
            if await locator.first.is_visible():
                logger.info(f"[{session.token}] get_by_label('{selector}') matched ({count} elements)")
                return ("label", selector)
    except Exception:
        pass

    # 3. Try by placeholder text (more precise than label scan for multi-input rows)
    try:
        locator = page.get_by_placeholder(selector, exact=False)
        if await locator.count() > 0 and await locator.first.is_visible():
            logger.info(f"[{session.token}] get_by_placeholder('{selector}') matched")
            return ("placeholder", selector)
    except Exception:
        pass

    # 4. Try finding a label element containing the text, then get associated input
    try:
        found = await page.evaluate("""(labelText) => {
            const lt = labelText.toLowerCase();
            const labels = document.querySelectorAll('label');
            for (const lbl of labels) {
                const text = lbl.textContent.replace(/[\\s*]+/g, ' ').trim().toLowerCase();
                if (!text.includes(lt) && !lt.includes(text)) continue;

                // If label has 'for' attribute, return the target ID
                if (lbl.htmlFor) {
                    const target = document.getElementById(lbl.htmlFor);
                    if (target) return '#' + lbl.htmlFor;
                }

                // Walk UP to a row-level container, then search ALL descendants.
                // Many forms put label in one column-div and input in a sibling column-div.
                const rowSelectors = '.row, .form-group, .form-row, [class*="row"], [class*="form-group"], form, fieldset';
                let container = lbl.closest(rowSelectors);
                if (!container) {
                    // Fallback: walk up 3 levels
                    container = lbl.parentElement;
                    if (container) container = container.parentElement || container;
                    if (container) container = container.parentElement || container;
                }
                if (container) {
                    const input = container.querySelector('input:not([type="hidden"]):not([type="radio"]):not([type="checkbox"]), select, textarea');
                    if (input && input.id) return '#' + input.id;
                    if (input && input.name) return '[name="' + input.name + '"]';
                }
            }
            return null;
        }""", selector)
        if found:
            el = await page.query_selector(found)
            if el:
                logger.info(f"[{session.token}] Label-text scan for '{selector}' found: {found}")
                return found
    except Exception as e:
        logger.debug(f"[{session.token}] Label scan error: {e}")

    # 5. Try by visible text (for buttons, links)
    try:
        locator = page.get_by_role("button", name=selector, exact=False)
        if await locator.count() > 0:
            logger.info(f"[{session.token}] get_by_role(button, '{selector}') matched")
            return ("role_button", selector)
    except Exception:
        pass

    try:
        locator = page.get_by_role("link", name=selector, exact=False)
        if await locator.count() > 0:
            logger.info(f"[{session.token}] get_by_role(link, '{selector}') matched")
            return ("role_link", selector)
    except Exception:
        pass

    logger.warning(f"[{session.token}] No element found for '{selector}' — returning as-is")
    return selector


@_on_pw_loop
async def element_exists(session: BrowserSession, selector: str) -> bool:
    """
    Check if the selector resolves to an element that is truly visible and
    reachable on the current page.  Uses bounding_box() which is more reliable
    than is_visible() — it catches React/SPA components that are in the DOM
    but hidden behind transitions (zero dimensions or wildly off-screen).
    """
    page = session.page

    def _bbox_ok(bbox) -> bool:
        """Return True only if element has positive size and is on-screen."""
        if not bbox:
            return False
        # Reject zero-size elements (React hidden panels)
        if bbox["width"] <= 0 or bbox["height"] <= 0:
            return False
        # Reject elements positioned far off-screen (e.g. left: -9999px tricks)
        if bbox["x"] < -50 or bbox["y"] < -50:
            return False
        return True

    try:
        loc = page.get_by_label(selector, exact=False)
        if await loc.count() > 0:
            bbox = await loc.first.bounding_box()
            if _bbox_ok(bbox):
                return True
    except Exception:
        pass
    try:
        loc = page.get_by_placeholder(selector, exact=False)
        if await loc.count() > 0:
            bbox = await loc.first.bounding_box()
            if _bbox_ok(bbox):
                return True
    except Exception:
        pass
    try:
        loc = page.get_by_role("button", name=selector, exact=False)
        if await loc.count() > 0:
            bbox = await loc.first.bounding_box()
            if _bbox_ok(bbox):
                return True
    except Exception:
        pass
    try:
        loc = page.get_by_role("link", name=selector, exact=False)
        if await loc.count() > 0:
            bbox = await loc.first.bounding_box()
            if _bbox_ok(bbox):
                return True
    except Exception:
        pass
    # JS scan: look for matching label/placeholder and verify bounding rect
    try:
        found = await page.evaluate("""(labelText) => {
            const lt = labelText.toLowerCase();

            function inViewport(el) {
                const r = el.getBoundingClientRect();
                if (r.width <= 0 || r.height <= 0) return false;
                if (r.x < -50 || r.y < -50)        return false;
                const style = window.getComputedStyle(el);
                if (style.display === 'none' || style.visibility === 'hidden') return false;
                // Walk up to check parent visibility
                let parent = el.parentElement;
                while (parent && parent !== document.body) {
                    const ps = window.getComputedStyle(parent);
                    if (ps.display === 'none' || ps.visibility === 'hidden') return false;
                    parent = parent.parentElement;
                }
                return true;
            }

            // Check labels
            for (const lbl of document.querySelectorAll('label')) {
                const text = lbl.textContent.replace(/[\\s*]+/g, ' ').trim().toLowerCase();
                if (!text.includes(lt) && !lt.includes(text)) continue;
                // Find associated input — direct association first
                const input = lbl.control ||
                              (lbl.htmlFor && document.getElementById(lbl.htmlFor)) ||
                              lbl.querySelector('input, textarea, select');
                if (input && inViewport(input)) return true;
                // Walk up to row-level container and search all descendants
                const rowSel = '.row, .form-group, .form-row, [class*="row"], [class*="form-group"], form, fieldset';
                let container = lbl.closest(rowSel);
                if (!container) {
                    container = lbl.parentElement;
                    if (container) container = container.parentElement || container;
                    if (container) container = container.parentElement || container;
                }
                if (container) {
                    const sibInput = container.querySelector('input:not([type="hidden"]), textarea, select');
                    if (sibInput && inViewport(sibInput)) return true;
                }
                // React-Select: check for custom dropdown containers near this label
                let p = lbl.parentElement;
                for (let d = 0; d < 4 && p; d++) {
                    const rs = p.querySelector('[class*="select__control"], [class*="-control"][class*="css-"]');
                    if (rs && inViewport(rs)) return true;
                    p = p.parentElement;
                }
                // Custom checkboxes/radios: the label itself is a valid clickable target
                if (inViewport(lbl)) return true;
            }
            // Check placeholders
            for (const inp of document.querySelectorAll('input, textarea, select')) {
                if (inp.placeholder && inp.placeholder.toLowerCase().includes(lt)) {
                    if (inViewport(inp)) return true;
                }
            }
            // Check buttons / links
            for (const btn of document.querySelectorAll('button, [role="button"], a')) {
                const text = btn.textContent.trim().toLowerCase();
                if (text.includes(lt) && inViewport(btn)) return true;
            }
            return false;
        }""", selector)
        if found:
            return True
    except Exception:
        pass
    return False


@_on_pw_loop
async def scan_page_fields(session: BrowserSession) -> list[dict]:
    """Scan the current page and return only truly visible interactive form fields."""
    page = session.page
    try:
        fields = await page.evaluate("""() => {
            const results = [];
            const seen = new Set();

            function inViewport(el) {
                const r = el.getBoundingClientRect();
                if (r.width <= 0 || r.height <= 0) return false;
                if (r.x < -50 || r.y < -50)        return false;
                const style = window.getComputedStyle(el);
                if (style.display === 'none' || style.visibility === 'hidden') return false;
                let parent = el.parentElement;
                while (parent && parent !== document.body) {
                    const ps = window.getComputedStyle(parent);
                    if (ps.display === 'none' || ps.visibility === 'hidden') return false;
                    parent = parent.parentElement;
                }
                return true;
            }

            // Scan all visible inputs, textareas, selects
            const elements = document.querySelectorAll('input, textarea, select');
            for (const el of elements) {
                if (el.type === 'hidden') continue;
                if (!inViewport(el)) continue;

                let label = '';
                let fieldType = 'fill';

                // Get label text
                if (el.id) {
                    const lbl = document.querySelector('label[for="' + el.id + '"]');
                    if (lbl) label = lbl.textContent.replace(/[\\s*]+/g, ' ').trim();
                }
                if (!label) {
                    const parentLabel = el.closest('label');
                    if (parentLabel) label = parentLabel.textContent.replace(/[\\s*]+/g, ' ').trim();
                }
                if (!label && el.placeholder) label = el.placeholder;
                if (!label && el.name) label = el.name;
                if (!label && el.getAttribute('aria-label')) label = el.getAttribute('aria-label');

                // Determine type
                if (el.tagName === 'SELECT') fieldType = 'select';
                else if (el.type === 'radio' || el.type === 'checkbox') fieldType = 'select';
                else if (el.type === 'submit') fieldType = 'click';
                else fieldType = 'fill';

                // Determine current value (for detecting already-filled fields)
                let fieldValue = '';
                if (el.type === 'radio') {
                    // For radio groups: check if ANY radio with same name is checked
                    const gn = el.name;
                    if (gn) {
                        const chk = document.querySelector('input[name="' + gn + '"]:checked');
                        fieldValue = chk ? chk.value : '';
                    } else {
                        fieldValue = el.checked ? 'checked' : '';
                    }
                } else if (el.type === 'checkbox') {
                    fieldValue = el.checked ? 'checked' : '';
                } else if (el.tagName === 'SELECT') {
                    fieldValue = el.selectedIndex > 0 ? el.options[el.selectedIndex].text : '';
                } else {
                    fieldValue = (el.value || '').trim();
                }

                // Skip duplicates
                const key = label + '|' + fieldType;
                if (seen.has(key) || !label) continue;
                seen.add(key);

                results.push({
                    label: label,
                    type: fieldType,
                    tag: el.tagName.toLowerCase(),
                    inputType: el.type || '',
                    name: el.name || '',
                    id: el.id || '',
                    value: fieldValue
                });
            }
            
            // Also scan buttons
            const buttons = document.querySelectorAll('button, [role="button"], input[type="submit"]');
            for (const btn of buttons) {
                if (btn.offsetParent === null) continue;
                const text = btn.textContent.trim() || btn.value || btn.getAttribute('aria-label') || '';
                if (!text || text.length > 50) continue;
                const key = text + '|click';
                if (seen.has(key)) continue;
                seen.add(key);
                results.push({ label: text, type: 'click', tag: btn.tagName.toLowerCase(), inputType: '', name: '', id: btn.id || '' });
            }

            // Scan React-Select / custom dropdown containers
            const rsControls = document.querySelectorAll('[class*="select__control"], [class*="-control"][class*="css-"]');
            for (const ctrl of rsControls) {
                if (!inViewport(ctrl)) continue;
                // Try to get label from nearby <label>
                let label = '';
                let searchNode = ctrl.parentElement;
                for (let d = 0; d < 5 && searchNode && !label; d++) {
                    const lbl = searchNode.querySelector('label');
                    if (lbl) label = lbl.textContent.replace(/[\\s*]+/g, ' ').trim();
                    searchNode = searchNode.parentElement;
                }
                // Try placeholder text inside the react-select
                if (!label) {
                    const ph = ctrl.querySelector('[class*="placeholder"]');
                    if (ph) label = ph.textContent.trim();
                }
                if (!label) continue;
                const key = label + '|select';
                if (seen.has(key)) continue;
                seen.add(key);
                results.push({ label: label, type: 'select', tag: 'div', inputType: 'react-select', name: '', id: '' });
            }
            
            return results;
        }""")
        logger.info(f"[{session.token}] Page scan found {len(fields)} fields: {[f['label'] for f in fields]}")
        return fields
    except Exception as e:
        logger.warning(f"[{session.token}] scan_page_fields failed: {e}")
        return []


@_on_pw_loop
async def wait_for_page_stable(session: BrowserSession, timeout_ms: int = 5000):
    """Wait for the page to be stable after a navigation or click."""
    page = session.page
    try:
        # Wait for network to settle
        await page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
    except Exception:
        pass
    try:
        await page.wait_for_load_state("networkidle", timeout=3000)
    except Exception:
        pass
    # Extra small wait for JS rendering
    await page.wait_for_timeout(500)


@_on_pw_loop
async def _resolve_locator(session: BrowserSession, resolved):
    """Convert a resolved selector (from _find_element) into a Playwright Locator."""
    page = session.page
    if isinstance(resolved, tuple):
        strategy, value = resolved
        if strategy == "label":
            return page.get_by_label(value, exact=False).first
        elif strategy == "placeholder":
            return page.get_by_placeholder(value, exact=False).first
        elif strategy == "role_button":
            return page.get_by_role("button", name=value, exact=False).first
        elif strategy == "role_link":
            return page.get_by_role("link", name=value, exact=False).first
    # CSS string
    return page.locator(resolved).first


# ---------------------------------------------------------------------------
# Browser actions
# ---------------------------------------------------------------------------

@_on_pw_loop
async def take_screenshot(session: BrowserSession) -> str:
    """Take a screenshot and return it as a base64-encoded JPEG string (faster, smaller than PNG)."""
    try:
        if session.page.is_closed():
            return ""
        jpg_bytes = await session.page.screenshot(type="jpeg", quality=70, full_page=False)
        return base64.b64encode(jpg_bytes).decode("utf-8")
    except Exception as e:
        # Suppress noisy "page closed" warnings during concurrent operations
        err_msg = str(e)
        if "closed" not in err_msg.lower():
            logger.warning(f"[{session.token}] Screenshot failed: {e}")
        return ""


@_on_pw_loop
async def highlight_element(session: BrowserSession, selector: str) -> bool:
    """Inject a glowing highlight border around the target element."""
    try:
        resolved = await _find_element(session, selector)
        locator = await _resolve_locator(session, resolved)
        
        # Scroll into view
        try:
            await locator.scroll_into_view_if_needed(timeout=3000)
        except Exception:
            pass

        # Get bounding box and highlight via JS
        try:
            bbox = await locator.bounding_box()
            if bbox:
                await session.page.evaluate("""(box) => {
                    // Remove previous highlights
                    document.querySelectorAll('.__funclink_highlight').forEach(el => el.remove());
                    
                    const overlay = document.createElement('div');
                    overlay.className = '__funclink_highlight';
                    overlay.style.cssText = `
                        position: fixed;
                        left: ${box.x - 4}px; top: ${box.y - 4}px;
                        width: ${box.width + 8}px; height: ${box.height + 8}px;
                        border: 3px solid #22d3ee;
                        box-shadow: 0 0 0 4px rgba(34,211,238,0.35), 0 0 15px rgba(34,211,238,0.3);
                        border-radius: 4px;
                        pointer-events: none;
                        z-index: 99999;
                        transition: all 0.2s;
                    `;
                    document.body.appendChild(overlay);
                }""", bbox)
                return True
        except Exception:
            pass

        # Fallback: try CSS-based highlighting if locator gave a CSS string
        if isinstance(resolved, str):
            await session.page.evaluate("""(selector) => {
                document.querySelectorAll('.__funclink_highlight_el').forEach(el => {
                    el.classList.remove('__funclink_highlight_el');
                    el.style.removeProperty('outline');
                    el.style.removeProperty('box-shadow');
                });
                const el = document.querySelector(selector);
                if (!el) return false;
                el.classList.add('__funclink_highlight_el');
                el.style.outline = '3px solid #22d3ee';
                el.style.boxShadow = '0 0 0 4px rgba(34,211,238,0.35)';
                el.scrollIntoView({ behavior: 'smooth', block: 'center' });
                return true;
            }""", resolved)
        return True
    except Exception as e:
        logger.warning(f"[{session.token}] highlight_element failed for '{selector}': {e}")
        return False


@_on_pw_loop
async def prefill_input(session: BrowserSession, selector: str, value: str) -> bool:
    """Click an input field, clear it, and type a value with human-like delay."""
    try:
        resolved = await _find_element(session, selector)
        locator = await _resolve_locator(session, resolved)
        
        try:
            await locator.scroll_into_view_if_needed(timeout=3000)
        except Exception:
            pass

        # Click, clear, type
        await locator.click(timeout=5000)
        await session.page.wait_for_timeout(150)
        await session.page.keyboard.press("Control+a")
        await session.page.keyboard.press("Backspace")
        await session.page.wait_for_timeout(100)
        await session.page.keyboard.type(value, delay=60)

        # Handle date pickers — set value via JS then close popup
        try:
            is_datepicker = await session.page.evaluate("""() => {
                const el = document.activeElement;
                if (!el) return false;
                return !!el.closest('.react-datepicker-wrapper, .react-datepicker__input-container, [class*="datepicker"]');
            }""")
            if is_datepicker:
                # react-datepicker overrides typed text; set via React value setter
                await session.page.evaluate("""(val) => {
                    const el = document.activeElement;
                    if (!el) return;
                    const nativeSet = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
                    nativeSet.call(el, val);
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                }""", value)
                await session.page.keyboard.press("Escape")
                await session.page.wait_for_timeout(200)
                logger.info(f"[{session.token}] Set date picker value for '{selector}'")
        except Exception:
            pass

        # Handle autocomplete / tag inputs — press Enter to confirm selection
        try:
            is_autocomplete = await session.page.evaluate("""() => {
                const el = document.activeElement;
                if (!el) return false;
                return !!el.closest('[class*="auto-complete"], [class*="autocomplete"], [class*="select__input"], [class*="tags-input"]');
            }""")
            if is_autocomplete:
                await session.page.wait_for_timeout(500)
                await session.page.keyboard.press("Enter")
                await session.page.wait_for_timeout(200)
                logger.info(f"[{session.token}] Confirmed autocomplete for '{selector}'")
        except Exception:
            pass

        logger.info(f"[{session.token}] Typed '{value}' into '{selector}'")
        return True
    except Exception as e:
        logger.warning(f"[{session.token}] prefill_input failed for '{selector}': {e}")
        # Fallback: try fill()
        try:
            resolved = await _find_element(session, selector)
            locator = await _resolve_locator(session, resolved)
            await locator.fill(value, timeout=5000)
            return True
        except Exception:
            pass
        return False


@_on_pw_loop
async def select_option(session: BrowserSession, selector: str, value: str) -> bool:
    """Select an option in a <select> dropdown or click a radio button, by label text."""
    page = session.page

    # Strategy 1: Find a <select> element by label and select the option
    try:
        resolved = await _find_element(session, selector)
        locator = await _resolve_locator(session, resolved)
        tag = await locator.evaluate("el => el.tagName.toLowerCase()")
        
        if tag == "select":
            # Use select_option with label matching
            await locator.select_option(label=value, timeout=5000)
            logger.info(f"[{session.token}] Selected '{value}' in dropdown '{selector}'")
            return True
    except Exception as e:
        logger.debug(f"[{session.token}] select dropdown attempt failed: {e}")

    # Strategy 2: Radio buttons / checkboxes — click LABEL (handles hidden custom inputs)
    try:
        clicked = await page.evaluate("""(args) => {
            const [fieldLabel, optionValue] = args;
            const ov = optionValue.toLowerCase().trim();

            const radios = document.querySelectorAll('input[type="radio"], input[type="checkbox"]');
            for (const radio of radios) {
                let matched = false;
                let lbl = null;

                // Check associated label
                if (radio.id) {
                    lbl = document.querySelector('label[for="' + radio.id + '"]');
                    if (lbl && lbl.textContent.trim().toLowerCase().includes(ov)) matched = true;
                }
                // Check value attribute
                if (!matched && radio.value.toLowerCase().trim() === ov) matched = true;
                // Check next sibling text
                if (!matched) {
                    const next = radio.nextSibling;
                    if (next && next.textContent && next.textContent.trim().toLowerCase().includes(ov)) matched = true;
                }
                // Check parent label
                if (!matched) {
                    const parentLabel = radio.closest('label');
                    if (parentLabel && parentLabel.textContent.trim().toLowerCase().includes(ov)) {
                        lbl = lbl || parentLabel;
                        matched = true;
                    }
                }

                if (matched) {
                    // Always prefer clicking the LABEL (works even when input is hidden by CSS)
                    if (lbl) { lbl.click(); return true; }
                    if (radio.id) {
                        const fallback = document.querySelector('label[for="' + radio.id + '"]');
                        if (fallback) { fallback.click(); return true; }
                    }
                    radio.click();
                    return true;
                }
            }
            return false;
        }""", [selector, value])
        if clicked:
            logger.info(f"[{session.token}] Clicked radio/checkbox '{value}' for '{selector}'")
            return True
    except Exception as e:
        logger.debug(f"[{session.token}] radio click attempt failed: {e}")

    # Strategy 3: React-Select / custom dropdown — click container, type value, Enter
    try:
        rs_found = await page.evaluate("""(args) => {
            const [fieldLabel] = args;
            const fl = fieldLabel.toLowerCase().trim();

            // Find label matching the field name, look for react-select nearby
            for (const lbl of document.querySelectorAll('label, [class*="label"]')) {
                const text = (lbl.textContent || '').replace(/[\\s*]+/g, ' ').trim().toLowerCase();
                if (!text.includes(fl)) continue;
                let node = lbl.parentElement;
                for (let d = 0; d < 5 && node; d++) {
                    const ctrl = node.querySelector(
                        '[class*="select__control"], [class*="-control"][class*="css-"], ' +
                        '[class*="select__value-container"]'
                    );
                    if (ctrl) { ctrl.click(); return true; }
                    node = node.parentElement;
                }
            }

            // Try finding by placeholder text like "Select State"
            for (const el of document.querySelectorAll('[class*="placeholder"]')) {
                const t = (el.textContent || '').toLowerCase();
                if (t.includes(fl) || t.includes('select ' + fl)) {
                    const ctrl = el.closest('[class*="control"]');
                    if (ctrl) { ctrl.click(); return true; }
                    el.click(); return true;
                }
            }
            return false;
        }""", [selector])

        if rs_found:
            await page.wait_for_timeout(300)
            await page.keyboard.type(value, delay=50)
            await page.wait_for_timeout(500)
            await page.keyboard.press("Enter")
            await page.wait_for_timeout(300)
            logger.info(f"[{session.token}] React-Select: selected '{value}' for '{selector}'")
            return True
    except Exception as e:
        logger.debug(f"[{session.token}] React-Select attempt failed: {e}")

    # Strategy 4: Try clicking text that matches the option value (general fallback)
    try:
        option_locator = page.get_by_text(value, exact=False).first
        if await option_locator.is_visible():
            await option_locator.click(timeout=3000)
            logger.info(f"[{session.token}] Clicked text '{value}' for '{selector}'")
            return True
    except Exception as e:
        logger.debug(f"[{session.token}] text-click attempt failed: {e}")

    logger.warning(f"[{session.token}] select_option failed for '{selector}' = '{value}'")
    return False


@_on_pw_loop
async def click_element(session: BrowserSession, selector: str) -> bool:
    """Click an element found by label, role, or CSS selector."""
    try:
        resolved = await _find_element(session, selector)
        locator = await _resolve_locator(session, resolved)
        
        try:
            await locator.scroll_into_view_if_needed(timeout=3000)
        except Exception:
            pass
        
        # Human-like: hover before clicking
        try:
            bbox = await locator.bounding_box()
            if bbox:
                cx = bbox["x"] + bbox["width"] / 2
                cy = bbox["y"] + bbox["height"] / 2
                await session.page.mouse.move(cx - 20, cy - 10)
                await asyncio.sleep(0.1)
                await session.page.mouse.move(cx, cy)
                await asyncio.sleep(0.1)
        except Exception:
            pass

        await locator.click(timeout=5000)
        logger.info(f"[{session.token}] Clicked '{selector}'")
        return True
    except Exception as e:
        logger.warning(f"[{session.token}] click_element failed for '{selector}': {e}")
        return False


@_on_pw_loop
async def navigate_to(session: BrowserSession, url: str) -> bool:
    """Navigate the browser to a new URL."""
    try:
        await session.page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await session.page.wait_for_timeout(1500)
        return True
    except Exception as e:
        logger.warning(f"[{session.token}] navigate_to failed for '{url}': {e}")
        return False


# ---------------------------------------------------------------------------
# Booking.com — specialized flow handlers
# ---------------------------------------------------------------------------

def is_booking_com(url: str) -> bool:
    """Check if the URL is a Booking.com page."""
    return "booking.com" in (url or "").lower()


@_on_pw_loop
async def booking_dismiss_overlays(session: BrowserSession) -> None:
    """Close the 'Sign in' banner, cookie consent, and any modal that
    intercepts pointer events on Booking.com.
    Uses CSS visibility:hidden + pointer-events:none rather than DOM removal
    to avoid triggering Booking.com's mutation observers / reload logic."""
    page = session.page
    try:
        # Hide overlays via CSS (safer than removing — avoids mutation observer triggers)
        await page.evaluate("""() => {
            // Hide login/sign-in overlay & backdrop
            document.querySelectorAll(
                '[role="dialog"], [aria-modal="true"], div.dc7e768484, div.bbe73dce14'
            ).forEach(el => {
                el.style.cssText = 'display:none !important; visibility:hidden !important; pointer-events:none !important;';
            });
            // Also hide any fixed-position overlays at top of z-stack
            document.querySelectorAll('[class*="genius"], [class*="signin"], [class*="login-banner"]').forEach(el => {
                el.style.cssText = 'display:none !important;';
            });
        }""")
        logger.info(f"[{session.token}] booking: hid overlays via CSS")
    except Exception:
        pass

    # Also try clicking common close / dismiss / accept buttons
    for sel in [
        'button[aria-label="Dismiss sign-in info."]',
        'button[aria-label="Dismiss sign in information."]',
        '#onetrust-accept-btn-handler',   # cookie consent
    ]:
        try:
            loc = page.locator(sel).first
            if await loc.count() > 0 and await loc.is_visible():
                await loc.click(timeout=2000, force=True)
                await page.wait_for_timeout(300)
                logger.info(f"[{session.token}] booking: clicked dismiss: {sel}")
        except Exception:
            pass


@_on_pw_loop
async def booking_highlight_step(session: BrowserSession, step_selector: str) -> bool:
    """Highlight a Booking.com search form element using data-testid selectors."""
    page = session.page

    SELECTOR_MAP = {
        "destination":  'input[name="ss"]',
        "dates":        '[data-testid="searchbox-dates-container"]',
        "check-in":     '[data-testid="date-display-field-start"]',
        "check-out":    '[data-testid="date-display-field-end"]',
        "guests":       '[data-testid="occupancy-config"]',
        "search":       'button[type="submit"]',
        "results":      'body',  # full-page highlight for results
    }

    css = SELECTOR_MAP.get(step_selector.lower())
    if not css:
        logger.warning(f"[{session.token}] booking_highlight: unknown selector '{step_selector}'")
        return False

    try:
        el = page.locator(css).first
        bbox = await el.bounding_box()
        if bbox:
            await el.scroll_into_view_if_needed(timeout=3000)
            await page.evaluate("""(b) => {
                document.querySelectorAll('.__fl_hl').forEach(e => e.remove());
                const d = document.createElement('div');
                d.className = '__fl_hl';
                d.style.cssText = `position:fixed;left:${b.x-4}px;top:${b.y-4}px;width:${b.width+8}px;height:${b.height+8}px;border:3px solid #22d3ee;box-shadow:0 0 0 4px rgba(34,211,238,0.35),0 0 15px rgba(34,211,238,0.3);border-radius:6px;pointer-events:none;z-index:99999;transition:all 0.2s;`;
                document.body.appendChild(d);
            }""", bbox)
            logger.info(f"[{session.token}] booking: highlighted '{step_selector}' via {css}")
            return True
    except Exception as e:
        logger.warning(f"[{session.token}] booking_highlight failed: {e}")
    return False


@_on_pw_loop
async def booking_execute_step(session: BrowserSession, step_selector: str, value: str) -> bool:
    """Execute a Booking.com search step using exact data-testid selectors.

    Uses JS clicks and force=True to bypass modal overlays that Booking.com
    shows for sign-in prompts and cookie consent.
    """
    page = session.page
    sel = step_selector.lower().strip()
    logger.info(f"[{session.token}] booking_execute: sel={sel}, value={value}")

    try:
        # ── DESTINATION ────────────────────────────────────────────
        if sel == "destination":
            # Focus destination with JS click to bypass any overlay
            await page.evaluate("""() => {
                const inp = document.querySelector('input[name="ss"]');
                if (inp) { inp.focus(); inp.click(); }
            }""")
            await page.wait_for_timeout(400)
            # Clear existing text
            inp = page.locator('input[name="ss"]')
            await inp.fill("", timeout=3000)
            await page.wait_for_timeout(200)
            # Type destination letter by letter for autocomplete
            await inp.type(value, delay=90)
            await page.wait_for_timeout(2000)  # wait for autocomplete dropdown

            # Try to click first autocomplete suggestion with multiple strategies
            picked = False
            for ac_sel in [
                '[data-testid="autocomplete-result"]',
                'ul[role="listbox"] li',
                '[role="option"]',
            ]:
                try:
                    first_opt = page.locator(ac_sel).first
                    if await first_opt.count() > 0:
                        await first_opt.click(timeout=3000, force=True)
                        logger.info(f"[{session.token}] booking: picked autocomplete via {ac_sel}")
                        picked = True
                        break
                except Exception:
                    continue

            if not picked:
                # Fallback: press Down+Enter to accept first suggestion
                await page.keyboard.press("ArrowDown")
                await page.wait_for_timeout(200)
                await page.keyboard.press("Enter")
                logger.info(f"[{session.token}] booking: autocomplete fallback Enter")

            await page.wait_for_timeout(600)
            return True

        # ── DATES (calendar picker) ────────────────────────────────
        elif sel == "dates":
            # Open calendar via JS click to bypass overlays
            await page.evaluate("""() => {
                const btn = document.querySelector('[data-testid="searchbox-dates-container"]');
                if (btn) btn.click();
            }""")
            await page.wait_for_timeout(1200)

            # Parse "2026-05-10 to 2026-05-15"
            parts = value.replace(" to ", "|").replace(" - ", "|").replace(",", "|").split("|")
            checkin = parts[0].strip() if len(parts) >= 1 else ""
            checkout = parts[1].strip() if len(parts) >= 2 else ""

            async def _navigate_and_click_date(date_str: str, label: str):
                """Scroll the calendar forward month by month until the
                [data-date] cell is visible, then click it."""
                for attempt in range(14):  # up to ~14 months ahead
                    # Check if the date cell exists in the current view
                    found = await page.evaluate(f"""() => {{
                        const c = document.querySelector('[data-date="{date_str}"]');
                        if (!c) return false;
                        const r = c.getBoundingClientRect();
                        return r.width > 0 && r.height > 0 && r.top > 0 && r.top < window.innerHeight;
                    }}""")
                    if found:
                        # Click via JS — guaranteed no overlay intercept
                        await page.evaluate(f"""() => {{
                            document.querySelector('[data-date="{date_str}"]').click();
                        }}""")
                        logger.info(f"[{session.token}] booking: clicked {label} {date_str} (attempt {attempt})")
                        await page.wait_for_timeout(500)
                        return True

                    # Click the '>' next-month button (aria-label="Next month")
                    clicked_next = await page.evaluate("""() => {
                        const btn = document.querySelector('button[aria-label="Next month"]');
                        if (btn) { btn.click(); return true; }
                        return false;
                    }""")
                    if not clicked_next:
                        logger.warning(f"[{session.token}] booking: no 'Next month' button found")
                        break
                    await page.wait_for_timeout(500)

                logger.warning(f"[{session.token}] booking: could not find {label} {date_str} after scrolling")
                return False

            if checkin:
                await _navigate_and_click_date(checkin, "check-in")
            if checkout:
                await _navigate_and_click_date(checkout, "check-out")

            return True

        # ── GUESTS (occupancy +/- popup) ───────────────────────────
        elif sel == "guests":
            # Open occupancy popup via JS to bypass overlays
            await page.evaluate("""() => {
                const btn = document.querySelector('[data-testid="occupancy-config"]');
                if (btn) btn.click();
            }""")
            await page.wait_for_timeout(800)

            # Parse value — formats: "2", "2 adults", "2 adults 1 child",
            # "adults=3,children=1,rooms=1"
            import re as _re
            target_adults = 2
            target_children = 0
            target_rooms = 1

            m_a = _re.search(r'(\d+)\s*adult', value.lower())
            if m_a:
                target_adults = int(m_a.group(1))
            elif value.strip().isdigit():
                target_adults = int(value.strip())

            m_c = _re.search(r'(\d+)\s*child', value.lower())
            if m_c:
                target_children = int(m_c.group(1))

            m_r = _re.search(r'(\d+)\s*room', value.lower())
            if m_r:
                target_rooms = int(m_r.group(1))

            async def _adjust_field(input_id: str, target: int, field_name: str):
                """Use JS to read the current value from the hidden input,
                then click its sibling minus (btn[0]) / plus (btn[1]) buttons."""
                result = await page.evaluate(f"""(target) => {{
                    const inp = document.getElementById('{input_id}');
                    if (!inp) return {{ok:false, err:'input not found'}};
                    const current = parseInt(inp.value) || 0;
                    // Find the two sibling buttons (minus, plus) in the same row
                    const row = inp.parentElement;
                    const btns = row ? row.querySelectorAll('button') : [];
                    if (btns.length < 2) return {{ok:false, err:'buttons not found', btnCount:btns.length}};
                    const minus = btns[0];
                    const plus  = btns[1];
                    let diff = target - current;
                    let clicks = 0;
                    while (diff > 0) {{ plus.click();  diff--; clicks++; }}
                    while (diff < 0) {{ minus.click(); diff++; clicks++; }}
                    // Re-read to confirm
                    const final_val = parseInt(inp.value) || 0;
                    return {{ok:true, from:current, to:final_val, clicks:clicks}};
                }}""", target)
                logger.info(f"[{session.token}] booking: {field_name} adjust: {result}")
                await page.wait_for_timeout(250)

            await _adjust_field('group_adults',   target_adults,   'adults')
            await _adjust_field('group_children', target_children, 'children')
            await _adjust_field('no_rooms',       target_rooms,    'rooms')

            # Close popup by clicking Done button
            try:
                await page.evaluate("""() => {
                    const popup = document.querySelector('[data-testid="occupancy-popup"]');
                    if (!popup) return;
                    const btns = popup.querySelectorAll('button');
                    for (const b of btns) {
                        if (b.textContent.trim() === 'Done') { b.click(); return; }
                    }
                }""")
                await page.wait_for_timeout(400)
                logger.info(f"[{session.token}] booking: closed occupancy popup")
            except Exception:
                pass

            return True

        # ── SEARCH (submit) ────────────────────────────────────────
        elif sel == "search":
            # Click the search/submit button via JS to bypass overlays
            await page.evaluate("""() => {
                const btn = document.querySelector('button[type="submit"]');
                if (btn) btn.click();
            }""")
            # Wait for the results page to load
            try:
                await page.wait_for_load_state('domcontentloaded', timeout=15000)
            except Exception:
                pass
            await page.wait_for_timeout(4000)
            logger.info(f"[{session.token}] booking: clicked Search, results loading")
            return True

        # ── RESULTS (view search results page) ─────────────────────
        elif sel == "results":
            # Just wait for the results page to fully render
            try:
                await page.wait_for_load_state('networkidle', timeout=10000)
            except Exception:
                pass
            await page.wait_for_timeout(2000)
            logger.info(f"[{session.token}] booking: results page displayed")
            return True

        else:
            logger.warning(f"[{session.token}] booking_execute: unknown step '{sel}'")
            return False

    except Exception as e:
        logger.warning(f"[{session.token}] booking_execute failed for '{sel}': {e}")
        return False
