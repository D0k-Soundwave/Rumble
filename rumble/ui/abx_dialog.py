"""Blind A/B/X listening test dialog (Phase 5, Task 4 — UI).

Consumes the Qt-free core (``rumble/core/abx.py``) without reimplementing any
of its logic: ``abx.level_match()`` gain-matches the candidate configs,
``abx.ABXSession`` runs the blind trial sequence, ``abx.ABXRunner`` switches
Equalizer APO's live config during a trial and guarantees the listener's true
active profile is restored on every exit path (including an exception or the
dialog simply being closed mid-trial).

Three pages in a ``QStackedWidget``:

- **Setup** — a user-chosen count (2 up to every available candidate: every
  saved profile plus a synthetic "Flat (no EQ)" entry) of configs are picked
  at random rather than chosen by the listener, so there is nothing to
  unconsciously bias toward; preview the level-match offset, Start.
- **Trials** — an anonymous lettered listen button per candidate (A, B, C...,
  also keyboard shortcuts) plus a big X button — which profile hides behind
  which letter is never shown; a dropdown of the candidate profile *names*
  (alphabetical, so its order can't betray the letter mapping) to, purely for
  fun, guess which profile the listener thinks X is, with immediate
  correct/incorrect feedback (revealing what X was) after each guess. Guessing
  is optional scoring, not a requirement to proceed. There is no fixed trial
  count — every guess is one trial, and "Show Results" ends the run whenever
  the listener chooses.
- **Results** — the verdict (``abx.verdict()`` at the 1-in-N chance rate), a
  per-trial table, a "Copy Summary" button (plain text, clipboard), Done.

The caller (``main_window._open_abx_dialog``) is responsible for refusing to
even construct this dialog while the editor has unsaved changes or a live
preview is in effect — this dialog has no opinion on that and always assumes
it is safe to take over the live Equalizer APO config for the duration of the
test, restoring the real active profile via ``ABXRunner.finish()`` on every
way out (Done, Show Results then Done, closing the window, or the OS
window-close box).
"""
from __future__ import annotations

import secrets
import string

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QFrame, QHBoxLayout, QLabel, QPushButton,
    QScrollArea, QSpinBox, QStackedWidget, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from rumble.core import abx
from rumble.core.apo_config import ProfileConfig
from rumble.ui.a11y import describe
from rumble.ui.density import dp
from rumble.ui.frameless import CHROME_STRIP_HEIGHT, FramelessDialog
from rumble.ui.hud_frame import HudFrame

#: Sentinel "profile id" for the synthetic "no EQ" reference config — never a
#: real profile id, so it can't collide with one.
_FLAT_LABEL = "Flat (no EQ)"

#: Below this, the level-match offset is reported as "already matched" rather
#: than a near-zero dB number (mirrors abx.level_match's own tolerance).
_OFFSET_DISPLAY_TOLERANCE_DB = 0.05

#: Default candidate count offered when the Setup page first opens — the
#: listener can raise or lower this (2..every available candidate) via
#: spin_count before Start.
_DEFAULT_CANDIDATE_COUNT = 5

# 'X' is excluded -- it's the reserved label/shortcut for the hidden
# reference button (self.btn_x) below, so a candidate can never be handed
# the same letter: past 23 saved profiles that collision would give two
# buttons the same label and register two QShortcuts on the same key, which
# Qt treats as ambiguous and fires neither -- silently breaking the X key,
# the single most-used shortcut in every trial.
_LETTERS = "".join(c for c in string.ascii_uppercase if c != "X")


def _candidate_label(index: int) -> str:
    """Anonymous listen-button/shortcut label for candidate ``index`` —
    which profile hides behind which label must never be shown before the
    listener answers. A-Z except 'X' (reserved, see _LETTERS), falling back
    to a plain number once that pool is exhausted (the candidate count can
    exceed it once someone has that many saved profiles)."""
    return _LETTERS[index] if index < len(_LETTERS) else str(index + 1)


def _page_heading(text: str) -> QLabel:
    label = QLabel(text)
    label.setProperty("class", "heading")
    return label


