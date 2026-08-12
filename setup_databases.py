#!/usr/bin/env python3
import sqlite3
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")

MASTER_DB_PATH = os.path.join(DATA_DIR, "master.db")
PRESCRIPTION_DB_PATH = os.path.join(DATA_DIR, "prescription.db")
REMINDER_DB_PATH = os.path.join(DATA_DIR, "reminder.db")

def init_databases():
    os.makedirs(DATA_DIR, exist_ok=True)

    # -------------------------------------------------------------
    # 1. Index Master DB (master.db / table: master) with B-Tree & FTS5
    # -------------------------------------------------------------
    master_path = MASTER_DB_PATH if os.path.exists(MASTER_DB_PATH) else os.path.join(BASE_DIR, "master.db")
    if not os.path.exists(master_path):
        print(f"⚠️ Warning: master.db not found at {master_path}.")
    else:
        print(f"⚡ Setting up B-Tree & FTS5 Trigram index on {master_path}...")
        conn = sqlite3.connect(master_path)
        cursor = conn.cursor()
        
        # B-Tree index for fast exact and prefix lookups
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_master_name ON master(name COLLATE NOCASE);")
        
        # FTS5 Trigram virtual table tied to master table
        cursor.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS master_fts USING fts5(
                name,
                content='master',
                content_rowid='rowid',
                tokenize='trigram'
            );
        """)
        # Populate or sync FTS5 index from master table
        cursor.execute("INSERT INTO master_fts(rowid, name) SELECT rowid, name FROM master;")
        conn.commit()
        conn.close()
        print("✅ master.db indexed successfully!")

    # -------------------------------------------------------------
    # 2. Initialize prescription.db (Matching Screenshot 2 schema)
    # -------------------------------------------------------------
    print(f"🛠️ Initializing prescription.db schema...")
    conn = sqlite3.connect(PRESCRIPTION_DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS prescription (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            master_id INTEGER,
            name TEXT NOT NULL,
            medicine_type TEXT,
            is_ongoing INTEGER DEFAULT 1,
            duration_days INTEGER,
            schedule_hours TEXT,     -- e.g. "[9, 13, 20]"
            dose_per_intake INTEGER DEFAULT 1,
            food_timing TEXT,        -- e.g. "[\"after\", \"after\", \"before\"]"
            expiry_date TEXT,
            context TEXT
        );
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_presc_name ON prescription(name COLLATE NOCASE);")
    conn.commit()
    conn.close()
    print("✅ prescription.db initialized!")

    # -------------------------------------------------------------
    # 3. Initialize reminder.db (Matching Screenshot 3 schema)
    # -------------------------------------------------------------
    print(f"🛠️ Initializing reminder.db schema...")
    conn = sqlite3.connect(REMINDER_DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS reminder (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            prescription_id INTEGER,
            timestamp DATETIME NOT NULL,
            message TEXT NOT NULL,
            is_taken INTEGER DEFAULT 0
        );
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_reminder_time ON reminder(timestamp);")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_reminder_presc ON reminder(prescription_id);")
    conn.commit()
    conn.close()
    print("✅ reminder.db initialized!")

if __name__ == "__main__":
    init_databases()