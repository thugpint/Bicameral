# Security policy

## Supported versions

Only the latest release and the `main` branch receive fixes.

## Reporting a vulnerability

Do not open a public issue. Report privately through [GitHub security advisories](https://github.com/thugpint/Bicameral/security/advisories/new).

Include the version or commit, the backend in use (Codex CLI, headless Claude Code, or an API model), steps to reproduce, and what an attacker gains. You will get an acknowledgement within a week. Fixes are released as soon as they are ready, and you will be credited in the advisory unless you ask not to be.

## What counts

Bicameral runs model-written diffs against your repository and executes the test, gate, and verify commands you configure. Reports in scope include:

- A model or repository content escaping the declared file scope, the sandboxed git checkpoint, or the rollback.
- Prompt injection from repository content that changes what a command runs.
- Secrets leaking into logs, lessons, or the mirrored `.bicameral/lessons.md`.
- Unsafe handling of the configured test, gate, or verify commands.

Out of scope: the behaviour of the model providers themselves, and issues that require an attacker to already control the machine or the repository's configuration.
