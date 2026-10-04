"""nova_core.rag.store — where documents, chunks and vectors live.

SQLite for anything relational, a numpy memmap for vectors, the original file
kept content-addressed on disk. No new database engine, per the audit: a
second store would be the seventh memory system in this repository.

Two boundaries are enforced here rather than trusted to callers:

**Scope.** Every row carries a `scope` -- `user:<id>` for private documents,
`project:<id>` for a shared workspace. Retrieval takes a list of scopes and
filters in SQL. A personal document cannot surface in a project answer
because the query never sees it, not because the prompt asked nicely.

**Change detection.** Documents are keyed by content hash. Re-adding the same
file is a no-op; adding a changed file supersedes the old version rather than
duplicating it, so "the deadline moved" does not leave both answers
retrievable with equal confidence.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

log = logging.getLogger("nova.rag.store")

SCHEMA_VERSION = 1


@dataclass
class Document:
    id: str
    scope: str
    filename: str
    kind: str
    checksum: str
    title: str = ""
    source: str = ""
    bytes: int = 0
    page_count: int = 0
    chunk_count: int = 0
    added_at: float = 0.0
    updated_at: float = 0.0
    embedding_backend: str = ""
    embedding_dim: int = 0
    asset_path: str = ""
    tags: list[str] = field(default_factory=list)
    superseded_by: str = ""
    warnings: list[str] = field(default_factory=list)


@dataclass
class ChunkRow:
    id: str
    document_id: str
    scope: str
    ordinal: int
    text: str
    locator: str = ""
    heading: str = ""
    kind: str = "text"
    vector_row: int = -1


@dataclass
class Hit:
    chunk: ChunkRow
    document: Document
    score: float
    how: str = ""              # "semantic" | "keyword" | "hybrid"

    def citation(self) -> str:
        where = self.chunk.locator or ""
        heading = self.chunk.heading or ""
        # "section: Budget, Budget" reads like a bug, because it is one: the
        # locator already carries the heading for structured formats.
        if heading and heading not in where:
            where = f"{where}, {heading}" if where else heading
        name = self.document.title or self.document.filename
        return f"{name} ({where})" if where else name


_DDL = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY, value TEXT
);
CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY,
    scope TEXT NOT NULL,
    filename TEXT NOT NULL,
    kind TEXT NOT NULL,
    checksum TEXT NOT NULL,
    title TEXT DEFAULT '',
    source TEXT DEFAULT '',
    bytes INTEGER DEFAULT 0,
    page_count INTEGER DEFAULT 0,
    chunk_count INTEGER DEFAULT 0,
    added_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    embedding_backend TEXT DEFAULT '',
    embedding_dim INTEGER DEFAULT 0,
    asset_path TEXT DEFAULT '',
    tags TEXT DEFAULT '[]',
    superseded_by TEXT DEFAULT '',
    warnings TEXT DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS ix_documents_scope ON documents(scope);
CREATE UNIQUE INDEX IF NOT EXISTS ix_documents_scope_checksum
    ON documents(scope, checksum);

CREATE TABLE IF NOT EXISTS chunks (
    id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    scope TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    text TEXT NOT NULL,
    locator TEXT DEFAULT '',
    heading TEXT DEFAULT '',
    kind TEXT DEFAULT 'text',
    vector_row INTEGER DEFAULT -1,
    tokens TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_chunks_scope ON chunks(scope);
CREATE INDEX IF NOT EXISTS ix_chunks_document ON chunks(document_id);
"""


