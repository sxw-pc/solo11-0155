"""Offline quality-investigation intake and exposure scoping.

A low-strength co-cured specimen only proves that its element did not meet
the release condition. It does not, by itself, say whether the cause was a
mould or a mix. This module keeps one investigation open across two
independent axes and computes which casts are still candidates on each axis.

Every window is decided by the time the cast physically happened. Evidence
entered late changes today's conclusion; it never back-dates itself to a
moment before it existed. For historical outbound decisions callers pass
``as_of`` and only evidence known by then is considered.
"""

from .source import find_sha, record_file

AXES = ('mould', 'mix')
_PASS = {'pass', 'passed', 'good', 'qualified', 'ok', 'accepted'}
_FAIL = {'fail', 'failed', 'defect', 'defective', 'bad', 'reject', 'rejected', 'unqualified'}


def _rows(db, sql, args=()):
    return [dict(r) for r in db.execute(sql, args).fetchall()]


def _one(db, sql, args=()):
    r = db.execute(sql, args).fetchone()
    return dict(r) if r else None


def _norm_result(result):
    text = str(result or '').strip().lower()
    if text in _PASS:
        return 'pass'
    if text in _FAIL:
        return 'fail'
    return 'unknown'


def _known_by(item):
    """The moment this record entered the ledger (never earlier than its event)."""
    return (item.get('recorded_at') or item.get('known_at') or item.get('sampled_at')
            or item.get('at') or item.get('effective_at') or item.get('revoked_at'))


def _evidence_sha(db, kind, ident):
    return find_sha(db, kind, ident)


# ---------------------------------------------------------------------------
# intake
# ---------------------------------------------------------------------------

def _require_fact(db, table, ident, lab):
    row = db.execute('SELECT * FROM %s WHERE id=?' % table, (ident,)).fetchone()
    if not row:
        raise ValueError('%s not found for investigation: %s' % (lab, ident))
    return dict(row)


def _next_investigation_id(specimen_id):
    return 'INV-' + specimen_id


def ensure_open(db, specimen, known_at=None):
    """Create the one investigation that a low-strength result requires.

    Idempotent on the specimen: re-importing the same specimen (or the same
    file) never creates a second investigation.  Returns the investigation id.
    """
    existing = _one(db, 'SELECT * FROM quality_investigation WHERE specimen_id=?',
                    (specimen['id'],))
    if existing:
        return existing['id']
    batch = _one(db, 'SELECT * FROM cast_batch WHERE id=?', (specimen['cast_id'],))
    cycle = _one(db, 'SELECT * FROM steam_cycle WHERE id=?', (specimen['cycle_id'],))
    lab = _one(db, 'SELECT * FROM lab_cert WHERE id=?', (specimen['lab_id'],))
    ident = _next_investigation_id(specimen['id'])
    db.execute(
        'INSERT INTO quality_investigation'
        '(id,specimen_id,element_id,cast_id,cycle_id,lab_cert_id,known_at,'
        ' specimen_sha,cast_sha,cycle_sha,lab_sha,state,mould_state,mix_state,version)'
        ' VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)',
        (ident, specimen['id'], specimen['element_id'], specimen['cast_id'],
         specimen['cycle_id'], specimen['lab_id'], known_at or specimen['at'],
         _evidence_sha(db, 'specimen', specimen['id']),
         _evidence_sha(db, 'cast', specimen['cast_id']) if batch else None,
         _evidence_sha(db, 'cycle', specimen['cycle_id']) if cycle else None,
         _evidence_sha(db, 'lab', specimen['lab_id']) if lab else None,
         'open', 'unresolved', 'unresolved'),
    )
    return ident


def _adopt_id(db, old_id, new_id):
    """Let an explicitly named intake take over an auto-created id."""
    if old_id == new_id:
        return new_id
    db.execute('UPDATE quality_investigation SET id=? WHERE id=?', (new_id, old_id))
    db.execute('UPDATE axis_recheck SET investigation_id=? WHERE investigation_id=?',
               (new_id, old_id))
    return new_id


