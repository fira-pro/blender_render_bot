"""
config.py — Load and expose all configuration from .env
"""
import os
from dotenv import load_dotenv

load_dotenv()

# ── Telegram ──────────────────────────────────────────────────────────────────
API_ID: int = int(os.environ["TELEGRAM_API_ID"])
API_HASH: str = os.environ["TELEGRAM_API_HASH"]
BOT_TOKEN: str = os.environ["TELEGRAM_BOT_TOKEN"]

WHITELIST_USER_IDS: set[int] = {
    int(uid.strip())
    for uid in os.getenv("WHITELIST_USER_IDS", "").split(",")
    if uid.strip()
}

# ── Blender ───────────────────────────────────────────────────────────────────
BLENDER_PATH: str = os.getenv("BLENDER_PATH", "blender")

# ── Storage ───────────────────────────────────────────────────────────────────
WORKSPACE_DIR: str = os.getenv("WORKSPACE_DIR", "./workspace")
SESSION_TTL_HOURS: int = int(os.getenv("SESSION_TTL_HOURS", "48"))
MAX_QUEUE_SIZE: int = int(os.getenv("MAX_QUEUE_SIZE", "10"))

# ── Progress update throttle (seconds between Telegram edits) ─────────────────
PROGRESS_UPDATE_INTERVAL: float = 5.0

# ── Blender script paths (relative to this file) ─────────────────────────────
import pathlib
_HERE = pathlib.Path(__file__).parent
RENDER_SCRIPT_PATH: str = str(_HERE / "blender_scripts" / "render_script.py")
BAKE_SCRIPT_PATH: str = str(_HERE / "blender_scripts" / "bake_script.py")
DETECT_DEVICES_SCRIPT_PATH: str = str(_HERE / "blender_scripts" / "detect_devices.py")
