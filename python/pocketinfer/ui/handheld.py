import time
import logging
import uuid
import displayio
import terminalio
from os.path import join
from adafruit_display_text import label, text_box
from adafruit_bitmap_font import bitmap_font
from adafruit_button.button import Button

from pocketinfer.ui import icons
from importlib.resources import files
from multiprocessing import Queue, Pipe
import multiprocessing.connection
from typing import NamedTuple


class HandheldUI:
    ''' This is a graphical UI based on the Adafruit displayIO framework.
    It was originally designed for a circuitpython IO expander board, but has been adapted to run on an Embedded linux platform 
    via the Adafruit blinka compatibility layer. It is designed for a 320x240 pixel touchscreen.
    This class is agnostic to the underlying display and transport, but will be subclassed for specific hardware support.
    '''
    ICON_FONT = bitmap_font.load_font(str(files('pocketinfer.ui').joinpath('forkawesome-16.pcf')))
    HINDI_FONT = bitmap_font.load_font(str(files('pocketinfer.ui').joinpath('NotoSansDevanagari-Regular-12.pcf')))
    TITLE_FONT = bitmap_font.load_font(str(files('pocketinfer.ui').joinpath('Poppins-SemiBold-40.pcf')))
    SUBTITLE_FONT = bitmap_font.load_font(str(files('pocketinfer.ui').joinpath('Poppins-SemiBold-14.pcf')))

    def __init__(self, display, touch, logger=None):
        ''' Load the UI into memory'''
        self.logger = logger or logging.getLogger(__name__)
        self.display = display
        self.touch = touch

        self.button_cbs = {}
        self.buttons = {}

        # Make the display context
        self.layers = displayio.Group()
        self.topbar = displayio.Group()
        self.appui = displayio.Group()
        self.display.root_group = self.layers

        # Create a 1x1 bitmap and stretch it to fill the 320x240 screen
        color_bitmap = displayio.Bitmap(1, 1, 1)
        self.bg_palette = displayio.Palette(1)
        
        # 0xFFFFFF renders as pure black on the inverted screen
        self.bg_palette[0] = 0xFFFFFF 

        bg_sprite = displayio.TileGrid(color_bitmap,
                                    pixel_shader=self.bg_palette,
                                    width=320, height=240)
        # Set text, font, and color
        font = terminalio.FONT
        
        # 0x000000 renders as bright white on the inverted screen
        color = 0x000000      
        
        # 0x666666 renders as a clean soft gray
        color_dim = 0x666666

        # Create the text label
        self.statusbar = label.Label(font, text=" "*52, color=color_dim)
        self.statusbar.anchor_point = (0.5, 1.0)
        self.statusbar.anchored_position = (160, 240)
        self.statusbar.text = "Initializing..."
        self.topbar.append(self.statusbar)

        self.modeval = label.Label(font, text=" "*52, color=color_dim)
        self.modeval.anchor_point = (0.0, 0.0)
        self.modeval.anchored_position = (0, 3)
        self.modeval.text = "Initializing..."
        self.topbar.append(self.modeval)

        def _toggle_setpage(name):
            self.buttons[name].selected = False # leave button deselected
            if self.setpage.hidden:
                # Opening settings: Hide everything else
                self.setpage.hidden = False
                self.appui.hidden = True
                self.homeui.hidden = True 
            else:
                # Closing settings: Return to the Home screen instead of a blank screen
                self.setpage.hidden = True
                self.show_home_ui(True)

        def _toggle_home(name):
            self.buttons[name].selected = False # leave button deselected
            # Always force the Home screen to show (prevents toggling it off by accident)
            self.show_home_ui(True)
            self.setpage.hidden = True

        # 1. The Home Button (White icon on black background, highlight inversion on touch)
        self.topbar.append(self._button('Home', x=320-28, y=0, width=28, height=28, label=icons.home, font=self.ICON_FONT,
                                        label_color=0x000000, fill_color=0xFFFFFF, outline_color=0xFFFFFF,
                                        selected_fill=0x000000, selected_outline=0x000000, selected_label=0xFFFFFF,
                                        cb=_toggle_home))

        # 2. The Settings Button (White icon on black background, highlight inversion on touch)
        self.topbar.append(self._button('Settings', x=320-28*2, y=0, width=28, height=28, label=icons.book, font=self.ICON_FONT,
                                        label_color=0x000000, fill_color=0xFFFFFF, outline_color=0xFFFFFF,
                                        selected_fill=0x000000, selected_outline=0x000000, selected_label=0xFFFFFF,
                                        cb=_toggle_setpage))

        self.toptext = text_box.TextBox(x=0, y=0, width=320, height=100, line_spacing=0.80, font=self.HINDI_FONT, color=color)
        self.toptext.anchor_point = (0.0, 0.0)
        self.toptext.anchored_position = (0, 16)
        self.appui.append(self.toptext)

        self.bottomtext = text_box.TextBox(x=0, y=100, width=320, height=100, line_spacing=0.8, font=self.HINDI_FONT, color=color)
        self.bottomtext.anchor_point = (0.0, 0.0)
        self.bottomtext.anchored_position = (0, 100)
        self.appui.append(self.bottomtext)

        self.setpage = displayio.Group()

        settingslabel = label.Label(font, text=" "*52, color=color)
        settingslabel.anchor_point = (0.5, 0.0)
        settingslabel.anchored_position = (160, 16)
        settingslabel.text = "Settings"
        self.setpage.append(settingslabel)

        input_lang = label.Label(font, text="ASR Lang ", color=color)
        input_lang.anchor_point = (0.0, 0.5)
        input_lang.anchored_position = (0, 48)
        self.setpage.append(input_lang)

        def _deselect_other_asr(name):
            for other in filter(lambda x: x.startswith('ASR ') and x != name, self.buttons.keys()):
                self.buttons[other].selected = False

        self.setpage.append(self._button('ASR En', x=64, y=32, selected=True, cb=_deselect_other_asr))
        self.setpage.append(self._button('ASR Hi', x=64+64, y=32, cb=_deselect_other_asr))
        self.setpage.append(self._button('ASR Ta', x=64+64*2, y=32, cb=_deselect_other_asr))

        output_lang = label.Label(font, text="TTS Lang ", color=color)
        output_lang.anchor_point = (0.0, 0.5)
        output_lang.anchored_position = (0, int(64+32/2))
        self.setpage.append(output_lang)

        def _deselect_other_tts(name):
            for other in filter(lambda x: x.startswith('TTS ') and x != name, self.buttons.keys()):
                self.buttons[other].selected = False

        self.setpage.append(self._button('TTS En', x=64, y=64, selected=True, cb=_deselect_other_tts))
        self.setpage.append(self._button('TTS Hi', x=64+64, y=64, cb=_deselect_other_tts))
        self.setpage.append(self._button('TTS Ta', x=64+64*2, y=64, cb=_deselect_other_tts))

        def _close_setpage(name):
            self.setpage.hidden = True
            self.appui.hidden = False

        self.setpage.append(self._button('Reset', x=64, y=192, cb=_close_setpage))
        self.setpage.append(self._button('Shutdown', x=64*2, y=192, cb=_close_setpage))
        self.setpage.append(self._button('Reboot', x=64*3, y=192, cb=_close_setpage))

        self.setpage.hidden = True

        self.homeui = displayio.Group()
        
        self.homeui.append(self._button(
            'Launch HearTheWorld', x=20, y=30, width=280, height=40,
            label="Hear the World", font=self.HINDI_FONT,
            label_color=0x000000, fill_color=0xDDDDDD, outline_color=0x888888,
            selected_fill=0x000000, selected_outline=0x000000, selected_label=0xFFFFFF
        ))
        
        self.homeui.append(self._button(
            'Launch Medical', x=20, y=80, width=280, height=40,
            label="Medicine Assistant", font=self.HINDI_FONT,
            label_color=0x000000, fill_color=0xDDDDDD, outline_color=0x888888,
            selected_fill=0x000000, selected_outline=0x000000, selected_label=0xFFFFFF
        ))

        # Add Prescription Button
        self.homeui.append(self._button(
            'Add Prescription', x=20, y=130, width=280, height=40,
            label="Add Prescription", font=self.HINDI_FONT,
            label_color=0x000000, fill_color=0xDDDDDD, outline_color=0x888888,
            selected_fill=0x000000, selected_outline=0x000000, selected_label=0xFFFFFF
        ))

        # Add Reminder Button
        self.homeui.append(self._button(
            'Add Reminder', x=20, y=180, width=280, height=40,
            label="Add Reminder", font=self.HINDI_FONT,
            label_color=0x000000, fill_color=0xDDDDDD, outline_color=0x888888,
            selected_fill=0x000000, selected_outline=0x000000, selected_label=0xFFFFFF
        ))
        
        self.homeui.hidden = True

        self.splashui = displayio.Group()

        splash_bitmap = displayio.Bitmap(1, 1, 1)
        self.splash_palette = displayio.Palette(1)
        self.splash_palette[0] = 0xE4E0B4   # inverted navy — renders as navy on this panel
        splash_bg = displayio.TileGrid(splash_bitmap, pixel_shader=self.splash_palette, width=320, height=240)
        self.splashui.append(splash_bg)

        title_label = label.Label(self.TITLE_FONT, text="SHRAVAN", color=0x000000)  # inverted white — renders white
        title_label.anchor_point = (0.5, 0.5)
        title_label.anchored_position = (160, 100)
        self.splashui.append(title_label)

        subtitle_label = label.Label(self.SUBTITLE_FONT, text="Care Beyond Connectivity", color=0x000000)  # inverted white
        subtitle_label.anchor_point = (0.5, 0.5)
        subtitle_label.anchored_position = (160, 140)
        self.splashui.append(subtitle_label)

        self.splashui.hidden = True

        self.reminderui = displayio.Group()

        rem_bg_bitmap = displayio.Bitmap(1, 1, 1)
        rem_bg_palette = displayio.Palette(1)
        rem_bg_palette[0] = 0xFFFFFF 
        rem_bg_sprite = displayio.TileGrid(rem_bg_bitmap, pixel_shader=rem_bg_palette, width=320, height=240)
        self.reminderui.append(rem_bg_sprite)

        # Title Label (Subtitle font, left-aligned)
        self.reminder_title = label.Label(self.SUBTITLE_FONT, text="", color=0x000000) # 0x000000 = white on inverted screen
        self.reminder_title.anchor_point = (0.0, 0.0)
        self.reminder_title.anchored_position = (20, 80)
        self.reminderui.append(self.reminder_title)

        # Subtitle Label (Subtitle font, left-aligned)
        self.reminder_sub = label.Label(self.SUBTITLE_FONT, text="", color=0x000000)
        self.reminder_sub.anchor_point = (0.0, 0.0)
        self.reminder_sub.anchored_position = (20, 120)
        self.reminderui.append(self.reminder_sub)

        self.reminderui.hidden = True

        self.layers.append(bg_sprite)
        self.layers.append(self.topbar)
        self.layers.append(self.appui)
        self.layers.append(self.homeui)
        self.layers.append(self.splashui)
        self.layers.append(self.setpage)
        self.layers.append(self.reminderui)

    def get_button_names(self):
        ''' Return a list of all button names in the UI '''
        return list(self.buttons.keys())
    
    def get_button_status(self):
        ''' Return a dict of button names and their selected status (True/False) '''
        return {name: self.buttons[name].selected for name in self.buttons.keys()}

    def _button(self, name, x, y, label=None, font=None, width=64, height=32,
                label_color=0x000000, fill_color=0xDDDDDD, outline_color=0x888888, 
                selected_fill=0x000000, selected_outline=0x000000, selected_label=0xFFFFFF,
                cb=None, selected=False):
        ''' Create a button and add it to the button list. If a callback is provided, it will be called when the button is pressed.
        Note that the button object returned should be added to the correct Group for it to be displayed'''
        if font is None:
            font = self.HINDI_FONT
        if label is None:
            label = name
        button = Button(
            x=x,
            y=y,
            width=width,
            height=height,
            label=label,
            label_font=font,
            label_color=label_color,
            fill_color=fill_color,
            outline_color=outline_color,
            selected_fill=selected_fill,
            selected_outline=selected_outline,
            selected_label=selected_label
        )
        button.selected = selected
        self.buttons[name] = button
        if cb:
            self.subscribe_to_button(name, cb)
        return button

    def top_text(self, text):
        ''' Set the top text area, which is the upper half of the screen. '''
        self.toptext.text = text
    
    def bottom_text(self, text):
        ''' Set the bottom text area, which is the lower half of the screen. '''
        self.bottomtext.text = text

    def statusbar_text(self, text):
        ''' Set the status bar text, which is the bottom line of the screen. '''
        self.statusbar.text = text
    
    def mode_text(self, text):
        ''' Set the Mode value, in the upper left hand corner. '''
        self.modeval.text = text

    def memory_text(self, text):
        ''' Set the RAM usage value. '''
        self.memval.text = text
    
    def clear_screen(self):
        self.toptext.text = ""
        self.bottomtext.text = ""
        self.statusbar.text = ""
        self.modeval.text = ""

    def set_background_color(self, color: str):
        ''' Changes the background color and adjusts text colors for contrast. '''
        if color == "white":
            # On inverted screen, 0x000000 renders as white
            self.bg_palette[0] = 0x000000
            
            # Text to black (0xFFFFFF on inverted screen)
            self.toptext.color = 0xFFFFFF
            self.bottomtext.color = 0xFFFFFF
            self.statusbar.color = 0xFFFFFF
            self.modeval.color = 0xFFFFFF
        else:
            # Default: 0xFFFFFF renders as black on inverted screen
            self.bg_palette[0] = 0xFFFFFF
            
            # Text to white (0x000000) and soft gray (0x666666)
            self.toptext.color = 0x000000
            self.bottomtext.color = 0x000000
            self.statusbar.color = 0x666666
            self.modeval.color = 0x666666
            
        self.force_refresh()

    def show_splash_ui(self, show: bool):
        ''' Shows the SHRAVAN splash screen with nothing else on screen (topbar hidden too). '''
        self.splashui.hidden = not show
        if show:
            self.topbar.hidden = True
            self.appui.hidden = True
            self.homeui.hidden = True
            self.setpage.hidden = True

    def show_reminder_ui(self, title: str, subtitle: str = ""):
        """Displays full-screen reminder card with left-aligned typography."""
        self.reminder_title.text = title
        self.reminder_sub.text = subtitle
        
        # Hide all other layers including topbar and splash to prevent text overlap
        self.topbar.hidden = True
        self.appui.hidden = True
        self.homeui.hidden = True
        self.splashui.hidden = True
        self.setpage.hidden = True
        self.reminderui.hidden = False

        self.force_refresh()

    def hide_reminder_ui(self):
        """Hides reminder card and restores topbar."""
        self.reminderui.hidden = True
        self.topbar.hidden = False
        self.appui.hidden = False

    def show_home_ui(self, show: bool):
        self.splashui.hidden = True
        self.reminderui.hidden = True
        self.topbar.hidden = False
        self.homeui.hidden = not show
        self.appui.hidden = show

        if show:
            home_buttons = ['Launch HearTheWorld', 'Launch Medical', 'Add Prescription', 'Add Reminder']
            for btn_name in home_buttons:
                if btn_name in self.buttons:
                    self.buttons[btn_name].selected = False

    def force_refresh(self):
        self.display.root_group = None
        self.display.root_group = self.layers
        self.display.refresh()

    def _dispatch_button_cb(self, button_name):
        ''' Call the callback for a button press, if one is registered. '''
        if button_name in self.button_cbs:
            cbs = self.button_cbs[button_name]
            for cb in cbs:
                if callable(cb):
                    cb(button_name)

    def subscribe_to_button(self, button_name, callback):
        ''' Register a callback for a button press. The callback will be called with the button name as an argument. '''
        if button_name not in self.button_cbs:
            self.button_cbs[button_name] = []
        self.button_cbs[button_name].append(callback)

    def unsubscribe_from_button(self, button_name, callback):
        ''' Unregister a callback for a button press. '''
        if button_name in self.button_cbs:
            cbs = self.button_cbs[button_name]
            if callback in cbs:
                cbs.remove(callback)

    def check_buttons(self, x, y):
        ''' Check if a touch event at (x, y) is within any button, and if so, call the callback for that button. '''
        for name in self.buttons:
            butt = self.buttons[name]
            if butt.selected:
                continue
            if butt.contains((x, y)):
                butt.selected = True
                self._dispatch_button_cb(name)

    def check_touch(self):
        import xpt2046_circuitpython as xpt2046
        try:
            if self.touch.is_pressed():
                    args = self.touch.get_coordinates()
                    if args is not None:
                        y, x = args
                        y = 240 - y
                        print(f"Touch at ({x}, {y})")
                        self.check_buttons(x, y)
        except xpt2046.ReadFailedException as e:
            pass

