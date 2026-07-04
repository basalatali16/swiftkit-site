"""
LANCOM entry point with a crash reporter.

python-for-android always launches `main.py`, so this thin wrapper runs
the real app (lancom_app.py) and, if ANYTHING crashes during import or
startup, shows the full traceback on the phone screen instead of
silently closing. On a sideloaded APK with no adb attached, a screenshot
of that screen is the only crash log we can get - "the app just closes"
is undebuggable, a traceback is a five-minute fix.

The traceback is also appended to lancom_crash.log in the app's private
directory so it survives for later inspection.
"""

import os
import time
import traceback


def _log_crash(tb_text):
    try:
        base = (os.environ.get("ANDROID_PRIVATE")
                or os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(base, "lancom_crash.log"), "a",
                  encoding="utf-8") as f:
            f.write("\n--- %s ---\n%s\n" % (time.ctime(), tb_text))
    except Exception:
        pass


def _crash_screen(tb_text):
    """Bare-bones Kivy app that just displays the traceback. Uses only
    core Kivy widgets so it works even when the crash came from one of
    our own modules or a third-party dependency."""
    from kivy.app import App
    from kivy.metrics import dp
    from kivy.uix.boxlayout import BoxLayout
    from kivy.uix.label import Label
    from kivy.uix.scrollview import ScrollView

    class CrashApp(App):
        def build(self):
            root = BoxLayout(orientation="vertical", padding=dp(12),
                             spacing=dp(8))
            title = Label(
                text="LANCOM could not start.\nPlease screenshot this "
                     "screen and send it to the developer.",
                bold=True, color=(1.0, 0.45, 0.35, 1), halign="center",
                size_hint_y=None, height=dp(64))
            title.bind(width=lambda inst, w: setattr(
                inst, "text_size", (w, None)))
            root.add_widget(title)
            scroll = ScrollView()
            body = Label(text=tb_text, font_size=dp(11),
                         color=(0.92, 0.92, 0.92, 1),
                         size_hint_y=None, halign="left", valign="top")
            body.bind(width=lambda inst, w: setattr(
                inst, "text_size", (w, None)))
            body.bind(texture_size=lambda inst, s: setattr(
                inst, "height", s[1]))
            scroll.add_widget(body)
            root.add_widget(scroll)
            return root

    CrashApp().run()


if __name__ == "__main__":
    try:
        from lancom_app import LancomApp
        LancomApp().run()
    except Exception:
        tb = traceback.format_exc()
        _log_crash(tb)
        try:
            _crash_screen(tb)
        except Exception:
            raise SystemExit(tb)
