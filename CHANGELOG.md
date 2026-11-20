# Changelog

All notable changes to this project are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project adheres to [Semantic Versioning](https://semver.org/).

## [1.1.1]

### Changed
- `libturbojpeg` is only required by `sae-watch` and `sae-record -d`; these exit with an installation hint if it is missing
- Bump visionlib to 1.2.0 (loads libturbojpeg lazily)

## [1.1.0]

### Added
- `sae-echo`: `-d/--dump-file` reads messages from a `.saedump` file instead of Valkey (prompts for streams)
- `sae-echo`: `-n/--max-messages` stops after N messages
- `--version` flag on all tools
- GitHub workflow for creating releases

### Changed
- `sae-echo` output is now JSON Lines (one object per line); IDs (`objectId`, `saeUuid`) are rendered as hex
- Bump visionlib to 1.1.2 (linear-time saedump splitter)

## [1.0.0]

Initial versioned release, containing `sae-echo`, `sae-play`, `sae-plot`, `sae-record` and `sae-watch`.
