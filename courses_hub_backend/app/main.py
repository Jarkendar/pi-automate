"""
Courses Hub API - pi-automate module.

Single source of truth for course progress, integrated with the pi-automate
n8n stack. Exposes a stable public contract under /api/v1.

Endpoints:
  GET   /api/health                                        - liveness
  GET   /api/v1/courses                                    - list with progress %
  GET   /api/v1/courses/{course_id}                        - full state
  PUT   /api/v1/courses/{course_id}                        - upsert full state
  PATCH /api/v1/courses/{course_id}/lessons/{lesson_id}    - partial lesson update
  DELETE /api/v1/courses/{course_id}                       - reset progress

n8n integration:
  If env N8N_WEBHOOK_URL is set, the backend POSTs a small JSON event whenever
  a course reaches 100% completion (transition only - not re-fired on already-
  completed courses). Use this in n8n to trigger Telegram/email notifications.

Environment:
  DB_PATH              SQLite file path (default /data/courses.db)
  LOG_LEVEL            INFO / DEBUG / WARNING (default INFO)
  CORS_ORIGINS         comma-separated list (default "*")
  N8N_WEBHOOK_URL      optional; receives POST on course completion
  N8N_WEBHOOK_TIMEOUT  seconds, default 3
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import urllib.error
import urllib.request
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DB_PATH = Path(os.getenv("DB_PATH", "/data/courses.db"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
CORS_ORIGINS = [
    o.strip() for o in os.getenv("CORS_ORIGINS", "*").split(",") if o.strip()
]
N8N_WEBHOOK_URL = os.getenv("N8N_WEBHOOK_URL", "").strip() or None
N8N_WEBHOOK_TIMEOUT = float(os.getenv("N8N_WEBHOOK_TIMEOUT", "3"))

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("courses-hub")


# ---------------------------------------------------------------------------
# Status normalization - accept legacy values from existing courses
# ---------------------------------------------------------------------------

_STATUS_MAP = {
    "": "not_started",
    "not-started": "not_started",
    "not_started": "not_started",
    "none": "not_started",
    "in-progress": "in_progress",
    "in_progress": "in_progress",
    "started": "in_progress",
    "doing": "in_progress",
    "completed": "completed",
    "done": "completed",
    "finished": "completed",
}


def normalize_status(raw: Any) -> str:
    if raw is None:
        return "not_started"
    return _STATUS_MAP.get(str(raw).strip().lower(), "not_started")


# ---------------------------------------------------------------------------
# Database - SQLite with WAL mode
# ---------------------------------------------------------------------------


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=3000;")
    return conn


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    log.info("Initializing SQLite at %s", DB_PATH)
    with _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS courses (
                course_id     TEXT PRIMARY KEY,
                title         TEXT NOT NULL DEFAULT '',
                total_lessons INTEGER NOT NULL DEFAULT 0,
                meta_json     TEXT NOT NULL DEFAULT '{}',
                created_at    TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS lesson_progress (
                course_id   TEXT NOT NULL,
                lesson_id   TEXT NOT NULL,
                status      TEXT NOT NULL DEFAULT 'not_started',
                notes       TEXT NOT NULL DEFAULT '',
                updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (course_id, lesson_id),
                FOREIGN KEY (course_id) REFERENCES courses(course_id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_progress_course
                ON lesson_progress(course_id);
            CREATE INDEX IF NOT EXISTS idx_progress_status
                ON lesson_progress(course_id, status);
            """
        )


# ---------------------------------------------------------------------------
# Schemas (public contract)
# ---------------------------------------------------------------------------


class LessonProgress(BaseModel):
    status: str = Field(default="not_started", description="not_started | in_progress | completed")
    notes: str = Field(default="", description="Free-text notes for the lesson")
    updated_at: str | None = None

    @field_validator("status", mode="before")
    @classmethod
    def _normalize(cls, v: Any) -> str:
        return normalize_status(v)


class CourseState(BaseModel):
    course_id: str
    title: str = ""
    total_lessons: int = 0
    lessons: dict[str, LessonProgress] = Field(default_factory=dict)
    meta: dict[str, Any] = Field(default_factory=dict)
    updated_at: str | None = None


class CourseSummary(BaseModel):
    course_id: str
    title: str
    total_lessons: int
    completed: int
    in_progress: int
    not_started: int
    progress_percent: float
    notes_count: int
    updated_at: str | None = None


class LessonPatch(BaseModel):
    status: str | None = None
    notes: str | None = None

    @field_validator("status", mode="before")
    @classmethod
    def _normalize(cls, v: Any) -> Any:
        return normalize_status(v) if v is not None else None


# ---------------------------------------------------------------------------
# n8n webhook (fire-and-forget, best-effort)
# ---------------------------------------------------------------------------


