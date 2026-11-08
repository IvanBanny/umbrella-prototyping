"""Chooses an Umbrella GPU node for the requested resources."""

from __future__ import annotations

import dataclasses
import datetime
import os
import re
import subprocess


@dataclasses.dataclass(frozen=True)
class Request:
    gpu: str = "auto"
    gpus: int = 1
    cpus: int = 4
    mem: str = "32G"
    time: str = "02:00:00"

    @property
    def memory_mib(self) -> int:
        """Converts the requested RAM to MiB."""
        match = re.fullmatch(r"([1-9][0-9]*)([MG])", self.mem.upper())
        if not match:
            raise ValueError("Memory must be a positive integer with M or G, e.g. 32G")
        return int(match[1]) * (1024 if match[2] == "G" else 1)


@dataclasses.dataclass(frozen=True)
class Target:
    name: str
    partition: str
    node: str
    gres: str

    def options(self, request: Request) -> list[str]:
        """Builds Slurm options for this node and GPU type."""
        return [
            f"--partition={self.partition}",
            f"--nodelist={self.node}",
            f"--gres=gpu:{self.gres}:{request.gpus}",
            "--nodes=1",
            "--ntasks=1",
            f"--cpus-per-task={request.cpus}",
            f"--mem={request.mem}",
            f"--time={request.time}",
        ]


TARGETS = (
    Target("l4-4", "tue.gpu2.q", "tue-gpub001", "nvidia_l4"),
    Target("l4-8", "tue.gpu3.q", "tue-gpuc001", "nvidia_l4"),
    Target("2080ti", "mcs.gpu.q", "mcs-gpub001", "nvidia_geforce_rtx_2080_ti"),
)


def command(args: list[str]) -> subprocess.CompletedProcess[str]:
    """Queries Slurm with a 45-second timeout."""
    return subprocess.run(args, capture_output=True, text=True, timeout=45)


def fields(text: str) -> dict[str, str]:
    """Parses the key=value fields in Slurm's one-line output."""
    return dict(re.findall(r"(\w+)=(\S+)", text))


def gpu_count(tres: str) -> int:
    """Reads the total GPU count, which already includes typed GPUs."""
    match = re.search(r"(?:^|,)gres/gpu=(\d+)", tres)
    return int(match[1]) if match else 0


@dataclasses.dataclass
class Candidate:
    target: Target
    free_gpus: int
    free_cpus: int
    free_memory: int
    total_gpus: int
    pressure: float
    immediate: bool
    estimate: str = "unknown"

    def rank(self) -> tuple[float, float, int]:
        """Ranks nodes by estimated start, queue pressure and TARGETS order."""
        try:
            timestamp = datetime.datetime.fromisoformat(self.estimate).timestamp()
        except ValueError:
            timestamp = float("inf")
        return timestamp, self.pressure, TARGETS.index(self.target)


def user_groups() -> set[str]:
    """Reads group membership on Umbrella, where the grp module is available."""
    import grp

    return {grp.getgrgid(gid).gr_name for gid in os.getgroups()}


def inspect(request: Request, forecasts: bool = True) -> list[Candidate]:
    """Checks node capacity, access and Slurm start estimates."""
    groups = user_groups()
    parts_result = command(["scontrol", "-o", "show", "partition"])
    parts_result.check_returncode()
    parts = {
        fields(line)["PartitionName"]: fields(line)
        for line in parts_result.stdout.splitlines()
    }
    jobs_result = command(
        ["squeue", "-a", "--array", "-h", "-t", "PD", "-o", "%i|%P|%b|%R"]
    )
    jobs_result.check_returncode()
    jobs = [line.split("|", 3) for line in jobs_result.stdout.splitlines()]
    candidates = []
    for target in TARGETS:
        if request.gpu not in ("auto", target.name):
            if request.gpu != "l4" or not target.name.startswith("l4-"):
                continue
        part = parts.get(target.partition)
        if not part or part.get("State") != "UP":
            continue
        allowed = part.get("AllowGroups", "ALL")
        if allowed != "ALL" and not groups.intersection(allowed.split(",")):
            continue
        result = command(["scontrol", "-o", "show", "node", target.node])
        result.check_returncode()
        node = fields(result.stdout)
        if any(
            s in node.get("State", "") for s in ("DOWN", "DRAIN", "FAIL", "UNKNOWN")
        ):
            continue
        total_gpus = gpu_count(node["CfgTRES"])
        total_cpus = int(node.get("CPUEfctv", node["CPUTot"]))
        total_memory = int(node["RealMemory"]) - int(node.get("MemSpecLimit", "0"))
        if (
            request.gpus > total_gpus
            or request.cpus > total_cpus
            or request.memory_mib > total_memory
        ):
            continue
        free_gpus = total_gpus - gpu_count(node.get("AllocTRES", ""))
        free_cpus = total_cpus - int(node["CPUAlloc"])
        free_memory = total_memory - int(node["AllocMem"])
        pressure = 0
        for job in jobs:
            if len(job) != 4:
                continue
            _, partitions, gres, reason = job
            if not set(partitions.split(",")).intersection(
                node["Partitions"].split(",")
            ):
                continue
            if any(s in reason for s in ("Dependency", "JobHeld", "Invalid", "Never")):
                continue
            typed = re.search(r"gpu:([^:,]+):(\d+)", gres)
            if typed and typed[1] != target.gres:
                continue
            generic = re.search(r"gpu:(\d+)(?:,|$)", gres)
            if not typed and not generic:
                continue
            count = int(generic[1]) if generic else 1
            pressure += int(typed[2]) if typed else count
        candidate = Candidate(
            target,
            free_gpus,
            free_cpus,
            free_memory,
            total_gpus,
            pressure / total_gpus,
            free_gpus >= request.gpus
            and free_cpus >= request.cpus
            and free_memory >= request.memory_mib,
        )
        if forecasts:
            estimate = command(
                ["sbatch", "--test-only", *target.options(request), "--wrap=true"]
            )
            output = estimate.stdout + estimate.stderr
            if estimate.returncode:
                print(f"{target.name}: request rejected: {output.strip()}", flush=True)
                continue
            match = re.search(
                r"(?:start time|to start at) (\d{4}-\d\d-\d\dT[\d:]+)", output
            )
            if match:
                candidate.estimate = match[1]
        candidates.append(candidate)
    return candidates


def describe(candidates: list[Candidate]) -> None:
    """Prints free resources and queue estimates for each candidate."""
    for candidate in candidates:
        print(
            f"{candidate.target.name:7} {candidate.target.node}: "
            f"free {candidate.free_gpus}/{candidate.total_gpus} GPUs, "
            f"{candidate.free_cpus} CPUs, {candidate.free_memory / 1024:.1f} GiB; "
            f"forecast {candidate.estimate}; "
            f"pending GPU requests/GPU {candidate.pressure:.1f}",
            flush=True,
        )