class RagStore:
    """One store per NOVA installation; scopes separate users and projects."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "index.sqlite"
        self.vectors_path = self.root / "vectors.npy"
        self.assets_dir = self.root / "assets"
        self.assets_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._vectors: np.ndarray | None = None
        self._dim = 0
        self._init_db()
        self._load_vectors()

    # -- database ----------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_db(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(_DDL)
            conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES(?, ?)",
                ("schema_version", str(SCHEMA_VERSION)))

    # -- vectors -----------------------------------------------------------

    def _load_vectors(self) -> None:
        if not self.vectors_path.exists():
            self._vectors = None
            return
        try:
            arr = np.load(self.vectors_path)
            if arr.ndim == 2:
                self._vectors = arr.astype(np.float32)
                self._dim = int(arr.shape[1])
        except Exception as e:
            # A corrupt vector file must not make the whole library
            # unreadable; the text index alone still answers keyword queries.
            log.warning("[RAG] vector file unreadable (%s); keyword search only",
                        type(e).__name__)
            self._vectors = None

    def _append_vectors(self, vectors: np.ndarray) -> int:
        """Append and return the row index of the first added vector."""
        with self._lock:
            if vectors.size == 0:
                return -1
            vectors = vectors.astype(np.float32)
            if self._vectors is None or self._vectors.size == 0:
                start = 0
                self._vectors = vectors
                self._dim = int(vectors.shape[1])
            else:
                if vectors.shape[1] != self._vectors.shape[1]:
                    raise ValueError(
                        f"embedding dimension changed "
                        f"({self._vectors.shape[1]} -> {vectors.shape[1]}). "
                        "The library must be reindexed after switching "
                        "embedding backend.")
                start = int(self._vectors.shape[0])
                self._vectors = np.vstack([self._vectors, vectors])
            np.save(self.vectors_path, self._vectors)
            return start

    @property
    def vector_count(self) -> int:
        return 0 if self._vectors is None else int(self._vectors.shape[0])

    # -- documents ---------------------------------------------------------

    def find_by_filename(self, scope: str, filename: str) -> Document | None:
        """The live document with this name, if any.

        Distinct from find_by_checksum: identical content is a duplicate,
        while the *same name with different content* is a new version. Keying
        supersession on the checksum could never work -- a changed file has a
        different checksum by definition, so the old version would stay live
        and both answers would remain retrievable.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM documents WHERE scope=? AND filename=? "
                "AND superseded_by='' ORDER BY added_at DESC LIMIT 1",
                (scope, filename)).fetchone()
        return _document(row) if row else None

    def find_by_checksum(self, scope: str, checksum: str) -> Document | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM documents WHERE scope=? AND checksum=?",
                (scope, checksum)).fetchone()
        return _document(row) if row else None

    def add_document(self, doc: Document, chunks: list[ChunkRow],
                     vectors: np.ndarray | None) -> Document:
        """Insert a document with its chunks and (optionally) their vectors."""
        with self._lock:
            first_row = -1
            if vectors is not None and vectors.size:
                if len(chunks) != vectors.shape[0]:
                    raise ValueError("chunk/vector count mismatch")
                first_row = self._append_vectors(vectors)

            now = time.time()
            doc.added_at = doc.added_at or now
            doc.updated_at = now
            doc.chunk_count = len(chunks)

            with self._connect() as conn:
                conn.execute(
                    """INSERT INTO documents
                       (id, scope, filename, kind, checksum, title, source,
                        bytes, page_count, chunk_count, added_at, updated_at,
                        embedding_backend, embedding_dim, asset_path, tags,
                        superseded_by, warnings)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (doc.id, doc.scope, doc.filename, doc.kind, doc.checksum,
                     doc.title, doc.source, doc.bytes, doc.page_count,
                     doc.chunk_count, doc.added_at, doc.updated_at,
                     doc.embedding_backend, doc.embedding_dim, doc.asset_path,
                     json.dumps(doc.tags), doc.superseded_by,
                     json.dumps(doc.warnings)))
                from .embeddings import tokenise
                for i, ch in enumerate(chunks):
                    ch.vector_row = (first_row + i) if first_row >= 0 else -1
                    conn.execute(
                        """INSERT INTO chunks
                           (id, document_id, scope, ordinal, text, locator,
                            heading, kind, vector_row, tokens)
                           VALUES (?,?,?,?,?,?,?,?,?,?)""",
                        (ch.id, ch.document_id, ch.scope, ch.ordinal, ch.text,
                         ch.locator, ch.heading, ch.kind, ch.vector_row,
                         " ".join(tokenise(ch.text))))
            return doc

    def supersede(self, old_id: str, new_id: str) -> None:
        """Mark an older version as replaced. Kept, not deleted: 'what did the
        proposal say before we changed it' is a real question."""
        with self._lock, self._connect() as conn:
            conn.execute("UPDATE documents SET superseded_by=?, updated_at=? "
                         "WHERE id=?", (new_id, time.time(), old_id))

    def delete_document(self, doc_id: str) -> bool:
        """Remove a document and its chunks.

        Vector rows are left in place and simply become unreferenced --
        compacting would renumber every other row. `reindex` reclaims them.
        """
        with self._lock, self._connect() as conn:
            cur = conn.execute("DELETE FROM documents WHERE id=?", (doc_id,))
            conn.execute("DELETE FROM chunks WHERE document_id=?", (doc_id,))
            return cur.rowcount > 0

    def get_document(self, doc_id: str) -> Document | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM documents WHERE id=?",
                               (doc_id,)).fetchone()
        return _document(row) if row else None

    def documents(self, scopes: list[str] | None = None,
                  include_superseded: bool = False) -> list[Document]:
        sql = "SELECT * FROM documents"
        args: list = []
        where = []
        if scopes:
            where.append(f"scope IN ({','.join('?' * len(scopes))})")
            args.extend(scopes)
        if not include_superseded:
            where.append("superseded_by = ''")
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY added_at DESC"
        with self._connect() as conn:
            return [_document(r) for r in conn.execute(sql, args).fetchall()]

    # -- retrieval ---------------------------------------------------------

    def candidate_chunks(self, scopes: list[str]) -> list[tuple[ChunkRow, str]]:
        """Every chunk visible to these scopes, with its token string.

        Scope filtering happens in SQL. That is the whole isolation guarantee:
        a private document is not ranked-then-hidden, it is never loaded.
        """
        if not scopes:
            return []
        placeholders = ",".join("?" * len(scopes))
        sql = (f"SELECT c.*, d.superseded_by FROM chunks c "
               f"JOIN documents d ON d.id = c.document_id "
               f"WHERE c.scope IN ({placeholders}) AND d.superseded_by = ''")
        with self._connect() as conn:
            rows = conn.execute(sql, scopes).fetchall()
        return [(_chunk(r), r["tokens"] or "") for r in rows]

    def vectors_for(self, rows: list[int]) -> np.ndarray | None:
        if self._vectors is None or not rows:
            return None
        valid = [r for r in rows if 0 <= r < self._vectors.shape[0]]
        if not valid:
            return None
        return self._vectors[valid]

    def stats(self, scopes: list[str] | None = None) -> dict:
        with self._connect() as conn:
            if scopes:
                ph = ",".join("?" * len(scopes))
                docs = conn.execute(
                    f"SELECT COUNT(*) FROM documents WHERE scope IN ({ph}) "
                    "AND superseded_by=''", scopes).fetchone()[0]
                chunks = conn.execute(
                    f"SELECT COUNT(*) FROM chunks WHERE scope IN ({ph})",
                    scopes).fetchone()[0]
            else:
                docs = conn.execute(
                    "SELECT COUNT(*) FROM documents WHERE superseded_by=''"
                ).fetchone()[0]
                chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        return {"documents": int(docs), "chunks": int(chunks),
                "vectors": self.vector_count, "dimension": self._dim,
                "root": str(self.root)}


def _document(row: sqlite3.Row) -> Document:
    return Document(
        id=row["id"], scope=row["scope"], filename=row["filename"],
        kind=row["kind"], checksum=row["checksum"], title=row["title"],
        source=row["source"], bytes=row["bytes"], page_count=row["page_count"],
        chunk_count=row["chunk_count"], added_at=row["added_at"],
        updated_at=row["updated_at"],
        embedding_backend=row["embedding_backend"],
        embedding_dim=row["embedding_dim"], asset_path=row["asset_path"],
        tags=json.loads(row["tags"] or "[]"),
        superseded_by=row["superseded_by"],
        warnings=json.loads(row["warnings"] or "[]"),
    )


def _chunk(row: sqlite3.Row) -> ChunkRow:
    return ChunkRow(
        id=row["id"], document_id=row["document_id"], scope=row["scope"],
        ordinal=row["ordinal"], text=row["text"], locator=row["locator"],
        heading=row["heading"], kind=row["kind"], vector_row=row["vector_row"],
    )


def new_id(prefix: str = "doc") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


__all__ = ["RagStore", "Document", "ChunkRow", "Hit", "new_id",
           "SCHEMA_VERSION"]
