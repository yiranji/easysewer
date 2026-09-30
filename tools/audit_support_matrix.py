"""Check the acceptance inventory and native option spellings, not all support gates.

Run with --native-source pointing at the reviewed SWMM 5.2.4 solver source.
The output binds navigation references and a concrete native keyword comparison.
It deliberately leaves full per-variant acceptance open.
"""
import argparse
import ast
import hashlib
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def review_evidence(row, required_gates, _cache=None, evidence_root=None):
    """Check a specific reviewed gate ledger, not merely module registration."""
    if row['acceptance'] == 'historical_gates_verified':
        # A proof bound to an earlier source snapshot is not current acceptance.
        assert 'acceptance_evidence' not in row
        reference = row['historical_evidence']
        relative = Path(reference['path'])
        assert not relative.is_absolute() and '..' not in relative.parts
        assert relative.parts[:2] == ('docs', 'qualification')
        assert re.fullmatch(r'[0-9a-f]{64}', reference['sha256'])
        if evidence_root is not None:
            archive = Path(evidence_root).resolve()
            path = (archive / relative).resolve()
            assert path.is_relative_to(archive)
            assert sha(path) == reference['sha256']
            proof = json.loads(path.read_bytes())
            assert proof['format'] == 'easysewer:variant-gate-evidence' and proof['version'] == 1
            assert row['section'] in proof['accepted_sections']
            entries = [e for e in proof['reviewed_gates'] if e['section'] == row['section']]
            expected = {(v, g) for v in row['variant_groups'] for g in required_gates}
            assert len(entries) == len(expected)
            assert {(e['variant'], e['gate']) for e in entries} == expected
        return None
    if row['acceptance'] == 'pending_assertion_and_evidence_review':
        assert 'acceptance_evidence' not in row
        return None
    assert row['acceptance'] == 'candidate_gates_verified'
    cache = {} if _cache is None else _cache
    def checked_json(path, digest):
        key = (path, digest)
        if key not in cache:
            assert sha(path) == digest
            cache[key] = json.loads(path.read_bytes())
        return cache[key]
    reference = row['acceptance_evidence']
    path = (ROOT / reference['path']).resolve()
    assert path.is_relative_to(ROOT.resolve())
    proof = checked_json(path, reference['sha256'])
    assert proof['format'] == 'easysewer:variant-gate-evidence' and proof['version'] == 1
    assert row['section'] in proof['accepted_sections']
    entries = [e for e in proof['reviewed_gates'] if e['section'] == row['section']]
    expected = {(variant, gate) for variant in row['variant_groups'] for gate in required_gates}
    assert len(entries) == len(expected) and {(e['variant'], e['gate']) for e in entries} == expected
    if ('runtime', path) not in cache:
        for source, digest in proof['package']['source_digests'].items():
            if source.startswith('src/easysewer/'):
                assert sha(ROOT/source) == digest, source
        cache['runtime', path] = True
    for entry in entries:
        assert entry['rationale'] and entry['input_selector'] and entry['assertions']
        for assertion in entry['assertions']:
            source = (ROOT/assertion['file']).resolve()
            assert source.is_relative_to(ROOT.resolve())
            method = assertion['method']
            method_key = ('method', method, source, assertion['source_sha256'],
                          assertion['line'], assertion['end_line'])
            if method_key not in cache:
                assert sha(source) == assertion['source_sha256']
                module, class_name, method_name = method.split('.')
                assert assertion['file'] == 'tests/' + module + '.py'
                tree = ast.parse(source.read_text(encoding='utf-8'))
                cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == class_name)
                fn = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == method_name)
                assert (fn.lineno, fn.end_lineno) == (assertion['line'], assertion['end_line'])
                cache[method_key] = True
            environments = assertion['executed_in']
            native = {'native-' + label for label in ('windows-313', 'windows-310', 'linux-312')}
            needed = native if method.startswith('test_native_') else native | {k.replace('native-', 'pure-') for k in native}
            assert set(environments) == needed
            for environment in environments:
                record = proof['records'][environment]
                original_path = (ROOT/record['record_path']).resolve()
                assert original_path.is_relative_to(ROOT.resolve())
                original = checked_json(original_path, record['record_sha256'])
                assert method in original['selected'] and not original['skipped']
                assert original['test_digests'][assertion['file']] == assertion['source_sha256']
                failed = {item[0].split(' ')[0] for item in original['failures'] + original['errors']}
                # Records use either TestCase.id() or str(TestCase); subtests
                # append their parameters after the same leading identifier.
                assert not {method, method.split('.')[-1]} & failed
        if entry['extra_lifecycle']:
            expected_environments = {kind + '-' + label for kind in ('native', 'pure')
                                     for label in ('windows-313', 'windows-310', 'linux-312')}
            assert len(entry['extra_records']) == 6 and set(entry['extra_records']) == expected_environments
            for name in entry['extra_records']:
                extra = proof['lifecycle_extra'][name]
                extra_path = (ROOT/extra['record_path']).resolve()
                assert extra_path.is_relative_to(ROOT.resolve())
                original_extra = checked_json(extra_path, extra['record_sha256'])
                assert original_extra == {k: v for k, v in extra.items()
                                          if k not in ('record_path', 'record_sha256')}
                assert extra['passed'] and extra['cases'] == len(extra['rows']) == 27
                assert extra['wheel_sha256'] == proof['records'][name]['wheel_sha256']
                assert extra['package_digests'] == proof['records'][name]['package_digests']
                assert extra['harness_sha256'] == proof['extra_harness_sha256']
    return dict(section=row['section'], variants=len(row['variant_groups']), gates=len(entries),
                evidence=reference, scope=proof['scope'])


