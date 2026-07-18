"""Self-test: the "Import as" combo's pure option-selection logic
(rumble.ui.autoeq_dialog._available_import_kinds). Fully offline — no Qt
widgets are constructed, just the plain function driven by stub catalog/entry
objects, so this runs in the headless gate."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from rumble.ui.autoeq_dialog import _available_import_kinds

failures = []


def check(cond, label):
    print(("  PASS  " if cond else "  FAIL  ") + label)
    if not cond:
        failures.append(label)


class _Cat:
    supports_fixed_band = True
    supports_convolution = False


class _Entry:
    def __init__(self, transducer):
        self.transducer = transducer


kinds = _available_import_kinds(_Cat(), _Entry("headphone"))
check([k for _, k in kinds] == ["standard", "fixed_band"],
      "headphone entry offers fixed-band only (no convolution support yet)")
kinds = _available_import_kinds(_Cat(), _Entry("speaker"))
check([k for _, k in kinds] == ["standard"],
      "speaker entry never offers headphone-only variants")
kinds = _available_import_kinds(_Cat(), None)
check([k for _, k in kinds] == ["standard"],
      "no selection offers only the standard variant")

print()
print("ALL CHECKS PASSED" if not failures else f"{len(failures)} FAILURE(S)")
sys.exit(1 if failures else 0)