def _upsert_investigation(db, item, specimen, batch, cycle, lab, known_at):
    declared = item.get('id') or _next_investigation_id(specimen['id'])
    old = _one(db, 'SELECT * FROM quality_investigation WHERE specimen_id=?',
               (specimen['id'],))
    if old is not None:
        ident = old['id']
        want = (old['specimen_id'], old['element_id'], old['cast_id'], old['cycle_id'],
                old['lab_cert_id'])
        same = (specimen['id'], specimen['element_id'], batch['id'], cycle['id'], lab['id'])
        if want != same:
            raise ValueError('investigation identity conflict: %s' % declared)
        if declared != ident:
            changed = _one(db, 'SELECT id FROM quality_investigation WHERE id=?', (declared,))
            if changed is None and ident == _next_investigation_id(specimen['id']):
                ident = _adopt_id(db, ident, declared)
        if cum_known(old['known_at'], known_at) != old['known_at']:
            db.execute('UPDATE quality_investigation SET known_at=? WHERE id=?',
                       (known_at, ident))
        return ident, False
    sha = _evidence_sha(db, 'investigation', declared)
    db.execute(
        'INSERT INTO quality_investigation'
        '(id,specimen_id,element_id,cast_id,cycle_id,lab_cert_id,known_at,'
        ' specimen_sha,cast_sha,cycle_sha,lab_sha,state,mould_state,mix_state,version)'
        ' VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)',
        (declared, specimen['id'], specimen['element_id'], batch['id'], cycle['id'],
         lab['id'], known_at, sha,
         _evidence_sha(db, 'cast', batch['id']),
         _evidence_sha(db, 'cycle', cycle['id']),
         _evidence_sha(db, 'lab', lab['id']),
         'open', 'unresolved', 'unresolved'),
    )
    return declared, True


def cum_known(a, b):
    """Earliest non-empty timestamp (the moment the fact became known)."""
    if not a:
        return b
    if not b:
        return a
    return min(a, b)


# ---------------------------------------------------------------------------
# windows
# ---------------------------------------------------------------------------

def _mould_window(db, mould_id, anchor_at, as_of=None):
    """Return (start, end, inspection, open) for one mould.

    ``start`` is the last qualified inspection at or before the *failing
    cast* (``anchor_at``).  A check performed later cannot retroactively
    qualify the cast, so it never moves the anchor.  ``end`` is the first
    effective repair after that inspection.  No inspection means the window
    is open from the beginning of time.

    ``as_of`` filters on when a record was *known*: late intake can reveal an
    old window but cannot back-date the knowledge of it.
    """
    checks = _rows(
        db,
        'SELECT * FROM mould_check WHERE mould_id=? AND result IN (?,?,?,?,?)'
        ' ORDER BY at,id',
        (mould_id, 'pass', 'passed', 'good', 'qualified', 'ok'),
    )
    if as_of:
        checks = [c for c in checks if (c['recorded_at'] or c['at']) <= as_of]
    checks = [c for c in checks if c['at'] <= anchor_at]
    repairs = _rows(
        db,
        'SELECT * FROM exposure_repair WHERE axis=? AND target_id=?'
        ' ORDER BY effective_at,id',
        ('mould', mould_id),
    )
    if as_of:
        repairs = [r for r in repairs if (r['recorded_at'] or r['effective_at']) <= as_of]
    if not checks:
        start, inspection = None, None
        end = repairs[0]['effective_at'] if repairs else None
        return start, end, inspection, end is None
    inspection = checks[-1]
    later = [r for r in repairs if r['effective_at'] >= inspection['at']]
    end = later[0]['effective_at'] if later else None
    return inspection['at'], end, inspection, end is None


