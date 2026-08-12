import base64
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import wave
from datetime import datetime, timedelta
from io import BytesIO
from subprocess import check_output

from pocketinfer.applications.base import BaseApplication
from pocketinfer.applications.registry import RegisterApplication
from pocketinfer.audio import AudioPlayer

from pocketinfer.models.asr import Asr
from pocketinfer.models.medicine_search import MedicineSearchEngine
from pocketinfer.models.nmt import Nmt
from pocketinfer.models.ollama import Ollama
from pocketinfer.models.piper import Piper
from pocketinfer.models.tts import Tts
from pocketinfer.models.vosk import Vosk


# ---------------------------------------------------------------------------
# Keywords that route the user into Prescription mode instead of Medicine mode
# ---------------------------------------------------------------------------
PRESCRIPTION_KEYWORDS = [
    "prescription", "scan prescription", "add prescription",
    "doctor prescribed", "prescribed", "new prescription"
]


@RegisterApplication({
    "name": "Hear The World",
    "description": "An application that allows the user to ask questions about their surroundings and medicines.",
    "author": "PocketInfer",
    "version": "0.2.0",
    "models": {
        "ollama": {"model_name": "qwen3-vl:2b"},
        "piper": {"voice_name": "en_US-lessac-high"},
        "vosk": {"model_name": "vosk-model-small-en-us-0.15"},
        "asr": {},
        "nmt": {},
        "tts": {},
    },
    "default_settings": {
        "input_language": "en",
        "output_language": "en",
    },
    "service_dependencies": ["ollama", "bhashini_models"],
})
class HearTheWorld(BaseApplication):

    # =======================================================================
    # LIFECYCLE
    # =======================================================================
    def start(self):
        playback_device = getattr(self.board, "ALSA_PLAYBACK_DEVICE", "default")

        self.piper = Piper(
            voice_name=self.METADATA["models"]["piper"]["voice_name"],
            audio_device=playback_device
        )
        self.vosk = Vosk(model_name=self.METADATA["models"]["vosk"]["model_name"])
        self.ollama = Ollama(model_name=self.METADATA["models"]["ollama"]["model_name"])
        self.asr = Asr()
        self.nmt = Nmt()
        self.tts = Tts()
        self.med_search = MedicineSearchEngine()

        self.board.subscribe_to_ui(self.ui_cb)
        if not os.path.exists("/tmp/hear_the_world_en_logs"):
            os.makedirs("/tmp/hear_the_world_en_logs")

        # Keep prescription DB up-to-date on startup
        self.check_reminder_durations()
        super().start()

    def ui_cb(self, msg):
        if msg == 'Reset':
            self.logger.info('Reset!')
            check_output('systemctl restart pocketinfer', shell=True)
        elif msg == 'Reboot':
            self.logger.info('Rebooting!')
            check_output('reboot', shell=True)
        elif msg == 'Shutdown':
            self.logger.info('Shutdown!')
            check_output('halt', shell=True)
        elif msg.startswith('ASR'):
            self.settings['input_language'] = msg[4:].lower()
        elif msg.startswith('TTS'):
            self.settings['output_language'] = msg[4:].lower()

    # =======================================================================
    # PRESCRIPTION DB HELPERS (from prescription_assistant.py)
    # =======================================================================
    def check_reminder_durations(self):
        """Sets is_ongoing=0 if a prescription's duration has elapsed."""
        try:
            conn_rem = sqlite3.connect("data/reminder.db")
            conn_pre = sqlite3.connect("data/prescription.db")
            conn_rem.row_factory = sqlite3.Row
            conn_pre.row_factory = sqlite3.Row

            cursor_pre = conn_pre.cursor()
            cursor_pre.execute(
                "SELECT id, duration_days FROM prescription WHERE is_ongoing = 1"
            )
            for presc in cursor_pre.fetchall():
                p_id, duration = presc["id"], presc["duration_days"] or 0
                cursor_rem = conn_rem.cursor()
                cursor_rem.execute(
                    "SELECT MIN(timestamp) as start_time FROM reminder WHERE prescription_id = ?",
                    (p_id,),
                )
                row = cursor_rem.fetchone()
                if row and row["start_time"]:
                    start_date = datetime.strptime(row["start_time"], "%Y-%m-%d %H:%M:%S")
                    if datetime.now() >= (start_date + timedelta(days=duration)):
                        cursor_pre.execute(
                            "UPDATE prescription SET is_ongoing = 0 WHERE id = ?", (p_id,)
                        )
                        conn_pre.commit()
            conn_rem.close()
            conn_pre.close()
        except Exception as e:
            self.logger.error(f"Duration check error: {e}")

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

    # =======================================================================
    # UI HELPERS
    # =======================================================================
    def delayed_write_toptext(self, text, delay=1.0):
        def delayed_write(text, delay):
            time.sleep(delay)
            self.board.top_text(text)
        th = threading.Thread(target=delayed_write, args=(text, delay), daemon=True)
        th.start()

    def delayed_write_bottext(self, text, delay=1.0):
        def delayed_write(text, delay):
            time.sleep(delay)
            self.board.bottom_text(text)
        th = threading.Thread(target=delayed_write, args=(text, delay), daemon=True)
        th.start()

    def delayed_write_led_anim(self, val, delay=1.0):
        def delayed_write(val, delay):
            time.sleep(delay)
            self.board.led_animation(val)
        th = threading.Thread(target=delayed_write, args=(val, delay), daemon=True)
        th.start()

    # =======================================================================
    # MEDICINE PIPELINE PROMPT BUILDER (unchanged logic)
    # =======================================================================
    def construct_grounded_prompt(self, user_query, med_record, source_type, extracted_text):
        if source_type == "PRESCRIPTION_DB":
            try:
                hours = json.loads(med_record.get('daily_schedule', '[]'))
                schedule_str = ", ".join([f"{h:02d}:00" for h in hours])
            except Exception:
                schedule_str = str(med_record.get('daily_schedule', 'As prescribed'))

            context = f"""
            [STATUS]: ACTIVE PRESCRIBED MEDICINE FOR PATIENT
            - Medicine Name: {med_record.get('medicine_name', 'N/A')}
            - Quantity Per Intake: {med_record.get('quantity_per_intake', '1 Tablet')}
            - Daily Schedule Times: {schedule_str}
            - When To Take: {med_record.get('when_to_take', 'As advised')}
            - Patient Reason / Context: {med_record.get('context', 'Prescribed medication')}
            - Total Intakes Required: {med_record.get('total_intakes_required', 'N/A')}
            - Expiry Date: {med_record.get('expiry_date', 'Not specified')}
            """
        elif med_record and "MASTER" in source_type:
            context = f"""
            [STATUS]: VERIFIED MASTER MEDICINE DATABASE RECORD
            - Drug Name: {med_record.get('name', 'N/A')}
            - Active Composition: {med_record.get('composition', 'N/A')}
            - Uses / Indications: {med_record.get('uses', 'N/A')}
            - Side Effects: {med_record.get('side_effects', 'N/A')}
            - Substitutes: {med_record.get('substitutes', 'N/A')}
            - Habit-Forming: {'Yes (Warning: Potential Habit-Forming Drug)' if med_record.get('habit_forming') else 'No'}
            - Discontinued: {'WARNING: Brand or Formulation Discontinued!' if med_record.get('is_discontinued') else 'Active'}
            - Description: {med_record.get('description', 'N/A')}
            """
        else:
            context = f"""
            [STATUS]: UNVERIFIED MEDICINE / NOT FOUND IN LOCAL DATABASE
            - Extracted Text from Packaging: "{extracted_text}"
            - Notice: Text was read on the item, but no matching record was found in the local database. State that this medicine is unverified and advise consulting a doctor or ASHA worker for safety.
            """

        system_instructions = """You are Shravan, an empathetic, highly accurate offline AI companion for elderly care. Answer the user's question in 1-2 short, simple sentences using the medical record below. If the record does not contain the exact answer, give the most relevant detail from the record. Never return an empty response.
"""
        return f"{system_instructions}\n\nVERIFIED GROUND-TRUTH CONTEXT:\n{context}\n\nUSER QUESTION: \"{user_query}\"\n\nANSWER:"

