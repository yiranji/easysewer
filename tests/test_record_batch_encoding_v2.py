"""Wire-format regression for cooperative batched pretty JSON."""
import json,random,unittest
from easysewer.io._record_work import record_dumps
from easysewer.validation._cooperative import checkpoint_scope
class BatchedRecordEncodingTests(unittest.TestCase):
    def test_group_boundaries_nested_values_and_keys_match_standard_json(self):
        atoms=[None,True,False,0,-1,1.25,'中文','line\nbreak','quote"slash\\']
        for size in (0,1,127,128,129,511,512,513,1025):
            rows=[atoms[i%len(atoms)] for i in range(size)]
            for value in (rows,tuple(rows),{'before':[], 'rows':rows,'after':{}},[rows,[{'nested':rows,'empty':()}]],{None:rows,1:tuple(rows),False:[]}):
                expected=json.dumps(value,ensure_ascii=False,allow_nan=False,indent=2)
                with checkpoint_scope(lambda:None):self.assertEqual(record_dumps(value),expected)
        randomizer=random.Random(162)
        def build(depth):
            if not depth:return randomizer.choice(atoms)
            if randomizer.randrange(2):return [build(depth-1) for _ in range(randomizer.randrange(1,8))]
            return {str(i):build(depth-1) for i in range(randomizer.randrange(1,5))}
        value=[build(3) for _ in range(600)];expected=json.dumps(value,ensure_ascii=False,allow_nan=False,indent=2)
        with checkpoint_scope(lambda:None):self.assertEqual(record_dumps(value),expected)

    def test_cycles_nonfinite_and_unsupported_values_keep_standard_errors(self):
        cyclic=[None]*513;cyclic[256]=cyclic
        values=(cyclic,[None]*512+[float('nan')],[None]*512+[float('inf')],[None]*512+[object()])
        for value in values:
            with self.assertRaises((ValueError,TypeError)) as old:json.dumps(value,ensure_ascii=False,allow_nan=False,indent=2)
            with self.assertRaises(type(old.exception)) as actual,checkpoint_scope(lambda:None):record_dumps(value)
            self.assertEqual(str(actual.exception),str(old.exception))

    def test_interruption_between_groups_keeps_original_exception_and_complete_retry(self):
        value=[str(i) for i in range(20000)];expected=json.dumps(value,ensure_ascii=False,allow_nan=False,indent=2);calls=[];error=KeyboardInterrupt('stop between groups')
        def check():
            calls.append(1)
            if len(calls)==8:raise error
        with self.assertRaises(KeyboardInterrupt) as caught,checkpoint_scope(check):record_dumps(value)
        self.assertIs(caught.exception,error)
        with checkpoint_scope(lambda:None):self.assertEqual(record_dumps(value),expected)
if __name__=='__main__':unittest.main()