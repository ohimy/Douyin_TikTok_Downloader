"""Offline contract tests; optional real Chromium tests use intercepted pages.

Run: .venv-local/bin/python -m unittest discover -s local_api/tests -v
Set RUN_PLAYWRIGHT_TESTS=1 to include actual browser click/scroll tests.
No test contacts Xiaohongshu or uses real account data.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock
from urllib.parse import parse_qs, urlparse

import httpx

from local_api.app import XhsReadCache, create_app
from local_api.xhs_browser import XhsBrowserReader, XhsBrowserUnavailable, XhsVerificationRequired
from local_api.xhs_runtime import XhsRiskControlError, XhsRuntimeUnavailable, XhsSessionExpiredError, XhsTokenRequiredError


class ReadingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = create_app(Path(self.temp.name))
        self.app.state.xhs_request_gate.min_interval_seconds = 0
        self.account = await self.app.state.files.replace_cookie(
            "a1=test-value; web_session=test-session", "test", None, platform="xhs",
        )
        self.account_id = self.account["account_id"]
        self.params = {"account_id": self.account_id}
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(self.app), base_url="http://local")
        self.addAsyncCleanup(self.client.aclose)
        self.addAsyncCleanup(self.app.state.xhs_browser.close)
        self.addAsyncCleanup(self.app.state.xhs_read_cache.close)
        self.app.state.xhs_client.user_posts = Mock(return_value={"items": [{"note_id": "n1", "xsec_token": "secret"}], "cursor": None, "has_more": False})
        self.app.state.xhs_browser.user_posts = AsyncMock(return_value={"items": [{"note_id": "n2", "xsec_token": "secret"}], "cursor": None, "has_more": False})
        self.app.state.xhs_browser.note_detail = AsyncMock(return_value={"item": {"id": "n1", "note_card": {"title": "test"}}})

    async def posts(self, user: str = "creator", **params: str) -> httpx.Response:
        return await self.client.get(f"/api/v1/xhs/users/{user}/posts", params={**self.params, **params})

    async def test_http_success_never_starts_browser_and_result_is_cached(self) -> None:
        one, two = await self.posts(), await self.posts()
        self.assertEqual(one.status_code, 200)
        self.assertEqual(one.json(), two.json())
        self.assertNotIn("xsec_token", str(one.json()))
        self.app.state.xhs_client.user_posts.assert_called_once()
        self.app.state.xhs_browser.user_posts.assert_not_awaited()

    async def test_signing_failure_switches_and_remembers_browser(self) -> None:
        self.app.state.xhs_client.user_posts.side_effect = XhsRuntimeUnavailable("signer unavailable")
        self.assertEqual((await self.posts()).status_code, 200)
        self.assertEqual((await self.posts("another")).status_code, 200)
        self.app.state.xhs_client.user_posts.assert_called_once()
        self.assertEqual(self.app.state.xhs_browser.user_posts.await_count, 2)
        status = self.app.state.xhs_auto_reader.status()["accounts"][0]
        self.assertEqual(status["preferred_transport"], "browser")

    async def test_failed_preferred_browser_returns_to_http(self) -> None:
        auto = self.app.state.xhs_auto_reader
        auto._preferences[(self.account_id, "user_posts")] = float("inf")
        self.app.state.xhs_browser.user_posts.side_effect = XhsBrowserUnavailable("browser unavailable")
        self.assertEqual((await self.posts()).status_code, 200)
        self.app.state.xhs_client.user_posts.assert_called_once()
        self.assertEqual(auto.status()["accounts"][0]["preferred_transport"], "http")

    async def test_461_does_not_retry_and_uses_browser_after_cooldown(self) -> None:
        self.app.state.xhs_client.user_posts.side_effect = XhsRiskControlError(461)
        response = await self.posts()
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers["retry-after"], "300")
        self.app.state.xhs_browser.user_posts.assert_not_awaited()
        self.assertEqual((await self.posts()).status_code, 429)
        self.app.state.xhs_request_gate._cooldown_until = 0
        self.assertEqual((await self.posts()).status_code, 200)
        self.app.state.xhs_client.user_posts.assert_called_once()
        self.app.state.xhs_browser.user_posts.assert_awaited_once()

    async def test_note_id_without_token_uses_browser(self) -> None:
        self.app.state.xhs_client.note_detail = Mock(side_effect=AssertionError("must not send empty token"))
        response = await self.client.get("/api/v1/xhs/notes/n1", params=self.params)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["item"]["id"], "n1")
        self.app.state.xhs_browser.note_detail.assert_awaited_once()

    async def test_expired_token_is_removed_before_browser_fallback(self) -> None:
        await self.app.state.files.cache_xhs_note_tokens(self.account_id, [{"note_id": "n1", "xsec_token": "old"}], source="pc_user")
        self.app.state.xhs_client.note_detail = Mock(side_effect=XhsTokenRequiredError(300031))
        response = await self.client.get("/api/v1/xhs/notes/n1", params=self.params)
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(self.app.state.files.xhs_note_token(self.account_id, "n1"))
        self.assertEqual(self.app.state.xhs_browser.note_detail.call_args.kwargs["xsec_token"], "")

    async def test_session_expiry_never_switches_transport(self) -> None:
        self.app.state.xhs_client.user_posts.side_effect = XhsSessionExpiredError("expired")
        self.assertEqual((await self.posts()).status_code, 409)
        self.app.state.xhs_browser.user_posts.assert_not_awaited()
        self.assertEqual(self.app.state.files.account(self.account_id)["status"], "needs_login")

    async def test_browser_verification_is_shown_and_does_not_fallback(self) -> None:
        self.app.state.xhs_auto_reader._preferences[(self.account_id, "user_posts")] = float("inf")
        self.app.state.xhs_browser.user_posts.side_effect = XhsVerificationRequired(461)
        self.assertEqual((await self.posts()).status_code, 429)
        self.app.state.xhs_client.user_posts.assert_not_called()
        self.assertEqual(self.app.state.files.account(self.account_id)["status"], "needs_verification")

    async def test_browser_cursor_never_goes_to_http(self) -> None:
        self.assertEqual((await self.posts(cursor="pw_cursor")).status_code, 200)
        self.app.state.xhs_client.user_posts.assert_not_called()

    async def test_cancelled_cache_waiter_does_not_cancel_other_waiters(self) -> None:
        cache = XhsReadCache()
        started, finish = asyncio.Event(), asyncio.Event()
        calls = 0

        async def loader() -> dict:
            nonlocal calls
            calls += 1
            started.set()
            await finish.wait()
            return {"items": [1]}

        one = asyncio.create_task(cache.get_or_load("k", ttl_seconds=60, loader=loader))
        await started.wait()
        two = asyncio.create_task(cache.get_or_load("k", ttl_seconds=60, loader=loader))
        await asyncio.sleep(0)
        one.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await one
        finish.set()
        self.assertEqual(await two, {"items": [1]})
        self.assertEqual(calls, 1)

    async def test_browser_page_split_keeps_every_note_and_is_account_scoped(self) -> None:
        browser = XhsBrowserReader(self.app.state.files, self.app.state.logins)
        browser._page = AsyncMock(return_value=object())
        browser._posts_page = AsyncMock(return_value={"items": [{"note_id": str(n)} for n in range(30)], "cursor": None, "has_more": False})
        first = await browser.user_posts(self.account_id, "creator", cursor="", count=20)
        second = await browser.user_posts(self.account_id, "creator", cursor=first["cursor"], count=20)
        self.assertEqual([x["note_id"] for x in first["items"] + second["items"]], [str(n) for n in range(30)])
        self.assertFalse(second["has_more"])
        browser._posts_page.assert_awaited_once()
        with self.assertRaises(XhsBrowserUnavailable):
            await browser.user_posts("another-account", "creator", cursor=first["cursor"], count=20)

    async def test_response_matching_rejects_unrelated_hosts_and_notes(self) -> None:
        response = SimpleNamespace(url="https://evil.example/api/sns/web/v1/user_posted?user_id=creator")
        self.assertFalse(XhsBrowserReader._matches(response, "posts", "creator"))
        response = SimpleNamespace(url="https://edith.xiaohongshu.com/api/sns/web/v1/feed", request=SimpleNamespace(post_data_json={"source_note_id": "other"}))
        self.assertFalse(XhsBrowserReader._matches(response, "note", "n1"))


@unittest.skipUnless(os.getenv("RUN_PLAYWRIGHT_TESTS") == "1", "optional real Chromium fixture")
class BrowserInteractionTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_click_and_response_capture_without_external_requests(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = create_app(Path(directory))
            account = await app.state.files.replace_cookie("a1=fixture; web_session=fixture", "fixture", None, platform="xhs")
            reader = app.state.xhs_browser
            try:
                page = await reader._page(account["account_id"])
                requests_seen: list[str] = []

                async def fixture(route: Any) -> None:
                    url = urlparse(route.request.url)
                    requests_seen.append(url.path)
                    headers = {"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "Content-Type", "Access-Control-Allow-Methods": "GET,POST,OPTIONS"}
                    if route.request.method == "OPTIONS":
                        await route.fulfill(status=204, headers=headers)
                        return
                    if url.path == "/api/sns/web/v1/user_posted":
                        is_next = bool(parse_qs(url.query).get("cursor"))
                        indices = range(30, 35) if is_next else range(30)
                        await route.fulfill(headers=headers, json={"success": True, "data": {
                            "notes": [{"note_id": f"n{i}"} for i in indices],
                            "cursor": None if is_next else "next", "has_more": not is_next,
                        }})
                    elif url.path == "/api/sns/web/v1/feed":
                        await route.fulfill(headers=headers, json={"success": True, "data": {"items": [{"id": "n1", "note_card": {"title": "clicked"}}]}})
                    elif url.path == "/user/profile/creator":
                        await route.fulfill(content_type="text/html", body='''<html><body>
                          <a href="/explore/n1?xsec_token=fixture">note</a>
                          <div style="height:4000px">scroll fixture</div>
                          <script>
                            fetch('https://edith.xiaohongshu.com/api/sns/web/v1/user_posted?user_id=creator&cursor=');
                            let more = true;
                            window.onscroll = () => {
                              if (!more) return; more = false;
                              fetch('https://edith.xiaohongshu.com/api/sns/web/v1/user_posted?user_id=creator&cursor=next');
                            };
                            document.querySelector('a').onclick = e => {
                              e.preventDefault(); fetch('https://edith.xiaohongshu.com/api/sns/web/v1/feed', {
                                method:'POST', headers:{'Content-Type':'application/json'},
                                body:JSON.stringify({source_note_id:'n1'})});
                            };
                          </script></body></html>''')
                    else:
                        await route.fulfill(status=404, body="fixture only")

                # Every request is fulfilled locally, including CORS preflights.
                await page.context.route("**/*", fixture)
                posts = await reader.user_posts(account["account_id"], "creator", cursor="", count=20)
                self.assertEqual(posts["items"][0]["note_id"], "n0")
                second = await reader.user_posts(account["account_id"], "creator", cursor=posts["cursor"], count=20)
                third = await reader.user_posts(account["account_id"], "creator", cursor=second["cursor"], count=20)
                self.assertEqual(len(posts["items"] + second["items"] + third["items"]), 35)
                self.assertFalse(third["has_more"])
                note = await reader.note_detail(account["account_id"], "n1")
                self.assertEqual(note["item"]["note_card"]["title"], "clicked")
                self.assertIn("/api/sns/web/v1/feed", requests_seen)
                self.assertNotIn("/explore/n1", requests_seen)  # click, not direct navigation
                self.assertIs(await reader._page(account["account_id"]), page)
            finally:
                await reader.close()


if __name__ == "__main__":
    unittest.main()
