import sys
import time
from datetime import datetime
import Jetson.GPIO as GPIO
import serial

from pocketinfer.applications.base import BaseApplication
from pocketinfer.applications.registry import RegisterApplication

@RegisterApplication({
    "name": "Emergency Signal",
    "description": "Transmits an SOS alert over LoRa UART and monitors the AUX pin.",
    "author": "PocketInfer",
    "version": "1.0.0",
    "models": {}, 
    "default_settings": {
        "serial_port": "/dev/ttyTHS1",
        "baud_rate": 9600,
        "aux_pin": GPIO01, 
        "aux_wait_timeout": 5
    },
    "service_dependencies": []
})
class EmergencySignal(BaseApplication):
    
    def start(self):
        GPIO.setmode(GPIO.BOARD)
        GPIO.setup(self.settings["aux_pin"], GPIO.IN)
        super().start()

    def run(self):
        self.logger.info('Starting Emergency Signal Transmission')
        
        self.board.statusbar("Initializing Radio...")
        self.board.top_text("SENDING SOS")
        self.board.bottom_text("Opening Serial Port...")
        self.board.led_animation(1) 
        
        ser = None
        try:
            try:
                ser = serial.Serial(self.settings["serial_port"], self.settings["baud_rate"], timeout=1)
            except serial.SerialException as e:
                self.logger.error(f"Failed to open serial port: {e}")
                self.board.bottom_text("ERROR: Port failure")
                time.sleep(3)
                self.running = False
                return

            timestamp = datetime.now().strftime('%H:%M:%S')
            message = f'SOS ALERT DIRECT TEST | Time: {timestamp}\n'

            self.board.bottom_text("Transmitting...")
            ser.write(message.encode('utf-8'))

            tx_detected = False
            start_time = time.time()
            
            # Poll AUX pin for 2 seconds to see it drop LOW
            while time.time() - start_time < 2:
                if GPIO.input(self.settings["aux_pin"]) == GPIO.LOW:
                    tx_detected = True
                    break
                time.sleep(0.01)

            # Wait for transmission to finish
            wait_start = time.time()
            while GPIO.input(self.settings["aux_pin"]) == GPIO.LOW:
                if time.time() - wait_start > self.settings["aux_wait_timeout"]:
                    break
                time.sleep(0.01)

            self.board.led_animation(0)

            # --- UI FEEDBACK: WHITE BACKGROUND ---
            if tx_detected:
                self.logger.info("PASSED! Data transmitted.")
                
                # Turn background white
                self.board.set_background_color("white")
                
                # Display the emergency text
                self.board.top_text("!!! EMERGENCY !!!")
                self.board.bottom_text("ALERT SENT SUCCESSFULLY")
                self.board.statusbar("Broadcast Complete")
                
            else:
                self.logger.warning("Data sent, but AUX pin didn't drop.")
                self.board.bottom_text("WARNING: AUX did not trigger")
                self.board.statusbar("Check Wiring")

            # Hold the screen so the user can read the result
            time.sleep(5)
            
            # Revert background to black before closing
            if tx_detected:
                self.board.set_background_color("black")
                self.board.clear_screen()
            
            self.running = False

        except Exception as e:
            self.logger.exception(f"Error during transmission: {e}")
            self.board.bottom_text("SYSTEM ERROR")
            time.sleep(3)
            self.running = False
            
        finally:
            if ser is not None:
                ser.close()