def audit(native, evidence_root=None):
    from easysewer.io.inp.network import default_schema
    from easysewer.schema.option_profile import OPTION_DEFINITIONS
    from easysewer.schema.profiles import EPA_SWMM_5_2_4
    path = ROOT / 'docs/2.0-support-matrix.json'
    matrix = json.loads(path.read_bytes())
    entries = matrix['sections']
    names = [row['section'] for row in entries]
    assert len(names) == len(set(names)) == 57
    assert set(names) == EPA_SWMM_5_2_4.sections
    owners = {name: set() for name in names}
    source_files = {}
    import inspect
    for descriptor, codec in default_schema().bindings:
        p = Path(inspect.getfile(type(codec))).resolve()
        source_files[p.relative_to(ROOT).as_posix()] = sha(p)
        for name in descriptor.sections:
            owners[name].update(c.key for c in codec.collections)
    tests = {}; reviewed = []; review_cache = {}
    for row in entries:
        assert row['owners'] and set(row['owners']) <= owners[row['section']], row['section']
        assert row['variant_groups'] and len(row['variant_groups']) == len(set(row['variant_groups']))
        assert row['boundary']
        review = review_evidence(row, matrix['required_gates'], review_cache, evidence_root)
        if review is not None: reviewed.append(review)
        assert row['test_modules']
        for name in row['test_modules']:
            p = ROOT / 'tests' / (name + '.py')
            tree = ast.parse(p.read_text(encoding='utf-8-sig'))
            cases = [c.name + '.' + f.name for c in tree.body if isinstance(c, ast.ClassDef)
                     for f in c.body if isinstance(f, ast.FunctionDef) and f.name.startswith('test_')]
            assert cases, name
            tests[name] = dict(sha256=sha(p),test_methods=cases)
    definitions = dict(re.findall(r'#define\s+(\w+)\s+"([^"\n]*)"', (native/'text.h').read_text()))
    keywords = (native/'keywords.c').read_text()
    def words(name):
        match = re.search(r'\b'+name+r'\[\]\s*=\s*\{([^}]+)\}', keywords, re.S)
        assert match, name
        return tuple(definitions[word] for word in re.findall(r'\bw_\w+\b', match[1]))
    assert set(words('OptionWords')) == {v.keyword for v in OPTION_DEFINITIONS}
    tables = {'FLOW_UNITS':'FlowUnitWords','INFILTRATION':'InfilModelWords',
              'FLOW_ROUTING':'RouteModelWords','LINK_OFFSETS':'LinkOffsetWords',
              'FORCE_MAIN_EQUATION':'ForceMainEqnWords','INERTIAL_DAMPING':'InertDampingWords',
              'NORMAL_FLOW_LIMITED':'NormalFlowWords','SURCHARGE_METHOD':'SurchargeWords'}
    tree = ast.parse((ROOT/'tests/test_option_variants_v2.py').read_text())
    choices = next(ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign)
                   and any(isinstance(t, ast.Name) and t.id == 'CHOICES' for t in n.targets))
    spellings = {}
    for keyword, table in tables.items():
        expected = words(table)
        if keyword == 'FLOW_ROUTING': expected = tuple(dict.fromkeys((*expected, *words('OldRouteModelWords'))))
        assert set(choices[keyword]) == set(expected), (keyword, choices[keyword], expected)
        definition = next(d for d in OPTION_DEFINITIONS if d.keyword == keyword)
        aliases = {'XKINWAVE', 'NF', 'KW', 'EKW', 'DW'} if keyword == 'FLOW_ROUTING' else set()
        assert set(definition.choices) == set(expected) - aliases, keyword
        spellings[keyword] = dict(native_table=table, native_spellings=expected,
            declared_choices=definition.choices, aliases=sorted(aliases))
    assert set(words('NoYesWords')) == {'NO','YES'}
    return dict(scope='Inventory consistency and complete categorical option spelling comparison only; not full R04/R05 acceptance',
        accepted=len(reviewed)==len(entries), reviewed_sections=reviewed,
        historical_sections=[row['section'] for row in entries if row['acceptance']=='historical_gates_verified'],
        historical_evidence_files_checked=evidence_root is not None,
        matrix_sha256=sha(path), sections=len(entries), variant_groups=sum(len(r['variant_groups']) for r in entries),
        required_gates=matrix['required_gates'], test_index=tests, codec_sources=source_files,
        option_keywords=words('OptionWords'), option_spellings=spellings,
        oracle_files={str(native/n):sha(native/n) for n in ('keywords.c','text.h','project.c','dwflow.c')},
        matrix_gaps=['Remaining per-variant assertions and evidence must be reviewed before their sections are accepted',
            'Optional forms, context/physical/topology/units and combinations are not certified by keyword parity',
            'Remaining parser producers and lexical diagnostics remain open',
            'Historical evidence does not certify the current source; see docs/2.0-testing.md and docs/2.0-known-issues.md'])


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--native-source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--evidence-root', type=Path,
                        help='Optional unpacked development archive containing docs/qualification; checks historical proof files, not current source acceptance')
    args = parser.parse_args()
    result = audit(args.native_source, args.evidence_root)
    with args.output.open('x', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(json.dumps(dict(sections=result['sections'], variant_groups=result['variant_groups'],
        test_modules=len(result['test_index']), native_option_keywords=len(result['option_keywords']),
        categorical_options=len(result['option_spellings']), full_acceptance=result['accepted'])))
