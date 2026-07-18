# tests/selftest_listener_slots.py
"""Selftest: rumble.core.listener_slots -- per-slot Test A storage, 10-run
retention, the averaged "effective" view, naming, and legacy migration."""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rumble.core import listener_slots


def test_save_and_load_round_trip():
    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        norm = {"left": {1000: 0.0}, "right": {1000: 0.0}, "imbalance": {1000: 4.5}}
        raw = {"left": {1000: -30.0}, "right": {1000: -34.5}}
        path = listener_slots.save_slot_result(data_dir, 1, norm, raw)
        assert path.exists()
        runs = listener_slots.load_slot_runs(data_dir, 1)
        assert len(runs) == 1
        assert runs[0]["norm"]["imbalance"][1000] == 4.5


def test_slots_are_independent():
    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        listener_slots.save_slot_result(
            data_dir, 1, {"left": {}, "right": {}, "imbalance": {1000: 3.0}},
            {"left": {}, "right": {}})
        listener_slots.save_slot_result(
            data_dir, 2, {"left": {}, "right": {}, "imbalance": {1000: -3.0}},
            {"left": {}, "right": {}})
        assert listener_slots.load_slot_runs(data_dir, 1)[0]["norm"]["imbalance"][1000] == 3.0
        assert listener_slots.load_slot_runs(data_dir, 2)[0]["norm"]["imbalance"][1000] == -3.0


def test_retention_caps_at_ten_runs_deleting_oldest():
    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        for i in range(12):
            listener_slots.save_slot_result(
                data_dir, 1, {"left": {}, "right": {}, "imbalance": {1000: float(i)}},
                {"left": {}, "right": {}})
        runs = listener_slots.load_slot_runs(data_dir, 1)
        assert len(runs) == listener_slots.MAX_SAVED_RUNS
        values = sorted(r["norm"]["imbalance"][1000] for r in runs)
        assert values == [float(i) for i in range(2, 12)]  # 0.0, 1.0 deleted


def test_effective_imbalance_curve_averages_saved_runs():
    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        listener_slots.save_slot_result(
            data_dir, 1, {"left": {}, "right": {}, "imbalance": {1000: 2.0, 4000: 6.0}},
            {"left": {}, "right": {}})
        listener_slots.save_slot_result(
            data_dir, 1, {"left": {}, "right": {}, "imbalance": {1000: 4.0, 4000: 2.0}},
            {"left": {}, "right": {}})
        as_dict = dict(listener_slots.effective_imbalance_curve(data_dir, 1))
        assert abs(as_dict[1000] - 3.0) < 1e-9   # (2.0 + 4.0) / 2
        assert abs(as_dict[4000] - 4.0) < 1e-9   # (6.0 + 2.0) / 2


def test_effective_imbalance_curve_empty_when_no_runs():
    with tempfile.TemporaryDirectory() as tmp:
        assert listener_slots.effective_imbalance_curve(Path(tmp), 3) == []


def test_slot_names_round_trip_and_blank_names_drop():
    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        assert listener_slots.load_slot_names(data_dir) == {}
        listener_slots.save_slot_names(data_dir, {1: "Rob", 2: "  ", 3: "Sam"})
        names = listener_slots.load_slot_names(data_dir)
        assert names == {1: "Rob", 3: "Sam"}  # slot 2's blank name is dropped


def test_migrate_legacy_hearing_history_copies_into_slot_1():
    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        legacy_dir = data_dir / "hearing" / "frequency"
        legacy_dir.mkdir(parents=True)
        payload = {"norm": {"left": {}, "right": {}, "imbalance": {1000: 5.0}},
                   "raw": {"left": {}, "right": {}}}
        (legacy_dir / "20260101T000000Z.json").write_text(json.dumps(payload), encoding="utf-8")

        migrated = listener_slots.migrate_legacy_hearing_history(data_dir)
        assert migrated == 1
        runs = listener_slots.load_slot_runs(data_dir, 1)
        assert len(runs) == 1
        assert runs[0]["norm"]["imbalance"][1000] == 5.0
        # legacy file is untouched (copy, not move)
        assert (legacy_dir / "20260101T000000Z.json").exists()