class ILI9341UIConfig(NamedTuple):
    ''' Configuration for the ILI9341 display and touch controller. '''
    reset_pin: str  # The Jetson SOC pin name for the LCD_RST line
    pwm_pin: str  # The Jetson SOC pin name for the LCD_BL line
    cs_pin: str  # The Jetson SOC pin name for the LCD_CS line
    dc_pin: str  # The Jetson SOC pin name for the LCD_DC line
    touch_cs: str  # The Jetson SOC pin name for the TP_CS line
    touch_irq: str  # The Jetson SOC pin name for the TP_IRQ line
    display_baudrate: int = 30000000    # Baud rate when communicating with the display controller over SPI
    touch_baudrate: int =    1000000    # Baud rate when communicating with the touch controller over SPI
    width: int = 320    # Width of the display in pixels
    height: int = 240   # Height of the display in pixels
    rotation: int = 90  # Rotation of the display in degrees

class UIRPCCall:
    ''' This class is used to send a function call from one process to another, and receive the result. '''
    def __init__(self, func_name, *args):
        self.func_name = func_name
        self.args = args
        self.executed = False
        self.exception = None 
        self.result = None
        self._id = uuid.uuid4()
    
    def send(self, rpc_pipe: multiprocessing.connection.Connection):
        rpc_pipe.send(self)
        ret = rpc_pipe.recv()
        if ret._id != self._id:
            raise RuntimeError("Mismatched RPC response ID, multiple RPC callers may be active at the same time, which is not supported.")
        if ret.exception is not None:
            raise ret.exception
        return ret.result
    
    def execute(self, func, rpc_pipe: multiprocessing.connection.Connection):
        if callable(func):
            try:
                self.result = func(*self.args)
            except Exception as e:
                self.exception = e 
            self.executed = True
        else:
            self.result = func
        rpc_pipe.send(self)


