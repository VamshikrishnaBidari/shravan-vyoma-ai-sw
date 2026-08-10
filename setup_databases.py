#!/usr/bin/env python3
import sqlite3
import os
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")

MEDICINES_DB_PATH = os.path.join(DATA_DIR, "medicines.db")
PRESCRIPTION_DB_PATH = os.path.join(DATA_DIR, "prescription.db")
REMINDER_DB_PATH = os.path.join(DATA_DIR, "reminder.db")

def init_databases():
    os.makedirs(DATA_DIR, exist_ok=True)

    # -------------------------------------------------------------
    # 1. Index 560MB Master medicines.db (FTS5 Trigram)
    # -------------------------------------------------------------
    master_path = MEDICINES_DB_PATH if os.path.exists(MEDICINES_DB_PATH) else os.path.join(BASE_DIR, "medicines.db")
    if not os.path.exists(master_path):
        print(f"⚠️ Warning: Master medicines.db not found at {master_path}. Place your 560MB database there.")
    else:
        print(f"⚡ Setting up FTS5 Trigram index on {master_path}...")
        conn = sqlite3.connect(master_path)
        cursor = conn.cursor()
        
        # B-Tree index for exact lookups
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_medicine_name ON medicines(name COLLATE NOCASE);")
        
        # FTS5 Trigram table
        cursor.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS medicines_fts USING fts5(
                name,
                content='medicines',
                content_rowid='id',
                tokenize='trigram'
            );
        """)
        # Populate FTS5 index
        cursor.execute("INSERT INTO medicines_fts(rowid, name) SELECT id, name FROM medicines;")
        conn.commit()
        conn.close()
        print("✅ Master medicines.db indexed!")

    # -------------------------------------------------------------
    # 2. Initialize empty prescription.db
    # -------------------------------------------------------------
    print(f"🛠️ Initializing empty prescription.db schema...")
    conn = sqlite3.connect(PRESCRIPTION_DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS patient_prescriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            medicine_id INTEGER,
            medicine_name TEXT NOT NULL,
            total_intakes_required INTEGER,
            expiry_date TEXT,
            context TEXT,
            daily_schedule TEXT NOT NULL,      -- Stored as JSON array, e.g. "[8, 20]"
            quantity_per_intake TEXT NOT NULL, -- e.g. "1 Tablet"
            is_active INTEGER DEFAULT 1,       -- 1 = Active, 0 = Inactive
            when_to_take TEXT                  -- e.g. "After food"
        );
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_presc_name ON patient_prescriptions(medicine_name COLLATE NOCASE);")
    conn.commit()
    conn.close()
    print("✅ prescription.db initialized!")

    # -------------------------------------------------------------
    # 3. Initialize empty standalone reminder.db
    # -------------------------------------------------------------
    print(f"🛠️ Initializing empty reminder.db schema...")
    conn = sqlite3.connect(REMINDER_DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,          -- e.g. "2026-08-02 09:00:00"
            message TEXT NOT NULL,
            is_taken INTEGER DEFAULT 0        -- 0 = pending, 1 = taken, -1 = missed
        );
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_reminder_time ON reminders(timestamp);")
    conn.commit()
    conn.close()
    print("✅ reminder.db initialized!")

if __name__ == "__main__":
    init_databases()