def _notify_n8n(event: str, payload: dict[str, Any]) -> None:
    """POST event to n8n. Failures are logged but never raised."""
    if not N8N_WEBHOOK_URL:
        return
    body = json.dumps({"event": event, "data": payload, "ts": _now()}).encode("utf-8")
    req = urllib.request.Request(
        N8N_WEBHOOK_URL,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=N8N_WEBHOOK_TIMEOUT) as resp:
            log.info("n8n webhook %s -> HTTP %s", event, resp.status)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        log.warning("n8n webhook %s failed: %s", event, e)


# ---------------------------------------------------------------------------
# Repository helpers
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _ensure_course(conn: sqlite3.Connection, course_id: str, title: str = "", total: int = 0) -> None:
    conn.execute(
        """
        INSERT INTO courses (course_id, title, total_lessons)
        VALUES (?, ?, ?)
        ON CONFLICT(course_id) DO UPDATE SET
            title         = CASE WHEN excluded.title <> '' THEN excluded.title ELSE courses.title END,
            total_lessons = CASE WHEN excluded.total_lessons > 0 THEN excluded.total_lessons ELSE courses.total_lessons END,
            updated_at    = datetime('now')
        """,
        (course_id, title, total),
    )


def _load_course_state(conn: sqlite3.Connection, course_id: str) -> CourseState | None:
    row = conn.execute(
        "SELECT course_id, title, total_lessons, meta_json, updated_at FROM courses WHERE course_id = ?",
        (course_id,),
    ).fetchone()
    if not row:
        return None

    lessons_rows = conn.execute(
        "SELECT lesson_id, status, notes, updated_at FROM lesson_progress WHERE course_id = ?",
        (course_id,),
    ).fetchall()

    meta = json.loads(row["meta_json"] or "{}")
    lessons = {
        r["lesson_id"]: LessonProgress(
            status=r["status"], notes=r["notes"], updated_at=r["updated_at"]
        )
        for r in lessons_rows
    }
    return CourseState(
        course_id=row["course_id"],
        title=row["title"],
        total_lessons=row["total_lessons"],
        lessons=lessons,
        meta=meta,
        updated_at=row["updated_at"],
    )


def _summarize_course(conn: sqlite3.Connection, course_id: str) -> CourseSummary:
    course_row = conn.execute(
        "SELECT title, total_lessons, updated_at FROM courses WHERE course_id = ?",
        (course_id,),
    ).fetchone()
    if not course_row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Course '{course_id}' not found")

    agg = conn.execute(
        """
        SELECT
            SUM(CASE WHEN status='completed'   THEN 1 ELSE 0 END) AS completed,
            SUM(CASE WHEN status='in_progress' THEN 1 ELSE 0 END) AS in_progress,
            SUM(CASE WHEN status='not_started' THEN 1 ELSE 0 END) AS not_started,
            SUM(CASE WHEN length(notes) > 0    THEN 1 ELSE 0 END) AS notes_count,
            COUNT(*) AS tracked
        FROM lesson_progress
        WHERE course_id = ?
        """,
        (course_id,),
    ).fetchone()

    total = max(course_row["total_lessons"] or 0, (agg["tracked"] or 0))
    completed = agg["completed"] or 0
    percent = round(100.0 * completed / total, 1) if total else 0.0

    return CourseSummary(
        course_id=course_id,
        title=course_row["title"] or course_id,
        total_lessons=total,
        completed=completed,
        in_progress=agg["in_progress"] or 0,
        not_started=max((course_row["total_lessons"] or 0) - completed - (agg["in_progress"] or 0), 0),
        progress_percent=percent,
        notes_count=agg["notes_count"] or 0,
        updated_at=course_row["updated_at"],
    )


def _is_fully_completed(conn: sqlite3.Connection, course_id: str) -> bool:
    """True if total_lessons > 0 and all of them are completed."""
    row = conn.execute(
        "SELECT total_lessons FROM courses WHERE course_id = ?", (course_id,)
    ).fetchone()
    if not row or not row["total_lessons"]:
        return False
    done = conn.execute(
        "SELECT COUNT(*) AS n FROM lesson_progress WHERE course_id = ? AND status = 'completed'",
        (course_id,),
    ).fetchone()["n"]
    return done >= row["total_lessons"]


# ---------------------------------------------------------------------------
# FastAPI app + routes
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ARG001
    init_db()
    if N8N_WEBHOOK_URL:
        log.info("n8n webhook events ENABLED -> %s", N8N_WEBHOOK_URL)
    yield


