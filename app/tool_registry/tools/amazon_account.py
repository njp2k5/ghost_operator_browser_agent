from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import json
import os
import logging
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

from tool_registry.registry import register_tool

DEFAULT_MARKETPLACE = "www.amazon.in"
DEFAULT_HEADLESS = True
DEFAULT_ORDER_LIMIT = 5
MAX_ORDER_LIMIT = 10
AMAZON_STORAGE_STATE_B64_ENV = "AMAZON_STORAGE_STATE_B64"
AMAZON_STORAGE_STATE_PATH_ENV = "AMAZON_STORAGE_STATE_PATH"

STAGE_AWAIT_EMAIL = "await_email"
STAGE_AWAIT_PASSWORD = "await_password"
STAGE_AWAIT_OTP = "await_otp"
STAGE_AUTHENTICATED = "authenticated"

ORDER_ID_PATTERN = re.compile(r"\b\d{3}-\d{7}-\d{7}\b")
STATUS_PATTERN = re.compile(
    r"(Delivered|Shipped|Out for delivery|Not yet shipped|Cancelled|Returned|Refunded)",
    re.IGNORECASE,
)
MOBILE_PATTERN = re.compile(r"^\+?\d[\d\s\-]{6,}$")


@dataclass
class AmazonSession:
    playwright: Any
    browser: Any
    context: Any
    page: Any
    marketplace: str
    profile_dir: str
    from_storage_state: bool = False
    stage: str = STAGE_AWAIT_EMAIL
    created_at: float = field(default_factory=time.time)


SESSIONS: dict[str, AmazonSession] = {}
CONTINUE_SIGNALS = {
    "continue",
    "done",
    "i solved it",
    "solved",
    "next",
    "ok done",
}

_LOG = logging.getLogger("ghost_operator.amazon_account")

# ---------------------------------------------------------------------------
# Stealth browser configuration
# ---------------------------------------------------------------------------
_STEALTH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
    "--disable-accelerated-2d-canvas",
    "--no-first-run",
    "--disable-infobars",
    "--window-size=1366,900",
]
_STEALTH_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
_STEALTH_EXTRA_HEADERS = {"Accept-Language": "en-IN,en;q=0.9"}
_DEBUG_SCREENSHOT_DIR = Path.home() / ".ghost_operator_amazon_profiles" / "_debug"

# ---------------------------------------------------------------------------
# Persistent ProactorEventLoop worker
# ---------------------------------------------------------------------------
# amazon_account creates stateful Playwright objects (browser/page) that are
# bound to the event loop they were created in.  Uvicorn on Windows runs a
# SelectorEventLoop which cannot spawn subprocesses, so we keep a single
# dedicated ProactorEventLoop running in a background daemon thread.  Every
# call to `run()` is dispatched to that loop via run_coroutine_threadsafe so
# all sessions share the same loop and Playwright objects stay valid across
# multiple turns.

_WORKER_LOOP: asyncio.AbstractEventLoop | None = None
_WORKER_THREAD: threading.Thread | None = None
_WORKER_LOCK = threading.Lock()


def _get_worker_loop() -> asyncio.AbstractEventLoop:
    global _WORKER_LOOP, _WORKER_THREAD
    with _WORKER_LOCK:
        if _WORKER_LOOP is not None and not _WORKER_LOOP.is_closed():
            return _WORKER_LOOP
        if sys.platform == "win32":
            new_loop: asyncio.AbstractEventLoop = asyncio.ProactorEventLoop()
        else:
            new_loop = asyncio.new_event_loop()
        _WORKER_LOOP = new_loop
        _WORKER_THREAD = threading.Thread(
            target=new_loop.run_forever,
            daemon=True,
            name="amazon-account-worker",
        )
        _WORKER_THREAD.start()
        return new_loop


tool_definition = {
    "name": "amazon_account",
    "description": "Guide Amazon account login over chat and fetch order history/status",
    "input_schema": {
        "type": "object",
        "properties": {
            "session_id": {
                "type": "string",
                "description": "Unique user/session id, typically WhatsApp sender",
            },
            "user_input": {
                "type": "string",
                "description": "User message or credential input for the current step",
            },
            "command": {
                "type": "string",
                "description": "Optional command: start, orders, order_status, logout, export_state",
            },
            "marketplace": {
                "type": "string",
                "description": "Amazon domain, e.g. www.amazon.in",
                "default": DEFAULT_MARKETPLACE,
            },
            "headless": {
                "type": "boolean",
                "description": "Whether to run browser headless",
                "default": DEFAULT_HEADLESS,
            },
            "limit": {
                "type": "number",
                "description": "Maximum number of orders to return",
                "default": DEFAULT_ORDER_LIMIT,
            },
            "storage_state_b64": {
                "type": "string",
                "description": "Base64-encoded Playwright storage state JSON for headless deployments",
            },
            "storage_state_path": {
                "type": "string",
                "description": "Path to Playwright storage state JSON file",
            },
        },
        "required": ["session_id"],
    },
}


