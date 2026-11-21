"""Outbound eligibility under pre-signed receiving contracts.

The same physical element can be eligible for one purpose and not another.
``engineering`` contracts demand that every exposed axis is either excluded by
an independent recheck or closed by a correction.  An element still under an
undetermined investigation therefore yields *no* commercial outbound receipt.
``destructive_review`` contracts may accept the same element as a sealed
suspect sample, producing a transfer receipt that is usable **only** for
laboratory receipt; its inventory state stays pending.

A receipt is a historical fact.  It is decided against the scope known at the
moment of the request (``as_of=request.at``), so evidence entered later can
never rewrite it.  ``recompute`` appends a new, separately cited version when
today's conclusion changes; earlier versions stay intact.
"""

from .source import record_file
from . import investigation


def _one(db, sql, args=()):
    r = db.execute(sql, args).fetchone()
    return dict(r) if r else None


def _rows(db, sql, args=()):
    return [dict(r) for r in db.execute(sql, args).fetchall()]


def _release(db, element_id):
    return _one(db, 'SELECT * FROM release_decision WHERE element_id=?', (element_id,))


def _shipping(db, element_id):
    return _one(db, 'SELECT * FROM shipping WHERE element_id=? ORDER BY loaded_at DESC', (element_id,))


def _basis_text(scope_reports, as_of):
    parts = []
    for rep in scope_reports:
        parts.append('%s:%s/mould=%s/mix=%s' % (
            rep['investigation_id'], rep['state'], rep['mould']['state'], rep['mix']['state']))
    where = 'current' if as_of is None else 'as_of=' + as_of
    return '%s;%s' % (where, ';'.join(parts) if parts else 'no-open-investigation')


_REJECTING_RELEASE = {'strength_hold', 'traceback', 'lab_invalid',
                      'specimen_mismatch', 'curing_hold'}


def classify(db, element_id, purpose, as_of):
    """Return (kind, inventory_state, usable_for, state, reason).

    ``kind`` is what the recipient physically receives:
    ``commercial_release``, ``sealed_sample_transfer`` or ``blocked``.

    Investigation-driven holds are taken from the scope at ``as_of`` rather
    than from today's ``release_decision`` row, so a receipt judged at an
    earlier instant is not retroactively blocked by evidence entered later.
    Non-investigation holds (failing specimen, bad lab, broken curing) count
    as independent rejections.
    """
    release = _release(db, element_id)
    scope = investigation.build_scope(db, as_of=as_of, only_element=element_id)
    governed = bool(investigation.exposure_map(db, as_of=as_of).get(element_id))
    rejected = release is None or release['state'] in _REJECTING_RELEASE
    contradicting = any(rep['mould']['conflicts'] or rep['mix']['conflicts'] for rep in scope)

    if purpose == 'destructive_review':
        if governed or rejected or contradicting:
            return ('sealed_sample_transfer', 'pending_investigation',
                    'laboratory_receipt_only', 'sample_accepted_for_lab',
                    'suspect element sealed for destructive review; not engineering release')
        return ('sealed_sample_transfer', release['state'], 'laboratory_receipt_only',
                'sample_accepted_for_lab', 'element transferred as a sealed review sample')
    if purpose != 'engineering':
        return ('blocked', 'held', 'none', 'rejected',
                'unknown receiving purpose: ' + str(purpose))
    if governed:
        return ('blocked', 'pending_investigation', 'none', 'blocked',
                'open quality investigation covers this element; no commercial release')
    if rejected:
        return ('blocked', 'held', 'none', 'blocked',
                'release decision is ' + (release['state'] if release else 'missing'))
    if contradicting:
        return ('blocked', 'pending_investigation', 'none', 'blocked',
                'contradictory recheck evidence keeps the element pending')
    return ('commercial_release', 'released', 'engineering_installation', 'released',
            'all exposed axes excluded or corrected; commercial release granted')


_REQUEST_MOMENT = object()


