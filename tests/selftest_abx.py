"""Tests for the blind A/B/X listening test core (rumble/core/abx.py).

Run:  python tests/selftest_abx.py

level_match(): gain-matching the candidate configs (any number, all reduced
to the quietest) before a blind test so loudness differences never masquerade
as quality differences. Uses the existing eq_math response-rendering function
(no reimplemented filter math).

Trial engine (ABXSession) and statistics (binomial_p, verdict): an open-ended
sequence of blind trials over N candidates (X is secretly one of them, chance
rate 1/N), with immediate per-answer correctness feedback and a verdict that
is meaningful after however many trials the listener chose to answer.

ABXRunner: the piece that actually switches Equalizer APO's config during
trials via ProfileManager.preview_config() and guarantees the true active
profile is restored (via ProfileManager.end_preview()) on every exit path,
including exceptions raised inside a `with ABXRunner(...):` block.

UI: rumble/ui/abx_dialog.py (ABXDialog, offscreen) — a user-chosen count
(2..every saved profile plus the synthetic Flat entry, via spin_count) of
randomly-picked candidates behind anonymous lettered listen buttons plus X,
and a dropdown of profile names to, just for fun, guess which one X is, with
immediate correct/incorrect feedback — plus the main_window.py dialog-opener's
dirty/preview/bypass refusals. QT_QPA_PLATFORM is set to "offscreen" below,
before any PySide6/Qt import, per house rule (see tests/selftest_auto_switch.py).
"""
from __future__ import annotations

import os
import random
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rumble.core import abx, apo_config
from rumble.core.profile_manager import ACTIVE_POINTER_FILENAME, PREVIEW_FILENAME, RUMBLE_SUBDIR, ProfileManager


def cfg(preamp, gain):
    return apo_config.parse_config(
        f"Preamp: {preamp} dB\nFilter 1: ON PK Fc 1000.0 Hz Gain {gain} dB Q 1.00\n")


def test_quieter_config_untouched_louder_reduced():
    a, b = cfg(-3, 0), cfg(-3, 6)          # b ~6 dB hotter mid-band
    clones, offsets = abx.level_match([a, b], 48000)
    a2, b2 = clones
    assert offsets[0] == 0.0                # quieter side untouched
    assert offsets[1] > 1.0                 # real physical value ~1.43 dB for this fixture
    assert a2.preamp == a.preamp
    assert b2.preamp < b.preamp
    assert b.preamp == cfg(-3, 6).preamp    # originals not mutated


def test_already_matched_zero_offsets():
    _, offsets = abx.level_match([cfg(-3, 2), cfg(-3, 2)], 48000)
    assert offsets == [0.0, 0.0]


def test_group_matches_all_to_quietest():
    a, b, c = cfg(-3, 0), cfg(-3, 3), cfg(-3, 6)
    clones, offsets = abx.level_match([a, b, c], 48000)
    assert offsets[0] == 0.0                       # the quietest is the reference
    assert 0.0 < offsets[1] < offsets[2]           # louder candidates reduced more
    assert clones[1].preamp < b.preamp
    assert clones[2].preamp < c.preamp


def test_none_preamp_treated_as_zero():
    """preamp=None must be treated as 0.0 dB, not raise/crash."""
    a = apo_config.parse_config("Filter 1: ON PK Fc 1000.0 Hz Gain 0 dB Q 1.00\n")
    b = apo_config.parse_config("Filter 1: ON PK Fc 1000.0 Hz Gain 6 dB Q 1.00\n")
    assert a.preamp is None and b.preamp is None
    clones, offsets = abx.level_match([a, b], 48000)
    assert offsets[1] > 1.0  # real physical value ~1.43 dB for this fixture
    assert clones[0].preamp is None  # quieter side untouched
    assert clones[1].preamp is not None and clones[1].preamp < 0.0


def test_returns_clones_not_same_objects():
    a, b = cfg(-3, 0), cfg(-3, 6)
    clones, _ = abx.level_match([a, b], 48000)
    assert clones[0] is not a
    assert clones[1] is not b


def test_binomial_p_16_of_16():
    p = abx.binomial_p(16, 16)
    assert abs(p - 1.526e-5) < 1e-3


def test_binomial_p_8_of_16():
    p = abx.binomial_p(8, 16)
    assert abs(p - 0.598) < 1e-3


def test_binomial_p_12_of_16():
    p = abx.binomial_p(12, 16)
    assert abs(p - 0.0384) < 1e-3


