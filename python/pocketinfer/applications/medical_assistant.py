# python/pocketinfer/applications/medical_assistant.py
import json
import logging
from pocketinfer.applications.base import BaseApplication
from pocketinfer.applications.registry import RegisterApplication
from pocketinfer.models.ollama import Ollama
from pocketinfer.models.piper import Piper
from pocketinfer.models.vosk import Vosk
from pocketinfer.models.tts import Tts
from pocketinfer.models.medicine_search import MedicineSearchEngine

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are Shravan, a compassionate, accurate offline AI companion for elderly care.
RULES:
1. When Ground-Truth Medical Context is provided, use ONLY that context to answer.
2. DO NOT invent dosages, uses, or side-effects not present in the context.
3. Keep spoken responses warm, empathetic, clear, and under 2-3 short sentences.
4. Always clearly state safety warnings (e.g. if a medicine is discontinued or habit-forming).
"""

@RegisterApplication({
    "name": "Medical Assistant",
    "description": "Offline elderly companion for medicine identification and dosage verification.",
    "author": "Vyoma AI",
    "version": "1.0.0",
    "models": {
        "ollama": {"model_name": "qwen3-vl:2b"},
        "piper": {"voice_name": "en_US-lessac-high"},
        "vosk": {"model_name": "vosk-model-small-en-us-0.15"},
    },
    "service_dependencies": ["ollama"],
})
class MedicalAssistantApp(BaseApplication):
    def start(self):
        """Initializes hardware interfaces, models, and DB search engine."""
        playback_device = getattr(self.board, "ALSA_PLAYBACK_DEVICE", "default")

        self.piper = Piper(
            voice_name=self.METADATA["models"]["piper"]["voice_name"], 
            audio_device=playback_device
        )
        self.vosk = Vosk(model_name=self.METADATA["models"]["vosk"]["model_name"])
        self.ollama = Ollama(model_name=self.METADATA["models"]["ollama"]["model_name"])
        self.med_search = MedicineSearchEngine()
        self.tts = Tts()
        
        logger.info("Medical Assistant Application initialized successfully.")
        super().start()

    def construct_grounded_prompt(self, user_query, med_record, source_type, extracted_text):
        """Builds strict prompt context depending on match source or fallbacks."""
        
        if source_type == "PRESCRIPTION_DB":
            try:
                hours = json.loads(med_record.get('daily_schedule', '[]'))
                schedule_str = ", ".join([f"{h:02d}:00" for h in hours])
            except Exception:
                schedule_str = med_record.get('daily_schedule', 'As prescribed')

            context = f"""
            [STATUS]: PRESCRIBED ACTIVE MEDICINE FOR PATIENT
            - Drug Name: {med_record.get('medicine_name', 'N/A')}
            - Quantity Per Intake: {med_record.get('quantity_per_intake', '1 Tablet')}
            - Daily Schedule Times: {schedule_str}
            - When To Take: {med_record.get('when_to_take', 'As advised')}
            - Personal Context/Reason: {med_record.get('context', 'General prescription')}
            - Expiry Date: {med_record.get('expiry_date', 'Not recorded')}
            """
        elif med_record and "MASTER" in source_type:
            context = f"""
            [STATUS]: MASTER MEDICINE DATABASE RECORD
            - Drug Name: {med_record.get('name', 'N/A')}
            - Active Composition: {med_record.get('composition', 'N/A')}
            - Approved Uses: {med_record.get('uses', 'N/A')}
            - Side Effects: {med_record.get('side_effects', 'N/A')}
            - Substitutes: {med_record.get('substitutes', 'N/A')}
            - Habit-Forming: {'Yes - Caution Required' if med_record.get('habit_forming') else 'No'}
            - Discontinued: {'WARNING - Brand Discontinued' if med_record.get('is_discontinued') else 'Active'}
            - Description: {med_record.get('description', 'N/A')}
            """
        else:
            context = f"""
            [STATUS]: UNVERIFIED / NOT FOUND IN LOCAL DATABASE
            - Extracted Text from Packaging: "{extracted_text}"
            - Notice: Medicine details were not found in local offline databases. Advise the patient to consult a doctor or ASHA worker for safety.
            """

        prompt = f"""
        VERIFIED GROUND-TRUTH CONTEXT:
        {context}

        PATIENT SPOKEN QUERY: "{user_query}"

        INSTRUCTION: Answer simply and clearly in 2 short sentences for voice playback based strictly on the context provided above:
        """
        return prompt

    def run(self):
        """Main application trigger loop."""
        while not self.stop_event.is_set():
            logger.info("Waiting for button trigger...")
            
            # 1. Wait for hardware trigger button press
            if not self.board.wait_for_trigger(timeout=1.0):
                continue

            logger.info("Trigger activated! Capturing audio & camera image...")
            img_jpeg = self.board.camera_frame_jpg()
            audio_pcm = self.board.record_audio()

            # 2. Transcribe voice query (ASR)
            spoken_query = self.vosk.transcribe(audio_pcm)
            if not spoken_query:
                spoken_query = "What is this tablet used for?"
            logger.info(f"User Query: {spoken_query}")

            # 3. Vision Pass: Extract text string from camera frame
            ocr_prompt = "Extract and output ONLY the primary brand name or drug name printed on this packaging strip. Output nothing else."
            ocr_resp = self.ollama.generate(images=[img_jpeg], prompt=ocr_prompt)
            extracted_text = ocr_resp.response.strip() if hasattr(ocr_resp, 'response') else str(ocr_resp).strip()
            logger.info(f"Extracted packaging text: '{extracted_text}'")

            # 4. 2-Tier Database Lookup using extracted text (< 4ms)
            record, source = self.med_search.resolve(extracted_text)
            logger.info(f"DB Match Source: {source} | Record: {record.get('name') or record.get('medicine_name') if record else 'None'}")

            # --- STEP 5: Pure Text Generation with Guardrails ---
            full_prompt = self.construct_grounded_prompt(spoken_query, record, source, extracted_text)
            resp = self.ollama.generate(prompt=full_prompt, system=SYSTEM_PROMPT)
            llm_response = resp.response.strip() if hasattr(resp, 'response') else str(resp).strip()

            # FALLBACK GUARD: Prevent empty outputs from breaking TTS
            if not llm_response:
                logger.warning("LLM returned an empty response. Triggering grounded fallback response.")
                med_name = (record.get('medicine_name') or record.get('name')) if record else extracted_text
                
                if source == "PRESCRIPTION_DB" and record:
                    llm_response = f"This is {med_name}. Take {record.get('quantity_per_intake', '1 tablet')} {record.get('when_to_take', 'as prescribed')}."
                elif record:
                    llm_response = f"This is {med_name}. It is primarily used for {record.get('uses', 'general medical treatment')}."
                else:
                    llm_response = f"This appears to be {extracted_text or 'an unknown medicine'}. Please consult an ASHA worker or doctor for safe guidance."

            logger.info(f"LLM Output: {llm_response}")

            # --- STEP 6: Robust Text-to-Speech Playback ---
            if hasattr(self, 'piper') and hasattr(self.piper, 'speak'):
                self.piper.speak(llm_response)
            elif hasattr(self, 'tts'):
                # Safe invocation for TTS service backends
                self.tts.infer(text=llm_response, language="en")