def _mix_window(db, mix_id, anchor_at, as_of=None):
    """Return (start, end, basis, open) for one mix version.

    ``start`` is the last *qualified retained sample* of that same version at
    or before the failing cast.  Before any such sample the engineering
    approval at or before the cast is the dated basis.  ``end`` is the first
    effective material change/replacement after that basis; no effective
    change leaves the window open.
    """
    samples = _rows(
        db,
        'SELECT * FROM mix_sample WHERE mix_id=? AND result IN (?,?,?,?,?) ORDER BY sampled_at,id',
        (mix_id, 'pass', 'passed', 'good', 'qualified', 'ok'),
    )
    if as_of:
        samples = [s for s in samples if (s['recorded_at'] or s['sampled_at']) <= as_of]
    samples = [s for s in samples if s['sampled_at'] <= anchor_at]
    basis = None
    if samples:
        basis = samples[-1]
    else:
        approval = _one(db, 'SELECT * FROM mix_approval WHERE id=?', (mix_id,))
        basis = approval if approval and approval['approved_at'] <= anchor_at else None
    if basis is None:
        return None, None, None, True
    start = basis['sampled_at'] if samples else basis['approved_at']
    version = basis['mix_version'] if samples else basis['version']
    repairs = _rows(
        db,
        'SELECT * FROM exposure_repair WHERE axis=? AND target_id=? AND target_version=?'
        ' ORDER BY effective_at,id',
        ('mix', mix_id, int(version)),
    )
    if as_of:
        repairs = [r for r in repairs if (r['recorded_at'] or r['effective_at']) <= as_of]
    later = [r for r in repairs if r['effective_at'] >= start]
    end = later[0]['effective_at'] if later else None
    return start, end, basis, end is None


def _covered(at, start, end):
    if start and at < start:
        return False
    if end and at > end:
        return False
    return True


def _mould_casts(db, target_id, start, end):
    out = []
    for c in _rows(db, 'SELECT * FROM cast_batch WHERE mould_id=? ORDER BY at,id', (target_id,)):
        if _covered(c['at'], start, end):
            out.append(c)
    return out


def _mix_casts(db, target_id, start, end):
    out = []
    for c in _rows(db, 'SELECT * FROM cast_batch WHERE mix_id=? ORDER BY at,id', (target_id,)):
        if _covered(c['at'], start, end):
            out.append(c)
    return out


# ---------------------------------------------------------------------------
# recheck evaluation
# ---------------------------------------------------------------------------

def _axis_target_version(db, axis, target):
    if axis == 'mould':
        row = _one(db, 'SELECT version FROM mould WHERE id=?', (target,))
    else:
        row = _one(db, 'SELECT version FROM mix_approval WHERE id=?', (target,))
    return row['version'] if row else None


def _axis_status(db, axis, target, as_of=None):
    """Evaluate the independent rechecks for one cause axis.

    A recheck only counts when it targets the *applicable version* of the
    mould/mix.  Revoked rechecks cannot support a conclusion.  Contradictory
    live rechecks leave the axis unresolved (and are surfaced as conflicts).
    A passing recheck for some other element never clears this one.
    """
    want_version = _axis_target_version(db, axis, target)
    rows = _rows(
        db,
        'SELECT * FROM axis_recheck WHERE axis=? AND target_id=? ORDER BY sampled_at,id',
        (axis, target),
    )
    if as_of:
        rows = [r for r in rows if (r['recorded_at'] or r['sampled_at']) <= as_of]
    applicable = [r for r in rows
                  if want_version is None or int(r['target_version']) == int(want_version)]
    broken = [r['id'] for r in applicable
              if _one(db, 'SELECT 1 FROM cert_revocation WHERE cert_kind=? AND cert_id=?'
                      ' AND (? IS NULL OR revoked_at<=?)',
                      ('recheck', r['id'], as_of, as_of))]
    live = [r for r in applicable if r['id'] not in broken]
    results = {_norm_result(r['result']) for r in live}
    results.discard('unknown')
    conflict = len(results) > 1
    if conflict:
        state = 'unresolved'
    elif 'pass' in results:
        state = 'excluded'
    elif 'fail' in results:
        state = 'defective'
    else:
        state = 'unresolved'
    return {
        'state': state,
        'rechecks': live,
        'broken_rechecks': broken,
        'conflicts': [
            {'id': r['id'], 'result': _norm_result(r['result']), 'sampled_at': r['sampled_at'],
             'signer': r['signer'], 'sha': r['sha']}
            for r in live
        ] if conflict else [],
    }