class IlI9341HandheldUI(HandheldUI):
    ''' A subclass of HandheldUI that runs on a SPI ILI9341 display with an XPT2046 touch controller, using the Adafruit Blinka compatibility layer for Jetson SOCs. '''
    def __init__(self, ui_config: ILI9341UIConfig, logger=None):
        import digitalio
        import board
        import fourwire
        import adafruit_ili9341
        import xpt2046_circuitpython as xpt2046
        self.logger = logger or logging.getLogger(__name__)

        reset_pin = digitalio.DigitalInOut(board.pin.Pin(ui_config.reset_pin))
        pwm_pin = digitalio.DigitalInOut(board.pin.Pin(ui_config.pwm_pin))
        touch_cs = digitalio.DigitalInOut(board.pin.Pin(ui_config.touch_cs))
        touch_irq = digitalio.DigitalInOut(board.pin.Pin(ui_config.touch_irq))
        tft_cs = board.pin.Pin(ui_config.cs_pin)
        tft_dc = board.pin.Pin(ui_config.dc_pin)

        self.logger.debug('Starting SPI and reset')
        spi = board.SPI()
        reset_pin.direction = digitalio.Direction.OUTPUT
        reset_pin.value = False
        time.sleep(0.005)
        reset_pin.value = True
        time.sleep(0.005)
        pwm_pin.direction = digitalio.Direction.OUTPUT
        pwm_pin.value = True

        self.logger.debug('Initialize bus and display')
        displayio.release_displays()
        display_bus = fourwire.FourWire(spi, command=tft_dc, chip_select=tft_cs, baudrate=ui_config.display_baudrate)
        display = adafruit_ili9341.ILI9341(display_bus, width=ui_config.width, height=ui_config.height, rotation=ui_config.rotation)
        touch = xpt2046.Touch(spi, cs=touch_cs, interrupt=touch_irq, force_baudrate=ui_config.touch_baudrate)

        self.logger.debug('load UI')
        UI = HandheldUI(display, touch)
        super().__init__(display, touch, self.logger)
    
    @classmethod
    def multiprocess_launch(cls, ui_config: ILI9341UIConfig, rpc_pipe: multiprocessing.connection.Connection, button_queue: multiprocessing.Queue):
        def button_cb(name):
            button_queue.put(name)
        UI = cls(ui_config)
        for name in UI.buttons:
            UI.subscribe_to_button(name, button_cb)
        UI.logger.debug('loop')
        while True:
            UI.check_touch()
            if rpc_pipe.poll(0.1):
                call = rpc_pipe.recv()
                func = getattr(UI, call.func_name, None)
                if func is not None:
                    call.execute(func, rpc_pipe)
                else:
                    UI.logger.error('Unknown RPC function: ' + call.func_name)
    
    @classmethod
    def get_remote(cls, rpc_pipe: multiprocessing.connection.Connection):
        class RemoteUI:
            def __init__(self, rpc_pipe):
                self.rpc_pipe = rpc_pipe
            
            def __getattr__(self, name):
                def remote_call(*args):
                    call = UIRPCCall(name, *args)
                    return self.rpc_pipe.send(call)
                return remote_call
        return RemoteUI(rpc_pipe)


