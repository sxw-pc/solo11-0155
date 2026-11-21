import argparse
import json
from pathlib import Path

from .store import connect
from .source import read, file_record
from . import cast, steam, strength, investigation, outbound, report

HANDLERS = {
    'cast-import': cast.import_data,
    'steam-import': steam.import_data,
    'strength-import': strength.import_data,
    'investigation-import': investigation.import_data,
    'outbound-import': outbound.import_data,
}
COMMANDS = [*HANDLERS, 'demo', 'report', 'investigations', 'receipts', 'traces', 'recompute', 'audit']


def _dump(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def main():
    p = argparse.ArgumentParser()
    p.add_argument('command', choices=COMMANDS)
    p.add_argument('--db', default='state.sqlite')
    p.add_argument('--input')
    p.add_argument('--as-of', dest='as_of', default=None,
                   help='historical scope: only evidence known by this instant')
    a = p.parse_args()
    db = connect(a.db)
    if a.command == 'report':
        _dump(report.build(db, as_of=a.as_of)); return
    if a.command == 'investigations':
        _dump(report.investigation_report(db, as_of=a.as_of)); return
    if a.command == 'receipts':
        _dump(report.receipts(db)); return
    if a.command == 'traces':
        _dump(report.traces(db)); return
    if a.command == 'recompute':
        with db:
            outbound.recompute(db)
            file_record(db, 'recompute', b'{}', unique=True)
        _dump(report.receipts(db))
        return
    if a.command == 'audit':
        _dump(report.audit(db)); return
    if a.command == 'demo':
        for cmd, name in [('cast-import', 'cast_ok.json'), ('steam-import', 'steam_ok.json'),
                          ('strength-import', 'strength_ok.json')]:
            doc, raw = read(Path(__file__).resolve().parents[1] / 'fixtures' / name)
            HANDLERS[cmd](db, doc)
            with db:
                file_record(db, cmd, raw)
        _dump(report.build(db)); return
    if not a.input:
        p.error('--input required')
    doc, raw = read(a.input)
    result = HANDLERS[a.command](db, doc)
    with db:
        file_record(db, a.command, raw)
    _dump(result)


if __name__ == '__main__':
    main()
