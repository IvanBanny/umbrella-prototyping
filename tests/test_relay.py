"""Tests packet framing and UDP round trips through the relay."""

import contextlib
import pathlib
import socket
import struct
import subprocess
import sys
import threading
import unittest
from unittest import mock

import relay


def receive(reader: socket.socket, size: int) -> bytes:
    """Reads the requested bytes, failing if the socket stalls or closes."""
    data = bytearray()
    reader.settimeout(5)
    while len(data) < size:
        chunk = reader.recv(size - len(data))
        if not chunk:
            raise EOFError("Relay closed before sending the complete frame")
        data.extend(chunk)
    return bytes(data)


class RelayTest(unittest.TestCase):
    def test_fragmented_and_coalesced_packets(self) -> None:
        buffer = bytearray(b"\x00\x00")
        self.assertEqual(relay.packets(buffer), [])
        buffer.extend(b"\x00\x03ab")
        self.assertEqual(relay.packets(buffer), [])
        buffer.extend(b"c\x00\x00\x00\x02de")
        self.assertEqual(relay.packets(buffer), [b"abc", b"de"])
        self.assertEqual(buffer, b"")

    def test_rejects_oversized_packet(self) -> None:
        with self.assertRaises(ValueError):
            relay.packets(bytearray(struct.pack("!I", 65508)))

    def test_real_udp_round_trip(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as server:
            server.bind(("127.0.0.1", 0))
            server.settimeout(5)
            process = subprocess.Popen(
                [
                    sys.executable,
                    str(pathlib.Path(relay.__file__)),
                    "--target",
                    "127.0.0.1",
                    "--port",
                    str(server.getsockname()[1]),
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                bufsize=0,
            )
            try:
                self.assertIsNotNone(process.stdin)
                self.assertIsNotNone(process.stdout)
                with relay.pipe_socket(process.stdout.fileno()) as reader:
                    relay.wait_ready(reader)
                    # catch Windows newline and Ctrl+Z translation with all byte values
                    for payload in (bytes(range(256)) * 32, b"", b"last packet"):
                        process.stdin.write(struct.pack("!I", len(payload)) + payload)
                        data, peer = server.recvfrom(65535)
                        self.assertEqual(data, payload)
                        server.sendto(payload[::-1], peer)
                        size = struct.unpack("!I", receive(reader, 4))[0]
                        self.assertEqual(receive(reader, size), payload[::-1])
                    process.stdin.close()
                    self.assertEqual(process.wait(timeout=5), 0)
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait()
                process.stdout.close()
                process.stdin.close()

    def test_ready_does_not_consume_first_packet(self) -> None:
        reader, writer = socket.socketpair()
        with reader, writer:
            writer.sendall(relay.READY + b"packet")
            relay.wait_ready(reader)
            self.assertEqual(receive(reader, 6), b"packet")

    def test_partial_ready_times_out(self) -> None:
        reader, writer = socket.socketpair()
        with reader, writer:
            writer.sendall(relay.READY[:3])
            with self.assertRaisesRegex(RuntimeError, "did not become ready"):
                relay.wait_ready(reader, timeout=0.05)

    def test_failed_ready(self) -> None:
        for reply in (b"", b"wrong reply\n"):
            reader, writer = socket.socketpair()
            with reader, writer:
                writer.sendall(reply)
                writer.shutdown(socket.SHUT_WR)
                with self.assertRaisesRegex(RuntimeError, "failed to start"):
                    relay.wait_ready(reader)


class ConnectionTest(unittest.TestCase):
    def setUp(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
            udp.bind(("127.0.0.1", 0))
            port = udp.getsockname()[1]
        self.connection = {
            "node": "compute-node",
            "stream_port": port,
            "signal_port": 42,
        }
        self.process = subprocess.Popen(
            [
                sys.executable,
                "-u",
                "-c",
                "import struct, sys; "
                "sys.stdout.buffer.write(b'SIMPROTO_RELAY_READY\\n'); "
                "sys.stdout.buffer.flush(); "
                "size = sys.stdin.buffer.read(4); "
                "data = sys.stdin.buffer.read(struct.unpack('!I', size)[0]); "
                "sys.stdout.buffer.write(size + data); "
                "sys.stdout.buffer.flush(); sys.exit(7)",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            bufsize=0,
        )
        self.addCleanup(self.cleanup_process)

    def cleanup_process(self) -> None:
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait(timeout=5)
        self.process.stdin.close()
        self.process.stdout.close()

    def test_client_round_trip_and_ssh_exit_status(self) -> None:
        ready = threading.Event()
        finished = []
        errors = []
        wait_ready = relay.wait_ready

        def notify_ready(reader: socket.socket) -> None:
            wait_ready(reader)
            ready.set()

        def connect() -> None:
            try:
                finished.append(relay.connect("test-host", self.connection, 49101))
            except Exception as error:
                errors.append(error)

        with (
            mock.patch.object(
                relay.subprocess, "Popen", return_value=self.process
            ) as ssh,
            mock.patch.object(relay, "wait_ready", side_effect=notify_ready),
            socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client,
        ):
            thread = threading.Thread(target=connect, daemon=True)
            thread.start()
            try:
                self.assertTrue(ready.wait(5))
                client.settimeout(5)
                payload = bytes(range(256)) * 32
                client.sendto(payload, ("127.0.0.1", self.connection["stream_port"]))
                self.assertEqual(client.recvfrom(65535)[0], payload)
                thread.join(timeout=10)
                self.assertFalse(thread.is_alive())
                self.assertEqual(errors, [])
                self.assertEqual(finished, [7])
                self.assertIn("127.0.0.1:49101:compute-node:42", ssh.call_args.args[0])
            finally:
                if self.process.poll() is None:
                    self.process.kill()
                thread.join(timeout=5)

    def test_ctrl_c_closes_child_and_releases_port(self) -> None:
        with (
            mock.patch.object(relay.subprocess, "Popen", return_value=self.process),
            mock.patch.object(relay, "pump", side_effect=KeyboardInterrupt),
        ):
            self.assertEqual(relay.connect("test-host", self.connection), 0)
        self.assertIsNotNone(self.process.poll())
        self.assertTrue(self.process.stdin.closed)
        self.assertTrue(self.process.stdout.closed)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
            udp.bind(("127.0.0.1", self.connection["stream_port"]))

    def test_failed_start_closes_child(self) -> None:
        with (
            mock.patch.object(relay.subprocess, "Popen", return_value=self.process),
            mock.patch.object(relay, "wait_ready", side_effect=RuntimeError("startup")),
        ):
            with self.assertRaisesRegex(RuntimeError, "startup"):
                relay.connect("test-host", self.connection)
        self.assertIsNotNone(self.process.poll())

    def test_closing_native_client_closes_tunnel(self) -> None:
        client = mock.Mock()
        client.poll.return_value = 0
        with (
            mock.patch.object(relay.subprocess, "Popen", return_value=self.process),
            mock.patch.object(
                relay.desktop, "launch", return_value=contextlib.nullcontext(client)
            ) as launch,
        ):
            self.assertEqual(
                relay.connect("test-host", self.connection, open_client=True), 0
            )
        launch.assert_called_once_with(
            "127.0.0.1", 49100, self.connection["stream_port"]
        )
        self.assertIsNotNone(self.process.poll())
        self.assertTrue(self.process.stdin.closed)

    def test_native_client_start_failure_closes_tunnel(self) -> None:
        with (
            mock.patch.object(relay.subprocess, "Popen", return_value=self.process),
            mock.patch.object(
                relay.desktop, "launch", side_effect=RuntimeError("native start failed")
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "native start failed"):
                relay.connect("test-host", self.connection, open_client=True)
        self.assertIsNotNone(self.process.poll())


if __name__ == "__main__":
    unittest.main()
