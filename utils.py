"""
utils.py — Formatting helpers and Telegram inline-keyboard builders.
"""
import os
from typing import Any, Dict, List, Optional

from telethon import Button

from job_queue import (
    BAKE_TYPES,
    DEFAULT_BAKE_SETTINGS,
    DEFAULT_RENDER_SETTINGS,
    Job,
    SessionState,
    UserSession,
)


# ── Text formatting ───────────────────────────────────────────────────────────

def fmt_size(n_bytes: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n_bytes < 1024:
            return f"{n_bytes:.1f} {unit}"
        n_bytes /= 1024
    return f"{n_bytes:.1f} TB"


def fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m {s:02d}s"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


def fmt_progress_bar(percent: float, width: int = 16) -> str:
    filled = int(width * percent / 100)
    bar = "█" * filled + "░" * (width - filled)
    return f"[{bar}] {percent:.1f}%"


def fmt_speed(done_bytes: int, elapsed: float) -> str:
    if elapsed <= 0:
        return "—"
    speed = done_bytes / elapsed
    return f"{fmt_size(int(speed))}/s"


# ── Keyboard builders ─────────────────────────────────────────────────────────

def kb_operation() -> List[List[Button]]:
    """Initial choice: Bake or Render."""
    return [
        [
            Button.inline("🎨  Bake", b"op:bake"),
            Button.inline("🖼  Render", b"op:render"),
        ]
    ]


def kb_settings(
    operation: str,
    settings: Dict[str, Any],
    available_gpu_types: List[str],
) -> List[List[Button]]:
    """
    Build the full settings keyboard for render or bake.
    Currently selected values are shown with a ✓.
    """
    rows: List[List[Button]] = []

    # ── Device row ────────────────────────────────────────────────────────────
    device_options = ["CPU"] + available_gpu_types
    cur_device = settings.get("device", "CPU")
    rows.append([
        Button.inline(
            f"{'✓ ' if d == cur_device else ''}{d}",
            f"cfg:device:{d}".encode(),
        )
        for d in device_options
    ])

    # ── Samples row ───────────────────────────────────────────────────────────
    sample_opts = ["default", "64", "128", "256", "512", "1024", "2048"]
    cur_samples = str(settings.get("samples", "default"))
    rows.append([
        Button.inline(
            f"{'✓ ' if s == cur_samples else ''}{s}",
            f"cfg:samples:{s}".encode(),
        )
        for s in sample_opts
    ])

    # ── Denoise row ───────────────────────────────────────────────────────────
    cur_denoise = settings.get("denoise", True)
    rows.append([
        Button.inline(
            f"{'✓ ' if cur_denoise else ''}🔇 Denoise ON",
            b"cfg:denoise:true",
        ),
        Button.inline(
            f"{'✓ ' if not cur_denoise else ''}Denoise OFF",
            b"cfg:denoise:false",
        ),
    ])

    # ── Tile size row ─────────────────────────────────────────────────────────
    tile_opts = ["default", "64", "256", "512", "1024", "2048"]
    cur_tile = str(settings.get("tile_size", "default"))
    rows.append([
        Button.inline(
            f"{'✓ ' if t == cur_tile else ''}{t}",
            f"cfg:tile:{t}".encode(),
        )
        for t in tile_opts
    ])

    # ── Bake-only rows ────────────────────────────────────────────────────────
    if operation == "bake":
        # Bake type — split into two rows of ~5
        cur_btype = settings.get("bake_type", "COMBINED")
        mid = len(BAKE_TYPES) // 2
        for chunk in [BAKE_TYPES[:mid], BAKE_TYPES[mid:]]:
            rows.append([
                Button.inline(
                    f"{'✓ ' if bt == cur_btype else ''}{bt}",
                    f"cfg:bake_type:{bt}".encode(),
                )
                for bt in chunk
            ])

        # Bake target
        cur_target = settings.get("bake_target", "single")
        rows.append([
            Button.inline(
                f"{'✓ ' if cur_target == 'single' else ''}Single image (all→one)",
                b"cfg:bake_target:single",
            ),
            Button.inline(
                f"{'✓ ' if cur_target == 'per_material' else ''}Per-material",
                b"cfg:bake_target:per_material",
            ),
        ])

    # ── Start button ──────────────────────────────────────────────────────────
    rows.append([Button.inline("▶  Start", b"cfg:start")])
    return rows


def kb_format(operation: str) -> List[List[Button]]:
    """Output format selection keyboard."""
    if operation == "render":
        formats = ["PNG", "JPEG", "EXR", "TIFF", "WEBP"]
    else:
        formats = ["PNG", "EXR", "TIFF"]
    return [
        [Button.inline(f, f"fmt:{f}".encode()) for f in formats]
    ]


def kb_compression(fmt: str) -> List[List[Button]]:
    """
    Compression/quality level keyboard, context-sensitive per format.
    PNG  → 0–9 (lossless levels)
    JPEG/WEBP → quality 10–100
    EXR/TIFF  → no compression needed (just confirm)
    """
    if fmt in ("EXR", "TIFF"):
        return [[Button.inline("✅  Send as-is", b"cmp:0")]]
    if fmt == "PNG":
        levels = [0, 1, 3, 6, 9]
        label = "PNG compression (0=fast, 9=smallest)"
        return [
            [Button.inline(f"{label}", b"_")],   # header-style label
            [Button.inline(str(lv), f"cmp:{lv}".encode()) for lv in levels],
        ]
    # JPEG / WEBP — quality
    qualities = [60, 75, 85, 90, 95, 100]
    label = "Quality (100=best)"
    return [
        [Button.inline(f"{label}", b"_")],
        [Button.inline(str(q), f"cmp:{q}".encode()) for q in qualities],
    ]


def kb_after_job() -> List[List[Button]]:
    """Offered after the result file is sent."""
    return [
        [
            Button.inline("🔄  Another operation", b"after:another"),
            Button.inline("✅  Done with file", b"after:done"),
        ]
    ]


# ── Message composers ─────────────────────────────────────────────────────────

def msg_settings_header(operation: str, settings: Dict[str, Any]) -> str:
    op_label = "🖼  Render" if operation == "render" else "🎨  Bake"
    lines = [
        f"**{op_label} Settings**",
        "",
        f"🖥  **Device:** `{settings.get('device', 'CPU')}`",
        f"🎯  **Samples:** `{settings.get('samples', 'default')}`",
        f"🔇  **Denoise:** `{'Yes' if settings.get('denoise', True) else 'No'}`",
        f"📐  **Tile size:** `{settings.get('tile_size', 'default')}`",
    ]
    if operation == "bake":
        lines += [
            f"🖌  **Bake type:** `{settings.get('bake_type', 'COMBINED')}`",
            f"📦  **Bake target:** `{settings.get('bake_target', 'single')}`",
        ]
    lines += ["", "_Tap a button to change a setting, then press ▶ Start._"]
    return "\n".join(lines)


def msg_render_progress(info: Dict[str, Any]) -> str:
    bar = fmt_progress_bar(info["percent"])
    elapsed = fmt_duration(info["elapsed"])
    remaining = info.get("remaining", "")
    remaining_str = f"  ⏳ Remaining: `{remaining}`" if remaining else ""
    return (
        f"🖼  **Rendering…**\n"
        f"`{bar}`\n"
        f"Sample `{info['sample']}/{info['total_samples']}` "
        f"({info['percent']:.1f}%)\n"
        f"⏱  Elapsed: `{elapsed}`{remaining_str}"
    )


def msg_bake_progress(info: Dict[str, Any]) -> str:
    bar = fmt_progress_bar(info["percent"])
    elapsed = fmt_duration(info["elapsed"])
    return (
        f"🎨  **Baking…**\n"
        f"`{bar}`\n"
        f"Object `{info['current_object']}` "
        f"({info['done']}/{info['total']})\n"
        f"⏱  Elapsed: `{elapsed}`"
    )


def msg_queued(position: int, job_id: str) -> str:
    return (
        f"⏳  **Job queued** (#{job_id[:8]})\n"
        f"Position in queue: **{position}**\n"
        f"_You'll be notified when it starts._"
    )


def msg_job_started(operation: str) -> str:
    op = "render" if operation == "render" else "bake"
    return f"🚀  **Starting {op}…**\n_Progress updates will appear here._"


def msg_download_progress(done: int, total: int, elapsed: float) -> str:
    pct = done / max(total, 1) * 100
    bar = fmt_progress_bar(pct)
    speed = fmt_speed(done, elapsed)
    return (
        f"📥  **Downloading .blend file…**\n"
        f"`{bar}`\n"
        f"{fmt_size(done)} / {fmt_size(total)}  |  {speed}"
    )


def msg_upload_progress(done: int, total: int, elapsed: float) -> str:
    pct = done / max(total, 1) * 100
    bar = fmt_progress_bar(pct)
    speed = fmt_speed(done, elapsed)
    return (
        f"📤  **Uploading result…**\n"
        f"`{bar}`\n"
        f"{fmt_size(done)} / {fmt_size(total)}  |  {speed}"
    )


def msg_info(
    queue_jobs: list,
    current_job: Optional[Job],
    gpu_types: List[str],
    sessions: Dict[int, UserSession],
) -> str:
    gpu_str = ", ".join(gpu_types) if gpu_types else "None (CPU only)"
    lines = [
        "ℹ️  **Bot Status**",
        "",
        f"**GPU devices detected:** `{gpu_str}`",
        f"**Jobs in queue:** `{len(queue_jobs)}`",
        f"**Active sessions:** `{len(sessions)}`",
        "",
    ]
    if current_job:
        lines += [
            "**▶ Currently running:**",
            f"  • Job `{current_job.job_id[:8]}` — "
            f"`{current_job.operation}` — status: `{current_job.status}`",
            "",
        ]
    if queue_jobs:
        lines.append("**⏳ Queue:**")
        for i, job in enumerate(queue_jobs, 1):
            lines.append(
                f"  {i}. `{job.job_id[:8]}` — `{job.operation}`"
            )
    return "\n".join(lines)
