import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from steamcast import cast, recorder, steam, strength
from steamcast.store import connect

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures"

CYCLE_ID = "CYCLE-1"
START = "2026-01-10T01:00:00"
MID = "2026-01-10T01:05:00"
END = "2026-01-10T01:10:00"
MAIN = recorder.MAIN_DEVICE
TERM = recorder.TERMINAL_DEVICE
ACTIVE_RUN = "RUN-ACTIVE"
PRIOR_RUN = "RUN-PRIOR"


def read(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def page_text(frames, banner="PAGE-BOUNDARY"):
    """合成一页原文字节：保留界标行，帧行带本地计数器/钟面/温度。"""
    lines = [banner]
    for counter, local_clock, celsius in frames:
        lines.append("FRAME|%d|%s|%s" % (counter, local_clock, celsius))
    return ("\n".join(lines) + "\n")


def base_db():
    db = connect(":memory:")
    cast.import_data(db, read("cast_ok.json"))
    steam.import_data(
        db,
        {
            "sensors": read("steam_ok.json")["sensors"],
            "cycles": read("steam_ok.json")["cycles"],
        },
    )
    return db


def active_run(offset=0):
    return {
        "id": ACTIVE_RUN,
        "role": "active",
        "start_local": "L00:00",
        "start_absolute": START,
        "clock_offset_minutes": offset,
    }


def prior_run():
    return {
        "id": PRIOR_RUN,
        "role": "prior",
        "start_local": "L00:00",
        "start_absolute": "2026-01-09T01:00:00",
        "clock_offset_minutes": 0,
    }


def fragment(celsius=41):
    # 与 fixtures/recorder_resume.json 同构：计数 1、读数 41，两个候选解释。
    return {
        "id": "FRAG-REBOOT-1",
        "cycle_id": CYCLE_ID,
        "local_counter": 1,
        "celsius": celsius,
        "explanations": [
            {"origin": "prior_run retransmission", "active_cycle_minute": None},
            {"origin": "active_run", "active_cycle_minute": 5},
        ],
    }


def auth(route):
    return {"id": "AUTH-" + route, "cycle_id": CYCLE_ID, "route": route}


def pulse(counter, absolute_at):
    return {
        "id": "PULSE-%d" % counter,
        "cycle_id": CYCLE_ID,
        "device_id": TERM,
        "local_counter": counter,
        "absolute_at": absolute_at,
    }


def states(db):
    row = db.execute("SELECT state FROM steam_decision WHERE element_id='ELEMENT-1'").fetchone()
    rel = db.execute("SELECT state FROM release_decision WHERE element_id='ELEMENT-1'").fetchone()
    return (row[0] if row else None, rel[0] if rel else None)


def usable_ats(result):
    return sorted(s["at"] for s in result["usable_samples"])


class RecorderResumeTests(unittest.TestCase):
    def test_fixture_contract_opening_and_closing(self):
        case = read("recorder_resume.json")
        self.assertEqual(case["opening"], {"absolute_minute": 0, "celsius": 40})
        self.assertEqual(case["closing"], {"absolute_minute": 10, "celsius": 42})
        self.assertEqual(case["unlinked_packet"], {"local_counter": 1, "celsius": 41})
        origins = {e["origin"]: e.get("active_cycle_minute")
                   for e in case["counter_reuse_explanations"]}
        self.assertEqual(origins["active_run"], 5)
        self.assertIsNone(origins["prior_run retransmission"])

    def test_duplicate_mail_keeps_one_page_and_preserves_bytes_and_position(self):
        db = base_db()
        try:
            raw = page_text([(0, "L00:00", 40), (2, "L00:10", 42)])
            page = {
                "id": "PAGE-MAIN-1",
                "cycle_id": CYCLE_ID,
                "device_id": MAIN,
                "run_id": ACTIVE_RUN,
                "route": recorder.READBACK,
                "raw": raw,
            }
            doc = {
                "cycle_id": CYCLE_ID,
                "authorizations": [auth(recorder.READBACK)],
                "runs": [active_run()],
                # 同一页文字节随两封邮件到达（可能乱序），接收顺序不证明发送时序。
                "pages": [dict(page), dict(page)],
            }
            result = recorder.import_data(db, doc)
            self.assertEqual(
                1, db.execute("SELECT count(*) FROM recorder_page").fetchone()[0]
            )
            self.assertEqual(
                2, db.execute("SELECT count(*) FROM recorder_frame").fetchone()[0]
            )
            stored = db.execute("SELECT raw FROM recorder_page").fetchone()[0]
            self.assertEqual(raw.encode("utf-8"), stored)
            positions = [
                (r["position"], r["local_counter"])
                for r in db.execute(
                    "SELECT position,local_counter FROM recorder_frame ORDER BY position"
                )
            ]
            self.assertEqual(positions, [(0, 0), (1, 2)])
            parsed = recorder.parse_page(stored)
            self.assertEqual([f["byte_offset"] for f in parsed], [14, 14 + 18])
            self.assertEqual([f["line"] for f in parsed],
                             ["FRAME|0|L00:00|40", "FRAME|2|L00:10|42"])
            # 重放同一份原件：幂等，裁决不漂移。
            again = recorder.import_data(db, doc)
            self.assertEqual(usable_ats(result), usable_ats(again))
        finally:
            db.close()

    def test_ambiguous_reboot_packet_stays_pending_without_new_evidence(self):
        db = base_db()
        try:
            raw = page_text([(0, "L00:00", 40), (2, "L00:10", 42)])
            result = recorder.import_data(
                db,
                {
                    "cycle_id": CYCLE_ID,
                    "authorizations": [auth(recorder.READBACK)],
                    "runs": [active_run()],
                    "pages": [
                        {
                            "id": "PAGE-MAIN-1",
                            "cycle_id": CYCLE_ID,
                            "device_id": MAIN,
                            "run_id": ACTIVE_RUN,
                            "route": recorder.READBACK,
                            "raw": raw,
                        }
                    ],
                    "fragments": [fragment()],
                },
            )
            # 计数 1 的 41 无法在“迟到重传/本轮 5 分钟新读数”间唯一化：
            # 不能按值消掉，也不能直接当成新采样。
            self.assertEqual([START, END], usable_ats(result))
            self.assertTrue(
                any(p["id"] == "PENDING/FRAG-REBOOT-1" for p in result["pending"])
            )
            self.assertTrue(all(p["next_action"] for p in result["pending"]))
            steam_state, _ = states(db)
            self.assertNotEqual("cured", steam_state)
            # 待核计划不得落成已执行事实：没有第 5 分钟样本。
            self.assertEqual(
                0,
                db.execute(
                    "SELECT count(*) FROM temp_sample WHERE at=?", (MID,)
                ).fetchone()[0],
            )
            # 即便强度试块合格，蒸养未证也只能 curing_hold。
            strength.import_data(db, read("strength_ok.json"))
            self.assertEqual("curing_hold", states(db)[1])
        finally:
            db.close()

    def test_readback_route_binds_missing_page_to_a_real_observation(self):
        db = base_db()
        try:
            first_page = {
                "id": "PAGE-MAIN-1",
                "cycle_id": CYCLE_ID,
                "device_id": MAIN,
                "run_id": ACTIVE_RUN,
                "route": recorder.READBACK,
                "raw": page_text([(0, "L00:00", 40), (2, "L00:10", 42)]),
            }
            doc1 = {
                "cycle_id": CYCLE_ID,
                "authorizations": [auth(recorder.READBACK)],
                "runs": [active_run()],
                "pages": [first_page],
                "fragments": [fragment()],
            }
            recorder.import_data(db, doc1)
            self.assertNotEqual("cured", states(db)[0])
            self.assertTrue(
                any(p["id"] == "PENDING/WINDOW/%s" % CYCLE_ID
                    for p in recorder.resolve(db, CYCLE_ID)["pending"])
            )

            # 复电后续读旧仪器缓冲：同一启动轮次、同一分页界标下取回缺页，
            # 计数器 1 的 41 被绑定为本轮第 5 分钟的实际读数。
            recovered = {
                "id": "PAGE-MAIN-2",
                "cycle_id": CYCLE_ID,
                "device_id": MAIN,
                "run_id": ACTIVE_RUN,
                "route": recorder.READBACK,
                "raw": page_text([(1, "L00:05", 41)]),
            }
            result = recorder.import_data(db, {"cycle_id": CYCLE_ID, "pages": [recovered]})
            self.assertEqual([START, MID, END], usable_ats(result))
            self.assertFalse(
                any(p["id"] == "PENDING/FRAG-REBOOT-1" for p in result["pending"])
            )
            self.assertFalse(
                any(p["id"] == "PENDING/WINDOW/%s" % CYCLE_ID
                    for p in result["pending"])
            )
            main_route = next(r for r in result["routes"] if r["device_id"] == MAIN)
            self.assertTrue(main_route["pages_complete_for_cycle"])
            strength.import_data(db, read("strength_ok.json"))
            self.assertEqual(("cured", "released"), states(db))
        finally:
            db.close()

    def test_power_loss_select_receipt_does_not_claim_next_frame_by_equal_value(self):
        db = base_db()
        try:
            doc = {
                "cycle_id": CYCLE_ID,
                "authorizations": [auth(recorder.READBACK)],
                "runs": [active_run()],
                "pages": [
                    {
                        "id": "PAGE-MAIN-1",
                        "cycle_id": CYCLE_ID,
                        "device_id": MAIN,
                        "run_id": ACTIVE_RUN,
                        "route": recorder.READBACK,
                        # 只向前窗口里，计数 2 的下一帧温度恰好也是 41。
                        "raw": page_text(
                            [(0, "L00:00", 40), (2, "L00:10", 41)]
                        ),
                    }
                ],
                "fragments": [fragment(41)],
                "selections": [
                    {
                        "id": "SELECT-1",
                        "request_ref": "FRAG-REBOOT-1",
                        "page_id": "PAGE-MAIN-1",
                        "position": 1,
                        "returned_counter": 2,
                    }
                ],
            }
            result = recorder.import_data(db, doc)
            # 温度相同也不能把下一帧当原请求的回帧：第 5 分钟依旧待核。
            self.assertEqual([START], usable_ats(result))
            self.assertTrue(
                any(p["id"] == "PENDING/FRAG-REBOOT-1" for p in result["pending"])
            )
            self.assertNotEqual("cured", states(db)[0])

            # 终端已有样本也不能默认原帧已确认：另起一个库给终端完整脉冲，
            # 原仪器待追回状态不变。
            db2 = base_db()
            try:
                term = recorder.import_data(
                    db2,
                    {
                        "cycle_id": CYCLE_ID,
                        "authorizations": [auth(recorder.TERMINAL)],
                        "common_pulses": [pulse(0, START), pulse(1, MID), pulse(2, END)],
                        "pages": [
                            {
                                "id": "PAGE-TERM-1",
                                "cycle_id": CYCLE_ID,
                                "device_id": TERM,
                                "route": recorder.TERMINAL,
                                "raw": page_text(
                                    [(0, "T00:00", 40), (1, "T00:05", 41), (2, "T00:10", 42)]
                                ),
                            }
                        ],
                        "fragments": [fragment()],
                    },
                )
                self.assertEqual([START, MID, END], usable_ats(term))
                self.assertTrue(
                    any(p["id"] == "PENDING/FRAG-REBOOT-1" for p in term["pending"])
                )
                main = next(r for r in term["routes"] if r["device_id"] == MAIN)
                self.assertFalse(main["pages_complete_for_cycle"])
            finally:
                db2.close()
        finally:
            db.close()

    def test_prior_run_originals_explain_retransmission_without_new_sample(self):
        db = base_db()
        try:
            result = recorder.import_data(
                db,
                {
                    "cycle_id": CYCLE_ID,
                    "authorizations": [auth(recorder.READBACK)],
                    "runs": [active_run(), prior_run()],
                    "pages": [
                        {
                            "id": "PAGE-MAIN-1",
                            "cycle_id": CYCLE_ID,
                            "device_id": MAIN,
                            "run_id": ACTIVE_RUN,
                            "route": recorder.READBACK,
                            "raw": page_text([(0, "L00:00", 40), (2, "L00:10", 42)]),
                        },
                        {
                            "id": "PAGE-PRIOR-1",
                            "cycle_id": CYCLE_ID,
                            "device_id": MAIN,
                            "run_id": PRIOR_RUN,
                            "route": recorder.READBACK,
                            "raw": page_text([(1, "L00:05", 41)]),
                        },
                    ],
                    "fragments": [fragment()],
                },
            )
            # 唯一存活解释是上一轮迟到重传：不产生本轮第 5 分钟新观测。
            self.assertEqual([START, END], usable_ats(result))
            self.assertFalse(
                any(p["id"] == "PENDING/FRAG-REBOOT-1" for p in result["pending"])
            )
            self.assertNotEqual("cured", states(db)[0])
        finally:
            db.close()

    def test_terminal_route_completes_cycle_but_cannot_claim_main_pages(self):
        db = base_db()
        try:
            result = recorder.import_data(
                db,
                {
                    "cycle_id": CYCLE_ID,
                    "authorizations": [auth(recorder.TERMINAL)],
                    "common_pulses": [pulse(0, START), pulse(1, MID), pulse(2, END)],
                    "pages": [
                        {
                            "id": "PAGE-TERM-1",
                            "cycle_id": CYCLE_ID,
                            "device_id": TERM,
                            "route": recorder.TERMINAL,
                            "raw": page_text(
                                [(0, "T00:00", 40), (1, "T00:05", 41), (2, "T00:10", 42)]
                            ),
                        }
                    ],
                },
            )
            self.assertEqual([START, MID, END], usable_ats(result))
            term_route = next(r for r in result["routes"] if r["device_id"] == TERM)
            main_route = next(r for r in result["routes"] if r["device_id"] == MAIN)
            self.assertTrue(term_route["pages_complete_for_cycle"])
            # 终端提供的是自己的观测，无权声称原仪器接收页已补齐。
            self.assertFalse(main_route["pages_complete_for_cycle"])
            strength.import_data(db, read("strength_ok.json"))
            self.assertEqual(("cured", "released"), states(db))
        finally:
            db.close()

    def test_terminal_missing_pulse_leaves_equal_temperature_unpaired(self):
        db = base_db()
        try:
            result = recorder.import_data(
                db,
                {
                    "cycle_id": CYCLE_ID,
                    "authorizations": [auth(recorder.TERMINAL)],
                    # 缺少正中脉冲：两个异步文件不能仅因温度相同就配对。
                    "common_pulses": [pulse(0, START), pulse(2, END)],
                    "pages": [
                        {
                            "id": "PAGE-TERM-1",
                            "cycle_id": CYCLE_ID,
                            "device_id": TERM,
                            "route": recorder.TERMINAL,
                            "raw": page_text(
                                [(0, "T00:00", 40), (1, "T00:05", 41), (2, "T00:10", 42)]
                            ),
                        }
                    ],
                },
            )
            self.assertEqual([START, END], usable_ats(result))
            self.assertNotEqual("cured", states(db)[0])
        finally:
            db.close()

    def test_reverse_branch_main_complete_terminal_missing_pulse(self):
        # 相同输入结构下，原仪器缓冲完整而独立终端缺脉冲：原仪器可完成，终端待原件。
        db = base_db()
        try:
            result = recorder.import_data(
                db,
                {
                    "cycle_id": CYCLE_ID,
                    "authorizations": [auth(recorder.READBACK), auth(recorder.TERMINAL)],
                    "runs": [active_run()],
                    "common_pulses": [pulse(0, START), pulse(2, END)],
                    "pages": [
                        {
                            "id": "PAGE-MAIN-1",
                            "cycle_id": CYCLE_ID,
                            "device_id": MAIN,
                            "run_id": ACTIVE_RUN,
                            "route": recorder.READBACK,
                            "raw": page_text(
                                [(0, "L00:00", 40), (1, "L00:05", 41), (2, "L00:10", 42)]
                            ),
                        },
                        {
                            "id": "PAGE-TERM-1",
                            "cycle_id": CYCLE_ID,
                            "device_id": TERM,
                            "route": recorder.TERMINAL,
                            "raw": page_text(
                                [(0, "T00:00", 40), (1, "T00:05", 41), (2, "T00:10", 42)]
                            ),
                        },
                    ],
                },
            )
            self.assertEqual([START, MID, END], usable_ats(result))
            main_route = next(r for r in result["routes"] if r["device_id"] == MAIN)
            term_route = next(r for r in result["routes"] if r["device_id"] == TERM)
            self.assertTrue(main_route["pages_complete_for_cycle"])
            self.assertFalse(term_route["pages_complete_for_cycle"])
            strength.import_data(db, read("strength_ok.json"))
            self.assertEqual(("cured", "released"), states(db))
        finally:
            db.close()

    def test_two_originals_same_observation_count_once_conflicts_stay_pending(self):
        db = base_db()
        try:
            result = recorder.import_data(
                db,
                {
                    "cycle_id": CYCLE_ID,
                    "authorizations": [auth(recorder.READBACK), auth(recorder.TERMINAL)],
                    "runs": [active_run()],
                    "common_pulses": [pulse(0, START), pulse(1, MID), pulse(2, END)],
                    "pages": [
                        {
                            "id": "PAGE-MAIN-1",
                            "cycle_id": CYCLE_ID,
                            "device_id": MAIN,
                            "run_id": ACTIVE_RUN,
                            "route": recorder.READBACK,
                            "raw": page_text(
                                [(0, "L00:00", 40), (1, "L00:05", 41), (2, "L00:10", 42)]
                            ),
                        },
                        {
                            "id": "PAGE-TERM-1",
                            "cycle_id": CYCLE_ID,
                            "device_id": TERM,
                            "route": recorder.TERMINAL,
                            "raw": page_text(
                                [(0, "T00:00", 40), (1, "T00:05", 41), (2, "T00:10", 42)]
                            ),
                        },
                    ],
                },
            )
            # 共同脉冲证实同一绝对时刻的同一合成观测：只计一次，但双方凭据都保留。
            self.assertEqual([START, MID, END], usable_ats(result))
            mid_obs = next(o for o in result["observations"] if o["absolute_at"] == MID)
            self.assertEqual(2, len(mid_obs["sources"]))
            self.assertEqual(
                3,
                db.execute("SELECT count(*) FROM temp_sample WHERE cycle_id=?", (CYCLE_ID,)).fetchone()[0],
            )

            # 冲突读数：同一绝对时刻出现不同温度，保留待核，不抽一条当事实。
            clash = recorder.import_data(
                db,
                {
                    "cycle_id": CYCLE_ID,
                    "common_pulses": [
                        {"id": "PULSE-1B", "cycle_id": CYCLE_ID, "device_id": TERM,
                         "local_counter": 3, "absolute_at": MID}
                    ],
                    "pages": [
                        {
                            "id": "PAGE-TERM-2",
                            "cycle_id": CYCLE_ID,
                            "device_id": TERM,
                            "route": recorder.TERMINAL,
                            "raw": page_text([(3, "T00:05", 44)]),
                        }
                    ],
                },
            )
            mid_rows = db.execute(
                "SELECT state,celsius FROM recorder_observation WHERE absolute_at=?", (MID,)
            ).fetchall()
            self.assertTrue(any(r["state"] == "conflicted" for r in mid_rows))
            self.assertTrue(any(p for p in clash["pending"]))
            # 冲突后旧的正式样本必须撤回，不能让单源旧值继续充当事实。
            self.assertEqual(
                0,
                db.execute(
                    "SELECT count(*) FROM temp_sample WHERE at=?", (MID,)
                ).fetchone()[0],
            )
            self.assertNotEqual("cured", states(db)[0])
            # 再补一份同值原件不会按值消掉冲突：44 仍在，读数继续待核；
            # 原件重放结果一致（撤回/重发幂等）。
            replayed = recorder.import_data(
                db,
                {
                    "cycle_id": CYCLE_ID,
                    "pages": [
                        {
                            "id": "PAGE-MAIN-3",
                            "cycle_id": CYCLE_ID,
                            "device_id": MAIN,
                            "run_id": ACTIVE_RUN,
                            "route": recorder.READBACK,
                            "raw": page_text([(1, "L00:05", 41)]),
                        }
                    ],
                },
            )
            self.assertEqual(
                0,
                db.execute(
                    "SELECT count(*) FROM temp_sample WHERE at=?", (MID,)
                ).fetchone()[0],
            )
            self.assertTrue(
                any(o["state"] == "conflicted" for o in replayed["observations"])
            )
            self.assertEqual(
                sorted(
                    r["state"]
                    for r in db.execute(
                        "SELECT DISTINCT state FROM recorder_observation WHERE absolute_at=?",
                        (MID,),
                    )
                ),
                ["conflicted"],
            )
        finally:
            db.close()

    def test_equal_value_independent_readings_at_distinct_times_both_survive(self):
        # 若原件容许相同数值来自两次真正读数，不能按值消掉后付也可确定的独立采集。
        db = base_db()
        try:
            run2 = {
                "id": "RUN-ACTIVE-2",
                "role": "active",
                "start_local": "L00:00",
                "start_absolute": START,
                "clock_offset_minutes": 0,
            }
            result = recorder.import_data(
                db,
                {
                    "cycle_id": CYCLE_ID,
                    "authorizations": [auth(recorder.READBACK)],
                    "runs": [active_run(), run2],
                    "pages": [
                        {
                            "id": "PAGE-MAIN-A",
                            "cycle_id": CYCLE_ID,
                            "device_id": MAIN,
                            "run_id": ACTIVE_RUN,
                            "route": recorder.READBACK,
                            # 两个不同绝对时刻、同读数 41 的独立帧。
                            "raw": page_text([(0, "L00:00", 40), (1, "L00:05", 41)]),
                        },
                        {
                            "id": "PAGE-MAIN-B",
                            "cycle_id": CYCLE_ID,
                            "device_id": MAIN,
                            "run_id": "RUN-ACTIVE-2",
                            "route": recorder.READBACK,
                            "raw": page_text([(2, "L00:10", 41)]),
                        },
                    ],
                },
            )
            same_value = [
                o for o in result["observations"] if o["celsius"] == 41
            ]
            self.assertEqual(2, len(same_value))
            self.assertEqual(
                sorted(o["absolute_at"] for o in same_value),
                ["2026-01-10T01:05:00", "2026-01-10T01:10:00"],
            )
        finally:
            db.close()

    def test_unauthorized_route_originals_never_become_samples(self):
        db = base_db()
        try:
            result = recorder.import_data(
                db,
                {
                    "cycle_id": CYCLE_ID,
                    # 未承认任何采录授权。
                    "common_pulses": [pulse(0, START), pulse(1, MID), pulse(2, END)],
                    "pages": [
                        {
                            "id": "PAGE-TERM-1",
                            "cycle_id": CYCLE_ID,
                            "device_id": TERM,
                            "route": recorder.TERMINAL,
                            "raw": page_text(
                                [(0, "T00:00", 40), (1, "T00:05", 41), (2, "T00:10", 42)]
                            ),
                        }
                    ],
                },
            )
            self.assertEqual([], usable_ats(result))
            self.assertTrue(any("authorization" in p["reason"] for p in result["pending"]))
            # 后到授权只重解受影响周期，裁决随之成立。
            later = recorder.import_data(
                db,
                {"cycle_id": CYCLE_ID, "authorizations": [auth(recorder.TERMINAL)]},
            )
            self.assertEqual([START, MID, END], usable_ats(later))
        finally:
            db.close()

    def test_clock_offset_shifts_all_frames_of_one_run_together(self):
        # 同一解释共用一套钟表偏移：偏移整体平移，不改变相邻间隔；
        # 移出周期边界的帧按 in_cycle 过滤，不强行落账。
        db = base_db()
        try:
            result = recorder.import_data(
                db,
                {
                    "cycle_id": CYCLE_ID,
                    "authorizations": [auth(recorder.READBACK)],
                    "runs": [active_run(offset=-1)],
                    "pages": [
                        {
                            "id": "PAGE-MAIN-1",
                            "cycle_id": CYCLE_ID,
                            "device_id": MAIN,
                            "run_id": ACTIVE_RUN,
                            "route": recorder.READBACK,
                            "raw": page_text(
                                [(0, "L00:00", 40), (1, "L00:05", 41), (2, "L00:10", 42)]
                            ),
                        }
                    ],
                },
            )
            # 00:59 早于周期起点被过滤；其余两帧整体平移一分钟、间隔仍为 5。
            self.assertEqual(
                ["2026-01-10T01:04:00", "2026-01-10T01:09:00"],
                usable_ats(result),
            )
            self.assertNotEqual("cured", states(db)[0])
        finally:
            db.close()

    def test_fragment_for_another_cycle_is_ignored(self):
        db = base_db()
        try:
            foreign = fragment()
            foreign["cycle_id"] = "CYCLE-OTHER"
            result = recorder.import_data(
                db,
                {
                    "cycle_id": CYCLE_ID,
                    "authorizations": [auth(recorder.READBACK)],
                    "runs": [active_run()],
                    "pages": [
                        {
                            "id": "PAGE-MAIN-1",
                            "cycle_id": CYCLE_ID,
                            "device_id": MAIN,
                            "run_id": ACTIVE_RUN,
                            "route": recorder.READBACK,
                            "raw": page_text(
                                [(0, "L00:00", 40), (1, "L00:05", 41), (2, "L00:10", 42)]
                            ),
                        }
                    ],
                    "fragments": [foreign],
                },
            )
            self.assertEqual([START, MID, END], usable_ats(result))
            self.assertFalse(
                any(p["id"] == "PENDING/FRAG-REBOOT-1" for p in result["pending"])
            )
        finally:
            db.close()

    def test_resume_sequence_replay_is_deterministic(self):
        # 后到凭据只对受影响周期重解：同一批原件按同一顺序重放，结果逐字节一致。
        def build(db):
            recorder.import_data(
                db,
                {
                    "cycle_id": CYCLE_ID,
                    "authorizations": [auth(recorder.READBACK)],
                    "runs": [active_run()],
                    "pages": [
                        {
                            "id": "PAGE-MAIN-1",
                            "cycle_id": CYCLE_ID,
                            "device_id": MAIN,
                            "run_id": ACTIVE_RUN,
                            "route": recorder.READBACK,
                            "raw": page_text([(0, "L00:00", 40), (2, "L00:10", 42)]),
                        }
                    ],
                    "fragments": [fragment()],
                },
            )
            return recorder.import_data(
                db,
                {
                    "cycle_id": CYCLE_ID,
                    "pages": [
                        {
                            "id": "PAGE-MAIN-2",
                            "cycle_id": CYCLE_ID,
                            "device_id": MAIN,
                            "run_id": ACTIVE_RUN,
                            "route": recorder.READBACK,
                            "raw": page_text([(1, "L00:05", 41)]),
                        }
                    ]
                },
            )

        db_a, db_b = base_db(), base_db()
        try:
            first = build(db_a)
            second = build(db_b)
            self.assertEqual(
                json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True)
            )
            snapshot_a = [
                dict(r)
                for r in db_a.execute(
                    "SELECT id,cycle_id,at,celsius FROM temp_sample ORDER BY at"
                )
            ]
            snapshot_b = [
                dict(r)
                for r in db_b.execute(
                    "SELECT id,cycle_id,at,celsius FROM temp_sample ORDER BY at"
                )
            ]
            self.assertEqual(snapshot_a, snapshot_b)
            # 再跑一遍同一序列不产生重复行、不改变裁决。
            build(db_a)
            self.assertEqual(
                snapshot_a,
                [
                    dict(r)
                    for r in db_a.execute(
                        "SELECT id,cycle_id,at,celsius FROM temp_sample ORDER BY at"
                    )
                ],
            )
        finally:
            db_a.close()
            db_b.close()

    def test_cli_recorder_import_replays_identically_in_audit(self):
        with tempfile.TemporaryDirectory() as d:
            db = str(Path(d) / "resume.sqlite")
            cast_doc = FIXTURES / "cast_ok.json"
            steam_doc = FIXTURES / "steam_ok.json"
            recorder_doc = Path(d) / "recorder_resume_local.json"
            recorder_doc.write_text(
                json.dumps(
                    {
                        "cycle_id": CYCLE_ID,
                        "authorizations": [auth(recorder.READBACK)],
                        "runs": [active_run()],
                        "pages": [
                            {
                                "id": "PAGE-MAIN-1",
                                "cycle_id": CYCLE_ID,
                                "device_id": MAIN,
                                "run_id": ACTIVE_RUN,
                                "route": recorder.READBACK,
                                "raw": page_text(
                                    [(0, "L00:00", 40), (1, "L00:05", 41), (2, "L00:10", 42)]
                                ),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            for cmd, path in [
                ("cast-import", cast_doc),
                ("steam-import", steam_doc),
                ("recorder-import", recorder_doc),
            ]:
                proc = subprocess.run(
                    [sys.executable, "-m", "steamcast", cmd, "--db", db, "--input", str(path)],
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)
            audit = subprocess.run(
                [sys.executable, "-m", "steamcast", "audit", "--db", db],
                cwd=ROOT,
                capture_output=True,
                text=True,
            )
            self.assertEqual(0, audit.returncode, audit.stderr)
            self.assertTrue(json.loads(audit.stdout)["ok"])


if __name__ == "__main__":
    unittest.main()