if __name__ == "__main__":
    import digitalio
    import board
    import fourwire
    import adafruit_ili9341
    import xpt2046_circuitpython as xpt2046

    reset_pin = digitalio.DigitalInOut(board.pin.Pin("GP36_SPI3_CLK"))
    pwm_pin = digitalio.DigitalInOut(board.D18)
    cs_pin = digitalio.DigitalInOut(board.D8)
    dc_pin = digitalio.DigitalInOut(board.D22)
    tft_cs = board.D8
    tft_dc = board.D22
    touch_cs = board.D7
    touch_irq = board.D25

    BAUDRATE = 240000

    i2c = board.I2C()
    spi = board.SPI()
    pwm_pin.direction = digitalio.Direction.OUTPUT
    pwm_pin.value = True

    displayio.release_displays()
    display_bus = fourwire.FourWire(spi, command=tft_dc, chip_select=tft_cs, baudrate=50000000)
    display = adafruit_ili9341.ILI9341(display_bus, width=320, height=240, rotation=90)
    touch = xpt2046.Touch(spi, cs=digitalio.DigitalInOut(touch_cs), interrupt=digitalio.DigitalInOut(touch_irq), force_baudrate=1000000)

    UI = HandheldUI(display, touch)

    while True:
        UI.check_touch()
        time.sleep(0.1)