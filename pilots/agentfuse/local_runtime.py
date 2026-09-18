"""Launch and stop the checksum-pinned CPU runtime; never use a remote model API."""

from __future__ import annotations

import hashlib
import http.client
import os
import socket
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from pilots.agentfuse.local_client import LocalPilotError

MODEL_SHA256 = "061b54daade076b5d3362dac252678d17da8c68f07560be70818cace6590cb1a"
SERVER_SHA256 = "aa2e1f5c67be55f11be26ae58d643a545ca07c6de498f6f870330ac2f4dfec73"


def verify_file(path: Path, expected: str) -> None:
    with path.open("rb") as source:
        actual = hashlib.file_digest(source, "sha256").hexdigest()
    if actual != expected:
        raise LocalPilotError("Local runtime/model checksum mismatch")


@contextmanager
def local_server(executable: Path, model: Path, port: int) -> Iterator[None]:
    """Windows CPU build b10964, Qwen3-1.7B Q8_0; caller owns both verified downloads.

    stdout/stderr are discarded, not written as raw prompt logs. The server runs with
    --offline and no web UI, listens only on loopback, and is stopped on every exit.
    """
    verify_file(executable, SERVER_SHA256)
    verify_file(model, MODEL_SHA256)
    # Refuse an already occupied port; do not accidentally query another service.
    with socket.socket() as probe:
        if os.name == "nt":
            probe.setsockopt(socket.SOL_SOCKET, getattr(socket, "SO_EXCLUSIVEADDRUSE"), 1)
        probe.bind(("127.0.0.1", port))
    process = subprocess.Popen([
        str(executable.resolve()), "--model", str(model.resolve()),
        "--host", "127.0.0.1", "--port", str(port), "--alias", "sixeyes-local",
        "--offline", "--no-webui", "--jinja", "--reasoning", "off",
        "--ctx-size", "8192", "--parallel", "1", "--n-predict", "512",
        "--threads", "6", "--temp", "0.2", "--seed", "42", "--log-disable",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise LocalPilotError("Local model server exited during startup")
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
            try:
                connection.request("GET", "/health")
                if connection.getresponse().status == 200:
                    break
            except (OSError, http.client.HTTPException):
                pass
            finally:
                connection.close()
            time.sleep(0.25)
        else:
            raise LocalPilotError("Local model startup timed out")
        yield
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
