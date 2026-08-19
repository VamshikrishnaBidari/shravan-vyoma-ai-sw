# pocketinfer/orchestrator.py
import logging
import threading

from pocketinfer.applications.registry import ApplicationRegistry


class Orchestrator:
    """Owns the currently-running application (if any) and the home-screen state.
    Only one application runs at a time. While one is running, launch requests for
    any other application are ignored ('blocked'). A separate 'go home' trigger stops
    whichever application is currently running and returns to the idle/home state."""

    def __init__(self, board):
        self.logger = logging.getLogger(__name__)
        self.board = board
        self._lock = threading.Lock()
        self.current_app = None
        self.current_app_name = None

    def is_busy(self):
        return self.current_app is not None

    def show_home_screen(self):
        self.board.clear_screen()
        self.board.mode_text("Home")
        self.board.top_text("")
        self.board.bottom_text("")
        self.board.statusbar("Select an application")
        if hasattr(self.board, 'show_home_ui'):
            self.board.show_home_ui(True)

    def launch(self, app_name, settings=None):
        with self._lock:
            if self.current_app is not None:
                self.logger.info(
                    "Busy running '%s', ignoring launch request for '%s'",
                    self.current_app_name, app_name
                )
                return False
            app_cls = ApplicationRegistry.get_application(app_name)
            if app_cls is None:
                self.logger.error("Unknown application: %s", app_name)
                return False
            try:
                app_cls.verify_dependencies()
            except Exception:
                self.logger.exception("Dependency verification failed for '%s'", app_name)
                self.board.statusbar(f"Error starting {app_name}")
                return False

            if hasattr(self.board, 'show_home_ui'):
                self.board.show_home_ui(False)
            self.board.clear_screen()
            self.board.mode_text(f"App: {app_name}")
            self.board.statusbar(f"Starting {app_name}...")

            app = app_cls(self.board, settings=settings)
            app.start()
            self.current_app = app
            self.current_app_name = app_name
            self.logger.info("Launched application '%s'", app_name)
            return True

    def go_home(self):
        with self._lock:
            if self.current_app is None:
                return False
            app = self.current_app
            app_name = self.current_app_name
            self.logger.info("Stopping application '%s'", app_name)
            self.board.statusbar("Closing...")
            app.stop()  # blocks until the app's own thread has fully exited
            self.current_app = None
            self.current_app_name = None
        self.show_home_screen()
        self.logger.info("Returned to home screen")
        return True

    def show_splash_screen(self):
        self.board.clear_screen()
        if hasattr(self.board, 'show_splash_ui'):
            self.board.show_splash_ui(True)

    def go_home(self):
        ''' Always returns to the application-select screen — stopping any running
        application first if one is active. Safe to call from the splash screen,
        the select screen itself, or while an app is running. '''
        with self._lock:
            if self.current_app is not None:
                app = self.current_app
                app_name = self.current_app_name
                self.logger.info("Stopping application '%s'", app_name)
                self.board.statusbar("Closing...")
                app.stop()
                self.current_app = None
                self.current_app_name = None
        self.show_home_screen()
        return True