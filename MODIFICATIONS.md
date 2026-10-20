# Local API fork: source and modification notice

This repository is a derivative of
[Evil0ctal/Douyin_TikTok_Download_API](https://github.com/Evil0ctal/Douyin_TikTok_Download_API).
The original copyright is retained:

Copyright 2021-2026 Evil0ctal and contributors

The original Apache License, Version 2.0 is preserved in `LICENSE`, and the
upstream attribution is preserved in `NOTICE`. This fork does not claim the
original project as its own original work or imply upstream endorsement.

## Local changes (notice updated 2026-10-20)

The standalone application is maintained in
[ohimy/Douyin_TikTok_Downloader](https://github.com/ohimy/Douyin_TikTok_Downloader).
The following additions and modifications belong to the local API fork:

- `local_api/`: standalone intranet HTTP service, Douyin public-read adapters,
  isolated account login and Cookie management, account checks, web dashboard,
  native desktop management, and request logging. Attribution is maintained in
  project documentation, not displayed inside the application.
- Xiaohongshu integration: an adapter using the separately licensed MIT signing
  runtime, plus Playwright page reading, automatic transport selection,
  request pacing, cooldown persistence, and response caching.
- `desktop_main.py`, `local_api/main.py`, and `build_windows.bat`: local service
  entry points and Windows application packaging/prerequisite installation.
- `tools/fetch_xhs_assets.py`, `tools/prepare_licenses.py`, and
  `README.LOCAL.zh-CN.md`: pinned third-party asset preparation, distribution
  license collection, and standalone application documentation.
- `README.md` and `README.zh-CN.md`: added fork attribution and a link to the
  standalone application documentation; upstream material is retained below.

The Douyin signing modules in `src/dtk/signing/native/` are reused from the
upstream project without asserting new authorship of those modules.

Local additions use the repository's Apache-2.0 license unless a file or
component states otherwise. Third-party components retain their own terms and
copyright notices; see `THIRD_PARTY_NOTICES.md` and the complete license texts
included in release packages. This modification notice does not relicense
third-party code or replace the original `NOTICE`.
