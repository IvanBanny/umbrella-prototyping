# NVENC GPU filter

Umbrella's driver 610.57.04 returned `NV_ENC_ERR_UNSUPPORTED_DEVICE` on
`mcs-gpub001` even though CUDA worked. The encoder was trying to open GPUs
assigned to other Slurm jobs. This also happened outside Apptainer.

The filter intercepts the driver's GPU list and keeps devices the process can
open. The runtime builds it on the allocated node and preloads it for GUI
sessions. Slurm's device permissions still apply. Headless jobs don't load it.

The source comes from
[webgl2video](https://github.com/Avi130805/webgl2video/tree/d8ec4fc668264ae5208f7e84b94743c49695a30a/nvenc-gpu-filter)
at commit `d8ec4fc668264ae5208f7e84b94743c49695a30a`, under the [MIT license](LICENSE).
We changed the device check from `access(F_OK)` to `open(O_RDWR | O_CLOEXEC)`.
On Umbrella, every GPU device file exists, including devices the job can't open.
Checking for existence alone therefore keeps GPUs outside the allocation.
Comments have also been edited locally.

This works around [NVIDIA's GPU enumeration issue](https://github.com/NVIDIA/nvidia-container-toolkit/issues/1249).
Check whether it's still needed after a driver update.

Run the CPU tests from this directory:

```bash
cc -DNVENC_FILTER_TEST -o /tmp/simproto-nvenc-test nvenc-gpu-filter.c -lpthread
/tmp/simproto-nvenc-test
rm /tmp/simproto-nvenc-test
```
