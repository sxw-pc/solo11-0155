from .source import record
from .store import history
def overlap(a,b):return a['at']<b['finish_at'] and b['at']<a['finish_at']
def decide(db,element_id):
    rows=db.execute('SELECT * FROM cast_batch WHERE element_id=? ORDER BY version DESC,id',(element_id,)).fetchall()
    if not rows:return None
    row=rows[0];same=[r for r in rows if r['version']==row['version']]
    mould=db.execute('SELECT * FROM mould WHERE id=?',(row['mould_id'],)).fetchone()
    mix=db.execute('SELECT * FROM mix_approval WHERE id=?',(row['mix_id'],)).fetchone()
    hold=db.execute('SELECT * FROM future_mould_hold WHERE mould_id=?',(row['mould_id'],)).fetchone()
    conflicts=[r for r in db.execute('SELECT * FROM cast_batch WHERE mould_id=? AND id<>?',(row['mould_id'],row['id'])) if overlap(r,row) and r['element_id']!=element_id]
    if len({(r['mould_id'],r['mix_id'],r['size']) for r in same})>1:state,reason='version_conflict','same version has different mould or mix'
    elif not mould or row['size']>mould['max_size'] or mould['inspected_until']<row['at']:state,reason='mould_invalid','mould does not fit element or inspection expired'
    elif conflicts:state,reason='mould_busy','mould interval overlaps another element'
    elif not mix or mix['grade']<row['grade'] or mix['approved_at']>row['at'] or mix['approved_by']!='engineer':state,reason='mix_invalid','mix grade/version not approved before pour'
    elif hold and hold['state']=='blocked' and row['version']>1:state,reason='quality_hold','late strength failure blocks future mould usage'
    else:state,reason='cast_accepted','mould, occupancy and mix accepted'
    old=db.execute('SELECT * FROM cast_decision WHERE element_id=?',(element_id,)).fetchone()
    if not old or (old['state'],old['reason'],old['batch_id'])!=(state,reason,row['id']):
        ver=old['version']+1 if old else 1
        db.execute('INSERT INTO cast_decision VALUES(?,?,?,?,?) ON CONFLICT(element_id) DO UPDATE SET state=excluded.state,reason=excluded.reason,batch_id=excluded.batch_id,version=excluded.version',(element_id,state,reason,row['id'],ver))
        history(db,'cast',element_id,state,reason,row['id'])
    from .steam import decide as steam
    steam(db,element_id)
    return dict(db.execute('SELECT * FROM cast_decision WHERE element_id=?',(element_id,)).fetchone())
def import_data(db,data):
    touched=set()
    with db:
        for m in data.get('moulds',[]):
            if record(db,'mould',m):db.execute('INSERT INTO mould VALUES(?,?,?,?)',(m['id'],m['version'],m['max_size'],m['inspected_until']))
            touched.update(r['element_id'] for r in db.execute('SELECT element_id FROM cast_batch WHERE mould_id=?',(m['id'],)))
        for m in data.get('mixes',[]):
            if record(db,'mix',m):db.execute('INSERT INTO mix_approval VALUES(?,?,?,?,?)',(m['id'],m['grade'],m['version'],m['approved_at'],m['approved_by']))
            touched.update(r['element_id'] for r in db.execute('SELECT element_id FROM cast_batch WHERE mix_id=?',(m['id'],)))
        for c in data.get('casts',[]):
            if record(db,'cast',c):db.execute('INSERT INTO cast_batch VALUES(?,?,?,?,?,?,?,?,?)',(c['id'],c['element_id'],c['mould_id'],c['mix_id'],c['at'],c['finish_at'],c['size'],c['grade'],c['version']))
            touched.add(c['element_id'])
        return [decide(db,e) for e in sorted(touched)]
