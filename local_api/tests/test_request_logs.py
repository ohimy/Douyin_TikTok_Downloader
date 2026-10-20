"""Request-log retention, legacy migration, and concrete address contracts."""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import httpx

from local_api.app import RequestAuditLog, create_app
from local_api.xhs_runtime import XhsRiskControlError, XhsRuntimeUnavailable


class RetentionTests(unittest.TestCase):
    def test_memory_disk_and_restart_retain_only_latest_100(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            audit = RequestAuditLog(Path(directory))
            for index in range(130):
                audit.record({"request_id": str(index), "path": f"/api/v1/xhs/notes/{index}"})
            self.assertEqual(len(audit.recent(200)), 100)
            self.assertEqual(audit.recent(200)[0]["request_id"], "129")
            self.assertEqual(audit.recent(200)[-1]["request_id"], "30")
            self.assertEqual(len(audit.path.read_text(encoding="utf-8").splitlines()), 100)
            restored = RequestAuditLog(Path(directory))
            self.assertEqual(restored.recent(100), audit.recent(100))

    def test_migration_trims_all_rotations_and_preserves_other_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            logs = Path(directory) / "logs"
            logs.mkdir()
            for suffix, indices in [(".2", range(10)), (".1", range(10, 70)), ("", range(70, 130))]:
                (logs / f"requests.jsonl{suffix}").write_text(
                    "".join(json.dumps({"request_id": str(index), "path": "/legacy"}) + "\n" for index in indices),
                    encoding="utf-8",
                )
            other_file = logs / "service.log"
            other_file.write_text("preserve", encoding="utf-8")
            audit = RequestAuditLog(Path(directory))
            self.assertEqual([item["request_id"] for item in audit.recent(100)], [str(index) for index in range(129, 29, -1)])
            self.assertFalse((logs / "requests.jsonl.1").exists())
            self.assertFalse((logs / "requests.jsonl.2").exists())
            self.assertEqual(len(audit.path.read_text(encoding="utf-8").splitlines()), 100)
            self.assertEqual(other_file.read_text(encoding="utf-8"), "preserve")


class AddressTests(unittest.IsolatedAsyncioTestCase):
    async def test_concrete_addresses_without_query_or_cookie(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = create_app(Path(directory))
            base_url = "http://192.168.1.20:8787"
            paths = [
                "/api/v1/xhs/users/real-user-id/posts",
                "/api/v1/xhs/notes/real-note-id",
                "/api/v1/douyin/users/real-sec-user-id/posts",
            ]
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url=base_url) as client:
                for path in paths:
                    response = await client.get(path, params={"xsec_token": "never-log-token"}, headers={"Cookie": "web_session=never-log-cookie"})
                    self.assertEqual(response.status_code, 409)  # no account; never contact upstream
                response = await client.get("/api/v1/logs/requests")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["retention"], {"max_records": 100})
                items = response.json()["items"]
                self.assertEqual([item["path"] for item in items], list(reversed(paths)))
                self.assertEqual([item["url"] for item in items], [base_url + path for path in reversed(paths)])
                contents = app.state.request_audit.path.read_text(encoding="utf-8")
                for secret in ["never-log-token", "never-log-cookie", "{user_id}", "{note_id}", "{sec_user_id}"]:
                    self.assertNotIn(secret, contents)
                self.assertEqual((await client.get("/api/v1/logs/requests?limit=101")).status_code, 422)
                self.assertEqual(len(app.state.request_audit.recent(100)), 3)


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.app = create_app(Path(temporary.name))
        self.app.state.xhs_request_gate.min_interval_seconds = 0
        account = await self.app.state.files.replace_cookie(
            "a1=fixture; web_session=fixture", "fixture", None, platform="xhs",
        )
        self.params = {"account_id": account["account_id"]}
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(self.app), base_url="http://local")
        self.addAsyncCleanup(self.client.aclose)
        self.addAsyncCleanup(self.app.state.xhs_read_cache.close)
        self.app.state.xhs_client.user_posts = Mock(return_value={"items": [], "cursor": None, "has_more": False})
        self.app.state.xhs_browser.user_posts = AsyncMock(return_value={"items": [], "cursor": None, "has_more": False})
        self.path = "/api/v1/xhs/users/creator/posts"

    def latest(self) -> dict:
        return self.app.state.request_audit.recent(1)[0]

    async def test_direct_request_and_cache_source_survive_restart(self) -> None:
        self.assertEqual((await self.client.get(self.path, params=self.params)).status_code, 200)
        self.assertEqual(self.latest()["transport"], "http")
        self.assertEqual(self.latest()["transport_attempts"], ["http"])
        self.assertEqual(self.latest()["cache_status"], "miss")
        self.assertEqual((await self.client.get(self.path, params=self.params)).status_code, 200)
        self.assertEqual(self.latest()["transport"], "http")
        self.assertEqual(self.latest()["cache_status"], "hit")
        self.app.state.xhs_client.user_posts.assert_called_once()
        restored = RequestAuditLog(self.app.state.files.data_dir)
        self.assertEqual(restored.recent(1)[0], self.latest())

    async def test_fallback_records_attempt_order_and_final_browser(self) -> None:
        self.app.state.xhs_client.user_posts.side_effect = XhsRuntimeUnavailable("unavailable")
        self.assertEqual((await self.client.get(self.path, params=self.params)).status_code, 200)
        self.assertEqual(self.latest()["transport"], "browser")
        self.assertEqual(self.latest()["transport_attempts"], ["http", "browser"])
        self.assertEqual((await self.client.get(self.path, params=self.params)).status_code, 200)
        self.assertEqual(self.latest()["transport_attempts"], ["http", "browser"])
        self.assertEqual(self.latest()["cache_status"], "hit")

    async def test_missing_token_records_only_browser(self) -> None:
        self.app.state.xhs_browser.note_detail = AsyncMock(return_value={"item": {"id": "note1"}})
        response = await self.client.get("/api/v1/xhs/notes/note1", params=self.params)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.latest()["transport"], "browser")
        self.assertEqual(self.latest()["transport_attempts"], ["browser"])

    async def test_cooldown_rejection_does_not_claim_a_new_http_request(self) -> None:
        self.app.state.xhs_client.user_posts.side_effect = XhsRiskControlError(461)
        self.assertEqual((await self.client.get(self.path, params=self.params)).status_code, 429)
        self.assertEqual(self.latest()["transport_attempts"], ["http"])
        self.assertEqual((await self.client.get(self.path, params=self.params)).status_code, 429)
        self.assertEqual(self.latest()["transport_attempts"], [])
        self.assertEqual(self.latest()["transport"], "none")

    async def test_shared_waiters_and_local_requests_have_independent_metadata(self) -> None:
        started, finish = asyncio.Event(), asyncio.Event()

        async def read_note(*_args, **_kwargs) -> dict:
            started.set()
            await finish.wait()
            return {"item": {"id": "note1"}}

        self.app.state.xhs_browser.note_detail = AsyncMock(side_effect=read_note)
        path = "/api/v1/xhs/notes/note1"
        first = asyncio.create_task(self.client.get(path, params=self.params))
        await started.wait()
        second = asyncio.create_task(self.client.get(path, params=self.params))
        await asyncio.sleep(0.02)
        local = await self.client.get("/api/v1/accounts")
        self.assertEqual(local.status_code, 200)
        self.assertEqual(self.latest()["transport"], "none")
        finish.set()
        results = await asyncio.gather(first, second)
        self.assertTrue(all(result.status_code == 200 for result in results))
        logs = [item for item in self.app.state.request_audit.recent(100) if item["path"] == path]
        self.assertEqual({item["cache_status"] for item in logs}, {"miss", "shared"})
        self.assertTrue(all(item["transport_attempts"] == ["browser"] for item in logs))
        self.assertNotEqual(logs[0]["request_id"], logs[1]["request_id"])
        self.app.state.xhs_browser.note_detail.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
