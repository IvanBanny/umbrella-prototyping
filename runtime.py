"""Runs Isaac inside a Slurm allocation."""

from __future__ import annotations

import json
import os
import pathlib
import socket
import subprocess
import sys
import tempfile


def run(session: pathlib.Path, temporary: pathlib.Path) -> int:
    """Runs the selected mode with the allocated GPUs."""
    config = json.loads((session / "request.json").read_text())
    job = os.environ["SLURM_JOB_ID"]
    (session / "job.id").write_text(job)
    node = socket.gethostname().split(".")[0]
    print(f"Allocated job {job} on {node}", flush=True)
    if config["mode"] == "probe":
        return subprocess.call(
            ["bash", str(pathlib.Path(__file__).with_name("probe.sh"))]
        )

    root = session.parent.parent
    image = root / "images/isaac-lab-3.0.0.sif"
    project = pathlib.Path(config["project"]).expanduser().resolve()
    home = session / "home"
    cache = root / "cache" / node
    for directory in (
        home / ".cache",
        home / "Documents/Kit/shared",
        home / "kit-data",
        cache,
        session / "output",
        session / "kit-logs",
        temporary / "tmp",
        temporary / "var-tmp",
    ):
        directory.mkdir(parents=True, exist_ok=True)
    # isolate session state while reusing compiled shaders
    binds = {
        str(project): "/project",
        str(session / "output"): "/output",
        str(root / "smoke.py"): "/simproto-smoke.py",
        str(cache): "/root/.cache",
        str(session / "kit-logs"): "/isaac-sim/kit/logs",
        str(home / "kit-data"): "/isaac-sim/kit/data",
        str(temporary / "tmp"): "/tmp",
        str(temporary / "var-tmp"): "/var/tmp",
    }
    kit_cache = cache / "kit"
    kit_cache.mkdir(exist_ok=True)
    binds[str(kit_cache)] = "/isaac-sim/kit/cache"
    options = [
        "apptainer",
        "exec",
        "--nv",
        "--containall",
        "--cleanenv",
        "--home",
        f"{home}:/root",
        "--pwd",
        "/project",
    ]
    # Umbrella's --nv setup omits NGX; without it, DLSS ray reconstruction
    # fails and RTX Real-Time 2.0 renders a noisy image
    for lib_dir in ("/usr/lib64", "/usr/lib/x86_64-linux-gnu", "/lib/x86_64-linux-gnu"):
        ngx = pathlib.Path(lib_dir) / "libnvidia-ngx.so.1"
        if ngx.is_file():
            options += [
                "--bind",
                f"{ngx.resolve()}:/usr/lib/x86_64-linux-gnu/libnvidia-ngx.so.1:ro",
            ]
            break
    else:
        print("Warning: host NGX library missing; DLSS may be unavailable", flush=True)
    for source, destination in binds.items():
        if ":" in source or "," in source:
            raise ValueError("Project paths must not contain ':' or ','")
        options += ["--bind", f"{source}:{destination}"]
    if config["mode"] == "gui":
        # NVENC otherwise tries to initialize GPUs outside the Slurm allocation
        encoder_filter = temporary / "nvenc-gpu-filter.so"
        subprocess.run(
            [
                "cc",
                "-O2",
                "-shared",
                "-fPIC",
                "-o",
                str(encoder_filter),
                str(root / "third_party/nvenc-gpu-filter/nvenc-gpu-filter.c"),
                "-ldl",
                "-lpthread",
            ],
            check=True,
        )
        options += ["--bind", f"{encoder_filter}:/simproto-nvenc-filter.so"]
    env = os.environ.copy()
    variables = {
        "ACCEPT_EULA": "Y",
        "OMNI_KIT_ACCEPT_EULA": "yes",
        "OMNICLIENT_HUB_MODE": "disabled",
        "CUDA_VISIBLE_DEVICES": os.environ.get("CUDA_VISIBLE_DEVICES", ""),
        "OMP_NUM_THREADS": str(config["cpus"]),
        "PYTHONUNBUFFERED": "1",
        "PYTHONFAULTHANDLER": "1",
        "PYTHONPATH": "/project:/project/.packages",
        "TMPDIR": "/tmp",
    }
    for key, value in variables.items():
        env[f"APPTAINERENV_{key}"] = value
    if config["mode"] == "gui":
        env["APPTAINERENV_LD_PRELOAD"] = "/simproto-nvenc-filter.so"

    ports = []
    if config["mode"] == "gui":
        # hold both ports until we are ready to start Kit
        for _ in range(100):
            tcp = socket.socket()
            udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            tcp.bind(("0.0.0.0", 0))
            udp.bind(("0.0.0.0", 0))
            signal_port, stream_port = tcp.getsockname()[1], udp.getsockname()[1]
            if signal_port != stream_port:
                ports = [tcp, udp]
                break
            tcp.close()
            udp.close()
        if not ports:
            raise RuntimeError("Could not select streaming ports")
        address = socket.gethostbyname(node) if config.get("direct") else "127.0.0.1"
        connection = {
            "job": job,
            "node": node,
            "ip": address,
            "signal_port": signal_port,
            "stream_port": stream_port,
            "direct": config.get("direct", False),
        }
        (session / "connection.json").write_text(json.dumps(connection, indent=2))
        print(f"Streaming endpoint: {json.dumps(connection)}", flush=True)
        print("Wait for Isaac's loaded message before connecting", flush=True)
        app = [
            "/isaac-sim/runheadless.sh",
            "--/renderer/multiGpu/enabled=false",
            "--/renderer/activeGpu=0",
            "--/exts/omni.kit.livestream.app/primaryStream/enableEventTracing=false",
            "--/log/channels/omni.kit.livestream.streamsdk=info",
            "--/log/channels/omni.kit.livestream.webrtc=info",
            "--/log/channels/omni.kit.livestream.app=info",
            f"--/exts/omni.kit.livestream.app/primaryStream/publicIp={address}",
            f"--/exts/omni.kit.livestream.app/primaryStream/signalPort={signal_port}",
            f"--/exts/omni.kit.livestream.app/primaryStream/streamPort={stream_port}",
        ]
    elif config["mode"] == "check":
        app = ["/workspace/isaaclab/isaaclab.sh", "-p", "/simproto-smoke.py"]
    else:
        app = config["command"]
        # apptainer exec does not load Docker's interactive shell aliases
        if app[0] in ("python", "python3"):
            app = ["/workspace/isaaclab/isaaclab.sh", "-p", *app[1:]]
        elif app[0] == "isaaclab":
            app = ["/workspace/isaaclab/isaaclab.sh", *app[1:]]
    for port in ports:
        port.close()
    return subprocess.call([*options, str(image), *app], env=env)


def main() -> int:
    """Uses job scratch for temporary files to avoid filling tmpfs."""
    with tempfile.TemporaryDirectory(
        prefix="simproto-", dir=os.environ.get("SLURM_TMPDIR")
    ) as temporary:
        return run(pathlib.Path(sys.argv[1]), pathlib.Path(temporary))


if __name__ == "__main__":
    sys.exit(main())
