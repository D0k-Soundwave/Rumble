"""Drive the real main() through a live interface-size toggle:
standard -> double -> standard, self-capturing the window at each stage.
Verifies the in-process QApplication rebuild applies the scale immediately."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import QTimer

from rumble import __main__ as rumble_main
from rumble.ui.main_window import MainWindow

CAPTURE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_rescale_captures")
os.makedirs(CAPTURE_DIR, exist_ok=True)

stage = {"n": 0}
results = []
_orig_init = MainWindow.__init__


def _step(window: MainWindow) -> None:
    n = stage["n"]
    stage["n"] += 1
    dpr = window.devicePixelRatio()
    pixmap = window.grab()
    path = os.path.join(CAPTURE_DIR, f"stage{n}_dpr{dpr}.png")
    pixmap.save(path)
    results.append((n, dpr, pixmap.width(), pixmap.height(),
                    window._dirty, window._loaded_profile_id))
    print(f"stage {n}: devicePixelRatio={dpr} grab={pixmap.width()}x{pixmap.height()} "
          f"dirty={window._dirty} loaded={window._loaded_profile_id}", flush=True)

    if n == 0:
        # Simulate a dirty edit so we can check it survives the rebuild
        window._dirty = True
        window.manager.settings["ui_double_size"] = True
        window.manager.save()
        window._apply_ui_size_change()
    elif n == 1:
        window.manager.settings["ui_double_size"] = False
        window.manager.save()
        window._apply_ui_size_change()
    else:
        window._quit_application()


def _patched_init(self, *args, **kwargs):
    _orig_init(self, *args, **kwargs)
    QTimer.singleShot(1200, lambda: _step(self))


MainWindow.__init__ = _patched_init

code = rumble_main.main()
print(f"main() returned {code}")

ok = (len(results) == 3
      and results[0][1] != 2.0 and results[1][1] == 2.0 and results[2][1] != 2.0
      and results[1][2] == 2 * 1280  # doubled physical width
      and results[1][4] is True      # dirty edit carried across rebuild
      and results[1][5] == results[0][5])
print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
