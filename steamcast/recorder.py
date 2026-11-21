"""离线采集机分页原件的采录恢复。

一份邮件是一次接收，不等于一次读数：相同字节可能是同一读数的重传，也可能是
另一轮记录器对同一温度的独立采集。本模块只按分页原件（原始页文字节、页内位置、
启动凭据/共同脉冲凭据）恢复观测身份，绝不按温度数值合并、也不按接收顺序推断
发送时序。

两条经授权的恢复路线（授权在拿到结论之前显式声明）：

* readback（原仪器记录器回读）：锁定原仪器启动凭据与分页界标，续读旧仪器缓冲，
  能把缺页/重复页明确绑定到实际读数身份。
* terminal（随炉独立记录终端）：凭共同机台脉冲与可验证采样时间提供自己的观测，
  可恢复该周期的有效观测，但无权声称原仪器的接收页已补齐。
"""

import json
from datetime import datetime, timedelta

from .source import record
from . import steam

PAGE_KIND = "recorder_page"
RUN_KIND = "recorder_run"
PULSE_KIND = "recorder_pulse"
FRAGMENT_KIND = "recorder_fragment"
SELECT_KIND = "recorder_select"
AUTH_KIND = "recorder_authorization"

READBACK = "readback"
TERMINAL = "terminal"
MAIN_DEVICE = "main-recorder"
TERMINAL_DEVICE = "furnace-terminal"


def _dt(value):
    return datetime.fromisoformat(value)


def _minutes(a, b):
    return (_dt(b) - _dt(a)).total_seconds() / 60.0


def _local_minutes(value):
    """解析本地钟面（合成格式 LHH:MM 或 ISO 串）为相对分钟。"""
    text = value[1:] if value and not value[0].isdigit() else value
    if "T" in text:
        text = text.split("T", 1)[1]
    hour, minute = text.split(":")[:2]
    return int(hour) * 60 + int(minute)


def _add_minutes(value, delta):
    return (_dt(value) + timedelta(minutes=delta)).isoformat()


def _store_once(db, table, key_col, key, cols, values):
    """幂等插入；同键不同内容即原件身份冲突。"""
    old = db.execute(
        "SELECT * FROM %s WHERE %s=?" % (table, key_col), (key,)
    ).fetchone()
    if old is not None:
        for col, val in zip(cols, values):
            if old[col] != val:
                raise ValueError("recorder identity conflict: " + str(key))
        return False
    db.execute(
        "INSERT INTO %s(%s,%s) VALUES(%s)"
        % (table, key_col, ",".join(cols), ",".join("?" * (len(cols) + 1))),
        (key, *values),
    )
    return True


def parse_page(raw):
    """解析一页原文字节，保留页内位置（帧序号与字节偏移）。

    原件为本地合成文本：每行一帧 ``FRAME|counter|local_clock|celsius``，
    非 FRAME 行（分页界标等）原样保留、不产生帧。
    """
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    frames = []
    offset = 0
    position = 0
    for line in raw.splitlines(keepends=True):
        text = line.decode("utf-8")
        stripped = text.rstrip("\r\n")
        start = offset
        offset += len(line)
        if not stripped.startswith("FRAME|"):
            continue
        parts = stripped.split("|")
        if len(parts) != 4:
            raise ValueError("malformed recorder frame: " + stripped)
        _, counter, local_clock, celsius = parts
        frames.append(
            {
                "position": position,
                "byte_offset": start,
                "line": stripped,
                "local_counter": int(counter),
                "local_clock": local_clock,
                "celsius": float(celsius),
            }
        )
        position += 1
    return frames


def _parse_fragment(item):
    return {
        "id": item["id"],
        "cycle_id": item.get("cycle_id"),
        "local_counter": int(item["local_counter"]),
        "celsius": float(item["celsius"]),
        "explanations": [
            {
                "origin": ex["origin"],
                "active_cycle_minute": ex.get("active_cycle_minute"),
            }
            for ex in item.get("explanations", ())
        ],
    }