app = FastAPI(
    title="Courses Hub API",
    version="1.0.0",
    description=(
        "Public contract for course progress sync - part of the pi-automate stack. "
        "See /api/v1/docs for the interactive OpenAPI spec."
    ),
    docs_url="/api/v1/docs",
    redoc_url="/api/v1/redoc",
    openapi_url="/api/v1/openapi.json",
    lifespan=lifespan,
)

if CORS_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=CORS_ORIGINS if "*" not in CORS_ORIGINS else ["*"],
        allow_methods=["GET", "PUT", "PATCH", "DELETE"],
        allow_headers=["*"],
    )


def get_db():
    conn = _connect()
    try:
        yield conn
    finally:
        conn.close()


@app.get("/api/health", tags=["meta"])
def health():
    return {"status": "ok", "time": _now()}


@app.get("/api/v1/courses", response_model=list[CourseSummary], tags=["courses"])
def list_courses(conn: sqlite3.Connection = Depends(get_db)):
    ids = [r["course_id"] for r in conn.execute("SELECT course_id FROM courses ORDER BY course_id")]
    return [_summarize_course(conn, cid) for cid in ids]


@app.get("/api/v1/courses/{course_id}", response_model=CourseState, tags=["courses"])
def get_course(course_id: str, conn: sqlite3.Connection = Depends(get_db)):
    state = _load_course_state(conn, course_id)
    if not state:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Course '{course_id}' not found")
    return state


@app.put("/api/v1/courses/{course_id}", response_model=CourseState, tags=["courses"])
def upsert_course(
    course_id: str,
    payload: CourseState,
    conn: sqlite3.Connection = Depends(get_db),
):
    if payload.course_id != course_id:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"course_id mismatch: URL={course_id}, body={payload.course_id}",
        )

    was_completed = _is_fully_completed(conn, course_id)
    now = _now()
    conn.execute("BEGIN IMMEDIATE")
    try:
        _ensure_course(conn, course_id, payload.title, payload.total_lessons)
        conn.execute(
            "UPDATE courses SET meta_json = ?, updated_at = ? WHERE course_id = ?",
            (json.dumps(payload.meta), now, course_id),
        )
        conn.execute("DELETE FROM lesson_progress WHERE course_id = ?", (course_id,))
        conn.executemany(
            """
            INSERT INTO lesson_progress (course_id, lesson_id, status, notes, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                (course_id, lid, lp.status, lp.notes, lp.updated_at or now)
                for lid, lp in payload.lessons.items()
            ],
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    is_completed_now = _is_fully_completed(conn, course_id)
    if is_completed_now and not was_completed:
        summary = _summarize_course(conn, course_id)
        _notify_n8n("course.completed", summary.model_dump())

    log.info("Upserted course %s (%d lessons)", course_id, len(payload.lessons))
    return _load_course_state(conn, course_id)


@app.patch(
    "/api/v1/courses/{course_id}/lessons/{lesson_id}",
    response_model=LessonProgress,
    tags=["lessons"],
)
def patch_lesson(
    course_id: str,
    lesson_id: str,
    patch: LessonPatch,
    conn: sqlite3.Connection = Depends(get_db),
):
    was_completed = _is_fully_completed(conn, course_id)
    now = _now()
    _ensure_course(conn, course_id)
    row = conn.execute(
        "SELECT status, notes FROM lesson_progress WHERE course_id = ? AND lesson_id = ?",
        (course_id, lesson_id),
    ).fetchone()

    new_status = patch.status if patch.status is not None else (row["status"] if row else "not_started")
    new_notes = patch.notes if patch.notes is not None else (row["notes"] if row else "")

    conn.execute(
        """
        INSERT INTO lesson_progress (course_id, lesson_id, status, notes, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(course_id, lesson_id) DO UPDATE SET
            status     = excluded.status,
            notes      = excluded.notes,
            updated_at = excluded.updated_at
        """,
        (course_id, lesson_id, new_status, new_notes, now),
    )
    conn.execute("UPDATE courses SET updated_at = ? WHERE course_id = ?", (now, course_id))

    is_completed_now = _is_fully_completed(conn, course_id)
    if is_completed_now and not was_completed:
        summary = _summarize_course(conn, course_id)
        _notify_n8n("course.completed", summary.model_dump())

    return LessonProgress(status=new_status, notes=new_notes, updated_at=now)


@app.delete("/api/v1/courses/{course_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["courses"])
def reset_course(course_id: str, conn: sqlite3.Connection = Depends(get_db)):
    conn.execute("DELETE FROM lesson_progress WHERE course_id = ?", (course_id,))
    conn.execute(
        "UPDATE courses SET meta_json = '{}', updated_at = datetime('now') WHERE course_id = ?",
        (course_id,),
    )
    log.info("Reset course %s", course_id)
    return None
