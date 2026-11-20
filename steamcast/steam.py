from datetime import datetime
from .source import record
from .store import history
def minutes(start,end):return (datetime.fromisoformat(end)-datetime.fromisoformat(start)).total_seconds()/60
def decide(db,element_id):
    cycle=db.execute('SELECT * FROM steam_cycle WHERE element_id=? ORDER BY start DESC,id',(element_id,)).fetchone()
    if not cycle:return None
    cert=db.execute('SELECT * FROM sensor_cert WHERE id=?',(cycle['sensor_id'],)).fetchone()
    samples=db.execute('SELECT * FROM temp_sample WHERE cycle_id=? ORDER BY at,id',(cycle['id'],)).fetchall()
    cast=db.execute('SELECT * FROM cast_decision WHERE element_id=?',(element_id,)).fetchone()
    if not cert or cert['valid_from']>cycle['start'] or cert['valid_to']<cycle['end']:state,reason='sensor_invalid','calibration must span entire cycle'
    elif len(samples)<3 or samples[0]['at']>cycle['start'] or samples[-1]['at']<cycle['end']:state,reason='incomplete_curve','temperature evidence missing at cycle boundaries'
    elif any(minutes(a['at'],b['at'])<=0 or minutes(a['at'],b['at'])>cycle['max_gap_minutes'] for a,b in zip(samples,samples[1:])):state,reason='curve_gap','sampling gap breaks continuous curing evidence'
    elif any((b['celsius']-a['celsius'])*60/minutes(a['at'],b['at'])>cycle['max_ramp_cph'] for a,b in zip(samples,samples[1:])):state,reason='ramp_exceeded','temperature rise faster than material limit'
    elif sum(minutes(a['at'],b['at']) for a,b in zip(samples,samples[1:]) if a['celsius']>=cycle['hold_temp_c'] and b['celsius']>=cycle['hold_temp_c'])<cycle['min_hold_minutes']:state,reason='hold_short','insufficient time continuously above curing temperature'
    elif not cast or cast['state']!='cast_accepted':state,reason='cast_hold','mould or mix decision blocks demoulding'
    else:state,reason='cured','continuous calibrated cycle qualifies for demoulding'
    old=db.execute('SELECT * FROM steam_decision WHERE element_id=?',(element_id,)).fetchone()
    if not old or (old['state'],old['reason'],old['cycle_id'])!=(state,reason,cycle['id']):
        ver=old['version']+1 if old else 1
        db.execute('INSERT INTO steam_decision VALUES(?,?,?,?,?) ON CONFLICT(element_id) DO UPDATE SET state=excluded.state,reason=excluded.reason,cycle_id=excluded.cycle_id,version=excluded.version',(element_id,state,reason,cycle['id'],ver))
        history(db,'steam',element_id,state,reason,cycle['id'])
    from .strength import decide as strength
    strength(db,element_id)
    return dict(db.execute('SELECT * FROM steam_decision WHERE element_id=?',(element_id,)).fetchone())
def import_data(db,data):
    touched=set()
    with db:
        for c in data.get('sensors',[]):
            if record(db,'sensor',c):db.execute('INSERT INTO sensor_cert VALUES(?,?,?,?,?)',(c['id'],c['sensor'],c['valid_from'],c['valid_to'],c['operator']))
            touched.update(r['element_id'] for r in db.execute('SELECT element_id FROM steam_cycle WHERE sensor_id=?',(c['id'],)))
        for c in data.get('cycles',[]):
            if record(db,'cycle',c):db.execute('INSERT INTO steam_cycle VALUES(?,?,?,?,?,?,?,?,?)',(c['id'],c['element_id'],c['sensor_id'],c['start'],c['end'],c['min_hold_minutes'],c['hold_temp_c'],c['max_ramp_cph'],c['max_gap_minutes']))
            touched.add(c['element_id'])
        for s in data.get('samples',[]):
            if record(db,'temp',s):db.execute('INSERT INTO temp_sample VALUES(?,?,?,?)',(s['id'],s['cycle_id'],s['at'],s['celsius']))
            touched.update(r['element_id'] for r in db.execute('SELECT element_id FROM steam_cycle WHERE id=?',(s['cycle_id'],)))
        return [decide(db,e) for e in sorted(touched)]