def test_migrate_legacy_hearing_history_is_a_noop_when_slot_1_already_has_data():
    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        legacy_dir = data_dir / "hearing" / "frequency"
        legacy_dir.mkdir(parents=True)
        (legacy_dir / "x.json").write_text(
            json.dumps({"norm": {"left": {}, "right": {}, "imbalance": {}},
                        "raw": {"left": {}, "right": {}}}), encoding="utf-8")
        listener_slots.save_slot_result(
            data_dir, 1, {"left": {}, "right": {}, "imbalance": {1000: 1.0}},
            {"left": {}, "right": {}})
        assert listener_slots.migrate_legacy_hearing_history(data_dir) == 0


def test_calibration_state_round_trip_and_raw_view():
    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        state = listener_slots.load_calibration_state(data_dir, 1)
        assert state["points"] == {"left": {}, "right": {}}  # blank slate
        listener_slots.record_calibration_point(state, "left", 1000, -55.0)
        listener_slots.record_calibration_point(state, "left", 16000, None)  # skip
        listener_slots.record_calibration_point(state, "right", 1000, -50.0,
                                                gave_up=False)
        listener_slots.save_calibration_state(data_dir, 1, state)

        loaded = listener_slots.load_calibration_state(data_dir, 1)
        assert loaded["points"]["left"][1000]["threshold"] == -55.0
        assert loaded["points"]["left"][16000]["threshold"] is None
        raw = listener_slots.calibration_raw(loaded)
        assert raw == {"left": {1000: -55.0, 16000: None}, "right": {1000: -50.0}}


def test_calibration_state_seeds_from_latest_run_when_absent():
    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        listener_slots.save_slot_result(
            data_dir, 1, {"left": {1000: 0.0}, "right": {1000: 0.0}, "imbalance": {}},
            {"left": {1000: -40.0}, "right": {1000: -42.0}})
        state = listener_slots.load_calibration_state(data_dir, 1)
        # In-memory seed from the latest run -- pre-dashboard listeners
        # keep their calibration -- but nothing was written to disk.
        assert state["points"]["left"][1000]["threshold"] == -40.0
        assert not (listener_slots._state_path(data_dir, 1)).exists()


def test_effective_imbalance_prefers_state_over_run_average():
    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        # Two history runs averaging to +3.0 at 1 kHz...
        for imb in (2.0, 4.0):
            listener_slots.save_slot_result(
                data_dir, 1, {"left": {}, "right": {}, "imbalance": {1000: imb}},
                {"left": {}, "right": {}})
        # ...but a working state whose raw imbalance is exactly +6.0.
        state = listener_slots.load_calibration_state(data_dir, 1)
        state["points"] = {"left": {}, "right": {}}
        listener_slots.record_calibration_point(state, "left", 1000, -44.0)
        listener_slots.record_calibration_point(state, "right", 1000, -50.0)
        listener_slots.save_calibration_state(data_dir, 1, state)
        curve = dict(listener_slots.effective_imbalance_curve(data_dir, 1))
        assert curve[1000.0] == 6.0   # the state, not the 3.0 average


def test_gave_up_point_never_reaches_the_correction():
    """A gave-up point ("not heard even at the ceiling") is a CENSORED
    observation: its stored value is the ceiling the ear failed to hear, a
    lower bound on the threshold, not a measurement. Feeding it to the
    correction would boost that ear at a frequency it cannot hear at ANY
    testable level -- and make every other frequency pay for that boost
    through the global anti-clipping preamp."""
    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        state = listener_slots.load_calibration_state(data_dir, 1)
        # 1 kHz: a real, measured 6 dB imbalance (left is the weak ear).
        listener_slots.record_calibration_point(state, "left", 1000, -44.0)
        listener_slots.record_calibration_point(state, "right", 1000, -50.0)
        # 8 kHz: the left ear never heard it, even at the -6 dB ceiling. The
        # naive imbalance here would be a whopping +44 dB.
        listener_slots.record_calibration_point(state, "left", 8000, -6.0, gave_up=True)
        listener_slots.record_calibration_point(state, "right", 8000, -50.0)
        listener_slots.save_calibration_state(data_dir, 1, state)

        raw = listener_slots.calibration_raw(state)
        assert raw["left"][8000] == -6.0            # the unfiltered record keeps it...
        measured = listener_slots.calibration_raw(state, measured_only=True)
        assert measured["left"][8000] is None       # ...the math does not
        assert 8000 in measured["left"]             # still "tested", not "never tested"
        assert measured["left"][1000] == -44.0      # real readings untouched

        curve = dict(listener_slots.effective_imbalance_curve(data_dir, 1))
        assert curve[1000.0] == 6.0                 # the genuine imbalance survives
        assert 8000.0 not in curve                  # the censored one is gone

        assert listener_slots.no_response_freqs(state) == {"left": [8000], "right": []}


