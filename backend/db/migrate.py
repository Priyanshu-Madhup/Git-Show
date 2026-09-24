"""Apply db/schema.sql to SUPABASE_DB_URL. Run from backend/: python db/migrate.py"""

import os
from pathlib import Path

import psycopg
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

schema = (Path(__file__).resolve().parent / "schema.sql").read_text(encoding="utf-8")

with psycopg.connect(os.environ["SUPABASE_DB_URL"], connect_timeout=15) as conn:
    conn.execute(schema)
    tables = conn.execute(
        "select table_name from information_schema.tables "
        "where table_schema = 'public' order by table_name"
    ).fetchall()

print("Schema applied. Tables:", ", ".join(t[0] for t in tables))
