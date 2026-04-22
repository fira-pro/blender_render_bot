"""
detect_devices.py — Run inside Blender headless to enumerate available
GPU compute device types. Output lines of the form:
    DEVICE_AVAILABLE:CUDA
    DEVICE_AVAILABLE:HIP
    ...

Called by blender_worker.py at bot startup.
"""
import sys

import bpy

DEVICE_TYPES = ["CUDA", "OPTIX", "HIP", "METAL", "ONEAPI"]

prefs = bpy.context.preferences.addons.get("cycles")
if prefs is None:
    print("cycles addon not found", file=sys.stderr)
    sys.exit(0)

cycles_prefs = bpy.context.preferences.addons["cycles"].preferences

for dtype in DEVICE_TYPES:
    try:
        cycles_prefs.compute_device_type = dtype
        cycles_prefs.refresh_devices()
        gpu_devs = [d for d in cycles_prefs.devices if d.type != "CPU"]
        if gpu_devs:
            print(f"DEVICE_AVAILABLE:{dtype}", flush=True)
    except Exception:
        pass
