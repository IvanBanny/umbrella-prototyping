#!/usr/bin/env bash
# build in a CPU job; all sessions in this account reuse the image
set -euo pipefail
root="${SIMPROTO_ROOT:-$HOME/simproto}"
mkdir -p "$root/images"
exec 9>"$root/images/setup.lock"
flock -n 9 || { echo "Image setup is already running" >&2; exit 1; }
image="$root/images/isaac-lab-3.0.0.sif"
if [[ -s "$image" ]]; then
    apptainer inspect "$image" >/dev/null
    echo "Image already installed: $image"
    exit 0
fi
scratch="${SLURM_TMPDIR:-/scratch-shared/$USER/simproto-build}"
mkdir -p "$scratch"
build_dir="$(mktemp -d "$scratch/build.XXXXXX")"
trap 'rm -rf -- "$build_dir"' EXIT
export APPTAINER_CACHEDIR="$build_dir/cache"
export APPTAINER_TMPDIR="$build_dir/tmp"
mkdir -p "$APPTAINER_CACHEDIR" "$APPTAINER_TMPDIR"
# pin the x86_64 image for Isaac Lab 3.0.0
source_image="docker://nvcr.io/nvidia/isaac-lab@sha256:88f1d5c1b588299897eeddc5c7f6376d11d1bfb8224896ae288ea13bf6e8f1e2"
# mksquashfs otherwise sizes its cache from host RAM and can exceed job memory
compression="-mem 4G -processors ${SLURM_CPUS_PER_TASK:-4} -comp gzip -Xcompression-level 1"
apptainer build --mksquashfs-args "$compression" "$build_dir/isaac.sif" "$source_image"
apptainer inspect "$build_dir/isaac.sif" >/dev/null
mv "$build_dir/isaac.sif" "$image.partial"
mv "$image.partial" "$image"
echo "Installed: $image"
