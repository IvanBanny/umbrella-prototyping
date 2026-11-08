#!/usr/bin/env bash
set -euo pipefail
nvidia-smi --query-gpu=name,uuid,driver_version,memory.total --format=csv
getconf GNU_LIBC_VERSION
ls /usr/share/vulkan/icd.d/
ldconfig -p | grep -E 'libnvidia-(encode|rtcore)|libvulkan'
curl --max-time 20 -sSI https://nvcr.io/v2/ | head -5
