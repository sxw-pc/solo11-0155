"""Offline dual-witness transfer-ticket certification for the lifting bench.

A released element still cannot tell the transfer table which pair of
physical lift points to commit to: its centre of gravity is only an interval
and the visible nameplate orientation may be incomplete.  This module decides
one of three outcomes for the steam-curing evidence bench:

* ``ticket_issued``    - a short-term ticket committing to exactly one pair of
                         physical lift points and one witnessed table
                         orientation, referencing the still-valid release
                         version and the element's on-table workstation;
* ``test_request``     - no fixed configuration is certifiable yet, so only a
                         request for the missing witnessed observations is
                         signed (never a real ticket ahead of evidence);
* ``issuance_blocked`` - no follow-up observation on the current element
                         record can make a single commitment hold.

Simulation scope: 1D statics, three candidate points per bench, two
orientations, interval inputs.  All sizes and loads are synthetic.
"""
import json

from .source import record
from .store import history

EPS = 1e-9
NARROW_DEFAULT = 'two-hook-narrow'


def _r(x):
    return round(float(x), 6)


# ---------------------------------------------------------------------
# 1D statics over one committed pair of lift points
# ---------------------------------------------------------------------

def reactions(load_units, pos_a, pos_b, x):
    """Reactions at two lift points for a unit-weight-``load_units`` element
    whose centre of gravity sits at table position ``x``."""
    if not pos_a < pos_b:
        raise ValueError('lift points must be ordered by position')
    return (load_units * (pos_b - x) / (pos_b - pos_a),
            load_units * (x - pos_a) / (pos_b - pos_a))


def pairs_of(points):
    """All candidate point pairs, ordered by table position."""
    names = sorted(points, key=lambda n: (points[n], n))
    return [(a, b) for i, a in enumerate(names) for b in names[i + 1:]]


def _limits(bench, orientation):
    """Effective per-point thresholds for one orientation: the bench limit
    and the nameplate limit of that orientation both apply."""
    base = bench['support_limit']
    nameplate = bench.get('orientation_limit', {}).get(orientation, {})
    return {p: min(base[p], nameplate.get(p, base[p])) for p in base}


def _ok_at(load, points, pair, limits, x):
    ra, rb = reactions(load, points[pair[0]], points[pair[1]], x)
    return (ra > EPS and rb > EPS
            and ra <= limits[pair[0]] + EPS
            and rb <= limits[pair[1]] + EPS)


def pair_failures(load, points, pair, limits, lo, hi):
    """Why ``pair`` cannot be committed for the whole centre interval.

    Both reactions are linear in x, so the endpoints of [lo, hi] decide.
    Each end must stay loaded (strictly positive reaction) and under its
    threshold; both ends are judged in the same world (same x), never in
    different worlds per end.
    """
    na, nb = pair
    pa, pb = points[na], points[nb]
    fails = []
    ra_hi, rb_hi = reactions(load, pa, pb, hi)  # ra minimal, rb maximal
    ra_lo, rb_lo = reactions(load, pa, pb, lo)  # ra maximal, rb minimal
    if ra_hi <= EPS:
        fails.append({'x': _r(hi), 'point': na, 'problem': 'unloaded',
                      'reaction': _r(ra_hi)})
    if rb_lo <= EPS:
        fails.append({'x': _r(lo), 'point': nb, 'problem': 'unloaded',
                      'reaction': _r(rb_lo)})
    if ra_lo > limits[na] + EPS:
        fails.append({'x': _r(lo), 'point': na, 'problem': 'over_limit',
                      'reaction': _r(ra_lo), 'limit': limits[na]})
    if rb_hi > limits[nb] + EPS:
        fails.append({'x': _r(hi), 'point': nb, 'problem': 'over_limit',
                      'reaction': _r(rb_hi), 'limit': limits[nb]})
    return fails


