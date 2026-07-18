"""Proof-of-concept: destroy and recreate QApplication in-process with a
different QT_SCALE_FACTOR, verifying Qt re-reads the env vars each time."""
import gc
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import shiboken6
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QLabel, QMainWindow


def run_cycle(scale: str | None) -> tuple[float, int]:
    if scale:
        os.environ["QT_ENABLE_HIGHDPI_SCALING"] = "0"
        os.environ["QT_SCALE_FACTOR"] = scale
    else:
        os.environ.pop("QT_ENABLE_HIGHDPI_SCALING", None)
        os.environ.pop("QT_SCALE_FACTOR", None)

    app = QApplication(sys.argv)
    window = QMainWindow()
    window.setFixedSize(400, 200)
    window.setCentralWidget(QLabel("scale test"))
    window.show()
    QTimer.singleShot(400, app.quit)
    app.exec()

    dpr = window.devicePixelRatio()
    phys_w = int(window.width() * dpr)
    window.close()
    shiboken6.delete(window)
    del window
    shiboken6.delete(app)
    del app
    gc.collect()
    assert QApplication.instance() is None, "QApplication did not fully tear down"
    return dpr, phys_w


if __name__ == "__main__":
    results = []
    for scale in (None, "2", None):
        dpr, w = run_cycle(scale)
        results.append((scale, dpr, w))
        print(f"scale={scale!r}: devicePixelRatio={dpr}, physical_width={w}")
    ok = (results[1][1] == 2.0 and results[0][1] != 2.0 and results[2][1] == results[0][1])
    print("RESULT:", "PASS" if ok else "FAIL")
