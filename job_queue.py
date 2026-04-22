"""
job_queue.py — FIFO job queue and per-user session state management.
"""
import asyncio
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class SessionState(str, Enum):
    IDLE = "idle"
    AWAITING_OPERATION = "awaiting_operation"
    CONFIGURING = "configuring"
    QUEUED = "queued"
    RUNNING = "running"
    AWAITING_FORMAT = "awaiting_format"
    AWAITING_COMPRESSION = "awaiting_compression"
    COMPLETED = "completed"


# Default settings that will be presented to the user.
# "default" means "use whatever is in the .blend file".
DEFAULT_RENDER_SETTINGS: Dict[str, Any] = {
    "device": "CPU",
    "samples": "default",
    "denoise": True,
    "tile_size": "default",
}

DEFAULT_BAKE_SETTINGS: Dict[str, Any] = {
    "device": "CPU",
    "samples": "default",
    "denoise": True,
    "tile_size": "default",
    "bake_type": "COMBINED",
    "bake_target": "single",
}

BAKE_TYPES: List[str] = [
    "COMBINED", "DIFFUSE", "GLOSSY", "TRANSMISSION",
    "ROUGHNESS", "NORMAL", "AO", "SHADOW", "EMIT",
    "ENVIRONMENT", "UV",
]


@dataclass
class UserSession:
    user_id: int
    chat_id: int
    blend_path: str                          # Absolute path on disk
    operation: Optional[str] = None          # "render" | "bake"
    settings: Dict[str, Any] = field(default_factory=dict)
    state: SessionState = SessionState.AWAITING_OPERATION
    job_id: Optional[str] = None
    # Telegram message IDs for in-place edits
    settings_msg_id: Optional[int] = None
    progress_msg_id: Optional[int] = None
    # Rate-limit progress edits
    last_progress_update: float = 0.0
    # Paths to files produced by the job
    output_files: List[str] = field(default_factory=list)
    # Chosen output format after job finishes
    output_format: Optional[str] = None
    output_compression: Optional[int] = None
    # Housekeeping
    created_at: float = field(default_factory=time.time)


@dataclass
class Job:
    job_id: str
    user_id: int
    chat_id: int
    blend_path: str
    operation: str          # "render" | "bake"
    settings: Dict[str, Any] = field(default_factory=dict)
    workspace_dir: str = ""
    status: str = "queued"  # queued | running | done | failed | cancelled
    error_message: str = ""


class JobQueue:
    """Async FIFO queue with a single background worker."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[Job] = asyncio.Queue()
        self._sessions: Dict[int, UserSession] = {}   # user_id → session
        self._current_job: Optional[Job] = None
        self._current_process: Optional[asyncio.subprocess.Process] = None
        self._worker_task: Optional[asyncio.Task] = None
        self._process_callback = None   # set by bot.py

    # ── Session management ────────────────────────────────────────────────────

    def get_session(self, user_id: int) -> Optional[UserSession]:
        return self._sessions.get(user_id)

    def create_session(
        self, user_id: int, chat_id: int, blend_path: str
    ) -> UserSession:
        session = UserSession(user_id=user_id, chat_id=chat_id, blend_path=blend_path)
        self._sessions[user_id] = session
        return session

    def delete_session(self, user_id: int) -> None:
        self._sessions.pop(user_id, None)

    def all_sessions(self) -> Dict[int, UserSession]:
        return dict(self._sessions)

    # ── Queue management ──────────────────────────────────────────────────────

    def queue_size(self) -> int:
        return self._queue.qsize()

    def queue_position(self, job_id: str) -> int:
        """Return 1-based position of job_id in the pending queue (0 = not found)."""
        for i, job in enumerate(list(self._queue._queue)):  # type: ignore[attr-defined]
            if job.job_id == job_id:
                return i + 1
        return 0

    def current_job(self) -> Optional[Job]:
        return self._current_job

    def set_process(self, proc: Optional[asyncio.subprocess.Process]) -> None:
        self._current_process = proc

    async def enqueue(self, job: Job) -> None:
        await self._queue.put(job)

    async def cancel_current(self) -> bool:
        """Terminate the currently running Blender process."""
        if self._current_process and self._current_process.returncode is None:
            try:
                self._current_process.terminate()
                await asyncio.sleep(3)
                if self._current_process.returncode is None:
                    self._current_process.kill()
            except ProcessLookupError:
                pass
            if self._current_job:
                self._current_job.status = "cancelled"
            return True
        return False

    # ── Worker lifecycle ──────────────────────────────────────────────────────

    def start_worker(self, process_callback) -> None:
        """
        Start the background worker coroutine.
        process_callback(job) is an async function that actually runs Blender
        and sends Telegram updates.
        """
        self._process_callback = process_callback
        self._worker_task = asyncio.create_task(self._worker_loop())

    async def _worker_loop(self) -> None:
        while True:
            job: Job = await self._queue.get()
            self._current_job = job
            job.status = "running"
            try:
                await self._process_callback(job)
            except asyncio.CancelledError:
                job.status = "cancelled"
            except Exception as exc:
                job.status = "failed"
                job.error_message = str(exc)
            finally:
                self._current_job = None
                self._current_process = None
                self._queue.task_done()

    # ── TTL cleanup ───────────────────────────────────────────────────────────

    def expired_sessions(self, ttl_seconds: float) -> List[int]:
        """Return user_ids of sessions older than ttl_seconds."""
        now = time.time()
        return [
            uid
            for uid, sess in self._sessions.items()
            if (now - sess.created_at) > ttl_seconds
        ]


def make_job_id() -> str:
    return uuid.uuid4().hex[:12]