def test_binomial_p_one_in_five_chance():
    # 5/5 correct at a 1-in-5 chance rate: p = 0.2^5 = 3.2e-4 exactly.
    p = abx.binomial_p(5, 5, chance=0.2)
    assert abs(p - 3.2e-4) < 1e-6


def test_binomial_p_survives_huge_open_ended_totals():
    """Sessions are open-ended, so total is unbounded — the log-space
    computation must not overflow where a big-int math.comb * float would
    (math.comb(2000, 1000) is far beyond float range)."""
    p = abx.binomial_p(1030, 2000)
    assert 0.0 < p < 1.0
    assert 0.0 < abx.binomial_p(1030, 2000, chance=0.2) < p  # harder chance, smaller tail
    assert "1030/2000" in abx.verdict(1030, 2000)
    assert abx.binomial_p(2000, 2000) < 1e-300  # extreme tail stays representable
    assert abx.binomial_p(0, 2000) == 1.0


def test_session_x_assignment_varies_and_counts():
    session = abx.ABXSession(n_candidates=3, rng=random.Random(1))
    seen = set()
    for _ in range(30):
        assert 0 <= session.x_index < 3
        seen.add(session.x_index)
        session.answer(0)
    assert session.trial == 30
    assert len(seen) > 1   # not constant across trials


def test_session_answer_reports_correctness_immediately():
    session = abx.ABXSession(n_candidates=4, rng=random.Random(2))
    for _ in range(20):
        assert session.answer(session.x_index) is True   # always guess right
    assert session.correct == 20

    session2 = abx.ABXSession(n_candidates=4, rng=random.Random(2))
    for _ in range(20):
        wrong = (session2.x_index + 1) % 4
        assert session2.answer(wrong) is False           # always guess wrong
    assert session2.correct == 0


def test_session_open_ended_verdict_meaningful_any_time():
    session = abx.ABXSession(n_candidates=2, rng=random.Random(1))
    for _ in range(10):
        session.answer(0)
    assert session.trial == 10
    msg = abx.verdict(session.correct, session.trial, session.n_candidates)
    assert "/10" in msg


def test_session_requires_two_candidates():
    try:
        abx.ABXSession(n_candidates=1)
    except ValueError:
        pass
    else:
        raise AssertionError("n_candidates=1 must be rejected")


def test_verdict_all_correct_says_reliably():
    msg = abx.verdict(16, 16)
    assert "reliably" in msg
    assert "16/16" in msg


def test_verdict_9_of_16_says_guessing():
    msg = abx.verdict(9, 16)
    assert "guessing" in msg
    assert "9/16" in msg


def test_verdict_uses_candidate_count_chance_rate():
    # 5/5 at 1-in-5 chance is p=3.2e-4 -> "reliably"; the same score judged
    # at the two-candidate rate (p=0.03125) would only be "probably".
    assert "reliably" in abx.verdict(5, 5, n_candidates=5)
    assert "probably" in abx.verdict(5, 5, n_candidates=2)


# ── ABXRunner (config switching + restore guard) ─────────────────────────

class _FixedRandom:
    """A minimal rng stand-in for ABXSession — .random() always returns the
    same fixed value, so x_index is deterministic for the test."""

    def __init__(self, value: float) -> None:
        self.value = value

    def random(self) -> float:
        return self.value


def _make_manager() -> tuple[ProfileManager, Path]:
    """A temp-dir ProfileManager (temp data_dir AND config_path — house rule)
    with a real activated profile, matching the fixture pattern used by
    tests/selftest_live_tuning.py."""
    tmp = Path(tempfile.mkdtemp(prefix="rumble_abxtest_"))
    manager = ProfileManager(data_dir=tmp / "data", config_path=tmp / "config")
    manager.ensure_first_run()
    manager.create_profile("Alpha", profile_id="alpha")
    manager.activate_profile("alpha")
    return manager, tmp


def _preview_path(tmp: Path) -> Path:
    return tmp / "config" / RUMBLE_SUBDIR / PREVIEW_FILENAME


def _pointer_path(tmp: Path) -> Path:
    return tmp / "config" / ACTIVE_POINTER_FILENAME


def test_present_writes_selected_config_body():
    manager, tmp = _make_manager()
    config_a, config_b = cfg(-2, 3), cfg(-2, -3)
    runner = abx.ABXRunner(manager, [config_a, config_b], 48000)
    try:
        runner.present(0)
        preview_text = _preview_path(tmp).read_text(encoding="utf-8")
        assert apo_config.render_config_body(config_a) in preview_text
    finally:
        runner.finish()


