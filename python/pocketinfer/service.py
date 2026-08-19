#!/usr/bin/env python3
import sys
import argparse
import logging
import time
import threading
import json
from pocketinfer.applications import *
from pocketinfer.applications.registry import ApplicationRegistry
from pocketinfer.boards.base import Board, DummyBoard
from pocketinfer.orchestrator import Orchestrator
from pocketinfer.reminders import ReminderMonitor
from psutil import virtual_memory


def _update_stats(board):
    while True:
        board.memory_text(f"{int(virtual_memory().percent)}%")
        time.sleep(2.0)

def main():
    parser = argparse.ArgumentParser(description="PocketInfer Application Runner")
    parser.add_argument('--log-level', type=str, default='INFO', help='Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)')
    parser.add_argument('--app', type=str, default=None, help='Name of the application to run')
    parser.add_argument('--list-apps', action='store_true', help='List available applications and exit')
    parser.add_argument('--update-app', action='store_true', default=False, help='Install dependencies for the specified application and exit')
    parser.add_argument('--dummy-board', action='store_true', default=False, help='Do not use hardware features - load audio and image from file')
    parser.add_argument('--audio-file', type=str, help='Path to 16kHz 16-bit wav file to use with dummy board')
    parser.add_argument('--image-file', type=str, help='Path to image file to use with dummy board')
    parser.add_argument('--settings-file', default=None, type=str, help='Path to JSON file with application settings to override defaults')
    parser.add_argument('--setting', default=[], type=str, action='append', help='Override a specific application setting (can be used multiple times, e.g. --setting input_language=hi --setting output_language=en)')
    args = parser.parse_args()
    # Temporary code to test application startup
    logging.basicConfig(level=getattr(logging, args.log_level.upper()))

    logging.debug(ApplicationRegistry._classes)
    if args.list_apps:
        print("Available applications:")
        for name in ApplicationRegistry._classes.keys():
            print(f"  {name}")
        sys.exit(0)

    if args.app and args.update_app:
        app_cls = ApplicationRegistry.get_application(args.app)
        if app_cls is None: 
            logging.error("Application not found")
            sys.exit(1)
        app_cls.update_dependencies()
        sys.exit(0)

    settings = {}
    if args.settings_file:
        with open(args.settings_file, 'r') as f:
            file_settings = json.load(f)
        if not isinstance(file_settings, dict):
            logging.error("Settings file must contain a JSON object (dictionary) at the top level")
            sys.exit(1)
        settings.update(file_settings)

    for setting_str in args.setting:
        if '=' not in setting_str:
            logging.error("Invalid setting format: %s. Must be key=value", setting_str)
            sys.exit(1)
        key, value = setting_str.split('=', 1)
        settings[key] = value

    if not args.dummy_board:
        board = Board.get_board()
    else:
        board = DummyBoard(vars(args))

    threading.Thread(target=_update_stats, args=(board,), daemon=True).start()
    board.button_led(False)

    orchestrator = Orchestrator(board)
    reminder_monitor = ReminderMonitor(board, orchestrator)
    reminder_monitor.start()

    APP_BUTTON_MAP = {
        'Launch HearTheWorld': 'HearTheWorld',
        'Launch Medical': 'MedicalAssistant',  
        'Add Prescription': 'PrescriptionAssistant'
    }

    def _on_ui_event(name):
        if orchestrator.is_busy():
            return
        app_name = APP_BUTTON_MAP.get(name)
        if app_name:
            orchestrator.launch(app_name, settings=settings)

    board.subscribe_to_ui(_on_ui_event)

    home_button = getattr(board, 'HOME_BUTTON', None)
    if home_button:
        def _on_home(pressed):
            if pressed:
                orchestrator.go_home()
        board.register_gpio_callback(home_button, _on_home)

    if args.app and args.app != "HearTheWorld":
        orchestrator.launch(args.app, settings=settings)
        if args.dummy_board:
            orchestrator.current_app.running = False
    else:
        orchestrator.show_splash_screen()

    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        if orchestrator.is_busy():
            orchestrator.go_home()
        sys.exit(0)

if __name__ == "__main__":
    main()