import argparse,json
from pathlib import Path
from .store import connect
from .source import read,file_record
from . import cast,steam,strength,handling,report
HANDLERS={'cast-import':cast.import_data,'steam-import':steam.import_data,'strength-import':strength.import_data,'handling-import':handling.import_data}
def main():
    p=argparse.ArgumentParser();p.add_argument('command',choices=[*HANDLERS,'demo','report','audit']);p.add_argument('--db',default='state.sqlite');p.add_argument('--input');a=p.parse_args();db=connect(a.db)
    if a.command=='report':print(json.dumps(report.build(db),ensure_ascii=False,indent=2));return
    if a.command=='audit':print(json.dumps(report.audit(db),ensure_ascii=False,indent=2));return
    if a.command=='demo':
        for cmd,name in [('cast-import','cast_ok.json'),('steam-import','steam_ok.json'),('strength-import','strength_ok.json')]:
            doc,raw=read(Path(__file__).resolve().parents[1]/'fixtures'/name);HANDLERS[cmd](db,doc)
            with db:file_record(db,cmd,raw)
        print(json.dumps(report.build(db),ensure_ascii=False,indent=2));return
    if not a.input:p.error('--input required')
    doc,raw=read(a.input);result=HANDLERS[a.command](db,doc)
    with db:file_record(db,a.command,raw)
    print(json.dumps(result,ensure_ascii=False,indent=2))
if __name__=='__main__':main()
