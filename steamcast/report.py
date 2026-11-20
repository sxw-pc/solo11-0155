import json
from .store import connect
from . import cast,steam,strength
def build(db):
    ids=[r[0] for r in db.execute('SELECT DISTINCT element_id FROM cast_batch UNION SELECT DISTINCT element_id FROM steam_cycle UNION SELECT DISTINCT element_id FROM specimen ORDER BY element_id')]
    return [{'element_id':e,'cast':dict(x) if (x:=db.execute('SELECT * FROM cast_decision WHERE element_id=?',(e,)).fetchone()) else None,'steam':dict(x) if (x:=db.execute('SELECT * FROM steam_decision WHERE element_id=?',(e,)).fetchone()) else None,'release':dict(x) if (x:=db.execute('SELECT * FROM release_decision WHERE element_id=?',(e,)).fetchone()) else None,'history':[dict(x) for x in db.execute('SELECT subsystem,state,reason,source_id FROM decision_history WHERE element_id=? ORDER BY id',(e,))]} for e in ids]
def audit(db):
    replay=connect(':memory:');handlers={'cast-import':cast.import_data,'steam-import':steam.import_data,'strength-import':strength.import_data}
    for r in db.execute('SELECT command,raw FROM file_journal ORDER BY id'):handlers[r['command']](replay,json.loads(r['raw']))
    equal=build(db)==build(replay);replay.close();return {'ok':equal,'files':db.execute('SELECT count(*) FROM file_journal').fetchone()[0]}