# ---------------------------------------------------------------------------
# public scope
# ---------------------------------------------------------------------------

def build_scope(db, as_of=None, cutoff=None, only_element=None):
    """Compute the exposure scope of every open investigation.

    Returns a list of investigation reports.  Each report contains the two
    axis sets (``mould_exposed``, ``mix_exposed``), their union, and per
    element the physical time window(s) that caused the hit.

    ``as_of`` is a *knowledge* boundary: an investigation not known yet at
    that instant is invisible, and checks/rechecks/repairs only count if they
    had been recorded by then.  The cast windows themselves still use the
    physical cast times, so a late back-fill changes today's conclusion but
    cannot pretend it was known earlier.
    """
    reports = []
    for inv in _rows(db, 'SELECT * FROM quality_investigation ORDER BY id'):
        if cutoff and inv['created_at'] and inv['created_at'] > cutoff:
            continue
        if as_of and inv['known_at'] and inv['known_at'] > as_of:
            continue
        cast = _one(db, 'SELECT * FROM cast_batch WHERE id=?', (inv['cast_id'],))
        if not cast:
            continue
        axis_info = {}
        for axis in AXES:
            target = cast['mould_id'] if axis == 'mould' else cast['mix_id']
            if axis == 'mould':
                start, end, basis, open_ = _mould_window(db, target, cast['at'], as_of)
            else:
                start, end, basis, open_ = _mix_window(db, target, cast['at'], as_of)
            status = _axis_status(db, axis, target, as_of)
            exposed = _mould_casts(db, target, start, end) if axis == 'mould' else _mix_casts(db, target, start, end)
            axis_info[axis] = {
                'target_id': target,
                'target_version': _axis_target_version(db, axis, target),
                'state': status['state'],
                'window': {'from': start, 'to': end, 'open': open_},
                'basis': basis,
                'casts': exposed,
                'conflicts': status['conflicts'],
                'broken_rechecks': status['broken_rechecks'],
                'rechecks': [
                    {'id': r['id'], 'result': _norm_result(r['result']),
                     'sampled_at': r['sampled_at'], 'target_version': r['target_version']}
                    for r in status['rechecks']
                ],
            }
        mould_set = {c['element_id'] for c in axis_info['mould']['casts']}
        mix_set = {c['element_id'] for c in axis_info['mix']['casts']}
        both = mould_set & mix_set
        # While either axis is still unresolved the governed set is the
        # union.  An excluded axis contributes nothing.
        governed = set()
        if axis_info['mould']['state'] != 'excluded':
            governed |= mould_set
        if axis_info['mix']['state'] != 'excluded':
            governed |= mix_set
        # The origin element is already held/traced by its own failing
        # specimen; the investigation governs the *other* casts that share
        # the suspect window.  Removing it must not clear its own hold.
        origin = cast['element_id']
        governed.discard(origin)
        windows = {}
        for element in sorted(governed | mould_set | mix_set):
            windows[element] = []
            for axis in AXES:
                if axis_info[axis]['state'] == 'excluded':
                    continue
                for c in axis_info[axis]['casts']:
                    if c['element_id'] == element:
                        windows[element].append({
                            'axis': axis,
                            'cast_id': c['id'],
                            'from': c['at'],
                            'finish_at': c['finish_at'],
                            'axis_window': axis_info[axis]['window'],
                        })
        reports.append({
            'investigation_id': inv['id'],
            'specimen_id': inv['specimen_id'],
            'element_id': origin,
            'cast_id': cast['id'],
            'known_at': inv['known_at'],
            'state': _overall_state(axis_info, origin, governed),
            'mould': axis_info['mould'],
            'mix': axis_info['mix'],
            'mould_exposed': sorted(mould_set),
            'mix_exposed': sorted(mix_set),
            'union_exposed': sorted(mould_set | mix_set),
            'both_exposed': sorted(both),
            'governed': sorted(governed),
            'windows': windows,
        })
    if only_element:
        reports = [r for r in reports if only_element in r['governed'] or only_element in r['union_exposed']]
    return reports


