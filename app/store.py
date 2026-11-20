import json
import sqlite3
from contextlib import closing, contextmanager

from app.errors import DomainError
from app.manifest import canonical

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    manifest TEXT NOT NULL,
    results TEXT NOT NULL,
    sample TEXT NOT NULL DEFAULT '',
    revision INTEGER NOT NULL DEFAULT 1,
    supersedes TEXT,
    lineage_root TEXT,
    revision_reason TEXT NOT NULL DEFAULT '',
    revision_detail TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(id),
    time TEXT NOT NULL,
    level TEXT NOT NULL,
    code TEXT NOT NULL,
    message TEXT NOT NULL,
    source_time TEXT
);
CREATE INDEX IF NOT EXISTS run_history ON runs(created_at DESC, id DESC);
"""

# 旧库在原地补列，保留已发出的结论；已有行由 initialize 回填。
ADDED_COLUMNS = {
    "sample": "TEXT NOT NULL DEFAULT ''",
    "revision": "INTEGER NOT NULL DEFAULT 1",
    "supersedes": "TEXT",
    "lineage_root": "TEXT",
    "revision_reason": "TEXT NOT NULL DEFAULT ''",
    "revision_detail": "TEXT NOT NULL DEFAULT '{}'",
}


class Store:
    def __init__(self, path):
        self.path = path

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        return db

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db:
            db.execute("PRAGMA journal_mode = WAL")
            db.executescript(SCHEMA)
            self._migrate(db)
            db.commit()

    @staticmethod
    def _migrate(db):
        columns = {row["name"] for row in db.execute("PRAGMA table_info(runs)")}
        for name, spec in ADDED_COLUMNS.items():
            if name not in columns:
                db.execute(f"ALTER TABLE runs ADD COLUMN {name} {spec}")
        db.execute("CREATE INDEX IF NOT EXISTS run_sample ON runs(sample, revision DESC)")
        for row in db.execute("SELECT id, manifest FROM runs WHERE sample = '' OR lineage_root IS NULL"):
            manifest = json.loads(row["manifest"])
            db.execute(
                """UPDATE runs SET sample = ?, revision = ?, supersedes = ?, lineage_root = ?,
                       revision_reason = ?, revision_detail = ? WHERE id = ?""",
                (
                    manifest.get("sample", ""),
                    manifest.get("revision", 1),
                    manifest.get("supersedes"),
                    manifest.get("lineage_root", row["id"]),
                    manifest.get("revision_reason", ""),
                    canonical(manifest.get("revision_detail", {})),
                    row["id"],
                ),
            )

    @contextmanager
    def transaction(self):
        try:
            with closing(self.connect()) as db, db:
                db.execute("BEGIN IMMEDIATE")
                yield db
        except sqlite3.OperationalError as exc:
            if (getattr(exc, "sqlite_errorcode", 0) & 255) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                raise DomainError("STORE_BUSY", 503) from exc
            raise

    def read(self, run_id):
        with closing(self.connect()) as db:
            result = find(db, run_id)
        if result is None:
            raise DomainError("RUN_UNKNOWN", 404)
        return result

    def history(self):
        with closing(self.connect()) as db:
            rows = db.execute("SELECT manifest FROM runs ORDER BY created_at DESC, id DESC LIMIT 50")
            return [json.loads(row["manifest"]) for row in rows]

    def lineage(self, run_id):
        with closing(self.connect()) as db:
            row = db.execute("SELECT lineage_root FROM runs WHERE id = ?", (run_id,)).fetchone()
            if row is None:
                raise DomainError("RUN_UNKNOWN", 404)
            root = row["lineage_root"] or run_id
            rows = db.execute(
                "SELECT manifest FROM runs WHERE lineage_root = ? OR id = ? ORDER BY revision, created_at, id",
                (root, root),
            )
            return [json.loads(item["manifest"]) for item in rows]

    def events(self, after, limit):
        with closing(self.connect()) as db:
            rows = db.execute("SELECT * FROM events WHERE seq > ? ORDER BY seq LIMIT ?", (after, limit))
            return [dict(row) for row in rows]


def find(db, run_id):
    row = db.execute("SELECT manifest, results FROM runs WHERE id = ?", (run_id,)).fetchone()
    return (json.loads(row["manifest"]), json.loads(row["results"])) if row else None


def latest(db, sample):
    row = db.execute(
        "SELECT id FROM runs WHERE sample = ? ORDER BY revision DESC, created_at DESC, id DESC LIMIT 1",
        (sample,),
    ).fetchone()
    return find(db, row["id"]) if row else None


def add_run(db, manifest, results):
    db.execute(
        """INSERT INTO runs
           (id, created_at, manifest, results, sample, revision, supersedes, lineage_root,
            revision_reason, revision_detail)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            manifest["run_id"], manifest["created_at"], canonical(manifest), canonical(results),
            manifest.get("sample", ""), manifest.get("revision", 1), manifest.get("supersedes"),
            manifest.get("lineage_root"), manifest.get("revision_reason", ""),
            canonical(manifest.get("revision_detail", {})),
        ),
    )


def add_events(db, run_id, events):
    db.executemany("""INSERT INTO events (run_id,time,level,code,message,source_time)
                    VALUES (?, ?, ?, ?, ?, ?)""", ((run_id, *event) for event in events))
