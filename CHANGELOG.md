# Changelog

All notable changes to Bicameral are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/). Before 1.0, minor versions may break
interfaces.

## [Unreleased]

## [0.1.0] - 2026-09-06

First public release.

### Added

- Architect/Editor loop for Claude Code: Claude plans and reviews, a second model writes the diffs.
- Editor backends: Codex CLI (ChatGPT plan), headless Claude Code, and Anthropic or OpenAI API keys.
- Local MCP server that routes each step, runs the test command, rolls back rejected or failing edits, and retries with feedback up to three times.
- Deterministic gates (lint, typecheck, build) that run before any model review; a gate that cannot start is a configuration error, not a pass.
- Tests-first steps that must leave the suite red, with the implementing step blocked from editing test files.
- Git checkpoints and per-step commits.
- Editor plan critique, cross-review of the Architect's own edits, attached concerns, and recorded disputes.
- Learned routing record of which model is accepted most per step kind, plus scored lessons from both models.
- Browser GUI, Textual TUI, and `bicameral` / `bicameral-mcp` entry points.
- 88 offline tests against scripted fake backends.

[Unreleased]: https://github.com/Devilz06/Bicameral/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/Devilz06/Bicameral/releases/tag/v0.1.0
