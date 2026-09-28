import json
import sqlite3
import threading

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    repo TEXT NOT NULL,
    number INTEGER NOT NULL,
    kind TEXT NOT NULL,              -- 'pr' | 'issue'
    title TEXT, state TEXT, is_draft INTEGER DEFAULT 0,
    author TEXT, url TEXT,
    created_at TEXT, updated_at TEXT, closed_at TEXT,
    labels TEXT DEFAULT '[]',        -- json [{name,color}]
    assignees TEXT DEFAULT '[]',     -- json [login]
    review_requests TEXT DEFAULT '[]',
    review_decision TEXT,
    head_sha TEXT, base_ref TEXT, head_ref TEXT,
    additions INTEGER, deletions INTEGER, changed_files INTEGER,
    comments_count INTEGER,
    ci_state TEXT,
    activity_at TEXT,                -- latest non-hidden, non-noise activity
    detail_updated_at TEXT,          -- items.updated_at at time of last detail sync
    PRIMARY KEY (repo, number)
);
CREATE TABLE IF NOT EXISTS tags (
    repo TEXT, number INTEGER, tag TEXT,
    PRIMARY KEY (repo, number, tag)
);
CREATE TABLE IF NOT EXISTS details (
    repo TEXT, number INTEGER,
    body_html TEXT,
    timeline TEXT,                   -- json list
    threads TEXT,                    -- json list
    files TEXT,                      -- json list (PR only)
    files_sha TEXT,
    PRIMARY KEY (repo, number)
);
CREATE TABLE IF NOT EXISTS marks (    -- local "reviewed at" baseline
    repo TEXT, number INTEGER, head_sha TEXT, marked_at TEXT,
    PRIMARY KEY (repo, number)
);
CREATE TABLE IF NOT EXISTS seen (
    repo TEXT, number INTEGER, seen_at TEXT,
    PRIMARY KEY (repo, number)
);
CREATE TABLE IF NOT EXISTS hidden_authors (pattern TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS blobs (   -- file contents cache for interdiffs
    sha TEXT, path TEXT, text TEXT,
    PRIMARY KEY (sha, path)
);
"""

_local = threading.local()


def conn() -> sqlite3.Connection:
    c = getattr(_local, "conn", None)
    if c is None:
        c = sqlite3.connect(DB_PATH, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        _local.conn = c
    return c


def init():
    conn().executescript(SCHEMA)
    conn().commit()


def get_meta(key, default=None):
    r = conn().execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default


def set_meta(key, value):
    conn().execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (key, value))
    conn().commit()


def jl(s, default=None):
    if s is None:
        return default if default is not None else []
    return json.loads(s)