def working_pairs_at(bench, orientation, x):
    """Pairs that would be safe if the centre were exactly ``x``."""
    points = bench['supports']
    limits = _limits(bench, orientation)
    return [list(p) for p in pairs_of(points)
            if _ok_at(bench['load_units'], points, p, limits, x)]


# ---------------------------------------------------------------------
# bench record and witnessed evidence
# ---------------------------------------------------------------------

def _normalize_bench(raw):
    b = dict(raw)
    if 'element_id' not in b:
        b['element_id'] = b['element']
    if 'id' not in b:
        b['id'] = b['fixture_version']
    return b


def _current_bench(db, element_id):
    rows = db.execute(
        'SELECT doc FROM bench_config WHERE element_id=? '
        'ORDER BY fixture_version DESC,id DESC', (element_id,)).fetchall()
    return json.loads(rows[0]['doc']) if rows else None


def _narrow_frame(bench):
    return bench.get('narrow_frame', NARROW_DEFAULT)


def _valid_weigh_stop(bench, w):
    """A stopped-table weighing is admissible only on the fixed two-hook
    frame, with calibrated scales and two independent witnesses.  Two scale
    groups at distinct positions back out the on-table centre range and may
    record the orientation monogram; a lone total weight cannot eliminate
    the left/right flip candidates and contributes nothing but a consistency
    check."""
    if w.get('bench_id') != bench['id'] or w.get('frame') != _narrow_frame(bench):
        return None
    if not w.get('table_stopped'):
        return None
    if len(set(w.get('witnesses', []))) < 2:
        return None
    scales = w.get('scales', [])
    if not scales:
        return None
    at = w.get('at', '')
    for s in scales:
        lo, hi = s.get('load', [0, -1])
        if lo > hi or lo < 0:
            return None
        if not (s.get('certified_from', '') <= at <= s.get('certified_until', '')):
            return None
    load = bench['load_units']
    tlo = sum(s['load'][0] for s in scales)
    thi = sum(s['load'][1] for s in scales)
    if not (tlo - EPS <= load <= thi + EPS):
        return None
    out = {'id': w['id']}
    if len({s['position'] for s in scales}) >= 2:
        nlo = sum(min(s['load'][0] * s['position'], s['load'][1] * s['position'])
                  for s in scales)
        nhi = sum(max(s['load'][0] * s['position'], s['load'][1] * s['position'])
                  for s in scales)
        out['range'] = (nlo / thi, nhi / tlo)
        monogram = w.get('orientation_monogram')
        if monogram in bench.get('orientations', {}):
            out['monogram'] = monogram
    return out


def _valid_frame_trial(bench, t):
    """A trial on the wide mechanically-balancing frame certifies exactly the
    tested (frame, orientation, pair): both hook envelopes must be witnessed
    loaded, balanced around half the load, and inside the wide-frame limits.
    The envelope is never lent to the narrow frame or another orientation."""
    wide = bench.get('wide_frame')
    if not wide or t.get('bench_id') != bench['id'] or t.get('frame') != wide['id']:
        return None
    if len(set(t.get('witnesses', []))) < 2:
        return None
    orientation = t.get('orientation')
    if orientation not in bench.get('orientations', {}):
        return None
    points = bench['supports']
    pair = t.get('pair', [])
    if len(pair) != 2 or len(set(pair)) != 2 or any(p not in points for p in pair):
        return None
    envelope = t.get('envelope', {})
    if set(envelope) != set(pair):
        return None
    half = bench['load_units'] / 2
    for p in pair:
        lo, hi = envelope[p]
        if not (lo > EPS and hi <= wide['point_limit'][p] + EPS):
            return None
        if not (lo - EPS <= half <= hi + EPS):
            return None
    slo = sum(envelope[p][0] for p in pair)
    shi = sum(envelope[p][1] for p in pair)
    if not (slo - EPS <= bench['load_units'] <= shi + EPS):
        return None
    pair = tuple(sorted(pair, key=lambda p: (points[p], p)))
    return {'id': t['id'], 'orientation': orientation, 'pair': pair}


