"""Tests node selection and allocation retries with mocked Slurm responses."""

import dataclasses
import importlib.machinery
import importlib.util
import json
import pathlib
import subprocess
import tempfile
import unittest
import unittest.mock

import scheduler

loader = importlib.machinery.SourceFileLoader(
    "launcher", str(pathlib.Path(__file__).resolve().parents[1] / "simproto")
)
spec = importlib.util.spec_from_loader(loader.name, loader)
launcher = importlib.util.module_from_spec(spec)
loader.exec_module(launcher)


class SelectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.free = {"l4-4": (1, 8, 64), "l4-8": (3, 16, 96), "2080ti": (2, 16, 64)}
        self.allowed = True
        self.jobs = ""
        self.forecast = "2026-11-10T04:19:51"
        self.patch = unittest.mock.patch.object(scheduler, "command", self.query)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        groups = unittest.mock.patch.object(
            scheduler, "user_groups", return_value={"test-users"}
        )
        groups.start()
        self.addCleanup(groups.stop)

    def query(self, args: list[str]) -> subprocess.CompletedProcess:
        text = ""
        if args[0] == "squeue":
            text = self.jobs
        elif args[0] == "sbatch":
            text = f"sbatch: Job 42 to start at {self.forecast} a using 4 processors"
        elif args[-1] == "partition":
            text = "\n".join(
                f"PartitionName={t.partition} AllowGroups=ALL State=UP"
                for t in scheduler.TARGETS
                if self.allowed or t.name != "2080ti"
            )
        else:
            target = next(t for t in scheduler.TARGETS if t.node == args[-1])
            gpu, cpu, mem = self.free[target.name]
            total = {"l4-4": 4, "l4-8": 8, "2080ti": 6}[target.name]
            parts = target.partition + (",tue.gpu.q" if target.name != "2080ti" else "")
            text = (
                f"State=MIXED CPUTot=32 CPUEfctv=32 CPUAlloc={32 - cpu} "
                f"RealMemory=131072 AllocMem={131072 - mem * 1024} "
                f"CfgTRES=gres/gpu={total},gres/gpu:{target.gres}={total} "
                f"AllocTRES=gres/gpu={total - gpu} Partitions={parts}"
            )
        return subprocess.CompletedProcess(args, 0, text, "")

    def test_order_and_real_slurm_forecast(self) -> None:
        choices = scheduler.inspect(scheduler.Request())
        self.assertEqual([c.target.name for c in choices], ["l4-4", "l4-8", "2080ti"])
        self.assertTrue(all(c.immediate for c in choices))
        self.assertEqual(choices[0].estimate, self.forecast)

    def test_memory_and_cpu_shortages_prevent_immediate(self) -> None:
        self.free["l4-4"] = (1, 8, 16)
        self.free["l4-8"] = (8, 2, 96)
        choices = scheduler.inspect(scheduler.Request())
        self.assertEqual([c.immediate for c in choices], [False, False, True])

    def test_explicit_gpu_never_falls_back_to_other_type(self) -> None:
        choices = scheduler.inspect(scheduler.Request(gpu="2080ti"))
        self.assertEqual([c.target.name for c in choices], ["2080ti"])
        choices = scheduler.inspect(scheduler.Request(gpu="l4"))
        self.assertEqual([c.target.name for c in choices], ["l4-4", "l4-8"])

    def test_inaccessible_partition_and_impossible_request(self) -> None:
        self.allowed = False
        self.assertFalse(scheduler.inspect(scheduler.Request(gpu="2080ti")))
        self.assertFalse(scheduler.inspect(scheduler.Request(gpus=9)))
        self.assertFalse(scheduler.inspect(scheduler.Request(mem="256G")))

    def test_shared_queue_tasks_count_once_and_skip_held(self) -> None:
        self.jobs = (
            "1|tue.gpu.q,tue.gpu3.q|gres/gpu:nvidia_l4:2|Resources\n"
            "2|tue.gpu.q|gres/gpu:nvidia_l4:1|JobHeldUser\n"
            "3|tue.gpu.q|gres/gpu:1g.6gb:1|Resources\n"
            "4|tue.gpu.q|N/A|Resources\n"
        )
        choices = scheduler.inspect(scheduler.Request())
        self.assertEqual(choices[1].pressure, 2 / 8)

    def test_estimate_precedes_queue_length(self) -> None:
        a, b = scheduler.inspect(scheduler.Request())[:2]
        a.estimate = "2026-11-12T00:00:00"
        a.pressure = 0
        b.pressure = 100
        self.assertEqual(min([a, b], key=lambda c: c.rank()), b)

    def test_unknown_forecasts_use_pressure(self) -> None:
        a, b = scheduler.inspect(scheduler.Request())[:2]
        a.estimate = b.estimate = "unknown"
        a.pressure, b.pressure = 1, 0
        self.assertEqual(min([a, b], key=lambda c: c.rank()), b)

    def test_resource_options_keep_gres_type(self) -> None:
        args = scheduler.TARGETS[2].options(
            scheduler.Request(gpus=2, cpus=8, mem="48G")
        )
        self.assertIn("--gres=gpu:nvidia_geforce_rtx_2080_ti:2", args)
        self.assertIn("--mem=48G", args)


class LaunchTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.session = pathlib.Path(self.temp.name)
        config = {**dataclasses.asdict(scheduler.Request()), "mode": "probe"}
        (self.session / "request.json").write_text(json.dumps(config))
        self.choices = [
            scheduler.Candidate(t, 1, 8, 65536, 4, 0, True) for t in scheduler.TARGETS
        ]

    def test_only_unavailable_allocations_are_retried(self) -> None:
        with (
            unittest.mock.patch.object(scheduler, "inspect", return_value=self.choices),
            unittest.mock.patch.object(launcher, "jobs", return_value=[]),
            unittest.mock.patch.object(subprocess, "call", side_effect=[75, 0]) as run,
        ):
            self.assertEqual(launcher.worker(self.session), 0)
            self.assertEqual(run.call_count, 2)
            self.assertIn("--partition=tue.gpu2.q", run.call_args_list[0].args[0])
            self.assertIn("--partition=tue.gpu3.q", run.call_args_list[1].args[0])

    def test_application_exit_75_never_starts_second_job(self) -> None:
        def application(args: list[str], **kwargs: object) -> int:
            (self.session / "job.id").write_text("42")
            return 75

        with (
            unittest.mock.patch.object(scheduler, "inspect", return_value=self.choices),
            unittest.mock.patch.object(
                subprocess, "call", side_effect=application
            ) as run,
        ):
            self.assertEqual(launcher.worker(self.session), 75)
            self.assertEqual(run.call_count, 1)

    def test_ambiguous_allocation_stops(self) -> None:
        with (
            unittest.mock.patch.object(scheduler, "inspect", return_value=self.choices),
            unittest.mock.patch.object(launcher, "jobs", return_value=["42"]),
            unittest.mock.patch.object(subprocess, "call", return_value=75) as run,
        ):
            with self.assertRaises(RuntimeError):
                launcher.worker(self.session)
            self.assertEqual(run.call_count, 1)

    def test_none_immediate_queues_exactly_one(self) -> None:
        for c in self.choices:
            c.immediate = False
        self.choices[1].estimate = "2026-11-08T14:00:00"
        with (
            unittest.mock.patch.object(scheduler, "inspect", return_value=self.choices),
            unittest.mock.patch.object(subprocess, "call", return_value=0) as run,
        ):
            self.assertEqual(launcher.worker(self.session), 0)
            self.assertEqual(run.call_count, 1)
            self.assertIn("--partition=tue.gpu3.q", run.call_args.args[0])
            self.assertNotIn("--immediate=1", run.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