def test_exception_in_with_block_still_restores():
    manager, tmp = _make_manager()
    pointer_path = _pointer_path(tmp)
    before = pointer_path.read_bytes()

    config_a, config_b = cfg(-2, 3), cfg(-2, -3)
    raised = False
    try:
        with abx.ABXRunner(manager, [config_a, config_b], 48000) as runner:
            runner.present(0)
            raise RuntimeError("simulated failure mid-trial")
    except RuntimeError:
        raised = True

    assert raised                      # the exception really did propagate
    assert not manager.is_previewing   # restore actually happened
    after = pointer_path.read_bytes()
    assert before == after


def test_present_x_resolves_per_session_x_index():
    manager, tmp = _make_manager()
    config_a, config_b = cfg(-2, 3), cfg(-2, -3)
    runner = abx.ABXRunner(manager, [config_a, config_b], 48000)
    try:
        session_0 = abx.ABXSession(n_candidates=2, rng=_FixedRandom(0.1))  # x_index -> 0
        assert session_0.x_index == 0
        runner.present_x(session_0)
        preview_text = _preview_path(tmp).read_text(encoding="utf-8")
        assert apo_config.render_config_body(config_a) in preview_text

        session_1 = abx.ABXSession(n_candidates=2, rng=_FixedRandom(0.9))  # x_index -> 1
        assert session_1.x_index == 1
        runner.present_x(session_1)
        preview_text = _preview_path(tmp).read_text(encoding="utf-8")
        assert apo_config.render_config_body(config_b) in preview_text
    finally:
        runner.finish()


class _StubManagerPreviewRaises:
    """A fake manager (not a real ProfileManager) whose preview_config()
    always raises, so this test isolates ABXRunner's own exception-safety
    from whatever profile_manager.py happens to do internally — per the
    reviewer's note that the restore guarantee currently rests on unmodified
    code abx.py doesn't own."""

    def __init__(self) -> None:
        self.end_preview_calls = 0

    def preview_config(self, config):
        raise RuntimeError("simulated preview failure")

    def end_preview(self) -> None:
        self.end_preview_calls += 1


def test_present_repeated_content_still_changes_bytes():
    """Regression: switching back to a side already heard (0 -> 1 -> 0) must
    still change the scratch file's bytes on disk, even though the rendered
    EQ content is byte-identical to the earlier write for that side --
    Equalizer APO's hot-reload has been observed to miss a change when a
    preview file is rewritten with content identical to a prior write in the
    same Include slot."""
    manager, tmp = _make_manager()
    config_a, config_b = cfg(-2, 3), cfg(-2, -3)
    runner = abx.ABXRunner(manager, [config_a, config_b], 48000)
    preview_path = _preview_path(tmp)
    try:
        runner.present(0)
        first = preview_path.read_bytes()
        runner.present(1)
        runner.present(0)
        second = preview_path.read_bytes()
        assert first != second
        assert apo_config.render_config_body(config_a) in preview_path.read_text(encoding="utf-8")
    finally:
        runner.finish()


def test_restore_still_runs_when_present_raises():
    stub_manager = _StubManagerPreviewRaises()
    config_a, config_b = cfg(-2, 3), cfg(-2, -3)
    raised = False
    try:
        with abx.ABXRunner(stub_manager, [config_a, config_b], 48000) as runner:
            runner.present(0)
    except RuntimeError:
        raised = True

    assert raised                                  # the exception really did propagate
    assert stub_manager.end_preview_calls == 1      # restore ran despite the raise in present()


# ── ABX dialog (UI, offscreen) ────────────────────────────────────────────

def _qapp():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _make_manager_with_beta() -> tuple[ProfileManager, Path]:
    manager, tmp = _make_manager()
    manager.create_profile("Beta", profile_id="beta")
    return manager, tmp


def _dialog(manager, rng=None):
    from rumble.ui.abx_dialog import ABXDialog
    from rumble.ui.theme.tokens import DEFAULT_THEME_NAME, get_theme
    return ABXDialog(manager, get_theme(DEFAULT_THEME_NAME), rng=rng)


def _answer(dlg, profile_name: str) -> None:
    """Answer via the dropdown the way a user would: select the item by its
    profile name, which fires QComboBox.activated (programmatic
    setCurrentIndex alone never does, by design — that's the dialog's
    reset-to-placeholder safety)."""
    combo_index = dlg.cmb_answer.findText(profile_name)
    assert combo_index > 0, f"{profile_name!r} not in the answer dropdown"
    dlg.cmb_answer.setCurrentIndex(combo_index)
    dlg.cmb_answer.activated.emit(combo_index)