def _evidence(db, bench):
    element_id = bench['element_id']
    stops = []
    for r in db.execute('SELECT doc FROM weigh_stop WHERE element_id=? ORDER BY id',
                        (element_id,)):
        v = _valid_weigh_stop(bench, json.loads(r['doc']))
        if v:
            stops.append(v)
    trials = []
    for r in db.execute('SELECT doc FROM frame_trial WHERE element_id=? ORDER BY id',
                        (element_id,)):
        v = _valid_frame_trial(bench, json.loads(r['doc']))
        if v:
            trials.append(v)
    return stops, trials


# ---------------------------------------------------------------------
# candidate worlds: orientation x centre interval
# ---------------------------------------------------------------------

def _orientation_range(bench, orientation):
    """Reported centre interval mapped onto table positions; a mirrored
    orientation moves the physical points relative to the element."""
    lo, hi = bench['reported_center_range']
    pos = bench['supports'].values()
    smin, smax = min(pos), max(pos)
    if bench.get('orientations', {}).get(orientation, {}).get('mirrored'):
        return smin + smax - hi, smin + smax - lo
    return lo, hi


def _worlds(bench, stops):
    """Orientations still possible and their centre intervals on the table."""
    monograms = {s['monogram'] for s in stops if 'monogram' in s}
    all_orientations = list(bench.get('orientations', {}))
    if len(monograms) > 1:
        return [], {}, False, sorted(monograms)  # contradictory monograms
    pinned = [o for o in all_orientations if o in monograms] if monograms \
        else list(all_orientations)
    ranges = {}
    for o in pinned:
        lo, hi = _orientation_range(bench, o)
        for s in stops:
            if 'range' in s:
                lo, hi = max(lo, s['range'][0]), min(hi, s['range'][1])
        ranges[o] = (lo, hi)
    witnessed = bool(monograms) and len(pinned) == 1
    return pinned, ranges, witnessed, sorted(monograms)


def _certifiable(bench, pinned, ranges, witnessed, trials):
    """Fixed configurations current evidence can certify."""
    configs = []
    points = bench['supports']
    load = bench['load_units']
    if witnessed:
        o = pinned[0]
        lo, hi = ranges[o]
        if lo <= hi + EPS:
            limits = _limits(bench, o)
            for pair in pairs_of(points):
                if not pair_failures(load, points, pair, limits, lo, hi):
                    configs.append((_narrow_frame(bench), o, pair))
    for t in trials:
        configs.append((bench['wide_frame']['id'], t['orientation'], t['pair']))
    return sorted(set(configs))


def _feasible_target(load, points, pair, limits, lo, hi):
    """A centre position inside [lo, hi] where ``pair`` holds, if any: a
    weighing that pins the centre near this target would certify the pair."""
    na, nb = pair
    pa, pb = points[na], points[nb]
    span = pb - pa
    a = max(lo, pb - limits[na] * span / load)
    b = min(hi, pa + limits[nb] * span / load)
    if a > b + EPS:
        return None
    for x in ((a + b) / 2, a, b):
        if pa + EPS < x < pb - EPS and _ok_at(load, points, pair, limits, x):
            return x
    return None


