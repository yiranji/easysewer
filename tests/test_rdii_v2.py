"""Complete seasonal RDII input, graph, unit and persistence contracts."""

from dataclasses import replace
import unittest

from easysewer.io.inp import InpDocument
from easysewer.io.json import JsonDocument
from easysewer.model import Model, Ref
from easysewer.model.rdii import UnitHydrograph, HydrographResponse, RdiiInflow, MONTHS, RESPONSES
from easysewer.scenario import ScenarioPatch, SetFields, FieldChange, RenameRecord
from easysewer.validation import ValidationError
from test_files_v2 import bind
from test_hydrology_v2 import hydrology_model
from test_scenario_v2 import portable


def response(month='ALL', kind='SHORT', fraction=.15, peak=1, k=2, **kwargs):
    return HydrographResponse(month=month, response=kind, fraction=fraction, time_to_peak=peak, recession_ratio=k, **kwargs)


def rdii_model():
    model = hydrology_model()
    model.hydrographs.add(UnitHydrograph(id='UH', rain_gage=Ref(collection='swmm:raingages', key='R'), responses=(
        response(maximum_abstraction=.1, recovery_rate=.2, initial_abstraction=.025),
        response(kind='MEDIUM', fraction=.1, peak=2, k=3),
        response(kind='LONG', fraction=.05, peak=4, k=4),
        response(month='FEB', fraction=.2, peak=.5, k=1))))
    model.rdii.add(RdiiInflow(node=Ref(collection='swmm:nodes', key='J'), hydrograph=Ref(collection='swmm:hydrographs', key='UH'), sewer_area=2))
    return model


