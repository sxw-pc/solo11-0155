import json
from pathlib import Path

from steamcast import cast, steam, strength
from steamcast.store import connect

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def test_strength_release_precedes_any_handling_evidence():
    def read(name):
        return json.loads((FIXTURES / name).read_text(encoding="utf-8"))

    db = connect(":memory:")
    try:
        cast.import_data(db, read("cast_ok.json"))
        steam.import_data(db, read("steam_ok.json"))
        result = strength.import_data(db, read("strength_ok.json"))
        assert [(row["element_id"], row["state"]) for row in result] == [
            ("ELEMENT-1", "released")
        ]
        assert read("release_lifting_bench.json")["element"] == "ELEMENT-1"
    finally:
        db.close()