def test_dialog_scripted_run_reaches_correct_verdict():
    _qapp()
    manager, tmp = _make_manager_with_beta()
    # rng 0.1: x_index = int(0.1 * 3) = 0 every trial -> always answering
    # the first-picked candidate's profile name is always correct.
    dlg = _dialog(manager, rng=_FixedRandom(0.1))
    dlg.btn_start.click()
    for _ in range(4):
        assert not dlg.cmb_answer.isEnabled()  # must hear X before answering
        dlg.btn_x.click()
        assert dlg.cmb_answer.isEnabled()
        _answer(dlg, dlg._candidates[0][0])
        assert "Correct" in dlg.lbl_feedback.text()
    dlg.btn_finish.click()
    assert dlg.stack.currentWidget() is dlg.results_page
    assert "4/4" in dlg.lbl_verdict.text()
    assert len(dlg._trial_history) == 4
    assert all(rec["correct"] for rec in dlg._trial_history)
    dlg.close()


def test_dialog_reports_incorrect_answer_immediately():
    _qapp()
    manager, tmp = _make_manager_with_beta()
    dlg = _dialog(manager, rng=_FixedRandom(0.1))  # x_index always 0
    dlg.btn_start.click()
    dlg.btn_x.click()
    _answer(dlg, dlg._candidates[1][0])  # deliberately guess a wrong profile
    assert "Incorrect" in dlg.lbl_feedback.text()
    # The feedback names the profile X actually was (candidate 0).
    assert dlg._candidates[0][0] in dlg.lbl_feedback.text()
    assert not dlg.cmb_answer.isEnabled()  # must hear X again for the next trial
    assert dlg.cmb_answer.currentIndex() == 0  # reset to the placeholder
    assert dlg._trial_history == [{
        "trial": 1,
        "guess": dlg._candidates[1][0],
        "actual": dlg._candidates[0][0],
        "correct": False,
    }]
    dlg.close()


def test_dialog_candidates_anonymous_until_answered():
    """The listen buttons are anonymous letters — which profile hides behind
    which letter is only revealed by the post-answer feedback. The answer
    dropdown lists profile names alphabetically, deliberately NOT in button
    order (a mirrored order would itself betray the letter mapping)."""
    _qapp()
    manager, tmp = _make_manager_with_beta()
    dlg = _dialog(manager, rng=_FixedRandom(0.1))
    names = [name for name, _ in dlg._candidates]
    for i, btn in enumerate(dlg.candidate_buttons):
        assert btn.text() == "ABCDE"[i]
        assert all(name not in btn.text() for name in names)
    dropdown_names = [dlg.cmb_answer.itemText(i)
                      for i in range(1, dlg.cmb_answer.count())]
    assert dropdown_names == sorted(names, key=str.lower)
    # Nothing before the first answer reveals a mapping...
    dlg.btn_start.click()
    dlg.btn_x.click()
    assert dlg.lbl_feedback.text() == ""
    # ...but the feedback after answering does reveal what X was.
    _answer(dlg, dlg._candidates[0][0])
    assert dlg._candidates[0][0] in dlg.lbl_feedback.text()
    dlg.close()


def test_dialog_close_mid_trial_restores_active_pointer():
    _qapp()
    manager, tmp = _make_manager_with_beta()
    pointer_path = _pointer_path(tmp)
    before = pointer_path.read_bytes()

    dlg = _dialog(manager, rng=_FixedRandom(0.9))
    dlg.btn_start.click()
    dlg.btn_x.click()  # mid-trial: X previewed, no answer given yet

    assert manager.is_previewing
    dlg.close()  # closeEvent must restore via ABXRunner.finish(), unconditionally

    assert not manager.is_previewing
    after = pointer_path.read_bytes()
    assert before == after


def test_dialog_has_no_trial_count_field():
    """The fixed trial count is gone: every dropdown answer is one trial and
    the listener stops whenever they choose ("Show Results")."""
    _qapp()
    manager, tmp = _make_manager_with_beta()
    dlg = _dialog(manager)
    assert not hasattr(dlg, "spin_trials")
    assert hasattr(dlg, "cmb_answer")
    assert hasattr(dlg, "btn_finish")
    dlg.close()


