import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from steamcast.store import connect
from steamcast import cast, steam, strength, investigation, outbound, report

ROOT = Path(__file__).resolve().parents[1]


def fx(name):
    return json.loads((ROOT / 'fixtures' / (name + '.json')).read_text(encoding='utf8'))


def element_states(db, element):
    def one(table):
        row = db.execute('SELECT state FROM %s WHERE element_id=?' % table, (element,)).fetchone()
        return row[0] if row else None
    return one('cast_decision'), one('steam_decision'), one('release_decision')


class CaseBase(unittest.TestCase):
    """Three synthetic elements: A (M1/X), B (M1/Y), C (M2/X)."""

    def setUp(self):
        self.db = connect(':memory:')
        cast.import_data(self.db, fx('abc_cast'))
        steam.import_data(self.db, fx('abc_steam'))
        strength.import_data(self.db, fx('abc_strength'))
        investigation.import_data(self.db, fx('investigation_ok'))
        self.assertEqual(element_states(self.db, 'A'), ('cast_accepted', 'cured', 'released'))
        self.assertEqual(element_states(self.db, 'B'), ('cast_accepted', 'cured', 'released'))
        self.assertEqual(element_states(self.db, 'C'), ('cast_accepted', 'cured', 'released'))
        strength.import_data(self.db, fx('abc_retest'))
        cast.decide(self.db, 'B')
        cast.decide(self.db, 'C')
        strength.decide(self.db, 'B')
        strength.decide(self.db, 'C')
        self.inv = self.db.execute('SELECT id FROM quality_investigation').fetchone()[0]

    def tearDown(self):
        self.db.close()

    def rechecks(self, mould_result, mix_result, prefix='R'):
        return investigation.import_data(self.db, {'rechecks': [
            {'id': prefix + '-M1', 'investigation_id': self.inv, 'axis': 'mould',
             'target_id': 'M1', 'target_version': 1, 'sampled_at': '2026-01-16T00:00:00',
             'signer': 'engineer', 'result': mould_result},
            {'id': prefix + '-X', 'investigation_id': self.inv, 'axis': 'mix',
             'target_id': 'X', 'target_version': 1, 'sampled_at': '2026-01-16T00:00:00',
             'signer': 'engineer', 'result': mix_result},
        ]})


