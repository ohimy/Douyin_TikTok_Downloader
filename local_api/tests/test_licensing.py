"""Document-only attribution and distribution license collection contracts."""

from __future__ import annotations

import ast
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from local_api.app import create_app
from tools import prepare_licenses as collector


class DocumentationTests(unittest.TestCase):
    def test_source_preserves_original_license_and_notice(self) -> None:
        documents = {document_id: collector.source_root() / relative for document_id, _, relative in collector.CORE_DOCUMENTS}
        self.assertIn("Apache License", documents["project-license"].read_text(encoding="utf-8"))
        self.assertTrue(documents["upstream-notice"].read_text(encoding="utf-8").startswith(
            "Douyin_TikTok_Download_API\nCopyright 2021-2026 Evil0ctal and contributors\n"
        ))
        mit = documents["xhs-license"].read_text(encoding="utf-8")
        self.assertIn("Copyright (c) 2026 Nirvana919", mit)
        self.assertIn("Permission is hereby granted", mit)

    def test_native_window_has_no_attribution_viewer(self) -> None:
        source = (collector.source_root() / "local_api/desktop.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        self.assertNotIn("show_about", {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)})
        self.assertNotIn("关于 / 开源许可", source)
        self.assertFalse(any(isinstance(node, ast.ImportFrom) and node.module == "local_api.licensing" for node in ast.walk(tree)))


class CollectorTests(unittest.TestCase):
    def test_node_uses_exact_runtime_version_and_full_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = collector.LicenseBundle(root / "bundle")
            executable = root / "node.exe"
            content = b"Node.js\nPermission is hereby granted\nThird-party original notices\n"
            with patch.object(collector.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "v20.12.2\n")), patch.object(
                collector, "urlopen", return_value=io.BytesIO(content)
            ) as download:
                collector.collect_node_runtime(bundle, executable)
            download.assert_called_once_with("https://raw.githubusercontent.com/nodejs/node/v20.12.2/LICENSE", timeout=60)
            entry = bundle.documents[0]
            self.assertEqual(entry["version"], "v20.12.2")
            self.assertEqual((bundle.directory / entry["file"]).read_bytes(), content)

    def test_node_reads_installed_license_without_network(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            content = "Installed runtime license\n"
            (root / "LICENSE").write_text(content, encoding="utf-8")
            bundle = collector.LicenseBundle(root / "bundle")
            with patch.object(collector.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "v22.0.0\n")), patch.object(
                collector, "urlopen", side_effect=AssertionError("Must not download")
            ):
                collector.collect_node_runtime(bundle, root / "node.exe")
            self.assertEqual((bundle.directory / bundle.documents[0]["file"]).read_text(encoding="utf-8"), content)

    def test_rebuild_removes_only_stale_generated_documents(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            crypto = root / "local_api/xhs_assets/node_modules/crypto-js"
            crypto.mkdir(parents=True)
            (crypto / "LICENSE").write_text("MIT crypto test license", encoding="utf-8")
            (crypto / "package.json").write_text('{"version":"4.2.0"}', encoding="utf-8")
            for _, _, relative in collector.CORE_DOCUMENTS:
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("Original copyright and license", encoding="utf-8")
            output = root / "output"
            output.mkdir()
            unrelated = output / "user-notes.txt"
            unrelated.write_text("preserve", encoding="utf-8")
            stale = output / "stale.txt"
            stale.write_text("old notice", encoding="utf-8")
            (output / "manifest.json").write_text(json.dumps({
                "generator": "local-api-license-collector", "documents": [{"file": "stale.txt"}],
            }), encoding="utf-8")
            with patch.object(collector, "source_root", return_value=root), patch.object(
                collector, "collect_python_runtime"
            ), patch.object(collector, "collect_python_dependencies"):
                manifest = collector.prepare_licenses(output)
            self.assertFalse(stale.exists())
            self.assertEqual(unrelated.read_text(encoding="utf-8"), "preserve")
            self.assertEqual(json.loads((output / "manifest.json").read_text(encoding="utf-8")), manifest)
            self.assertEqual(len(manifest["documents"]), len(collector.CORE_DOCUMENTS) + 1)
            for entry in manifest["documents"]:
                text = (output / entry["file"]).read_text(encoding="utf-8")
                self.assertEqual(text, "MIT crypto test license" if entry["id"] == "crypto-js" else "Original copyright and license")


class EndpointTests(unittest.IsolatedAsyncioTestCase):
    async def test_attribution_is_not_exposed_in_ui_api_or_schema(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = create_app(Path(directory))
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://local") as client:
                for path in ("/dashboard", "/accounts"):
                    response = await client.get(path)
                    self.assertEqual(response.status_code, 200)
                    self.assertNotIn("/about", response.text)
                    self.assertNotIn("开源许可", response.text)
                for path in ("/about", "/api/v1/licenses", "/api/v1/licenses/project-license"):
                    self.assertEqual((await client.get(path)).status_code, 404)
                schema = (await client.get("/openapi.json")).json()
                self.assertEqual(schema["info"]["version"], "1.1.0")
                self.assertFalse(any(path == "/about" or path.startswith("/api/v1/licenses") for path in schema["paths"]))
                self.assertIn("/api/v1/xhs/notes/{note_id}", schema["paths"])
                self.assertIn("/api/v1/douyin/videos/{aweme_id}", schema["paths"])
                self.assertEqual((await client.get("/health")).status_code, 200)


if __name__ == "__main__":
    unittest.main()
