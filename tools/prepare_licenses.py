"""Collect actual build-environment licenses for the standalone distribution.

Local API fork addition (2026-10-20); see MODIFICATIONS.md.
Run from the repository root: python -m tools.prepare_licenses
Windows builds additionally pass --node-executable to include that Node build.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import re
import shutil
import subprocess
import sys
import sysconfig
import tempfile
from pathlib import Path, PurePath
from urllib.request import urlopen

CORE_DOCUMENTS = (
    ("project-license", "项目许可证 · Apache-2.0", "LICENSE"),
    ("upstream-notice", "原作者版权与 NOTICE", "NOTICE"),
    ("modifications", "本地版本修改说明", "MODIFICATIONS.md"),
    ("third-party", "第三方来源与归属", "THIRD_PARTY_NOTICES.md"),
    ("xhs-license", "小红书运行时 · MIT", "local_api/xhs_assets/THIRD_PARTY_NOTICES.md"),
)


def source_root() -> Path:
    return Path(__file__).resolve().parent.parent


def is_notice_file(name: str) -> bool:
    basename = PurePath(name).name.upper()
    return (
        basename.startswith(("LICENSE", "LICENCE", "COPYING", "NOTICE", "AUTHORS", "COPYRIGHT", "THIRDPARTYNOTICES", "THIRD_PARTY_NOTICES"))
        or basename.endswith(".LICENSE")
    ) and not basename.endswith((".PY", ".PYC", ".JSON"))


class LicenseBundle:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.documents: list[dict[str, str]] = []

    def add(self, document_id: str, title: str, source: Path, *, component: str = "", version: str = "") -> None:
        if not source.is_file() or not source.stat().st_size:
            raise RuntimeError(f"Required license file is missing or empty: {source}")
        target = self.directory / "texts" / document_id / source.name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        self.documents.append({
            "id": document_id,
            "title": title,
            "file": target.relative_to(self.directory).as_posix(),
            "component": component,
            "version": version,
        })


def collect_python_dependencies(bundle: LicenseBundle) -> None:
    """Keep full installed license/NOTICE files, including vendored drivers."""
    for distribution in sorted(importlib.metadata.distributions(), key=lambda item: item.metadata["Name"].lower()):
        name = distribution.metadata["Name"]
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
        files = [item for item in distribution.files or [] if is_notice_file(str(item))]
        if not files:
            raise RuntimeError(f"No complete license text found for {name} {distribution.version}; prepare its license before distributing.")
        for relative in sorted(files, key=str):
            digest = hashlib.sha256(str(relative).encode()).hexdigest()[:12]
            bundle.add(
                f"python-{slug}-{digest}", f"{name} {distribution.version} · {relative}",
                Path(distribution.locate_file(relative)), component=name, version=distribution.version,
            )


def collect_python_runtime(bundle: LicenseBundle) -> None:
    candidates = [Path(sys.base_prefix) / "LICENSE.txt", Path(sys.base_prefix) / "LICENSE", Path(sysconfig.get_path("stdlib")) / "LICENSE.txt"]
    source = next((path for path in candidates if path.is_file()), None)
    if source is None:
        raise RuntimeError("The build Python installation is missing its LICENSE text.")
    bundle.add("python-runtime", f"Python {sys.version.split()[0]} · 许可证", source, component="Python", version=sys.version.split()[0])
    # Windows Python distributions install Tcl/Tk alongside the interpreter.
    for prefix in (Path(sys.base_prefix) / "tcl", Path(sys.base_prefix) / "lib"):
        if prefix.is_dir():
            for path in sorted(prefix.glob("t[ck]l*/license.terms")) + sorted(prefix.glob("tk*/license.terms")):
                digest = hashlib.sha256(str(path.relative_to(prefix)).encode()).hexdigest()[:12]
                bundle.add(f"tcl-tk-{digest}", f"Tcl/Tk · {path.parent.name}", path, component="Tcl/Tk")


def collect_node_runtime(bundle: LicenseBundle, executable: Path) -> None:
    version = subprocess.run([str(executable), "--version"], check=True, capture_output=True, text=True).stdout.strip()
    if not re.fullmatch(r"v\d+\.\d+\.\d+", version):
        raise RuntimeError("Cannot determine the bundled Node.js version.")
    candidates = [executable.parent / "LICENSE", executable.parent / "LICENSE.txt"]
    source = next((path for path in candidates if path.is_file()), None)
    if source is None:
        # Winget packages do not all install license files. Fetch only the
        # complete license from the exact Node tag, never the moving main tag.
        source = bundle.directory / "node-license" / "LICENSE"
        source.parent.mkdir(parents=True, exist_ok=True)
        with urlopen(f"https://raw.githubusercontent.com/nodejs/node/{version}/LICENSE", timeout=60) as response:
            content = response.read()
        if b"Node.js" not in content or b"Permission is hereby granted" not in content:
            raise RuntimeError("Node.js license download did not contain the expected license text.")
        source.write_bytes(content)
    bundle.add("node-runtime", f"Node.js {version} · 许可证与第三方声明", source, component="Node.js", version=version)


def prepare_licenses(output: Path, *, node_executable: Path | None = None) -> dict:
    root = source_root()
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    # Validate and collect everything before replacing the published manifest.
    with tempfile.TemporaryDirectory(prefix="license-stage-", dir=output.parent) as temporary:
        stage = Path(temporary)
        bundle = LicenseBundle(stage)
        for document_id, title, relative in CORE_DOCUMENTS:
            bundle.add(document_id, title, root / relative, component="Local API")
        collect_python_runtime(bundle)
        collect_python_dependencies(bundle)
        crypto = root / "local_api" / "xhs_assets" / "node_modules" / "crypto-js"
        package = json.loads((crypto / "package.json").read_text(encoding="utf-8"))
        bundle.add("crypto-js", f"CryptoJS {package['version']} · MIT", crypto / "LICENSE", component="crypto-js", version=package["version"])
        if node_executable is not None:
            collect_node_runtime(bundle, node_executable.resolve())
        manifest = {"format_version": 1, "generator": "local-api-license-collector", "documents": bundle.documents}
        output.mkdir(parents=True, exist_ok=True)
        for entry in bundle.documents:
            target = output / entry["file"]
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(stage / entry["file"], target)
        manifest_path = output / "manifest.json"
        old = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {}
        new_files = {entry["file"] for entry in bundle.documents}
        if old.get("generator") == manifest["generator"]:
            for entry in old.get("documents", []):
                if entry.get("file") not in new_files:
                    candidate = (output / entry["file"]).resolve()
                    candidate.relative_to(output)
                    candidate.unlink(missing_ok=True)
        # The manifest exposes only this build's current, validated documents.
        (stage / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        shutil.copyfile(stage / "manifest.json", manifest_path)
        return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=source_root() / "build" / "local-api-licenses")
    parser.add_argument("--node-executable", type=Path)
    args = parser.parse_args()
    manifest = prepare_licenses(args.output, node_executable=args.node_executable)
    print(f"Prepared {len(manifest['documents'])} license/notice documents in {args.output}")


if __name__ == "__main__":
    main()
