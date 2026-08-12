import sys
import time
from datetime import datetime
import Jetson.GPIO as GPIO
import serial

# Configure Pins & Serial
SERIAL_PORT = '/dev/ttyTHS1'
BAUD_RATE = 9600
AUX_PIN = 29
AUX_WAIT_TIMEOUT = 5  # seconds, safety net so we never hang forever

# Set up Jetson GPIO to read the AUX pin using physical board numbers
GPIO.setmode(GPIO.BOARD)
GPIO.setup(AUX_PIN, GPIO.IN)

ser = None
try:
    try:
        ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1)
        print('====================================')
        print('[1/3] Serial Port (/dev/ttyTHS1) Opened Successfully!')
    except serial.SerialException as e:
        print(f'[ERROR] Failed to open serial port: {e}')
        sys.exit(1)

    # Check AUX Pin baseline (Should be HIGH / True when idle)
    aux_state = GPIO.input(AUX_PIN)
    print(
        f'[2/3] LoRa AUX Pin Status on Pin {AUX_PIN}:'
        f' {"HIGH (Idle)" if aux_state else "LOW (Busy)"}'
    )

    # Create payload message
    timestamp = datetime.now().strftime('%H:%M:%S')
    message = f'SOS ALERT DIRECT TEST | Time: {timestamp}\n'

    # Send message to LoRa module over UART
    print(f'[3/3] Writing payload to serial: "{message.strip()}"')
    ser.write(message.encode('utf-8'))

    # Watch the AUX pin react to the transmission
    print('\n[MONITORING TRANSMISSION...]')
    tx_detected = False

    # Poll AUX pin for 2 seconds to see it drop LOW during transmission
    start_time = time.time()
    while time.time() - start_time < 2:
        if GPIO.input(AUX_PIN) == GPIO.LOW:
            tx_detected = True
            print(
                '  ==> [SUCCESS] AUX Pin dropped LOW! LoRa is actively broadcasting RF'
                ' signal...'
            )
            break
        time.sleep(0.01)

    # Wait for AUX to go back HIGH (transmission done), with a timeout
    wait_start = time.time()
    while GPIO.input(AUX_PIN) == GPIO.LOW:
        if time.time() - wait_start > AUX_WAIT_TIMEOUT:
            print('[WARNING] AUX pin stuck LOW past timeout — check wiring/module state.')
            break
        time.sleep(0.01)

    if tx_detected:
        print(
            '\n[RESULT] PASSED! Data was passed to LoRa and transmitted into the'
            ' air!'
        )
    else:
        print(
            "\n[RESULT] Data sent over UART, but AUX pin didn't drop. Check M0/M1"
            ' ground connections on Pins 14 & 20.'
        )

finally:
    if ser is not None:
        ser.close()
    GPIO.cleanup()