def test_dialog_picks_random_candidates_no_manual_picker():
    """Which specific profiles land in the candidate set is not user-choosable
    -- the dialog draws a random distinct set itself (bounded by spin_count,
    2..pool size) and just displays what it picked. How many is user-chosen,
    which profiles is not."""
    _qapp()
    manager, tmp = _make_manager_with_beta()
    dlg = _dialog(manager, rng=_FixedRandom(0.5))
    assert not hasattr(dlg, "cmb_a")
    assert not hasattr(dlg, "cmb_b")
    ids = [profile_id for _, profile_id in dlg._candidates]
    assert 2 <= len(ids) <= dlg._pool_size
    assert len(set(ids)) == len(ids)  # all distinct
    for name, _ in dlg._candidates:
        assert name in dlg.lbl_candidates.text()
    assert dlg.btn_start.isEnabled()
    # One listen button per candidate, plus the answer dropdown entries.
    assert len(dlg.candidate_buttons) == len(ids)
    assert dlg.cmb_answer.count() == len(ids) + 1  # + placeholder
    dlg.close()


def test_dialog_defaults_to_five_candidates_when_available():
    _qapp()
    manager, tmp = _make_manager_with_beta()
    for i in range(6):
        manager.create_profile(f"Extra {i}", profile_id=f"extra{i}")
    dlg = _dialog(manager, rng=_FixedRandom(0.37))
    assert dlg.spin_count.value() == 5
    assert len(dlg._candidates) == 5
    dlg.close()


def test_dialog_count_spinbox_range_and_redraw():
    """spin_count is bounded 2..pool_size (min 2, max = total available
    candidates: every saved profile plus the synthetic Flat entry) and
    changing it redraws the candidate set plus every widget derived from it
    -- listen buttons and the answer dropdown."""
    _qapp()
    manager, tmp = _make_manager_with_beta()
    for i in range(6):
        manager.create_profile(f"Extra {i}", profile_id=f"extra{i}")
    dlg = _dialog(manager, rng=_FixedRandom(0.37))
    assert dlg._pool_size == 9  # Alpha, Beta, 6 Extras, Flat
    assert dlg.spin_count.minimum() == 2
    assert dlg.spin_count.maximum() == 9

    dlg.spin_count.setValue(9)
    assert len(dlg._candidates) == 9
    assert len(dlg.candidate_buttons) == 9
    assert dlg.cmb_answer.count() == 10  # 9 candidates + placeholder
    assert dlg.candidate_buttons[-1].text() == "I"  # 9th letter

    dlg.spin_count.setValue(2)
    assert len(dlg._candidates) == 2
    assert len(dlg.candidate_buttons) == 2
    assert dlg.cmb_answer.count() == 3
    dlg.close()


def test_candidate_letters_never_collide_with_the_reserved_x_button():
    """'X' is reserved for the hidden-reference button -- with 24+ saved
    profiles (pool_size >= 25) a candidate landing on the 24th letter must
    NOT also be labeled 'X', or two QShortcuts on the same key would make
    Qt fire neither (silently breaking the X shortcut for both)."""
    _qapp()
    manager, tmp = _make_manager_with_beta()
    for i in range(23):  # + Alpha, Beta, Flat = 26 pool
        manager.create_profile(f"Extra {i}", profile_id=f"extra{i}")
    dlg = _dialog(manager, rng=_FixedRandom(0.37))
    assert dlg._pool_size == 26
    dlg.spin_count.setValue(26)
    assert len(dlg._candidates) == 26

    labels = [btn.text() for btn in dlg.candidate_buttons]
    assert "X" not in labels, f"a candidate button is labeled X: {labels!r}"
    assert len(set(labels)) == len(labels), f"duplicate candidate labels: {labels!r}"
    assert dlg.btn_x.text() == "X"

    shortcut_keys = [sc.key().toString() for sc in dlg._shortcuts]
    assert shortcut_keys.count("X") == 1, (
        f"expected exactly one 'X' shortcut (the reference button), got {shortcut_keys!r}")
    dlg.close()


