"""XHS reads through persistent Playwright pages and real page interactions.

No API request is constructed in this module. Responses are observed after
navigation, clicking a note card, or scrolling the public page. Sessions are
isolated by account and released before login/deletion and at server shutdown.
"""

from __future__ import annotations

import asyncio
import copy
import re
import secrets
import time
from collections import OrderedDict
from contextlib import suppress
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlparse

from local_api.xhs_runtime import (
    XhsClient,
    XhsRiskControlError,
    XhsSessionExpiredError,
    XhsTokenRequiredError,
)


class XhsBrowserUnavailable(RuntimeError):
    pass


class XhsVerificationRequired(XhsRiskControlError):
    """A browser page requires the account holder to complete verification."""


class XhsBrowserReader:
    def __init__(self, files: Any, logins: Any) -> None:
        self.files = files
        self.logins = logins
        self._playwright: Any = None
        self._startup_lock = asyncio.Lock()
        self._sessions: dict[str, tuple[Any, Any]] = {}
        self._session_cookies: dict[str, str] = {}
        self._positions: dict[str, tuple[str, str | None]] = {}
        self._pages: OrderedDict[tuple[str, str, str], tuple[float, dict[str, Any]]] = OrderedDict()
        self._cursors: OrderedDict[str, tuple[float, str, str, list[Any], str | None]] = OrderedDict()

    def _lock(self, account_id: str) -> asyncio.Lock:
        return self.logins._xhs_resolution_locks.setdefault(account_id, asyncio.Lock())

    @staticmethod
    def _identifier(identifier: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", identifier):
            raise ValueError("小红书 ID 格式无效。")
        return identifier

    async def _page(self, account_id: str) -> Any:
        task = self.logins._tasks.get(account_id)
        if task is not None and not task.done():
            raise XhsBrowserUnavailable("该账号正在登录，请完成登录后再读取。")
        account = self.files.account(account_id)
        if account is None or account.get("platform") != "xhs":
            raise XhsSessionExpiredError("小红书账号不存在，请先登录。")
        if account.get("status") in {"needs_login", "needs_verification", "logging_in"}:
            raise XhsSessionExpiredError("小红书账号需要登录或完成验证。")
        cookie = self.files.read_cookie(account_id, platform="xhs")
        if not cookie:
            raise XhsSessionExpiredError("小红书账号没有保存的 Cookie。")
        session = self._sessions.get(account_id)
        if session is not None and not session[1].is_closed() and self._session_cookies.get(account_id) == cookie:
            return session[1]
        if session is not None:
            await self.close_account_locked(account_id)
        async with self._startup_lock:
            if self._playwright is None:
                try:
                    from playwright.async_api import async_playwright

                    self._playwright = await async_playwright().start()
                except Exception as exc:
                    raise XhsBrowserUnavailable("Playwright 运行环境不可用。") from exc
        profile = self.files._profile_dir_for_account(account_id)
        if profile is None:
            raise XhsBrowserUnavailable("账号浏览器目录无效。")
        context = None
        try:
            context = await self._playwright.chromium.launch_persistent_context(
                str(profile), headless=True, **self.logins._browser_launch_options(),
            )
            # Import only this account's saved session; never use the user's
            # personal Chrome profile or a different account's Cookie jar.
            await context.clear_cookies()
            await context.add_cookies([
                {"name": name, "value": value, "domain": ".xiaohongshu.com", "path": "/", "secure": True}
                for name, value in XhsClient._cookie_map(cookie).items()
            ])
            page = context.pages[0] if context.pages else await context.new_page()
            page.set_default_timeout(8000)
            self._sessions[account_id] = (context, page)
            self._session_cookies[account_id] = cookie
            return page
        except Exception as exc:
            if context is not None:
                with suppress(Exception):
                    await context.close()
            if isinstance(exc, XhsSessionExpiredError):
                raise
            raise XhsBrowserUnavailable("无法启动账号浏览器，请检查 Chrome / Edge 是否可用。") from exc

    async def _check_page(self, page: Any) -> None:
        parsed = urlparse(page.url)
        query = parse_qs(parsed.query)
        code = (query.get("error_code") or [""])[0]
        if code == "300012":
            raise XhsRiskControlError(200, code)
        if "captcha" in parsed.path or code in {"300017", "300034"}:
            raise XhsVerificationRequired(461)
        if code in {"300031", "300033"} or parsed.path.startswith("/404"):
            raise XhsTokenRequiredError(code)
        if parsed.path.startswith("/website-login/login"):
            raise XhsSessionExpiredError("小红书网页登录已失效，请重新登录。")
        text = await page.locator("body").inner_text(timeout=3000)
        if len(text) < 1000 and re.search(r"安全验证|请完成验证|访问过于频繁|操作过于频繁|Security Verification", text):
            raise XhsVerificationRequired(461)

    @staticmethod
    def _matches(response: Any, kind: str, identifier: str) -> bool:
        parsed = urlparse(response.url)
        if parsed.hostname not in {"edith.xiaohongshu.com", "www.xiaohongshu.com"}:
            return False
        if kind == "posts" and parsed.path == "/api/sns/web/v1/user_posted":
            return (parse_qs(parsed.query).get("user_id") or [""])[0] == identifier
        if kind == "note" and parsed.path == "/api/sns/web/v1/feed":
            try:
                return response.request.post_data_json.get("source_note_id") == identifier
            except (ValueError, AttributeError):
                return False
        return False

    async def _payload(self, response: Any) -> dict[str, Any]:
        if response.status == 429:
            raise XhsRiskControlError(429)
        if response.status in {461, 471}:
            raise XhsVerificationRequired(response.status)
        if response.status in {401, 403}:
            raise XhsSessionExpiredError("小红书网页登录已失效或无访问权限。")
        if response.status >= 400:
            raise XhsBrowserUnavailable("小红书页面读取返回异常状态。")
        try:
            payload = await response.json()
        except Exception as exc:
            raise XhsBrowserUnavailable("小红书页面未返回 JSON 数据。") from exc
        if not isinstance(payload, dict):
            raise XhsBrowserUnavailable("小红书页面数据格式异常。")
        XhsClient._reject_if_failed(payload)
        return payload

    async def _wait_response(self, page: Any, queue: asyncio.Queue[Any]) -> Any:
        # Check redirects/challenges while awaiting a page-generated response.
        # Every wait is bounded; a blocked page never becomes an empty success.
        for _ in range(20):
            if not queue.empty():
                return queue.get_nowait()
            await self._check_page(page)
            try:
                return await asyncio.wait_for(queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                pass
        raise XhsBrowserUnavailable("等待小红书页面数据超时，请稍后重试。")

    def _remember_page(self, key: tuple[str, str, str], result: dict[str, Any]) -> None:
        self._pages[key] = (time.monotonic() + 120, copy.deepcopy(result))
        self._pages.move_to_end(key)
        while len(self._pages) > 100:
            self._pages.popitem(last=False)

    async def _initial_posts(self, page: Any) -> dict[str, Any] | None:
        notes = await page.evaluate("""() => {
            const unwrap = v => v?._rawValue ?? v?._value ?? v;
            let notes = unwrap(window.__INITIAL_STATE__?.user?.notes);
            if (!Array.isArray(notes)) return null;
            notes = Array.isArray(notes[0]) ? notes[0] : notes;
            return notes.filter(n => n && (n.noteId || n.note_id || n.id));
        }""")
        if not isinstance(notes, list) or not notes:
            return None
        return {
            "items": self._snake_fields(notes),
            # The next page must be obtained by scrolling the retained page;
            # no undocumented server cursor is guessed from rendered notes.
            "cursor": "__page_next__", "has_more": True,
        }

    async def _posts_page(self, page: Any, account_id: str, user_id: str, cursor: str) -> dict[str, Any]:
        key = (account_id, user_id, cursor)
        cached = self._pages.get(key)
        if cached and cached[0] > time.monotonic():
            return copy.deepcopy(cached[1])
        queue: asyncio.Queue[Any] = asyncio.Queue()

        def capture(response: Any) -> None:
            if self._matches(response, "posts", user_id):
                queue.put_nowait(response)

        page.on("response", capture)
        try:
            on_profile = urlparse(page.url).path == f"/user/profile/{user_id}"
            resume = on_profile and bool(cursor) and self._positions.get(account_id) == (user_id, cursor)
            if resume:
                await self._check_page(page)
                await page.mouse.wheel(0, 10000)
            else:
                if cursor == "__page_next__":
                    raise XhsBrowserUnavailable("浏览器页面已切换，请从主页第一页重新读取。")
                await page.goto(f"https://www.xiaohongshu.com/user/profile/{quote(user_id)}", wait_until="domcontentloaded", timeout=30000)
            # Only real page scrolling is used. A foreign HTTP cursor can be
            # recovered by replaying at most eight pages, never by fabricating
            # a browser request or silently returning the first page instead.
            for index in range(8):
                try:
                    response = await self._wait_response(page, queue)
                except XhsBrowserUnavailable:
                    if not cursor and index == 0:
                        initial = await self._initial_posts(page)
                        if initial is not None:
                            self._remember_page(key, initial)
                            self._positions[account_id] = (user_id, initial["cursor"])
                            return initial
                    text = await page.locator("body").inner_text(timeout=3000)
                    if re.search(r"没有更多了|已加载全部|还没有发布过笔记|暂无笔记", text):
                        return {"items": [], "cursor": None, "has_more": False}
                    raise
                requested_cursor = (parse_qs(urlparse(response.url).query).get("cursor") or [""])[0]
                result = XhsClient.parse_posts(await self._payload(response))
                self._remember_page((account_id, user_id, requested_cursor), result)
                self._positions[account_id] = (user_id, result["cursor"])
                if requested_cursor == cursor or (resume and cursor == "__page_next__"):
                    return result
                if not result["has_more"] or index == 7:
                    break
                await page.wait_for_timeout(8000)
                await page.mouse.wheel(0, 10000)
            raise XhsBrowserUnavailable("浏览器未能定位此分页游标，请从主页第一页重新读取。")
        finally:
            page.remove_listener("response", capture)

    async def user_posts(self, account_id: str, user_id: str, *, cursor: str, count: int, **_kwargs: Any) -> dict[str, Any]:
        self._identifier(user_id)
        async with self._lock(account_id):
            page = await self._page(account_id)
            if cursor.startswith("pw_"):
                saved = self._cursors.get(cursor)
                if saved is None or saved[0] <= time.monotonic() or saved[1:3] != (account_id, user_id):
                    raise XhsBrowserUnavailable("浏览器分页已过期，请从主页第一页重新读取。")
                _, _, _, pending, upstream_cursor = saved
                pending = copy.deepcopy(pending)
                if not pending and upstream_cursor is not None:
                    result = await self._posts_page(page, account_id, user_id, upstream_cursor)
                    pending, upstream_cursor = result["items"], result["cursor"]
            else:
                result = await self._posts_page(page, account_id, user_id, cursor)
                pending, upstream_cursor = result["items"], result["cursor"]
            items, remaining = pending[:count], pending[count:]
            has_more = bool(remaining) or upstream_cursor is not None
            next_cursor = None
            if has_more:
                next_cursor = "pw_" + secrets.token_urlsafe(18)
                self._cursors[next_cursor] = (time.monotonic() + 1800, account_id, user_id, remaining, upstream_cursor)
                while len(self._cursors) > 500:
                    self._cursors.popitem(last=False)
            return {"items": items, "cursor": next_cursor, "has_more": has_more}

    async def note_detail(self, account_id: str, note_id: str, *, xsec_token: str = "", xsec_source: str = "pc_user") -> dict[str, Any]:
        self._identifier(note_id)
        async with self._lock(account_id):
            page = await self._page(account_id)
            queue: asyncio.Queue[Any] = asyncio.Queue()

            def capture(response: Any) -> None:
                if self._matches(response, "note", note_id):
                    queue.put_nowait(response)

            page.on("response", capture)
            try:
                # Prefer an actual note card on the retained page, preserving
                # its current link/token. A direct URL is the bounded fallback.
                card = page.locator(f'a[href*="/explore/{note_id}"], a[href*="/discovery/item/{note_id}"]')
                if await card.count():
                    await card.first.click()
                else:
                    fields = {"xsec_token": xsec_token, "xsec_source": xsec_source} if xsec_token else {}
                    suffix = "?" + urlencode(fields) if fields else ""
                    await page.goto(f"https://www.xiaohongshu.com/explore/{quote(note_id)}{suffix}", wait_until="domcontentloaded", timeout=30000)
                await self._check_page(page)
                if not queue.empty():
                    return XhsClient.parse_note(await self._payload(queue.get_nowait()))
                # Some note pages render server-provided state without issuing
                # a feed XHR. Read only the matching note, never an unrelated one.
                note = await page.evaluate("""id => {
                    const s = window.__INITIAL_STATE__;
                    const n = s?.note?.noteDetailMap?.[id]?.note;
                    return n && (n.noteId === id || n.note_id === id) ? n : null;
                }""", note_id)
                if isinstance(note, dict):
                    return {"item": {"id": note_id, "note_card": self._snake_fields(note)}}
                response = await self._wait_response(page, queue)
                return XhsClient.parse_note(await self._payload(response))
            except (XhsRiskControlError, XhsSessionExpiredError, XhsTokenRequiredError, XhsBrowserUnavailable):
                raise
            except Exception as exc:
                raise XhsBrowserUnavailable("小红书浏览器页面操作失败，请稍后重试。") from exc
            finally:
                page.remove_listener("response", capture)

    @classmethod
    def _snake_fields(cls, value: Any) -> Any:
        """Align server-rendered camelCase fields with the JSON API response."""
        if isinstance(value, dict):
            return {re.sub(r"(?<!^)(?=[A-Z])", "_", key).lower(): cls._snake_fields(item) for key, item in value.items()}
        if isinstance(value, list):
            return [cls._snake_fields(item) for item in value]
        return value

    async def close_account(self, account_id: str) -> None:
        async with self._lock(account_id):
            await self.close_account_locked(account_id)

    async def close_account_locked(self, account_id: str) -> None:
        """Release a profile while its shared account lock is already held."""
        session = self._sessions.pop(account_id, None)
        self._session_cookies.pop(account_id, None)
        self._positions.pop(account_id, None)
        if session is not None:
            with suppress(Exception):
                await session[0].close()
        for key in list(self._pages):
            if key[0] == account_id:
                self._pages.pop(key, None)
        for key, saved in list(self._cursors.items()):
            if saved[1] == account_id:
                self._cursors.pop(key, None)

    async def close(self) -> None:
        for account_id in list(self._sessions):
            await self.close_account(account_id)
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None