class AbcCase(CaseBase):
    # -- the original gap -------------------------------------------------
    def test_version_one_cannot_escape_freeze(self):
        # B is a version-1 cast; the old future_mould_hold only blocked v>1.
        # The investigation scope must catch it anyway.
        self.assertNotEqual(element_states(self.db, 'B')[0], 'cast_accepted')
        self.assertNotEqual(element_states(self.db, 'B')[2], 'released')
        # C shares no mould with A but shares mix X.
        self.assertNotEqual(element_states(self.db, 'C')[0], 'cast_accepted')
        self.assertNotEqual(element_states(self.db, 'C')[2], 'released')

    def test_per_axis_and_union_windows(self):
        reps = investigation.build_scope(self.db)
        self.assertEqual(len(reps), 1)
        rep = reps[0]
        self.assertEqual(rep['mould_exposed'], ['A', 'B'])
        self.assertEqual(rep['mix_exposed'], ['A', 'C'])
        self.assertEqual(rep['union_exposed'], ['A', 'B', 'C'])
        self.assertEqual(rep['governed'], ['B', 'C'])  # origin A is held by strength
        self.assertEqual(rep['state'], 'undetermined')
        b_axes = {w['axis'] for w in rep['windows']['B']}
        c_axes = {w['axis'] for w in rep['windows']['C']}
        self.assertEqual(b_axes, {'mould'})
        self.assertEqual(c_axes, {'mix'})
        for w in rep['windows']['B'] + rep['windows']['C']:
            self.assertLessEqual(w['from'], w['finish_at'])

    # -- two independent diagnoses ---------------------------------------
    def test_mould_defect_keeps_b_and_releases_c(self):
        self.rechecks('fail', 'pass')
        self.assertEqual(element_states(self.db, 'B')[0], 'quality_investigation')
        self.assertEqual(element_states(self.db, 'B')[2], 'investigation_hold')
        self.assertEqual(element_states(self.db, 'C'), ('cast_accepted', 'cured', 'released'))
        rep = investigation.build_scope(self.db)[0]
        self.assertEqual((rep['mould']['state'], rep['mix']['state']), ('defective', 'excluded'))
        self.assertEqual(rep['governed'], ['B'])

    def test_mix_defect_keeps_c_and_releases_b(self):
        self.rechecks('pass', 'fail')
        self.assertEqual(element_states(self.db, 'C')[0], 'quality_investigation')
        self.assertEqual(element_states(self.db, 'C')[2], 'investigation_hold')
        self.assertEqual(element_states(self.db, 'B'), ('cast_accepted', 'cured', 'released'))
        rep = investigation.build_scope(self.db)[0]
        self.assertEqual((rep['mould']['state'], rep['mix']['state']), ('excluded', 'defective'))
        self.assertEqual(rep['governed'], ['C'])

    def test_exclusion_does_not_release_origin_itself(self):
        self.rechecks('pass', 'fail')
        # A's own specimen is still 35 MPa, so A must remain held.
        self.assertEqual(element_states(self.db, 'A')[2], 'strength_hold')

    def test_mixed_exposure_needs_both_axes_cleared(self):
        # D is cast on M1 with X: it is exposed on *both* axes.
        d = [dict(c, id='CAST-D', element_id='D', mould_id='M1', mix_id='X',
                  at='2026-01-10T06:00:00', finish_at='2026-01-10T07:00:00', version=1)
             for c in fx('abc_cast')['casts'] if c['id'] == 'CAST-A']
        cast.import_data(self.db, {'casts': d})
        rep = investigation.build_scope(self.db)[0]
        self.assertIn('D', rep['both_exposed'])
        # Clear only the mould axis: D must stay governed through the mix axis.
        investigation.import_data(self.db, {'rechecks': [
            {'id': 'RM', 'investigation_id': self.inv, 'axis': 'mould', 'target_id': 'M1',
             'target_version': 1, 'sampled_at': '2026-01-16T00:00:00', 'signer': 'a', 'result': 'pass'}]})
        rep = investigation.build_scope(self.db)[0]
        self.assertEqual(rep['mould']['state'], 'excluded')
        self.assertIn('D', rep['mix_exposed'])
        # D is no longer governed by the mould axis, but the mix axis still
        # covers it, so it remains pending.
        self.assertIn('D', rep['governed'])
        # D has no specimen yet; the freeze is visible where it matters, in
        # the cast decision that would allow demoulding.
        self.assertEqual(element_states(self.db, 'D')[0], 'quality_investigation')

    def test_contradictory_rechecks_stay_pending(self):
        investigation.import_data(self.db, {'rechecks': [
            {'id': 'R1', 'investigation_id': self.inv, 'axis': 'mould', 'target_id': 'M1',
             'target_version': 1, 'sampled_at': '2026-01-16T00:00:00', 'signer': 'a', 'result': 'fail'},
            {'id': 'R2', 'investigation_id': self.inv, 'axis': 'mould', 'target_id': 'M1',
             'target_version': 1, 'sampled_at': '2026-01-17T00:00:00', 'signer': 'b', 'result': 'pass'},
            {'id': 'R3', 'investigation_id': self.inv, 'axis': 'mix', 'target_id': 'X',
             'target_version': 1, 'sampled_at': '2026-01-16T00:00:00', 'signer': 'c', 'result': 'pass'},
        ]})
        rep = investigation.build_scope(self.db)[0]
        self.assertEqual(rep['mould']['state'], 'unresolved')
        self.assertEqual(rep['mix']['state'], 'excluded')
        self.assertEqual(len(rep['mould']['conflicts']), 2)
        self.assertEqual(element_states(self.db, 'B')[2], 'investigation_hold')

    def test_passing_specimen_of_other_element_does_not_exonerate(self):
        # B and C already have 45 MPa specimens; A's later specimen is a
        # separate fact and must not clear anyone.
        self.assertEqual(self.db.execute(
            "SELECT strength_mpa FROM specimen WHERE element_id='B'").fetchone()[0], 45)
        rep = investigation.build_scope(self.db)[0]
        self.assertIn('B', rep['governed'])
        self.assertIn('C', rep['governed'])

    # -- direct module calls ---------------------------------------------
    def test_direct_cast_decide_sees_investigation(self):
        self.assertEqual(cast.decide(self.db, 'B')['state'], 'quality_investigation')
        self.assertEqual(cast.decide(self.db, 'C')['state'], 'quality_investigation')

    # -- late back-fill ---------------------------------------------------
    def test_late_recheck_does_not_rewrite_history(self):
        receipts = outbound.import_data(self.db, {
            'contracts': [
                {'id': 'ENG', 'purpose': 'engineering', 'recipient': 'site',
                 'signed_at': '2026-01-05T00:00:00', 'signed_by': 'owner'},
            ],
            'requests': [
                {'id': 'REQ-B', 'element_id': 'B', 'contract_id': 'ENG',
                 'at': '2026-01-15T00:00:00'},
            ]})['receipts'][0]
        self.assertEqual(receipts['kind'], 'blocked')
        # Evidence dated *after* the request must not make the historical
        # decision look different retroactively.
        self.rechecks('fail', 'pass')
        outbound.recompute(self.db)
        versions = [r['version'] for r in self.db.execute(
            'SELECT version FROM outbound_receipt WHERE request_id=? ORDER BY version', ('REQ-B',))]
        self.assertEqual(versions, [1, 2])
        self.assertEqual(self.db.execute(
            'SELECT kind FROM outbound_receipt WHERE request_id=? AND version=1', ('REQ-B',)).fetchone()[0],
            'blocked')

    def test_receipt_before_investigation_known_stays_released(self):
        # Contract request happens before the investigation is known: the
        # historical receipt must not be blocked by later knowledge.
        outbound.import_data(self.db, {
            'contracts': [{'id': 'ENG', 'purpose': 'engineering', 'recipient': 'site',
                           'signed_at': '2026-01-05T00:00:00', 'signed_by': 'owner'}],
            'requests': [{'id': 'REQ-C-EARLY', 'element_id': 'C', 'contract_id': 'ENG',
                          'at': '2026-01-11T12:00:00'}]})
        receipt = self.db.execute(
            'SELECT * FROM outbound_receipt WHERE request_id=?', ('REQ-C-EARLY',)).fetchone()
        self.assertEqual(receipt['kind'], 'commercial_release')
        # A request after the low report is known is blocked.
        outbound.import_data(self.db, {
            'requests': [{'id': 'REQ-C-LATE', 'element_id': 'C', 'contract_id': 'ENG',
                          'at': '2026-01-15T00:00:00'}]})
        late = self.db.execute(
            'SELECT * FROM outbound_receipt WHERE request_id=?', ('REQ-C-LATE',)).fetchone()
        self.assertEqual(late['kind'], 'blocked')

    def test_repair_closes_future_only(self):
        investigation.import_data(self.db, {'repairs': [
            {'id': 'FIX-M1', 'axis': 'mould', 'target_id': 'M1', 'target_version': 1,
             'kind': 'repair', 'effective_at': '2026-01-20T00:00:00',
             'signer': 'engineer', 'note': 'replaced suspect insert'},
        ]})
        # B happened before the repair and stays exposed.
        self.assertEqual(element_states(self.db, 'B')[2], 'investigation_hold')
        # A fresh cast after the repair is outside the mould window.
        new = [dict(c, id='CAST-D', element_id='D', mould_id='M1', mix_id='Y',
                    at='2026-01-21T00:00:00', finish_at='2026-01-21T01:00:00', version=1)
               for c in fx('abc_cast')['casts'] if c['id'] == 'CAST-B']
        cast.import_data(self.db, {'casts': new})
        rep = investigation.build_scope(self.db)[0]
        self.assertNotIn('D', rep['mould_exposed'])

    def test_mix_replacement_only_closes_mix_window(self):
        investigation.import_data(self.db, {'repairs': [
            {'id': 'NEW-X', 'axis': 'mix', 'target_id': 'X', 'target_version': 1,
             'kind': 'material_change', 'effective_at': '2026-01-20T00:00:00',
             'signer': 'engineer'},
        ]})
        self.assertEqual(element_states(self.db, 'C')[2], 'investigation_hold')
        new = [dict(c, id='CAST-D', element_id='D', mould_id='M2', mix_id='X',
                    at='2026-01-21T00:00:00', finish_at='2026-01-21T01:00:00', version=1)
               for c in fx('abc_cast')['casts'] if c['id'] == 'CAST-C']
        cast.import_data(self.db, {'casts': new})
        rep = investigation.build_scope(self.db)[0]
        self.assertNotIn('D', rep['mix_exposed'])

    def test_mould_window_starts_at_last_qualified_check(self):
        # A failed check after the last qualified one does not close the
        # window (only an effective repair does), and it does not move the
        # start forward either: the window remains [last qualified check, repair].
        investigation.import_data(self.db, {'mould_checks': [
            {'id': 'CHK-M1-FAIL', 'mould_id': 'M1', 'mould_version': 1,
             'at': '2026-01-10T00:30:00', 'valid_until': '2026-01-10T00:30:00',
             'result': 'fail', 'signer': 'engineer'},
        ]})
        rep = investigation.build_scope(self.db)[0]
        self.assertIn('B', rep['mould_exposed'])
        self.assertEqual(rep['mould']['window']['from'], '2026-01-01T00:00:00')
        self.assertIsNone(rep['mould']['window']['to'])
        # A check performed *after* B's cast cannot retroactively qualify it:
        # the anchor is the last qualified check at or before the failing cast.
        investigation.import_data(self.db, {'mould_checks': [
            {'id': 'CHK-M1-LATE', 'mould_id': 'M1', 'mould_version': 1,
             'at': '2026-01-12T00:00:00', 'valid_until': '2026-03-01T00:00:00',
             'result': 'pass', 'signer': 'engineer'},
        ]})
        rep = investigation.build_scope(self.db)[0]
        self.assertEqual(rep['mould']['window']['from'], '2026-01-01T00:00:00')
        self.assertIn('B', rep['mould_exposed'])

    def test_certificate_withdrawal_recomputes(self):
        self.rechecks('pass', 'fail')
        self.assertEqual(element_states(self.db, 'B')[2], 'released')
        investigation.import_data(self.db, {'revocations': [
            {'id': 'REV-1', 'cert_kind': 'recheck', 'cert_id': 'R-M1',
             'revoked_at': '2026-01-20T00:00:00', 'signer': 'lab'},
        ]})
        # With the mould recheck withdrawn only the mix axis remains, so C stays
        # governed and B is no longer tied to a live exclusion.
        rep = investigation.build_scope(self.db)[0]
        self.assertEqual(rep['mould']['state'], 'unresolved')
        self.assertIn('R-M1', rep['mould']['broken_rechecks'])


