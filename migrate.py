import sqlite3
from pathlib import Path

db_path = Path("data/edgefleet.db")
if db_path.exists():
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    columns = [row[1] for row in cur.execute("PRAGMA table_info(tasks)").fetchall()]
    print("Existing columns:", columns)
    if "payload_kg" not in columns:
        cur.execute("ALTER TABLE tasks ADD COLUMN payload_kg REAL DEFAULT 150.0")
    if "payload_size" not in columns:
        cur.execute("ALTER TABLE tasks ADD COLUMN payload_size VARCHAR DEFAULT 'medium'")
    if "urgency" not in columns:
        cur.execute("ALTER TABLE tasks ADD COLUMN urgency VARCHAR DEFAULT 'standard'")
    conn.commit()
    conn.close()
    print("Migration finished successfully.")
