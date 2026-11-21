import json

from .store import connect
from . import cast, investigation, steam, strength


def _element_ids(db):
    return [r[0] for r in db.execute(
        'SELECT DISTINCT element_id FROM cast_batch'
        ' UNION SELECT DISTINCT element_id FROM steam_cycle'
        ' UNION SELECT DISTINCT element_id FROM specimen ORDER BY element_id')]


def _decision(db, table, element_id):
    row = db.execute('SELECT * FROM %s WHERE element_id=?' % table, (element_id,)).fetchone()
    return dict(row) if row else None


def build(db, as_of=None):
    scope = investigation.build_scope(db, as_of=as_of)
    by_element = {}
    for rep in scope:
        for element in set(rep['union_exposed']) | set(rep['governed']):
            by_element.setdefault(element, []).append(rep)
    out = []
    for element in _element_ids(db):
        reports = by_element.get(element, [])
        release = _decision(db, 'release_decision', element)
        out.append({
            'element_id': element,
            'cast': _decision(db, 'cast_decision', element),
            'steam': _decision(db, 'steam_decision', element),
            'release': release,
            'investigation': {
                'governed': any(element in rep['governed'] for rep in reports),
                'axes': sorted({w['axis'] for rep in reports
                                for w in rep['windows'].get(element, [])}),
                'investigations': [rep['investigation_id'] for rep in reports],
                'windows': [w for rep in reports for w in rep['windows'].get(element, [])],
            },
            'receipts': [dict(r) for r in db.execute(
                'SELECT * FROM outbound_receipt WHERE element_id=? ORDER BY version', (element,))],
            'trace': [dict(r) for r in db.execute(
                'SELECT * FROM trace_record WHERE element_id=? ORDER BY version', (element,))],
            'history': [dict(x) for x in db.execute(
                'SELECT subsystem,state,reason,source_id,basis,version,known_at'
                ' FROM decision_history WHERE element_id=? ORDER BY id', (element,))],
        })
    return out


def investigation_report(db, as_of=None):
    """The investigation-facing view: axes, union and per-element windows."""
    scope = investigation.build_scope(db, as_of=as_of)
    return [{
        'investigation_id': rep['investigation_id'],
        'specimen_id': rep['specimen_id'],
        'element_id': rep['element_id'],
        'cast_id': rep['cast_id'],
        'known_at': rep['known_at'],
        'state': rep['state'],
        'mould': {
            'target_id': rep['mould']['target_id'],
            'target_version': rep['mould']['target_version'],
            'state': rep['mould']['state'],
            'window': rep['mould']['window'],
            'exposed': rep['mould_exposed'],
            'conflicts': rep['mould']['conflicts'],
            'broken_rechecks': rep['mould']['broken_rechecks'],
        },
        'mix': {
            'target_id': rep['mix']['target_id'],
            'target_version': rep['mix']['target_version'],
            'state': rep['mix']['state'],
            'window': rep['mix']['window'],
            'exposed': rep['mix_exposed'],
            'conflicts': rep['mix']['conflicts'],
            'broken_rechecks': rep['mix']['broken_rechecks'],
        },
        'union_exposed': rep['union_exposed'],
        'both_exposed': rep['both_exposed'],
        'governed': rep['governed'],
        'windows': rep['windows'],
    } for rep in scope]


def receipts(db):
    return [dict(r) for r in db.execute('SELECT * FROM outbound_receipt ORDER BY element_id,version')]


def traces(db):
    return [dict(r) for r in db.execute('SELECT * FROM trace_record ORDER BY element_id,version')]


_VOLATILE = {'created_at', 'recorded_at'}


def _audit_view(value):
    """Strip wall-clock bookkeeping fields before comparing a replay.

    ``created_at``/``recorded_at`` record *when* a row was written, not what
    was decided, so they legitimately differ between the live database and a
    fresh replay.  Everything that carries a conclusion (states, reasons,
    versions, bases, digests) is compared.
    """
    if isinstance(value, dict):
        return {k: _audit_view(v) for k, v in value.items() if k not in _VOLATILE}
    if isinstance(value, list):
        return [_audit_view(v) for v in value]
    return value


def audit(db):
    replay = connect(':memory:')
    handlers = {
        'cast-import': cast.import_data,
        'steam-import': steam.import_data,
        'strength-import': strength.import_data,
        'investigation-import': investigation.import_data,
    }
    from . import outbound
    handlers['outbound-import'] = outbound.import_data
    handlers['recompute'] = lambda replay, _doc: outbound.recompute(replay)
    for r in db.execute('SELECT command,raw FROM file_journal ORDER BY id'):
        handlers[r['command']](replay, json.loads(r['raw']))
    equal = _audit_view(build(db)) == _audit_view(build(replay))
    replay.close()
    return {'ok': equal, 'files': db.execute('SELECT count(*) FROM file_journal').fetchone()[0]}