class OutboundCase(CaseBase):
    def _contracts(self, purpose_by_id):
        contracts = [
            {'id': cid, 'purpose': purpose, 'recipient': 'r-' + cid,
             'signed_at': '2026-01-05T00:00:00', 'signed_by': 'owner'}
            for cid, purpose in purpose_by_id.items()
        ]
        requests = [
            {'id': 'REQ-%s-%s' % (element, cid), 'element_id': element, 'contract_id': cid,
             'at': '2026-01-15T00:00:00'}
            for element in ('B', 'C') for cid in purpose_by_id
        ]
        return outbound.import_data(self.db, {'contracts': contracts, 'requests': requests})

    def latest(self, element, contract):
        return self.db.execute(
            'SELECT * FROM outbound_receipt WHERE element_id=? AND contract_id=?'
            ' ORDER BY version DESC LIMIT 1', (element, contract)).fetchone()

    def test_engineering_vs_destructive_same_instant(self):
        self._contracts({'ENG': 'engineering', 'LAB': 'destructive_review'})
        b_eng, b_lab = self.latest('B', 'ENG'), self.latest('B', 'LAB')
        c_eng, c_lab = self.latest('C', 'ENG'), self.latest('C', 'LAB')
        self.assertEqual((b_eng['kind'], b_eng['usable_for']), ('blocked', 'none'))
        self.assertEqual((c_eng['kind'], c_eng['usable_for']), ('blocked', 'none'))
        self.assertEqual((b_lab['kind'], b_lab['usable_for']),
                         ('sealed_sample_transfer', 'laboratory_receipt_only'))
        self.assertEqual((c_lab['kind'], c_lab['usable_for']),
                         ('sealed_sample_transfer', 'laboratory_receipt_only'))
        self.assertEqual(b_lab['inventory_state'], 'pending_investigation')
        self.assertEqual(c_lab['inventory_state'], 'pending_investigation')
        self.assertNotEqual(b_lab['kind'], b_eng['kind'])

    def test_exclusion_restores_only_the_cleared_element(self):
        self._contracts({'ENG': 'engineering', 'LAB': 'destructive_review'})
        self.rechecks('fail', 'pass')  # M1 defective, X excluded -> C clear
        outbound.recompute(self.db)
        self.assertEqual(self.latest('C', 'ENG')['kind'], 'commercial_release')
        self.assertEqual(self.latest('B', 'ENG')['kind'], 'blocked')
        # The lab receipt for C remains a lab-only document.
        self.assertEqual(self.latest('C', 'LAB')['usable_for'], 'laboratory_receipt_only')

    def test_historical_receipt_not_rewritten_by_later_evidence(self):
        self._contracts({'ENG': 'engineering'})
        before = dict(self.latest('C', 'ENG'))
        self.rechecks('pass', 'fail')  # C now governed
        outbound.recompute(self.db)
        old = self.db.execute(
            'SELECT * FROM outbound_receipt WHERE request_id=? AND version=1',
            ('REQ-C-ENG',)).fetchone()
        self.assertEqual(dict(old), before)
        new = self.latest('C', 'ENG')
        self.assertEqual(new['version'], 2)


