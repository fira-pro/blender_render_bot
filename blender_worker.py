"""
blender_worker.py — Launch Blender as a subprocess, stream stdout for
progress, and report back via an async callback.
"""
import asyncio
import logging
import os
import re
import shutil
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

from config import BLENDER_PATH, DETECT_DEVICES_SCRIPT_PATH

log = logging.getLogger(__name__)

# ── Regex patterns for Blender stdout ────────────────────────────────────────
# Render progress line:
#   Fra:1 Mem:123M ... | Scene, Layer | Sample 64/512
_RE_RENDER_SAMPLE = re.compile(
    r"Fra:(\d+).*?Sample\s+(\d+)/(\d+)", re.IGNORECASE
)
# Remaining time:
#   | Remaining:00:02.34 |
_RE_REMAINING = re.compile(r"Remaining:(\d+:\d+(?:\.\d+)?)", re.IGNORECASE)
# Elapsed time:
#   | Time:00:01.23 |
_RE_ELAPSED = re.compile(r"\|\s*Time:(\d+:\d+(?:\.\d+)?)", re.IGNORECASE)
# Bake progress marker emitted by our bake_script.py:
#   BAKE_PROGRESS:2/8:ObjectName
_RE_BAKE_PROGRESS = re.compile(r"BAKE_PROGRESS:(\d+)/(\d+):(.+)")
# Completion markers
_RE_RENDER_COMPLETE = re.compile(r"RENDER_COMPLETE:(.+)")
_RE_BAKE_COMPLETE = re.compile(r"BAKE_COMPLETE:(.+)")
_RE_RENDER_FAILED = re.compile(r"RENDER_FAILED:(.+)")
_RE_BAKE_FAILED = re.compile(r"BAKE_FAILED:(.+)")
# Device detection marker:
#   DEVICE_AVAILABLE:CUDA
_RE_DEVICE = re.compile(r"DEVICE_AVAILABLE:(\w+)")


# ── Device detection ──────────────────────────────────────────────────────────

async def detect_blender_devices() -> List[str]:
    """
    Run a tiny Blender script to enumerate available GPU compute device types.
    Returns a list like ['CUDA'], ['HIP'], ['METAL'], or [] if GPU-only CPU.
    """
    cmd = [BLENDER_PATH, "--background", "--python", DETECT_DEVICES_SCRIPT_PATH]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
        devices: List[str] = []
        for line in stdout.decode(errors="replace").splitlines():
            m = _RE_DEVICE.search(line)
            if m:
                dt = m.group(1)
                if dt not in devices:
                    devices.append(dt)
        return devices
    except Exception as exc:
        log.warning(f"Device detection failed: {exc}")
        return []


# ── Job runner ────────────────────────────────────────────────────────────────

async def run_blender_job(
    job_id: str,
    blend_path: str,
    operation: str,
    settings: Dict,
    workspace_dir: str,
    script_path: str,
    progress_cb: Callable,            # async (info: dict) -> None
    set_process_cb: Callable,         # sync  (proc) -> None
) -> Dict:
    """
    Launch Blender headless, stream stdout, call progress_cb with status dicts,
    and return a result dict with keys: success, output_files, error.
    """
    output_dir = os.path.join(workspace_dir, "output")
    os.makedirs(output_dir, exist_ok=True)

    # Build argument list passed after '--' to the Blender Python script
    extra_args = _build_script_args(operation, settings, output_dir)

    cmd = [
        BLENDER_PATH,
        "--background",
        blend_path,
        "--python", script_path,
        "--",
        *extra_args,
    ]

    log.info(f"[{job_id}] Launching: {' '.join(cmd)}")

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,   # merge stderr into stdout
        cwd=workspace_dir,
    )
    set_process_cb(proc)

    result = {"success": False, "output_files": [], "error": ""}
    start_time = time.time()
    stderr_tail: List[str] = []   # keep last 30 lines for error reporting

    async for raw_line in proc.stdout:
        line = raw_line.decode(errors="replace").rstrip()
        if line:
            stderr_tail.append(line)
            if len(stderr_tail) > 30:
                stderr_tail.pop(0)

        info = _parse_line(line, operation, start_time)
        if info:
            await progress_cb(info)

        # Check for completion/failure markers
        m = _RE_RENDER_COMPLETE.search(line)
        if m:
            result["success"] = True
            result["output_files"] = _collect_outputs(m.group(1).strip(), output_dir)
            break

        m = _RE_BAKE_COMPLETE.search(line)
        if m:
            result["success"] = True
            result["output_files"] = _collect_outputs(m.group(1).strip(), output_dir)
            break

        m = _RE_RENDER_FAILED.search(line)
        if m:
            result["error"] = m.group(1).strip()
            break

        m = _RE_BAKE_FAILED.search(line)
        if m:
            result["error"] = m.group(1).strip()
            break

    await proc.wait()
    set_process_cb(None)

    if proc.returncode != 0 and not result["success"]:
        if not result["error"]:
            result["error"] = (
                f"Blender exited with code {proc.returncode}.\n"
                + "\n".join(stderr_tail[-10:])
            )

    # If we got a success marker but no output listed, scan the output dir
    if result["success"] and not result["output_files"]:
        result["output_files"] = _scan_output_dir(output_dir)

    return result


