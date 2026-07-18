"""About RUMBLE — a polished, theme-aware overview of the tool.

A hero header (app mark, name, tagline, chips), a responsive grid of feature
cards covering everything the app does — every subsystem in
docs/RUMBLE_FEATURE_MAP.md, condensed to one line each — and compatibility /
author sections, all built from the shared theme tokens and QSS classes so it
tracks every theme.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QFrame, QGridLayout, QHBoxLayout, QLabel,
    QScrollArea, QTabWidget, QVBoxLayout, QWidget,
)

from rumble.ui import icons
from rumble.ui.ai_profiles_dialog import AIPromptGuidePage, ImportMethodsPage
from rumble.ui.frameless import (
    CHROME_STRIP_HEIGHT, STANDARD_DIALOG_HEIGHT, STANDARD_DIALOG_WIDTH, FramelessDialog,
)

_APP_ICON = Path(__file__).resolve().parent.parent / "rumble.ico"

# The overview intro, shared with Settings → Accessibility so the "Test voice"
# button reads real About-page copy aloud rather than a throwaway phrase.
ABOUT_INTRO = (
    "RUMBLE turns hand-editing Equalizer APO's text configuration into "
    "a polished workflow: create, edit, organise, preview and instantly "
    "activate equalization profiles for your headphones, IEMs and speakers "
    "— plus hearing calibration, room/speaker correction, crossfeed, bass "
    "management and a full live-monitoring toolkit built on top.")

# (icon name, title, one-line description) — the complete capability set.
# Kept in sync with docs/RUMBLE_FEATURE_MAP.md, the code-verified full
# feature map — update that file first, then reflect changes here.
_FEATURES: list[tuple[str, str, str]] = [
    ("tune", "Parametric EQ editor",
     "Up to 32 bands — peak, shelves, pass, notch and custom IIR — over a live "
     "frequency-response graph, with GraphicEQ ↔ parametric conversion."),
    ("play_arrow", "Real-time preview",
     "Unsaved edits are auditioned live, with auto-preamp, solo/A-B compare "
     "and full undo/redo history."),
    ("folder_open", "Profile library",
     "Portable, self-contained text files organised in a searchable, tag-based "
     "tree, with a recycle bin and 7 built-in starting profiles."),
    ("check_circle", "Instant switching",
     "Activate any profile system-wide, toggle bypass, switch from the "
     "system tray, or assign different profiles to different audio devices."),
    ("chevron_right", "Automatic profile switching",
     "Rules that switch profiles by running app, output device or time of "
     "day — with an optional native Equalizer APO export that runs with no "
     "RUMBLE process at all."),
    ("edit", "AI-assisted profile authoring",
     "Copy-paste prompts (Tools → AI Profiles) for any AI assistant, sized "
     "for both web-connected and small offline models — RUMBLE itself has "
     "no built-in AI."),
    ("link", "Measurement database",
     "One federated search across AutoEQ's ~4,000 measured headphones/IEMs "
     "and Spinorama's anechoic speaker database."),
    ("add", "AutoEQ merge & gear simulation",
     "Average several measurements of one model into a cleaner profile, or "
     "build a source→target delta so one set of gear mimics another."),
    ("content_copy", "REW & Peace import",
     "Bring in your own REW room/headphone measurements or existing Peace "
     "presets, blended with or replacing community data."),
    ("tune", "Speaker correction",
     "Target-curve correction (Harman, diffuse-field or a flat/room-tilt "
     "start) with directivity-aware boost limiting from published data."),
    ("info", "Ear Calibration",
     "A per-ear frequency hearing test for up to 4 listener profiles, with "
     "gentle, weak-ear-only correction applied automatically."),
    ("expand_more", "ISO 226 loudness compensation",
     "Real equal-loudness-contour compensation that tracks your live "
     "Windows volume, so quiet listening keeps its tonal balance."),
    ("drag_handle", "Session tone trim",
     "An instant bass/treble/tilt control for late-night or one-off "
     "listening, bakeable into a profile when you want to keep it."),
    ("link", "Headphone crossfeed",
     "Five cited classic circuits (Chu Moy, Meier, Linkwitz, Bauer/bs2b) "
     "plus a custom mode and an FFmpeg-derived soundstage blend."),
    ("settings", "Bass management & crossovers",
     "Linkwitz-Riley crossover design, sub/mains bass redirection, and "
     "mic-measured multi-speaker time alignment."),
    ("check_circle", "Blind A/B/X testing",
     "Level-matched blind comparison between profiles with a real "
     "statistical verdict, not just a gut feeling."),
    ("play_arrow", "Live spectrum analyzer & meters",
     "A real-time spectrum overlay plus VU/Peak/Reference level meters "
     "built on named broadcast-standard ballistics."),
    ("shield", "Equalizer APO integration",
     "Takes ownership of config.txt safely, with a foreign-tool coexistence "
     "dashboard, a config-health viewer and drift-snapshot recovery."),
    ("backup", "Import & export",
     "Bring in .txt profiles or .zip packs (with convolution impulse responses), "
     "and share your own."),
    ("settings", "Themes",
     "Seven built-in colour themes plus a live System theme, and custom "
     "theme import and export."),
    ("restore", "Backup & restore",
     "Snapshot your whole library and APO setup into a single portable zip."),
]


class AboutDialog(FramelessDialog):
    """Modal dialog showing a polished overview of RUMBLE."""

    def __init__(self, theme: dict, parent: QWidget | None = None):
        super().__init__(parent)
        self.theme = theme
        self.setWindowTitle("About RUMBLE")
        self.setModal(True)
        self.setFixedSize(STANDARD_DIALOG_WIDTH, STANDARD_DIALOG_HEIGHT + CHROME_STRIP_HEIGHT)

        root = QVBoxLayout(self.body)
        root.setSpacing(16)
        root.setContentsMargins(20, 20, 20, 20)

        root.addWidget(self._build_hero())

        # The overview plus the AI-profile and import guides as selectable pages.
        tabs = QTabWidget()
        tabs.addTab(self._build_overview(), "Overview")
        tabs.addTab(AIPromptGuidePage(self.theme), "AI Profiles")
        tabs.addTab(ImportMethodsPage(self.theme), "Import")
        tabs.setTabToolTip(0, "Everything RUMBLE does, plus APO compatibility notes.")
        tabs.setTabToolTip(1, "How to generate a profile using an AI assistant prompt.")
        tabs.setTabToolTip(2, "The different ways to get an existing profile into your library.")
        root.addWidget(tabs, 1)

        root.addWidget(self._build_footer())

    # ── overview tab ─────────────────────────────────────────────────
    def _build_overview(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)

        content = QWidget()
        scroll.setWidget(content)
        body = QVBoxLayout(content)
        body.setSpacing(14)
        body.setContentsMargins(2, 4, 10, 4)

        intro = QLabel(ABOUT_INTRO)
        intro.setWordWrap(True)
        body.addWidget(intro)

        body.addWidget(self._subheading("Everything it does"))
        body.addWidget(self._feature_grid())

        body.addWidget(self._subheading("Standard APO compatibility"))
        body.addWidget(self._info_card(
            "Every RUMBLE profile is a valid Equalizer APO config file. Import "
            "any APO profile — hand-written or exported from measurement tools "
            "— edit it here, and it stays a working APO config. RUMBLE only "
            "adds its metadata (name, description, tags) as comment lines APO "
            "ignores, so the same file works in both."))

        body.addWidget(self._subheading("About the author"))
        author = QLabel(
            "RUMBLE was created by <b>D0k</b> to make advanced audio shaping "
            "approachable — pairing careful UX design with powerful processing "
            "so everyone from casual listeners to audio engineers can dial in the "
            "sound they want.")
        author.setWordWrap(True)
        author.setProperty("class", "secondary")
        body.addWidget(author)

        body.addWidget(self._subheading("Terms & Conditions"))
        body.addWidget(self._info_card(
            "By agreeing to the terms and conditions outlined during the app "
            "install, you have entered into a binding, non-breakable contract "
            "to address the developer as “Your D0kkyness” in any and "
            "all future meetings, calls, messages, or other communication, of "
            "any kind, forever, no exceptions. You should have read the "
            "installer more carefully."))

        body.addStretch()
        return scroll

    # ── hero ─────────────────────────────────────────────────────────
    def _build_hero(self) -> QWidget:
        hero = QFrame()
        hero.setProperty("class", "hero")
        lay = QHBoxLayout(hero)
        lay.setContentsMargins(20, 18, 20, 18)
        lay.setSpacing(18)

        if _APP_ICON.exists():
            mark = QLabel()
            # Loaded as a QIcon (not a bare QPixmap) so Qt picks the closest
            # embedded frame in rumble.ico's multi-size PNG-in-ICO set (up to
            # 256px) rather than always decoding the file's first/smallest
            # frame — a flat QPixmap(path) load would grab the 16px frame and
            # scale it up to 64px, which is what produced the blurry, grainy
            # mark here before.
            pix = QIcon(str(_APP_ICON)).pixmap(128, 128)
            if not pix.isNull():
                mark.setPixmap(pix.scaled(
                    64, 64, Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation))
                lay.addWidget(mark, 0, Qt.AlignmentFlag.AlignTop)

        text = QVBoxLayout()
        text.setSpacing(4)

        title = QLabel("RUMBLE")
        title.setProperty("class", "app-title")
        text.addWidget(title)

        tagline = QLabel("EQ Profile Manager for Equalizer APO")
        tagline.setProperty("class", "secondary")
        text.addWidget(tagline)

        # Static descriptors — neutral chips. Accent is reserved for clickable
        # things (buttons, links, navigation), never plain status labels.
        chips = QHBoxLayout()
        chips.setSpacing(6)
        for label in ("For Equalizer APO", "Windows desktop", "Portable profiles"):
            chip = QLabel(label)
            chip.setProperty("class", "chip-muted")
            chips.addWidget(chip)
        chips.addStretch()
        text.addSpacing(4)
        text.addLayout(chips)

        lay.addLayout(text, 1)
        return hero

    # ── feature grid ─────────────────────────────────────────────────
    def _feature_grid(self) -> QWidget:
        wrap = QWidget()
        grid = QGridLayout(wrap)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(10)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        for i, (icon_name, title, desc) in enumerate(_FEATURES):
            grid.addWidget(self._feature_card(icon_name, title, desc), i // 2, i % 2)
        return wrap

    def _feature_card(self, icon_name: str, title: str, desc: str) -> QWidget:
        card = QFrame()
        card.setProperty("class", "card")
        lay = QHBoxLayout(card)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(12)

        glyph = QLabel()
        glyph.setPixmap(icons.icon_pixmap(icon_name, self.theme["accent"], 22))
        glyph.setFixedWidth(24)
        lay.addWidget(glyph, 0, Qt.AlignmentFlag.AlignTop)

        col = QVBoxLayout()
        col.setSpacing(2)
        name = QLabel(title)
        name.setProperty("class", "feature-title")
        col.addWidget(name)
        text = QLabel(desc)
        text.setProperty("class", "secondary")
        text.setWordWrap(True)
        col.addWidget(text)
        lay.addLayout(col, 1)
        return card

    # ── helpers ──────────────────────────────────────────────────────
    def _subheading(self, text: str) -> QLabel:
        label = QLabel(text.upper())
        label.setProperty("class", "subheading")
        return label

    def _info_card(self, text: str) -> QWidget:
        card = QFrame()
        card.setProperty("class", "info-banner")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(14, 12, 14, 12)
        label = QLabel(text)
        label.setWordWrap(True)
        label.setProperty("class", "secondary")
        lay.addWidget(label)
        return card

    def _build_footer(self) -> QWidget:
        footer = QWidget()
        lay = QHBoxLayout(footer)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        links = QLabel(
            "<a href='https://sourceforge.net/projects/equalizerapo/'>Equalizer APO</a>"
            " &nbsp;·&nbsp; "
            "<a href='https://github.com/jaakkopasanen/AutoEq'>AutoEq project</a>")
        links.setOpenExternalLinks(True)
        links.setProperty("class", "muted")
        lay.addWidget(links)
        lay.addStretch()
        return footer