class TraceCase(CaseBase):
    def _contracts(self):
        outbound.import_data(self.db, {'contracts': [
            {'id': 'ENG', 'purpose': 'engineering', 'recipient': 'site',
             'signed_at': '2026-01-05T00:00:00', 'signed_by': 'owner'}],
            'requests': [
                {'id': 'REQ-B', 'element_id': 'B', 'contract_id': 'ENG',
                 'at': '2026-01-15T00:00:00'},
                {'id': 'REQ-C', 'element_id': 'C', 'contract_id': 'ENG',
                 'at': '2026-01-15T00:00:00'}]})

    def latest(self, request_id):
        return self.db.execute(
            'SELECT * FROM outbound_receipt WHERE request_id=? ORDER BY version DESC LIMIT 1',
            (request_id,)).fetchone()

    def test_new_low_strength_result_recomputes_receipt(self):
        self._contracts()
        self.rechecks('fail', 'pass')  # B blocked, C released
        outbound.recompute(self.db)
        self.assertEqual(self.latest('REQ-C')['kind'], 'commercial_release')
        # A fresh low result on C arrives late: recompute must withdraw it.
        strength.import_data(self.db, {'specimens': [
            {'id': 'SPEC-C2', 'element_id': 'C', 'cast_id': 'CAST-C',
             'cycle_id': 'CYCLE-C', 'lab_id': 'LAB-C', 'at': '2026-01-18T00:00:00',
             'strength_mpa': 33, 'kind': 'retest'}]})
        outbound.recompute(self.db)
        self.assertEqual(self.latest('REQ-C')['kind'], 'blocked')
        self.assertGreater(self.latest('REQ-C')['version'], 1)

    def test_certificate_revocation_recomputes(self):
        self._contracts()
        self.rechecks('fail', 'pass')
        outbound.recompute(self.db)
        self.assertEqual(self.latest('REQ-C')['kind'], 'commercial_release')
        investigation.import_data(self.db, {'revocations': [
            {'id': 'REV-LAB-C', 'cert_kind': 'lab', 'cert_id': 'LAB-C',
             'revoked_at': '2026-01-20T00:00:00', 'signer': 'lab'}]})
        outbound.recompute(self.db)
        self.assertEqual(self.latest('REQ-C')['kind'], 'blocked')
        # The earlier released receipt version remains as a historical fact.
        old = self.db.execute(
            'SELECT kind FROM outbound_receipt WHERE request_id=? AND version=2',
            ('REQ-C',)).fetchone()[0]
        self.assertEqual(old, 'commercial_release')

    def test_loaded_gets_traceback_unloaded_blocked(self):
        strength.import_data(self.db, {'shipping': [
            {'id': 'SHIP-B', 'element_id': 'B', 'loaded_at': '2026-01-13T00:00:00',
             'recipient': 'site'},
        ]})
        outbound.recompute(self.db)
        rows = {r['element_id']: r['state'] for r in self.db.execute(
            'SELECT element_id,state FROM trace_record')}
        self.assertEqual(rows['B'], 'traceback')
        self.assertEqual(rows['C'], 'shipment_blocked')
        self.assertNotIn('A', rows)  # origin is held by its own specimen

    def test_repair_does_not_erase_history(self):
        strength.import_data(self.db, {'shipping': [
            {'id': 'SHIP-B', 'element_id': 'B', 'loaded_at': '2026-01-13T00:00:00',
             'recipient': 'site'}]})
        outbound.recompute(self.db)
        investigation.import_data(self.db, {'repairs': [
            {'id': 'FIX-M1', 'axis': 'mould', 'target_id': 'M1', 'target_version': 1,
             'kind': 'repair', 'effective_at': '2026-01-20T00:00:00', 'signer': 'engineer'}]})
        outbound.recompute(self.db)
        states = [r['state'] for r in self.db.execute(
            "SELECT state FROM trace_record WHERE element_id='B' ORDER BY version")]
        self.assertEqual(states[0], 'traceback')
        self.assertGreaterEqual(len(states), 1)

    def test_exclusion_appends_clearance_without_erasing_exposure(self):
        outbound.recompute(self.db)
        before = [r['state'] for r in self.db.execute(
            "SELECT state FROM trace_record WHERE element_id='B' ORDER BY version")]
        self.assertEqual(before, ['shipment_blocked'])
        self.rechecks('fail', 'pass')  # mix excluded but B remains mould-exposed
        outbound.recompute(self.db)
        after = [r['state'] for r in self.db.execute(
            "SELECT state FROM trace_record WHERE element_id='B' ORDER BY version")]
        self.assertEqual(after[0], 'shipment_blocked')
        # C is cleared; the earlier blocked row is retained alongside it.
        c_states = [r['state'] for r in self.db.execute(
            "SELECT state FROM trace_record WHERE element_id='C' ORDER BY version")]
        self.assertEqual(c_states[0], 'shipment_blocked')
        self.assertEqual(c_states[-1], 'cleared')


