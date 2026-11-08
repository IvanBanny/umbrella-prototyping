"""Tests local commands, SSH forwarding and the installation archive."""

import importlib.machinery
import importlib.util
import io
import os
import pathlib
import shlex
import socket
import subprocess
import sys
import tarfile
import unittest
from unittest import mock

SOURCE = pathlib.Path(__file__).resolve().parents[1]
loader = importlib.machinery.SourceFileLoader(
    "client_launcher", str(SOURCE / "simproto")
)
spec = importlib.util.spec_from_loader(loader.name, loader)
launcher = importlib.util.module_from_spec(spec)
loader.exec_module(launcher)


class LauncherTest(unittest.TestCase):
    def test_help_runs_with_native_python(self) -> None:
        result = subprocess.run(
            [sys.executable, str(SOURCE / "simproto"), "--help"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("connect", result.stdout)

    def test_commands_dispatch_over_ssh(self) -> None:
        for command in (
            ["setup"],
            ["plan", "--gpu", "l4"],
            ["start", "--detach", "--project", "~/project with spaces"],
            ["status"],
            ["logs", "session", "--follow"],
            ["stop"],
            ["stop", "session"],
        ):
            with (
                self.subTest(command=command),
                mock.patch.object(
                    sys, "argv", ["simproto", "--host", "cluster", *command]
                ),
                mock.patch.object(launcher.shutil, "which", return_value=None),
                mock.patch.object(launcher.subprocess, "call", return_value=7) as run,
            ):
                self.assertEqual(launcher.main(), 7)
                args = run.call_args.args[0]
                self.assertEqual(
                    args[:4], ["ssh", "-o", "ConnectTimeout=20", "cluster"]
                )
                forwarded = shlex.split(args[4])[4:]
                self.assertEqual(
                    forwarded[0],
                    "--client-command=" + launcher.client_command("cluster"),
                )
                self.assertEqual(forwarded[1:], command)

    def test_application_arguments_survive_forwarding(self) -> None:
        command = [
            "start",
            "--mode",
            "run",
            "--",
            "python",
            "/project/my script.py",
            "--host=app-host",
            "--host",
            "other-host",
            'quotes " and $HOME',
        ]
        for prefix in ([], ["--host", "cluster"], ["--host=cluster"]):
            with (
                mock.patch.object(sys, "argv", ["simproto", *prefix, *command]),
                mock.patch.object(launcher.shutil, "which", return_value=None),
                mock.patch.object(launcher.subprocess, "call", return_value=0) as run,
            ):
                self.assertEqual(launcher.main(), 0)
                self.assertEqual(shlex.split(run.call_args.args[0][-1])[5:], command)

    @unittest.skipUnless(sys.platform == "win32", "PowerShell command hints")
    def test_printed_command_uses_working_python_from_another_directory(self) -> None:
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-Command",
                launcher.client_command("cluster with 'quotes' $and `ticks")
                + " --help",
            ],
            cwd=SOURCE.parent,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("connect", result.stdout)

    def test_connect_dispatches_to_local_relay(self) -> None:
        with (
            mock.patch.object(sys, "argv", ["simproto", "connect", "session"]),
            mock.patch.object(launcher.shutil, "which", return_value=None),
            mock.patch.object(launcher, "connect", return_value=0) as connect,
        ):
            self.assertEqual(launcher.main(), 0)
            self.assertEqual(connect.call_args.args[1:], ("session", 49100, True))

    def test_connect_without_id_and_manual_client(self) -> None:
        with (
            mock.patch.object(sys, "argv", ["simproto", "connect", "--no-client"]),
            mock.patch.object(launcher.shutil, "which", return_value=None),
            mock.patch.object(launcher, "connect", return_value=0) as connect,
        ):
            self.assertEqual(launcher.main(), 0)
        self.assertEqual(connect.call_args.args[1:], (None, 49100, False))

    def test_gui_start_submits_once_then_connects_to_returned_session(self) -> None:
        with (
            mock.patch.object(sys, "argv", ["simproto", "start", "--gpu", "2080ti"]),
            mock.patch.object(launcher.shutil, "which", return_value=None),
            mock.patch.object(launcher.socket, "socket"),
            mock.patch.object(
                launcher.desktop, "executable", return_value="client.exe"
            ),
            mock.patch.object(
                launcher,
                "remote_json",
                return_value={"id": "d6efd4", "session": "20261108-182745-d6efd4"},
            ) as submit,
            mock.patch.object(launcher, "connect", return_value=0) as connect,
        ):
            self.assertEqual(launcher.main(), 0)
        submit.assert_called_once()
        self.assertEqual(
            submit.call_args.args[1], ["start", "--json", "--gpu", "2080ti"]
        )
        self.assertEqual(
            connect.call_args.args[1:], ("20261108-182745-d6efd4", 49100, True)
        )

    def test_missing_client_does_not_allocate_a_gpu(self) -> None:
        with (
            mock.patch.object(sys, "argv", ["simproto", "start"]),
            mock.patch.object(launcher.shutil, "which", return_value=None),
            mock.patch.object(
                launcher.desktop,
                "executable",
                side_effect=RuntimeError("missing client"),
            ),
            mock.patch.object(launcher, "remote_json") as submit,
        ):
            with self.assertRaisesRegex(RuntimeError, "missing client"):
                launcher.main()
        submit.assert_not_called()

    def test_busy_local_port_does_not_allocate_another_gpu(self) -> None:
        with socket.socket() as busy:
            busy.bind(("127.0.0.1", 0))
            busy.listen(1)
            port = busy.getsockname()[1]
            with (
                mock.patch.object(
                    sys,
                    "argv",
                    ["simproto", "start", "--no-client", "--local-port", str(port)],
                ),
                mock.patch.object(launcher.shutil, "which", return_value=None),
                mock.patch.object(launcher, "remote_json") as submit,
            ):
                with self.assertRaisesRegex(RuntimeError, "already in use"):
                    launcher.main()
            submit.assert_not_called()

    def test_remote_endpoint_preserves_host_and_literal_session(self) -> None:
        connection = {
            "direct": False,
            "node": "gpu-node",
            "stream_port": 1234,
            "signal_port": 4321,
        }
        with (
            mock.patch.object(
                launcher, "remote_json", return_value=connection
            ) as remote,
            mock.patch.object(launcher.relay, "connect", return_value=0) as relay,
        ):
            self.assertEqual(launcher.connect("cluster", "d6efd4", 49101, False), 0)
        remote.assert_called_once_with(
            "cluster", ["connect", "--json", "--wait", "d6efd4"]
        )
        relay.assert_called_once_with("cluster", connection, 49101, open_client=False)

    def test_install_archive_has_linux_permissions(self) -> None:
        class ArchivePipe(io.BytesIO):
            def close(self) -> None:
                self.contents = self.getvalue()
                super().close()

        pipe = ArchivePipe()
        process = mock.Mock(stdin=pipe)
        process.wait.return_value = 0
        with (
            mock.patch.object(sys, "argv", ["simproto", "install"]),
            mock.patch.object(launcher.subprocess, "Popen", return_value=process),
        ):
            self.assertEqual(launcher.main(), 0)
        with tarfile.open(fileobj=io.BytesIO(pipe.contents)) as archive:
            self.assertEqual(archive.getmember("simproto").mode, 0o700)
            self.assertEqual(archive.getmember("setup.sh").mode, 0o700)
            self.assertEqual(archive.getmember("relay.py").mode, 0o600)
            self.assertEqual(
                archive.getmember("third_party/nvenc-gpu-filter").mode, 0o700
            )
            for name in ("setup.sh", "probe.sh"):
                self.assertNotIn(b"\r\n", archive.extractfile(name).read())

    @unittest.skipUnless(sys.platform == "win32", "Windows entry point")
    def test_cmd_wrapper_from_another_directory(self) -> None:
        env = os.environ.copy()
        env["PATH"] = (
            str(pathlib.Path(sys.executable).parent) + os.pathsep + env["PATH"]
        )
        result = subprocess.run(
            [str(SOURCE / "simproto.cmd"), "--help"],
            cwd=SOURCE.parent,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("connect", result.stdout)

    @unittest.skipUnless(sys.platform == "win32", "Windows client guard")
    def test_remote_mode_is_rejected_on_windows(self) -> None:
        with mock.patch.object(sys, "argv", ["simproto", "--remote", "status"]):
            with self.assertRaisesRegex(ValueError, "Linux Umbrella host"):
                launcher.main()


if __name__ == "__main__":
    unittest.main()