def test_clear_calibration_state_removes_only_the_state_file():
    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        listener_slots.save_slot_result(
            data_dir, 1, {"left": {}, "right": {}, "imbalance": {1000: 3.0}},
            {"left": {}, "right": {}})
        state = listener_slots.load_calibration_state(data_dir, 1)
        listener_slots.save_calibration_state(data_dir, 1, state)
        listener_slots.clear_calibration_state(data_dir, 1)
        assert not listener_slots._state_path(data_dir, 1).exists()
        assert len(listener_slots.load_slot_runs(data_dir, 1)) == 1  # history kept


def test_slot_run_files_matches_load_slot_runs_order():
    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        for i in range(3):
            listener_slots.save_slot_result(
                data_dir, 1, {"left": {}, "right": {}, "imbalance": {1000: float(i)}},
                {"left": {}, "right": {}})
        paths = listener_slots.slot_run_files(data_dir, 1)
        runs = listener_slots.load_slot_runs(data_dir, 1)
        assert len(paths) == len(runs) == 3
        assert [p.suffix for p in paths] == [".json"] * 3


def test_delete_slot_runs_removes_only_named_files():
    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        for i in range(3):
            listener_slots.save_slot_result(
                data_dir, 1, {"left": {}, "right": {}, "imbalance": {1000: float(i)}},
                {"left": {}, "right": {}})
        paths = listener_slots.slot_run_files(data_dir, 1)
        listener_slots.delete_slot_runs(data_dir, 1, [paths[0].name])
        remaining = listener_slots.slot_run_files(data_dir, 1)
        assert len(remaining) == 2
        assert paths[0].name not in {p.name for p in remaining}


def test_recompile_calibration_from_runs_rolls_back_to_latest_survivor():
    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        listener_slots.save_slot_result(
            data_dir, 1, {"left": {}, "right": {}, "imbalance": {}},
            {"left": {1000: -40.0}, "right": {1000: -42.0}})
        listener_slots.save_slot_result(
            data_dir, 1, {"left": {}, "right": {}, "imbalance": {}},
            {"left": {1000: -50.0}, "right": {1000: -52.0}})
        # The working state mirrors the second (latest) run, same as any
        # completed pass leaves it.
        state = listener_slots.load_calibration_state(data_dir, 1)
        listener_slots.record_calibration_point(state, "left", 1000, -50.0)
        listener_slots.record_calibration_point(state, "right", 1000, -52.0)
        listener_slots.save_calibration_state(data_dir, 1, state)

        # Delete the latest run -- the working state should roll back to
        # the first run's values, not keep the deleted run's numbers.
        paths = listener_slots.slot_run_files(data_dir, 1)
        listener_slots.delete_slot_runs(data_dir, 1, [paths[-1].name])
        listener_slots.recompile_calibration_from_runs(data_dir, 1)

        rolled_back = listener_slots.load_calibration_state(data_dir, 1)
        assert rolled_back["points"]["left"][1000]["threshold"] == -40.0
        assert rolled_back["points"]["right"][1000]["threshold"] == -42.0


def test_recompile_calibration_from_runs_clears_state_when_no_runs_remain():
    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        listener_slots.save_slot_result(
            data_dir, 1, {"left": {}, "right": {}, "imbalance": {}},
            {"left": {1000: -40.0}, "right": {1000: -42.0}})
        state = listener_slots.load_calibration_state(data_dir, 1)
        listener_slots.record_calibration_point(state, "left", 1000, -40.0)
        listener_slots.save_calibration_state(data_dir, 1, state)

        paths = listener_slots.slot_run_files(data_dir, 1)
        listener_slots.delete_slot_runs(data_dir, 1, [p.name for p in paths])
        listener_slots.recompile_calibration_from_runs(data_dir, 1)

        assert not listener_slots._state_path(data_dir, 1).exists()
        cleared = listener_slots.load_calibration_state(data_dir, 1)
        assert cleared["points"] == {"left": {}, "right": {}}


if __name__ == "__main__":
    for n, f in sorted(globals().items()):
        if n.startswith("test_"):
            f()
            print("PASS", n)