def test_listen_row_scrolls_instead_of_overflowing_the_fixed_dialog():
    """The dialog is setFixedSize (non-resizable) and spin_count's upper
    bound is the whole saved-profile library, so a wide candidate row must
    live in a scroll wrapper -- not squeeze/overlap or force the dialog
    wider. Regression guard for the removed _MAX_CANDIDATES=5 cap."""
    from PySide6.QtWidgets import QScrollArea
    _qapp()
    manager, tmp = _make_manager_with_beta()
    for i in range(23):
        manager.create_profile(f"Extra {i}", profile_id=f"extra{i}")
    dlg = _dialog(manager, rng=_FixedRandom(0.37))
    dlg.spin_count.setValue(dlg._pool_size)  # 26 candidates
    assert len(dlg.candidate_buttons) == 26

    scrolls = dlg.trials_page.findChildren(QScrollArea)
    assert any(sc.widget() is btn.parentWidget() for sc in scrolls
               for btn in [dlg.candidate_buttons[0]]), (
        "candidate buttons must live inside a QScrollArea")
    fixed_w, fixed_h = dlg.width(), dlg.height()
    dlg.show()
    assert dlg.width() == fixed_w and dlg.height() == fixed_h, (
        "the dialog must stay fixed-size regardless of candidate count")
    dlg.close()


def test_dialog_count_spinbox_disabled_below_two_profiles():
    _qapp()
    tmp = Path(tempfile.mkdtemp(prefix="rumble_abxtest_"))
    manager = ProfileManager(data_dir=tmp / "data", config_path=tmp / "config")
    manager.ensure_first_run()  # no profiles saved: pool is just "Flat" alone
    dlg = _dialog(manager)
    assert not dlg.spin_count.isEnabled()
    assert not dlg.btn_start.isEnabled()
    dlg.close()


def test_dialog_random_candidates_deterministic_with_fixed_rng():
    _qapp()
    manager, tmp = _make_manager_with_beta()
    dlg1 = _dialog(manager, rng=_FixedRandom(0.5))
    dlg2 = _dialog(manager, rng=_FixedRandom(0.5))
    assert dlg1._candidates == dlg2._candidates
    dlg1.close()
    dlg2.close()


def _run_refusal_scenario(setup) -> tuple[list, list]:
    """Shared scaffold for the _open_abx_dialog refusal tests: patch
    QMessageBox.information to record instead of block, subclass ABXDialog to
    record construction, build a MainWindow, apply the test's precondition via
    ``setup(manager, win)``, call _open_abx_dialog, and restore both globals
    in a finally. Returns ``(opened, shown)`` — the refusal contract is that
    ``opened`` stays empty and ``shown`` records exactly one explanation."""
    app = _qapp()
    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QMessageBox
    from rumble.ui import main_window as mw_module
    from rumble.ui.theme.theme_engine import ThemeEngine
    from rumble.ui.theme.tokens import DEFAULT_THEME_NAME, get_theme

    manager, tmp = _make_manager_with_beta()
    theme_engine = ThemeEngine(get_theme(DEFAULT_THEME_NAME))
    theme_engine.apply(app)

    shown = []
    orig_information = QMessageBox.information
    QMessageBox.information = staticmethod(lambda *a, **k: shown.append(a) or None)

    opened = []
    orig_dialog_cls = mw_module.ABXDialog

    class _RecordingDialog(orig_dialog_cls):
        def __init__(self, *a, **k):
            opened.append(True)
            super().__init__(*a, **k)

    win = mw_module.MainWindow(manager, theme_engine, QIcon())
    mw_module.ABXDialog = _RecordingDialog
    try:
        setup(manager, win)
        win._open_abx_dialog()
    finally:
        mw_module.ABXDialog = orig_dialog_cls
        QMessageBox.information = orig_information
    return opened, shown


def test_dirty_editor_refuses_to_open_abx_dialog():
    opened, shown = _run_refusal_scenario(
        lambda manager, win: setattr(win, "_dirty", True))
    assert not opened            # dialog was never constructed
    assert shown                 # a message box explained the refusal


def test_preview_diverged_refuses_to_open_abx_dialog():
    def diverge(manager, win):
        win._dirty = False
        win._preview_diverged = True

    opened, shown = _run_refusal_scenario(diverge)
    assert not opened            # dialog was never constructed
    assert shown                 # a message box explained the refusal


def test_bypassed_eq_refuses_to_open_abx_dialog():
    """preview_config() silently no-ops while EQ is bypassed (by design), so
    every listen button would play the same unprocessed passthrough regardless
    of which candidate was requested -- the dialog must refuse to open rather
    than let the user run a test that can never show a difference."""
    opened, shown = _run_refusal_scenario(
        lambda manager, win: manager.set_bypass(True))
    assert not opened            # dialog was never constructed
    assert shown                 # a message box explained the refusal


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"PASS {name}")
