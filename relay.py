"""Carries WebRTC UDP packets through SSH."""

from __future__ import annotations

import argparse
import contextlib
import os
import select
import selectors
import shlex
import socket
import struct
import subprocess
import sys
import threading
import time
from collections.abc import Iterator

import desktop

READY = b"SIMPROTO_RELAY_READY\n"


@contextlib.contextmanager
def pipe_socket(reader: int) -> Iterator[socket.socket]:
    """Wraps a pipe in a socket so Windows select can wait on it.

    Close the pipe producer before leaving this context so the reader thread
    can exit. The socket buffer limits memory use if the consumer falls behind.
    """
    receiver, sender = socket.socketpair()
    stopped = threading.Event()
    errors: list[OSError] = []

    def copy() -> None:
        with sender:
            try:
                while not stopped.is_set():
                    data = os.read(reader, 131072)
                    if not data:
                        break
                    sender.sendall(data)
            except OSError as error:
                if not stopped.is_set():
                    errors.append(error)

    thread = threading.Thread(target=copy, name="simproto-pipe", daemon=True)
    thread.start()
    try:
        yield receiver
        if errors:
            raise errors[0]
    finally:
        stopped.set()
        try:
            receiver.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        receiver.close()
        thread.join(timeout=1)


def wait_ready(reader: socket.socket, timeout: float = 30) -> None:
    """Waits for the relay startup marker without consuming packet data."""
    deadline = time.monotonic() + timeout
    received = bytearray()
    while len(received) < len(READY):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("SSH relay did not become ready within 30 seconds")
        if not select.select([reader], [], [], min(remaining, 0.5))[0]:
            continue
        data = reader.recv(1)
        received.extend(data)
        if not data or not READY.startswith(received):
            raise RuntimeError("SSH relay failed to start; check the SSH error above")


def packets(buffer: bytearray) -> list[bytes]:
    """Reads complete length-prefixed packets, leaving any partial packet buffered."""
    result = []
    while len(buffer) >= 4:
        size = struct.unpack("!I", buffer[:4])[0]
        if size > 65507:
            raise ValueError("Invalid UDP frame length")
        if len(buffer) < 4 + size:
            break
        result.append(bytes(buffer[4 : 4 + size]))
        del buffer[: 4 + size]
    return result


def pump(
    udp: socket.socket,
    reader: socket.socket,
    writer: int,
    remote: bool,
    client: subprocess.Popen | None = None,
) -> None:
    """Copies packets between UDP and the length-prefixed SSH stream."""
    selector = selectors.DefaultSelector()
    selector.register(udp, selectors.EVENT_READ)
    selector.register(reader, selectors.EVENT_READ)
    buffer = bytearray()
    peer = None
    try:
        while True:
            if client is not None and client.poll() is not None:
                return
            # wake periodically so Windows can handle Ctrl+C while the stream is idle
            for key, _ in selector.select(timeout=0.5):
                if key.fileobj == udp:
                    try:
                        data, address = udp.recvfrom(65535)
                    except (ConnectionRefusedError, ConnectionResetError):
                        # ICE can send probes before the server starts listening
                        continue
                    if not remote:
                        if peer is not None and peer != address:
                            continue
                        peer = address
                    framed = memoryview(struct.pack("!I", len(data)) + data)
                    while framed:
                        written = os.write(writer, framed)
                        framed = framed[written:]
                else:
                    data = reader.recv(131072)
                    if not data:
                        return
                    buffer.extend(data)
                    for packet in packets(buffer):
                        if remote:
                            try:
                                udp.send(packet)
                            except (ConnectionRefusedError, ConnectionResetError):
                                continue
                        elif peer is not None:
                            udp.sendto(packet, peer)
    finally:
        selector.close()


def connect(
    host: str,
    connection: dict,
    local_port: int = 49100,
    open_client: bool = False,
) -> int:
    """Tunnels signaling and media over SSH and optionally opens the client."""
    remote_args = shlex.join(
        ["--target", connection["node"], "--port", str(connection["stream_port"])]
    )
    command = 'exec python3 "$HOME/simproto/relay.py" ' + remote_args
    forward = f"127.0.0.1:{local_port}:{connection['node']}:{connection['signal_port']}"
    args = [
        "ssh",
        "-T",
        "-o",
        "ControlMaster=no",
        "-o",
        "ControlPath=none",
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        "ConnectTimeout=20",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=3",
        "-L",
        forward,
        host,
        command,
    ]
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
        udp.bind(("127.0.0.1", int(connection["stream_port"])))
        process = subprocess.Popen(
            args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, bufsize=0
        )
        assert process.stdin is not None and process.stdout is not None
        cancelled = False
        with (
            pipe_socket(process.stdout.fileno()) as reader,
            contextlib.ExitStack() as stack,
        ):
            try:
                wait_ready(reader)
                print("Isaac WebRTC client settings:")
                print(
                    f"  Server: 127.0.0.1  Signal: {local_port}  "
                    f"Stream: {connection['stream_port']}"
                )
                if sys.platform == "win32" and not open_client:
                    print(
                        "Windows: launch the client with .\\stream-client.cmd "
                        "from another terminal in this repo."
                    )
                print(
                    "Keep this terminal open. Ctrl+C disconnects; "
                    "the Slurm job stays alive."
                )
                print(
                    "SSH media adds latency; use direct mode if reachable.", flush=True
                )
                client = None
                if open_client:
                    client = stack.enter_context(
                        desktop.launch(
                            "127.0.0.1", local_port, int(connection["stream_port"])
                        )
                    )
                pump(udp, reader, process.stdin.fileno(), False, client)
                if client is not None and client.poll() is not None:
                    cancelled = True
                else:
                    process.wait(timeout=5)
            except KeyboardInterrupt:
                cancelled = True
            finally:
                if process.poll() is None:
                    process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                process.stdin.close()
                process.stdout.close()
        return (
            0 if cancelled else (process.returncode if process.returncode >= 0 else 1)
        )


def main() -> None:
    """Runs the relay on the login node; stdout carries binary packet data."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", required=True)
    parser.add_argument("--port", required=True, type=int)
    args = parser.parse_args()
    if sys.platform == "win32":
        import msvcrt

        # os.read and os.write use the raw descriptors, which need binary mode
        msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
        msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
        udp.connect((args.target, args.port))
        sys.stdout.buffer.write(READY)
        sys.stdout.buffer.flush()
        with pipe_socket(sys.stdin.fileno()) as reader:
            pump(udp, reader, sys.stdout.fileno(), True)


if __name__ == "__main__":
    main()
