"""Static sweep for project_dialog_standard_size.md's hard rule: no pop-out
dialog gets its own footer "Close" button -- the frameless chrome strip's X
closes them, and a second Close button was flagged as redundant (2026-07-16).
This is a source-text sweep rather than instantiating every dialog (which
would need a bespoke Qt fixture per class) -- cheap, and it directly encodes
the rule that let three real instances (PromptViewerDialog, CoexistenceDialog,
ProfileTrashDialog) slip through review before a full-codebase audit caught
them on 2026-07-17.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

UI_DIR = Path(__file__).parent.parent / "rumble" / "ui"

# QDialogButtonBox.StandardButton.Close (or the older un-namespaced
# QDialogButtonBox.Close spelling) is always a redundant Close button in this
# app -- Ok/Cancel/Save and friends on the same box are fine and not matched.
_STANDARD_CLOSE_RE = re.compile(r"QDialogButtonBox\.(StandardButton\.)?Close\b")

# A literal QPushButton("Close") / QPushButton('Close') footer button. Doesn't
# match buttons with a longer/different label (e.g. "Show Results", "Done"
# used by ABXDialog's finish flow, which carries distinct accept semantics,
# not a duplicate of the chrome X) or a variable named close_btn assigned
# from something else.
_LITERAL_CLOSE_BUTTON_RE = re.compile(r"""QPushButton\(\s*["']Close["']\s*\)""")


def test_no_ui_file_adds_its_own_close_button() -> None:
    offenders = []
    for path in sorted(UI_DIR.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        if _STANDARD_CLOSE_RE.search(text) or _LITERAL_CLOSE_BUTTON_RE.search(text):
            offenders.append(path.name)
    assert not offenders, (
        f"these files add a redundant Close button (chrome-strip X should be "
        f"the only close control): {offenders!r}")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"PASS {name}")
    sys.exit(0)