def import_data(db, data):
    """接收一批分页原件与凭据，重解受影响周期，再走原有两个裁决。"""
    cycle_id = data["cycle_id"]
    with db:
        # 授权必须在拿到结论之前声明；先落证据，解析阶段才读取。
        for auth in data.get("authorizations", ()):
            record(db, AUTH_KIND, auth)
        for run in data.get("runs", ()):
            record(db, RUN_KIND, run)
        for pulse in data.get("common_pulses", ()):
            record(db, PULSE_KIND, pulse)
        for frag in data.get("fragments", ()):
            record(db, FRAGMENT_KIND, frag)
        for sel in data.get("selections", ()):
            record(db, SELECT_KIND, sel)

        # 同一页文字节重复接收（重传邮件）：原件身份相同，只登记一次接收。
        # 接收顺序只说明到达次序，不证明发送时序。
        existing = dict(
            db.execute("SELECT id,received_seq FROM recorder_page")
        )
        next_seq = (max(existing.values()) + 1) if existing else 1
        seen = set(existing)
        for mail in data.get("pages", ()):
            page_id = mail["id"]
            raw = mail["raw"]
            if isinstance(raw, str):
                raw = raw.encode("utf-8")
            record(
                db,
                PAGE_KIND,
                {
                    "id": page_id,
                    "cycle_id": mail["cycle_id"],
                    "device_id": mail["device_id"],
                    "run_id": mail.get("run_id"),
                    "route": mail["route"],
                    "raw": raw.decode("utf-8"),
                },
            )
            if page_id in seen:
                continue
            seen.add(page_id)
            _store_once(
                db,
                "recorder_page",
                "id",
                page_id,
                ["cycle_id", "device_id", "run_id", "route", "raw", "received_seq"],
                [
                    mail["cycle_id"],
                    mail["device_id"],
                    mail.get("run_id"),
                    mail["route"],
                    raw,
                    next_seq,
                ],
            )
            next_seq += 1
            for fr in parse_page(raw):
                _store_once(
                    db,
                    "recorder_frame",
                    "id",
                    "%s/F%02d" % (page_id, fr["position"]),
                    ["page_id", "position", "local_counter", "local_clock", "celsius"],
                    [
                        page_id,
                        fr["position"],
                        fr["local_counter"],
                        fr["local_clock"],
                        fr["celsius"],
                    ],
                )

        result = resolve(db, cycle_id)
        element_id = result["element_id"]
        # 后到凭据只对受影响周期重新调用现有裁决；原件重放结果一致。
        result["decision"] = steam.decide(db, element_id) if element_id else None
        return result


def _load_evidence(db, kind):
    return [
        json.loads(r["raw"])
        for r in db.execute(
            "SELECT raw FROM evidence WHERE kind=? ORDER BY id", (kind,)
        )
    ]


def _window_ok(observations, cycle):
    """两个邻接窗只能在同一相容解释里补齐：相邻观测间隔为正且不超最大间隔。"""
    ordered = sorted(observations, key=lambda o: o["absolute_at"])
    return all(
        0 < _minutes(a["absolute_at"], b["absolute_at"]) <= cycle["max_gap_minutes"]
        for a, b in zip(ordered, ordered[1:])
    )


