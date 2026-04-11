import asyncio
import base64
import logging
from dataclasses import dataclass, field
from typing import Optional

from playwright.async_api import async_playwright, Browser, BrowserContext, Page, Playwright

logger = logging.getLogger(__name__)

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

async def take_screenshot(session: BrowserSession) -> str:
    """Take a screenshot and return it as a base64-encoded JPEG string (faster, smaller than PNG)."""
    try:
        jpg_bytes = await session.page.screenshot(type="jpeg", quality=70, full_page=False)
        return base64.b64encode(jpg_bytes).decode("utf-8")
    except Exception as e:
        logger.warning(f"[{session.token}] Screenshot failed: {e}")
        return ""


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


async def navigate_to(session: BrowserSession, url: str) -> bool:
    """Navigate the browser to a new URL."""
    try:
        await session.page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await session.page.wait_for_timeout(1500)
        return True
    except Exception as e:
        logger.warning(f"[{session.token}] navigate_to failed for '{url}': {e}")
        return False
