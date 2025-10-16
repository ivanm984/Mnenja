"""Database layer with MySQL and PostgreSQL backends."""
from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import parse_qs, urlparse, unquote

try:  # MySQL is optional
    import pymysql
    from pymysql.cursors import DictCursor as MySQLDictCursor
except Exception:  # pragma: no cover - optional dependency
    pymysql = None
    MySQLDictCursor = None

try:  # PostgreSQL is optional
    import psycopg
    from psycopg.rows import dict_row as PostgresDictRow
except Exception:  # pragma: no cover - optional dependency
    psycopg = None
    PostgresDictRow = None

from .config import DATABASE_URL, build_mysql_dsn, build_postgres_dsn


class DatabaseManager:
    """A tiny database abstraction supporting MySQL and PostgreSQL."""

    def __init__(self, database_url: Optional[str] = None) -> None:
        self.lock = threading.Lock()
        dsn = database_url or DATABASE_URL or build_mysql_dsn() or build_postgres_dsn()
        if not dsn:
            raise RuntimeError(
                "❌ Podatkovna baza ni konfigurirana. Nastavite spremenljivko DATABASE_URL "
                "ali ustrezne MYSQL_/POSTGRES_ vrednosti v okolju."
            )
        self.backend, self.connection_info = self._parse_backend(dsn)

    # ------------------------------------------------------------------
    # Connection helpers
    # ------------------------------------------------------------------
    def _parse_backend(self, dsn: str) -> Tuple[str, Any]:
        parsed = urlparse(dsn)
        scheme = (parsed.scheme or "").lower()
        if scheme.startswith("mysql"):
            if not pymysql:
                raise RuntimeError(
                    "MySQL DSN je podan, vendar modul 'pymysql' ni nameščen. "
                    "Namestite ga z `pip install pymysql`."
                )
            return "mysql", self._build_mysql_params(parsed)
        if scheme in {"postgresql", "postgres"}:
            if not psycopg or not PostgresDictRow:
                raise RuntimeError(
                    "PostgreSQL DSN je podan, vendar modul 'psycopg' ni nameščen. "
                    "Namestite ga z `pip install psycopg[binary]`."
                )
            normalised = dsn
            if scheme == "postgres":
                normalised = dsn.replace("postgres://", "postgresql://", 1)
            return "postgresql", normalised
        raise RuntimeError(
            "Nepodprt format DATABASE_URL. Podprti shemi sta 'mysql://' in 'postgresql://'."
        )

    def _build_mysql_params(self, parsed) -> Dict[str, Any]:
        database = (parsed.path or "").lstrip("/")
        if not database:
            raise RuntimeError("MySQL povezava mora vsebovati ime baze podatkov.")
        query_params = parse_qs(parsed.query or "")
        charset = query_params.get("charset", ["utf8mb4"])[0]
        user = unquote(parsed.username) if parsed.username else ""
        password = unquote(parsed.password) if parsed.password else ""
        return {
            "host": parsed.hostname or "localhost",
            "user": user,
            "password": password,
            "database": database,
            "port": parsed.port or 3306,
            "charset": charset or "utf8mb4",
            "autocommit": False,
            "cursorclass": MySQLDictCursor,
        }

    @contextmanager
    def connect(self):
        if self.backend == "mysql":
            conn = pymysql.connect(**self.connection_info)
        else:  # postgresql
            conn = psycopg.connect(self.connection_info, autocommit=False, row_factory=PostgresDictRow)
        try:
            yield conn
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Schema management
    # ------------------------------------------------------------------
    def init_db(self) -> None:
        with self.lock, self.connect() as conn:
            if self.backend == "mysql":
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
                    cursor.execute(
                        """
                        CREATE TABLE IF NOT EXISTS generated_reports (
                            id BIGINT AUTO_INCREMENT PRIMARY KEY,
                            session_id VARCHAR(64) NOT NULL,
                            project_name VARCHAR(255),
                            summary TEXT,
                            metadata_json LONGTEXT,
                            key_data_json LONGTEXT,
                            excluded_ids_json LONGTEXT,
                            analysis_scope VARCHAR(32),
                            total_analyzed INT,
                            total_available INT,
                            docx_path VARCHAR(500),
                            xlsx_path VARCHAR(500),
                            created_at DATETIME NOT NULL,
                            INDEX idx_reports_session (session_id),
                            INDEX idx_reports_created_at (created_at)
                        ) CHARACTER SET utf8mb4
                        """
                    )
                    cursor.execute(
                        """
                        CREATE TABLE IF NOT EXISTS knowledge_resources (
                            name VARCHAR(100) PRIMARY KEY,
                            payload_json LONGTEXT NOT NULL,
                            updated_at DATETIME NOT NULL
                        ) CHARACTER SET utf8mb4
                        """
                    )
                conn.commit()
            else:  # postgresql
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        CREATE TABLE IF NOT EXISTS saved_sessions (
                            session_id VARCHAR(64) PRIMARY KEY,
                            project_name VARCHAR(255),
                            summary TEXT,
                            data_json TEXT NOT NULL,
                            updated_at TIMESTAMPTZ NOT NULL
                        )
                        """
                    )
                    cursor.execute(
                        """
                        CREATE TABLE IF NOT EXISTS session_revisions (
                            id BIGSERIAL PRIMARY KEY,
                            session_id VARCHAR(64) NOT NULL,
                            requirement_id VARCHAR(64),
                            filename VARCHAR(255),
                            file_path VARCHAR(500),
                            mime_type VARCHAR(100),
                            note VARCHAR(500),
                            uploaded_at TIMESTAMPTZ NOT NULL
                        )
                        """
                    )
                    cursor.execute(
                        """
                        CREATE INDEX IF NOT EXISTS idx_session_requirement
                        ON session_revisions (session_id, requirement_id)
                        """
                    )
                    cursor.execute(
                        """
                        CREATE TABLE IF NOT EXISTS generated_reports (
                            id BIGSERIAL PRIMARY KEY,
                            session_id VARCHAR(64) NOT NULL,
                            project_name VARCHAR(255),
                            summary TEXT,
                            metadata_json TEXT,
                            key_data_json TEXT,
                            excluded_ids_json TEXT,
                            analysis_scope VARCHAR(32),
                            total_analyzed INT,
                            total_available INT,
                            docx_path TEXT,
                            xlsx_path TEXT,
                            created_at TIMESTAMPTZ NOT NULL
                        )
                        """
                    )
                    cursor.execute(
                        """
                        CREATE INDEX IF NOT EXISTS idx_reports_session
                        ON generated_reports (session_id, created_at DESC)
                        """
                    )
                    cursor.execute(
                        """
                        CREATE TABLE IF NOT EXISTS knowledge_resources (
                            name VARCHAR(100) PRIMARY KEY,
                            payload_json JSONB NOT NULL,
                            updated_at TIMESTAMPTZ NOT NULL
                        )
                        """
                    )
                conn.commit()

    # ------------------------------------------------------------------
    # Session storage
    # ------------------------------------------------------------------
    def upsert_session(
        self,
        session_id: str,
        project_name: str,
        summary: str,
        data: Dict,
        *,
        updated_at_override: Optional[str] = None,
    ) -> None:
        payload = json.dumps(data, ensure_ascii=False)
        timestamp = updated_at_override or datetime.utcnow().isoformat()
        with self.lock, self.connect() as conn:
            if self.backend == "mysql":
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
            else:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO saved_sessions (session_id, project_name, summary, data_json, updated_at)
                        VALUES (%s, %s, %s, %s, %s)
                        ON CONFLICT (session_id) DO UPDATE SET
                            project_name = EXCLUDED.project_name,
                            summary = EXCLUDED.summary,
                            data_json = EXCLUDED.data_json,
                            updated_at = EXCLUDED.updated_at
                        """,
                        (session_id, project_name, summary, payload, timestamp),
                    )
                conn.commit()

    def delete_session(self, session_id: str) -> None:
        with self.lock, self.connect() as conn:
            if self.backend == "mysql":
                with conn.cursor() as cursor:
                    cursor.execute("DELETE FROM generated_reports WHERE session_id = %s", (session_id,))
                    cursor.execute("DELETE FROM session_revisions WHERE session_id = %s", (session_id,))
                    cursor.execute("DELETE FROM saved_sessions WHERE session_id = %s", (session_id,))
                conn.commit()
            else:
                with conn.cursor() as cursor:
                    cursor.execute("DELETE FROM generated_reports WHERE session_id = %s", (session_id,))
                    cursor.execute("DELETE FROM session_revisions WHERE session_id = %s", (session_id,))
                    cursor.execute("DELETE FROM saved_sessions WHERE session_id = %s", (session_id,))
                conn.commit()

    def fetch_sessions(self) -> List[Dict]:
        with self.lock, self.connect() as conn:
            if self.backend == "mysql":
                with conn.cursor() as cursor:
                    cursor.execute(
                        "SELECT session_id, project_name, summary, updated_at FROM saved_sessions ORDER BY updated_at DESC"
                    )
                    rows = cursor.fetchall()
            else:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "SELECT session_id, project_name, summary, updated_at FROM saved_sessions ORDER BY updated_at DESC"
                    )
                    rows = cursor.fetchall()
        return [self._normalise_timestamp_dict(row) for row in rows]

    def fetch_session(self, session_id: str) -> Optional[Dict]:
        with self.lock, self.connect() as conn:
            if self.backend == "mysql":
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT session_id, project_name, summary, data_json, updated_at
                        FROM saved_sessions WHERE session_id = %s
                        """,
                        (session_id,),
                    )
                    row = cursor.fetchone()
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
        if not isinstance(row, dict):
            row = dict(row)
        record = {
            "session_id": row["session_id"],
            "project_name": row.get("project_name"),
            "summary": row.get("summary"),
            "updated_at": self._normalise_timestamp(row.get("updated_at")),
            "data": self._load_json(row.get("data_json"), default={}),
        }
        record["reports"] = self.fetch_reports(session_id)
        return record

    # ------------------------------------------------------------------
    # Revisions
    # ------------------------------------------------------------------
    def record_revision(
        self,
        session_id: str,
        filenames: Iterable[str],
        file_paths: Iterable[str],
        requirement_id: Optional[str] = None,
        note: Optional[str] = None,
        mime_types: Optional[Iterable[str]] = None,
        *,
        uploaded_at_override: Optional[str] = None,
    ) -> Dict[str, str]:
        timestamp = uploaded_at_override or datetime.utcnow().isoformat()
        filenames = list(filenames)
        file_paths = list(file_paths)
        mime_types = list(mime_types or [])
        if mime_types and len(mime_types) != len(filenames):
            mime_types = []  # ignore inconsistent data
        with self.lock, self.connect() as conn:
            if self.backend == "mysql":
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
        query = (
            "SELECT requirement_id, filename, file_path, mime_type, note, uploaded_at "
            "FROM session_revisions WHERE session_id = %s"
        )
        params: List[Any] = [session_id]
        if requirement_id:
            query += " AND requirement_id = %s"
            params.append(requirement_id)
        query += " ORDER BY uploaded_at DESC, id DESC"

        with self.lock, self.connect() as conn:
            if self.backend == "mysql":
                with conn.cursor() as cursor:
                    cursor.execute(query, tuple(params))
                    rows = cursor.fetchall()
            else:
                with conn.cursor() as cursor:
                    cursor.execute(query, tuple(params))
                    rows = cursor.fetchall()
        return [self._normalise_timestamp_dict(row) for row in rows]

    # ------------------------------------------------------------------
    # Report storage
    # ------------------------------------------------------------------
    def record_report(
        self,
        session_id: str,
        project_name: Optional[str],
        summary: Optional[str],
        metadata: Dict[str, Any],
        key_data: Dict[str, Any],
        excluded_ids: Iterable[str],
        analysis_scope: Optional[str],
        total_analyzed: Optional[int],
        total_available: Optional[int],
        docx_path: Optional[str],
        xlsx_path: Optional[str],
    ) -> Dict[str, Any]:
        metadata_json = json.dumps(metadata or {}, ensure_ascii=False)
        key_data_json = json.dumps(key_data or {}, ensure_ascii=False)
        excluded_json = json.dumps(list(excluded_ids or []), ensure_ascii=False)
        timestamp = datetime.utcnow().isoformat()
        with self.lock, self.connect() as conn:
            if self.backend == "mysql":
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO generated_reports (
                            session_id, project_name, summary, metadata_json, key_data_json, excluded_ids_json,
                            analysis_scope, total_analyzed, total_available, docx_path, xlsx_path, created_at
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            session_id,
                            project_name,
                            summary,
                            metadata_json,
                            key_data_json,
                            excluded_json,
                            analysis_scope,
                            total_analyzed,
                            total_available,
                            docx_path,
                            xlsx_path,
                            timestamp,
                        ),
                    )
                    cursor.execute("SELECT LAST_INSERT_ID() AS report_id")
                    inserted = cursor.fetchone()
                conn.commit()
            else:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO generated_reports (
                            session_id, project_name, summary, metadata_json, key_data_json, excluded_ids_json,
                            analysis_scope, total_analyzed, total_available, docx_path, xlsx_path, created_at
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        RETURNING id
                        """,
                        (
                            session_id,
                            project_name,
                            summary,
                            metadata_json,
                            key_data_json,
                            excluded_json,
                            analysis_scope,
                            total_analyzed,
                            total_available,
                            docx_path,
                            xlsx_path,
                            timestamp,
                        ),
                    )
                    inserted = cursor.fetchone()
                conn.commit()
        if inserted and not isinstance(inserted, dict):
            inserted = dict(inserted)
        report_id = None
        if isinstance(inserted, dict):
            report_id = inserted.get("id") or inserted.get("report_id")
        return {"id": report_id, "created_at": timestamp}

    def fetch_reports(self, session_id: str) -> List[Dict[str, Any]]:
        with self.lock, self.connect() as conn:
            if self.backend == "mysql":
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT id, project_name, summary, metadata_json, key_data_json, excluded_ids_json,
                               analysis_scope, total_analyzed, total_available, docx_path, xlsx_path, created_at
                        FROM generated_reports
                        WHERE session_id = %s
                        ORDER BY created_at DESC, id DESC
                        """,
                        (session_id,),
                    )
                    rows = cursor.fetchall()
            else:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT id, project_name, summary, metadata_json, key_data_json, excluded_ids_json,
                               analysis_scope, total_analyzed, total_available, docx_path, xlsx_path, created_at
                        FROM generated_reports
                        WHERE session_id = %s
                        ORDER BY created_at DESC, id DESC
                        """,
                        (session_id,),
                    )
                    rows = cursor.fetchall()
        parsed: List[Dict[str, Any]] = []
        for row in rows:
            record = dict(row)
            record["metadata"] = self._load_json(record.pop("metadata_json", None), default={})
            record["key_data"] = self._load_json(record.pop("key_data_json", None), default={})
            record["excluded_ids"] = self._load_json(
                record.pop("excluded_ids_json", None), default=[]
            )
            record["created_at"] = self._normalise_timestamp(record.get("created_at"))
            parsed.append(record)
        return parsed

    # ------------------------------------------------------------------
    # Knowledge base storage
    # ------------------------------------------------------------------
    def upsert_knowledge_resource(self, name: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Insert or update a knowledge base resource payload."""

        encoded = json.dumps(payload or {}, ensure_ascii=False)
        timestamp = datetime.utcnow().isoformat()
        with self.lock, self.connect() as conn:
            if self.backend == "mysql":
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO knowledge_resources (name, payload_json, updated_at)
                        VALUES (%s, %s, %s)
                        ON DUPLICATE KEY UPDATE
                            payload_json = VALUES(payload_json),
                            updated_at = VALUES(updated_at)
                        """,
                        (name, encoded, timestamp),
                    )
                conn.commit()
            else:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO knowledge_resources (name, payload_json, updated_at)
                        VALUES (%s, %s::jsonb, %s)
                        ON CONFLICT (name) DO UPDATE SET
                            payload_json = EXCLUDED.payload_json,
                            updated_at = EXCLUDED.updated_at
                        """,
                        (name, encoded, timestamp),
                    )
                conn.commit()
        return {"updated_at": timestamp}

    def fetch_knowledge_resource(self, name: str) -> Optional[Dict[str, Any]]:
        """Return a decoded knowledge resource payload if present."""

        with self.lock, self.connect() as conn:
            if self.backend == "mysql":
                with conn.cursor() as cursor:
                    cursor.execute(
                        "SELECT payload_json, updated_at FROM knowledge_resources WHERE name = %s",
                        (name,),
                    )
                    row = cursor.fetchone()
            else:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "SELECT payload_json, updated_at FROM knowledge_resources WHERE name = %s",
                        (name,),
                    )
                    row = cursor.fetchone()
        if not row:
            return None
        payload = row.get("payload_json") if isinstance(row, dict) else row[0]
        if isinstance(payload, (bytes, str)):
            payload = self._load_json(payload.decode() if isinstance(payload, bytes) else payload, default={})
        elif payload is None:
            payload = {}
        result = {
            "name": name,
            "payload": payload,
            "updated_at": self._normalise_timestamp(row.get("updated_at") if isinstance(row, dict) else row[1]),
        }
        return result

    def fetch_all_knowledge_resources(self) -> Dict[str, Any]:
        """Return all knowledge resources as a mapping."""

        with self.lock, self.connect() as conn:
            if self.backend == "mysql":
                with conn.cursor() as cursor:
                    cursor.execute("SELECT name, payload_json FROM knowledge_resources")
                    rows = cursor.fetchall()
            else:
                with conn.cursor() as cursor:
                    cursor.execute("SELECT name, payload_json FROM knowledge_resources")
                    rows = cursor.fetchall()

        resources: Dict[str, Any] = {}
        for row in rows:
            if not isinstance(row, dict):
                row = dict(row)
            payload = row.get("payload_json")
            if isinstance(payload, (bytes, str)):
                payload = self._load_json(payload.decode() if isinstance(payload, bytes) else payload, default={})
            elif payload is None:
                payload = {}
            resources[row["name"]] = payload
        return resources

    def delete_all_knowledge_resources(self) -> None:
        with self.lock, self.connect() as conn:
            if self.backend == "mysql":
                with conn.cursor() as cursor:
                    cursor.execute("DELETE FROM knowledge_resources")
                conn.commit()
            else:
                with conn.cursor() as cursor:
                    cursor.execute("DELETE FROM knowledge_resources")
                conn.commit()

    def supports_vector_search(self) -> bool:
        return self.backend == "postgresql"

    def search_vector_knowledge(
        self,
        embedding: Sequence[float],
        *,
        limit: int = 20,
        sources: Optional[Sequence[str]] = None,
    ) -> List[Dict[str, Any]]:
        """Run a nearest-neighbour search over the vectorised knowledge base."""

        if self.backend != "postgresql":
            raise RuntimeError("Vektorsko iskanje je podprto le pri PostgreSQL bazi podatkov.")
        if not embedding:
            return []

        params = {
            "embedding": str([float(x) for x in embedding]),  # Vektor pretvorimo v string
            "limit": int(limit),
        }
        source_list: List[str] = []
        if sources:
            source_list = [str(item) for item in sources if str(item).strip()]

        with self.lock, self.connect() as conn:
            with conn.cursor() as cursor:
                params: List[Any] = [clean_embedding]
                where_clause = ""
                if source_list:
                    where_clause = "WHERE vir = ANY(%s)"
                    params.append(source_list)
                params.extend([clean_embedding, int(limit)])
                cursor.execute(
                    f"""
                    SELECT id, vir, kljuc, vsebina,
                           1.0 / (1.0 + (vektor <-> %s)) AS similarity
                    FROM vektorizirano_znanje
                    {where_clause}
                    ORDER BY vektor <-> %s
                    LIMIT %s
                    """,
                    params,
                )
                rows = cursor.fetchall()

        results: List[Dict[str, Any]] = []
        for row in rows:
            record = dict(row)
            record["similarity"] = float(record.get("similarity") or 0.0)
            results.append(record)
        return results

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _normalise_timestamp(self, value: Any) -> Any:
        if isinstance(value, datetime):
            return value.isoformat()
        return value

    def _normalise_timestamp_dict(self, row: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(row)
        if "updated_at" in result:
            result["updated_at"] = self._normalise_timestamp(result["updated_at"])
        if "uploaded_at" in result:
            result["uploaded_at"] = self._normalise_timestamp(result["uploaded_at"])
        if "created_at" in result:
            result["created_at"] = self._normalise_timestamp(result["created_at"])
        return result

    def _load_json(self, payload: Optional[str], *, default: Any) -> Any:
        if payload is None:
            return default
        text = payload.strip()
        if not text:
            return default
        try:
            return json.loads(text)
        except Exception:
            return default


def compute_session_summary(data: Dict[str, Any]) -> str:
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

def migrate_sqlite_database(sqlite_path: Path, target_manager: DatabaseManager) -> Dict[str, int]:
    """Migrate data from the legacy SQLite database into the configured backend."""

    import sqlite3

    sqlite_path = Path(sqlite_path)
    if not sqlite_path.exists():
        raise FileNotFoundError(f"SQLite baza {sqlite_path} ne obstaja.")

    target_manager.init_db()

    migrated_sessions = 0
    migrated_revisions = 0

    with sqlite3.connect(str(sqlite_path)) as source:
        source.row_factory = sqlite3.Row
        session_rows = source.execute(
            "SELECT session_id, project_name, summary, data_json, updated_at FROM saved_sessions"
        ).fetchall()
        revision_rows = source.execute(
            """
            SELECT session_id, requirement_id, filename, file_path, mime_type, note, uploaded_at
            FROM session_revisions
            ORDER BY uploaded_at ASC, id ASC
            """
        ).fetchall()

    for row in session_rows:
        payload = {}
        raw_payload = row["data_json"]
        if raw_payload:
            try:
                payload = json.loads(raw_payload)
            except Exception:
                payload = {}
        target_manager.upsert_session(
            row["session_id"],
            row["project_name"] or "",
            row["summary"] or "",
            payload,
            updated_at_override=row["updated_at"],
        )
        migrated_sessions += 1

    for row in revision_rows:
        mime = row["mime_type"]
        mime_list = [mime] if mime else []
        target_manager.record_revision(
            row["session_id"],
            [row["filename"] or ""],
            [row["file_path"]],
            requirement_id=row["requirement_id"],
            note=row["note"],
            mime_types=mime_list,
            uploaded_at_override=row["uploaded_at"],
        )
        migrated_revisions += 1

    return {"sessions": migrated_sessions, "revisions": migrated_revisions}


__all__ = ["DatabaseManager", "compute_session_summary", "migrate_sqlite_database"]
