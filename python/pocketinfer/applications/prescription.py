import json
import sqlite3
import logging
import threading
import re
import difflib
import os
import time
from datetime import datetime, timedelta
from pocketinfer.applications.base import BaseApplication
from pocketinfer.applications.registry import RegisterApplication
from pocketinfer.models.ollama import Ollama
from pocketinfer.models.piper import Piper
from pocketinfer.models.vosk import Vosk
from pocketinfer.models.medicine_search import MedicineSearchEngine

logger = logging.getLogger(__name__)

# Words the user actually says during dosage/schedule voice input.
# Constraining the small Vosk model to this tight vocabulary fixes missed words.
DOSAGE_GRAMMAR = [
    # numbers
    "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
    "eleven", "twelve", "twenty", "thirty", "forty", "fifty", "half", "quarter",
    # units
    "tablet", "tablets", "capsule", "capsules", "pill", "pills", "dose", "doses",
    "spoon", "spoons", "sachet", "sachets", "drop", "drops", "injection", "inhaler",
    # food / relation
    "before", "after", "with", "without", "food", "meal", "meals", "breakfast",
    "lunch", "dinner", "snack", "empty", "stomach", "water", "milk",
    # schedule
    "morning", "evening", "night", "noon", "midnight", "bedtime", "daily", "hourly",
    "every", "hours", "minutes", "times", "once", "twice", "thrice",
    # duration
    "day", "days", "week", "weeks", "month", "months", "year", "years", "today", "tomorrow",
    # connectors / verbs
    "for", "per", "and", "or", "a", "an", "the", "take", "eat", "drink", "apply", "use",
    "in", "at", "on", "to", "of", "am", "pm"
]

# Resolve project-root data directory so this works no matter where it is imported from
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.abspath(os.path.join(_SCRIPT_DIR, "..", "..", "..", "data"))
os.makedirs(DATA_DIR, exist_ok=True)

MASTER_DB = os.path.join(DATA_DIR, "master.db")
PRESCRIPTION_DB = os.path.join(DATA_DIR, "prescription.db")
REMINDER_DB = os.path.join(DATA_DIR, "reminder.db")


