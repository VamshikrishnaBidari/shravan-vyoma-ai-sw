import logging
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta

from pocketinfer.models.piper import Piper

logger = logging.getLogger(__name__)

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.abspath(os.path.join(_SCRIPT_DIR, "..", "..", "data"))

REMINDER_DB = os.path.join(DATA_DIR, "reminder.db")
PRESCRIPTION_DB = os.path.join(DATA_DIR, "prescription.db")

LEAD_MINUTES = 10           # alert this many minutes before the scheduled time
POLL_INTERVAL_SECONDS = 15  # how often to check for due reminders
ACK_TIMEOUT_SECONDS = 60    # how long an alert waits for the trigger button before auto-dismissing


class ReminderMonitor:
    def __init__(self, board, orchestrator, voice_name="en_US-lessac-medium"):
        self.board = board
        self.orchestrator = orchestrator
        self.logger = logger
        self.piper = Piper(voice_name=voice_name, audio_device=getattr(board, "alsa_playback_device", "default"))
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._running = False

    def start(self):
        self._running = True
        self._thread.start()

    def stop(self):
        self._running = False

    def _get_due_reminder(self):
        cutoff = (datetime.now() + timedelta(minutes=LEAD_MINUTES)).strftime("%Y-%m-%d %H:%M:%S")
        try:
            with sqlite3.connect(REMINDER_DB) as conn_rem, sqlite3.connect(PRESCRIPTION_DB) as conn_pre:
                conn_rem.row_factory = sqlite3.Row
                conn_pre.row_factory = sqlite3.Row

                row = conn_rem.execute("""
                    SELECT id, prescription_id, timestamp, message
                    FROM reminder
                    WHERE is_taken = 0 AND timestamp <= ?
                    ORDER BY timestamp ASC
                    LIMIT 1
                """, (cutoff,)).fetchone()
                if row is None:
                    return None

                presc = conn_pre.execute("""
                    SELECT name, medicine_type, dose_per_intake, is_ongoing
                    FROM prescription WHERE id = ?
                """, (row["prescription_id"],)).fetchone()

                if presc is None or not presc["is_ongoing"]:
                    self._mark_taken(row["id"])
                    return None

                return {
                    "reminder_id": row["id"],
                    "message": row["message"],
                    "name": presc["name"],
                    "medicine_type": presc["medicine_type"] or "Tablet",
                    "dose_per_intake": presc["dose_per_intake"] if "dose_per_intake" in presc.keys() else "1"
                }
        except Exception as e:
            self.logger.error(f"Reminder DB query error: {e}")
            return None

    def _mark_taken(self, reminder_id):
        try:
            with sqlite3.connect(REMINDER_DB) as conn:
                conn.execute("UPDATE reminder SET is_taken = 1 WHERE id = ?", (reminder_id,))
                conn.commit()
        except Exception as e:
            self.logger.error(f"Failed to mark reminder {reminder_id} taken: {e}")

    def _fire_alert(self, reminder):
        self.logger.info(f"Firing reminder: {reminder['message']}")

        med_name = reminder['name']
        dose_val = reminder.get('dose_per_intake', '1')
        med_type = reminder.get('medicine_type', 'Tablet')
        dose_subtitle = f"Take {dose_val} {med_type}(s)"

        self.board.clear_screen()
        self.board.mode_text("Reminder")
        self.board.statusbar("Press button to confirm")

        # Show reminder screen
        self.board.show_reminder_ui(med_name, dose_subtitle)
        self.board.led_animation(1)

        try:
            self.piper.start_playback(f"Reminder! Please take {dose_val} {med_type} of {med_name}.")
        except Exception:
            self.logger.exception("Failed to play reminder audio")

        # Wait ONLY for physical Pin 7 (GP167) trigger button press
        self.board.wait_for_trigger_button_down(timeout=ACK_TIMEOUT_SECONDS)

        # Mark taken in database
        self._mark_taken(reminder["reminder_id"])

        # Display confirmation screen
        self.board.statusbar("Confirmed")
        self.board.show_reminder_ui("Medicine Taken", "Recorded successfully!")

        try:
            self.piper.start_playback("Medicine marked as taken.")
        except Exception:
            pass

        time.sleep(2.5)

        # Clean up and return home
        self.board.hide_reminder_ui()
        self.board.led_animation(0)
        self.orchestrator.show_home_screen()
        
    def _run(self):
        self.logger.info(
            "Reminder monitor started (lead=%d min, poll=%ds)",
            LEAD_MINUTES, POLL_INTERVAL_SECONDS
        )
        while self._running:
            try:
                if not self.orchestrator.is_busy():
                    reminder = self._get_due_reminder()
                    if reminder:
                        self._fire_alert(reminder)
            except Exception:
                self.logger.exception("Error in reminder monitor loop")
            time.sleep(POLL_INTERVAL_SECONDS)