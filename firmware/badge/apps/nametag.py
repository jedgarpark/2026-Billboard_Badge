"""Nametag app"""

import lvgl

from apps.base_app import BaseApp
from lib.button import MenuMinus, MenuPlus, UserA, UserB, Boot
from lib.epd import display, display_height
from lib.led_bar import NUM_LEDS, leds
from lib import oled


_WHITE = lvgl.color_hex(0xFFFFFF)
_RED = lvgl.color_hex(0xFF0000)
_BLACK = lvgl.color_hex(0x000000)
_FONT = lvgl.font_montserrat_48
# White name band on hello_i_am_blank.png. Center the plate there.
_NAME_TOP = 221
_NAME_BOTTOM = 557
_NAME_DY = (_NAME_TOP + _NAME_BOTTOM) // 2 - display_height // 2


# LED colours (WS2812s are very bright, so keep values low)
_RED = (1, 0, 0)
_WHITE = (0, 0, 0)


def teal_pink_sequence():
    """Blocks of purple and pink marching along the LED bar."""
    pattern = [_RED, _RED, _WHITE, _WHITE]
    vals = [[0, 0, 0] for _ in range(NUM_LEDS)]
    step = 0
    while True:
        for i in range(NUM_LEDS):
            vals[i] = pattern[(i + step) % len(pattern)]
        yield vals, 250  # ms until the next frame
        step += 1


# Images to show. Launching with A starts on the first, B on the second.
# While running, A steps through them in order.
_IMAGES = [
    "/data/graphics/hello_i_am_blank.png",
    "/data/graphics/hello_i_am_blank2.png",
]


class NametagApp(BaseApp):
    def __init__(self, name: str | None = None) -> None:
        self.name = "Nametag"
        self.sleep_ms = 100
        self.launched = False
        self.launch_button = "A"  # set by the app switcher on launch
        self.menu_hint = "A:go1 B:go2"  # shown on the SAO OLED in the menu
        self.owns_oled = True  # we draw our own OLED status, not the switcher
        self.image_idx = 0
        self.drawing = False  # True while the e-paper refresh is in progress
        self.pending_draw = False  # draw once the current refresh finishes
        # Our own LED state: the menu pauses the LEDs, so leds.sequence_paused
        # doesn't tell us what the user chose.
        self.leds_paused = False
        self.frozen_leds = None  # LED colours at the moment B paused them

    def oled_status(self, drawing=False):
        n = f"{self.image_idx + 1}/{len(_IMAGES)}"
        leds_text = "LEDs: paused" if self.leds_paused else "LEDs: animated"
        oled.show_lines([
            "Nametag",
            "Drawing img " + n if drawing else "Image " + n,
            leds_text,
            "A:img  B:LEDs",
        ])

    def draw_image(self):
        self.drawing = True
        self.oled_status(drawing=True)
        display.draw_png(_IMAGES[self.image_idx])
        # Name is baked into the PNG; no LVGL text plate (it painted a white bar over the art).
        display.update()  # queues the refresh; display.run() does it in the background

    def can_resume(self, button):
        """Called by the app switcher when this app is picked again from the menu.
        True means nothing needs redrawing, so it just resumes."""
        return self.image_idx == (1 if button == "B" else 0)

    def resume(self):
        """Back from the menu without relaunching: restore LEDs and OLED."""
        if self.leds_paused:
            leds.pause()
            # The menu repainted the LEDs; put back the frame B froze.
            if self.frozen_leds:
                leds.set(self.frozen_leds)
                leds.write()
        else:
            leds.unpause()
        self.oled_status(drawing=self.drawing or self.pending_draw)

    def refresh_done(self):
        # The display task clears _framebuffer_ready only after the refresh finishes.
        return display.is_done_updating() and not display._framebuffer_ready.is_set()

    def run(self):
        if self.launched:
            # Clear any press left over from launching so it doesn't act right away.
            UserA.pressed()
            UserB.pressed()
            self.image_idx = 1 if self.launch_button == "B" else 0
            # The menu pauses the LED sequencer; start ours animated.
            self.leds_paused = False
            leds.set_sequence(teal_pink_sequence)
            leds.unpause()
            if self.drawing and not self.refresh_done():
                # Relaunched mid-refresh; a second update now would be lost.
                self.pending_draw = True
                self.oled_status(drawing=True)
            else:
                self.draw_image()
            return
        if self.drawing and self.refresh_done():
            self.drawing = False
            if self.pending_draw:
                self.pending_draw = False
                self.draw_image()
            else:
                self.oled_status()
        if UserA.pressed() and not self.drawing:
            # Next image. Ignored mid-refresh: a second update queued then gets lost.
            # LEDs keep whatever state they're in.
            self.image_idx = (self.image_idx + 1) % len(_IMAGES)
            self.draw_image()
        if UserB.pressed():
            # Freeze / resume the LED animation on its current frame.
            self.leds_paused = not self.leds_paused
            if self.leds_paused:
                leds.pause()
                self.frozen_leds = [tuple(leds[i]) for i in range(NUM_LEDS)]
            else:
                leds.unpause()
            self.oled_status(drawing=self.drawing)