def resolve(db, cycle_id):
    authorizations = _load_evidence(db, AUTH_KIND)
    runs = {r["id"]: r for r in _load_evidence(db, RUN_KIND)}
    pulses = _load_evidence(db, PULSE_KIND)
    fragments = [_parse_fragment(x) for x in _load_evidence(db, FRAGMENT_KIND)]
    selections = _load_evidence(db, SELECT_KIND)

    cycle = db.execute(
        "SELECT * FROM steam_cycle WHERE id=?", (cycle_id,)
    ).fetchone()
    routes = {a["route"] for a in authorizations if a.get("cycle_id") == cycle_id}
    readback_ok = READBACK in routes

    pages = db.execute(
        "SELECT * FROM recorder_page WHERE cycle_id=? ORDER BY id", (cycle_id,)
    ).fetchall()
    frames = {}
    for page in pages:
        for fr in db.execute(
            "SELECT * FROM recorder_frame WHERE page_id=? ORDER BY position",
            (page["id"],),
        ):
            frames[fr["id"]] = {"frame": fr, "page": page}

    # 共同机台脉冲：把终端本地帧对齐到绝对时刻。两个异步文件只在有签认共同
    # 脉冲时才能配对，温度相同本身不构成配对。
    pulse_index = {(p["device_id"], p["local_counter"]): p for p in pulses}
    prior_run_ids = {rid for rid, r in runs.items() if r.get("role") == "prior"}
    active_run_id = next(
        iter(sorted(rid for rid, r in runs.items() if r.get("role") == "active")),
        next(iter(sorted(runs)), None),
    )

    def frame_absolute(frame_id):
        fr = frames[frame_id]["frame"]
        page = frames[frame_id]["page"]
        if page["route"] == READBACK:
            # 同一解释只有一个启动轮次与一套钟表偏移。
            run = runs[page["run_id"]]
            return _add_minutes(
                run["start_absolute"],
                _local_minutes(fr["local_clock"]) - _local_minutes(run["start_local"])
                + run.get("clock_offset_minutes", 0),
            )
        pulse = pulse_index.get((page["device_id"], fr["local_counter"]))
        return pulse["absolute_at"] if pulse else None

    # 点选回执：只向前的一次读取窗口，重复点选取出下一帧（温度可能相同）。
    # 回执以 (page, position) 锁定实际返回帧；它回答不了原请求时，该下一帧
    # 不能当回原请求，也不能脱离请求自行锚定为一条普通观测。
    select_by_request = {s["request_ref"]: s for s in selections}
    frame_by_page_pos = {
        (b["frame"]["page_id"], b["frame"]["position"]): frame_id
        for frame_id, b in frames.items()
    }
    answered_frames = set()
    receipt_bound = set()
    for request_ref, receipt in select_by_request.items():
        returned = frame_by_page_pos.get((receipt["page_id"], receipt["position"]))
        if returned is None:
            continue
        receipt_bound.add(returned)
        rb = frames[returned]["frame"]
        request_frag = next((f for f in fragments if f["id"] == request_ref), None)
        if (
            request_frag is not None
            and rb["local_counter"] == request_frag["local_counter"]
            and rb["celsius"] == request_frag["celsius"]
        ):
            answered_frames.add(returned)

    # 已锚定的实际观测候选。授权在结论之前声明：未承认的路线只保留原件，
    # 其帧不能成为可用样本；被点选动作占用、却未回答任何请求的下一帧也不在
    # 此自行落账（温度相同也不行）。
    candidates = []
    unauthorized = []
    pending = []
    for frame_id in sorted(frames):
        bundle = frames[frame_id]
        if bundle["page"]["route"] not in routes:
            unauthorized.append(frame_id)
            continue
        if frame_id in receipt_bound and frame_id not in answered_frames:
            continue
        absolute_at = frame_absolute(frame_id)
        if absolute_at is None:
            continue
        candidates.append(
            {
                "frame_id": frame_id,
                "fragment_id": None,
                "route": bundle["page"]["route"],
                "device_id": bundle["page"]["device_id"],
                "run_id": bundle["page"]["run_id"],
                "absolute_at": absolute_at,
                "celsius": bundle["frame"]["celsius"],
            }
        )
    if unauthorized:
        pending.append(
            {
                "id": "PENDING/%s/AUTH" % cycle_id,
                "cycle_id": cycle_id,
                "reason": "paged originals present from a route whose recording "
                "authorization is not recognized",
                "next_action": "declare the recording authorization for the route "
                "before its observations can be used",
            }
        )

    def in_cycle(at):
        return cycle is not None and cycle["start"] <= at <= cycle["end"]

    confirmed = list(candidates)
    for frag in fragments:
        if frag["cycle_id"] not in (None, cycle_id):
            continue
        # 一个待核页组在同一解释中只有一个实际启动轮次和一套钟表偏移；
        # 解释必须被原件支持才能存活，列出来不等于成立。
        retransmission_evidenced = any(
            b["page"]["run_id"] in prior_run_ids
            and b["frame"]["local_counter"] == frag["local_counter"]
            and b["frame"]["celsius"] == frag["celsius"]
            for b in frames.values()
        )
        active_explanation = next(
            (e for e in frag["explanations"] if e["origin"] == "active_run"), None
        )
        active_evidenced = False
        active_at = None
        if (
            active_explanation is not None
            and active_explanation.get("active_cycle_minute") is not None
            and cycle is not None
            and readback_ok
        ):
            minute = active_explanation["active_cycle_minute"]
            active_at = _add_minutes(cycle["start"], minute)
            if in_cycle(active_at):
                # 回读缺页里同计数器、同字节的帧可绑定该读数；点选回执只有
                # 真正回答本请求（计数器相符）才算凭据，下一帧温度相同也不算。
                receipt = select_by_request.get(frag["id"])
                recovered = any(
                    b["page"]["route"] == READBACK
                    and b["frame"]["local_counter"] == frag["local_counter"]
                    and b["frame"]["celsius"] == frag["celsius"]
                    and frame_absolute(fid) == active_at
                    for fid, b in frames.items()
                )
                receipt_ok = False
                if receipt is not None:
                    returned = frame_by_page_pos.get(
                        (receipt["page_id"], receipt["position"])
                    )
                    if returned is not None:
                        rb = frames[returned]["frame"]
                        receipt_ok = (
                            rb["local_counter"] == frag["local_counter"]
                            and rb["celsius"] == frag["celsius"]
                        )
                active_evidenced = recovered or receipt_ok
        surviving = []
        if retransmission_evidenced:
            surviving.append(("retransmission", None))
        if active_evidenced:
            surviving.append(("active_run", active_at))
        unique = {(kind, at) for kind, at in surviving}
        if len(unique) == 1:
            kind, absolute_at = next(iter(unique))
            if kind == "active_run":
                confirmed.append(
                    {
                        "frame_id": None,
                        "fragment_id": frag["id"],
                        "route": READBACK,
                        "device_id": MAIN_DEVICE,
                        "run_id": active_run_id,
                        "absolute_at": absolute_at,
                        "celsius": frag["celsius"],
                    }
                )
            # 重传解释成立时不产生新观测：它只是已有读数的又一份原件。
        else:
            pending.append(
                {
                    "id": "PENDING/" + frag["id"],
                    "cycle_id": cycle_id,
                    "reason": "ambiguous counter reuse: %d surviving explanation(s)"
                    % len(unique),
                    "next_action": "read back the original recorder buffer or obtain "
                    "signed common-pulse evidence before treating it as a sample",
                }
            )

    # 同一绝对时刻的合成观测只计一次；跨路线经共同脉冲证实为同一观测的原件合并
    # 为一条观测并保留双方凭据。冲突读数保留待核。
    in_window = [c for c in confirmed if in_cycle(c["absolute_at"])]
    observations = _merge_observations(cycle_id, in_window)

    # 邻接窗一致性：进入裁决的观测都已锚定在同一相容解释（同一启动轮次/钟表
    # 偏移或同一共同脉冲），不能每个窗口临时挑一个对齐时刻。间隔本身是否达标
    # 交给 steam.decide 的 curve_gap/incomplete_curve 裁决，缺页只登记待核，
    # 不把已经证实的边界观测抹掉；后到原件补齐窗口后该待核项自动消失。
    usable = [o for o in observations if o["state"] == "usable"]
    if cycle is not None and len(usable) > 1 and not _window_ok(usable, cycle):
        pending.append(
            {
                "id": "PENDING/WINDOW/%s" % cycle_id,
                "cycle_id": cycle_id,
                "reason": "adjacent window cannot close within one consistent explanation",
                "next_action": "recover the missing page of the same run before adjudicating",
            }
        )

    expected_counters = list(range(int(_minutes(cycle["start"], cycle["end"])) // 5 + 1)) \
        if cycle is not None else []
    route_status = []
    device_ids = sorted({p["device_id"] for p in pages} | {MAIN_DEVICE, TERMINAL_DEVICE})
    for device_id in device_ids:
        owned = [p for p in pages if p["device_id"] == device_id]
        route = owned[0]["route"] if owned else (
            READBACK if device_id == MAIN_DEVICE else TERMINAL
        )
        counters = sorted(
            b["frame"]["local_counter"]
            for b in frames.values()
            if b["page"]["device_id"] == device_id
        )
        # 完整不仅要求计数器齐，还要求每帧都能锚定到绝对时刻（终端缺共同
        # 脉冲时即便页数齐也不算完整）。
        anchored = all(
            frame_absolute(fid) is not None
            for fid, b in frames.items()
            if b["page"]["device_id"] == device_id
        )
        route_status.append(
            {
                "device_id": device_id,
                "route": route,
                "authorized": route in routes,
                "page_count": len(owned),
                "frame_count": len(counters),
                # 终端即便完整也无权声称原仪器接收页已补齐。
                "pages_complete_for_cycle": bool(expected_counters)
                and counters == expected_counters
                and anchored,
                "usable_observations": [
                    o["id"]
                    for o in observations
                    if any(s["device_id"] == device_id for s in o["sources"])
                ],
            }
        )

    db.execute(
        "DELETE FROM recorder_observation_source WHERE observation_id LIKE 'OBS-%@%'"
    )
    db.execute("DELETE FROM recorder_observation WHERE cycle_id=?", (cycle_id,))
    db.execute("DELETE FROM recorder_pending WHERE cycle_id=?", (cycle_id,))
    # 重解前撤回本周期由记录器发布的正式样本：冲突读数保留待核，不能让旧的
    # 单源样本继续充当事实。重新发布仍以同 ID 幂等落账，原件重放结果一致。
    db.execute(
        "DELETE FROM temp_sample WHERE cycle_id=? AND id LIKE 'OBS-%@%'",
        (cycle_id,),
    )
    for obs in observations:
        db.execute(
            "INSERT INTO recorder_observation VALUES(?,?,?,?,?,?,?)",
            (
                obs["id"],
                cycle_id,
                obs["absolute_at"],
                obs["celsius"],
                obs["state"],
                obs["reason"],
                obs["authorizing_route"],
            ),
        )
        for src in obs["sources"]:
            db.execute(
                "INSERT OR IGNORE INTO recorder_observation_source VALUES(?,?,?)",
                (obs["id"], src["frame_id"], src["route"]),
            )
        if obs["state"] == "usable":
            _publish_sample(db, cycle_id, obs)
    for item in pending:
        db.execute(
            "INSERT OR REPLACE INTO recorder_pending VALUES(?,?,?,?)",
            (item["id"], item["cycle_id"], item["reason"], item["next_action"]),
        )

    return {
        "cycle_id": cycle_id,
        "element_id": cycle["element_id"] if cycle is not None else None,
        "authorized_routes": sorted(routes),
        "observations": [
            {
                "id": o["id"],
                "absolute_at": o["absolute_at"],
                "celsius": o["celsius"],
                "state": o["state"],
                "sources": o["sources"],
            }
            for o in observations
        ],
        "usable_samples": [
            {"id": o["id"], "at": o["absolute_at"], "celsius": o["celsius"]}
            for o in observations
            if o["state"] == "usable"
        ],
        "pending": pending,
        "routes": route_status,
    }


def _merge_observations(cycle_id, candidates):
    merged = {}
    for c in candidates:
        at = c["absolute_at"]
        entry = merged.setdefault(
            at, {"values": set(), "sources": [], "frame_ids": set()}
        )
        frame_ref = c["frame_id"] or ("FRAGMENT/" + c["fragment_id"])
        if frame_ref in entry["frame_ids"]:
            continue
        entry["frame_ids"].add(frame_ref)
        entry["values"].add(c["celsius"])
        entry["sources"].append(
            {
                "frame_id": frame_ref,
                "route": c["route"],
                "device_id": c["device_id"],
                "run_id": c.get("run_id"),
            }
        )
    observations = []
    for at in sorted(merged):
        entry = merged[at]
        obs_id = "OBS-%s@%s" % (cycle_id, at)
        if len(entry["values"]) > 1:
            # 冲突读数：设备与轮次已可确定，仍保留待核，不抽一条当作事实。
            observations.append(
                {
                    "id": obs_id,
                    "absolute_at": at,
                    "celsius": None,
                    "state": "conflicted",
                    "reason": "same absolute time carries distinct temperature readings",
                    "authorizing_route": None,
                    "sources": entry["sources"],
                }
            )
            continue
        observations.append(
            {
                "id": obs_id,
                "absolute_at": at,
                "celsius": next(iter(entry["values"])),
                "state": "usable",
                "reason": "corroborated observation"
                if len(entry["sources"]) > 1
                else "single sourced observation",
                "authorizing_route": entry["sources"][0]["route"],
                "sources": entry["sources"],
            }
        )
    return observations


def _publish_sample(db, cycle_id, obs):
    """把证实的观测以正式 temp_sample 接口落库；旧接口输入继续走原路径。

    冲突撤回只删 temp_sample 行、保留 evidence 原件，因此证据已存在时仍要把
    正式样本行补回，保证原件重放结果一致。
    """
    item = {
        "id": obs["id"],
        "cycle_id": cycle_id,
        "at": obs["absolute_at"],
        "celsius": obs["celsius"],
    }
    record(db, "temp", item)
    db.execute(
        "INSERT OR IGNORE INTO temp_sample VALUES(?,?,?,?)",
        (obs["id"], cycle_id, obs["absolute_at"], obs["celsius"]),
    )
