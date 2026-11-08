"""Tests session IDs, default selection and cancellation with mocked jobs."""

import importlib.machinery
import importlib.util
import io
import json
import pathlib
import subprocess
import tempfile
import unittest
from unittest import mock

loader = importlib.machinery.SourceFileLoader(
    "session_launcher", str(pathlib.Path(__file__).resolve().parents[1] / "simproto")
)
spec = importlib.util.spec_from_loader(loader.name, loader)
launcher = importlib.util.module_from_spec(spec)
loader.exec_module(launcher)


class SessionTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = pathlib.Path(temporary.name)
        patch = mock.patch.object(launcher, "ROOT", self.root)
        patch.start()
        self.addCleanup(patch.stop)

    def session(self, name: str) -> pathlib.Path:
        path = self.root / "runs" / name
        path.mkdir(parents=True)
        (path / "request.json").write_text("{}")
        (path / "launcher.log").write_text("retained log")
        return path

    def test_full_and_short_ids_resolve_to_same_session(self) -> None:
        path = self.session("20261108-182745-d6efd4")
        self.assertEqual(launcher.session_path(path.name), path)
        self.assertEqual(launcher.session_path("d6efd4"), path)
        self.assertEqual(launcher.session_id(path), "d6efd4")

    def test_unknown_and_unsafe_ids_are_rejected(self) -> None:
        self.session("20261108-182745-d6efd4")
        for name in ("missing", "../runs", "*", "", "/tmp"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                launcher.session_path(name)

    def test_ambiguous_suffix_requires_full_id(self) -> None:
        first = self.session("20261108-182745-d6efd4")
        second = self.session("20261109-182745-d6efd4")
        with self.assertRaisesRegex(ValueError, "Ambiguous session"):
            launcher.session_path("d6efd4")
        for path in (first, second):
            self.assertEqual(launcher.session_id(path), path.name)
            self.assertEqual(launcher.session_path(path.name), path)

    def test_exact_name_takes_precedence_over_suffix(self) -> None:
        long = self.session("20261108-182745-d6efd4")
        short = self.session("d6efd4")
        self.assertEqual(launcher.session_path("d6efd4"), short)
        self.assertEqual(launcher.session_id(long), long.name)

    def test_stop_parser_accepts_no_id(self) -> None:
        self.assertIsNone(launcher.parser().parse_args(["stop"]).session)
        self.assertEqual(
            launcher.parser().parse_args(["stop", "d6efd4"]).session, "d6efd4"
        )

    def test_session_defaults_and_ambiguity(self) -> None:
        finished = self.session("20261108-172745-def456")
        (finished / "exit.code").write_text("0")
        self.assertEqual(launcher.select_session(None, recent=True), finished)
        with self.assertRaisesRegex(ValueError, "No active session"):
            launcher.select_session(None)
        current = self.session("20261108-182745-d6efd4")
        (current / "request.json").write_text('{"mode": "gui"}')
        self.assertEqual(launcher.select_session(None), current)
        batch = self.session("20261108-182746-abc123")
        (batch / "request.json").write_text('{"mode": "run"}')
        self.assertEqual(launcher.select_session(None, gui=True), current)
        with self.assertRaisesRegex(ValueError, "Several sessions"):
            launcher.select_session(None)

    def test_endpoint_waits_through_queue_and_startup(self) -> None:
        path = self.session("20261108-182745-d6efd4")
        (path / "request.json").write_text('{"mode": "gui"}')
        connection = {"node": "gpu-node", "stream_port": 1234, "signal_port": 4321}
        calls = []

        def progress(seconds: float) -> None:
            calls.append(seconds)
            if len(calls) == 1:
                (path / "job.id").write_text("41")
                (path / "connection.json").write_text(json.dumps(connection))
            else:
                (path / "launcher.log").write_text(
                    "Isaac Sim Full Streaming App is loaded."
                )

        with (
            mock.patch.object(launcher, "jobs", side_effect=[[], ["41"], ["41"]]),
            mock.patch.object(launcher.time, "sleep", side_effect=progress),
            mock.patch("sys.stderr", new_callable=io.StringIO) as output,
        ):
            self.assertEqual(launcher.endpoint(path, wait=True), connection)
        self.assertEqual(len(calls), 2)
        self.assertIn("Waiting for a GPU", output.getvalue())
        self.assertIn("Starting Isaac", output.getvalue())

    def test_endpoint_rejects_gpu_failure_even_after_loaded(self) -> None:
        path = self.session("20261108-182745-d6efd4")
        (path / "request.json").write_text('{"mode": "gui"}')
        (path / "launcher.log").write_text("App is loaded.\ncudaErrorIllegalAddress")
        with self.assertRaisesRegex(RuntimeError, "GPU error"):
            launcher.endpoint(path, wait=True)

    def test_endpoint_does_not_wait_forever_on_failed_or_batch_session(self) -> None:
        path = self.session("20261108-182745-d6efd4")
        (path / "request.json").write_text('{"mode": "run"}')
        with self.assertRaisesRegex(ValueError, "Only GUI"):
            launcher.endpoint(path, wait=True)
        (path / "request.json").write_text('{"mode": "gui"}')
        (path / "exit.code").write_text("1")
        with self.assertRaisesRegex(ValueError, "has ended"):
            launcher.endpoint(path, wait=True)

    def test_jobs_are_scoped_to_user_and_exact_session(self) -> None:
        path = self.session("20261108-182745-d6efd4")
        output = (
            f"41|simproto:{path.name}\n"
            f"42|simproto:{path.name}-other\n"
            "43|unrelated-training\n"
        )
        with (
            mock.patch.object(launcher.os, "getuid", return_value=123, create=True),
            mock.patch.object(
                launcher.scheduler,
                "command",
                return_value=subprocess.CompletedProcess([], 0, output, ""),
            ) as query,
        ):
            self.assertEqual(launcher.jobs(path), ["41"])
        self.assertEqual(query.call_args.args[0][1:5], ["-h", "-u", "123", "-o"])

    def test_stop_all_handles_running_and_waiting_sessions(self) -> None:
        running = self.session("20261108-182745-d6efd4")
        waiting = self.session("20261108-182746-abc123")
        finished = self.session("20261108-172745-def456")
        (finished / "exit.code").write_text("0")
        setup_log = self.root / "runs/setup-40.log"
        setup_log.write_text("retained setup log")
        with (
            mock.patch.object(
                launcher, "jobs", side_effect=lambda p: ["41"] if p == running else []
            ),
            mock.patch.object(launcher.subprocess, "run") as cancel,
        ):
            self.assertEqual(launcher.stop_all(), 0)
            cancel.assert_called_once_with(["scancel", "41"], check=True)
        self.assertTrue((running / "stop").exists())
        self.assertTrue((waiting / "stop").exists())
        self.assertFalse((finished / "stop").exists())
        for path in (running, waiting, finished):
            self.assertEqual((path / "request.json").read_text(), "{}")
            self.assertEqual((path / "launcher.log").read_text(), "retained log")
        self.assertEqual(setup_log.read_text(), "retained setup log")

    def test_live_allocation_is_stopped_despite_existing_markers(self) -> None:
        path = self.session("20261108-182745-d6efd4")
        (path / "stop").touch()
        (path / "exit.code").write_text("143")
        with (
            mock.patch.object(launcher, "jobs", return_value=["41"]),
            mock.patch.object(launcher.subprocess, "run") as cancel,
        ):
            self.assertEqual(launcher.stop_all(), 0)
            cancel.assert_called_once_with(["scancel", "41"], check=True)

    def test_empty_or_already_stopped_is_successful(self) -> None:
        with mock.patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(launcher.stop_all(), 0)
        self.assertIn("No active simproto sessions", output.getvalue())
        path = self.session("20261108-182745-d6efd4")
        (path / "stop").touch()
        with (
            mock.patch.object(launcher, "jobs", return_value=[]),
            mock.patch.object(launcher.subprocess, "run") as cancel,
        ):
            self.assertEqual(launcher.stop_all(), 0)
            cancel.assert_not_called()

    def test_one_cancellation_failure_does_not_skip_remaining_sessions(self) -> None:
        first = self.session("20261108-182745-d6efd4")
        second = self.session("20261108-182746-abc123")
        with (
            mock.patch.object(
                launcher, "jobs", side_effect=lambda p: ["41" if p == first else "42"]
            ),
            mock.patch.object(
                launcher.subprocess,
                "run",
                side_effect=[subprocess.CalledProcessError(1, "scancel"), None],
            ) as cancel,
            mock.patch("sys.stderr", new_callable=io.StringIO) as errors,
        ):
            self.assertEqual(launcher.stop_all(), 1)
        self.assertEqual(cancel.call_count, 2)
        self.assertIn("could not stop", errors.getvalue())
        self.assertTrue((second / "stop").exists())

    def test_controller_is_stopped_before_querying_allocations(self) -> None:
        path = self.session("20261108-182745-d6efd4")
        (path / "controller.pid").write_text("12345")
        command = b"\0".join(
            [
                b"python3",
                str(self.root / "simproto").encode(),
                b"--remote",
                b"_work",
                path.name.encode(),
            ]
        )
        events = []
        with (
            mock.patch.object(pathlib.Path, "read_bytes", return_value=command),
            mock.patch.object(
                launcher.os,
                "killpg",
                side_effect=lambda *args: events.append("kill"),
                create=True,
            ) as kill,
            mock.patch.object(
                launcher, "jobs", side_effect=lambda p: events.append("query") or []
            ),
        ):
            launcher.stop_session(path)
        self.assertEqual(events, ["kill", "query"])
        kill.assert_called_once_with(12345, launcher.signal.SIGTERM)

    def test_stale_controller_pid_does_not_kill_another_process(self) -> None:
        path = self.session("20261108-182745-d6efd4")
        (path / "controller.pid").write_text("12345")
        with (
            mock.patch.object(pathlib.Path, "read_bytes", return_value=b"unrelated\0"),
            mock.patch.object(launcher.os, "killpg", create=True) as kill,
            mock.patch.object(launcher, "jobs", return_value=[]),
        ):
            launcher.stop_session(path)
            kill.assert_not_called()


if __name__ == "__main__":
    unittest.main()