# ── Helpers ───────────────────────────────────────────────────────────────────

def _build_script_args(operation: str, settings: Dict, output_dir: str) -> List[str]:
    device = settings.get("device", "CPU")
    # Derive device_type (CUDA/HIP/METAL/ONEAPI) vs plain CPU
    if device == "CPU":
        device_type = "CPU"
        use_gpu = "false"
    else:
        device_type = device          # e.g. "CUDA", "HIP"
        use_gpu = "true"

    samples = str(settings.get("samples", "default"))
    denoise = "true" if settings.get("denoise", True) else "false"
    tile_size = str(settings.get("tile_size", "default"))

    args = [
        "--device-type", device_type,
        "--use-gpu", use_gpu,
        "--samples", samples,
        "--denoise", denoise,
        "--tile-size", tile_size,
        "--output-dir", output_dir,
    ]

    if operation == "bake":
        args += [
            "--bake-type", settings.get("bake_type", "COMBINED"),
            "--bake-target", settings.get("bake_target", "single"),
        ]

    return args


def _parse_line(line: str, operation: str, start_time: float) -> Optional[Dict]:
    """Parse a stdout line and return a progress dict or None."""
    elapsed = time.time() - start_time

    if operation == "render":
        m = _RE_RENDER_SAMPLE.search(line)
        if m:
            frame = int(m.group(1))
            current_sample = int(m.group(2))
            total_samples = int(m.group(3))
            remaining = ""
            rm = _RE_REMAINING.search(line)
            if rm:
                remaining = rm.group(1)
            return {
                "type": "render_progress",
                "frame": frame,
                "sample": current_sample,
                "total_samples": total_samples,
                "percent": round(current_sample / max(total_samples, 1) * 100, 1),
                "elapsed": elapsed,
                "remaining": remaining,
                "raw_line": line,
            }
    elif operation == "bake":
        m = _RE_BAKE_PROGRESS.search(line)
        if m:
            done = int(m.group(1))
            total = int(m.group(2))
            obj_name = m.group(3).strip()
            return {
                "type": "bake_progress",
                "done": done,
                "total": total,
                "percent": round(done / max(total, 1) * 100, 1),
                "current_object": obj_name,
                "elapsed": elapsed,
            }

    return None


def _collect_outputs(paths_str: str, output_dir: str) -> List[str]:
    """Parse a colon-separated list of output paths from the Blender script."""
    results = []
    for p in paths_str.split("|"):
        p = p.strip()
        if p and os.path.isfile(p):
            results.append(p)
    if not results:
        results = _scan_output_dir(output_dir)
    return results


def _scan_output_dir(output_dir: str) -> List[str]:
    """Collect all non-hidden files from the output directory."""
    files = []
    if os.path.isdir(output_dir):
        for f in sorted(Path(output_dir).iterdir()):
            if f.is_file() and not f.name.startswith("."):
                files.append(str(f))
    return files


def cleanup_workspace(workspace_dir: str) -> None:
    """Delete the entire workspace directory."""
    try:
        if os.path.isdir(workspace_dir):
            shutil.rmtree(workspace_dir)
            log.info(f"Cleaned up workspace: {workspace_dir}")
    except Exception as exc:
        log.warning(f"Failed to clean workspace {workspace_dir}: {exc}")