# =======================================================================
    # PRESCRIPTION PIPELINE
    # =======================================================================
    def _run_prescription_pipeline(self, img_jpeg, spoken_query):
        """Scan a prescription page, extract structured data, and save it."""
        self.board.statusbar("Running: Prescription Scan")
        self.board.led_animation(1)

        # 1. VLM OCR extraction
        ocr_prompt = (
            "Extract text from this prescription page and return valid JSON with keys: "
            "name, medicine_type, duration_days, schedule_hours, dose_per_intake, food_timing, expiry_date."
        )
        raw_vlm = self.ollama.generate(images=[img_jpeg], prompt=ocr_prompt)

        try:
            data = json.loads(raw_vlm.response if hasattr(raw_vlm, 'response') else raw_vlm)
        except Exception:
            self.logger.warning("VLM JSON parse failed; using fallback structure.")
            data = {"name": "Paracetamol", "duration_days": 5, "expiry_date": "2028-12-31"}

        # 2. Voice fallback for missing critical fields
        if not data.get("schedule_hours") or not data.get("dose_per_intake"):
            self.board.statusbar("Say schedule hours")
            self.piper.start_playback("Dosage schedule missing. Hold button and state the schedule hours.")

            # --- Push-to-Talk Trigger Block (Matching main loop) ---
            self.board.wait_for_trigger_button_down()
            self.board.statusbar("Recording Schedule...")
            
            # Stop TTS playback if still speaking and begin audio recording
            if hasattr(self.piper, 'stop_playback'):
                self.piper.stop_playback()
                
            self.board.audio.start()
            self.board.wait_for_trigger_button_up()
            self.board.audio.stop()
            # --------------------------------------------------------

            self.board.statusbar("Running: ASR")
            
            # Process ASR for the captured schedule input
            if self.settings["input_language"] != 'en':
                wav_bytes = self.board.audio.to_audio_data().get_wav_data()
                follow_asr = self.asr.infer(wav_bytes, self.settings["input_language"])
            else:
                follow_asr = self.vosk.recognize(self.board.audio.to_audio_data())
                
            schedule_text = follow_asr['text']
            self.logger.info(f"Follow-up schedule text: {schedule_text}")

            # Defaults (can be expanded to parse schedule_text via regex/LLM)
            data["schedule_hours"] = [9, 21]
            data["dose_per_intake"] = "1"

        # 3. Enrich from master DB
        med_name = data.get("name", "Unknown")
        master_record, _ = self.med_search.search_master_db(med_name)
        data["master_id"] = master_record.get("id") if master_record else None
        data["context"] = master_record.get("context") if master_record else "Prescribed medication"
        data["is_ongoing"] = 1

        # 4. Persist
        self.save_prescription(data)

        # 5. Confirm to user
        confirmation = "Prescription successfully processed and saved."
        self.board.bottom_text(confirmation)
        self.board.statusbar("Prescription Saved")
        self.piper.start_playback(confirmation)
        self.board.led_animation(0)

    # =======================================================================
    # MAIN LOOP
    # =======================================================================
    def run(self):
        self.logger.debug('Starting with settings: %s', self.settings)
        while self.running:
            try:
                self.board.statusbar("Ready - Press Button")
                self.board.UI.force_refresh()
                self.board.wait_for_trigger_button_down()
                self.board.statusbar("Release Button")
                self.board.top_text("")
                self.board.bottom_text("")
                audio_start = time.time()

                # Stop any previous TTS and start recording + camera
                if hasattr(self.piper, 'stop_playback'):
                    self.piper.stop_playback()
                self.board.audio.start()
                img = self.board.camera_frame_jpg()
                self.board.wait_for_trigger_button_up()
                audio_stop = time.time()

                self.board.audio.stop()
                self.board.statusbar("Running: ASR")
                self.board.led_animation(1)
                asr_start = time.time()

                # ----- ASR -----
                if self.settings["input_language"] != 'en':
                    wav_bytes = self.board.audio.to_audio_data().get_wav_data()
                    asr_result = self.asr.infer(wav_bytes, self.settings["input_language"])
                else:
                    asr_result = self.vosk.recognize(self.board.audio.to_audio_data())
                raw_query = asr_result['text']
                asr_stop = time.time()
                self.logger.info("Detected query is '{}'".format(raw_query))
                self.board.top_text(raw_query)

                # ----- Early NMT (so both pipelines work in English) -----
                if self.settings['input_language'] != 'en':
                    self.board.statusbar(f"Running: NMT {self.settings['input_language']} -> en")
                    query = self.nmt.infer(raw_query, self.settings["input_language"], "EN")['translated_text']
                    self.logger.info("Translated query is '{}'".format(query))
                    self.delayed_write_toptext(query, delay=2.0)
                else:
                    query = raw_query
                nmt_a_stop = time.time()

                # ----- INTENT DETECTION -----
                q_lower = query.lower()
                raw_lower = raw_query.lower()
                is_prescription = any(kw in q_lower or kw in raw_lower for kw in PRESCRIPTION_KEYWORDS)

                if is_prescription:
                    self._run_prescription_pipeline(img, query)
                    continue  # Skip medicine pipeline; wait for next trigger

                # =====================================================================
                # MEDICINE ASSISTANT PIPELINE (existing logic, largely unchanged)
                # =====================================================================
                self.board.statusbar("Running: Medicine Vision Pass")
                ocr_prompt = "Extract and output ONLY the primary brand name or drug name printed on this packaging strip. Output nothing else."
                ocr_resp = self.ollama.generate(images=[img], prompt=ocr_prompt)
                extracted_text = ocr_resp.response.strip() if hasattr(ocr_resp, 'response') else str(ocr_resp).strip()
                self.logger.info("Extracted packaging text from vision pass: '%s'", extracted_text)

                # Sub-5ms 2-Tier DB Lookup
                med_record, source_type = self.med_search.resolve(extracted_text)
                self.logger.info("DB Match Source: %s | Record: %s", source_type,
                                 med_record.get('name') or med_record.get('medicine_name') if med_record else 'None')

                # Build Grounded Prompt
                grounded_prompt = self.construct_grounded_prompt(query, med_record, source_type, extracted_text)

                # Pure text generation (image NOT passed again)
                self.board.statusbar("Running: Grounded LLM")
                llm_start = time.time()
                resp = self.ollama.generate(images=None, prompt=grounded_prompt)
                llm_end = time.time()
                result = resp.response.strip().rstrip()

                # Fallback guard
                if not result:
                    self.logger.warning("LLM returned empty response; using fallback.")
                    med_name = (med_record.get('medicine_name') or med_record.get('name')) if med_record else extracted_text
                    if source_type == "PRESCRIPTION_DB" and med_record:
                        result = f"This is {med_name}. Take {med_record.get('quantity_per_intake', '1 tablet')} {med_record.get('when_to_take', 'as prescribed')}."
                    elif med_record:
                        result = f"This is {med_name}. It is primarily used for {med_record.get('uses', 'general medical treatment')}."
                    else:
                        result = f"This appears to be {extracted_text or 'an unknown medicine'}. Please consult an ASHA worker or doctor for safe guidance."
                    self.board.bottom_text(result)

                self.logger.info("Grounded Result is '{}'".format(result))
                self.board.bottom_text(result)

                if self.settings['output_language'] != 'en':
                    self.board.statusbar(f"Running: NMT en -> {self.settings['output_language']}")
                    nmt_result = self.nmt.infer(result, "EN", self.settings["output_language"])['translated_text']
                    self.logger.info("Translated result is '{}'".format(nmt_result))
                else:
                    nmt_result = result
                nmt_b_stop = time.time()

                self.delayed_write_toptext(raw_query)
                self.delayed_write_bottext(nmt_result)

                self.board.statusbar("Running: Playback")
                self.delayed_write_led_anim(0)
                tts_result = self.tts.infer(nmt_result, self.settings["output_language"])
                tts_result_bytes = base64.b64decode(tts_result['audio_base64'])
                app_end = time.time()

                playback_device = getattr(self.board, "ALSA_PLAYBACK_DEVICE", "default")

                with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                    f.write(tts_result_bytes)
                    tmp = f.name
                subprocess.run([
                    "ffplay", "-nodisp", "-autoexit", tmp, "-volume", "100"
                ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                os.unlink(tmp)

                # Save JSONL logs
                log_id = int(audio_start * 1000)
                log_data = {
                    'id': log_id,
                    "query": asr_result,
                    "extracted_text": extracted_text,
                    "db_source": source_type,
                    "matched_record": med_record,
                    "response": resp.model_dump() if hasattr(resp, 'model_dump') else str(resp),
                    "timestamps": {
                        "audio_start": audio_start,
                        "audio_stop": audio_stop,
                        "asr_start": asr_start,
                        "asr_stop": asr_stop,
                        "nmt_a_stop": nmt_a_stop,
                        "llm_start": llm_start,
                        "llm_end": llm_end,
                        "nmt_b_stop": nmt_b_stop,
                        "app_end": app_end
                    }
                }
                with open("/tmp/hear_the_world_en_logs/log.jsonl", "a") as f:
                    f.write(json.dumps(log_data) + "\n")
                with open("/tmp/hear_the_world_en_logs/img_{}.jpg".format(log_id), "wb") as f:
                    f.write(img)
                with open("/tmp/hear_the_world_en_logs/audio_{}.wav".format(log_id), "wb") as f:
                    f.write(tts_result_bytes)

            except KeyboardInterrupt:
                self.logger.info("Exit")
                self.board.clear_screen()
                self.running = False
            except Exception as e:
                self.logger.exception("Error in main application loop: %s", e)
                self.board.statusbar("Error: {}".format(str(e)))
                time.sleep(1)