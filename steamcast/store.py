import sqlite3

SCHEMA = """
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

-- Offline quality investigation intake.
CREATE TABLE IF NOT EXISTS quality_investigation(
 id TEXT PRIMARY KEY,
 specimen_id TEXT NOT NULL UNIQUE,
 element_id TEXT NOT NULL,
 cast_id TEXT NOT NULL,
 cycle_id TEXT NOT NULL,
 lab_cert_id TEXT NOT NULL,
 known_at TEXT NOT NULL,
 specimen_sha TEXT,
 cast_sha TEXT,
 cycle_sha TEXT,
 lab_sha TEXT,
 state TEXT NOT NULL,
 mould_state TEXT NOT NULL,
 mix_state TEXT NOT NULL,
 version INTEGER NOT NULL,
 created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS mould_check(
 id TEXT PRIMARY KEY,
 mould_id TEXT NOT NULL,
 mould_version INTEGER NOT NULL,
 at TEXT NOT NULL,
 valid_until TEXT NOT NULL,
 result TEXT NOT NULL,
 signer TEXT NOT NULL,
 sha TEXT NOT NULL,
 recorded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS axis_recheck(
 id TEXT PRIMARY KEY,
 investigation_id TEXT,
 axis TEXT NOT NULL,
 target_id TEXT NOT NULL,
 target_version INTEGER NOT NULL,
 sampled_at TEXT NOT NULL,
 signer TEXT NOT NULL,
 sha TEXT NOT NULL,
 result TEXT NOT NULL,
 note TEXT,
 recorded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS mix_sample(
 id TEXT PRIMARY KEY,
 mix_id TEXT NOT NULL,
 mix_version INTEGER NOT NULL,
 sampled_at TEXT NOT NULL,
 valid_until TEXT NOT NULL,
 result TEXT NOT NULL,
 signer TEXT NOT NULL,
 sha TEXT NOT NULL,
 recorded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS exposure_repair(
 id TEXT PRIMARY KEY,
 axis TEXT NOT NULL,
 target_id TEXT NOT NULL,
 target_version INTEGER NOT NULL,
 kind TEXT NOT NULL,
 effective_at TEXT NOT NULL,
 signer TEXT NOT NULL,
 sha TEXT NOT NULL,
 note TEXT,
 recorded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cert_revocation(
 id TEXT PRIMARY KEY,
 cert_kind TEXT NOT NULL,
 cert_id TEXT NOT NULL,
 revoked_at TEXT NOT NULL,
 signer TEXT NOT NULL,
 sha TEXT NOT NULL,
 recorded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS receiving_contract(
 id TEXT PRIMARY KEY,
 purpose TEXT NOT NULL,
 recipient TEXT NOT NULL,
 signed_at TEXT NOT NULL,
 signed_by TEXT NOT NULL,
 version INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS outbound_request(
 id TEXT PRIMARY KEY,
 element_id TEXT NOT NULL,
 contract_id TEXT NOT NULL,
 at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS outbound_receipt(
 id TEXT PRIMARY KEY,
 request_id TEXT NOT NULL,
 element_id TEXT NOT NULL,
 contract_id TEXT NOT NULL,
 purpose TEXT NOT NULL,
 kind TEXT NOT NULL,
 inventory_state TEXT NOT NULL,
 usable_for TEXT NOT NULL,
 state TEXT NOT NULL,
 reason TEXT NOT NULL,
 at TEXT NOT NULL,
 version INTEGER NOT NULL,
 basis TEXT NOT NULL,
 created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS trace_record(
 id TEXT PRIMARY KEY,
 element_id TEXT NOT NULL,
 investigation_id TEXT,
 shipping_id TEXT,
 loaded_at TEXT,
 state TEXT NOT NULL,
 reason TEXT NOT NULL,
 basis TEXT NOT NULL,
 version INTEGER NOT NULL,
 recorded_at TEXT NOT NULL
);
"""


def connect(path):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    # Existing demo databases predate the investigation columns on the
    # append-only decision journal.  Add them in place without rewriting rows.
    cols = {r['name'] for r in db.execute('PRAGMA table_info(decision_history)')}
    for column, decl in (('basis', 'TEXT'), ('version', 'INTEGER'), ('known_at', 'TEXT')):
        if column not in cols:
            db.execute('ALTER TABLE decision_history ADD COLUMN %s %s' % (column, decl))
    ecols = {r['name'] for r in db.execute('PRAGMA table_info(evidence)')}
    # File-backed evidence keeps two digests: ``sha`` is the digest of the
    # original file (the source summary callers cite), ``semantic`` is the
    # digest of the canonical facts (used for idempotency/conflict).
    for column, decl in (('source_sha', 'TEXT'), ('semantic', 'TEXT')):
        if column not in ecols:
            db.execute('ALTER TABLE evidence ADD COLUMN %s %s' % (column, decl))
    return db


def history(db, subsystem, element_id, state, reason, source_id=None,
            basis=None, version=None, known_at=None):
    db.execute(
        'INSERT INTO decision_history'
        '(subsystem,element_id,state,reason,source_id,basis,version,known_at)'
        ' VALUES(?,?,?,?,?,?,?,?)',
        (subsystem, element_id, state, reason, source_id, basis, version, known_at),
    )
