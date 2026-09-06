"""Install the /bicameral skill and the MCP server into Claude Code."""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .providers.claude_cli import clean_env, find_executable

SERVER_NAME = "bicameral"


def skill_source() -> Path:
    return Path(__file__).parent / "skill" / "SKILL.md"


def skills_root() -> Path:
    return Path.home() / ".claude" / "skills"


def skill_target() -> Path:
    return skills_root() / SERVER_NAME / "SKILL.md"


def mcp_command() -> list[str]:
    return [sys.executable, "-m", "bicameral.mcp_server"]


@dataclass
class InstallState:
    skill_installed: bool
    mcp_installed: bool
    claude_found: bool
    detail: str = ""


def install_skill(root: Path | None = None) -> Path:
    target = (root or skills_root()) / SERVER_NAME / "SKILL.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(skill_source(), target)
    return target


def uninstall_skill(root: Path | None = None) -> bool:
    target = (root or skills_root()) / SERVER_NAME / "SKILL.md"
    if target.exists():
        target.unlink()
        try:
            target.parent.rmdir()
        except OSError:
            pass
        return True
    return False


def _claude(args: list[str], runner=subprocess.run) -> subprocess.CompletedProcess:
    exe = find_executable()
    if not exe:
        raise FileNotFoundError("claude executable not found on PATH")
    return runner([exe, *args], capture_output=True, text=True, encoding="utf-8", errors="replace", env=clean_env(), timeout=60)


def mcp_installed(runner=subprocess.run) -> bool:
    try:
        return _claude(["mcp", "get", SERVER_NAME], runner).returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False


def install_mcp(scope: str = "user", runner=subprocess.run) -> str:
    """Register the server with `claude mcp add`; replaces an existing registration."""
    try:
        if mcp_installed(runner):
            _claude(["mcp", "remove", "-s", scope, SERVER_NAME], runner)
        proc = _claude(["mcp", "add", "-s", scope, SERVER_NAME, "--", *mcp_command()], runner)
    except FileNotFoundError:
        return manual_mcp_instructions()
    except (subprocess.TimeoutExpired, OSError) as e:
        return f"claude mcp add failed: {e}\n" + manual_mcp_instructions()
    if proc.returncode != 0:
        return f"claude mcp add failed: {(proc.stderr or proc.stdout).strip()[-400:]}\n" + manual_mcp_instructions()
    return (proc.stdout or "").strip() or f"registered MCP server '{SERVER_NAME}' ({scope} scope)"


def uninstall_mcp(scope: str = "user", runner=subprocess.run) -> str:
    try:
        proc = _claude(["mcp", "remove", "-s", scope, SERVER_NAME], runner)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as e:
        return f"could not run claude: {e}"
    return (proc.stdout or proc.stderr or "").strip() or "removed"


def manual_mcp_instructions() -> str:
    cmd = " ".join(f'"{c}"' if " " in c else c for c in mcp_command())
    return f"Register the server manually:\n  claude mcp add -s user {SERVER_NAME} -- {cmd}"


def state(runner=subprocess.run) -> InstallState:
    return InstallState(skill_target().exists(), mcp_installed(runner), find_executable() is not None)


def install_all(runner=subprocess.run) -> list[str]:
    msgs = [f"skill installed: {install_skill()}"]
    msgs.append(install_mcp(runner=runner))
    msgs.append("restart Claude Code, then run /bicameral <task> in any repo")
    return msgs


def uninstall_all(runner=subprocess.run) -> list[str]:
    return [f"skill removed: {uninstall_skill()}", uninstall_mcp(runner=runner)]
