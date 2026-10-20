# Third-party sources and license notices

This document identifies the components used by the standalone Douyin /
Xiaohongshu Local API. Original license texts and copyright notices remain
authoritative; this inventory does not replace or modify them.

## Douyin_TikTok_Download_API

- Source: https://github.com/Evil0ctal/Douyin_TikTok_Download_API
- Copyright: 2021-2026 Evil0ctal and contributors
- License: Apache License, Version 2.0
- Applicable material: the forked repository and the unchanged Douyin signing
  modules used by the standalone application.
- Complete license and attribution: `LICENSE` and `NOTICE`.
- Local changes: `MODIFICATIONS.md`.

## Xiaohongshu signing runtime and adapted client portions

- Source: https://github.com/Nirvana919/XHS_Crawler
- Pinned source revision: `d96edb9584330622931064781db00c3f96fa2abe`
- Copyright: (c) 2026 Nirvana919
- License: MIT
- Runtime file: `Spider_XHS-xhs_new_api/static/xhs_main_260411.js` in that revision.
- Local adapter: `local_api/xhs_runtime.py`; local changes include integration
  with account storage, error classification, and common response parsing.
- Complete MIT copyright and permission text:
  `local_api/xhs_assets/THIRD_PARTY_NOTICES.md`.
- Original license:
  https://github.com/Nirvana919/XHS_Crawler/blob/d96edb9584330622931064781db00c3f96fa2abe/LICENSE

## Distributed runtimes and dependencies

The Windows build includes Python, a Node.js executable, Playwright's driver,
CryptoJS, and Python runtime dependencies such as FastAPI, Starlette, Uvicorn,
HTTPX, Requests, Pydantic, and PyExecJS. Each component retains its own license.

`tools/prepare_licenses.py` collects license/notice files from the actual build
environment, Python installation, Node.js version, and CryptoJS installation.
The generated `licenses/manifest.json` identifies their versions and documents.
These files are copied into the release's `licenses/` directory for document-only
attribution. Build-tool notices may also be included because the
collector retains notices from the dedicated packaging environment.

The application does not display attribution or provide a license viewer;
sources and original texts are recorded only in the accompanying documents.
When redistributing the application,
retain `LICENSE`, `NOTICE`, and the generated `licenses/` directory alongside
the executable, including all nested third-party notice files.

Chrome and Edge are detected or installed separately; they are not bundled
inside this project's executable. Their own distribution terms continue to
apply.
