from .source import record
from .store import history
def decide(db,element_id):
    samples=db.execute('SELECT * FROM specimen WHERE element_id=? ORDER BY at DESC,id',(element_id,)).fetchall()
    if not samples:return None
    s=samples[0]
    cast=db.execute('SELECT * FROM cast_decision WHERE element_id=?',(element_id,)).fetchone()
    steam=db.execute('SELECT * FROM steam_decision WHERE element_id=?',(element_id,)).fetchone()
    lab=db.execute('SELECT * FROM lab_cert WHERE id=?',(s['lab_id'],)).fetchone()
    shipping=db.execute('SELECT * FROM shipping WHERE element_id=? ORDER BY loaded_at DESC',(element_id,)).fetchone()
    batch=db.execute('SELECT * FROM cast_batch WHERE id=?',(cast['batch_id'],)).fetchone() if cast else None
    if not lab or not(lab['start']<=s['at']<=lab['end']):state,reason='lab_invalid','strength test lab unqualified at reading time'
    elif not batch or s['cast_id']!=batch['id'] or not steam or s['cycle_id']!=steam['cycle_id']:state,reason='specimen_mismatch','specimen not co-cured with element'
    elif s['strength_mpa']<40 and shipping and shipping['loaded_at']:state,reason='traceback','loaded element has failing late strength result'
    elif s['strength_mpa']<40:state,reason='strength_hold','minimum strength not attained; do not ship'
    elif not steam or steam['state']!='cured':state,reason='curing_hold','steam cycle not qualified for demoulding'
    else:state,reason='released','co-cured specimen meets strength and curing requirements'
    old=db.execute('SELECT * FROM release_decision WHERE element_id=?',(element_id,)).fetchone()
    if not old or (old['state'],old['reason'],old['specimen_id'])!=(state,reason,s['id']):
        ver=old['version']+1 if old else 1
        db.execute('INSERT INTO release_decision VALUES(?,?,?,?,?) ON CONFLICT(element_id) DO UPDATE SET state=excluded.state,reason=excluded.reason,specimen_id=excluded.specimen_id,version=excluded.version',(element_id,state,reason,s['id'],ver))
        history(db,'strength',element_id,state,reason,s['id'])
    if state in ('strength_hold','traceback') and batch:
        db.execute('INSERT INTO future_mould_hold VALUES(?,?,?) ON CONFLICT(mould_id) DO UPDATE SET state=excluded.state,reason=excluded.reason',(batch['mould_id'],'blocked','late strength failure requires mould/process review'))
    return dict(db.execute('SELECT * FROM release_decision WHERE element_id=?',(element_id,)).fetchone())
def import_data(db,data):
    touched=set()
    with db:
        for l in data.get('labs',[]):
            if record(db,'lab',l):db.execute('INSERT INTO lab_cert VALUES(?,?,?,?)',(l['id'],l['lab'],l['start'],l['end']))
            touched.update(r['element_id'] for r in db.execute('SELECT element_id FROM specimen WHERE lab_id=?',(l['id'],)))
        for s in data.get('specimens',[]):
            if record(db,'specimen',s):db.execute('INSERT INTO specimen VALUES(?,?,?,?,?,?,?,?)',(s['id'],s['element_id'],s['cast_id'],s['cycle_id'],s['lab_id'],s['at'],s['strength_mpa'],s.get('kind','initial')))
            touched.add(s['element_id'])
        for sh in data.get('shipping',[]):
            if record(db,'shipping',sh):db.execute('INSERT INTO shipping VALUES(?,?,?,?)',(sh['id'],sh['element_id'],sh.get('loaded_at'),sh['recipient']))
            touched.add(sh['element_id'])
        return [decide(db,e) for e in sorted(touched) if db.execute('SELECT 1 FROM specimen WHERE element_id=?',(e,)).fetchone()]
