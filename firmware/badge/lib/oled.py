"""Optional SSD1306 OLED on an SAO port (e.g. Adafruit 0.91" 128x32, I2C 0x3C).

If no OLED answers on the I2C bus, every function here is a no-op, so the
badge works the same with or without one plugged in.

All SAO ports share one I2C bus, so I2C alone can't tell which port the OLED
is in. To pick a mode by port, the OLED's adapter ties one of the SAO GPIO
pins to GND; at startup we look for which port has a GPIO pulled low:

    SAO1 -> "nametag":  static "HELLO I AM" / name
    SAO2 -> "preview":  previews the e-paper (menu, app status)
    SAO3 -> "scroller": animated scrolling name

No GPIO jumper (or ambiguous) -> "preview". The port is only checked at
startup, so power the badge off before moving the OLED.
"""

import framebuf
import time

try:
    import asyncio as aio
    import machine
    from lib import sao
    from lib.sao import i2c
except Exception as exc:  # never let the optional OLED stop the badge booting
    print("OLED disabled, couldn't load lib.sao:", exc)
    i2c = None

OLED_ADDR = 0x3C
WIDTH = 128
HEIGHT = 32

# Personalize here.
NAME = "JOHN PARK"
HELLO = "HELLO I AM"
SCROLL_TEXT = "HELLO I AM JOHN PARK  *  "

PORT_MODES = {1: "nametag", 2: "preview", 3: "scroller"}
DEFAULT_MODE = "preview"


class Oled(framebuf.FrameBuffer):
    """Minimal SSD1306 driver: draw with the FrameBuffer methods, then show()."""

    def __init__(self, bus, addr=OLED_ADDR, width=WIDTH, height=HEIGHT):
        self.bus = bus
        self.addr = addr
        self.width = width
        self.height = height
        self.pages = height // 8
        self.buf = bytearray(width * self.pages)
        super().__init__(self.buf, width, height, framebuf.MONO_VLSB)
        self._cmd_buf = bytearray(2)
        self._glyph = framebuf.FrameBuffer(bytearray(8), 8, 8, framebuf.MONO_VLSB)
        self._big_glyphs = {}  # char -> 16x16 FrameBuffer, for fast blitting
        for cmd in (
            0xAE,                    # display off
            0x20, 0x00,              # horizontal addressing mode
            0x40,                    # start line 0
            0xA1,                    # column 127 -> SEG0
            0xA8, height - 1,        # multiplex ratio
            0xC8,                    # scan COM from bottom
            0xD3, 0x00,              # no display offset
            0xDA, 0x02 if width > 2 * height else 0x12,  # COM pin config
            0xD5, 0x80,              # clock divide
            0xD9, 0xF1,              # precharge (internal VCC)
            0xDB, 0x30,              # VCOMH level
            0x81, 0xFF,              # contrast
            0xA4,                    # follow RAM
            0xA6,                    # normal (not inverted)
            0x8D, 0x14,              # charge pump on
            0xAF,                    # display on
        ):
            self._cmd(cmd)
        self.fill(0)
        self.show()

    def _cmd(self, cmd):
        self._cmd_buf[0] = 0x80
        self._cmd_buf[1] = cmd
        self.bus.writeto(self.addr, self._cmd_buf)

    def show(self):
        for cmd in (0x21, 0, self.width - 1, 0x22, 0, self.pages - 1):
            self._cmd(cmd)
        self.bus.writevto(self.addr, (b"\x40", self.buf))

    def big_glyph(self, ch):
        """A 16x16 (2x scaled) glyph, rendered once and cached."""
        g = self._big_glyphs.get(ch)
        if g is None:
            g = framebuf.FrameBuffer(bytearray(32), 16, 16, framebuf.MONO_VLSB)
            small = self._glyph
            small.fill(0)
            small.text(ch, 0, 0, 1)
            for gy in range(8):
                for gx in range(8):
                    if small.pixel(gx, gy):
                        g.fill_rect(gx * 2, gy * 2, 2, 2, 1)
            self._big_glyphs[ch] = g
        return g

    def big_text(self, text, x, y, advance=16):
        """Draw 2x text. advance < 16 squeezes letters to fit more."""
        for ch in text:
            if ch != " ":
                self.blit(self.big_glyph(ch), x, y, 0)  # 0 = transparent
            x += advance


_oled = None  # None = not checked yet, False = not present
mode = DEFAULT_MODE