def _overall_state(axis_info, origin, governed):
    states = {a['state'] for a in axis_info.values()}
    if 'unresolved' in states:
        return 'undetermined'
    if governed:
        return 'defect_confirmed'
    return 'no_forward_exposure'


def exposure_map(db, as_of=None, cutoff=None):
    """element_id -> {'axes', 'investigations', 'windows', 'known_at'}.

    Only the governed set (the union of axes that are not excluded) is
    returned; the full per-axis breakdown stays in :func:`build_scope`.
    """
    out = {}
    for rep in build_scope(db, as_of=as_of, cutoff=cutoff):
        for element in rep['governed']:
            hit = out.setdefault(element, {'axes': set(), 'investigations': [],
                                           'windows': [], 'known_at': None})
            for w in rep['windows'].get(element, []):
                hit['axes'].add(w['axis'])
                hit['windows'].append(w)
            hit['investigations'].append(rep['investigation_id'])
            hit['known_at'] = cum_known(hit['known_at'], rep['known_at'])
    return out


def investigation_for_element(db, element_id):
    return _rows(
        db,
        'SELECT i.* FROM quality_investigation i JOIN cast_batch c ON c.id=i.cast_id'
        ' WHERE c.element_id=? ORDER BY i.id',
        (element_id,),
    )


# ---------------------------------------------------------------------------
# import
# ---------------------------------------------------------------------------

