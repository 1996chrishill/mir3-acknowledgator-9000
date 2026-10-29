"""Playwright-driven MIR3 recipient client.

Flow (mapped from the live MIR3 recipient web UI):
  inbox   /prepareShowActiveNotifications.do -> links call selectNotification(eventHistId, recipientHistId)
  reply   selectNotification posts command=reply -> Web Reply page with optionHistId radios
  ack     check the "Acknowledge" radio, click Save (saveNav)
"""
import logging
import re
from dataclasses import dataclass, asdict

from playwright.async_api import (
    async_playwright,
    Browser,
    BrowserContext,
    Page,
    Playwright,
    TimeoutError as PWTimeout,
)

from .config import Config

log = logging.getLogger("mir3")

LINK_SEL = 'a[href^="javascript:selectNotification"]'
ID_RE = re.compile(r"selectNotification\('([^']+)'\s*,\s*'([^']+)'\)")
INC_RE = re.compile(r"\bINC\d+\b")
ACK_LABEL = re.compile(r"^\s*Acknowledge\s*$", re.I)


class Mir3Error(Exception):
    code = "mir3_error"


class LoginError(Mir3Error):
    code = "login_failed"


@dataclass
class Notification:
    title: str
    event_hist_id: str
    recipient_hist_id: str
    incident: str | None

    def to_dict(self) -> dict:
        return asdict(self)


class Mir3Client:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.inbox_url = f"{cfg.base_url}/prepareShowActiveNotifications.do"
        self.login_url = f"{cfg.base_url}/jsp/security/showLogin.jsp"
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._ctx: BrowserContext | None = None
        self._page: Page | None = None

    # ---------- lifecycle ----------

    async def start(self) -> None:
        self._pw = await async_playwright().start()
        await self._open_browser()

    async def stop(self) -> None:
        await self._save_state()
        for obj in (self._ctx, self._browser):
            try:
                if obj:
                    await obj.close()
            except Exception:
                pass
        if self._pw:
            await self._pw.stop()

    async def _open_browser(self) -> None:
        self._browser = await self._pw.chromium.launch(
            headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
        state = str(self.cfg.state_file) if self.cfg.state_file.exists() else None
        try:
            self._ctx = await self._browser.new_context(storage_state=state)
        except Exception as e:
            log.warning("Saved session unreadable (%s); starting fresh", e)
            self._ctx = await self._browser.new_context()
        self._ctx.set_default_timeout(self.cfg.timeout_ms)
        self._page = await self._ctx.new_page()
        log.info("Browser started (saved session: %s)", "yes" if state else "no")

    async def _ensure_browser(self) -> Page:
        if not self._browser or not self._browser.is_connected() or self._page.is_closed():
            log.warning("Browser not available; restarting")
            try:
                if self._browser:
                    await self._browser.close()
            except Exception:
                pass
            await self._open_browser()
        return self._page

    async def _save_state(self) -> None:
        try:
            if self._ctx:
                await self._ctx.storage_state(path=str(self.cfg.state_file))
        except Exception as e:
            log.warning("Could not save session: %s", e)

    # ---------- auth ----------

    async def _on_login_page(self, page: Page) -> bool:
        return "showLogin" in page.url or await page.locator("#username").count() > 0

    async def _login(self, page: Page) -> None:
        log.info("Logging in as %s", self.cfg.username)
        await page.goto(self.login_url)
        await page.fill("#username", self.cfg.username)
        await page.fill("#password", self.cfg.password)
        # page enables the button on input events; fire them, then force-enable as fallback
        for ev in ("input", "keyup", "change"):
            await page.dispatch_event("#password", ev)
        await page.evaluate("document.getElementById('loginButton')?.classList.remove('disabled')")
        await page.click("#loginButton")
        try:
            await page.wait_for_url(lambda u: "showLogin" not in u, timeout=20000)
        except PWTimeout:
            if await page.locator("#mfaCode:visible, #twofactorKey:visible").count():
                raise LoginError("MFA prompt shown; cannot complete automatically")
            err = ""
            if await page.locator("#error-alert").count():
                err = (await page.locator("#error-alert").inner_text()).strip()
            raise LoginError(err or "Login did not complete (still on login page)")
        await self._save_state()
        log.info("Login OK")

    async def _goto_inbox(self) -> Page:
        page = await self._ensure_browser()
        await page.goto(self.inbox_url)
        if await self._on_login_page(page):
            await self._login(page)
            await page.goto(self.inbox_url)
            if await self._on_login_page(page):
                raise LoginError("Redirected to login after successful login")
        return page

    # ---------- operations ----------

    async def list_pending(self) -> list[Notification]:
        page = await self._goto_inbox()
        out: list[Notification] = []
        for a in await page.locator(LINK_SEL).all():
            m = ID_RE.search(await a.get_attribute("href") or "")
            if not m:
                continue
            title = (await a.inner_text()).strip()
            inc = INC_RE.search(title)
            out.append(Notification(title, m.group(1), m.group(2), inc.group(0) if inc else None))
        await self._save_state()
        return out

    async def acknowledge(self, n: Notification) -> None:
        page = await self._goto_inbox()
        await page.evaluate("([e, r]) => selectNotification(e, r)", [n.event_hist_id, n.recipient_hist_id])
        await page.wait_for_selector('input[name="optionHistId"]')
        radio = page.get_by_label(ACK_LABEL)
        if await radio.count() == 0:
            raise Mir3Error("No 'Acknowledge' option on reply page")
        await radio.first.check()
        async with page.expect_navigation():
            await page.click('input[name="saveNav"]')
        log.info("Submitted ack for %s", n.title)
