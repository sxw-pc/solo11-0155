import json
import unittest
from pathlib import Path

from steamcast import cast, steam, strength
from steamcast.store import connect

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def read(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class RecorderBaseline(unittest.TestCase):
    def test_reusing_normalized_time_with_a_new_id_blocks_release(self):
        case = read("recorder_resume.json")
        doc = read("steam_ok.json")
        sample = next(row for row in doc["samples"] if row["id"] == "TEMP-2")
        self.assertEqual(case["cycle_id"], sample["cycle_id"])
        self.assertEqual(case["unlinked_packet"]["celsius"], sample["celsius"])
        db = connect(":memory:")
        try:
            cast.import_data(db, read("cast_ok.json"))
            self.assertEqual("cured", steam.import_data(db, doc)[0]["state"])
            self.assertEqual(
                "released", strength.import_data(db, read("strength_ok.json"))[0]["state"]
            )
            second = dict(sample, id="TEMP-2-UNRECOGNIZED-REPLAY")
            self.assertEqual(
                "curve_gap", steam.import_data(db, {"samples": [second]})[0]["state"]
            )
            result = db.execute("SELECT state FROM release_decision").fetchone()[0]
            self.assertEqual("curing_hold", result)
        finally:
            db.close()