def import_data(db, data):
    """Intake investigations plus their independent rechecks and repairs."""
    from . import cast as cast_mod, steam as steam_mod, strength as strength_mod

    touched = set()
    with db:
        for item in data.get('investigations', []):
            specimen = _require_fact(db, 'specimen', item['specimen_id'], 'specimen')
            batch = _require_fact(db, 'cast_batch', item.get('cast_id') or specimen['cast_id'], 'cast')
            cycle = _require_fact(db, 'steam_cycle', item.get('cycle_id') or specimen['cycle_id'], 'cycle')
            lab = _require_fact(db, 'lab_cert', item.get('lab_cert_id') or specimen['lab_id'], 'lab')
            for fact, table, ident in (
                    ('specimen', 'specimen', specimen['id']),
                    ('cast', 'cast_batch', batch['id']),
                    ('cycle', 'steam_cycle', cycle['id']),
                    ('lab', 'lab_cert', lab['id'])):
                if not _evidence_sha(db, fact, ident):
                    raise ValueError('%s evidence missing for investigation: %s' % (fact, ident))
            known_at = item.get('known_at') or specimen['at']
            ident, _ = _upsert_investigation(db, item, specimen, batch, cycle, lab, known_at)
            record_file(db, 'investigation', ident, dict(item, id=ident))
            touched.add(batch['element_id'])

        for check in data.get('mould_checks', []):
            record_file(db, 'mould_check', check['id'], check)
            db.execute(
                'INSERT INTO mould_check VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING',
                (check['id'], check['mould_id'], int(check.get('mould_version', 1)),
                 check['at'], check['valid_until'], check['result'],
                 check['signer'], _evidence_sha(db, 'mould_check', check['id']),
                 _known_by(check)),
            )
            touched.update(r['element_id'] for r in db.execute(
                'SELECT element_id FROM cast_batch WHERE mould_id=?', (check['mould_id'],)))

        for rc in data.get('rechecks', []):
            record_file(db, 'recheck', rc['id'], rc)
            db.execute(
                'INSERT INTO axis_recheck VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING',
                (rc['id'], rc.get('investigation_id'), rc['axis'], rc['target_id'],
                 int(rc.get('target_version', 1)), rc['sampled_at'], rc['signer'],
                 _evidence_sha(db, 'recheck', rc['id']), rc['result'], rc.get('note', ''),
                 _known_by(rc)),
            )
            touched.update(r['element_id'] for r in db.execute(
                'SELECT element_id FROM cast_batch WHERE %s=?' % ('mould_id' if rc['axis'] == 'mould' else 'mix_id'),
                (rc['target_id'],)))

        for sample in data.get('mix_samples', []):
            record_file(db, 'mix_sample', sample['id'], sample)
            db.execute(
                'INSERT INTO mix_sample VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING',
                (sample['id'], sample['mix_id'], int(sample.get('mix_version', 1)),
                 sample['sampled_at'], sample['valid_until'], sample['result'],
                 sample['signer'], _evidence_sha(db, 'mix_sample', sample['id']),
                 _known_by(sample)),
            )
            touched.update(r['element_id'] for r in db.execute(
                'SELECT element_id FROM cast_batch WHERE mix_id=?', (sample['mix_id'],)))

        for rep in data.get('repairs', []):
            record_file(db, 'repair', rep['id'], rep)
            db.execute(
                'INSERT INTO exposure_repair VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING',
                (rep['id'], rep['axis'], rep['target_id'], int(rep.get('target_version', 1)),
                 rep.get('kind', 'corrective'), rep['effective_at'], rep['signer'],
                 _evidence_sha(db, 'repair', rep['id']), rep.get('note', ''),
                 _known_by(rep)),
            )
            touched.update(r['element_id'] for r in db.execute(
                'SELECT element_id FROM cast_batch WHERE %s=?' % ('mould_id' if rep['axis'] == 'mould' else 'mix_id'),
                (rep['target_id'],)))

        for rev in data.get('revocations', []):
            record_file(db, 'revocation', rev['id'], rev)
            db.execute(
                'INSERT INTO cert_revocation VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING',
                (rev['id'], rev['cert_kind'], rev['cert_id'], rev['revoked_at'],
                 rev['signer'], _evidence_sha(db, 'revocation', rev['id']),
                 _known_by(rev)),
            )
            if rev['cert_kind'] == 'specimen':
                touched.update(r['element_id'] for r in db.execute(
                    'SELECT element_id FROM specimen WHERE id=?', (rev['cert_id'],)))
            elif rev['cert_kind'] == 'lab':
                touched.update(r['element_id'] for r in db.execute(
                    'SELECT element_id FROM specimen WHERE lab_id=?', (rev['cert_id'],)))
            else:
                touched.update(r['element_id'] for r in db.execute('SELECT element_id FROM specimen'))

        _refresh(db, cast_mod, steam_mod, strength_mod, touched)
    return {'investigations': len(data.get('investigations', [])),
            'touched': sorted(touched),
            'scope': [{'investigation_id': r['investigation_id'], 'state': r['state'],
                       'governed': r['governed']} for r in build_scope(db)]}


def _refresh(db, cast_mod, steam_mod, strength_mod, elements):
    for element in sorted(elements):
        cast_mod.decide(db, element)
        steam_mod.decide(db, element)
        strength_mod.decide(db, element)


def rederive(db):
    """Re-decide every element in dependency order (cast -> steam -> strength)."""
    from . import cast as cast_mod, steam as steam_mod, strength as strength_mod
    elements = [r[0] for r in db.execute(
        'SELECT DISTINCT element_id FROM cast_batch'
        ' UNION SELECT DISTINCT element_id FROM steam_cycle'
        ' UNION SELECT DISTINCT element_id FROM specimen ORDER BY element_id')]
    _refresh(db, cast_mod, steam_mod, strength_mod, elements)
    return elements
