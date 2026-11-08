"""Tests client discovery, connection settings and process cleanup."""

import base64
import hashlib
import io
import json
import os
import pathlib
import socket
import struct
import subprocess
import sys
import threading
import unittest
from unittest import mock

import desktop


class ClientTest(unittest.TestCase):
    def test_windows_install_paths_keep_spaces_and_literal_characters(self) -> None:
        local = str(pathlib.Path("User's $data `folder"))
        program_files = str(pathlib.Path("Program Files"))
        expected = str(
            pathlib.Path(program_files)
            / "Isaac Sim WebRTC Streaming Client"
            / "Isaac Sim WebRTC Streaming Client.exe"
        )
        with (
            mock.patch.object(desktop.sys, "platform", "win32"),
            mock.patch.dict(
                os.environ,
                {"LOCALAPPDATA": local, "ProgramFiles": program_files},
                clear=True,
            ),
            mock.patch.object(
                desktop.shutil, "which", side_effect=[None, expected]
            ) as which,
        ):
            self.assertEqual(desktop.executable(), expected)
        first = str(
            pathlib.Path(local)
            / "Programs"
            / "isaacsim-webrtc-streaming-client"
            / "Isaac Sim WebRTC Streaming Client.exe"
        )
        self.assertEqual(which.call_args_list, [mock.call(first), mock.call(expected)])

    def test_client_override_is_one_executable_not_a_shell_command(self) -> None:
        path = "client with spaces $and `ticks.exe"
        with (
            mock.patch.dict(os.environ, {"SIMPROTO_CLIENT": path}),
            mock.patch.object(desktop.shutil, "which", return_value=path) as which,
        ):
            self.assertEqual(desktop.executable(), path)
        which.assert_called_once_with(path)

    def test_configuration_uses_native_file_url_and_exact_ports(self) -> None:
        page = {
            "type": "page",
            "url": "file:///C:/Program%20Files/Client/resources/app.asar/dist/renderer/index.html",
            "webSocketDebuggerUrl": "ws://127.0.0.1:12345/devtools/page/one",
        }
        response = io.BytesIO(json.dumps([page]).encode())
        with (
            mock.patch.object(desktop.urllib.request, "urlopen", return_value=response),
            mock.patch.object(desktop, "navigate") as navigate,
        ):
            desktop.configure(12345, "127.0.0.1", 49101, 58207)
        args = navigate.call_args.args
        self.assertEqual(args[0], page["webSocketDebuggerUrl"])
        self.assertEqual(
            args[1],
            page["url"] + "?server=127.0.0.1&signalPort=49101&streamPort=58207"
            "&resolution=1&autoConnect=1",
        )

    def test_native_process_cleanup_after_configuration_failure(self) -> None:
        process = subprocess.Popen(
            [
                sys.executable,
                "-u",
                "-c",
                "import sys, time; "
                "print('DevTools listening on ws://127.0.0.1:12345/devtools/browser/test',"
                " file=sys.stderr, flush=True); time.sleep(30)",
            ],
            stderr=subprocess.PIPE,
        )
        try:
            with (
                mock.patch.object(
                    desktop, "executable", return_value="client with spaces.exe"
                ),
                mock.patch.object(
                    desktop.subprocess, "Popen", return_value=process
                ) as launch,
                mock.patch.object(
                    desktop,
                    "configure",
                    side_effect=RuntimeError("configuration failed"),
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "configuration failed"):
                    with desktop.launch("127.0.0.1", 49100, 58207):
                        self.fail("A failed client must not be returned")
            args = launch.call_args.args[0]
            self.assertEqual(args[0], "client with spaces.exe")
            self.assertIn("--allow-loopback-in-peer-connection", args)
            self.assertIn("--remote-debugging-port=0", args)
            profile = pathlib.Path(
                next(
                    a.split("=", 1)[1] for a in args if a.startswith("--user-data-dir=")
                )
            )
            self.assertFalse(profile.exists())
            self.assertIsNotNone(process.poll())
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()
            process.stderr.close()

    def test_configuration_cannot_target_a_remote_debugger(self) -> None:
        with self.assertRaisesRegex(ValueError, "local"):
            desktop.navigate("ws://example.com:12345/page", "file:///index.html")

    def test_real_websocket_masks_commands_and_handles_ping_and_fragments(self) -> None:
        errors = []
        commands = []
        with socket.socket() as server:
            server.bind(("127.0.0.1", 0))
            server.listen(1)
            server.settimeout(5)

            def serve() -> None:
                try:
                    connection, _ = server.accept()
                    with connection, connection.makefile("rb") as reader:
                        connection.settimeout(5)
                        self.assertIn(b"GET /devtools/page/test", reader.readline())
                        headers = {}
                        while True:
                            line = reader.readline()
                            if line == b"\r\n":
                                break
                            name, value = line.decode().split(":", 1)
                            headers[name.lower()] = value.strip()
                        accept = base64.b64encode(
                            hashlib.sha1(
                                (
                                    headers["sec-websocket-key"]
                                    + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
                                ).encode()
                            ).digest()
                        ).decode()
                        connection.sendall(
                            (
                                "HTTP/1.1 101 Switching Protocols\r\n"
                                "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                                f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
                            ).encode()
                        )
                        first, second = reader.read(2)
                        self.assertEqual(first, 0x81)
                        self.assertTrue(second & 0x80)
                        size = second & 0x7F
                        if size == 126:
                            size = struct.unpack("!H", reader.read(2))[0]
                        mask = reader.read(4)
                        payload = reader.read(size)
                        commands.append(
                            json.loads(
                                bytes(v ^ mask[i % 4] for i, v in enumerate(payload))
                            )
                        )
                        connection.sendall(b"\x89\x01x")
                        first, second = reader.read(2)
                        self.assertEqual(first, 0x8A)
                        mask = reader.read(4)
                        self.assertEqual(
                            reader.read(second & 0x7F)[0] ^ mask[0], ord("x")
                        )
                        reply = b'{"id":1,"result":{"frameId":"test"}}'
                        connection.sendall(bytes([0x01, 10]) + reply[:10])
                        connection.sendall(bytes([0x80, len(reply) - 10]) + reply[10:])
                except Exception as error:
                    errors.append(error)

            thread = threading.Thread(target=serve, daemon=True)
            thread.start()
            try:
                url = "file:///C:/Program%20Files/Client/renderer/index.html?server=127.0.0.1&signalPort=49100&streamPort=58207&autoConnect=1"
                desktop.navigate(
                    f"ws://127.0.0.1:{server.getsockname()[1]}/devtools/page/test", url
                )
            finally:
                thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(
            commands, [{"id": 1, "method": "Page.navigate", "params": {"url": url}}]
        )


if __name__ == "__main__":
    unittest.main()