def _followups(bench, pinned, ranges, witnessed):
    """Blocking counterexample positions and the follow-up observations that
    could still make one commitment hold on the current element record."""
    points = bench['supports']
    load = bench['load_units']
    blocking = []
    for o in pinned:
        lo, hi = ranges[o]
        if lo > hi + EPS:
            blocking.append({'orientation': o, 'pair': None, 'x': None,
                             'problem': 'empty_range'})
            continue
        limits = _limits(bench, o)
        for pair in pairs_of(points):
            for f in pair_failures(load, points, pair, limits, lo, hi):
                blocking.append({'orientation': o, 'pair': list(pair), **f})
    requests = []
    for o in pinned:
        lo, hi = ranges[o]
        resolves_o = [b for b in blocking if b['orientation'] == o]
        wide = bench.get('wide_frame')
        if wide:
            ok_pairs = [list(pair) for pair in pairs_of(points)
                        if load / 2 <= wide['point_limit'][pair[0]] + EPS
                        and load / 2 <= wide['point_limit'][pair[1]] + EPS]
            if ok_pairs:
                requests.append({'kind': 'frame_trial', 'frame': wide['id'],
                                 'orientation': o, 'pairs': ok_pairs,
                                 'pins_orientation': not witnessed,
                                 'resolves': resolves_o})
        if lo > hi + EPS:
            continue
        limits = _limits(bench, o)
        for pair in pairs_of(points):
            target = _feasible_target(load, points, pair, limits, lo, hi)
            if target is None:
                continue
            requests.append({'kind': 'weigh_stop', 'frame': _narrow_frame(bench),
                             'orientation': o, 'pair': list(pair),
                             'target_x': _r(target),
                             'pins_orientation': not witnessed,
                             'resolves': [b for b in blocking
                                          if b['orientation'] == o
                                          and b['pair'] == list(pair)]})
    return blocking, requests


# ---------------------------------------------------------------------
# decision
# ---------------------------------------------------------------------

