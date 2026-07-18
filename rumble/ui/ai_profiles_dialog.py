"""AI profile guidance — reusable info pages.

The two info surfaces, ``AIPromptGuidePage`` (how to generate a profile with an
AI) and ``ImportMethodsPage`` (how to get a profile into the library), are
self-contained QWidgets so they can be embedded anywhere — they are shown as
tabs inside About RUMBLE.

Accent colour is reserved for genuinely interactive elements (buttons, links).
Static status pills use the neutral ``chip-muted`` style; the one accent badge,
"Recommended", is a filled label paired with the highlighted card, not a pill
that could be mistaken for a button.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QMessageBox, QPlainTextEdit,
    QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from rumble.ui import icons
from rumble.ui.a11y import describe
from rumble.ui.frameless import (
    CHROME_STRIP_HEIGHT, STANDARD_DIALOG_HEIGHT, STANDARD_DIALOG_WIDTH,
    FramelessDialog,
)

# The two shipped prompts, in order of capability. Each is sized for a
# different class of AI — see assets/prompts/RUMBLE_Which_Prompt_To_Use.md.
PROMPTS = [
    {
        "key": "universal",
        "file": "RUMBLE_Universal_AI_Prompt.md",
        "title": "Universal",
        "badge": "Recommended",
        "recommended": True,
        "tagline": "Maximum accuracy & every feature — the everyday default",
        "blurb": (
            "Everything RUMBLE can do: device-accurate correction, online gear "
            "research, measurement fitting, speaker time-alignment, the optional "
            "tone-control layer, and the 10-band export."
        ),
        "best_for": "Any capable web-connected assistant — ChatGPT, Gemini, Copilot, and lighter/free tiers.",
        "needs": "Web access + good maths (ideally code execution); a mid-size context window (~16k tokens and up).",
    },
    {
        "key": "minimal",
        "file": "RUMBLE_Minimal_Prompt.md",
        "title": "Minimal",
        "badge": "Offline-friendly",
        "recommended": False,
        "tagline": "Preference-only — runs on small / offline models",
        "blurb": (
            "A stripped-down, preference-only builder. No internet, no measurements, "
            "no heavy maths — it asks a handful of simple questions and produces a "
            "sensible general-purpose profile. Not a measured per-device correction."
        ),
        "best_for": "Small models you run yourself or offline (Llama, Mistral, Qwen, Gemma, Phi, DeepSeek), or any AI without web access.",
        "needs": "Nothing special — basic question-answering only; fits a tiny context window (~4k tokens).",
    },
]

# (icon, "what you have", recommended prompt) — the quick-pick guide.
_GUIDE = [
    ("link", "A web-connected assistant (ChatGPT, Gemini, Copilot), including free tiers",
     "Universal"),
    ("folder_open", "A small / offline model on your own PC (Llama, Mistral, Qwen, Gemma…)",
     "Minimal"),
]

# Extra Equalizer APO syntax beyond the core parametric interview that an AI
# (or a human typing a profile by hand) can include — surfaced here so it's
# known to exist and where RUMBLE exposes it for editing afterwards.
_EXTRA_SYNTAX = [
    {
        "command": "LoudnessCorrection",
        "example": "LoudnessCorrection: State 1 ReferenceLevel 0 ReferenceOffset 0",
        "use": "Compensates for the ear's reduced bass/treble sensitivity at low "
               "playback volume — good for low-volume or night listening.",
        "editor": "Advanced → Loudness",
    },
    {
        "command": "Ear compensation (from an Ear Calibration export)",
        "example": "See the downloaded .txt file's per-ear frequency table and "
                   "imbalance table for the exact numbers to use.",
        "use": "If the user has run RUMBLE's Frequency Calibration (Settings → "
               "Ear Calibration) and shares the exported results file, author "
               "a GraphicEQ or parametric profile that gently corrects the "
               "measured left/right imbalance -- cap any single boost at "
               "+6 dB, and do not attempt to correct for absolute hearing "
               "loss (these are relative, uncalibrated-hardware measurements, "
               "not clinical thresholds).",
        "editor": "Settings → Ear Calibration → Run Ear Calibration… → "
                  "Frequency Calibration → Save Results to File…",
    },
]


# ── shared building blocks ───────────────────────────────────────────────

def subheading(text: str) -> QLabel:
    label = QLabel(text.upper())
    label.setProperty("class", "subheading")
    return label


def steps_card(steps: list[str], *, bullet: bool = False) -> QWidget:
    """A card listing numbered steps, or bulleted best-practice points."""
    card = QFrame()
    card.setProperty("class", "card")
    lay = QVBoxLayout(card)
    lay.setContentsMargins(14, 12, 14, 12)
    lay.setSpacing(8)
    for i, step in enumerate(steps, start=1):
        row = QHBoxLayout()
        row.setSpacing(10)
        if bullet:
            marker = QLabel("•")
            marker.setProperty("class", "secondary")
            marker.setFixedWidth(12)
        else:
            marker = QLabel(str(i))
            marker.setProperty("class", "chip-accent")  # ordinal marker, not a button
            marker.setAlignment(Qt.AlignmentFlag.AlignCenter)
            marker.setFixedWidth(26)
        row.addWidget(marker, 0, Qt.AlignmentFlag.AlignTop)
        text = QLabel(step)
        text.setWordWrap(True)
        row.addWidget(text, 1)
        lay.addLayout(row)
    return card


def _load_prompt_text(filename: str) -> str:
    """Load a prompt's markdown text by filename.

    The prompts are bundled under ``assets/prompts/`` alongside the package
    root both in the source tree and in the frozen build (rumble.spec already
    stages the whole ``rumble/assets`` tree into ``_internal/``, which is what
    ``__file__.parent.parent`` resolves to there).
    """
    base = Path(__file__).parent.parent
    candidate = base / "assets" / "prompts" / filename
    if candidate.exists():
        return candidate.read_text(encoding="utf-8")
    return f"Error: Prompt file not found: {filename}"


# ── prompt viewer ─────────────────────────────────────────────────────────

class PromptViewerDialog(FramelessDialog):
    """Modal dialog showing the full AI prompt text in a monospace view."""

    def __init__(self, prompt_text: str, parent: QWidget | None = None,
                 title: str = "AI Profile Generation Prompt"):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        # Standard pop-out dialog size (project_dialog_standard_size.md: a
        # hard ceiling, no dialog may render larger than this). The prompt
        # view (a QPlainTextEdit, built below) already scrolls internally, so
        # long prompt text is clipped into its own scrollbar, not the window.
        self.setFixedSize(STANDARD_DIALOG_WIDTH, STANDARD_DIALOG_HEIGHT + CHROME_STRIP_HEIGHT)

        root = QVBoxLayout(self.body)
        root.setSpacing(12)
        root.setContentsMargins(16, 16, 16, 16)

        view = QPlainTextEdit()
        view.setPlainText(prompt_text)
        view.setReadOnly(True)
        view.setProperty("class", "raw-text-edit")
        describe(view, "AI prompt text",
                 "The full prompt to give an AI assistant. Read-only — use Copy "
                 "to Clipboard to take it.")
        root.addWidget(view, 1)

        buttons = QHBoxLayout()
        buttons.addStretch()
        copy_btn = QPushButton("Copy to Clipboard")
        copy_btn.setProperty("class", "primary")
        describe(copy_btn, "Copy to clipboard",
                 "Copy the whole prompt to the clipboard, ready to paste into an "
                 "AI assistant.")
        copy_btn.clicked.connect(lambda: self._copy(prompt_text))
        buttons.addWidget(copy_btn)
        root.addLayout(buttons)

    def _copy(self, text: str) -> None:
        QGuiApplication.clipboard().setText(text)
        QMessageBox.information(self, "Copied", "Prompt copied to clipboard!")


# ── reusable info pages ───────────────────────────────────────────────────

class AIPromptGuidePage(QScrollArea):
    """Guidance on picking and using one of the AI prompts."""

    def __init__(self, theme: dict, parent: QWidget | None = None):
        super().__init__(parent)
        self.theme = theme
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)

        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setSpacing(12)
        layout.setContentsMargins(2, 4, 10, 4)

        intro = QLabel(
            "RUMBLE ships two ready-made prompts that do the same job — "
            "they are just sized for different AIs. <b>Use Universal unless your "
            "AI is a small or offline model.</b>")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        layout.addWidget(subheading("Which prompt should I use?"))
        layout.addWidget(self._guide_card())

        layout.addWidget(subheading("The two prompts"))
        for spec in PROMPTS:
            layout.addWidget(self._prompt_card(spec))

        layout.addWidget(subheading("How to use a prompt"))
        layout.addWidget(steps_card([
            "Pick a prompt above and click <b>View Prompt</b> to read it, or "
            "<b>Copy Prompt</b> to copy it.",
            "Open your preferred AI and paste the prompt in.",
            "Answer its questions about your equipment and listening preferences.",
            "Copy the Equalizer APO profile it produces and save it as a .txt file.",
            "In RUMBLE, go to <b>File → Import Profile</b> and select that file.",
            "Load it to apply your new profile — see <b>Import</b> for details.",
        ]))

        layout.addWidget(subheading("Extra syntax an AI can use"))
        layout.addWidget(steps_card([
            f"<b>{s['command']}</b> — {s['use']} Example: <code>{s['example']}</code>. "
            f"RUMBLE edits it under <b>{s['editor']}</b>."
            for s in _EXTRA_SYNTAX
        ], bullet=True))

        layout.addStretch()
        self.setWidget(container)

    def _guide_card(self) -> QWidget:
        card = QFrame()
        card.setProperty("class", "card")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(10)
        for icon_name, have, pick in _GUIDE:
            row = QHBoxLayout()
            row.setSpacing(10)
            glyph = QLabel()
            glyph.setPixmap(icons.icon_pixmap(icon_name, self.theme["accent"], 18))
            glyph.setFixedWidth(22)
            row.addWidget(glyph, 0, Qt.AlignmentFlag.AlignVCenter)
            label = QLabel(have)
            label.setWordWrap(True)
            label.setProperty("class", "secondary")
            row.addWidget(label, 1)
            pick_label = QLabel(pick)
            pick_label.setProperty("class", "feature-title")  # emphasis, not a pill
            row.addWidget(pick_label, 0, Qt.AlignmentFlag.AlignVCenter)
            lay.addLayout(row)
        return card

    def _prompt_card(self, spec: dict) -> QWidget:
        card = QFrame()
        card.setProperty("class", "card-selected" if spec["recommended"] else "card")
        v = QVBoxLayout(card)
        v.setSpacing(7)
        v.setContentsMargins(14, 12, 14, 12)

        header = QHBoxLayout()
        header.setSpacing(8)
        title = QLabel(spec["title"])
        title.setProperty("class", "feature-title")
        header.addWidget(title)
        badge = QLabel(spec["badge"])
        badge.setProperty("class", "chip-accent" if spec["recommended"] else "chip-muted")
        header.addWidget(badge)
        header.addStretch()
        v.addLayout(header)

        tagline = QLabel(spec["tagline"])
        tagline.setWordWrap(True)
        tagline.setProperty("class", "secondary")
        v.addWidget(tagline)

        blurb = QLabel(spec["blurb"])
        blurb.setWordWrap(True)
        v.addWidget(blurb)

        meta = QLabel(
            f"<b>Best on:</b> {spec['best_for']}<br><b>Needs:</b> {spec['needs']}")
        meta.setWordWrap(True)
        meta.setProperty("class", "secondary")
        v.addWidget(meta)

        buttons = QHBoxLayout()
        view_btn = QPushButton("View Prompt")
        view_btn.setProperty("class", "primary")
        describe(view_btn, "View prompt",
                 "Open the full AI prompt for this method so you can read it "
                 "before copying.")
        view_btn.clicked.connect(lambda _=False, s=spec: self._view_prompt(s))
        buttons.addWidget(view_btn)
        copy_btn = QPushButton("Copy Prompt")
        describe(copy_btn, "Copy prompt",
                 "Copy this method's AI prompt straight to the clipboard.")
        copy_btn.clicked.connect(lambda _=False, s=spec: self._copy_prompt(s))
        buttons.addWidget(copy_btn)
        buttons.addStretch()
        v.addLayout(buttons)
        return card

    def _view_prompt(self, spec: dict) -> None:
        prompt = _load_prompt_text(spec["file"])
        PromptViewerDialog(prompt, self, title=f"RUMBLE {spec['title']} Prompt").exec()

    def _copy_prompt(self, spec: dict) -> None:
        QGuiApplication.clipboard().setText(_load_prompt_text(spec["file"]))
        QMessageBox.information(
            self, "Copied",
            f"{spec['title']} prompt copied to clipboard! Paste it into your AI of choice.")


class ImportMethodsPage(QScrollArea):
    """The ways to get an existing profile into the library."""

    _METHODS = [
        ("Direct file import", "Easiest",
         "For a .txt profile from an AI, an export, or any other source: "
         "<b>File → Import Profile</b>, choose the .txt, and RUMBLE detects "
         "the profile, name and metadata automatically. Works with any valid "
         "Equalizer APO config."),
        ("Bundle import", "With extra files",
         "If a profile uses convolution IR files, zip the .txt together with its "
         ".wav/.pcm impulse responses, then <b>File → Import Profile</b> and pick "
         "the .zip — everything is extracted and organised together."),
        ("Manual text paste", "No file needed",
         "If the AI printed the profile in chat: copy the text, paste it into a "
         "new .txt file (e.g. in Notepad), save it, then <b>File → Import "
         "Profile</b>."),
        ("From Equalizer APO", "Reuse existing config",
         "Already have a config in <code>EqualizerAPO\\config</code>? Import it "
         "directly with <b>File → Import Profile</b>; it comes in as-is, ready to "
         "edit and re-tag here."),
    ]

    def __init__(self, theme: dict, parent: QWidget | None = None):
        super().__init__(parent)
        self.theme = theme
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)

        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setSpacing(12)
        layout.setContentsMargins(2, 4, 10, 4)

        intro = QLabel(
            "RUMBLE accepts profiles from many sources. Pick whichever route "
            "matches what your AI handed back.")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        for i, (title, tag, desc) in enumerate(self._METHODS, start=1):
            layout.addWidget(self._method_card(i, title, tag, desc))

        layout.addWidget(subheading("Best practices"))
        layout.addWidget(steps_card([
            "<b>Test before saving</b> — audition the profile live first.",
            "<b>Name clearly</b> — e.g. “Warm Jazz Headphones”.",
            "<b>Add tags</b> — organise with hierarchies like Music / Jazz.",
            "<b>Iterate</b> — try variations to find your preference.",
            "<b>Back up</b> — <b>File → Backup Library</b> snapshots everything.",
            "<b>Share</b> — <b>File → Export Profile</b> packages it for others.",
        ], bullet=True))

        layout.addStretch()
        self.setWidget(container)

    def _method_card(self, number: int, title: str, tag: str, desc: str) -> QWidget:
        card = QFrame()
        card.setProperty("class", "card")
        lay = QHBoxLayout(card)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(12)

        num = QLabel(str(number))
        num.setProperty("class", "chip-accent")  # ordinal marker, not a button
        num.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(num, 0, Qt.AlignmentFlag.AlignTop)

        col = QVBoxLayout()
        col.setSpacing(4)
        head = QHBoxLayout()
        head.setSpacing(8)
        name = QLabel(title)
        name.setProperty("class", "feature-title")
        head.addWidget(name)
        chip = QLabel(tag)
        chip.setProperty("class", "chip-muted")
        head.addWidget(chip)
        head.addStretch()
        col.addLayout(head)
        text = QLabel(desc)
        text.setWordWrap(True)
        text.setProperty("class", "secondary")
        col.addWidget(text)
        lay.addLayout(col, 1)
        return card
