# Umbrella prototyping

`simproto` runs Isaac Sim and Isaac Lab on Umbrella and streams the editor to your laptop. It works (I hope) on Linux, macOS and Windows (no WSL needed). If anything's broken - your cli agent should be able to fix it easily. Same goes for working with Isaac Sim and Isaac Lab; they have a nice agents skills collection. The container has Isaac Sim 6.2, Isaac Lab 3.0.0 and Python 3.12.

## Setup

### 1. Get access to Umbrella

Request your own account through [TU/e's Umbrella account page](https://supercomputing.tue.nl/documentation/steps/request/). Ask your nearest Project Lead or a certified Serpentine Tech Lead for details as to how you should fill the form.

Install [eduVPN](https://www.eduvpn.org/client-apps/), select TU/e, sign in with your university account and connect using the **student-split** profile. Keep it connected while using Umbrella from outside the TU/e network. TU/e's [cluster access guide](https://supercomputing.tue.nl/documentation/steps/access/) links to the university's VPN instructions.

### 2. Install the local tools

You need Git, Python 3.9+ and OpenSSH on your laptop. The launcher uses only the Python standard library, so there's no local environment or pip install to do.

On Windows, install ~~a Linux distribution~~, [Git](https://gitforwindows.org/), and [Python](https://www.python.org/downloads/windows/). Enable Python on PATH, open a new PowerShell window and check:

```powershell
git --version
python --version
ssh -V
```

If `python` opens the Microsoft Store, finish installing Python and reopen the terminal. If you use the Python launcher, `py -3 simproto` works in place of `.\simproto.cmd` below. If SSH is missing, install **OpenSSH Client** in Windows Optional Features; see [Microsoft's instructions](https://learn.microsoft.com/en-us/windows-server/administration/openssh/openssh_install_firstuse).

On Linux/macOS, check `git --version`, `python3 --version` and `ssh -V` and install any missing tools with your package manager.

### 3. Set up SSH keys

The launcher opens several SSH connections. Set up key authentication so these can run without asking for your Umbrella password each time.

Use an existing key if you have one, or create a dedicated key below. If that filename already exists, reuse it or choose another name; don't overwrite it. Use a passphrase and load the key into your SSH agent.

Linux/macOS:

```bash
mkdir -p ~/.ssh
chmod 700 ~/.ssh
ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519_umbrella
cat ~/.ssh/id_ed25519_umbrella.pub | ssh YOUR_USERNAME@hpc.tue.nl 'umask 077; mkdir -p ~/.ssh; cat >> ~/.ssh/authorized_keys; chmod 700 ~/.ssh; chmod 600 ~/.ssh/authorized_keys'
ssh-add ~/.ssh/id_ed25519_umbrella
```

If no agent is running, run `eval "$(ssh-agent -s)"` before `ssh-add`.

Windows (PowerShell):

```powershell
New-Item -ItemType Directory -Force -Path "$HOME\.ssh" | Out-Null
ssh-keygen -t ed25519 -f "$HOME\.ssh\id_ed25519_umbrella"
Get-Content "$HOME\.ssh\id_ed25519_umbrella.pub" | ssh YOUR_USERNAME@hpc.tue.nl 'umask 077; mkdir -p ~/.ssh; cat >> ~/.ssh/authorized_keys; chmod 700 ~/.ssh; chmod 600 ~/.ssh/authorized_keys'
```

Use your Umbrella username in place of `YOUR_USERNAME`. This first connection asks for your account password. Check the server's host-key fingerprint against TU/e's information before accepting it. Only the `.pub` file goes to Umbrella; the private key stays on your laptop.

On Windows, enable the SSH agent once in an administrator PowerShell:

```powershell
Set-Service ssh-agent -StartupType Automatic
Start-Service ssh-agent
```

Then return to a normal PowerShell and load your key:

```powershell
ssh-add "$HOME\.ssh\id_ed25519_umbrella"
```

### 4. Add the ugpu SSH alias

Edit `~/.ssh/config` on Linux/macOS or `$HOME\.ssh\config` on Windows. Create the file if needed; keep any existing entries. On Windows you can open it with `notepad "$HOME\.ssh\config"`. Make sure it's saved as `config`, without `.txt`.

```sshconfig
Host ugpu
    HostName hpc.tue.nl
    User YOUR_USERNAME
    IdentityFile ~/.ssh/id_ed25519_umbrella
    IdentitiesOnly yes
    ServerAliveInterval 30
    ServerAliveCountMax 3
```

Replace the username and, if you used another key, its filename. On Linux/macOS, run `chmod 600 ~/.ssh/config`. Test from your laptop:

```text
ssh -o BatchMode=yes ugpu hostname
```

This should print a login-node hostname without a password prompt. Get this working before continuing. A timeout usually means a network/VPN problem; `Permission denied` means you should check your username, key and account access.

### 5. Install the launcher and container

Run locally:

```text
git clone https://github.com/IvanBanny/umbrella-prototyping.git
cd umbrella-prototyping
```

Linux/macOS:

```bash
./simproto install
./simproto setup
./simproto status
```

Windows:

```powershell
.\simproto.cmd install
.\simproto.cmd setup
.\simproto.cmd status
```

Use `.\simproto.cmd` in place of `./simproto` in the rest of this README. `python simproto ...` also works on any platform.

`install` copies the launcher to `~/simproto` on Umbrella. `setup` submits a CPU job to build the container and prints its job ID. Wait for the build to finish before starting Isaac. Replace `JOB_ID` below with that number (works in PowerShell too):

```text
ssh ugpu 'tail -f ~/simproto/runs/setup-JOB_ID.log'
```

The log appears once the job starts. Wait for `Installed:` or `Image already installed:`, then press Ctrl+C. The image is about 13 GiB(Isaac be big like that) and stays in your Umbrella home along with the caches; check your home quota before building. Temporary build files use job scratch and are removed afterwards. Running `setup` again reuses the image. Starting Isaac accepts NVIDIA's EULA through the container environment.

### 6. Install the streaming client

Download and install the **Isaac Sim WebRTC Streaming Client 2.0** for your OS from [NVIDIA's download page](https://docs.isaacsim.omniverse.nvidia.com/6.2.0/installation/download.html#latest-release). You only need the client locally. The full Isaac installation runs on Umbrella. Skip this step if you'll only run headless jobs.

On Ubuntu/Debian, install the downloaded `.deb` with `sudo apt install ./FILE.deb`. This also sets up Electron's sandbox; an extracted copy can fail on Ubuntu 24.04. If the launcher can't find your client, set `SIMPROTO_CLIENT` to its executable path (`$env:SIMPROTO_CLIENT = 'C:\path\to\client.exe'` in PowerShell, or `export SIMPROTO_CLIENT=/path/to/client` in Bash).

## Open the editor

```bash
./simproto start
```

This requests one GPU, four CPU cores and 32 GiB RAM for two hours, waits for Isaac to load, then opens the SSH tunnel and streaming client. The first launch can take several minutes to compile shaders; later launches reuse the cache. Keep the terminal open while you work. Each editor accepts one viewer.

Start with NVIDIA's [basic usage tutorial](https://docs.isaacsim.omniverse.nvidia.com/6.2.0/introduction/quickstart_isaacsim.html) to learn the editor. The [Isaac Sim 6.2 docs](https://docs.isaacsim.omniverse.nvidia.com/6.2.0/index.html) cover scripting, sensors and robot import. These links match the container's version. Save scenes under `/project`, which is `~/simproto/project` on Umbrella. Try saving and reopening a small scene before doing longer work.

Closing the client (Quit on macOS) or pressing Ctrl+C disconnects **and leaves the session running**. To return:

```bash
./simproto connect
```

When you're done, save your work and release the allocation:

```bash
./simproto stop
```

`stop` stops all your simproto sessions, including queued ones, and keeps their files. Use `stop SESSION` to stop one. Other Slurm jobs are unaffected. The job also ends at its time limit, **so save before then**. In fact - you might want to go ahead and save frequently, cause I'm not quite too sure of the stability of this setup. A login-node reboot can interrupt the session controllers.

`status` lists your jobs and recent sessions. `connect` selects your sole active editor; `logs` selects your sole active session, or the latest finished one. If there are several, choose one of the printed short IDs:

```bash
./simproto connect d6efd4
./simproto logs d6efd4 --follow
./simproto stop d6efd4
```

## Check availability and choose resources

```bash
./simproto plan
./simproto plan --gpu l4 --gpus 1 --cpus 8 --mem 48G --time 04:00:00
```

`plan` checks the request without allocating anything. For each accessible node it prints free GPUs, CPU cores and RAM, a start estimate, and pending GPU requests per GPU. All requested resources must fit on the same node. A free GPU can still be waiting for CPU cores or RAM, and Slurm can reserve free resources for higher-priority jobs. The forecast can change.

The launcher supports these nodes (usable RTX nodes we have access to):

| `--gpu` | Node | Partition | Total GPUs | VRAM per GPU | Slurm CPUs | RAM |
| --- | --- | --- | --- | --- | --- | --- |
| `l4-4` | `tue-gpub001` | `tue.gpu2.q` | 4 x L4 | 24 GB | 32 | ~251 GiB |
| `l4-8` | `tue-gpuc001` | `tue.gpu3.q` | 8 x L4 | 24 GB | 192 | ~504 GiB |
| `2080ti` | `mcs-gpub001` | `mcs.gpu.q` | 6 x RTX 2080 Ti | 11 GB | 56 | ~238 GiB |

Both L4 partitions currently allow jobs up to five days (`--time 5-00:00:00`). The MCS partition has no partition-level time limit and is usually more free, giving an immediate job allocation. Account or QoS limits can still restrict a request. Run `plan` with your intended resources before a long job.

`l4` allows either L4 node. The default, `auto`, tries free capacity in the table's order. If nothing fits immediately, it queues one job using Slurm's earliest start estimate, then pending requests per GPU to break ties. It counts jobs from overlapping partitions too. Once queued, the job stays on that node. Inaccessible partitions are skipped; failed applications aren't resubmitted.

I'd use one L4 for camera work or larger scenes. The 2080 Ti works alright too (but, technically, 11 GB VRAM is below recommended NVIDIA specs, cause, let's be real, you should probably buy a newer gpu from NVIDIA). The native Windows client has passed the vibe check and a quick test in action on my Windows partition, which God abandoned long ago.

Both `plan` and `start` accept:

| Option | Default | What to choose |
| --- | --- | --- |
| `--gpu` | `auto` | `l4` for either L4 node, or a specific choice above |
| `--gpus` | `1` | Keep one for the editor; request more for code that uses multiple GPUs |
| `--cpus` | `4` | Start with 4 for editing; try 8 for heavier simulation or data loading |
| `--mem` | `32G` | Total system RAM for the job; try `48G` or `64G` when the workload needs more |
| `--time` | `02:00:00` | Time for the whole job, including Isaac startup; accepts `HH:MM:SS` or `D-HH:MM:SS` |

Memory accepts positive whole numbers ending in `M` or `G`. It is separate from GPU VRAM; you get the whole GPU. More GPUs don't pool their VRAM or automatically distribute training; your script has to use them. The editor renders on one GPU.

For example, an L4 editor session with more room for simulation:

```bash
./simproto start --gpu l4 --cpus 8 --mem 48G --time 04:00:00
```

Request what you'll use; larger requests are harder to fit. The table lists hardware totals; your account and partition limits can be lower. To inspect Umbrella yourself, run `ssh ugpu`, then:

```bash
squeue -a -p tue.gpu2.q,tue.gpu3.q,mcs.gpu.q
sinfo -N -p tue.gpu2.q,tue.gpu3.q,mcs.gpu.q -o "%N %P %t %c %m %G"
scontrol show node tue-gpub001
scontrol show partition tue.gpu2.q
sacctmgr show assoc where user=$USER format=Account,Partition,QOS,MaxTRES,GrpTRES
sacctmgr show qos format=Name,MaxWall,MaxTRESPJ,MaxTRESPU,GrpTRES
```

In [`squeue`](https://slurm.schedmd.com/squeue.html), `R` means running and `PD` means pending; the last column gives the pending reason. In the node output, compare `CfgTRES` with `AllocTRES` for total and allocated resources. `AllocMem` is reserved RAM; `FreeMem` is the OS's current reading and doesn't tell you how much Slurm will allocate. Partition output shows limits such as `MaxTime` and access groups. [Association and QoS output](https://slurm.schedmd.com/sacctmgr.html) shows account limits when the cluster exposes them. Run `plan` with your exact request to check Slurm's response; `start` is still subject to Slurm's final decision. Exit the SSH shell when you're done inspecting.

## Work on code

Edit `~/simproto/project` through your editor's SSH support (for example, VS Code Remote - SSH with host `ugpu`), or copy a local project there:

```bash
rsync -av --exclude .git --exclude .venv ./my-project/ ugpu:simproto/project/
./simproto start --mode run -- python /project/my_sim.py --headless
```

On Windows, OpenSSH includes `scp`:

```powershell
scp -r .\my-project ugpu:simproto/
.\simproto.cmd start --mode run --project '~/simproto/my-project' -- python /project/my_sim.py --headless
```

Copy only the files you need; `scp` also copies local environments and Git metadata. `--project` names a directory on Umbrella. Inside the container it becomes `/project` and the working directory; it is also on Python's import path, including in the Script Editor. Use `--project /absolute/remote/path` for an existing remote checkout.

Headless jobs submit and return immediately. Follow them with `status` and `logs SESSION --follow`. `/output` is separate for each session and maps to `~/simproto/runs/SESSION/output` (use the full directory name from Umbrella). Write run results there and copy back what you need:

```text
scp -r ugpu:simproto/runs/FULL_SESSION_ID/output ./output
```

The container is read-only. `python`, `python3` and `isaaclab` load the bundled Isaac Lab environment. Extra packages can go in `/project/.packages`:

```bash
./simproto start --mode run -- python -m pip install --target /project/.packages -r /project/requirements.txt
```

Keep the image's PyTorch, CUDA and Isaac versions when adding dependencies. Back up your code and results outside Umbrella.

For a small Isaac Lab training run:

```bash
./simproto start --mode run --gpu l4 --cpus 8 --mem 48G --time 01:00:00 -- isaaclab train --task Isaac-Cartpole --num_envs 64 --max_iterations 100 physics=isaacsim_physx
```

Lab writes relative training logs under `/project/logs`. See the [Isaac Lab 3.0 quickstart](https://docs.nvidia.com/isaaclab/3.0/isaac-lab/quickstart) and use `isaaclab train --help` through `--mode run` for the installed CLI's options.

## Other launcher options

| Option | Use |
| --- | --- |
| `start --mode gui` | Stream the editor (default) |
| `start --mode run -- COMMAND` | Run your command in the container |
| `start --mode check` | Check physics and camera rendering |
| `start --mode probe` | Inspect the GPU and driver without loading Isaac |
| `start --detach` | Submit the editor without waiting or connecting |
| `start --project REMOTE_PATH` | Mount an existing remote project at `/project` |
| `start --no-client` / `connect --no-client` | Open the tunnel and print settings for a manual client |
| `start --local-port 49101` / `connect --local-port 49101` | Use another local signaling port if 49100 is busy |
| `start --direct` | Connect directly to the compute node; requires TCP and UDP reachability |
| `--host ALIAS` | Use another SSH alias; put this before the command |

`SIMPROTO_HOST` also overrides `ugpu`. Use `--help` or `start --help` for the CLI. The default tunnel sends signaling and UDP media through SSH via the login node. This works without a direct route to the compute node, though sending media over TCP can add latency. Direct streaming has no user authentication; keep it on the private cluster network.

## Troubleshooting

If a scene import turns the stream black, check `status` and `logs` first. A long loading stall can disconnect video while Isaac keeps running (quite a probable scenario). Wait for loading to finish, close the client, press Ctrl+C in the tunnel terminal and run `connect SESSION` again. Reopen the tunnel as well as the client; a new client can't reuse the old UDP endpoint. If the job ended or reports a GPU error, read its logs before stopping it and starting another session.

For a manual Windows connection, run `connect --no-client`, then `.\stream-client.cmd` in another PowerShell window and enter the printed ports. The helper adds `--allow-loopback-in-peer-connection`; the normal Desktop shortcut omits it and can give a black screen with STUN timeouts. It accepts an executable path if the client is installed elsewhere. On Linux, use `isaacsim-webrtc-streaming-client --allow-loopback-in-peer-connection`.

Isaac 6.2 can show repeated `nvstTraceClientBlobStats` / `NVST_R_INVALID_STATE` messages during a working stream ([upstream issue](https://github.com/isaac-sim/IsaacSim/issues/917)). That probably means you're doing just fine, champ. Check whether video and input still work before restarting.

Heavy speckled noise in RTX Real-Time 2.0 can mean DLSS failed to initialize; look for `Failed to initialize NGX` in the Kit logs. The runtime mounts the host's `libnvidia-ngx.so.1`, which Umbrella's default Apptainer setup omits. After updating the launcher, run `install` and start a fresh session to pick up mount changes. Save your scene first. The launcher also uses an [NVENC filter](third_party/nvenc-gpu-filter/README.md) to keep the encoder within the allocated GPUs.

The client opens with the 1080p preset. Viewport resolution and rendering FPS are separate from stream resolution and video FPS. For slow scenes, try a 1280 x 720 viewport or simpler lighting; see NVIDIA's [performance guide](https://docs.isaacsim.omniverse.nvidia.com/6.2.0/reference_material/sim_performance_optimization_handbook.html).

## Update and check the launcher

After updating your local checkout, run `install` again between sessions. This keeps your image, project and caches. The pinned image and build settings are in [setup.sh](setup.sh); changing the Isaac version is a separate update.

To check a GPU allocation:

```bash
./simproto start --mode probe --cpus 1 --mem 2G --time 00:02:00
./simproto start --mode check --time 00:15:00
```

Read the logs and exit code. `check` verifies a falling cube and rendered camera output; you can check editor input and scene persistence by moving an object, saving to `/project`, reopening the file and reconnecting.

Local checks (use `python3` on Linux/macOS if needed):

```bash
python -m unittest discover -s tests
uvx ruff format --check . simproto
uvx ruff check . simproto
uvx --from mypy==1.15.0 mypy --platform linux simproto scheduler.py runtime.py relay.py desktop.py
```

CI runs the local tests on Linux, macOS and Windows. The type check targets Linux because the source includes the cluster runtime.
