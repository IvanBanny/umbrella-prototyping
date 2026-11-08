"""Opens NVIDIA's streaming client and connects it to the session."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import pathlib
import queue
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import typing
import urllib.parse
import urllib.request
from collections.abc import Iterator


def executable() -> str:
    """Finds the client executable, checking SIMPROTO_CLIENT first."""
    override = os.environ.get("SIMPROTO_CLIENT")
    if override:
        candidates = [override]
    elif sys.platform == "win32":
        candidates = [
            str(
                pathlib.Path(os.environ.get("LOCALAPPDATA", ""))
                / "Programs"
                / "isaacsim-webrtc-streaming-client"
                / "Isaac Sim WebRTC Streaming Client.exe"
            ),
            str(
                pathlib.Path(os.environ.get("ProgramFiles", ""))
                / "Isaac Sim WebRTC Streaming Client"
                / "Isaac Sim WebRTC Streaming Client.exe"
            ),
        ]
    elif sys.platform == "darwin":
        suffix = (
            "Isaac Sim WebRTC Streaming Client.app/Contents/MacOS/"
            "Isaac Sim WebRTC Streaming Client"
        )
        candidates = [
            str(pathlib.Path("/Applications") / suffix),
            str(pathlib.Path.home() / "Applications" / suffix),
        ]
    else:
        candidates = ["isaacsim-webrtc-streaming-client"]
    for candidate in candidates:
        found = shutil.which(candidate)
        if found:
            return found
    raise RuntimeError(
        "Install NVIDIA's Isaac Sim WebRTC Streaming Client 2.0 first, "
        "or set SIMPROTO_CLIENT to its executable. "
        "Use --no-client to manage the client yourself."
    )


def _send_frame(sock: socket.socket, opcode: int, payload: bytes) -> None:
    mask = os.urandom(4)
    length = len(payload)
    if length < 126:
        header = bytes([0x80 | opcode, 0x80 | length])
    else:
        header = bytes([0x80 | opcode, 0x80 | 126]) + struct.pack("!H", length)
    masked = bytes(value ^ mask[i % 4] for i, value in enumerate(payload))
    sock.sendall(header + mask + masked)


def _read_exact(reader: typing.BinaryIO, size: int) -> bytes:
    data = reader.read(size)
    if data is None or len(data) != size:
        raise RuntimeError("Streaming client closed during configuration")
    return data


def navigate(websocket_url: str, page_url: str) -> None:
    """Sets the client's connection URL through its local DevTools socket.

    Client 2.0 reads connection settings from the renderer URL. It has no
    desktop flags for these settings, so we navigate once and close the socket.
    """
    endpoint = urllib.parse.urlsplit(websocket_url)
    if (
        endpoint.scheme != "ws"
        or endpoint.hostname != "127.0.0.1"
        or endpoint.port is None
    ):
        raise ValueError("Expected a local streaming-client debugging endpoint")
    with socket.create_connection(("127.0.0.1", endpoint.port), timeout=10) as sock:
        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            f"GET {endpoint.path} HTTP/1.1\r\nHost: {endpoint.netloc}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        sock.sendall(request.encode())
        with sock.makefile("rb") as reader:
            status = reader.readline(4096)
            headers = {}
            while True:
                line = reader.readline(4096)
                if line == b"\r\n":
                    break
                if not line or b":" not in line:
                    raise RuntimeError("Invalid streaming-client handshake")
                name, value = line.decode().split(":", 1)
                headers[name.lower()] = value.strip()
            accept = base64.b64encode(
                hashlib.sha1(
                    (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()
                ).digest()
            ).decode()
            if b" 101 " not in status or headers.get("sec-websocket-accept") != accept:
                raise RuntimeError("Streaming-client handshake failed")
            command = {"id": 1, "method": "Page.navigate", "params": {"url": page_url}}
            _send_frame(sock, 1, json.dumps(command).encode())
            message = bytearray()
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                first, second = _read_exact(reader, 2)
                size = second & 0x7F
                if size == 126:
                    size = struct.unpack("!H", _read_exact(reader, 2))[0]
                elif size == 127:
                    size = struct.unpack("!Q", _read_exact(reader, 8))[0]
                if second & 0x80 or size > 1_048_576:
                    raise RuntimeError("Invalid streaming-client response frame")
                payload = _read_exact(reader, size)
                opcode = first & 0x0F
                if opcode == 9:
                    _send_frame(sock, 10, payload)
                    continue
                if opcode == 8:
                    raise RuntimeError("Streaming client closed during configuration")
                if opcode not in (0, 1):
                    continue
                message.extend(payload)
                if not first & 0x80:
                    continue
                response = json.loads(message)
                message.clear()
                if response.get("id") == 1:
                    error = response.get("error") or response.get("result", {}).get(
                        "errorText"
                    )
                    if error:
                        raise RuntimeError(
                            f"Could not configure streaming client: {error}"
                        )
                    return
            raise RuntimeError(
                "Streaming client did not accept its connection settings"
            )


def configure(port: int, server: str, signal_port: int, stream_port: int) -> None:
    """Finds the client page and connects with the 1080p preset."""
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/json/list", timeout=5
        ) as r:
            pages = json.load(r)
        for page in pages:
            url = urllib.parse.urlsplit(page.get("url", ""))
            if page.get("type") != "page" or url.scheme != "file":
                continue
            if not url.path.endswith("/renderer/index.html"):
                continue
            query = urllib.parse.urlencode(
                {
                    "server": server,
                    "signalPort": signal_port,
                    "streamPort": stream_port,
                    "resolution": 1,
                    "autoConnect": 1,
                }
            )
            navigate(page["webSocketDebuggerUrl"], url._replace(query=query).geturl())
            return
        time.sleep(0.1)
    raise RuntimeError("Could not find the native client's connection page")


@contextlib.contextmanager
def launch(
    server: str, signal_port: int, stream_port: int
) -> Iterator[subprocess.Popen]:
    """Opens the client and cleans up its process and temporary profile on exit."""
    binary = executable()
    with tempfile.TemporaryDirectory(prefix="simproto-client-") as profile:
        # client 2.0 reads window state before opening the window
        (pathlib.Path(profile) / "window-state.json").write_text(
            json.dumps({"width": 1280, "height": 800, "isMaximized": True})
        )
        process = subprocess.Popen(
            [
                binary,
                "--allow-loopback-in-peer-connection",
                "--remote-debugging-port=0",
                f"--user-data-dir={profile}",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            start_new_session=os.name != "nt",
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
        assert process.stderr is not None
        ready: queue.Queue[int] = queue.Queue()
        errors: list[str] = []

        def read_errors() -> None:
            assert process.stderr is not None
            for data in process.stderr:
                line = data.decode(errors="replace").strip()
                match = re.search(
                    r"DevTools listening on ws://127\.0\.0\.1:(\d+)/", line
                )
                if match:
                    ready.put(int(match[1]))
                elif len(errors) < 10:
                    errors.append(line)
            ready.put(0)

        thread = threading.Thread(target=read_errors, daemon=True)
        thread.start()
        try:
            try:
                port = ready.get(timeout=20)
            except queue.Empty:
                raise RuntimeError(
                    "Streaming client did not start within 20 seconds"
                ) from None
            if not port:
                raise RuntimeError(
                    "Streaming client could not start: " + "\n".join(errors)
                )
            configure(port, server, signal_port, stream_port)
            print(
                "Opened Isaac client with the 1080p preset. "
                "Close it (Quit on macOS) to disconnect.",
                flush=True,
            )
            yield process
        finally:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            thread.join(timeout=2)
            process.stderr.close()
