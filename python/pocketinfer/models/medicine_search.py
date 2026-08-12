import sqlite3
import json
import os
from rapidfuzz import process, fuzz

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../"))
DATA_DIR = os.path.join(BASE_DIR, "data")

class MedicineSearchEngine:
    def __init__(self, master_db_path=None, prescription_db_path=None):
        self.master_db_path = master_db_path or os.path.join(DATA_DIR, "master.db")
        self.prescription_db_path = prescription_db_path or os.path.join(DATA_DIR, "prescription.db")

        # Fallbacks for root directory placement
        if not os.path.exists(self.master_db_path):
            self.master_db_path = os.path.join(BASE_DIR, "master.db")
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
            conn.execute("PRAGMA cache_size = -64000;")  # 64MB RAM cache
            conn.execute("PRAGMA temp_store = MEMORY;")

    def search_prescription_db(self, ocr_text):
        """Tier 1: Checks patient prescriptions matching screenshot 2 schema."""
        cursor = self.prescription_conn.cursor()
        try:
            cursor.execute("SELECT * FROM prescription WHERE is_ongoing = 1")
            prescriptions = cursor.fetchall()
        except sqlite3.OperationalError:
            return None, "NOT_FOUND"

        if not prescriptions:
            return None, "NOT_FOUND"

        choices = {row["id"]: row["name"] for row in prescriptions}
        match = process.extractOne(ocr_text, choices, scorer=fuzz.WRatio, score_cutoff=75)

        if match:
            matched_id = match[2]
            cursor.execute("SELECT * FROM prescription WHERE id = ?", (matched_id,))
            return dict(cursor.fetchone()), "PRESCRIPTION_DB"

        return None, "NOT_FOUND"

    def resolve_substitutes(self, substitute_raw_str, limit=5):
        """Step 4: B-tree Index lookup for substitute medicine names."""
        if not substitute_raw_str:
            return []
            
        try:
            # Parse JSON string array if needed, otherwise parse comma-separated string
            if substitute_raw_str.strip().startswith("["):
                sub_names = json.loads(substitute_raw_str)
            else:
                sub_names = [s.strip().strip('"') for s in substitute_raw_str.split(",")]
        except Exception:
            sub_names = [substitute_raw_str]

        resolved_subs = []
        cursor = self.master_conn.cursor()
        
        for sub_name in sub_names[:limit]:
            clean_sub = sub_name.strip()
            if not clean_sub:
                continue
            # Fast B-Tree exact lookup on master name
            cursor.execute("SELECT name, composition, uses FROM master WHERE name = ? LIMIT 1", (clean_sub,))
            match = cursor.fetchone()
            if match:
                resolved_subs.append(dict(match))
            else:
                resolved_subs.append({"name": clean_sub})

        return resolved_subs

    def search_master_db(self, ocr_text, limit=25):
        """Tier 2: 4-Step Master Database Search Engine."""
        cursor = self.master_conn.cursor()
        clean_text = "".join(e for e in ocr_text if e.isalnum() or e.isspace()).strip()

        if len(clean_text) < 3:
            return None, "NO_MATCH"

        record = None
        source_type = "NO_MATCH"

        # Step 1: B-Tree Exact / Prefix Match on master.name (< 1ms)
        cursor.execute("SELECT rowid, * FROM master WHERE name LIKE ? LIMIT 1", (f"{clean_text}%",))
        exact = cursor.fetchone()
        if exact:
            record = dict(exact)
            source_type = "MASTER_EXACT"
        else:
            # Step 2: FTS5 Trigram Candidate Narrowing
            try:
                cursor.execute(
                    "SELECT rowid, name FROM master_fts WHERE master_fts MATCH ? LIMIT ?", 
                    (f'"{clean_text}"', limit)
                )
                candidates = cursor.fetchall()
            except sqlite3.OperationalError:
                candidates = []

            if not candidates:
                return None, "NO_MATCH"

            # Step 3: RapidFuzz Candidate Ranking on FTS5 Candidates
            cand_map = {row[0]: row[1] for row in candidates}
            best = process.extractOne(clean_text, cand_map, scorer=fuzz.WRatio, score_cutoff=60)

            if not best:
                return None, "NO_MATCH"

            matched_rowid = best[2]
            cursor.execute("SELECT rowid, * FROM master WHERE rowid = ?", (matched_rowid,))
            matched_row = cursor.fetchone()
            if matched_row:
                record = dict(matched_row)
                source_type = "MASTER_FUZZY"

        # Step 4: B-tree lookup for substitute details
        if record and record.get("substitutes"):
            record["substitutes_details"] = self.resolve_substitutes(record["substitutes"])

        return record, source_type

    def resolve(self, ocr_text):
        """Unified 2-Tier Matcher."""
        res, source = self.search_prescription_db(ocr_text)
        if res:
            return res, source
        return self.search_master_db(ocr_text)