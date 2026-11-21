import sqlite3
SCHEMA="""
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS evidence(id TEXT PRIMARY KEY,kind TEXT NOT NULL,sha TEXT NOT NULL,raw BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS file_journal(id INTEGER PRIMARY KEY,command TEXT NOT NULL,sha TEXT NOT NULL,raw BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS mould(id TEXT PRIMARY KEY,version INTEGER NOT NULL,max_size INTEGER NOT NULL,inspected_until TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS mix_approval(id TEXT PRIMARY KEY,grade INTEGER NOT NULL,version INTEGER NOT NULL,approved_at TEXT NOT NULL,approved_by TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cast_batch(id TEXT PRIMARY KEY,element_id TEXT NOT NULL,mould_id TEXT NOT NULL,mix_id TEXT NOT NULL,at TEXT NOT NULL,finish_at TEXT NOT NULL,size INTEGER NOT NULL,grade INTEGER NOT NULL,version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS cast_decision(element_id TEXT PRIMARY KEY,state TEXT NOT NULL,reason TEXT NOT NULL,batch_id TEXT,version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS sensor_cert(id TEXT PRIMARY KEY,sensor TEXT NOT NULL,valid_from TEXT NOT NULL,valid_to TEXT NOT NULL,operator TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS steam_cycle(id TEXT PRIMARY KEY,element_id TEXT NOT NULL,sensor_id TEXT NOT NULL,start TEXT NOT NULL,end TEXT NOT NULL,min_hold_minutes INTEGER NOT NULL,hold_temp_c REAL NOT NULL,max_ramp_cph REAL NOT NULL,max_gap_minutes INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS temp_sample(id TEXT PRIMARY KEY,cycle_id TEXT NOT NULL,at TEXT NOT NULL,celsius REAL NOT NULL);
CREATE TABLE IF NOT EXISTS steam_decision(element_id TEXT PRIMARY KEY,state TEXT NOT NULL,reason TEXT NOT NULL,cycle_id TEXT,version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS lab_cert(id TEXT PRIMARY KEY,lab TEXT NOT NULL,start TEXT NOT NULL,end TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS specimen(id TEXT PRIMARY KEY,element_id TEXT NOT NULL,cast_id TEXT NOT NULL,cycle_id TEXT NOT NULL,lab_id TEXT NOT NULL,at TEXT NOT NULL,strength_mpa REAL NOT NULL,kind TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS shipping(id TEXT PRIMARY KEY,element_id TEXT NOT NULL,loaded_at TEXT,recipient TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS release_decision(element_id TEXT PRIMARY KEY,state TEXT NOT NULL,reason TEXT NOT NULL,specimen_id TEXT,version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS future_mould_hold(mould_id TEXT PRIMARY KEY,state TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS decision_history(id INTEGER PRIMARY KEY,subsystem TEXT NOT NULL,element_id TEXT NOT NULL,state TEXT NOT NULL,reason TEXT NOT NULL,source_id TEXT,created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS recorder_page(id TEXT PRIMARY KEY,cycle_id TEXT NOT NULL,device_id TEXT NOT NULL,run_id TEXT,route TEXT NOT NULL,raw BLOB NOT NULL,received_seq INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS recorder_frame(id TEXT PRIMARY KEY,page_id TEXT NOT NULL,position INTEGER NOT NULL,local_counter INTEGER NOT NULL,local_clock TEXT NOT NULL,celsius REAL NOT NULL,UNIQUE(page_id,position));
CREATE TABLE IF NOT EXISTS recorder_observation(id TEXT PRIMARY KEY,cycle_id TEXT NOT NULL,absolute_at TEXT NOT NULL,celsius REAL,state TEXT NOT NULL,reason TEXT NOT NULL,authorizing_route TEXT);
CREATE TABLE IF NOT EXISTS recorder_observation_source(observation_id TEXT NOT NULL,frame_id TEXT NOT NULL,route TEXT NOT NULL,PRIMARY KEY(observation_id,frame_id));
CREATE TABLE IF NOT EXISTS recorder_pending(id TEXT PRIMARY KEY,cycle_id TEXT NOT NULL,reason TEXT NOT NULL,next_action TEXT NOT NULL);
"""
def connect(path):
    db=sqlite3.connect(path);db.row_factory=sqlite3.Row;db.executescript(SCHEMA);return db
def history(db,subsystem,element_id,state,reason,source_id=None):
    db.execute('INSERT INTO decision_history(subsystem,element_id,state,reason,source_id) VALUES(?,?,?,?,?)',(subsystem,element_id,state,reason,source_id))
