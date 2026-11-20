import copy,json,subprocess,sys,tempfile,unittest
from pathlib import Path
from steamcast import __file__ as imported_file
from steamcast.store import connect
from steamcast import cast,steam,strength
ROOT=Path(__file__).resolve().parents[1]
def fx(name):return json.loads((ROOT/'fixtures'/(name+'.json')).read_text(encoding='utf8'))
class SteamTests(unittest.TestCase):
    def setUp(self):self.db=connect(':memory:')
    def tearDown(self):self.db.close()
    def state(self,table):return self.db.execute('SELECT state FROM '+table+' WHERE element_id="ELEMENT-1"').fetchone()[0]
    def first(self):cast.import_data(self.db,fx('cast_ok'))
    def second(self):self.first();steam.import_data(self.db,fx('steam_ok'))
    def good(self):self.second();strength.import_data(self.db,fx('strength_ok'))
    def test_module_local(self):
        print('imported module',imported_file)
        self.assertTrue(str(Path(imported_file).resolve()).startswith(str(ROOT.resolve())))
    def test_normal_three_systems(self):
        self.good();self.assertEqual((self.state('cast_decision'),self.state('steam_decision'),self.state('release_decision')),('cast_accepted','cured','released'))
    def test_mould_spec_and_mix_version(self):
        self.assertEqual(cast.import_data(self.db,fx('cast_bad'))[0]['state'],'mould_invalid')
        self.db.close();self.db=connect(':memory:')
        x=fx('cast_ok');x['mixes'][0]['approved_at']='2026-01-11T00:00:00'
        self.assertEqual(cast.import_data(self.db,x)[0]['state'],'mix_invalid')
        self.db.close();self.db=connect(':memory:');self.first()
        c=fx('cast_ok')['casts'][0].copy();c.update(id='CAST-CONFLICT',mix_id='MIX-OTHER')
        self.assertEqual(cast.import_data(self.db,{'casts':[c]})[0]['state'],'version_conflict')
    def test_bad_cast_blocks_demould_and_release(self):
        x=fx('cast_ok');x['moulds'][0]['max_size']=1
        cast.import_data(self.db,x);steam.import_data(self.db,fx('steam_ok'));strength.import_data(self.db,fx('strength_ok'))
        self.assertEqual(self.state('steam_decision'),'cast_hold')
        self.assertEqual(self.state('release_decision'),'curing_hold')
    def test_same_mould_overlapping_cast(self):
        self.first();other=fx('cast_ok')['casts'][0].copy();other.update(id='CAST-OTHER',element_id='ELEMENT-OTHER')
        self.assertEqual(cast.import_data(self.db,{'casts':[other]})[0]['state'],'mould_busy')
    def test_expired_sensor_and_curve_gap(self):
        self.first();self.assertEqual(steam.import_data(self.db,fx('steam_bad'))[0]['state'],'sensor_invalid')
        self.db.close();self.db=connect(':memory:');self.first()
        x=fx('steam_ok');x['cycles'][0]['max_gap_minutes']=3
        self.assertEqual(steam.import_data(self.db,x)[0]['state'],'curve_gap')
    def test_curve_gap_blocks_shipping_release(self):
        self.first();x=fx('steam_ok');x['cycles'][0]['max_gap_minutes']=3
        steam.import_data(self.db,x);strength.import_data(self.db,fx('strength_ok'))
        self.assertEqual(self.state('release_decision'),'curing_hold')
    def test_ramp_and_hold_duration(self):
        self.first();x=fx('steam_ok');x['samples'][1]['celsius']=50
        self.assertEqual(steam.import_data(self.db,x)[0]['state'],'ramp_exceeded')
        self.db.close();self.db=connect(':memory:');self.first()
        x=fx('steam_ok');x['cycles'][0]['min_hold_minutes']=12
        self.assertEqual(steam.import_data(self.db,x)[0]['state'],'hold_short')
        self.db.close();self.db=connect(':memory:');self.first()
        x=fx('steam_ok');x['cycles'][0]['hold_temp_c']=45
        self.assertEqual(steam.import_data(self.db,x)[0]['state'],'hold_short')
    def test_specimen_identity_and_lab(self):
        self.second();x=fx('strength_ok');x['specimens'][0]['cast_id']='OTHER-CAST'
        self.assertEqual(strength.import_data(self.db,x)[0]['state'],'specimen_mismatch')
        self.db.close();self.db=connect(':memory:');self.second()
        x=fx('strength_ok');x['labs'][0]['end']='2026-01-10T00:00:00'
        self.assertEqual(strength.import_data(self.db,x)[0]['state'],'lab_invalid')
    def test_late_low_strength_freezes_release_and_future_mould(self):
        self.good();late=fx('strength_ok')['specimens'][0].copy();late.update(id='SPEC-LATE',at='2026-01-12T00:00:00',strength_mpa=35,kind='retest')
        strength.import_data(self.db,{'specimens':[late]})
        self.assertEqual(self.state('release_decision'),'strength_hold')
        self.assertEqual(self.db.execute('SELECT state FROM future_mould_hold WHERE mould_id="MOULD-1"').fetchone()[0],'blocked')
        revised=fx('cast_ok')['casts'][0].copy();revised.update(id='CAST-REV2',version=2)
        self.assertEqual(cast.import_data(self.db,{'casts':[revised]})[0]['state'],'quality_hold')
    def test_loaded_element_late_failure_traces_back(self):
        self.good();strength.import_data(self.db,{'shipping':[{'id':'SHIP-1','element_id':'ELEMENT-1','loaded_at':'2026-01-11T12:00:00','recipient':'site'}]})
        late=fx('strength_ok')['specimens'][0].copy();late.update(id='SPEC-FAIL',at='2026-01-12T00:00:00',strength_mpa=31,kind='retest')
        strength.import_data(self.db,{'specimens':[late]})
        self.assertEqual(self.state('release_decision'),'traceback')
    def test_idempotent_and_conflict_rollback(self):
        self.first();cast.import_data(self.db,fx('cast_ok'))
        before=self.db.execute('SELECT count(*) FROM evidence').fetchone()[0]
        x=fx('cast_ok');x['casts'][0]['size']=9
        with self.assertRaises(ValueError):cast.import_data(self.db,x)
        self.assertEqual(self.db.execute('SELECT count(*) FROM evidence').fetchone()[0],before)
    def test_cli_demo_audit(self):
        with tempfile.TemporaryDirectory() as d:
            db=str(Path(d)/'demo.sqlite')
            for cmd in ('demo','audit'):
                p=subprocess.run([sys.executable,'-m','steamcast',cmd,'--db',db],cwd=ROOT,capture_output=True,text=True)
                self.assertEqual(p.returncode,0,p.stderr)
                if cmd=='audit':self.assertTrue(json.loads(p.stdout)['ok'])
if __name__=='__main__':unittest.main()