def decide(db, element_id):
    bench = _current_bench(db, element_id)
    if bench is None:
        return None
    release = db.execute('SELECT * FROM release_decision WHERE element_id=?',
                         (element_id,)).fetchone()
    old = db.execute('SELECT * FROM handling_ticket WHERE element_id=?',
                     (element_id,)).fetchone()
    if old and old['state'] == 'moved':
        return public(old)  # a recorded lift-off is never rewritten

    stops, trials = _evidence(db, bench)
    pinned, ranges, witnessed, monograms = _worlds(bench, stops)
    configs = _certifiable(bench, pinned, ranges, witnessed, trials)
    released = release is not None and release['state'] == 'released'

    frame = orientation = pa = pb = None
    detail = {'workstation': bench['workstation']}

    if old and old['state'] in ('ticket_issued', 'claimed'):
        committed = (old['frame'], old['orientation'],
                     (old['point_a'], old['point_b']))
        config_ok = committed in configs
        causes = []
        if not released:
            causes.append('release state is %s'
                          % (release['state'] if release else 'missing'))
        if release and release['version'] != old['release_version']:
            causes.append('release revised v%s -> v%s'
                          % (old['release_version'], release['version']))
        if bench['fixture_version'] != old['bench_version']:
            causes.append('bench revised %s -> %s'
                          % (old['bench_version'], bench['fixture_version']))
        if not config_ok:
            causes.append('committed configuration %s/%s/%s no longer '
                          'certifiable' % (old['frame'], old['orientation'],
                                           [old['point_a'], old['point_b']]))
        if released and config_ok:
            # a revision that still certifies only refreshes the references;
            # the committed pair itself is never re-picked before lift-off
            state, reason = old['state'], old['reason']
            frame, orientation = old['frame'], old['orientation']
            pa, pb = old['point_a'], old['point_b']
            detail = json.loads(old['detail'])
            if causes:
                detail['basis_refresh'] = causes
        else:
            state = 'frozen'
            reason = ('unclaimed ticket no longer certifiable: '
                      + '; '.join(causes))
            detail.update({'committed': {'frame': old['frame'],
                                         'orientation': old['orientation'],
                                         'pair': [old['point_a'],
                                                  old['point_b']]},
                           'causes': causes})
    elif not released:
        state = 'issuance_blocked'
        reason = ('no valid released version to reference (release state: %s)'
                  % (release['state'] if release else 'missing'))
        detail['release_state'] = release['state'] if release else None
    elif configs:
        frame, orientation, pair = configs[0]
        pa, pb = pair
        state = 'ticket_issued'
        reason = ('fixed lift pair and witnessed orientation certified '
                  'against release v%s and bench %s'
                  % (release['version'], bench['fixture_version']))
        if frame == _narrow_frame(bench):
            evidence = [s['id'] for s in stops]
            cog = [_r(ranges[orientation][0]), _r(ranges[orientation][1])]
        else:
            evidence = [t['id'] for t in trials
                        if t['orientation'] == orientation and t['pair'] == pair]
            cog = None
        detail.update({'config': {'frame': frame, 'orientation': orientation,
                                  'pair': [pa, pb]},
                       'cog_range': cog, 'evidence': evidence,
                       'release_version': release['version'],
                       'validity': 'fixed until first lift-off or any '
                                   'release/bench revision'})
    else:
        blocking, requests = _followups(bench, pinned, ranges, witnessed)
        detail.update({'orientations_possible': list(pinned),
                       'cog_ranges': {o: [_r(ranges[o][0]), _r(ranges[o][1])]
                                      for o in pinned},
                       'blocking': blocking, 'requests': requests,
                       'evidence': [s['id'] for s in stops]
                                   + [t['id'] for t in trials]})
        if monograms and len(monograms) > 1:
            detail['orientation_conflict'] = monograms
        if requests:
            state = 'test_request'
            reason = ('fixed configuration not yet certifiable; witnessed '
                      'observations required before issuing')
        else:
            state = 'issuance_blocked'
            reason = ('no feasible follow-up observation can certify a '
                      'fixed configuration')

    rel_v = release['version'] if release else None
    base = (state, reason, bench['id'], bench['fixture_version'], rel_v,
            frame, orientation, pa, pb)
    old_base = None if not old else (
        old['state'], old['reason'], old['bench_id'], old['bench_version'],
        old['release_version'], old['frame'], old['orientation'],
        old['point_a'], old['point_b'])
    prospective = (old['version'] + 1 if old_base != base else old['version']) \
        if old else 1

    if state in ('ticket_issued', 'claimed'):
        events = db.execute(
            'SELECT * FROM handling_event WHERE element_id=? AND actor=? '
            'ORDER BY at,id', (element_id, bench['workstation'])).fetchall()
        moves = [e for e in events if e['kind'] == 'move'
                 and e['ticket_version'] == prospective]
        claims = [e for e in events if e['kind'] == 'claim'
                  and e['ticket_version'] == prospective]
        if moves:
            state = 'moved'
            reason = ('lift-off recorded against ticket v%s; record is '
                      'terminal' % prospective)
            detail = {'workstation': bench['workstation'],
                      'committed': {'frame': frame, 'orientation': orientation,
                                    'pair': [pa, pb]},
                      'move_event': moves[-1]['id'],
                      'release_version': rel_v}
        elif claims and state == 'ticket_issued':
            state = 'claimed'
            reason = ('ticket claimed by %s; lift points locked until first '
                      'lift-off' % bench['workstation'])
            detail['claim_event'] = claims[-1]['id']

    detail_json = json.dumps(detail, ensure_ascii=False, sort_keys=True)
    final = (state, reason, bench['id'], bench['fixture_version'], rel_v,
             frame, orientation, pa, pb, detail_json)
    old_final = None if not old else (
        old['state'], old['reason'], old['bench_id'], old['bench_version'],
        old['release_version'], old['frame'], old['orientation'],
        old['point_a'], old['point_b'], old['detail'])
    if old_final != final:
        ver = old['version'] + 1 if old else 1
        db.execute(
            'INSERT INTO handling_ticket VALUES(?,?,?,?,?,?,?,?,?,?,?,?) '
            'ON CONFLICT(element_id) DO UPDATE SET state=excluded.state,'
            'reason=excluded.reason,bench_id=excluded.bench_id,'
            'bench_version=excluded.bench_version,'
            'release_version=excluded.release_version,frame=excluded.frame,'
            'orientation=excluded.orientation,point_a=excluded.point_a,'
            'point_b=excluded.point_b,detail=excluded.detail,'
            'version=excluded.version',
            (element_id, state, reason, bench['id'], bench['fixture_version'],
             rel_v, frame, orientation, pa, pb, detail_json, ver))
        history(db, 'handling', element_id, state, reason, bench['id'])
    return public(db.execute('SELECT * FROM handling_ticket WHERE element_id=?',
                             (element_id,)).fetchone())