@RegisterApplication({
    "name": "Prescription Assistant",
    "description": "Scans prescriptions and saves medications.",
    "author": "Developer",
    "version": "1.1.0",
    "models": {
        "ollama": {"model_name": "qwen3-vl:2b-instruct"},
        "piper": {"voice_name": "en_US-lessac-high"},
        "vosk": {"model_name": "vosk-model-en-us-0.22-lgraph"},
    },
    "service_dependencies": ["ollama"],
})
class PrescriptionAssistant(BaseApplication):
    def start(self):
        self.piper = Piper(
            voice_name=self.METADATA["models"]["piper"]["voice_name"],
            audio_device=self.board.alsa_playback_device
        )
        self.vosk = Vosk(model_name=self.METADATA["models"]["vosk"]["model_name"])
        self.ollama = Ollama(model_name=self.METADATA["models"]["ollama"]["model_name"])
        self.ollama_text = Ollama(model_name="qwen2.5:1.5b-instruct")
        self.med_search = MedicineSearchEngine()
        self._known_medicine_names = self._load_medicine_names()

        self._audio_lock = threading.Lock()

        super().start()
        logger.info("Prescription Assistant Application started successfully.")

    def _extract_ollama_text(self, raw_response) -> str:
        """Safely extracts text string whether Ollama returns a dict, object, or str."""
        if isinstance(raw_response, dict):
            return str(raw_response.get("response", "")).strip()
        if hasattr(raw_response, "response"):
            return str(raw_response.response).strip()
        return str(raw_response).strip()

    def _load_medicine_names(self):
        try:
            with sqlite3.connect(MASTER_DB) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                for table in ("medicines", "master", "medicine"):
                    try:
                        cursor.execute(f"SELECT name FROM {table}")
                        return [r["name"] for r in cursor.fetchall() if r["name"]]
                    except sqlite3.OperationalError:
                        continue
        except Exception as e:
            logger.error(f"Failed to load medicine names for grammar: {e}")
        return []

    def _record_push_to_talk(self):
        """Records audio using the physical trigger button (Push-to-Talk)."""
        self.board.statusbar("Press Button to Speak")
        
        # Wait for the user to press the button down
        self.board.wait_for_trigger_button_down()
        
        # Stop Piper immediately if the user interrupts the prompt
        try:
            self.piper.stop_playback()
        except Exception:
            pass
            
        self.board.statusbar("Recording... Release to Stop")
        
        # Cleanup any lingering streams safely
        try:
            self.board.audio.stop()
        except Exception:
            pass
            
        # Start recording
        self.board.audio.start()
        
        # Wait for the user to release the button
        self.board.wait_for_trigger_button_up()
        
        # Stop recording and return the data
        self.board.audio.stop()
        self.board.statusbar("Processing Voice...")
        
        try:
            return self.board.audio.to_audio_data()
        except Exception as e:
            logger.error(f"Failed to extract audio data: {e}")
            return None

    def _clean_llm_json(self, raw_text):
        """Non-greedy regex + direct parse fallback."""
        if not raw_text:
            return None
        text = re.sub(r"^```(?:json)?\s*", "", raw_text.strip(), flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)

        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        for match in re.finditer(r"(\{.*?\})", text, re.DOTALL):
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                continue
        return None

    def _sanitize_llm_output(self, parsed):
        """Coerce types so SQLite INSERT and logic never fail."""
        if not isinstance(parsed, dict):
            parsed = {}

        dose = parsed.get("dose_per_intake", "1")
        if not isinstance(dose, str):
            dose = str(dose)

        sched_raw = parsed.get("schedule_hours", [9, 21])
        if isinstance(sched_raw, str):
            try:
                sched_raw = json.loads(sched_raw)
            except Exception:
                sched_raw = [9, 21]
        if not isinstance(sched_raw, list):
            sched_raw = [9, 21]

        sched = []
        for h in sched_raw:
            try:
                hour = int(float(h))
            except (TypeError, ValueError):
                continue
            if 0 <= hour <= 23:
                sched.append(hour)
        if not sched:
            sched = [9, 21]

        food = parsed.get("food_timing", ["after"])
        if isinstance(food, str):
            try:
                food = json.loads(food)
            except Exception:
                food = [food]
        if not isinstance(food, list):
            food = [str(food)]
        food = [str(f) for f in food]

        duration = parsed.get("duration_days", 5)
        try:
            duration = int(duration)
        except (ValueError, TypeError):
            duration = 5

        return {
            "dose_per_intake": dose,
            "schedule_hours": sched,
            "food_timing": food,
            "duration_days": duration
        }

    def _fuzzy_search_master(self, name):
        if not name or not name.strip():
            return None
        record, _ = self.med_search.search_master_db(name)
        if record:
            return record
        try:
            with sqlite3.connect(MASTER_DB) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                all_names = []
                for table in ("medicines", "master", "medicine"):
                    try:
                        cursor.execute(f"SELECT name FROM {table}")
                        all_names = [r["name"] for r in cursor.fetchall() if r["name"]]
                        break
                    except sqlite3.OperationalError:
                        continue
                if not all_names:
                    return None
                matches = difflib.get_close_matches(
                    name.lower(), [n.lower() for n in all_names], n=1, cutoff=0.7
                )
                if matches:
                    corrected = matches[0]
                    original = next((n for n in all_names if n.lower() == corrected), corrected)
                    logger.info(f"Fuzzy match: '{name}' -> '{original}'")
                    record, _ = self.med_search.search_master_db(original)
                    return record
        except Exception as e:
            logger.error(f"Fuzzy search error: {e}")
        return None

    def _get_grammar_candidates(self, seed_text, limit=30):
        """Returns a small list of medicine names from master.db that are plausible
        matches for seed_text — meant to be used as a Vosk grammar, not the full table."""
        if not seed_text or not seed_text.strip():
            return None  # no signal to narrow with — caller should use unconstrained recognition

        try:
            with sqlite3.connect(MASTER_DB) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute("SELECT name FROM master")
                all_names = [r["name"] for r in cursor.fetchall() if r["name"]]
        except Exception as e:
            logger.error(f"Failed to load names for grammar candidates: {e}")
            return None

        if not all_names:
            return None

        matches = difflib.get_close_matches(
            seed_text.lower(), [n.lower() for n in all_names], n=limit, cutoff=0.3
        )
        if not matches:
            return None

        # Grammar words must be lowercase single tokens for Vosk — split multi-word
        # names into individual words and dedupe, dropping anything with digits/units
        words = set()
        for name in matches:
            for token in name.split():
                token = token.strip().lower()
                if token and not any(ch.isdigit() for ch in token) and '%' not in token:
                    words.add(token)

        return list(words) if words else None

    def run(self):
        """Main loop: prescription scanning only."""
        while self.running:
            try:
                logger.info("Waiting for physical trigger button press...")
                self.board.statusbar("Ready - Press Button")
                
                self.board.wait_for_trigger_button_down()
                
                try:
                    self.piper.stop_playback()
                except Exception:
                    pass
                
                self.board.statusbar("Release Button")
                self.board.wait_for_trigger_button_up()

                logger.info("Trigger activated! Capturing camera frame...")
                self.board.statusbar("Capturing Data...")
                
                img_jpeg = self.board.camera_frame_jpg()
                
                logger.info("Intent: ADD_PRESCRIPTION. Entering prescription flow.")
                self.board.statusbar("Processing Image...")

                ocr_prompt = (
                    "Extract and output ONLY the primary brand name or drug name AND its dosage/strength "
                    "(e.g., 'Paracetamol (250mg)'). Format your response strictly as 'Name (Dosage)'. "
                    "Output nothing else."
                )
                try:
                    raw_vlm = self.ollama.generate(images=[img_jpeg], prompt=ocr_prompt)
                    vlm_text = self._extract_ollama_text(raw_vlm)
                except Exception as e:
                    logger.error(f"VLM OCR failed: {e}")
                    vlm_text = ""
                detected_name = vlm_text if vlm_text.upper() != "UNKNOWN" else ""
                logger.info(f"OCR detected medicine name: '{detected_name}'")

                master_record = self._fuzzy_search_master(detected_name)

                # --- FALLBACK 1: MEDICINE NAME ---
                if not master_record:
                    for attempt in range(3):
                        self.piper.start_playback("Medicine not detected clearly. Please hold the button and say the medicine name.")

                        audio_data = self._record_push_to_talk()
                        if not audio_data:
                            self.piper.start_playback("I didn't catch that, let's try again.")
                            time.sleep(2.0)
                            continue

                        # grammar = self._get_grammar_candidates(detected_name)  # seeded from OCR's (possibly weak) text
                        asr_result = self.vosk.recognize(audio_data, grammar=DOSAGE_GRAMMAR)
                        voice_name = asr_result.get("text", "").strip()
                        logger.info(f"Voice fallback medicine name: '{voice_name}' (grammar size: {len(grammar) if grammar else 0})")

                        if not voice_name:
                            self.piper.start_playback("I didn't catch that, let's try again.")
                            time.sleep(2.0)
                            continue

                        master_record = self._fuzzy_search_master(voice_name)
                        if master_record:
                            break

                        self.piper.start_playback("Not a valid medicine. Let's try again.")
                        time.sleep(2.5)

                    if not master_record:
                        self.piper.start_playback("Failed to recognize medicine. Cancelling scan.")
                        logger.warning("Medicine not found in master_db after 3 attempts.")
                        time.sleep(2.5)
                        continue

                med_name = master_record.get("name", "Unknown")
                dosage_text = ""

                # --- FALLBACK 2: DOSAGE ---
                for attempt in range(3):
                    self.piper.start_playback(f"Detected {med_name}. Please hold the button and say the dosage and schedule.")
                    
                    # Use Push-to-Talk!
                    audio_data = self._record_push_to_talk()
                    
                    if not audio_data:
                        self.piper.start_playback("I didn't catch that, let's try again.")
                        time.sleep(2.0)
                        continue

                    asr_result = self.vosk.recognize(audio_data)
                    dosage_text = asr_result.get("text", "").strip()
                    logger.info(f"Voice dosage input: '{dosage_text}'")
                    if dosage_text:
                        break

                if not dosage_text:
                    self.piper.start_playback("Could not understand dosage. Cancelling scan.")
                    time.sleep(2.5)
                    continue

                self.board.statusbar("Parsing Details...")
                parse_prompt = (
                    'Extract prescription details from this spoken instruction and return ONLY a JSON object, nothing else.\n\n'
                    'Keys:\n'
                    '- dose_per_intake: string, e.g. "1", "2"\n'
                    '- schedule_hours: list of ints in 24-hour time, e.g. 9 = 9 AM, 21 = 9 PM\n'
                    '- food_timing: list of strings, e.g. ["after"], ["before"]\n'
                    '- duration_days: int, number of days to take the medicine\n\n'
                    'Example:\n'
                    'Input: "one tablet after breakfast for three days"\n'
                    'Output: {"dose_per_intake": "1", "schedule_hours": [9], "food_timing": ["after"], "duration_days": 3}\n\n'
                    f'Input: "{dosage_text}"\n'
                    'Output:'
                )

                try:
                    parsed_raw = self.ollama_text.generate(
                        prompt=parse_prompt,
                        format="json",
                        options={"temperature": 0.1, "num_predict": 150}
                    )
                    parsed_text = self._extract_ollama_text(parsed_raw)
                    if not parsed_text and hasattr(parsed_raw, 'thinking') and parsed_raw.thinking:
                        logger.warning("Ollama response was empty, falling back to 'thinking' field")
                        parsed_text = parsed_raw.thinking
                    logger.info(f"Raw dosage LLM output: '{parsed_text}'")
                except Exception as e:
                    logger.error(f"LLM dosage parse failed: {e}")
                    parsed_text = ""

                raw_parsed = self._clean_llm_json(parsed_text)
                if raw_parsed is None:
                    logger.warning(f"Failed to parse dosage JSON from: {parsed_text}")

                parsed = self._sanitize_llm_output(raw_parsed)

                data = {
                    "master_id": master_record.get("id"),
                    "name": med_name,
                    "medicine_type": master_record.get("medicine_type", "Tablet"),
                    "is_ongoing": 1,
                    "duration_days": parsed["duration_days"],
                    "schedule_hours": parsed["schedule_hours"],
                    "dose_per_intake": parsed["dose_per_intake"],
                    "food_timing": parsed["food_timing"],
                    "expiry_date": master_record.get("expiry_date", "2028-12-31"),
                    "context": master_record.get("context", "Prescribed medication")
                }

                hours_str = ", ".join(f"{h}:00" for h in parsed["schedule_hours"])
                confirmation = (
                    f"Saving {med_name}, dose {parsed['dose_per_intake']}, "
                    f"at {hours_str}, for {parsed['duration_days']} days."
                )
                
                self.board.statusbar("Saving...")
                self.piper.start_playback(confirmation)

                prescription_id = self.save_prescription(data)
                if prescription_id:
                    self.create_reminders(prescription_id, data)
                
                time.sleep(4.0) 
                
                self.piper.start_playback("Prescription successfully processed and saved.")
                logger.info("Prescription saved. Returning to wait for next trigger.")
                
            except KeyboardInterrupt:
                logger.info("Exit")
                self.board.clear_screen()
                self.running = False
            except Exception as e:
                logger.exception("Error in main application loop: %s", e)
                self.board.statusbar("Error: {}".format(str(e)))
                time.sleep(1)

    def save_prescription(self, data):
        with sqlite3.connect(PRESCRIPTION_DB) as conn:
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
                data.get("duration_days"),
                json.dumps(data.get("schedule_hours")),
                data.get("dose_per_intake"),
                json.dumps(data.get("food_timing")),
                data.get("expiry_date"),
                data.get("context")
            ))
            conn.commit()
            return cursor.lastrowid
    def create_reminders(self, prescription_id, data):
        """One reminder row per (day, scheduled hour) for the prescription's full duration."""
        schedule_hours = data.get("schedule_hours") or [9, 21]
        duration_days = data.get("duration_days") or 5
        dose = data.get("dose_per_intake", "1")
        name = data.get("name", "your medicine")
        med_type = data.get("medicine_type", "Tablet")
        food_timing_list = data.get("food_timing") or ["after"]
        food_timing = food_timing_list[0] if food_timing_list else "after"

        message = f"{dose} {name} {med_type}, {food_timing} food"

        today = datetime.now().replace(minute=0, second=0, microsecond=0)
        rows = []
        for day_offset in range(duration_days):
            day = today + timedelta(days=day_offset)
            for hour in schedule_hours:
                scheduled_time = day.replace(hour=hour, minute=0, second=0, microsecond=0)
                rows.append((prescription_id, scheduled_time.strftime("%Y-%m-%d %H:%M:%S"), message))

        try:
            with sqlite3.connect(REMINDER_DB) as conn:
                conn.executemany("""
                    INSERT INTO reminder (prescription_id, timestamp, message, is_taken)
                    VALUES (?, ?, ?, 0)
                """, rows)
                conn.commit()
            logger.info(f"Created {len(rows)} reminders for prescription id {prescription_id}")
        except Exception as e:
            logger.error(f"Failed to create reminders: {e}")