def detect_port():
    """Which SAO port has a GPIO tied to GND? Returns 1/2/3, or None."""
    ports = {
        1: (sao.SAO1_GPIO1, sao.SAO1_GPIO2),
        2: (sao.SAO2_GPIO1, sao.SAO2_GPIO2),
        3: (sao.SAO3_GPIO1, sao.SAO3_GPIO2),
    }
    found = []
    for port, pins in ports.items():
        for p in pins:
            pin = machine.Pin(p, machine.Pin.IN, machine.Pin.PULL_UP)
            time.sleep_us(100)  # let the pull-up settle
            low = pin.value() == 0
            machine.Pin(p, machine.Pin.IN)  # drop the pull-up again
            if low:
                found.append(port)
                break
    return found[0] if len(found) == 1 else None


def get():
    """Return the Oled, or None if there isn't one. Checks once, at first use."""
    global _oled, mode
    if _oled is None:
        if i2c is None:
            _oled = False
            return None
        try:
            _oled = Oled(i2c) if OLED_ADDR in i2c.scan() else False
        except OSError:
            _oled = False
        if not _oled:
            print("No OLED on SAO I2C bus")
            return None
        port = detect_port()
        mode = PORT_MODES.get(port, DEFAULT_MODE)
        print(f"OLED found, SAO port {port}, mode {mode}")
        if mode == "nametag":
            _draw_nametag(_oled)
        elif mode == "scroller":
            aio.create_task(_scroller(_oled))
    return _oled or None


def _preview():
    """The OLED, but only in preview mode (other modes own the screen)."""
    o = get()
    return o if mode == "preview" else None


# ---- SAO1: static name tag -------------------------------------------------

def _draw_nametag(o):
    try:
        o.fill(0)
        o.fill_rect(0, 0, o.width, 11, 1)  # "red band" as a white bar
        o.text(HELLO, (o.width - len(HELLO) * 8) // 2, 2, 0)
        advance = min(16, o.width // max(1, len(NAME)))
        x = (o.width - (len(NAME) - 1) * advance - 16) // 2
        o.big_text(NAME, max(0, x), 14, advance)
        o.show()
    except OSError:
        pass


# ---- SAO3: wavy scrolling text ---------------------------------------------

# One period of a sine wave, 0..12 px, so letters bob within the 32px screen.
_WAVE = [int(6 + 6 * v) for v in (
    0.0, 0.38, 0.71, 0.92, 1.0, 0.92, 0.71, 0.38,
    0.0, -0.38, -0.71, -0.92, -1.0, -0.92, -0.71, -0.38,
)]


def scroller_frame(o, step):
    """Draw one frame of the scroller. step = pixels scrolled so far."""
    text = SCROLL_TEXT
    o.fill(0)
    first = step // 16
    for i in range(o.width // 16 + 2):
        idx = first + i
        ch = text[idx % len(text)]
        x = idx * 16 - step
        if ch != " ":
            y = 2 + _WAVE[(idx + step // 4) % len(_WAVE)]
            o.blit(o.big_glyph(ch), x, y, 0)
    o.show()
    return step + 2


async def _scroller(o):
    step = 0
    while True:
        try:
            step = scroller_frame(o, step)
        except OSError:
            pass  # OLED unplugged; keep trying quietly
        await aio.sleep_ms(30)


# ---- SAO2: e-paper preview -------------------------------------------------

def _title_screen(header, name):
    o = _preview()
    if o is None:
        return
    try:
        o.fill(0)
        o.text(header[:16], 0, 0, 1)
        if len(name) * 16 <= o.width:
            o.big_text(name, 0, 14)           # large, 16px tall
        else:
            o.text(name[:16], 0, 16, 1)       # too long for large text
            if len(name) > 16:
                o.text(name[16:32], 0, 24, 1)
        o.show()
    except OSError:
        pass  # OLED unplugged mid-use; ignore


def show_lines(lines):
    """Up to 4 lines of small text (16 chars each)."""
    o = _preview()
    if o is None:
        return
    try:
        o.fill(0)
        for i, line in enumerate(lines[:4]):
            o.text(line[:16], 0, i * 8, 1)
        o.show()
    except OSError:
        pass


def show_menu(name, idx, total, hint="A/B=go"):
    """Menu mode: which app is selected, plus a short button hint."""
    _title_screen(f"{idx + 1}/{total} {hint}", name)


def show_loading(name):
    """An app was just launched; the e-paper is about to refresh."""
    _title_screen("Loading...", name)


def show_app(name):
    """The app has finished its first draw."""
    _title_screen("Now showing:", name)
