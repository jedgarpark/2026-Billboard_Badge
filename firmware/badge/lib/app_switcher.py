"""App Switcher"""

import asyncio as aio  # type: ignore
import os
import sys
import btree
import time

from apps.base_app import BaseApp
from lib.button import MenuMinus, MenuPlus, UserA, UserB, PRESSED_READ
from lib.led_bar import leds
from lib import oled

# Idle time in the menu before returning to the running app (if there is one).
MENU_TIMEOUT_MILLISECONDS = 3000


APPMOD_DENYLIST = {
    "__init__",
    "base_app",
    "template",
}


class AppSwitcher:
    def __init__(self, initial_idx: int = 0) -> None:
        self.apps = []
        self.active_idx = initial_idx
        self.selecting_mode = True
        self.selection_idx = initial_idx
        self._oled_idx = None  # selection last drawn on the OLED
        self._app_running = False  # has any app been launched yet?
        self._menu_activity_ms = time.ticks_ms()  # last menu button press

        # Discover and load apps
        app_mods = []
        for filename, _, _, _ in os.ilistdir("/apps"):  # pyright: ignore[reportAssignmentType]
            if not filename.endswith(".py") or filename.startswith("_"):
                continue
            app_modname = filename.split(".")[0]
            app_mods.append(app_modname)
        if app_mods:
            print(f"Found app mods: {app_mods}")
            with open("/data/user_apps.py", "w") as out:
                out.write(f"from apps import {', '.join(app_mods)}\n")

        import data.user_apps as apps  # pyright: ignore[reportMissingImports]
        apps_list = [app for app in dir(apps) if "__" not in app]
        print(f"App module list: {apps_list}")
        for appmod_name in apps_list:
            if appmod_name.startswith("_") or appmod_name in APPMOD_DENYLIST:
                continue
            appmod = getattr(apps, appmod_name)
            try:
                for modattr in appmod.__dict__.values():
                    # Subclasses of an app class count too. _abstract skips the
                    # shared base so only the concrete apps are constructed.
                    if not isinstance(modattr, type) or not issubclass(modattr, BaseApp):
                        continue
                    if modattr is BaseApp or modattr.__dict__.get("_abstract"):
                        continue
                    app = modattr()
                    self.apps.append(app)
                    print(f"Added app: {app.name}")
            except Exception as exc:
                print(f"Failed to load app module: {appmod}")
                sys.print_exception(exc)

        try:
            apps_file = open("/data/app_list", "r+b")
        except OSError:
            apps_file = open("/data/app_list", "w+b")
        # Now open a database itself
        apps_db = btree.open(apps_file)
        for idx, app in enumerate(self.apps):
            apps_db[bytes([idx])] = bytes(app.name, "UTF-8")
        apps_db.flush()
        apps_db.close()
        apps_file.close()


    async def arun(self):
        while True:
            if self.selecting_mode:
               
                ## Display current app from all installed apps
                leds.menu_display(self.selection_idx, len(self.apps))
                if self._oled_idx != self.selection_idx:
                    self._oled_idx = self.selection_idx
                    app = self.apps[self.selection_idx]
                    # Apps can set menu_hint to explain what A/B do on launch.
                    oled.show_menu(app.name, self.selection_idx, len(self.apps),
                                   getattr(app, "menu_hint", "A/B=go"))

                if MenuPlus.pressed():
                    self._menu_activity_ms = time.ticks_ms()
                    self.selection_idx = (self.selection_idx + 1) % len(self.apps)
                    print(
                        f"Selected app {self.selection_idx}: {self.apps[self.selection_idx].name}"
                    )
                if MenuMinus.pressed():
                    self._menu_activity_ms = time.ticks_ms()
                    self.selection_idx = (self.selection_idx - 1) % len(self.apps)
                    print(
                        f"Selected app {self.selection_idx}: {self.apps[self.selection_idx].name}"
                    )
                # Read both buttons (no short-circuit) so we know which one launched the app.
                a_pressed = UserA.pressed()
                b_pressed = UserB.pressed()
                button = "B" if b_pressed else "A"
                selected = self.apps[self.selection_idx]
                if (a_pressed or b_pressed) and self._can_resume(selected, button):
                    # Picked the app that's already running, with nothing to redraw.
                    self._resume()
                elif a_pressed or b_pressed:
                    print(f"Switching to {self.apps[self.selection_idx].name}")
                    self.active_idx = self.selection_idx
                    # Tell the app which button launched it: "A" or "B".
                    self.apps[self.active_idx].launch_button = button
                    self.apps[self.active_idx].launched = True
                    self._oled_idx = None  # redraw the menu preview next time
                    oled.show_loading(self.apps[self.active_idx].name)
                    leds.clear_sequence()
                    leds.fill((0, 0, 0))
                    leds.write()
                    self.selecting_mode = False
                    self._app_running = True
                elif self._app_running and time.ticks_diff(
                    time.ticks_ms(), self._menu_activity_ms
                ) > MENU_TIMEOUT_MILLISECONDS:
                    # Menu left idle: go back to the running app without relaunching.
                    self._resume()
                await aio.sleep_ms(10)
            else:
                if MenuMinus.pressed() or MenuPlus.pressed():
                    self.selection_idx = self.active_idx
                    self.selecting_mode = True
                    self._menu_activity_ms = time.ticks_ms()
                    ## Reset pressed state so that button doesn't advance menu, just enters menu state
                    MenuMinus._pressed_state = PRESSED_READ
                    MenuPlus._pressed_state = PRESSED_READ
                    print("Activated app switching")
                first_run = self.apps[self.active_idx].launched
                await self.apps[self.active_idx].arun()
                await aio.sleep_ms(0)
                self.apps[self.active_idx].launched = False
                # Apps that set owns_oled draw their own OLED status screen.
                if first_run and not getattr(self.apps[self.active_idx], "owns_oled", False):
                    oled.show_app(self.apps[self.active_idx].name)


    def _can_resume(self, app, button):
        """Choosing the running app again skips the relaunch if the app says
        nothing would change (e.g. Nametag already showing that image)."""
        if not self._app_running or app is not self.apps[self.active_idx]:
            return False
        can_resume = getattr(app, "can_resume", None)
        return bool(can_resume and can_resume(button))

    def _resume(self):
        """Leave the menu and return to the running app without relaunching it."""
        app = self.apps[self.active_idx]
        print(f"Resuming {app.name}")
        self.selecting_mode = False
        self.selection_idx = self.active_idx
        self._oled_idx = None
        if hasattr(app, "resume"):
            app.resume()  # app restores its own LEDs / OLED
        else:
            # The menu took over the LEDs; blank them and let any app sequence continue.
            leds.clear()
            leds.unpause()
            if not getattr(app, "owns_oled", False):
                oled.show_app(app.name)


switcher = AppSwitcher()
