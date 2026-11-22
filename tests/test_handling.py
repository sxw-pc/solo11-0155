"""Executable cases for the offline dual-witness transfer-ticket check.

Simulation scope: 1D statics, three candidate points per bench, two
orientations, interval inputs.  All sizes and loads are synthetic and only
exercise the in-repo model.
"""
import copy
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from steamcast import cast, handling, steam, strength
from steamcast.store import connect

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures"


def fx(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def released_db():
    db = connect(":memory:")
    cast.import_data(db, fx("cast_ok.json"))
    steam.import_data(db, fx("steam_ok.json"))
    strength.import_data(db, fx("strength_ok.json"))
    return db


def bench_doc():
    return fx("release_lifting_bench.json")


def weigh_two_group(**over):
    doc = {
        "id": "WEIGH-1",
        "element_id": "ELEMENT-1",
        "bench_id": "BENCH-SIM-1",
        "frame": "two-hook-narrow",
        "at": "2026-01-11T08:00:00",
        "table_stopped": True,
        "witnesses": ["shift-a", "shift-b"],
        "orientation_monogram": "nameplate-toward-L",
        "scales": [
            {"id": "SCALE-L", "position": 0,
             "certified_from": "2026-01-01T00:00:00",
             "certified_until": "2026-02-01T00:00:00", "load": [7.4, 7.6]},
            {"id": "SCALE-R", "position": 4,
             "certified_from": "2026-01-01T00:00:00",
             "certified_until": "2026-02-01T00:00:00", "load": [4.4, 4.6]},
        ],
    }
    doc.update(over)
    return doc


def frame_trial(**over):
    doc = {
        "id": "TRIAL-1",
        "element_id": "ELEMENT-1",
        "bench_id": "BENCH-SIM-1",
        "frame": "wide-balanced",
        "orientation": "nameplate-toward-L",
        "pair": ["L", "M"],
        "at": "2026-01-11T09:00:00",
        "witnesses": ["shift-a", "shift-b"],
        "envelope": {"L": [5.8, 6.2], "M": [5.8, 6.2]},
    }
    doc.update(over)
    return doc


def late_specimen(**over):
    doc = {"id": "SPEC-LATE", "element_id": "ELEMENT-1", "cast_id": "CAST-1",
           "cycle_id": "CYCLE-1", "lab_id": "LAB-1",
           "at": "2026-01-12T00:00:00", "strength_mpa": 35, "kind": "retest"}
    doc.update(over)
    return doc


def ticket(db, element_id="ELEMENT-1"):
    row = db.execute("SELECT * FROM handling_ticket WHERE element_id=?",
                     (element_id,)).fetchone()
    return handling.public(row) if row else None


def issued_db():
    """Released element with a live issued ticket on the bench."""
    db = released_db()
    handling.import_data(db, bench_doc())
    handling.import_data(db, {"weigh_stops": [weigh_two_group()]})
    return db


# ---------------------------------------------------------------------
# released alone cannot claim a pair
# ---------------------------------------------------------------------

def test_released_alone_cannot_claim_a_pair():
    db = released_db()
    try:
        result = handling.import_data(db, bench_doc())
        t = result[0]
        # the element is released, yet no fixed pair may be committed
        assert t["state"] == "test_request"
        assert db.execute(
            "SELECT state,version FROM release_decision "
            "WHERE element_id='ELEMENT-1'").fetchone()[:] == ("released", 1)
        detail = t["detail"]
        assert detail["workstation"] == "BENCH-7"
        # each candidate position has some working pair, but no single pair
        # covers the whole reported interval
        bench = bench_doc()
        assert handling.working_pairs_at(bench, "nameplate-toward-L", 1.5) == [["L", "M"]]
        assert handling.working_pairs_at(bench, "nameplate-toward-L", 2.5) == [["M", "R"]]
        assert [s["working_pair"] for s in bench["center_scenarios"]] == [
            ["L", "M"], ["M", "R"]]
        # the naive "best average position" pick (L,R) is fine at x=2.0 ...
        assert handling.reactions(12, 0, 4, 2.0) == (6.0, 6.0)
        # ... but the same counterexample positions that block issuance are
        # reported: (L,R) exceeds its threshold at the interval ends
        assert {"orientation": "nameplate-toward-L", "pair": ["L", "R"],
                "x": 1.5, "point": "L", "problem": "over_limit",
                "reaction": 7.5, "limit": 6} in detail["blocking"]
        assert {"orientation": "nameplate-toward-L", "pair": ["L", "M"],
                "x": 2.5, "point": "M", "problem": "over_limit",
                "reaction": 15.0, "limit": 10} in detail["blocking"]
        assert any(b["problem"] == "unloaded" for b in detail["blocking"])
        # both follow-up means are offered, and each request explains the
        # same counterexample position it would eliminate
        kinds = {r["kind"] for r in detail["requests"]}
        assert kinds == {"weigh_stop", "frame_trial"}
        req = next(r for r in detail["requests"]
                   if r["kind"] == "weigh_stop"
                   and r["orientation"] == "nameplate-toward-L"
                   and r["pair"] == ["L", "M"])
        assert req["pins_orientation"] is True
        assert req["resolves"]
        for entry in req["resolves"]:
            assert entry in detail["blocking"]
        assert any(e["x"] == 2.5 for e in req["resolves"])
    finally:
        db.close()


def test_two_calibrated_scale_groups_issue_fixed_ticket():
    db = released_db()
    try:
        handling.import_data(db, bench_doc())
        result = handling.import_data(db, {"weigh_stops": [weigh_two_group()]})
        t = result[0]
        assert t["state"] == "ticket_issued"
        # the ticket commits to exactly one pair and one witnessed orientation
        assert (t["frame"], t["orientation"]) == ("two-hook-narrow",
                                                  "nameplate-toward-L")
        assert (t["point_a"], t["point_b"]) == ("L", "M")
        # it references the still-valid release version and the workstation
        assert t["release_version"] == 1
        assert t["bench_version"] == "BENCH-SIM-1"
        assert t["detail"]["workstation"] == "BENCH-7"
        assert t["detail"]["evidence"] == ["WEIGH-1"]
        assert t["detail"]["cog_range"] == [1.5, 1.559322]
        version = t["version"]
        # duplicate import is idempotent
        handling.import_data(db, {"weigh_stops": [weigh_two_group()]})
        assert ticket(db)["version"] == version
        assert handling.recheck(db, "ELEMENT-1")["verdict"] == "still_valid"
    finally:
        db.close()


def test_single_total_weight_cannot_clear_flip_candidates():
    db = released_db()
    try:
        handling.import_data(db, bench_doc())
        single = weigh_two_group(
            scales=[{"id": "SCALE-T", "position": 2,
                     "certified_from": "2026-01-01T00:00:00",
                     "certified_until": "2026-02-01T00:00:00",
                     "load": [11.9, 12.1]}])
        result = handling.import_data(db, {"weigh_stops": [single]})
        t = result[0]
        # a lone total weight is a valid consistency check but eliminates
        # neither the left/right flip candidates nor the centre interval
        assert t["state"] == "test_request"
        assert t["detail"]["orientations_possible"] == [
            "nameplate-toward-L", "nameplate-toward-R"]
        assert t["detail"]["cog_ranges"]["nameplate-toward-L"] == [1.5, 2.5]
        assert all(r["pins_orientation"] for r in t["detail"]["requests"])
    finally:
        db.close()


def test_wide_frame_trial_certifies_when_weighbridge_broken():
    db = released_db()
    try:
        handling.import_data(db, bench_doc())
        broken = weigh_two_group()
        broken["scales"][0]["certified_until"] = "2026-01-01T00:00:00"
        result = handling.import_data(
            db, {"weigh_stops": [broken], "frame_trials": [frame_trial()]})
        t = result[0]
        # the expired weighbridge cannot narrow the centre, but the balanced
        # wide-frame trial certifies exactly the tested configuration
        assert t["state"] == "ticket_issued"
        assert t["frame"] == "wide-balanced"
        assert t["orientation"] == "nameplate-toward-L"
        assert (t["point_a"], t["point_b"]) == ("L", "M")
        assert t["detail"]["evidence"] == ["TRIAL-1"]
    finally:
        db.close()


def test_trial_envelope_not_lendable_and_must_cover_config():
    db = released_db()
    try:
        handling.import_data(db, bench_doc())
        # envelope over the wide-frame limit: trial is inadmissible
        over = frame_trial(id="TRIAL-OVER",
                           envelope={"L": [5.8, 6.2], "M": [12.4, 12.8]})
        result = handling.import_data(db, {"frame_trials": [over]})
        assert result[0]["state"] == "test_request"
        # an unbalanced envelope contradicts the balancing frame
        skewed = frame_trial(id="TRIAL-SKEWED",
                             envelope={"L": [2.0, 3.0], "M": [9.0, 10.0]})
        result = handling.import_data(db, {"frame_trials": [skewed]})
        assert result[0]["state"] == "test_request"
        # a valid trial certifies only its own frame/orientation/pair: the
        # issued ticket is the wide frame, never the old narrow frame
        result = handling.import_data(db, {"frame_trials": [frame_trial()]})
        assert result[0]["state"] == "ticket_issued"
        assert result[0]["frame"] == "wide-balanced"
    finally:
        db.close()


def test_orientation_mirroring_and_nameplate_limits():
    # mirrored orientation maps the reported interval onto the table
    db = released_db()
    try:
        bench = bench_doc()
        bench["reported_center_range"] = [1.0, 1.4]
        handling.import_data(db, bench)
        weigh = weigh_two_group(
            orientation_monogram="nameplate-toward-R",
            scales=[{"id": "SCALE-L", "position": 0,
                     "certified_from": "2026-01-01T00:00:00",
                     "certified_until": "2026-02-01T00:00:00",
                     "load": [3.5, 3.7]},
                    {"id": "SCALE-R", "position": 4,
                     "certified_from": "2026-01-01T00:00:00",
                     "certified_until": "2026-02-01T00:00:00",
                     "load": [8.3, 8.5]}])
        t = handling.import_data(db, {"weigh_stops": [weigh]})[0]
        # measured on-table centre ~2.8 only fits the mirrored orientation
        assert t["state"] == "ticket_issued"
        assert t["orientation"] == "nameplate-toward-R"
        assert (t["point_a"], t["point_b"]) == ("M", "R")
    finally:
        db.close()
    # same measured loads, different witnessed orientation: the nameplate
    # limits of the flipped element reject the pair the forward one accepts
    db = released_db()
    try:
        bench = bench_doc()
        bench["orientation_limit"]["nameplate-toward-R"] = {
            "L": 5, "M": 8, "R": 6}
        handling.import_data(db, bench)
        weigh = weigh_two_group(orientation_monogram="nameplate-toward-R")
        t = handling.import_data(db, {"weigh_stops": [weigh]})[0]
        assert t["state"] == "test_request"
    finally:
        db.close()
    db = released_db()
    try:
        bench = bench_doc()
        bench["orientation_limit"]["nameplate-toward-R"] = {
            "L": 5, "M": 8, "R": 6}
        handling.import_data(db, bench)
        t = handling.import_data(db, {"weigh_stops": [weigh_two_group()]})[0]
        assert t["state"] == "ticket_issued"
        assert t["orientation"] == "nameplate-toward-L"
    finally:
        db.close()


# ---------------------------------------------------------------------
# lifecycle: issuance -> claim -> lift-off, freeze, post-hoc check
# ---------------------------------------------------------------------

def test_unclaimed_ticket_freezes_when_release_revised():
    db = issued_db()
    try:
        assert ticket(db)["state"] == "ticket_issued"
        strength.import_data(db, {"specimens": [late_specimen()]})
        t = ticket(db)
        assert t["state"] == "frozen"
        assert "release" in t["reason"]
        assert db.execute("SELECT state FROM release_decision "
                          "WHERE element_id='ELEMENT-1'").fetchone()[0] == \
            "strength_hold"
        rc = handling.recheck(db, "ELEMENT-1")
        assert rc["verdict"] == "not_currently_valid"
        assert rc["config"]["pair"] == ["L", "M"]
    finally:
        db.close()


def test_release_revision_that_still_certifies_only_refreshes_basis():
    db = issued_db()
    try:
        version = ticket(db)["version"]
        ok_retest = late_specimen(id="SPEC-OK", at="2026-01-11T12:00:00",
                                  strength_mpa=42.5)
        strength.import_data(db, {"specimens": [ok_retest]})
        assert db.execute("SELECT state,version FROM release_decision "
                          "WHERE element_id='ELEMENT-1'").fetchone()[:] == \
            ("released", 2)
        t = ticket(db)
        assert t["state"] == "ticket_issued"
        assert (t["point_a"], t["point_b"]) == ("L", "M")  # pair never moved
        assert t["release_version"] == 2
        assert t["version"] > version
    finally:
        db.close()


def test_bench_revision_freezes_then_new_evidence_reissues():
    db = issued_db()
    try:
        revised = bench_doc()
        revised["fixture_version"] = "BENCH-SIM-2"
        handling.import_data(db, revised)
        t = ticket(db)
        # the old weighing is bound to the superseded bench revision
        assert t["state"] == "frozen"
        assert "BENCH-SIM-1 -> BENCH-SIM-2" in t["reason"]
        result = handling.import_data(
            db, {"weigh_stops": [weigh_two_group(id="WEIGH-2",
                                                 bench_id="BENCH-SIM-2")]})
        t = result[0]
        assert t["state"] == "ticket_issued"
        assert t["bench_version"] == "BENCH-SIM-2"
        assert t["detail"]["evidence"] == ["WEIGH-2"]
    finally:
        db.close()


def test_claim_move_and_moved_ticket_is_never_rewritten():
    db = issued_db()
    try:
        v = ticket(db)["version"]
        handling.import_data(db, {"events": [
            {"id": "CLAIM-1", "element_id": "ELEMENT-1", "kind": "claim",
             "ticket_version": v, "at": "2026-01-11T09:00:00",
             "by": "BENCH-7"}]})
        t = ticket(db)
        assert t["state"] == "claimed"
        assert t["detail"]["claim_event"] == "CLAIM-1"
        v = t["version"]
        handling.import_data(db, {"events": [
            {"id": "MOVE-1", "element_id": "ELEMENT-1", "kind": "move",
             "ticket_version": v, "at": "2026-01-11T09:30:00",
             "by": "BENCH-7"}]})
        t = ticket(db)
        assert t["state"] == "moved"
        assert (t["frame"], t["point_a"], t["point_b"]) == (
            "two-hook-narrow", "L", "M")
        # a later release revision must not forge the moved ticket back
        strength.import_data(db, {"specimens": [late_specimen()]})
        t = ticket(db)
        assert t["state"] == "moved"
        assert db.execute("SELECT state FROM release_decision "
                          "WHERE element_id='ELEMENT-1'").fetchone()[0] == \
            "strength_hold"
        # post-hoc check carries the specific element versions
        rc = handling.recheck(db, "ELEMENT-1")
        assert rc["verdict"] == "moved_as_recorded"
        assert rc["release_version_on_ticket"] == 1
        assert rc["release_version_now"] == 2
        assert rc["bench_version_on_ticket"] == "BENCH-SIM-1"
        assert rc["config"] == {"frame": "two-hook-narrow",
                                "orientation": "nameplate-toward-L",
                                "pair": ["L", "M"]}
    finally:
        db.close()


# ---------------------------------------------------------------------
# gating, evidence identity, replay
# ---------------------------------------------------------------------

def test_no_valid_release_means_no_ticket():
    db = connect(":memory:")
    try:
        result = handling.import_data(db, bench_doc())
        assert result[0]["state"] == "issuance_blocked"
        assert "release" in result[0]["reason"]
        cast.import_data(db, fx("cast_ok.json"))
        steam.import_data(db, fx("steam_ok.json"))
        strength.import_data(db, fx("strength_bad.json"))
        assert ticket(db)["state"] == "issuance_blocked"
        assert db.execute("SELECT state FROM release_decision "
                          "WHERE element_id='ELEMENT-1'").fetchone()[0] == \
            "strength_hold"
    finally:
        db.close()


def test_impossible_bench_stops_issuance():
    db = released_db()
    try:
        bench = bench_doc()
        bench["reported_center_range"] = [3.0, 1.0]  # empty interval
        result = handling.import_data(db, bench)
        t = result[0]
        # the broken interval blocks every analytical commitment; only the
        # empirical wide-frame trial remains as a follow-up
        assert t["state"] == "test_request"
        assert any(b["problem"] == "empty_range"
                   for b in t["detail"]["blocking"])
        assert {r["kind"] for r in t["detail"]["requests"]} == {"frame_trial"}
    finally:
        db.close()
    db = released_db()
    try:
        bench = bench_doc()
        bench["support_limit"] = {"L": 1, "M": 1, "R": 1}
        bench["orientation_limit"] = {
            "nameplate-toward-L": {"L": 1, "M": 1, "R": 1},
            "nameplate-toward-R": {"L": 1, "M": 1, "R": 1}}
        bench["wide_frame"] = {"id": "wide-balanced",
                               "point_limit": {"L": 1, "M": 1, "R": 1}}
        result = handling.import_data(db, bench)
        # no follow-up observation can ever certify a fixed configuration
        assert result[0]["state"] == "issuance_blocked"
        assert result[0]["detail"]["requests"] == []
    finally:
        db.close()


def test_duplicate_correction_and_conflict():
    db = issued_db()
    try:
        before = db.execute("SELECT count(*) FROM evidence").fetchone()[0]
        handling.import_data(db, bench_doc())  # duplicate original record
        handling.import_data(db, {"weigh_stops": [weigh_two_group()]})
        assert db.execute("SELECT count(*) FROM evidence").fetchone()[0] == \
            before
        assert ticket(db)["state"] == "ticket_issued"
        # a corrected record under the same id conflicts and rolls back
        corrupted = bench_doc()
        corrupted["load_units"] = 13
        with pytest.raises(ValueError):
            handling.import_data(db, corrupted)
        assert ticket(db)["state"] == "ticket_issued"
    finally:
        db.close()


def test_cli_import_report_and_audit_replay():
    with tempfile.TemporaryDirectory() as d:
        db_path = str(Path(d) / "state.sqlite")

        def run(*args):
            p = subprocess.run(
                [sys.executable, "-B", "-m", "steamcast", *args,
                 "--db", db_path],
                cwd=ROOT, capture_output=True, text=True)
            assert p.returncode == 0, p.stderr
            return p.stdout

        for cmd, name in [("cast-import", "cast_ok.json"),
                          ("steam-import", "steam_ok.json"),
                          ("strength-import", "strength_ok.json")]:
            run(cmd, "--input", str(FIXTURES / name))
        # the raw bench fixture is itself a valid handling-import input
        out = run("handling-import", "--input",
                  str(FIXTURES / "release_lifting_bench.json"))
        assert json.loads(out)[0]["state"] == "test_request"
        weigh_file = Path(d) / "weigh.json"
        weigh_file.write_text(json.dumps(
            {"weigh_stops": [weigh_two_group()]}), encoding="utf-8")
        out = run("handling-import", "--input", str(weigh_file))
        assert json.loads(out)[0]["state"] == "ticket_issued"
        report = json.loads(run("report"))
        element = next(e for e in report if e["element_id"] == "ELEMENT-1")
        assert element["release"]["state"] == "released"
        assert element["handling"]["state"] == "ticket_issued"
        assert element["handling"]["release_version"] == 1
        assert element["handling_recheck"]["verdict"] == "still_valid"
        # audit replays the original records to the same business output
        assert json.loads(run("audit"))["ok"] is True


def test_old_release_demo_unchanged_by_handling():
    with tempfile.TemporaryDirectory() as d:
        db_path = str(Path(d) / "demo.sqlite")
        p = subprocess.run([sys.executable, "-B", "-m", "steamcast", "demo",
                            "--db", db_path], cwd=ROOT,
                           capture_output=True, text=True)
        assert p.returncode == 0, p.stderr
        element = json.loads(p.stdout)[0]
        assert (element["cast"]["state"], element["steam"]["state"],
                element["release"]["state"]) == (
                    "cast_accepted", "cured", "released")
        assert element["handling"] is None
        p = subprocess.run([sys.executable, "-B", "-m", "steamcast", "audit",
                            "--db", db_path], cwd=ROOT,
                           capture_output=True, text=True)
        assert json.loads(p.stdout)["ok"] is True
