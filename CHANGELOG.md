# Changelog

All notable changes to this project are documented here. This project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html) and [Keep a
Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

## [0.1.0] - TBD

### Added
- Async Python client for Elegoo Centauri Carbon printers speaking SDCP v3 over
  WebSocket (`ws://<host>:3030/websocket`).
- UDP broadcast discovery on port 3000 with the `M99999` probe.
- MJPEG snapshot grabber for the built-in webcam
  (`/network-device-manager/network/camera`).
- `centauri` CLI with `discover`, `status`, `watch`, `snapshot`, `attributes`,
  `files`, `print {start,pause,resume,stop}`, `upload`, `mcp`.
- Optional MCP server (`python -m pycentauri.mcp`) with read-only tools by
  default; control tools registered only when `--enable-control` is set.
- Apache-2.0 license.