class IdempotencyCase(CaseBase):
    def test_investigation_reupload_idempotent(self):
        doc = {'investigations': []}
        # The auto-created investigation is reused by a matching explicit intake.
        explicit = {'id': self.inv, 'specimen_id': 'SPEC-A2', 'cast_id': 'CAST-A',
                    'cycle_id': 'CYCLE-A', 'lab_cert_id': 'LAB-A',
                    'known_at': '2026-01-12T00:00:00'}
        investigation.import_data(self.db, {'investigations': [explicit]})
        count = self.db.execute('SELECT count(*) FROM quality_investigation').fetchone()[0]
        investigation.import_data(self.db, {'investigations': [explicit]})
        self.assertEqual(self.db.execute(
            'SELECT count(*) FROM quality_investigation').fetchone()[0], count)

    def test_conflicting_recheck_file_does_not_overwrite(self):
        self.rechecks('fail', 'pass', prefix='R')
        before = self.db.execute('SELECT result FROM axis_recheck WHERE id=?', ('R-M1',)).fetchone()[0]
        with self.assertRaises(ValueError):
            investigation.import_data(self.db, {'rechecks': [
                {'id': 'R-M1', 'investigation_id': self.inv, 'axis': 'mould',
                 'target_id': 'M1', 'target_version': 1, 'sampled_at': '2026-01-16T00:00:00',
                 'signer': 'engineer', 'result': 'pass'}]})
        self.assertEqual(self.db.execute(
            'SELECT result FROM axis_recheck WHERE id=?', ('R-M1',)).fetchone()[0], before)

    def test_reupload_same_file_is_idempotent(self):
        from steamcast.source import file_record, canon
        payload = fx('abc_retest')
        self.assertTrue(file_record(self.db, 'strength-import', canon(payload)))
        self.assertFalse(file_record(self.db, 'strength-import', canon(payload)))
        self.assertEqual(self.db.execute(
            "SELECT count(*) FROM file_journal WHERE command='strength-import'").fetchone()[0], 1)

    def test_mould_and_mix_both_unresolved_use_union(self):
        rep = investigation.build_scope(self.db)[0]
        self.assertEqual((rep['mould']['state'], rep['mix']['state']),
                         ('unresolved', 'unresolved'))
        self.assertEqual(rep['governed'], sorted((set(rep['mould_exposed']) | set(rep['mix_exposed'])) - {'A'}))

    def test_axis_recheck_of_wrong_version_does_not_count(self):
        # A recheck of M1 v2 cannot clear casts made under M1 v1.
        investigation.import_data(self.db, {'rechecks': [
            {'id': 'R-M1V2', 'investigation_id': self.inv, 'axis': 'mould',
             'target_id': 'M1', 'target_version': 2, 'sampled_at': '2026-01-16T00:00:00',
             'signer': 'engineer', 'result': 'pass'},
        ]})
        rep = investigation.build_scope(self.db)[0]
        self.assertEqual(rep['mould']['state'], 'unresolved')
        self.assertIn('B', rep['governed'])

    def test_missing_qualified_check_leaves_window_open(self):
        # A fresh database without mould_checks: the mould axis cannot be
        # narrowed, so every cast on that mould remains a candidate.
        db = connect(':memory:')
        try:
            cast.import_data(db, fx('abc_cast'))
            steam.import_data(db, fx('abc_steam'))
            strength.import_data(db, fx('abc_strength'))
            strength.import_data(db, fx('abc_retest'))
            rep = investigation.build_scope(db)[0]
            self.assertEqual(rep['mould']['window'], {'from': None, 'to': None, 'open': True})
            self.assertEqual(rep['mould_exposed'], ['A', 'B'])
            self.assertEqual(rep['governed'], ['B', 'C'])
        finally:
            db.close()


