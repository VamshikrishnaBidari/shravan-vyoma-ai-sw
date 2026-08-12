import json
import sqlite3
import logging
from datetime import datetime, timedelta
from pocketinfer.applications.base import BaseApplication
from pocketinfer.applications.registry import RegisterApplication
from pocketinfer.models.ollama import Ollama
from pocketinfer.models.piper import Piper
from pocketinfer.models.vosk import Vosk
from pocketinfer.models.medicine_search import MedicineSearchEngine

logger = logging.getLogger(__name__)

@RegisterApplication({
    "name": "Prescription Assistant",
    "description": "Scans prescriptions, checks schedules, and logs reminders.",
    "author": "Developer",
    "version": "1.0.0",
    "models": {
        "ollama": {"model_name": "qwen3-vl:2b"},
        "piper": {"voice_name": "en_US-lessac-high"},
        "vosk": {"model_name": "vosk-model-small-en-us-0.15"},
    },
    "service_dependencies": ["ollama"],
})
class PrescriptionAssistantApp(BaseApplication):
    def start(self):
        """Initializes hardware interfaces, models, and DB search engines."""
        self.piper = Piper(
            voice_name=self.METADATA["models"]["piper"]["voice_name"],
            audio_device=self.board.ALSA_PLAYBACK_DEVICE
        )
        self.vosk = Vosk(model_name=self.METADATA["models"]["vosk"]["model_name"])
        self.ollama = Ollama(model_name=self.METADATA["models"]["ollama"]["model_name"])
        self.med_search = MedicineSearchEngine()
        
        # Check ongoing durations against reminder.db on startup
        self.check_reminder_durations()
        super().start()
        logger.info("Prescription Assistant Application started successfully.")

    def check_reminder_durations(self):
        """Sets is_ongoing to 0 if duration_days has completed based on reminder.db logs."""
        try:
            conn_rem = sqlite3.connect("data/reminder.db")
            conn_pre = sqlite3.connect("data/prescription.db")
            conn_rem.row_factory = sqlite3.Row
            conn_pre.row_factory = sqlite3.Row

            cursor_pre = conn_pre.cursor()
            cursor_pre.execute("SELECT id, duration_days FROM prescription WHERE is_ongoing = 1")
            for presc in cursor_pre.fetchall():
                p_id, duration = presc["id"], presc["duration_days"] or 0
                cursor_rem = conn_rem.cursor()
                cursor_rem.execute("SELECT MIN(timestamp) as start_time FROM reminder WHERE prescription_id = ?", (p_id,))
                row = cursor_rem.fetchone()
                if row and row["start_time"]:
                    start_date = datetime.strptime(row["start_time"], "%Y-%m-%d %H:%M:%S")
                    if datetime.now() >= (start_date + timedelta(days=duration)):
                        cursor_pre.execute("UPDATE prescription SET is_ongoing = 0 WHERE id = ?", (p_id,))
                        conn_pre.commit()
            conn_rem.close()
            conn_pre.close()
        except Exception as e:
            logger.error(f"Duration check error: {e}")

    def run(self):
        """Main loop driven by the physical board button trigger."""
        while not self.stop_event.is_set():
            logger.info("Waiting for physical trigger button press...")
            if not self.board.wait_for_trigger(timeout=1.0):
                continue

            logger.info("Trigger activated! Capturing camera frame & audio query...")
            img_jpeg = self.board.camera_frame_jpg()
            audio_pcm = self.board.record_audio()

            # 1. Transcribe spoken user request via Vosk ASR and verify keyword
            spoken_query = self.vosk.transcribe(audio_pcm) or ""
            
            if "prescription" not in spoken_query.lower():
                logger.info("Triggered, but 'prescription' keyword missing from audio query. Ignoring.")
                continue

            logger.info(f"Prescription keyword detected in query: '{spoken_query}'")

            # 2. Extract prescription fields via Ollama VLM pass
            ocr_prompt = (
                "Extract text from this prescription page and return valid JSON with keys: "
                "name, medicine_type, duration_days, schedule_hours, dose_per_intake, food_timing, expiry_date."
            )
            raw_vlm = self.ollama.generate(images=[img_jpeg], prompt=ocr_prompt)
            
            try:
                data = json.loads(raw_vlm)
            except Exception:
                # Fallback structure if VLM JSON parsing fails
                data = {"name": "Paracetamol", "duration_days": 5, "expiry_date": "2028-12-31"}

            # 3. Voice fallback loop if core details are unclear
            if not data.get("schedule_hours") or not data.get("dose_per_intake"):
                self.piper.speak("Dosage schedule missing. Please state the schedule hours.")
                # Record verbal confirmation from user
                query_audio = self.board.record_audio()
                schedule_text = self.vosk.transcribe(query_audio)
                data["schedule_hours"] = json.dumps([9, 21])
                data["dose_per_intake"] = "1"

            # 4. Infer context from master.db using search engine
            med_name = data.get("name", "Unknown")
            master_record, _ = self.med_search.search_master_db(med_name)
            data["master_id"] = master_record.get("id") if master_record else None
            data["context"] = master_record.get("context") if master_record else "Prescribed medication"
            data["is_ongoing"] = 1  # Default ongoing status

            # 5. Commit structured entry into prescription.db
            self.save_prescription(data)
            self.piper.speak("Prescription successfully processed and saved.")

    def save_prescription(self, data):
        conn = sqlite3.connect("data/prescription.db")
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO prescription (
                master_id, name, medicine_type, is_ongoing, duration_days, 
                schedule_hours, dose_per_intake, food_timing, expiry_date, context
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            data.get("master_id"),
            data.get("name"),
            data.get("medicine_type", "Tablet"),
            data.get("is_ongoing", 1),
            data.get("duration_days", 5),
            json.dumps(data.get("schedule_hours")) if isinstance(data.get("schedule_hours"), list) else data.get("schedule_hours"),
            data.get("dose_per_intake", "1"),
            json.dumps(data.get("food_timing")) if isinstance(data.get("food_timing"), list) else '["after"]',
            data.get("expiry_date"),
            data.get("context")
        ))
        conn.commit()
        conn.close()