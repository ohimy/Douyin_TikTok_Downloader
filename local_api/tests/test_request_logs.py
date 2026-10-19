"""Request-log retention, legacy migration, and concrete address contracts."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import httpx

from local_api.app import RequestAuditLog, create_app


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


if __name__ == "__main__":
    unittest.main()