class CliCase(unittest.TestCase):
    def test_cli_investigation_flow(self):
        with tempfile.TemporaryDirectory() as d:
            db = str(Path(d) / 'abc.sqlite')

            def run(*args):
                p = subprocess.run([sys.executable, '-m', 'steamcast', *args, '--db', db],
                                   cwd=ROOT, capture_output=True, text=True)
                self.assertEqual(p.returncode, 0, p.stderr)
                return p.stdout

            for cmd, name in (('cast-import', 'abc_cast'), ('steam-import', 'abc_steam'),
                              ('strength-import', 'abc_strength'),
                              ('investigation-import', 'investigation_ok'),
                              ('strength-import', 'abc_retest')):
                run(cmd, '--input', str(ROOT / 'fixtures' / (name + '.json')))
            payload = json.loads(run('investigations'))
            self.assertEqual(payload[0]['mould']['exposed'], ['A', 'B'])
            self.assertEqual(payload[0]['mix']['exposed'], ['A', 'C'])
            self.assertEqual(payload[0]['union_exposed'], ['A', 'B', 'C'])
            self.assertTrue(json.loads(run('audit'))['ok'])

    def test_cli_outbound_purpose_switch(self):
        with tempfile.TemporaryDirectory() as d:
            db = str(Path(d) / 'abc.sqlite')

            def run(*args):
                p = subprocess.run([sys.executable, '-m', 'steamcast', *args, '--db', db],
                                   cwd=ROOT, capture_output=True, text=True)
                self.assertEqual(p.returncode, 0, p.stderr)
                return p.stdout

            for cmd, name in (('cast-import', 'abc_cast'), ('steam-import', 'abc_steam'),
                              ('strength-import', 'abc_strength'),
                              ('investigation-import', 'investigation_ok'),
                              ('strength-import', 'abc_retest'),
                              ('outbound-import', 'abc_outbound')):
                run(cmd, '--input', str(ROOT / 'fixtures' / (name + '.json')))
            receipts = {(r['request_id']): r for r in json.loads(run('receipts'))}
            self.assertEqual(receipts['REQ-B-ENG']['kind'], 'blocked')
            self.assertEqual(receipts['REQ-C-ENG']['kind'], 'blocked')
            self.assertEqual(receipts['REQ-B-LAB']['kind'], 'sealed_sample_transfer')
            self.assertEqual(receipts['REQ-B-LAB']['usable_for'], 'laboratory_receipt_only')
            self.assertEqual(receipts['REQ-C-LAB']['inventory_state'], 'pending_investigation')
            self.assertTrue(json.loads(run('audit'))['ok'])

    def _abc_db(self, d, extra=()):
        db = str(Path(d) / 'abc.sqlite')

        def run(*args):
            p = subprocess.run([sys.executable, '-m', 'steamcast', *args, '--db', db],
                               cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(p.returncode, 0, p.stderr)
            return p.stdout

        for cmd, name in (('cast-import', 'abc_cast'), ('steam-import', 'abc_steam'),
                          ('strength-import', 'abc_strength'),
                          ('investigation-import', 'investigation_ok'),
                          ('strength-import', 'abc_retest'),
                          ('outbound-import', 'abc_outbound')):
            run(cmd, '--input', str(ROOT / 'fixtures' / (name + '.json')))
        run('recompute')
        for cmd, name in extra:
            run(cmd, '--input', str(ROOT / 'fixtures' / (name + '.json')))
        run('recompute')
        return run

    def _latest_receipts(self, run):
        by_req = {}
        for r in json.loads(run('receipts')):
            if r['request_id'] not in by_req or r['version'] > by_req[r['request_id']]['version']:
                by_req[r['request_id']] = r
        return by_req

    def test_cli_diagnosis_mould_defect(self):
        with tempfile.TemporaryDirectory() as d:
            run = self._abc_db(d, [('investigation-import', 'abc_diag_mould')])
            scope = json.loads(run('investigations'))[0]
            self.assertEqual((scope['mould']['state'], scope['mix']['state']),
                             ('defective', 'excluded'))
            r = self._latest_receipts(run)
            self.assertEqual(r['REQ-B-ENG']['kind'], 'blocked')
            self.assertEqual(r['REQ-C-ENG']['kind'], 'commercial_release')
            self.assertTrue(json.loads(run('audit'))['ok'])

    def test_cli_diagnosis_mix_defect(self):
        with tempfile.TemporaryDirectory() as d:
            run = self._abc_db(d, [('investigation-import', 'abc_diag_mix')])
            scope = json.loads(run('investigations'))[0]
            self.assertEqual((scope['mould']['state'], scope['mix']['state']),
                             ('excluded', 'defective'))
            r = self._latest_receipts(run)
            self.assertEqual(r['REQ-C-ENG']['kind'], 'blocked')
            self.assertEqual(r['REQ-B-ENG']['kind'], 'commercial_release')
            self.assertTrue(json.loads(run('audit'))['ok'])


if __name__ == '__main__':
    unittest.main()