def decide(db, request_id, as_of=_REQUEST_MOMENT):
    """Evaluate one outbound request and append/refresh its receipt.

    By default the decision is judged with the knowledge available *at the
    request moment* (``req['at']``): evidence entered later cannot retroactively
    retroactively unblock or block that shipment decision.  ``recompute``
    passes ``as_of=None`` to append a new version reflecting today's
    knowledge while the historical version stays intact.
    """
    req = _one(db, 'SELECT * FROM outbound_request WHERE id=?', (request_id,))
    if not req:
        raise ValueError('outbound request not found: ' + request_id)
    if as_of is _REQUEST_MOMENT:
        as_of = req['at']
    contract = _one(db, 'SELECT * FROM receiving_contract WHERE id=?', (req['contract_id'],))
    if not contract:
        raise ValueError('receiving contract not found: ' + req['contract_id'])
    if contract['signed_at'] > req['at']:
        kind, inventory, usable, state, reason = (
            'blocked', 'held', 'none', 'rejected',
            'receiving contract was signed after the outbound moment')
    else:
        kind, inventory, usable, state, reason = classify(
            db, req['element_id'], contract['purpose'], as_of)
    scope = investigation.build_scope(db, as_of=as_of, only_element=req['element_id'])
    basis = _basis_text(scope, as_of)
    old = _one(db, 'SELECT * FROM outbound_receipt WHERE request_id=? ORDER BY version DESC', (request_id,))
    if old and (old['kind'], old['inventory_state'], old['usable_for'], old['state'], old['reason'], old['basis']) == (kind, inventory, usable, state, reason, basis):
        return old
    version = (old['version'] + 1) if old else 1
    rid = 'RC-%s-v%d' % (request_id, version)
    db.execute(
        'INSERT INTO outbound_receipt'
        '(id,request_id,element_id,contract_id,purpose,kind,inventory_state,usable_for,'
        ' state,reason,at,version,basis) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (rid, request_id, req['element_id'], req['contract_id'], contract['purpose'],
         kind, inventory, usable, state, reason, req['at'], version, basis))
    return dict(db.execute('SELECT * FROM outbound_receipt WHERE id=?', (rid,)).fetchone())


def import_data(db, data):
    """Intake contracts and outbound requests, then decide their receipts."""
    with db:
        for c in data.get('contracts', []):
            record_file(db, 'contract', c['id'], c)
            db.execute(
                'INSERT INTO receiving_contract VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING',
                (c['id'], c['purpose'], c['recipient'], c['signed_at'],
                 c['signed_by'], int(c.get('version', 1))))
        for r in data.get('requests', []):
            record_file(db, 'outbound_request', r['id'], r)
            db.execute(
                'INSERT INTO outbound_request VALUES(?,?,?,?) ON CONFLICT(id) DO NOTHING',
                (r['id'], r['element_id'], r['contract_id'], r['at']))
        receipts = []
        for r in data.get('requests', []):
            receipts.append(decide(db, r['id']))
    return {'receipts': receipts}


def recompute(db):
    """Re-decide everything with the facts known *now*.

    Decisions are re-derived in dependency order first, then every outbound
    request is re-evaluated.  Historical receipts are never mutated: a
    changed conclusion appends a new version.  Loaded affected elements also
    get an append-only trace record; elements not yet loaded are marked
    blocked for shipment.
    """
    with db:
        investigation.rederive(db)
        out = [decide(db, req['id'], as_of=None) for req in
               _rows(db, 'SELECT * FROM outbound_request ORDER BY id')]
        out.extend(refresh_trace(db))
    return out


def refresh_trace(db):
    """Append trace/blocking records for elements touched by an investigation.

    Append-only: a later exclusion adds a ``cleared`` conclusion but never
    removes the earlier ``traceback``/``shipment_blocked`` record, so the
    exposure history of an already-loaded element stays visible.
    """
    written = []
    scope = investigation.build_scope(db)
    affected = {}
    for rep in scope:
        for element in rep['governed']:
            affected.setdefault(element, []).append(rep['investigation_id'])
    previously = {r['element_id'] for r in _rows(db, 'SELECT DISTINCT element_id FROM trace_record')}
    for element, inv_ids in sorted(affected.items()):
        ship = _shipping(db, element)
        if ship and ship['loaded_at']:
            state = 'traceback'
            reason = 'loaded element is inside an open exposure window'
        else:
            state = 'shipment_blocked'
            reason = 'unloaded element is inside an open exposure window'
        basis = 'investigations=' + ','.join(inv_ids)
        written.extend(_append_trace(db, element, inv_ids[0], ship, state, reason, basis))
    # An element that had a prior record but is now clear gets an explicit
    # clearance conclusion, without touching the earlier rows.
    for element in sorted(previously - set(affected)):
        ship = _shipping(db, element)
        written.extend(_append_trace(
            db, element, None, ship, 'cleared',
            'no open exposure window remains; earlier exposure history retained',
            'investigations=none'))
    return written


def _append_trace(db, element, investigation_id, ship, state, reason, basis):
    old = _one(db, 'SELECT * FROM trace_record WHERE element_id=? ORDER BY version DESC', (element,))
    ship_id = ship['id'] if ship else None
    if old and (old['state'], old['reason'], old['basis'], old['shipping_id']) == (state, reason, basis, ship_id):
        return []
    version = old['version'] + 1 if old else 1
    tid = 'TR-%s-v%d' % (element, version)
    db.execute(
        'INSERT INTO trace_record'
        '(id,element_id,investigation_id,shipping_id,loaded_at,state,reason,basis,version,recorded_at)'
        ' VALUES(?,?,?,?,?,?,?,?,?,?)',
        (tid, element, investigation_id, ship_id,
         ship['loaded_at'] if ship else None, state, reason, basis, version, _now(db)))
    return [dict(db.execute('SELECT * FROM trace_record WHERE id=?', (tid,)).fetchone())]


def _now(db):
    return db.execute("SELECT strftime('%Y-%m-%dT%H:%M:%S','now')").fetchone()[0]
