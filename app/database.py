"""Database layer with SQLite and optional MySQL backends."""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional

try:  # MySQL is optional
    import pymysql
    from pymysql.cursors import DictCursor as MySQLDictCursor
except Exception:  # pragma: no cover - optional dependency
    pymysql = None
    MySQLDictCursor = None

from .config import (
    DEFAULT_SQLITE_PATH,
    MYSQL_DATABASE,
    MYSQL_HOST,
    MYSQL_PASSWORD,
    MYSQL_PORT,
    MYSQL_USER,
)


class DatabaseManager:
    """A tiny database abstraction supporting SQLite and MySQL."""

    def __init__(self, sqlite_path: Optional[Path] = None) -> None:
        self.lock = threading.Lock()
        if MYSQL_HOST and MYSQL_USER and MYSQL_PASSWORD and MYSQL_DATABASE:
            if not pymysql:
                raise RuntimeError(
                    "MySQL konfiguracija je nastavljena, vendar modul 'pymysql' ni nameščen. "
                    "Namestite ga z `pip install pymysql` ali odstranite MySQL okoljske spremenljivke."
                )
            self.backend = "mysql"
            self.mysql_params = {
                "host": MYSQL_HOST,
                "user": MYSQL_USER,
                "password": MYSQL_PASSWORD,
                "database": MYSQL_DATABASE,
                "port": int(MYSQL_PORT or 3306),
                "charset": "utf8mb4",
                "autocommit": False,
                "cursorclass": MySQLDictCursor,
            }
            self.sqlite_path = None
        else:
            self.backend = "sqlite"
            self.sqlite_path = str((sqlite_path or DEFAULT_SQLITE_PATH).resolve())
            Path(self.sqlite_path).parent.mkdir(parents=True, exist_ok=True)
            self.mysql_params = None

    @contextmanager
    def connect(self):
        if self.backend == "sqlite":
            conn = sqlite3.connect(self.sqlite_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
        else:  # mysql
            conn = pymysql.connect(**self.mysql_params)
        try:
            yield conn
        finally:
            conn.close()

    def init_db(self) -> None:
        with self.lock, self.connect() as conn:
            if self.backend == "sqlite":
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS saved_sessions (
                        session_id TEXT PRIMARY KEY,
                        project_name TEXT,
                        summary TEXT,
                        data_json TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS session_revisions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        session_id TEXT NOT NULL,
                        requirement_id TEXT,
                        filename TEXT,
                        file_path TEXT,
                        mime_type TEXT,
                        note TEXT,
                        uploaded_at TEXT NOT NULL
                    )
                    """
                )
            else:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        CREATE TABLE IF NOT EXISTS saved_sessions (
                            session_id VARCHAR(64) PRIMARY KEY,
                            project_name VARCHAR(255),
                            summary TEXT,
                            data_json LONGTEXT NOT NULL,
                            updated_at DATETIME NOT NULL
                        ) CHARACTER SET utf8mb4
                        """
                    )
                    cursor.execute(
                        """
                        CREATE TABLE IF NOT EXISTS session_revisions (
                            id BIGINT AUTO_INCREMENT PRIMARY KEY,
                            session_id VARCHAR(64) NOT NULL,
                            requirement_id VARCHAR(64),
                            filename VARCHAR(255),
                            file_path VARCHAR(500),
                            mime_type VARCHAR(100),
                            note VARCHAR(500),
                            uploaded_at DATETIME NOT NULL,
                            INDEX idx_session_requirement (session_id, requirement_id)
                        ) CHARACTER SET utf8mb4
                        """
                    )
                conn.commit()

    def upsert_session(self, session_id: str, project_name: str, summary: str, data: Dict) -> None:
        payload = json.dumps(data, ensure_ascii=False)
        timestamp = datetime.utcnow().isoformat()
        with self.lock, self.connect() as conn:
            if self.backend == "sqlite":
                conn.execute(
                    """
                    INSERT INTO saved_sessions (session_id, project_name, summary, data_json, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(session_id) DO UPDATE SET
                        project_name=excluded.project_name,
                        summary=excluded.summary,
                        data_json=excluded.data_json,
                        updated_at=excluded.updated_at
                    """,
                    (session_id, project_name, summary, payload, timestamp),
                )
                conn.commit()
            else:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO saved_sessions (session_id, project_name, summary, data_json, updated_at)
                        VALUES (%s, %s, %s, %s, %s)
                        ON DUPLICATE KEY UPDATE
                            project_name=VALUES(project_name),
                            summary=VALUES(summary),
                            data_json=VALUES(data_json),
                            updated_at=VALUES(updated_at)
                        """,
                        (session_id, project_name, summary, payload, timestamp),
                    )
                conn.commit()

    def delete_session(self, session_id: str) -> None:
        with self.lock, self.connect() as conn:
            if self.backend == "sqlite":
                conn.execute("DELETE FROM saved_sessions WHERE session_id = ?", (session_id,))
                conn.execute("DELETE FROM session_revisions WHERE session_id = ?", (session_id,))
                conn.commit()
            else:
                with conn.cursor() as cursor:
                    cursor.execute("DELETE FROM saved_sessions WHERE session_id = %s", (session_id,))
                    cursor.execute("DELETE FROM session_revisions WHERE session_id = %s", (session_id,))
                conn.commit()

    def fetch_sessions(self) -> List[Dict]:
        with self.lock, self.connect() as conn:
            if self.backend == "sqlite":
                cursor = conn.execute(
                    "SELECT session_id, project_name, summary, updated_at FROM saved_sessions ORDER BY updated_at DESC"
                )
                return [dict(row) for row in cursor.fetchall()]
            else:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "SELECT session_id, project_name, summary, updated_at FROM saved_sessions ORDER BY updated_at DESC"
                    )
                    return cursor.fetchall()

    def fetch_session(self, session_id: str) -> Optional[Dict]:
        with self.lock, self.connect() as conn:
            if self.backend == "sqlite":
                cursor = conn.execute(
                    "SELECT session_id, project_name, summary, data_json, updated_at FROM saved_sessions WHERE session_id = ?",
                    (session_id,),
                )
                row = cursor.fetchone()
                if not row:
                    return None
                data = json.loads(row["data_json"])
                return {
                    "session_id": row["session_id"],
                    "project_name": row["project_name"],
                    "summary": row["summary"],
                    "updated_at": row["updated_at"],
                    "data": data,
                }
            else:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT session_id, project_name, summary, data_json, updated_at
                        FROM saved_sessions WHERE session_id = %s
                        """,
                        (session_id,),
                    )
                    row = cursor.fetchone()
                    if not row:
                        return None
                    data = json.loads(row["data_json"])
                    row["data"] = data
                    return row

    def record_revision(
        self,
        session_id: str,
        filenames: Iterable[str],
        file_paths: Iterable[str],
        requirement_id: Optional[str] = None,
        note: Optional[str] = None,
        mime_types: Optional[Iterable[str]] = None,
    ) -> Dict[str, str]:
        timestamp = datetime.utcnow().isoformat()
        filenames = list(filenames)
        file_paths = list(file_paths)
        mime_types = list(mime_types or [])
        if mime_types and len(mime_types) != len(filenames):
            mime_types = []  # ignore inconsistent data
        with self.lock, self.connect() as conn:
            if self.backend == "sqlite":
                for index, name in enumerate(filenames):
                    conn.execute(
                        """
                        INSERT INTO session_revisions (session_id, requirement_id, filename, file_path, mime_type, note, uploaded_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            session_id,
                            requirement_id,
                            name,
                            file_paths[index] if index < len(file_paths) else None,
                            mime_types[index] if index < len(mime_types) else None,
                            note,
                            timestamp,
                        ),
                    )
                conn.commit()
            else:
                with conn.cursor() as cursor:
                    for index, name in enumerate(filenames):
                        cursor.execute(
                            """
                            INSERT INTO session_revisions (session_id, requirement_id, filename, file_path, mime_type, note, uploaded_at)
                            VALUES (%s, %s, %s, %s, %s, %s, %s)
                            """,
                            (
                                session_id,
                                requirement_id,
                                name,
                                file_paths[index] if index < len(file_paths) else None,
                                mime_types[index] if index < len(mime_types) else None,
                                note,
                                timestamp,
                            ),
                        )
                conn.commit()
        return {"uploaded_at": timestamp}

    def fetch_revisions(self, session_id: str, requirement_id: Optional[str] = None) -> List[Dict]:
        query = "SELECT requirement_id, filename, file_path, mime_type, note, uploaded_at FROM session_revisions WHERE session_id = ?"
        params: List = [session_id]
        placeholder = "?"
        if self.backend == "mysql":
            query = query.replace("?", "%s")
            placeholder = "%s"
        if requirement_id:
            query += f" AND requirement_id = {placeholder}"
            params.append(requirement_id)
        query += " ORDER BY uploaded_at DESC, id DESC"

        with self.lock, self.connect() as conn:
            if self.backend == "sqlite":
                cursor = conn.execute(query, tuple(params))
                return [dict(row) for row in cursor.fetchall()]
            else:
                with conn.cursor() as cursor:
                    cursor.execute(query, tuple(params))
                    return cursor.fetchall()


def compute_session_summary(data: Dict[str, any]) -> str:
    try:
        zahteve = data.get("zahteve") or []
        if not isinstance(zahteve, list):
            return ""
        total = len(zahteve)
        results_map = data.get("resultsMap") or {}
        if not isinstance(results_map, dict):
            results_map = {}
        non_compliant = 0
        for item in zahteve:
            if not isinstance(item, dict):
                continue
            result = results_map.get(item.get("id"), {})
            status_text = (result.get("skladnost") or "").lower()
            if "nesklad" in status_text:
                non_compliant += 1
        if total == 0:
            return "Ni zahtev" if results_map else ""
        return f"{total} zahtev, {non_compliant} neskladnih"
    except Exception:
        return ""


__all__ = ["DatabaseManager", "compute_session_summary"]
