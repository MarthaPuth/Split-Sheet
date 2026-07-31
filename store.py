"""Tiny key/value store.

Uses Postgres when DATABASE_URL is set (that's what runs in production, because
hosted filesystems get wiped on every restart and your login tokens would go
with them). Falls back to a JSON file when running on your own machine.
"""

import json
import os
import threading

_lock = threading.Lock()
DB_URL = os.environ.get("DATABASE_URL", "").strip()
FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "store.json")


def _pg():
    import psycopg
    url = DB_URL.replace("postgres://", "postgresql://", 1)
    return psycopg.connect(url, connect_timeout=10)


def init():
    if not DB_URL:
        os.makedirs(os.path.dirname(FILE), exist_ok=True)
        if not os.path.exists(FILE):
            with open(FILE, "w") as f:
                json.dump({}, f)
        return "file"
    with _pg() as c, c.cursor() as cur:
        cur.execute("CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT NOT NULL)")
        c.commit()
    return "postgres"


def get(key, default=None):
    with _lock:
        if not DB_URL:
            try:
                with open(FILE) as f:
                    return json.load(f).get(key, default)
            except (OSError, ValueError):
                return default
        try:
            with _pg() as c, c.cursor() as cur:
                cur.execute("SELECT v FROM kv WHERE k = %s", (key,))
                row = cur.fetchone()
                return json.loads(row[0]) if row else default
        except Exception:
            return default


def put(key, value):
    with _lock:
        if not DB_URL:
            os.makedirs(os.path.dirname(FILE), exist_ok=True)
            try:
                with open(FILE) as f:
                    all_ = json.load(f)
            except (OSError, ValueError):
                all_ = {}
            all_[key] = value
            with open(FILE, "w") as f:
                json.dump(all_, f)
            return
        with _pg() as c, c.cursor() as cur:
            cur.execute(
                "INSERT INTO kv (k, v) VALUES (%s, %s) "
                "ON CONFLICT (k) DO UPDATE SET v = EXCLUDED.v",
                (key, json.dumps(value)))
            c.commit()


def drop(key):
    with _lock:
        if not DB_URL:
            try:
                with open(FILE) as f:
                    all_ = json.load(f)
            except (OSError, ValueError):
                return
            all_.pop(key, None)
            with open(FILE, "w") as f:
                json.dump(all_, f)
            return
        with _pg() as c, c.cursor() as cur:
            cur.execute("DELETE FROM kv WHERE k = %s", (key,))
            c.commit()
