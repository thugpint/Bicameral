"""Editor backend that runs the OpenAI Codex CLI on the user's ChatGPT account.

`codex login` (Sign in with ChatGPT) stores credentials in ~/.codex/auth.json.
`codex exec` runs non-interactively; `-o FILE` captures the final message and
`--output-schema FILE` constrains it to a JSON schema.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from .base import AgentEditResult, Completion, ProviderError


def find_executable() -> str | None:
    return shutil.which("codex")


def auth_file() -> Path:
    return Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex")) / "auth.json"


def auth_status() -> dict[str, Any]:
    exe = find_executable()
    p = auth_file()
    mode = None
    if p.exists():
        try:
            data = json.loads(p.read_text("utf-8"))
            if data.get("OPENAI_API_KEY"):
                mode = "api-key"
            elif isinstance(data.get("tokens"), dict) and data["tokens"].get("access_token"):
                mode = data.get("auth_mode") or "chatgpt"
        except (json.JSONDecodeError, OSError):
            mode = None
    return {"installed": bool(exe), "loggedIn": mode is not None, "authMethod": mode or "none"}


def bare_model(model: str) -> str:
    return model.split(":", 1)[1] if ":" in model else model


class CodexCliProvider:
    name = "codex-cli"

    def __init__(self, executable: str | None = None, runner=subprocess.run):
        self.exe = executable or find_executable()
        if not self.exe:
            raise ProviderError("codex-cli: the `codex` executable is not on PATH (npm i -g @openai/codex)")
        self._run = runner

    def _invoke(self, args: list[str], stdin: str, cwd: Path, out_file: Path, timeout: int) -> tuple[str, int]:
        t0 = time.perf_counter()
        try:
            proc = self._run(
                [self.exe, *args], input=stdin, capture_output=True, text=True, encoding="utf-8", errors="replace",
                cwd=str(cwd), timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            raise ProviderError(f"codex-cli: timed out after {timeout}s")
        except OSError as e:
            raise ProviderError(f"codex-cli: could not start `codex`: {e}")
        latency = int((time.perf_counter() - t0) * 1000)
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "").strip()[-800:]
            raise ProviderError(f"codex-cli: exited {proc.returncode}: {tail}")
        text = out_file.read_text("utf-8") if out_file.exists() else (proc.stdout or "")
        return text.strip(), latency

    def complete(
        self,
        model: str,
        system: str,
        user: str,
        *,
        json_schema: dict[str, Any] | None = None,
        schema_name: str = "response",
        effort: str | None = None,
        max_tokens: int = 16000,
    ) -> Completion:
        with tempfile.TemporaryDirectory(prefix="bicameral-codex-") as tmp:
            scratch = Path(tmp)
            out = scratch / "last-message.txt"
            args = ["exec", "--skip-git-repo-check", "-C", str(scratch), "--sandbox", "read-only",
                    "-m", bare_model(model), "-o", str(out)]
            if json_schema:
                schema_path = scratch / "schema.json"
                schema_path.write_text(json.dumps(json_schema), "utf-8")
                args += ["--output-schema", str(schema_path)]
            if effort:
                args += ["-c", f'model_reasoning_effort="{effort}"']
            prompt = f"{system}\n\n{user}" if system else user
            text, latency = self._invoke(args, prompt, scratch, out, timeout=1800)
        return Completion(text=text, input_tokens=0, output_tokens=0, latency_ms=latency, cost_usd=0.0)

    def edit_in_place(self, model: str, root: Path, prompt: str, effort: str | None = None) -> AgentEditResult:
        with tempfile.TemporaryDirectory(prefix="bicameral-codex-") as tmp:
            out = Path(tmp) / "last-message.txt"
            args = ["exec", "--skip-git-repo-check", "-C", str(root), "--full-auto", "-m", bare_model(model), "-o", str(out)]
            if effort:
                args += ["-c", f'model_reasoning_effort="{effort}"']
            text, latency = self._invoke(args, prompt, root, out, timeout=3600)
        return AgentEditResult(summary=text, latency_ms=latency, cost_usd=0.0)

    def list_models(self) -> list[str]:
        return ["codex:gpt-5-codex", "codex:gpt-5"]