def _normalize_marketplace(value: Any) -> str:
    if value is None:
        return DEFAULT_MARKETPLACE
    marketplace = str(value).strip().lower()
    if not marketplace:
        return DEFAULT_MARKETPLACE
    marketplace = marketplace.replace("https://", "").replace("http://", "").strip("/")
    if "." not in marketplace or " " in marketplace:
        raise ValueError("'marketplace' must be a valid domain like 'www.amazon.in'")
    return marketplace


def _normalize_headless(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return DEFAULT_HEADLESS
    text = str(value).strip().lower()
    if text in {"0", "false", "no", "off"}:
        return False
    if text in {"1", "true", "yes", "on"}:
        return True
    return DEFAULT_HEADLESS


def _normalize_limit(value: Any) -> int:
    if value is None:
        return DEFAULT_ORDER_LIMIT
    try:
        limit = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("'limit' must be a number") from exc
    return max(1, min(MAX_ORDER_LIMIT, limit))


async def _page_diagnostic(page) -> str:
    url = page.url
    try:
        title = await page.title()
    except Exception:  # noqa: BLE001
        title = "<n/a>"
    return f"url={url!r} title={title!r}"


async def _is_amazon_error_page(page) -> bool:
    """Return True when Amazon shows its generic 'Something went wrong' error page."""
    url = page.url.lower()
    if "/errors/" in url or "somethingwentwrong" in url.replace("-", ""):
        return True
    try:
        title = (await page.title()).lower()
        if "something went wrong" in title:
            return True
    except Exception:  # noqa: BLE001
        pass
    try:
        body = await page.locator("body").inner_text(timeout=3000)
        low = body.lower()
        if "something went wrong on our end" in low or "sorry, something went wrong" in low:
            return True
    except Exception:  # noqa: BLE001
        pass
    return False


async def _save_debug_screenshot(page, label: str) -> str:
    """Save a full-page screenshot to the debug folder and return the path."""
    try:
        _DEBUG_SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        ts = int(time.time())
        out_path = _DEBUG_SCREENSHOT_DIR / f"{label}_{ts}.png"
        await page.screenshot(path=str(out_path), full_page=True)
        return str(out_path)
    except Exception as exc:  # noqa: BLE001
        return f"<screenshot failed: {exc}>"


def _signin_url(marketplace: str) -> str:
    return_to = quote(f"https://{marketplace}/?ref_=nav_signin", safe="")
    identifier_select = quote(
        "http://specs.openid.net/auth/2.0/identifier_select",
        safe="",
    )
    openid_ns = quote("http://specs.openid.net/auth/2.0", safe="")
    return (
        f"https://{marketplace}/ap/signin"
        f"?openid.return_to={return_to}"
        f"&openid.identity={identifier_select}"
        "&openid.assoc_handle=inflex"
        "&openid.mode=checkid_setup"
        f"&openid.claimed_id={identifier_select}"
        f"&openid.ns={openid_ns}"
    )


def _orders_url(marketplace: str) -> str:
    return f"https://{marketplace}/gp/css/order-history"


def _session_profile_dir(session_id: str) -> str:
    safe_name = re.sub(r"[^a-zA-Z0-9_.-]", "_", session_id).strip("._") or "default"
    root = Path.home() / ".ghost_operator_amazon_profiles"
    root.mkdir(parents=True, exist_ok=True)
    return str(root / safe_name)


def _decode_storage_state_b64(encoded: str) -> dict[str, Any] | None:
    try:
        raw = base64.b64decode(encoded)
    except Exception:  # noqa: BLE001
        return None

    try:
        parsed = json.loads(raw.decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None

    return parsed if isinstance(parsed, dict) else None


def _load_storage_state_path(path_value: str) -> dict[str, Any] | None:
    path = Path(path_value).expanduser().resolve()
    if not path.exists() or not path.is_file():
        return None

    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None

    return parsed if isinstance(parsed, dict) else None


def _resolve_storage_state(params: dict[str, Any]) -> dict[str, Any] | None:
    inline_b64 = str(params.get("storage_state_b64") or "").strip()
    if inline_b64:
        state = _decode_storage_state_b64(inline_b64)
        if state is not None:
            return state

    inline_path = str(params.get("storage_state_path") or "").strip()
    if inline_path:
        state = _load_storage_state_path(inline_path)
        if state is not None:
            return state

    env_b64 = os.getenv(AMAZON_STORAGE_STATE_B64_ENV, "").strip()
    if env_b64:
        state = _decode_storage_state_b64(env_b64)
        if state is not None:
            return state

    env_path = os.getenv(AMAZON_STORAGE_STATE_PATH_ENV, "").strip()
    if env_path:
        state = _load_storage_state_path(env_path)
        if state is not None:
            return state

    return None


def _looks_like_email_or_mobile(text: str) -> bool:
    value = text.strip()
    if not value:
        return False
    if "@" in value:
        return True
    return bool(MOBILE_PATTERN.match(value))


def _is_continue_signal(text: str) -> bool:
    return text.strip().lower() in CONTINUE_SIGNALS


def _result(
    *,
    success: bool,
    assistant_reply: str,
    stage: str,
    session_active: bool,
    awaiting_input: bool = False,
    **extra: Any,
) -> dict[str, Any]:
    payload = {
        "success": success,
        "assistant_reply": assistant_reply,
        "stage": stage,
        "session_active": session_active,
        "awaiting_input": awaiting_input,
    }
    payload.update(extra)
    return payload


async def _close_session(session_id: str) -> None:
    session = SESSIONS.pop(session_id, None)
    if session is None:
        return

    try:
        await session.page.close()
    except Exception:  # noqa: BLE001
        pass
    try:
        await session.context.close()
    except Exception:  # noqa: BLE001
        pass
    try:
        await session.browser.close()
    except Exception:  # noqa: BLE001
        pass
    try:
        await session.playwright.stop()
    except Exception:  # noqa: BLE001
        pass


async def _new_session(
    session_id: str,
    marketplace: str,
    headless: bool,
    storage_state: dict[str, Any] | None = None,
) -> AmazonSession:
    _LOG.info(
        "[%s] _new_session start — marketplace=%s headless=%s storage_state=%s",
        session_id, marketplace, headless, storage_state is not None,
    )
    await _close_session(session_id)

    playwright = await async_playwright().start()
    profile_dir = _session_profile_dir(session_id)
    _LOG.info("[%s] profile_dir=%s", session_id, profile_dir)

    if storage_state is not None:
        browser = await playwright.chromium.launch(
            headless=headless,
            args=_STEALTH_ARGS,
            slow_mo=50,
        )
        context = await browser.new_context(
            storage_state=storage_state,
            user_agent=_STEALTH_UA,
            viewport={"width": 1366, "height": 900},
            locale="en-IN",
        )
        page = await context.new_page()
    else:
        context = await playwright.chromium.launch_persistent_context(
            user_data_dir=profile_dir,
            headless=headless,
            args=_STEALTH_ARGS,
            slow_mo=50,
            user_agent=_STEALTH_UA,
            viewport={"width": 1366, "height": 900},
            locale="en-IN",
        )
        page = context.pages[0] if context.pages else await context.new_page()
        browser = context.browser

    await context.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
    )
    await context.set_extra_http_headers(_STEALTH_EXTRA_HEADERS)
    page.set_default_navigation_timeout(30000)
    page.set_default_timeout(12000)
    _LOG.info("[%s] browser launched, navigating to signin page...", session_id)
    await _goto_signin(page, marketplace)
    _LOG.info("[%s] signin page loaded: %s", session_id, await _page_diagnostic(page))

    session = AmazonSession(
        playwright=playwright,
        browser=browser,
        context=context,
        page=page,
        marketplace=marketplace,
        profile_dir=profile_dir,
        from_storage_state=storage_state is not None,
        stage=STAGE_AWAIT_EMAIL,
    )
    SESSIONS[session_id] = session
    return session


async def _has_selector(page, selector: str) -> bool:
    return await page.locator(selector).count() > 0


async def _goto_signin(page, marketplace: str) -> None:
    url = _signin_url(marketplace)
    _LOG.info("[goto_signin] navigating to %s", url)
    await page.goto(url, wait_until="domcontentloaded")
    try:
        await page.wait_for_load_state("networkidle", timeout=7000)
    except PlaywrightTimeoutError:
        pass
    _LOG.info("[goto_signin] loaded: %s", await _page_diagnostic(page))


async def _first_selector(page, selectors: list[str]):
    for selector in selectors:
        node = page.locator(selector).first
        if await node.count() > 0:
            return node
    return None


async def _is_captcha(page) -> bool:
    if "validateCaptcha" in page.url:
        return True
    return await _has_selector(page, "form[action*='validateCaptcha']")


async def _is_password_step(page) -> bool:
    return await _has_selector(page, "input#ap_password, input[name='password']")


async def _is_otp_step(page) -> bool:
    return await _has_selector(
        page,
        "input[name='otpCode'], input[name='code'], input#cvf-input-code, input[type='tel']",
    )


async def _find_email_node(page):
    return await _first_selector(
        page,
        [
            "form[name='signIn'] input#ap_email",
            "form[name='signIn'] input[name='email']",
            "input#ap_email",
            "input[name='email']",
            "input#ap_email_login",
            "input[type='email']",
        ],
    )


async def _continue_after_manual_action(session: AmazonSession) -> dict[str, Any]:
    page = session.page

    try:
        await page.wait_for_load_state("domcontentloaded", timeout=8000)
    except PlaywrightTimeoutError:
        pass

    if await _is_signed_in(page):
        session.stage = STAGE_AUTHENTICATED
        return _result(
            success=True,
            assistant_reply="Login detected. You are signed in. Ask me to show your orders.",
            stage=session.stage,
            session_active=True,
        )

    if await _is_password_step(page):
        session.stage = STAGE_AWAIT_PASSWORD
        return _result(
            success=True,
            assistant_reply="Please enter your Amazon password.",
            stage=session.stage,
            session_active=True,
            awaiting_input=True,
        )

    if await _is_otp_step(page):
        session.stage = STAGE_AWAIT_OTP
        return _result(
            success=True,
            assistant_reply="Please enter the OTP sent by Amazon.",
            stage=session.stage,
            session_active=True,
            awaiting_input=True,
        )

    if await _is_captcha(page):
        return _result(
            success=False,
            assistant_reply=(
                "Challenge is still active in browser. Please solve it, then send 'continue'."
            ),
            stage=session.stage,
            session_active=True,
            awaiting_input=True,
        )

    err = await _extract_auth_error(page)
    return _result(
        success=False,
        assistant_reply=err or "Still waiting at Amazon login. If page changed, send 'continue' again.",
        stage=session.stage,
        session_active=True,
        awaiting_input=True,
    )


async def _is_signed_in(page) -> bool:
    url = page.url.lower()
    if "/ap/signin" in url:
        return False
    if await _has_selector(page, "#nav-orders, a[href*='order-history']"):
        return True
    return "youraccount" in url or "order-history" in url


async def _extract_auth_error(page) -> str:
    node = await _first_selector(
        page,
        [
            "#auth-error-message-box .a-alert-content",
            "#auth-warning-message-box .a-alert-content",
            ".a-alert-content",
        ],
    )
    if node is None:
        return ""
    try:
        text = (await node.inner_text()).strip()
    except Exception:  # noqa: BLE001
        return ""
    return " ".join(text.split())


async def _extract_orders(session: AmazonSession, limit: int) -> list[dict[str, str]]:
    page = session.page
    _LOG.info("[orders] navigating to order history — marketplace=%s limit=%d", session.marketplace, limit)
    await page.goto(_orders_url(session.marketplace), wait_until="domcontentloaded")
    try:
        await page.wait_for_load_state("networkidle", timeout=7000)
    except PlaywrightTimeoutError:
        pass

    _LOG.info("[orders] page loaded: %s", await _page_diagnostic(page))

    if await _is_captcha(page):
        _LOG.warning("[orders] CAPTCHA on orders page: %s", page.url)
        return []

    if await _is_amazon_error_page(page):
        _LOG.warning("[orders] Amazon error page on orders page: %s", page.url)
        return []

    cards = page.locator("div.order-card, div.a-box-group.a-spacing-base.order")
    count = await cards.count()
    _LOG.info("[orders] found %d order cards", count)
    seen: set[str] = set()
    orders: list[dict[str, str]] = []

    for idx in range(count):
        if len(orders) >= limit:
            break

        card = cards.nth(idx)
        text = ""
        try:
            text = " ".join((await card.inner_text()).split())
        except Exception:  # noqa: BLE001
            continue

        order_id_match = ORDER_ID_PATTERN.search(text)
        order_id = order_id_match.group(0) if order_id_match else ""

        title = ""
        title_node = card.locator("a[href*='/dp/'], a.a-link-normal").first
        if await title_node.count() > 0:
            try:
                title = " ".join((await title_node.inner_text()).split())
            except Exception:  # noqa: BLE001
                title = ""

        detail_url = ""
        detail_node = card.locator("a[href*='order-details'], a[href*='order-summary']").first
        if await detail_node.count() > 0:
            href = await detail_node.get_attribute("href")
            if href:
                detail_url = href if href.startswith("http") else f"https://{session.marketplace}{href}"

        status_match = STATUS_PATTERN.search(text)
        status = status_match.group(1) if status_match else "Status unavailable"

        key = order_id or f"idx-{idx}-{title}"
        if key in seen:
            continue
        seen.add(key)

        orders.append(
            {
                "order_id": order_id,
                "status": status,
                "title": title,
                "detail_url": detail_url,
            }
        )

    _LOG.info("[orders] extracted %d orders", len(orders))
    return orders


async def _export_storage_state(session: AmazonSession) -> tuple[str, int]:
    state = await session.context.storage_state()
    raw = json.dumps(state, ensure_ascii=False)
    target = Path(session.profile_dir) / "storage_state.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(raw, encoding="utf-8")

    b64 = base64.b64encode(raw.encode("utf-8")).decode("ascii")
    os.environ[AMAZON_STORAGE_STATE_B64_ENV] = b64
    os.environ[AMAZON_STORAGE_STATE_PATH_ENV] = str(target)
    return str(target), len(b64)


async def _try_export_storage_state(session: AmazonSession) -> None:
    """Silently save storage state after successful login so future sessions reuse it."""
    try:
        path, b64_len = await _export_storage_state(session)
        _LOG.info("[export] storage state saved to %s (%d b64 chars)", path, b64_len)
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("[export] failed to save storage state: %s", exc)


def _extract_order_id(text: str) -> str:
    match = ORDER_ID_PATTERN.search(text)
    return match.group(0) if match else ""


def _is_orders_intent(text: str) -> bool:
    msg = text.lower()
    return "order" in msg or "orders" in msg or "status" in msg


async def _handle_email(session: AmazonSession, user_input: str) -> dict[str, Any]:
    page = session.page
    _LOG.info("[email] start — page=%s input=%r", await _page_diagnostic(page), user_input[:40] if user_input else "")
    email_node = await _find_email_node(page)
    if email_node is None:
        _LOG.info("[email] email input not found, re-navigating to signin")
        await _goto_signin(page, session.marketplace)
        email_node = await _find_email_node(page)

    if email_node is None:
        _LOG.warning("[email] email input still not found after re-nav: %s", await _page_diagnostic(page))
        if await _is_captcha(page):
            return _result(
                success=False,
                assistant_reply=(
                    "Amazon challenge/CAPTCHA is blocking automated login. "
                    "Please retry with a visible browser and solve the challenge manually."
                ),
                stage=session.stage,
                session_active=True,
                awaiting_input=True,
            )

        return _result(
            success=False,
            assistant_reply=(
                "I could not open Amazon sign-in email step. "
                "Please type 'start amazon login' to reopen login."
            ),
            stage=session.stage,
            session_active=True,
            awaiting_input=True,
        )

    await email_node.fill(user_input)
    continue_btn = await _first_selector(
        page,
        ["#continue", "input#continue", "button#continue", "input[type='submit']"],
    )
    if continue_btn is not None:
        _LOG.info("[email] clicking continue button")
        await continue_btn.click()

    try:
        await page.wait_for_load_state("domcontentloaded", timeout=15000)
    except PlaywrightTimeoutError:
        pass

    _LOG.info("[email] post-submit page: %s", await _page_diagnostic(page))

    if await _is_amazon_error_page(page):
        screenshot = await _save_debug_screenshot(page, "email_error")
        _LOG.warning("[email] Amazon error page after email submit. screenshot=%s", screenshot)
        try:
            await _goto_signin(page, session.marketplace)
            session.stage = STAGE_AWAIT_EMAIL
        except Exception:  # noqa: BLE001
            pass
        return _result(
            success=False,
            assistant_reply=(
                "Amazon returned an error after your email (bot detection). "
                "Please wait a moment and send your email again."
            ),
            stage=STAGE_AWAIT_EMAIL,
            session_active=True,
            awaiting_input=True,
        )

    if await _is_captcha(page):
        _LOG.warning("[email] CAPTCHA detected after email: %s", page.url)
        return _result(
            success=False,
            assistant_reply=(
                "Amazon showed a CAPTCHA/challenge after email. "
                "Try again later or run a visible browser session and solve the challenge manually."
            ),
            stage=session.stage,
            session_active=True,
            awaiting_input=True,
        )

    if await _is_password_step(page):
        _LOG.info("[email] → password step")
        session.stage = STAGE_AWAIT_PASSWORD
        return _result(
            success=True,
            assistant_reply="Email received. Please enter your Amazon password.",
            stage=session.stage,
            session_active=True,
            awaiting_input=True,
        )

    if await _is_otp_step(page):
        _LOG.info("[email] → OTP step")
        session.stage = STAGE_AWAIT_OTP
        return _result(
            success=True,
            assistant_reply="Please enter the OTP sent by Amazon.",
            stage=session.stage,
            session_active=True,
            awaiting_input=True,
        )

    if await _is_signed_in(page):
        _LOG.info("[email] → already signed in")
        session.stage = STAGE_AUTHENTICATED
        return _result(
            success=True,
            assistant_reply="You are signed in. Ask me to show your recent orders or order status.",
            stage=session.stage,
            session_active=True,
        )

    err = await _extract_auth_error(page)
    _LOG.warning("[email] unrecognised state — error=%r page=%s", err, await _page_diagnostic(page))
    return _result(
        success=False,
        assistant_reply=err or "Email step failed. Please re-enter your Amazon email or mobile number.",
        stage=STAGE_AWAIT_EMAIL,
        session_active=True,
        awaiting_input=True,
    )


async def _handle_password(session: AmazonSession, user_input: str) -> dict[str, Any]:
    page = session.page
    _LOG.info("[password] start — page=%s", await _page_diagnostic(page))
    pwd_node = await _first_selector(page, ["input#ap_password", "input[name='password']"])
    if pwd_node is None:
        _LOG.warning("[password] password input not found: %s", await _page_diagnostic(page))
        return _result(
            success=False,
            assistant_reply="Password input is not visible. Please send your email again to restart login.",
            stage=STAGE_AWAIT_EMAIL,
            session_active=True,
            awaiting_input=True,
        )

    await pwd_node.fill(user_input)
    sign_in_btn = await _first_selector(page, ["#signInSubmit", "input#signInSubmit", "input[type='submit']"])
    if sign_in_btn is not None:
        _LOG.info("[password] clicking sign-in button")
        await sign_in_btn.click()

    try:
        await page.wait_for_load_state("domcontentloaded", timeout=15000)
    except PlaywrightTimeoutError:
        pass

    _LOG.info("[password] post-submit page: %s", await _page_diagnostic(page))

    if await _is_amazon_error_page(page):
        screenshot = await _save_debug_screenshot(page, "password_error")
        _LOG.warning("[password] Amazon error page after password submit. screenshot=%s", screenshot)
        # Navigate back to sign-in so the session can retry
        try:
            await _goto_signin(page, session.marketplace)
            session.stage = STAGE_AWAIT_EMAIL
        except Exception:  # noqa: BLE001
            pass
        return _result(
            success=False,
            assistant_reply=(
                "Amazon returned an error after your password — this is usually bot detection. "
                "Please wait 30 seconds then send your email again to retry."
            ),
            stage=STAGE_AWAIT_EMAIL,
            session_active=True,
            awaiting_input=True,
        )

    if await _is_otp_step(page):
        _LOG.info("[password] → OTP step")
        session.stage = STAGE_AWAIT_OTP
        return _result(
            success=True,
            assistant_reply="Password accepted. Please enter the OTP sent by Amazon.",
            stage=session.stage,
            session_active=True,
            awaiting_input=True,
        )

    if await _is_signed_in(page):
        _LOG.info("[password] → authenticated")
        session.stage = STAGE_AUTHENTICATED
        return _result(
            success=True,
            assistant_reply="Login successful. Ask me to show your recent Amazon orders.",
            stage=session.stage,
            session_active=True,
        )

    if await _is_captcha(page):
        _LOG.warning("[password] CAPTCHA/challenge after password: %s", page.url)
        return _result(
            success=False,
            assistant_reply=(
                "Amazon requested an extra challenge after password. "
                "Please solve it in the opened browser, then send 'continue'."
            ),
            stage=session.stage,
            session_active=True,
            awaiting_input=True,
        )

    err = await _extract_auth_error(page)
    _LOG.warning("[password] unrecognised state — error=%r page=%s", err, await _page_diagnostic(page))
    return _result(
        success=False,
        assistant_reply=err or "Password was not accepted. Please re-enter your password.",
        stage=STAGE_AWAIT_PASSWORD,
        session_active=True,
        awaiting_input=True,
    )


async def _handle_otp(session: AmazonSession, user_input: str) -> dict[str, Any]:
    page = session.page
    _LOG.info("[otp] start — page=%s", await _page_diagnostic(page))
    otp_node = await _first_selector(
        page,
        ["input[name='otpCode']", "input#cvf-input-code", "input[name='code']", "input[type='tel']"],
    )
    if otp_node is None:
        _LOG.warning("[otp] OTP input not found: %s", await _page_diagnostic(page))
        return _result(
            success=False,
            assistant_reply="OTP input is not visible. Please try login again.",
            stage=STAGE_AWAIT_EMAIL,
            session_active=True,
            awaiting_input=True,
        )

    await otp_node.fill(user_input)
    submit_btn = await _first_selector(
        page,
        [
            "input#cvf-submit-otp-button",
            "input#auth-signin-button",
            "input[type='submit']",
            "button[type='submit']",
        ],
    )
    if submit_btn is not None:
        _LOG.info("[otp] clicking submit")
        await submit_btn.click()

    try:
        await page.wait_for_load_state("domcontentloaded", timeout=15000)
    except PlaywrightTimeoutError:
        pass

    _LOG.info("[otp] post-submit page: %s", await _page_diagnostic(page))

    if await _is_amazon_error_page(page):
        screenshot = await _save_debug_screenshot(page, "otp_error")
        _LOG.warning("[otp] Amazon error page after OTP. screenshot=%s", screenshot)
        try:
            await _goto_signin(page, session.marketplace)
            session.stage = STAGE_AWAIT_EMAIL
        except Exception:  # noqa: BLE001
            pass
        return _result(
            success=False,
            assistant_reply=(
                "Amazon returned an error after OTP. "
                "Please wait a moment and send your email again to retry."
            ),
            stage=STAGE_AWAIT_EMAIL,
            session_active=True,
            awaiting_input=True,
        )

    if await _is_signed_in(page):
        _LOG.info("[otp] → authenticated")
        session.stage = STAGE_AUTHENTICATED
        return _result(
            success=True,
            assistant_reply="OTP verified. You are signed in. Ask me to list your orders.",
            stage=session.stage,
            session_active=True,
        )

    if await _is_captcha(page):
        _LOG.warning("[otp] CAPTCHA after OTP: %s", page.url)
        return _result(
            success=False,
            assistant_reply=(
                "Amazon challenge is still active. Please solve it in browser and send OTP again."
            ),
            stage=STAGE_AWAIT_OTP,
            session_active=True,
            awaiting_input=True,
        )

    err = await _extract_auth_error(page)
    _LOG.warning("[otp] unrecognised state — error=%r page=%s", err, await _page_diagnostic(page))
    return _result(
        success=False,
        assistant_reply=err or "OTP verification failed. Please enter the OTP again.",
        stage=STAGE_AWAIT_OTP,
        session_active=True,
        awaiting_input=True,
    )


async def _handle_authenticated(
    session_id: str,
    session: AmazonSession,
    user_input: str,
    command: str,
    limit: int,
) -> dict[str, Any]:
    text = (user_input or "").strip()
    lower = text.lower()

    if command in {"logout", "cancel"} or lower in {"logout", "log out", "exit amazon", "stop amazon"}:
        await _close_session(session_id)
        return _result(
            success=True,
            assistant_reply="Amazon session closed.",
            stage="logged_out",
            session_active=False,
        )

    if command in {"export_state", "save_state", "prepare_deploy"} or lower in {
        "export session",
        "export state",
        "save session",
        "prepare deploy",
    }:
        path, b64_len = await _export_storage_state(session)
        return _result(
            success=True,
            assistant_reply=(
                "Saved Amazon authenticated session state for deployment. "
                f"File: {path}. Set env AMAZON_STORAGE_STATE_PATH to this file "
                "(or use AMAZON_STORAGE_STATE_B64)."
            ),
            stage=session.stage,
            session_active=True,
            storage_state_path=path,
            storage_state_b64_length=b64_len,
        )

    if command in {"orders", "order_status"} or _is_orders_intent(lower):
        orders = await _extract_orders(session, limit)
        if not orders:
            return _result(
                success=False,
                assistant_reply=(
                    "I could not read your order history right now. "
                    "Amazon may require an additional verification step."
                ),
                stage=session.stage,
                session_active=True,
                orders=[],
            )

        requested_order_id = _extract_order_id(text)
        filtered_orders = orders
        if requested_order_id:
            filtered_orders = [item for item in orders if item.get("order_id") == requested_order_id]

        if requested_order_id and not filtered_orders:
            return _result(
                success=False,
                assistant_reply=f"I could not find order {requested_order_id} in the recent list.",
                stage=session.stage,
                session_active=True,
                orders=orders,
            )

        if requested_order_id and filtered_orders:
            one = filtered_orders[0]
            return _result(
                success=True,
                assistant_reply=(
                    f"Order {one.get('order_id') or requested_order_id}: "
                    f"{one.get('status', 'Status unavailable')}"
                ),
                stage=session.stage,
                session_active=True,
                orders=filtered_orders,
            )

        return _result(
            success=True,
            assistant_reply=f"Found {len(filtered_orders)} recent order(s).",
            stage=session.stage,
            session_active=True,
            orders=filtered_orders,
        )

    return _result(
        success=True,
        assistant_reply=(
            "You are logged into Amazon. You can ask: 'show my orders', "
            "'order status <order-id>', or 'logout amazon'."
        ),
        stage=session.stage,
        session_active=True,
        awaiting_input=False,
    )


async def _run_impl(params: dict[str, Any]) -> dict[str, Any]:
    session_id = str(params.get("session_id") or "").strip()
    if not session_id:
        return _result(
            success=False,
            assistant_reply="session_id is required for amazon_account.",
            stage="invalid",
            session_active=False,
        )

    command = str(params.get("command") or "").strip().lower()
    user_input = str(params.get("user_input") or "").strip()
    marketplace = _normalize_marketplace(params.get("marketplace"))
    headless = _normalize_headless(params.get("headless"))
    limit = _normalize_limit(params.get("limit"))
    storage_state = _resolve_storage_state(params)

    _LOG.info(
        "[run] session=%s command=%r user_input=%r marketplace=%s headless=%s storage_state=%s existing_session=%s",
        session_id, command, user_input[:40] if user_input else "",
        marketplace, headless, storage_state is not None, session_id in SESSIONS,
    )

    if command in {"logout", "cancel"}:
        _LOG.info("[run] logout command — closing session")
        await _close_session(session_id)
        return _result(
            success=True,
            assistant_reply="Amazon session closed.",
            stage="logged_out",
            session_active=False,
        )

    session = SESSIONS.get(session_id)
    if session is None:
        _LOG.info("[run] no existing session — creating new session")
        try:
            session = await _new_session(
                session_id,
                marketplace,
                headless,
                storage_state=storage_state,
            )
        except Exception as exc:  # noqa: BLE001
            return _result(
                success=False,
                assistant_reply=f"Could not start Amazon session: {exc}",
                stage="startup_error",
                session_active=False,
            )

        if await _is_signed_in(session.page):
            _LOG.info("[run] new session — already signed in, setting authenticated")
            session.stage = STAGE_AUTHENTICATED
            if command in {"orders", "order_status"} or _is_orders_intent(user_input.lower()):
                return await _handle_authenticated(session_id, session, user_input, command, limit)
            return _result(
                success=True,
                assistant_reply=(
                    "Amazon session is already authenticated. "
                    "Ask 'show my orders' or 'order status <order-id>'."
                ),
                stage=session.stage,
                session_active=True,
            )

        if await _is_captcha(session.page):
            _LOG.warning("[run] CAPTCHA on initial load: %s", session.page.url)
            if storage_state is not None:
                return _result(
                    success=False,
                    assistant_reply=(
                        "Saved Amazon session appears expired or challenged. "
                        "Refresh storage state from a one-time local headed login."
                    ),
                    stage=session.stage,
                    session_active=True,
                    awaiting_input=True,
                )
            return _result(
                success=False,
                assistant_reply=(
                    "Amazon presented a CAPTCHA/challenge at login start. "
                    "Headless automation is often blocked for account flows. "
                    "Use headed mode and solve the challenge manually in the opened browser."
                ),
                stage=session.stage,
                session_active=True,
                awaiting_input=True,
            )

        _LOG.info("[run] new session — at email stage, page=%s", await _page_diagnostic(session.page))
        if user_input and _looks_like_email_or_mobile(user_input):
            result = await _handle_email(session, user_input)
            if result.get("stage") == STAGE_AUTHENTICATED and result.get("success"):
                await _try_export_storage_state(session)
            return result

        return _result(
            success=True,
            assistant_reply="Let's login to your Amazon account. Please send your Amazon email or mobile number.",
            stage=session.stage,
            session_active=True,
            awaiting_input=True,
        )

    _LOG.info("[run] existing session — stage=%s", session.stage)
    if session.stage == STAGE_AWAIT_EMAIL:
        if not user_input:
            return _result(
                success=True,
                assistant_reply="Please enter your Amazon email or mobile number.",
                stage=session.stage,
                session_active=True,
                awaiting_input=True,
            )
        if _is_continue_signal(user_input):
            result = await _continue_after_manual_action(session)
        else:
            result = await _handle_email(session, user_input)
        if result.get("stage") == STAGE_AUTHENTICATED and result.get("success"):
            await _try_export_storage_state(session)
        return result

    if session.stage == STAGE_AWAIT_PASSWORD:
        if not user_input:
            return _result(
                success=True,
                assistant_reply="Please enter your Amazon password.",
                stage=session.stage,
                session_active=True,
                awaiting_input=True,
            )
        if _is_continue_signal(user_input):
            result = await _continue_after_manual_action(session)
        else:
            result = await _handle_password(session, user_input)
        if result.get("stage") == STAGE_AUTHENTICATED and result.get("success"):
            await _try_export_storage_state(session)
        return result

    if session.stage == STAGE_AWAIT_OTP:
        if not user_input:
            return _result(
                success=True,
                assistant_reply="Please enter the OTP received from Amazon.",
                stage=session.stage,
                session_active=True,
                awaiting_input=True,
            )
        if _is_continue_signal(user_input):
            result = await _continue_after_manual_action(session)
        else:
            result = await _handle_otp(session, user_input)
        if result.get("stage") == STAGE_AUTHENTICATED and result.get("success"):
            await _try_export_storage_state(session)
        return result

    if session.stage == STAGE_AUTHENTICATED:
        return await _handle_authenticated(session_id, session, user_input, command, limit)

    return _result(
        success=False,
        assistant_reply="Amazon session is in an unknown state. Please restart login.",
        stage=session.stage,
        session_active=True,
    )


async def run(params: dict[str, Any]) -> dict[str, Any]:
    """Dispatch _run_impl to the persistent ProactorEventLoop worker thread."""
    worker_loop = _get_worker_loop()
    future = asyncio.run_coroutine_threadsafe(_run_impl(params), worker_loop)
    return await asyncio.get_running_loop().run_in_executor(
        None, future.result
    )


register_tool(tool_definition, run)