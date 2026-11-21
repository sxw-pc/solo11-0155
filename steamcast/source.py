import hashlib, json
from pathlib import Path

# Characters that make an evidence id ambiguous when embedded in a composite
# lookup key.  Keeping the identity stable is more important than cosmetics.
_SEP = '\x1f'


def canon(item):
    return json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()


def digest(item):
    return hashlib.sha256(canon(item)).hexdigest()


def raw_digest(raw):
    return hashlib.sha256(raw).hexdigest()


def read(path):
    raw = Path(path).read_bytes()
    return json.loads(raw), raw


def record(db, kind, item):
    """Record one piece of structured evidence exactly once.

    Re-sending the same identity with the same facts is a no-op.  Re-sending
    the same identity with *different* facts is a conflict: the caller's
    transaction is rolled back and the old fact is never overwritten.
    """
    raw = canon(item)
    sha = hashlib.sha256(raw).hexdigest()
    ident = item['id']
    old = db.execute('SELECT * FROM evidence WHERE id=?', (ident,)).fetchone()
    if old:
        stored = old['semantic'] or old['sha']
        if stored != sha:
            raise ValueError('evidence identity conflict: ' + ident)
        return False
    db.execute('INSERT INTO evidence(id,kind,sha,raw,source_sha,semantic) VALUES(?,?,?,?,?,?)',
               (ident, kind, sha, raw, sha, sha))
    return True


def record_file(db, kind, ident, item, raw=None):
    """Record a *file-backed* fact.

    The natural key is (kind, id).  ``semantic`` is the digest of the
    canonical facts; two byte-different files that carry the same facts are
    idempotent.  Two files that disagree on facts raise ``ValueError``
    instead of overwriting.  ``sha`` is the digest of the original file, the
    source summary callers cite; the evidence row keeps it when it changes
    on an otherwise-identical re-upload.
    """
    raw = canon(item) if raw is None else raw
    source_sha = hashlib.sha256(raw).hexdigest()
    semantic = hashlib.sha256(canon(item)).hexdigest()
    key = kind + _SEP + ident
    old = db.execute('SELECT * FROM evidence WHERE id=?', (key,)).fetchone()
    if old:
        stored_semantic = old['semantic'] or old['sha']
        if stored_semantic != semantic:
            raise ValueError('evidence identity conflict: %s %s' % (kind, ident))
        if (old['source_sha'] or old['sha']) != source_sha:
            db.execute('UPDATE evidence SET sha=?,source_sha=? WHERE id=?',
                       (source_sha, source_sha, key))
        return False, source_sha
    db.execute('INSERT INTO evidence(id,kind,sha,raw,source_sha,semantic) VALUES(?,?,?,?,?,?)',
               (key, kind, source_sha, canon(item), source_sha, semantic))
    return True, source_sha


def file_record(db, command, raw, unique=False):
    """Journal an input file (or an action) for replay/audit.

    File re-uploads are idempotent: a byte-identical file for the same
    command is not journalled twice.  Actions (``unique=True``) are always
    journalled because a recompute is an event, not a fact, even when its
    payload is empty.
    """
    sha = hashlib.sha256(raw).hexdigest()
    if not unique:
        old = db.execute('SELECT sha FROM file_journal WHERE command=? ORDER BY id',
                         (command,)).fetchall()
        if old and any(r['sha'] == sha for r in old):
            return False
    db.execute('INSERT INTO file_journal(command,sha,raw) VALUES(?,?,?)', (command, sha, raw))
    return True


def digest_of(db, ident):
    row = db.execute('SELECT sha FROM evidence WHERE id=?', (ident,)).fetchone()
    return row['sha'] if row else None


def find_sha(db, kind, ident):
    """Digest for a file-backed fact, falling back to legacy plain-id evidence."""
    for key in (kind + _SEP + ident, ident):
        row = db.execute('SELECT sha FROM evidence WHERE id=? AND kind=?', (key, kind)).fetchone()
        if row:
            return row['sha']
    return None