class ABXDialog(FramelessDialog):
    """Blind A/B/X listening test: Setup -> Trials -> Results."""

    def __init__(self, manager, theme: dict, parent: QWidget | None = None,
                 rng: object | None = None):
        super().__init__(parent)
        self.setWindowTitle("Blind A/B/X Listening Test")
        # Original resize() target plus FramelessDialog's chrome strip on
        # top, same pattern as settings_dialog.py's SettingsDialog. The
        # trial-history table (self.table_trials, built below) already
        # scrolls internally, so growing this fixed rather than clipping
        # a long trial log.
        self.setFixedSize(dp(640, 560), dp(600, 520) + CHROME_STRIP_HEIGHT)
        self.manager = manager
        self.theme = theme
        # Only ever set by tests, to make both the random candidate draw and
        # the trial sequence deterministic — production callers never pass
        # this, so real sessions stay crypto-random.
        self._rng = rng if rng is not None else secrets.SystemRandom()

        self._profiles = manager.list_profiles()
        self._session: abx.ABXSession | None = None
        self._runner: abx.ABXRunner | None = None
        self._trial_history: list[dict] = []

        # Every saved profile plus the synthetic "Flat" reference is a
        # candidate; the listener picks how many of them (2..all) this test
        # draws via spin_count on the Setup page.
        self._pool_size = len(self._profiles) + 1
        self._candidate_count = (min(_DEFAULT_CANDIDATE_COUNT, self._pool_size)
                                  if self._pool_size >= 2 else self._pool_size)

        # The specific candidates are picked at random, not chosen by the
        # listener — nothing to unconsciously bias toward before the trial
        # even starts.
        self._candidates = self._pick_random_candidates(self._candidate_count)

        self._matched: list[ProfileConfig] = []
        self._offset_db = 0.0
        self._sample_rate = 0
        self._recompute_level_match()

        root = QVBoxLayout(self.body)
        root.setContentsMargins(20, 20, 20, 20)
        root.setSpacing(12)

        self.stack = QStackedWidget()
        root.addWidget(self.stack, 1)

        self.setup_page = self._build_setup_page()
        self.trials_page = self._build_trials_page()
        self.results_page = self._build_results_page()
        self.stack.addWidget(self.setup_page)
        self.stack.addWidget(self.trials_page)
        self.stack.addWidget(self.results_page)

        self._refresh_setup_labels()
        self._populate_trial_controls()

    def _pick_random_candidates(self, count: int) -> list[tuple[str, str | None]]:
        """Randomly picks ``count`` distinct (name, profile_id) entries from
        the synthetic "Flat" reference plus every saved profile — empty if
        fewer than two candidates are available or requested. Draws one
        value from ``self._rng`` per pick so a fixed test double makes the
        draw, like the trial sequence, deterministic."""
        pool = [(_FLAT_LABEL, None)] + [(meta.name, meta.id) for meta in self._profiles]
        if len(pool) < 2 or count < 2:
            return []
        picks: list[tuple[str, str | None]] = []
        remaining = list(pool)
        for _ in range(min(count, len(pool))):
            picks.append(remaining.pop(abx.roll_index(self._rng, len(remaining))))
        return picks

    def _recompute_level_match(self) -> None:
        """Re-runs level matching for the current ``self._candidates``.
        Called on init and every time spin_count redraws the candidate set,
        since the match result depends entirely on which configs are in it."""
        self._matched = []
        self._offset_db = 0.0
        self._sample_rate = 0
        if len(self._candidates) >= 2:
            try:
                self._sample_rate = self.manager.effective_sample_rate()
                self._matched, offsets = abx.level_match(
                    self._load_candidate_configs(), self._sample_rate)
                self._offset_db = max(offsets)
            except Exception:
                self._matched = []  # reported below; Start stays disabled

    def _refresh_setup_labels(self) -> None:
        if len(self._candidates) < 2:
            self.lbl_candidates.setText("Save at least one profile to run a blind test.")
            self.btn_start.setEnabled(False)
            return
        names = "   ·   ".join(name for name, _ in self._candidates)
        self.lbl_candidates.setText(f"Randomly comparing: {names}")
        if not self._matched:
            self.lbl_offset.setText("Level matching failed for these candidates.")
            self.btn_start.setEnabled(False)
        elif self._offset_db < _OFFSET_DISPLAY_TOLERANCE_DB:
            self.lbl_offset.setText("Already level-matched — no loudness adjustment needed.")
            self.btn_start.setEnabled(True)
        else:
            self.lbl_offset.setText(
                f"Level-matched: louder candidates will be reduced by up to "
                f"{self._offset_db:.2f} dB so loudness alone can't bias your answer.")
            self.btn_start.setEnabled(True)

    def _on_count_changed(self, value: int) -> None:
        self._candidate_count = value
        self._candidates = self._pick_random_candidates(value)
        self._recompute_level_match()
        self._refresh_setup_labels()
        self._populate_trial_controls()

    # ── setup page ────────────────────────────────────────────────────

    def _build_setup_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setSpacing(12)
        lay.addWidget(_page_heading("BLIND A/B/X TEST — SETUP"))

        hint = QLabel(
            "RUMBLE draws the number of your saved configurations you choose "
            "below at random — nothing to unconsciously bias toward — and "
            "gain-matches them so loudness alone can't bias your answer. "
            "Each trial, listen to the anonymous lettered candidates and to "
            "the hidden X, then guess which profile you think X is, purely "
            "for fun: you're told immediately whether you were right, and "
            "only then which profile X was. Answer as many trials as you "
            "like, then show the results.")
        hint.setWordWrap(True)
        hint.setProperty("class", "muted")
        lay.addWidget(hint)

        count_row = QHBoxLayout()
        count_row.addWidget(QLabel("Profiles to compare:"))
        self.spin_count = QSpinBox()
        self.spin_count.setRange(2, max(self._pool_size, 2))
        self.spin_count.setValue(self._candidate_count if self._pool_size >= 2 else 2)
        self.spin_count.setEnabled(self._pool_size >= 2)
        describe(self.spin_count, "Number of profiles to compare",
                 "How many configurations — your saved profiles plus the "
                 "synthetic Flat reference — this test randomly draws and "
                 f"blind-compares. From 2 up to all {self._pool_size} available.")
        self.spin_count.valueChanged.connect(self._on_count_changed)
        count_row.addWidget(self.spin_count)
        count_row.addStretch(1)
        lay.addLayout(count_row)

        self.lbl_candidates = QLabel("")
        self.lbl_candidates.setProperty("class", "heading")
        self.lbl_candidates.setWordWrap(True)
        describe(self.lbl_candidates, "Configs being compared",
                 "The randomly-chosen configurations for this test.")
        lay.addWidget(self.lbl_candidates)

        self.lbl_offset = QLabel("")
        self.lbl_offset.setProperty("class", "muted")
        self.lbl_offset.setWordWrap(True)
        lay.addWidget(self.lbl_offset)

        lay.addStretch(1)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        self.btn_start = QPushButton("Start")
        self.btn_start.setProperty("class", "primary")
        self.btn_start.clicked.connect(self._on_start)
        describe(self.btn_start, "Start", "Begin the blind trial sequence.")
        btn_row.addWidget(self.btn_start)
        lay.addLayout(btn_row)

        return page

    def _load_config(self, profile_id: str | None) -> ProfileConfig:
        if profile_id is None:
            return ProfileConfig()
        config = self.manager.load_profile_config(profile_id)
        return config if config is not None else ProfileConfig()

    def _load_candidate_configs(self) -> list[ProfileConfig]:
        return [self._load_config(profile_id) for _, profile_id in self._candidates]

    def _on_start(self) -> None:
        if not self._matched:
            return
        self._session = abx.ABXSession(n_candidates=len(self._matched), rng=self._rng)
        self._runner = abx.ABXRunner(self.manager, self._matched, self._sample_rate)
        self._trial_history = []
        self.cmb_answer.setEnabled(False)
        self.lbl_feedback.setText("")
        self._update_trial_progress()
        self.stack.setCurrentWidget(self.trials_page)

    # ── trials page ───────────────────────────────────────────────────

    def _build_trials_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setSpacing(12)
        lay.addWidget(_page_heading("BLIND A/B/X TEST — TRIALS"))

        disclosure = QLabel(
            "Switching takes a moment (~0.2 s) — an Equalizer APO limitation.")
        disclosure.setProperty("class", "muted")
        disclosure.setWordWrap(True)
        lay.addWidget(disclosure)

        self.lbl_progress = QLabel("")
        self.lbl_progress.setProperty("class", "heading")
        lay.addWidget(self.lbl_progress)

        frame = HudFrame(class_name="panel")
        frame.set_theme(self.theme)
        frame_lay = QVBoxLayout(frame)
        frame_lay.setContentsMargins(dp(14, 10), dp(14, 10), dp(14, 10), dp(14, 10))
        frame_lay.setSpacing(10)

        # The candidate listen buttons, shortcuts, and answer-dropdown items
        # all depend on the current candidate count — which spin_count on the
        # Setup page can change after this skeleton is built — so they're
        # (re)built by _populate_trial_controls() rather than here. The row
        # lives inside a horizontal-scroll wrapper because spin_count's upper
        # bound is the whole saved-profile library — with no cap on how many
        # candidates fit, a wide row must be able to scroll rather than
        # squeeze/overlap inside this dialog's fixed, non-resizable width.
        self._listen_row = QHBoxLayout()
        self.candidate_buttons: list[QPushButton] = []
        listen_container = QWidget()
        listen_container.setLayout(self._listen_row)
        listen_scroll = QScrollArea()
        listen_scroll.setWidget(listen_container)
        listen_scroll.setWidgetResizable(True)
        listen_scroll.setFrameShape(QFrame.Shape.NoFrame)
        listen_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        listen_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        listen_scroll.setFixedHeight(dp(84, 64))
        frame_lay.addWidget(listen_scroll)

        self.btn_x = QPushButton("X")
        self.btn_x.setMinimumHeight(dp(64, 48))
        self.btn_x.setProperty("class", "ghost")
        describe(self.btn_x, "Play X", "Listen to the hidden reference — the "
                                        "one you must identify.")
        self.btn_x.clicked.connect(self._present_x)
        frame_lay.addWidget(self.btn_x)

        answer_row = QHBoxLayout()
        answer_row.addWidget(QLabel("X is:"))
        self.cmb_answer = QComboBox()
        self.cmb_answer.setEnabled(False)
        describe(self.cmb_answer, "Which profile is X",
                 "Just for fun: guess which profile you think the hidden "
                 "reference X is — you're told immediately whether you were "
                 "right.")
        # `activated` fires only on a real user pick, so resetting the combo
        # back to the placeholder programmatically can never re-answer.
        self.cmb_answer.activated.connect(self._on_answer_selected)
        answer_row.addWidget(self.cmb_answer, 1)
        frame_lay.addLayout(answer_row)

        self.lbl_feedback = QLabel("")
        self.lbl_feedback.setProperty("class", "muted")
        self.lbl_feedback.setWordWrap(True)
        frame_lay.addWidget(self.lbl_feedback)

        lay.addWidget(frame)
        lay.addStretch(1)

        finish_row = QHBoxLayout()
        self.btn_finish = QPushButton("Show Results")
        self.btn_finish.setProperty("class", "ghost")
        self.btn_finish.clicked.connect(self._finish_trials)
        describe(self.btn_finish, "Show results",
                 "End the test now and see results for the trials answered so far.")
        finish_row.addWidget(self.btn_finish)
        finish_row.addStretch(1)
        lay.addLayout(finish_row)

        # Keyboard shortcuts, scoped to this page (WidgetWithChildrenShortcut)
        # so X/candidate-letter keys never fire while Setup or Results is
        # showing. Populated (and re-populated on a candidate-count change)
        # by _populate_trial_controls().
        self._shortcuts: list[QShortcut] = []

        return page

    def _populate_trial_controls(self) -> None:
        """(Re)builds the per-candidate listen buttons, keyboard shortcuts,
        and answer-dropdown items from the current ``self._candidates``.
        Called on init and whenever spin_count redraws the candidate set —
        the trials page is otherwise built once, but its per-candidate
        widgets must always match whatever count is currently selected."""
        while self._listen_row.count():
            item = self._listen_row.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.candidate_buttons = []

        for sc in self._shortcuts:
            sc.setEnabled(False)
            sc.deleteLater()
        self._shortcuts = []

        for i in range(len(self._candidates)):
            letter = _candidate_label(i)
            btn = QPushButton(letter)
            btn.setMinimumHeight(dp(64, 48))
            btn.setProperty("class", "ghost")
            describe(btn, f"Play candidate {letter}",
                     "Listen to this candidate. Which profile it is stays "
                     "anonymous until you answer.")
            btn.clicked.connect(lambda checked=False, idx=i: self._present_candidate(idx))
            self._listen_row.addWidget(btn)
            self.candidate_buttons.append(btn)

        self.cmb_answer.clear()
        self.cmb_answer.addItem("Select a profile…")
        # Alphabetical, deliberately NOT in candidate/button order — a
        # dropdown mirroring the lettered buttons' order would itself reveal
        # which profile hides behind which letter.
        self._combo_candidate_indices = sorted(
            range(len(self._candidates)), key=lambda i: self._candidates[i][0].lower())
        for idx in self._combo_candidate_indices:
            self.cmb_answer.addItem(self._candidates[idx][0])

        targets = [("X", self.btn_x)]
        targets += [(_candidate_label(i), btn) for i, btn in enumerate(self.candidate_buttons)]
        for key, target in targets:
            if len(key) != 1:
                continue  # only single-key labels get a keyboard shortcut
            sc = QShortcut(QKeySequence(key), self.trials_page)
            sc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            sc.activated.connect(target.click)
            self._shortcuts.append(sc)

    def _present_candidate(self, index: int) -> None:
        if self._runner is None:
            return
        self._runner.present(index)

    def _present_x(self) -> None:
        if self._runner is None or self._session is None:
            return
        self._runner.present_x(self._session)
        # The combo's enabled state IS the "you have heard X this trial"
        # gate: activated can only fire while it is enabled.
        self.cmb_answer.setEnabled(True)

    def _update_trial_progress(self) -> None:
        if self._session is None:
            return
        answered = self._session.trial
        if answered == 0:
            self.lbl_progress.setText("Trial 1")
        else:
            self.lbl_progress.setText(
                f"Trial {answered + 1}  —  {self._session.correct}/{answered} correct so far")

    def _on_answer_selected(self, combo_index: int) -> None:
        # Index 0 is the "Select a profile…" placeholder, not an answer.
        if combo_index <= 0 or self._session is None:
            return
        guess = self._combo_candidate_indices[combo_index - 1]
        actual = self._session.x_index
        trial_no = self._session.trial + 1
        was_correct = self._session.answer(guess)
        self._trial_history.append({
            "trial": trial_no,
            "guess": self._candidates[guess][0],
            "actual": self._candidates[actual][0],
            "correct": was_correct,
        })
        self.cmb_answer.setCurrentIndex(0)
        self.cmb_answer.setEnabled(False)  # must hear X again next trial
        actual_name = self._candidates[actual][0]
        self.lbl_feedback.setText(
            f"Correct — X was {actual_name}." if was_correct
            else f"Incorrect — X was {actual_name}.")
        self._update_trial_progress()

    def _finish_trials(self) -> None:
        if self._runner is not None:
            self._runner.finish()
        self._populate_results()
        self.stack.setCurrentWidget(self.results_page)

    # ── results page ──────────────────────────────────────────────────

    def _build_results_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setSpacing(12)
        lay.addWidget(_page_heading("BLIND A/B/X TEST — RESULTS"))

        self.lbl_verdict = QLabel("")
        self.lbl_verdict.setProperty("class", "display")
        self.lbl_verdict.setWordWrap(True)
        lay.addWidget(self.lbl_verdict)

        self.lbl_offset_result = QLabel("")
        self.lbl_offset_result.setProperty("class", "muted")
        self.lbl_offset_result.setWordWrap(True)
        lay.addWidget(self.lbl_offset_result)

        self.table_trials = QTableWidget(0, 4)
        self.table_trials.setHorizontalHeaderLabels(["Trial", "Guess", "Actual", "Correct"])
        self.table_trials.horizontalHeader().setStretchLastSection(True)
        self.table_trials.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table_trials.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        describe(self.table_trials, "Trial results",
                 "Per-trial record of what you guessed, what it actually was, "
                 "and whether you were correct.")
        lay.addWidget(self.table_trials, 1)

        btn_row = QHBoxLayout()
        self.btn_copy = QPushButton("Copy Summary")
        self.btn_copy.setProperty("class", "ghost")
        self.btn_copy.clicked.connect(self._copy_summary)
        describe(self.btn_copy, "Copy summary",
                 "Copy a plain-text summary of the verdict and every trial to "
                 "the clipboard.")
        btn_row.addWidget(self.btn_copy)
        btn_row.addStretch(1)
        self.btn_done = QPushButton("Done")
        self.btn_done.setProperty("class", "primary")
        self.btn_done.clicked.connect(self.accept)
        describe(self.btn_done, "Done", "Close the test.")
        btn_row.addWidget(self.btn_done)
        lay.addLayout(btn_row)

        return page

    def _populate_results(self) -> None:
        correct = self._session.correct if self._session is not None else 0
        total = self._session.trial if self._session is not None else 0
        n = self._session.n_candidates if self._session is not None else max(len(self._candidates), 2)
        verdict_text = abx.verdict(correct, total, n_candidates=n)
        self.lbl_verdict.setText(verdict_text)
        chance_note = f"Chance guess rate: 1 in {n}."
        if self._offset_db < _OFFSET_DISPLAY_TOLERANCE_DB:
            self.lbl_offset_result.setText(
                f"{chance_note} The candidates were already level-matched.")
        else:
            self.lbl_offset_result.setText(
                f"{chance_note} Level-match offset applied: up to {self._offset_db:.2f} dB.")

        self.table_trials.setRowCount(len(self._trial_history))
        for row, rec in enumerate(self._trial_history):
            self.table_trials.setItem(row, 0, QTableWidgetItem(str(rec["trial"])))
            self.table_trials.setItem(row, 1, QTableWidgetItem(rec["guess"]))
            self.table_trials.setItem(row, 2, QTableWidgetItem(rec["actual"]))
            self.table_trials.setItem(row, 3, QTableWidgetItem("Yes" if rec["correct"] else "No"))
        self.table_trials.resizeColumnsToContents()

        self._summary_text = self._build_summary_text(verdict_text, n)

    def _build_summary_text(self, verdict_text: str, n_candidates: int) -> str:
        names = " | ".join(name for name, _ in self._candidates)
        lines = [
            "RUMBLE Blind A/B/X Test",
            f"Comparing: {names}",
            f"{verdict_text} (chance = 1 in {n_candidates})",
            f"Level-match offset applied: up to {self._offset_db:.2f} dB",
            "",
            "Trial  Guess / Actual / Correct",
        ]
        for rec in self._trial_history:
            lines.append(
                f"{rec['trial']:>5}  {rec['guess']} / {rec['actual']} / "
                f"{'Yes' if rec['correct'] else 'No'}")
        return "\n".join(lines)

    def _copy_summary(self) -> None:
        QGuiApplication.clipboard().setText(getattr(self, "_summary_text", ""))

    # ── exit paths always restore the true active profile ───────────

    def _ensure_restored(self) -> None:
        if self._runner is not None:
            self._runner.finish()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
        self._ensure_restored()
        super().closeEvent(event)

    def reject(self) -> None:
        self._ensure_restored()
        super().reject()

    def accept(self) -> None:
        self._ensure_restored()
        super().accept()