class RdiiTests(unittest.TestCase):
    def test_all_months_responses_optional_tails_and_source_preservation(self):
        for month in ('ALL', *MONTHS):
            for kind in RESPONSES:
                for tail in ('', ' .2', ' .2 .1', ' .2 .1 .05'):
                    with self.subTest(month=month, kind=kind, tail=tail):
                        source = hydrology_model().to_document().text + f'[HYDROGRAPHS]\nUH R\nUH {month} {kind} .1 1.25 2{tail}\n[RDII]\nJ UH 2\n'
                        model = Model.from_document(InpDocument.from_text(source), strict=True)
                        self.assertEqual(model.to_document().text, source)
                        self.assertEqual(Model.from_document(model.to_document(normalize=True), strict=True).hydrographs['UH'], model.hydrographs['UH'])
                        self.assertEqual(tuple(portable(model).rdii.values()), tuple(model.rdii.values()))
                        group = model.hydrographs['UH']
                        selected = 1 if month == 'ALL' else MONTHS.index(month)+1
                        self.assertEqual(group.for_month(selected)[RESPONSES.index(kind)], group.responses[0])
                        self.assertEqual(group.responses[0].native_peak_seconds, 4500)

    def test_ordered_all_and_month_overrides_do_not_reset_other_components(self):
        group = UnitHydrograph(id='UH', responses=(response(), response(kind='LONG', fraction=.03),
            response(month='JUL', fraction=.4), response(month='JUL', kind='MEDIUM', fraction=.2)))
        self.assertEqual(tuple(r.fraction if r else None for r in group.for_month(7)), (.4, .2, .03))
        self.assertEqual(tuple(r.fraction if r else None for r in group.for_month(1)), (.15, None, .03))
        later_all = replace(group, responses=(*group.responses, response(fraction=.1)))
        self.assertEqual(later_all.for_month(7)[0].fraction, .1)
        for bad in (0, 13, True, 'JAN'):
            with self.assertRaises(ValueError): group.for_month(bad)

    def test_legacy_triples_expand_without_loss_or_double_assignment(self):
        for tail in ('', ' .2', ' .2 .1', ' .2 .1 .05'):
            source = hydrology_model().to_document().text + f'[HYDROGRAPHS]\nUH R\nUH ALL .1 1 2 .2 3 4 .3 5 6{tail}\nUH JAN SHORT .25 2 3\n[RDII]\nJ UH 2\n'
            model = Model.from_document(InpDocument.from_text(source), strict=True)
            self.assertEqual(model.to_document().text, source)
            group = model.hydrographs['UH']
            self.assertEqual(len(group.responses), 4)
            self.assertEqual(tuple(r.fraction for r in group.for_month(1)), (.25, .2, .3))
            self.assertEqual(tuple(r.fraction for r in group.for_month(2)), (.1, .2, .3))
            normalized = Model.from_document(model.to_document(normalize=True), strict=True)
            self.assertEqual(normalized.hydrographs['UH'], group)
            self.assertEqual(len(normalized.document.records('HYDROGRAPHS')), 5)

    def test_renames_derived_keys_deletion_and_transaction_rollback(self):
        model = rdii_model()
        model.raingages.rename('R', 'Gauge')
        model.hydrographs.rename('UH', 'Seasonal')
        model.nodes.rename('J', 'Receiving')
        self.assertEqual(model.hydrographs['Seasonal'].rain_gage.key, 'Gauge')
        self.assertEqual(model.rdii['Receiving'].hydrograph.key, 'Seasonal')
        self.assertNotIn('J', model.rdii)
        for collection, key in ((model.hydrographs,'Seasonal'), (model.raingages,'Gauge'), (model.nodes,'Receiving')):
            with self.assertRaises(ValidationError): collection.remove(key)
        before = model.to_json_document().to_bytes()
        with self.assertRaises(ValidationError):
            with model.transaction():
                model.hydrographs.update('Seasonal', responses=(response(fraction=1.1),))
        self.assertEqual(model.to_json_document().to_bytes(), before)

    def test_unit_conversion_and_scenario_json_preserve_full_chain(self):
        model = rdii_model(); before = model.to_json_document().to_bytes()
        patch = ScenarioPatch(flow_units='CFS', operations=(
            RenameRecord(target=Ref(collection='swmm:hydrographs', key='UH'), new_id='Storm'),
            SetFields(target=Ref(collection='swmm:rdii', key='J'), changes=(FieldChange(name='sewer_area', value=4.),)),
        ))
        edited = ScenarioPatch.from_json_document(patch.to_json_document()).apply(model).model
        self.assertEqual(edited.rdii['J'].hydrograph.key, 'Storm')
        self.assertEqual(edited.rdii['J'].sewer_area, 4.)
        edited.convert_units('CMS')
        r = edited.hydrographs['Storm'].responses[0]
        self.assertAlmostEqual(r.maximum_abstraction, 2.54)
        self.assertAlmostEqual(r.recovery_rate, 5.08)
        self.assertAlmostEqual(r.initial_abstraction, .635)
        self.assertEqual((r.fraction, r.time_to_peak, r.recession_ratio), (.15, 1, 2))
        self.assertAlmostEqual(edited.rdii['J'].sewer_area, 4*(.92903e-5/2.2956e-5))
        restored = portable(edited)
        self.assertEqual(tuple(restored.hydrographs.values()), tuple(edited.hydrographs.values()))
        self.assertEqual(tuple(restored.rdii.values()), tuple(edited.rdii.values()))
        self.assertEqual(model.to_json_document().to_bytes(), before)

    def test_invalid_input_keeps_whole_affected_group_and_rejects_bad_values(self):
        for row in ('UH ALL SHORT .1 1', 'UH INVALID SHORT .1 1 2', 'UH ALL SHORT NaN 1 2',
                    'UH ALL SHORT .1 -1 2', 'UH ALL .1 1 2 .2 3 4', 'UH ALL SHORT .1 1e30 2'):
            source = f'[HYDROGRAPHS]\nUH R\n{row}\n'
            parsed = Model.from_document(InpDocument.from_text(source))
            self.assertFalse(parsed.validate().is_valid)
            self.assertFalse(parsed.hydrographs)
            self.assertEqual(parsed.document.text, source)
        for r in (response(fraction=True), response(peak=float('inf')), response(k=-1), response(month='jan')):
            model = rdii_model(); model.hydrographs.update('UH', responses=(r,))
            self.assertFalse(model.validate().is_valid)
            with self.assertRaises(ValidationError): model.to_document()
        model = rdii_model(); model.rdii.update('J', sewer_area=-1)
        self.assertFalse(model.validate().is_valid)
        with self.assertRaises(ValidationError): model.to_document()
        model = rdii_model(); model.hydrographs.add(UnitHydrograph(id='Empty'))
        self.assertFalse(model.validate().is_valid)
        with self.assertRaises(ValidationError): model.to_document()

    def test_duplicate_source_references_and_native_prefixes(self):
        source = hydrology_model().to_document().text + '[HYDROGRAPHS]\nUH R\nUH R\nUH JANUARY SHORTER .1 1 2 extra\n[RDII]\nJ Missing 2\nJ UH 3 trailing\n'
        # Invalid numeric IA is not ignored simply because it looks like extra text.
        bad = Model.from_document(InpDocument.from_text(source))
        self.assertFalse(bad.validate().is_valid)
        source = source.replace('2 extra', '2 0 0 0 extra')
        model = Model.from_document(InpDocument.from_text(source), strict=True)
        self.assertEqual(model.hydrographs['UH'].responses[0].month, 'JAN')
        self.assertEqual(model.rdii['J'].sewer_area, 3.)
        self.assertIn('rdii.source_reference', {d.code for d in model.validate(for_run=True).errors})
        normalized = Model.from_document(model.to_document(normalize=True), strict=True)
        self.assertNotIn('rdii.source_reference', {d.code for d in normalized.validate(for_run=True).errors})

    def test_prior_gages_remain_live_references_with_full_persistence(self):
        model = rdii_model()
        model.raingages.add(replace(model.raingages['R'], id='Earlier'))
        source = model.to_document().text.replace('UH R', 'UH Earlier\nUH R')
        model = Model.from_document(InpDocument.from_text(source), strict=True)
        self.assertEqual(model.to_document().text, source)
        self.assertEqual(model.hydrographs['UH'].prior_rain_gages, (Ref(collection='swmm:raingages', key='Earlier'),))
        model.raingages.rename('Earlier', 'Activated')
        with self.assertRaises(ValidationError): model.raingages.remove('Activated')
        for rebuilt in (portable(model), Model.from_document(model.to_document(normalize=True), strict=True)):
            self.assertEqual(rebuilt.hydrographs['UH'], model.hydrographs['UH'])
            self.assertIn('UH Activated', rebuilt.to_document().text)
        for updates in ({'rain_gage':None}, {'prior_rain_gages':(Ref(collection='swmm:nodes', key='J'),)}):
            invalid = model.copy(); invalid.hydrographs.update('UH', **updates)
            self.assertFalse(invalid.validate().is_valid)
            with self.assertRaises(ValidationError): invalid.to_document()

    def test_generation_requires_gage_but_existing_cache_can_supply_flow(self):
        model = rdii_model(); model.hydrographs.update('UH', rain_gage=None)
        self.assertTrue(model.validate().is_valid)
        self.assertIn('rdii.missing_gage', {d.code for d in model.validate(for_run=True).errors})
        bind(model, 'RDII', 'USE', 'cached.bin')
        self.assertNotIn('rdii.missing_gage', {d.code for d in model.validate(for_run=True).errors})
        for mutation in (lambda: model.rdii.update('J', sewer_area=3), lambda: model.hydrographs.update('UH', responses=(response(),)),
                         lambda: model.hydrographs.rename('UH', 'Other'), lambda: model.rdii.remove('J')):
            with self.assertRaises(ValidationError): mutation()

    def test_native_seconds_rounding_inactive_responses_and_ratio_tolerance(self):
        model = rdii_model()
        model.hydrographs.update('UH', responses=(response(peak=.0001, k=1), response(kind='LONG', peak=.0003, k=1)))
        report = model.validate(for_run=True)
        self.assertIn('rdii.truncated_seconds', {d.code for d in report.diagnostics})
        self.assertIn('rdii.inactive_response', {d.code for d in report.diagnostics})
        group = model.hydrographs['UH']
        self.assertEqual((group.responses[1].native_peak_seconds, group.responses[1].native_base_seconds), (1, 2))
        model.hydrographs.update('UH', responses=(response(fraction=1.005),))
        self.assertTrue(model.validate().is_valid)
        self.assertIn('rdii.response_tolerance', {d.code for d in model.validate().diagnostics})


if __name__ == '__main__':
    unittest.main()
