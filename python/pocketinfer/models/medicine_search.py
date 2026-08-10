# python/pocketinfer/models/medicine_search.py
import sqlite3
import json
import os
import re
from rapidfuzz import process, fuzz

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../"))
DATA_DIR = os.path.join(BASE_DIR, "data")

class MedicineSearchEngine:
    def __init__(self, master_db_path=None, prescription_db_path=None):
        self.master_db_path = master_db_path or os.path.join(DATA_DIR, "medicines.db")
        self.prescription_db_path = prescription_db_path or os.path.join(DATA_DIR, "prescription.db")

        # Fallbacks for root directory placement
        if not os.path.exists(self.master_db_path):
            self.master_db_path = os.path.join(BASE_DIR, "medicines.db")
        if not os.path.exists(self.prescription_db_path):
            self.prescription_db_path = os.path.join(BASE_DIR, "prescription.db")

        self.master_conn = sqlite3.connect(self.master_db_path, check_same_thread=False)
        self.prescription_conn = sqlite3.connect(self.prescription_db_path, check_same_thread=False)

        self.master_conn.row_factory = sqlite3.Row
        self.prescription_conn.row_factory = sqlite3.Row

        # Performance Tuning PRAGMAs for Jetson SD/NVMe storage
        for conn in (self.master_conn, self.prescription_conn):
            conn.execute("PRAGMA journal_mode = WAL;")
            conn.execute("PRAGMA synchronous = NORMAL;")
            conn.execute("PRAGMA temp_store = MEMORY;")
            conn.execute("PRAGMA cache_size = -64000;") # 64MB RAM Cache

    def search_prescription(self, ocr_text):
        """Fast prescription DB match."""
        if not ocr_text:
            return None
        cursor = self.prescription_conn.cursor()
        spaced_text = re.sub(r'[^a-zA-Z0-9]+', ' ', ocr_text).strip()
        if not spaced_text:
            return None

        # Search by exact or prefix name match
        cursor.execute(
            "SELECT * FROM patient_prescriptions WHERE medicine_name LIKE ? AND is_active = 1 LIMIT 1",
            (f"{spaced_text}%",)
        )
        res = cursor.fetchone()
        return dict(res) if res else None

    def search_master(self, ocr_text, limit=50):
        """High-speed multi-stage master database lookup engine (< 15ms)."""
        if not ocr_text:
            return None, "NO_MATCH"

        cursor = self.master_conn.cursor()

        # 1. Normalize OCR text: Replace non-alphanumeric chars with space
        spaced_text = re.sub(r'[^a-zA-Z0-9]+', ' ', ocr_text).strip()
        collapsed_text = re.sub(r'[^a-zA-Z0-9]+', '', ocr_text).strip()

        if len(spaced_text) < 2 and len(collapsed_text) < 2:
            return None, "NO_MATCH"

        # Step A: B-Tree Exact / Prefix Match (< 1ms)
        for term in (spaced_text, collapsed_text):
            if term:
                cursor.execute("SELECT * FROM medicines WHERE name LIKE ? LIMIT 1", (f"{term}%",))
                exact = cursor.fetchone()
                if exact:
                    return dict(exact), "MASTER_EXACT"

        # Step B: FTS5 Trigram Candidate Broadening
        tokens = [t for t in spaced_text.split() if len(t) >= 2]
        candidates = []

        if tokens:
            # Query 1: Wildcard match all tokens (e.g., 'Dolo* 650*')
            fts_query_all = " ".join(f"{t}*" for t in tokens)
            try:
                cursor.execute(
                    "SELECT rowid, name FROM medicines_fts WHERE medicines_fts MATCH ? LIMIT ?",
                    (fts_query_all, limit)
                )
                candidates = cursor.fetchall()
            except sqlite3.OperationalError:
                candidates = []

            # Query 2: Fallback to primary alphabetical token if multi-word query produced 0 rows
            if not candidates:
                alpha_tokens = [t for t in tokens if t.isalpha() and len(t) >= 3]
                if alpha_tokens:
                    fts_query_primary = f"{alpha_tokens[0]}*"
                    try:
                        cursor.execute(
                            "SELECT rowid, name FROM medicines_fts WHERE medicines_fts MATCH ? LIMIT ?",
                            (fts_query_primary, limit)
                        )
                        candidates = cursor.fetchall()
                    except sqlite3.OperationalError:
                        candidates = []

        if not candidates:
            return None, "NO_MATCH"

        # Step C: RapidFuzz Candidate Ranking
        cand_map = {row[0]: row[1] for row in candidates}
        best = process.extractOne(spaced_text, cand_map, scorer=fuzz.WRatio, score_cutoff=55)

        if not best:
            return None, "NO_MATCH"

        # Step D: Fetch Full Row
        matched_id = best[2]
        cursor.execute("SELECT * FROM medicines WHERE id = ?", (matched_id,))
        matched_row = cursor.fetchone()
        return (dict(matched_row), "MASTER_FUZZY") if matched_row else (None, "NO_MATCH")

    def resolve(self, ocr_text):
        """Unified 2-Tier Matcher: Check active prescriptions first, then Master DB."""
        if not ocr_text:
            return None, "NO_MATCH"

        # Tier 1: Patient Prescription Database
        presc_record = self.search_prescription(ocr_text)
        if presc_record:
            return presc_record, "PRESCRIPTION_DB"

        # Tier 2: Master Medicines Database
        master_record, source = self.search_master(ocr_text)
        if master_record:
            return master_record, source

        return None, "NO_MATCH"