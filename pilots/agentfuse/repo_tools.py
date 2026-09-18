"""Read-only tools over a fixed public Git tree, never the working directory."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

AGENTFUSE_REVISION = "9aa96de5d0a9e5a4a5ad65cc42f8b1397fe8813d"

TOOLS: list[dict[str, Any]] = [
    {"type": "function", "function": {
        "name": "search_files", "description": "Find literal text in public AgentFuse Python source; returns paths and line numbers.",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
    }},
    {"type": "function", "function": {
        "name": "read_file", "description": "Read bounded lines from a public source file found by search_files.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}, "start_line": {"type": "integer"}, "max_lines": {"type": "integer"},
        }, "required": ["path"]},
    }},
]


class PublicRepoTools:
    """Snapshot only regular .py blobs in agentfuse/ at the authorized public commit.

    Reading blob IDs avoids traversal, symlinks, .env files, untracked files and local
    edits. Git replacement objects are disabled. No shell or model-generated command
    is executed. A bounded in-memory snapshot also keeps reads stable during the run.
    """

    def __init__(self, repo: Path) -> None:
        self.repo = repo
        self.search_count = 0
        self.read_count = 0
        self.rejected_count = 0
        self._files: dict[str, list[str]] = {}
        entries = self._git("ls-tree", "-rz", AGENTFUSE_REVISION, "--", "agentfuse/")
        for entry in entries.split(b"\0"):
            if not entry:
                continue
            meta, raw_path = entry.split(b"\t", 1)
            mode, kind, oid = meta.decode("ascii").split()
            path = raw_path.decode("utf-8")
            if mode not in ("100644", "100755") or kind != "blob" or not path.endswith(".py"):
                continue
            if int(self._git("cat-file", "-s", oid)) > 256_000:
                continue
            self._files[path] = self._git("cat-file", "blob", oid).decode("utf-8").splitlines()
        if "agentfuse/monitor.py" not in self._files:
            raise ValueError("Authorized public AgentFuse source is unavailable")

    def _git(self, *args: str) -> bytes:
        result = subprocess.run(
            ["git", "--no-replace-objects", "-C", str(self.repo), *args],
            capture_output=True, check=False, timeout=20,
        )
        if result.returncode:
            raise ValueError("Unable to read the pinned public Git tree")
        return result.stdout

    def __call__(self, name: str, arguments: Any) -> str:
        if not isinstance(arguments, dict):
            return self._reject()
        if name == "search_files" and set(arguments) == {"query"}:
            query = arguments["query"]
            if not isinstance(query, str) or not 1 <= len(query) <= 160:
                return self._reject()
            self.search_count += 1
            matches = []
            for path, lines in sorted(self._files.items()):
                for number, line in enumerate(lines, start=1):
                    if query.casefold() in line.casefold():
                        matches.append(f"{path}:{number}: {line[:180]}")
                        if len(matches) >= 20:
                            return "\n".join(matches) + "\n[20-match limit reached]"
            return "\n".join(matches) or "No matches in the authorized source snapshot."
        if name == "read_file" and set(arguments) <= {"path", "start_line", "max_lines"}:
            requested_path = arguments.get("path")
            start = arguments.get("start_line", 1)
            count = arguments.get("max_lines", 60)
            if (not isinstance(requested_path, str) or requested_path not in self._files or
                    type(start) is not int or type(count) is not int or start < 1 or not 1 <= count <= 100):
                return self._reject()
            self.read_count += 1
            rows = [f"{i}: {line}" for i, line in enumerate(self._files[requested_path][start - 1:start - 1 + count], start)]
            text = "\n".join(rows)
            return text[:6_000] + ("\n[output truncated]" if len(text) > 6_000 else "")
        return self._reject()

    def _reject(self) -> str:
        self.rejected_count += 1
        return "ERROR: unsupported tool or arguments; only public snapshot search/read are allowed."
