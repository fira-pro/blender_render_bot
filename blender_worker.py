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
_RE_BAKE_PROGRESS   = re.compile(r"BAKE_PROGRESS:(\d+)/(\d+):(.+)")
# Per-image / per-frame completion (uploaded immediately)
_RE_IMAGE_COMPLETE  = re.compile(r"IMAGE_COMPLETE:([^:]+):([^:]+):(.*)")
_RE_FRAME_COMPLETE  = re.compile(r"FRAME_COMPLETE:(\d+):([^:]+):(.*)")
# Checkpoint state (JSON) — worker throttles uploads to Telegram
_RE_CHECKPOINT      = re.compile(r"CHECKPOINT_STATE:(.+)")
# Overall completion markers
_RE_RENDER_COMPLETE = re.compile(r"RENDER_COMPLETE:(.+)")
_RE_BAKE_COMPLETE   = re.compile(r"BAKE_COMPLETE:(.+)")
_RE_RENDER_FAILED   = re.compile(r"RENDER_FAILED:(.+)")
_RE_BAKE_FAILED     = re.compile(r"BAKE_FAILED:(.+)")
# Device detection
_RE_DEVICE          = re.compile(r"DEVICE_AVAILABLE:(\w+)")

# How often (seconds) to upload a checkpoint even if the job is still running
CHECKPOINT_INTERVAL = 300   # 5 minutes


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
    progress_cb: Callable,        # async (info: dict) -> None
    set_process_cb: Callable,     # sync  (proc) -> None
    image_complete_cb: Optional[Callable] = None,  # async (img_name, exr, preview) -> None
    frame_complete_cb: Optional[Callable]  = None, # async (frame_num, exr, preview) -> None
    checkpoint_cb: Optional[Callable]      = None, # async (state_json: str) -> None
) -> Dict:
    """
    Launch Blender headless, stream stdout, call progress_cb with status dicts,
    and return a result dict with keys: success, output_files, error.
    """
    # Resolve all paths to absolute BEFORE passing to the subprocess.
    # Using relative paths with cwd set would cause Blender to double-resolve them.
    blend_path_abs  = os.path.abspath(blend_path)
    workspace_abs   = os.path.abspath(workspace_dir)
    script_path_abs = os.path.abspath(script_path)
    output_dir      = os.path.join(workspace_abs, "output")
    os.makedirs(output_dir, exist_ok=True)

    # Build argument list passed after '--' to the Blender Python script
    extra_args = _build_script_args(operation, settings, output_dir)

    cmd = [
        BLENDER_PATH,
        "--background",
        blend_path_abs,
        "--python", script_path_abs,
        "--",
        *extra_args,
    ]

    log.info(f"[{job_id}] Launching: {' '.join(cmd)}")

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,   # merge stderr into stdout
        # No cwd — all paths are absolute so Blender resolves them correctly
    )
    set_process_cb(proc)

    result     = {"success": False, "output_files": [], "error": ""}
    start_time = time.time()
    last_checkpoint_upload = 0.0   # epoch timestamp of last checkpoint upload
    stderr_tail: List[str] = []

    async for raw_line in proc.stdout:
        line = raw_line.decode(errors="replace").rstrip()
        if line:
            stderr_tail.append(line)
            if len(stderr_tail) > 30:
                stderr_tail.pop(0)

        info = _parse_line(line, operation, start_time)
        if info:
            await progress_cb(info)

        # ── Per-image upload (bake) ──────────────────────────────────────────
        m = _RE_IMAGE_COMPLETE.search(line)
        if m and image_complete_cb:
            # create_task so large EXR uploads never block stdout reading
            asyncio.create_task(
                image_complete_cb(m.group(1).strip(), m.group(2).strip(), m.group(3).strip())
            )

        # ── Per-frame upload (render) ────────────────────────────────────────
        m = _RE_FRAME_COMPLETE.search(line)
        if m and frame_complete_cb:
            asyncio.create_task(
                frame_complete_cb(int(m.group(1)), m.group(2).strip(), m.group(3).strip())
            )

        # ── Checkpoint (throttled) ───────────────────────────────────────────
        m = _RE_CHECKPOINT.search(line)
        if m and checkpoint_cb:
            now = time.time()
            if now - last_checkpoint_upload >= CHECKPOINT_INTERVAL:
                last_checkpoint_upload = now
                asyncio.create_task(checkpoint_cb(m.group(1).strip()))

        # ── Terminal markers ─────────────────────────────────────────────────
        m = _RE_RENDER_COMPLETE.search(line)
        if m:
            result["success"]      = True
            result["output_files"] = _collect_outputs(m.group(1).strip(), output_dir)
            break

        m = _RE_BAKE_COMPLETE.search(line)
        if m:
            result["success"]      = True
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
    if device == "CPU":
        device_type = "CPU"
        use_gpu     = "false"
    else:
        device_type = device
        use_gpu     = "true"

    samples   = str(settings.get("samples",    "default"))
    denoise   = "true" if settings.get("denoise", True) else "false"
    tile_size = str(settings.get("tile_size",   "default"))
    depth     = str(settings.get("color_depth", "32"))
    codec     = str(settings.get("exr_codec",   "PIZ"))

    args = [
        "--device-type", device_type,
        "--use-gpu",     use_gpu,
        "--samples",     samples,
        "--denoise",     denoise,
        "--tile-size",   tile_size,
        "--color-depth", depth,
        "--exr-codec",   codec,
        "--output-dir",  output_dir,
    ]

    if operation == "bake":
        use_clear = "true" if settings.get("use_clear", False) else "false"
        margin    = str(settings.get("margin", 16))
        args += [
            "--bake-type",   settings.get("bake_type",   "COMBINED"),
            "--bake-target", settings.get("bake_target", "single"),
            "--use-clear",   use_clear,
            "--margin",      margin,
        ]
        # Resume args (populated when job is created from a checkpoint)
        resume = settings.get("_resume", {})
        if resume.get("completed_images"):
            args += ["--completed-images",   ",".join(resume["completed_images"])]
        if resume.get("current_image"):
            args += ["--resume-image-name",  resume["current_image"]]
        if resume.get("partial_exr"):
            args += ["--resume-image-path",  resume["partial_exr"]]
        if resume.get("done_objects"):
            args += ["--resume-done-objects", ",".join(resume["done_objects"])]

    elif operation == "render":
        # Frame range
        if settings.get("frame_start"):
            args += ["--frame-start", str(settings["frame_start"])]
        if settings.get("frame_end"):
            args += ["--frame-end",   str(settings["frame_end"])]
        if settings.get("frame_step"):
            args += ["--frame-step",  str(settings["frame_step"])]
        # Resume args
        resume = settings.get("_resume", {})
        if resume.get("completed_frames"):
            args += ["--completed-frames", ",".join(str(f) for f in resume["completed_frames"])]

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