def import_data(db, data):
    if isinstance(data, dict) and 'supports' in data \
            and ('element' in data or 'element_id' in data):
        data = {'benches': [data]}
    touched = set()
    with db:
        for raw in data.get('benches', []):
            b = _normalize_bench(raw)
            if record(db, 'bench', b):
                db.execute('INSERT INTO bench_config VALUES(?,?,?,?,?)',
                           (b['id'], b['element_id'], b['workstation'],
                            b['fixture_version'],
                            json.dumps(b, ensure_ascii=False,
                                       sort_keys=True)))
            touched.add(b['element_id'])
        for w in data.get('weigh_stops', []):
            if record(db, 'weigh_stop', w):
                db.execute('INSERT INTO weigh_stop VALUES(?,?,?,?)',
                           (w['id'], w['element_id'], w['bench_id'],
                            json.dumps(w, ensure_ascii=False, sort_keys=True)))
            touched.add(w['element_id'])
        for t in data.get('frame_trials', []):
            if record(db, 'frame_trial', t):
                db.execute('INSERT INTO frame_trial VALUES(?,?,?,?)',
                           (t['id'], t['element_id'], t['bench_id'],
                            json.dumps(t, ensure_ascii=False, sort_keys=True)))
            touched.add(t['element_id'])
        for e in data.get('events', []):
            if record(db, 'handling_event', e):
                db.execute('INSERT INTO handling_event VALUES(?,?,?,?,?,?)',
                           (e['id'], e['element_id'], e['kind'],
                            e['ticket_version'], e['at'], e['by']))
            touched.add(e['element_id'])
        return [decide(db, e) for e in sorted(touched)
                if db.execute('SELECT 1 FROM bench_config WHERE element_id=?',
                              (e,)).fetchone()]


# ---------------------------------------------------------------------
# post-hoc check against the recorded element versions
# ---------------------------------------------------------------------

def public(row):
    out = dict(row)
    out['detail'] = json.loads(out['detail'])
    return out


def recheck(db, element_id):
    row = db.execute('SELECT * FROM handling_ticket WHERE element_id=?',
                     (element_id,)).fetchone()
    if not row:
        return None
    t = public(row)
    release = db.execute('SELECT * FROM release_decision WHERE element_id=?',
                         (element_id,)).fetchone()
    bench = _current_bench(db, element_id)
    config = {'frame': t['frame'], 'orientation': t['orientation'],
              'pair': [t['point_a'], t['point_b']] if t['point_a'] else None}
    if config['frame'] is None \
            and isinstance(t['detail'].get('committed'), dict):
        config = t['detail']['committed']
    out = {'element_id': element_id, 'state': t['state'],
           'ticket_version': t['version'],
           'release_version_on_ticket': t['release_version'],
           'release_version_now': release['version'] if release else None,
           'release_state_now': release['state'] if release else None,
           'bench_version_on_ticket': t['bench_version'],
           'bench_version_now': bench['fixture_version'] if bench else None,
           'config': config}
    if t['state'] == 'moved':
        out['verdict'] = 'moved_as_recorded'
        out['note'] = ('lift-off already recorded; not re-judged against '
                       'current evidence')
    elif t['state'] in ('ticket_issued', 'claimed'):
        stops, trials = _evidence(db, bench)
        pinned, ranges, witnessed, _ = _worlds(bench, stops)
        configs = _certifiable(bench, pinned, ranges, witnessed, trials)
        committed = (t['frame'], t['orientation'],
                     (t['point_a'], t['point_b']))
        ok = (committed in configs and release is not None
              and release['state'] == 'released'
              and release['version'] == t['release_version']
              and bench['fixture_version'] == t['bench_version'])
        out['verdict'] = 'still_valid' if ok else 'invalidated'
    else:
        out['verdict'] = 'not_currently_valid'
    return out
