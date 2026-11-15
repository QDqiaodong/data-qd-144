
import copy,json,unittest,tempfile,subprocess,sys,hashlib,random,itertools
from pathlib import Path
from fractions import Fraction
from cutting_stock.model import analyze
ROOT=Path(__file__).resolve().parents[1]
def sample():return json.loads((ROOT/'fixtures/input.json').read_text())
class Business(unittest.TestCase):
    def bad(self,d):
        with self.assertRaises((ValueError,TypeError)):analyze(d)
    def test_unknown_fields(self):
        d=sample();d['unknown']=1;self.bad(d)

    def test_plan(self):
        r=analyze(sample());self.assertEqual(r['bar_count'],2);self.assertEqual([[p['id'] for p in b['pieces']] for b in r['bars']],[['a','c'],['b','d']]);self.assertEqual((r['piece_length'],r['kerf_loss'],r['remainder'],r['conservation_residual']),(16,2,2,0))
    def test_kerf_blocks_fit(self):
        d={'stock_length':10,'kerf':1,'pieces':[{'id':'a','length':5},{'id':'b','length':5}]};self.assertEqual(analyze(d)['bar_count'],2);d['kerf']=0;self.assertEqual(analyze(d)['bar_count'],1)
    def test_exact_stock(self):
        d={'stock_length':10,'kerf':1,'pieces':[{'id':'a','length':10}]};r=analyze(d);self.assertEqual(r['kerf_loss'],0);self.assertEqual(r['remainder'],0)
    def test_empty(self):
        d=sample();d['pieces']=[];self.assertEqual(analyze(d)['bar_count'],0)
    def test_too_long(self):
        d=sample();d['pieces'][0]['length']=11;self.bad(d)
    def test_duplicate(self):
        d=sample();d['pieces'].append(d['pieces'][0]);self.bad(d)
    def test_order(self):
        d=sample();d['pieces'].reverse();self.assertEqual(analyze(d),analyze(sample()))
    def test_random_conservation(self):
        rng=random.Random(7)
        for _ in range(40):
            d={'stock_length':100,'kerf':rng.randrange(5),'pieces':[{'id':str(i),'length':rng.randrange(1,101)} for i in range(40)]};r=analyze(d);self.assertEqual(r['conservation_residual'],0);self.assertEqual(sorted(p['id'] for b in r['bars'] for p in b['pieces']),sorted(p['id'] for p in d['pieces']));self.assertTrue(all(b['used']<=100 for b in r['bars']))

class Command(unittest.TestCase):
    def call(self,source,output):
        return subprocess.run([sys.executable,'-m','cutting_stock.cli','--source',str(source),'--output',str(output)],capture_output=True,text=True)
    def test_cli_replay(self):
        with tempfile.TemporaryDirectory() as td:
            out=Path(td)/'out.json';src=ROOT/'fixtures/input.json';r=self.call(src,out);self.assertEqual(r.returncode,0,r.stderr);first=out.read_bytes();self.assertEqual(json.loads(first)['source_sha256'],hashlib.sha256(src.read_bytes()).hexdigest());self.assertEqual(self.call(src,out).returncode,0);self.assertEqual(first,out.read_bytes())
    def test_failure_preserves(self):
        with tempfile.TemporaryDirectory() as td:
            src=Path(td)/'bad.json';out=Path(td)/'out.json';out.write_text('prior')
            for body in ['{}','{"a":1,"a":2}','NaN']:
                src.write_text(body);self.assertEqual(self.call(src,out).returncode,2);self.assertEqual(out.read_text(),'prior')
    def test_same_path_and_symlink(self):
        with tempfile.TemporaryDirectory() as td:
            src=Path(td)/'in.json';src.write_bytes((ROOT/'fixtures/input.json').read_bytes());before=src.read_bytes();self.assertEqual(self.call(src,src).returncode,2);self.assertEqual(src.read_bytes(),before);out=Path(td)/'link.json';out.symlink_to(src);self.assertEqual(self.call(src,out).returncode,2);self.assertEqual(src.read_bytes(),before)
