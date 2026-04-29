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

    # ── Samples rows (split in two so the keyboard stays readable) ───────────
    sample_opts_low  = ["default", "1", "4", "8", "16", "32"]
    sample_opts_high = ["64", "128", "256", "512", "1024", "2048"]
    cur_samples = str(settings.get("samples", "default"))
    for row_opts in (sample_opts_low, sample_opts_high):
        rows.append([
            Button.inline(
                f"{'✓ ' if s == cur_samples else ''}{s}",
                f"cfg:samples:{s}".encode(),
            )
            for s in row_opts
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
    tile_opts = ["default", "64", "256", "512", "1024", "2048", "4096"]
    cur_tile = str(settings.get("tile_size", "default"))
    rows.append([
        Button.inline(
            f"{'\u2713 ' if t == cur_tile else ''}{t}",
            f"cfg:tile:{t}".encode(),
        )
        for t in tile_opts
    ])

    # ── Color depth row (applies to the initial OpenEXR save) ───────────────────────
    depth_opts = [("16", "16-bit half"), ("32", "32-bit float")]
    cur_depth  = str(settings.get("color_depth", "32"))
    rows.append([
        Button.inline(
            f"{'\u2713 ' if d == cur_depth else ''}{label}",
            f"cfg:color_depth:{d}".encode(),
        )
        for d, label in depth_opts
    ])

    # ── EXR codec row ──────────────────────────────────────────────────────────────
    codec_opts = [("PIZ", "PIZ (wavelet)"), ("ZIP", "ZIP (deflate)"), ("ZIPS", "ZIPS"), ("RLE", "RLE")]
    cur_codec  = str(settings.get("exr_codec", "PIZ"))
    rows.append([
        Button.inline(
            f"{'\u2713 ' if c == cur_codec else ''}{label}",
            f"cfg:exr_codec:{c}".encode(),
        )
        for c, label in codec_opts
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

        # Clear before bake
        cur_clear = settings.get("use_clear", False)
        rows.append([
            Button.inline(
                f"{'✓ ' if cur_clear else ''}🗑 Clear image first",
                b"cfg:use_clear:true",
            ),
            Button.inline(
                f"{'✓ ' if not cur_clear else ''}Keep background",
                b"cfg:use_clear:false",
            ),
        ])

        # Margin (bleed)
        margin_opts = ["0", "4", "8", "16", "32", "64"]
        cur_margin = str(settings.get("margin", 16))
        rows.append([
            Button.inline(
                f"{'✓ ' if m == cur_margin else ''}px{m}",
                f"cfg:margin:{m}".encode(),
            )
            for m in margin_opts
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
        [Button.inline(f, f"fmt:{f}".encode()) for f in formats],
        [Button.inline("\U0001f4e6  Raw EXR (no conversion)", b"fmt:RAW_EXR")],
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
        [Button.inline("\U0001f4e5  Different format", b"after:reformat")],
        [
            Button.inline("\U0001f504  Another operation", b"after:another"),
            Button.inline("\u2705  Done with file", b"after:done"),
        ],
    ]


# ── Message composers ─────────────────────────────────────────────────────────

def msg_settings_header(operation: str, settings: Dict[str, Any]) -> str:
    op_label = "\U0001f5bc  Render" if operation == "render" else "\U0001f3a8  Bake"
    lines = [
        f"**{op_label} Settings**",
        "",
        f"\U0001f5a5  **Device:** `{settings.get('device', 'CPU')}`",
        f"\U0001f3af  **Samples:** `{settings.get('samples', 'default')}`",
        f"\U0001f507  **Denoise:** `{'Yes' if settings.get('denoise', True) else 'No'}`",
        f"\U0001f4d0  **Tile size:** `{settings.get('tile_size', 'default')}`",
        f"\U0001f3a8  **Color depth:** `{settings.get('color_depth', '32')}-bit`",
        f"\U0001f4e6  **EXR codec:** `{settings.get('exr_codec', 'PIZ')}`",
    ]
    if operation == "bake":
        lines += [
            f"\U0001f58c  **Bake type:** `{settings.get('bake_type', 'COMBINED')}`",
            f"\U0001f4e6  **Bake target:** `{settings.get('bake_target', 'single')}`",
            f"\U0001f5d1  **Clear first:** `{'Yes' if settings.get('use_clear', False) else 'No'}`",
            f"\U0001f4cf  **Margin:** `{settings.get('margin', 16)} px`",
        ]
    lines += ["", "_Tap a button to change a setting, then press \u25b6 Start._"]
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
