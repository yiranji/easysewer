"""Storage/identity tests use an opaque synthetic native tail, never claim C validity."""
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import struct
import tempfile
import unittest
from unittest.mock import patch

from easysewer.io.json import JsonDocument
from easysewer.runtime._checkpoint_container import Builder, Limits, load, execution_digest
from easysewer.runtime.backend import BackendInfo
from easysewer.runtime.results import RunSnapshot, ResourceSnapshot
from easysewer.model import Ref
from test_options_v2 import network
from test_runner_v2 import config


def snapshot(root, *, model=None, backend=None, resources=(), settings=None):
    model = network() if model is None else model
    source = model.to_document().text.encode()
    raw = model.to_json_document().to_bytes()
    backend = backend or BackendInfo(key='swmm:standard',available=True,reason=None,
        library='/old/solver',sha256='a'*64,engine_version=52004,platform='Test',architecture='64',
        abi='cdecl:64',profiles=(model.profile.key,),capabilities=(),isolation='process')
    return RunSnapshot(run_id='checkpoint-fixture',created_at=datetime(2020,1,1,tzinfo=timezone.utc),
        model_json=raw,config_json=config(root/'published').to_json_document().to_bytes(),
        model_sha256=hashlib.sha256(raw).hexdigest(),input_bytes=source,input_sha256=hashlib.sha256(source).hexdigest(),
        profile=model.profile,units=model.units,options=model.effective_options,backend=backend,
        resources=resources,execution_directory=str(root),backend_settings=settings)


def native_prefix(value, binding, seconds=7):
    raw = bytearray(220)
    raw[:8] = b'ESCKPT02'; raw[20:52] = binding; raw[52:60] = b'ESCLK001'
    family = int(value.backend.key == 'easysewer:flexible-ponding')
    struct.pack_into('<Iii',raw,8,2,family,52004)
    struct.pack_into('<i',raw,60,family)
    struct.pack_into('<d',raw,100,value.options.duration.total_seconds()*1000)
    struct.pack_into('<d',raw,160,seconds*1000)
    return bytes(raw)+b'opaque synthetic owners'


def save(root):
    value = snapshot(root)
    with Builder(root/'saved',value) as builder:
        return builder.finish(native_prefix(value,builder.binding),())


def rewrite(root, change):
    path = root/'checkpoint.json'
    data = json.loads(path.read_bytes());change(data)
    raw = JsonDocument.from_data(data).to_bytes();path.write_bytes(raw)
    (root/'checkpoint.commit').write_bytes(hashlib.sha256(raw).hexdigest().encode()+b'\n')


class CheckpointContainerTests(unittest.TestCase):
    def test_storage_loads_without_native_and_moves_without_original_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);original=root/'original';original.mkdir()
            with patch('ctypes.CDLL',side_effect=AssertionError('No native loading')):
                saved=save(original)
                (original/'saved').rename(root/'搬移存档')
                original.rmdir()
                loaded=load(root/'搬移存档')
                self.assertEqual(loaded.snapshot,saved.snapshot)
                self.assertEqual(loaded.native_state,saved.native_state)
                self.assertEqual(loaded.binding,saved.binding)
                self.assertEqual(loaded.simulation_seconds,7)
                self.assertNotIn('library',loaded.data)

    def test_binding_covers_configuration_model_engine_profile_and_generated_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);value=snapshot(root)
            first=execution_digest(value,[])
            self.assertEqual(execution_digest(replace(value,execution_directory='/new',
                backend=replace(value.backend,library='/new/lib')),[]),first)
            variants=[replace(value,config_json=JsonDocument.from_data({'changed':True}).to_bytes()),
                      replace(value,backend=replace(value.backend,sha256='b'*64)),
                      replace(value,backend=replace(value.backend,abi='different'))]
            model=network();model.update_options(allow_ponding=True)
            variants.append(snapshot(root,model=model))
            for changed in variants:self.assertNotEqual(execution_digest(changed,[]),first)
            self.assertNotEqual(execution_digest(value,[dict(identity='swmm:input:rain',
                blob=dict(sha256='c'*64,size=10))]),first)

    def test_inventory_corruption_and_commits_are_rejected(self):
        mutations=[lambda d:d.update(schema_version='2.0'),lambda d:d.update(extra=1),
                   lambda d:d.update(execution_sha256='0'*64),
                   lambda d:d['blobs'].append(d['blobs'][0]),
                   lambda d:d['native_state'].update(size=True),
                   lambda d:d['native_inputs'].extend([dict(identity='../escape',blob=d['native_state'])]),
                   lambda d:d['outputs'].append(dict(index=1,role=1,text=False,blob=d['native_state']))]
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index),tempfile.TemporaryDirectory() as directory:
                root=Path(directory);save(root);rewrite(root/'saved',mutate)
                with self.assertRaises((ValueError,TypeError)):load(root/'saved')
        for mode in ('missing-commit','bad-commit','truncated','changed','extra-file','missing-blob'):
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as directory:
                root=Path(directory);saved=save(root);folder=root/'saved'
                blob=folder/'blobs'/saved.data['native_state']['sha256']
                if mode=='missing-commit':(folder/'checkpoint.commit').unlink()
                elif mode=='bad-commit':(folder/'checkpoint.commit').write_bytes(b'0'*64+b'\n')
                elif mode=='truncated':blob.write_bytes(blob.read_bytes()[:-1])
                elif mode=='changed':blob.write_bytes(b'X'+blob.read_bytes()[1:])
                elif mode=='extra-file':(folder/'blobs'/'unexpected').touch()
                else:blob.unlink()
                with self.assertRaises(ValueError):load(folder)

    def test_failed_save_cancellation_limits_and_existing_directory_preserve_ownership(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);value=snapshot(root);target=root/'saved'
            for mode in ('cancel','limit','bad-binding','write'):
                def cancel(): raise KeyboardInterrupt('cancel capture')
                limits=Limits(total_bytes=1) if mode=='limit' else Limits()
                with self.subTest(mode=mode), self.assertRaises((KeyboardInterrupt,ValueError,OSError)):
                    with Builder(target,value,limits=limits,checkpoint=cancel if mode=='cancel' else lambda:None) as builder:
                        raw=native_prefix(value,builder.binding if mode!='bad-binding' else b'\0'*32)
                        if mode=='write':
                            with patch('os.fsync',side_effect=OSError('disk full')):builder.finish(raw,())
                        else:builder.finish(raw,())
                self.assertFalse(target.exists())
            target.mkdir();(target/'keep').write_bytes(b'original')
            with self.assertRaises(FileExistsError):
                with Builder(target,value):pass
            self.assertEqual((target/'keep').read_bytes(),b'original')

    def test_input_hashes_sealing_and_copy_after_load_are_checked(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=root/'input';source.write_bytes(b'original')
            value=snapshot(root)
            with Builder(root/'saved',value) as builder:
                builder.add_input('swmm:input:rain',source)
                binding=builder.binding
                with self.assertRaises(ValueError):builder.add_input('swmm:input:rdii',source)
                saved=builder.finish(native_prefix(value,binding),())
            desc=saved.data['native_inputs'][0]['blob']
            saved.copy_blob(desc,root/'copy');self.assertEqual((root/'copy').read_bytes(),b'original')
            with self.assertRaises(FileExistsError):saved.copy_blob(desc,root/'copy')
            blob=saved.directory/'blobs'/desc['sha256'];blob.write_bytes(b'changed!')
            with self.assertRaises(ValueError):saved.copy_blob(desc,root/'rejected')
            self.assertFalse((root/'rejected').exists())
            self.assertEqual((root/'copy').read_bytes(),b'original')

    def test_snapshot_resource_mismatch_and_unsafe_reconstruction_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'input').write_bytes(b'wrong')
            resource=ResourceSnapshot(owner=Ref(collection='swmm:timeseries',key='R'),field=('file',),
                role='swmm:timeseries',format='test',kind='file',access='read',active=True,required=True,
                original_path='/old/input',relative_path='input',sha256='a'*64,size=5)
            with self.assertRaises(ValueError):
                with Builder(root/'saved',snapshot(root,resources=(resource,))):pass
            self.assertFalse((root/'saved').exists())
            for path in ('../outside','/absolute','C:/external','a\\b','NUL','a/../b'):
                with self.subTest(path=path),self.assertRaises(ValueError):
                    with Builder(root/'saved',snapshot(root,resources=(replace(resource,relative_path=path),))):pass
                self.assertFalse((root/'saved').exists())

    def test_symlink_blob_rejected_and_load_limits_enforced(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);saved=save(root)
            with self.assertRaises(ValueError):load(root/'saved',limits=Limits(native_bytes=1))
            with self.assertRaises(ValueError):load(root/'saved',limits=Limits(manifest_bytes=1))
            with self.assertRaises(ValueError):load(root/'saved',limits=Limits(total_bytes=1))
            desc=saved.data['native_state'];blob=root/'saved'/'blobs'/desc['sha256']
            outside=root/'outside';shutil.copyfile(blob,outside);blob.unlink()
            try:blob.symlink_to(outside)
            except OSError as error:
                blob.write_bytes(outside.read_bytes())
                self.skipTest('Symlink creation unavailable: '+str(error))
            with self.assertRaises(ValueError):load(root/'saved')
            self.assertEqual(outside.read_bytes(),saved.native_state)

    def test_executed_input_cannot_reconstruct_external_output_or_unlisted_input(self):
        for statement in ('SAVE HOTSTART "/outside/state.hsf"','USE INFLOWS "../outside/data"',
                          'USE INFLOWS "missing.dat"'):
            with self.subTest(statement=statement),tempfile.TemporaryDirectory() as directory:
                root=Path(directory);value=snapshot(root)
                raw=value.input_bytes+b'\n[FILES]\n'+statement.encode()+b'\n'
                value=replace(value,input_bytes=raw,input_sha256=hashlib.sha256(raw).hexdigest())
                with Builder(root/'saved',value) as builder:
                    saved=builder.finish(native_prefix(value,builder.binding),())
                with self.assertRaises(ValueError):saved.materialize(root/'unsafe')
                self.assertFalse((root/'unsafe').exists())

    def test_large_input_change_and_cancellation_leave_no_partial_archive(self):
        for mode in ('changed','cancelled'):
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as directory:
                root=Path(directory);source=root/'source';raw=b'A'*(3*1024**2);source.write_bytes(raw)
                expected=dict(sha256=hashlib.sha256(raw).hexdigest(),size=len(raw))
                armed=False;calls=0
                def check():
                    nonlocal calls
                    if not armed:return
                    calls+=1
                    if calls==2:
                        if mode=='cancelled':raise KeyboardInterrupt('cancel streaming input')
                        with source.open('r+b') as stream:stream.write(b'B')
                with self.assertRaises((ValueError,KeyboardInterrupt)):
                    with Builder(root/'saved',snapshot(root),checkpoint=check) as builder:
                        armed=True
                        builder.add_input('swmm:input:rain',source,expected=expected)
                self.assertFalse((root/'saved').exists())
                self.assertTrue(source.is_file())

    def test_portable_output_aliases_are_rejected_before_workspace_creation(self):
        for name in ('MODEL.INP', 'MODEL.OUT', 'model.out/nested/state.hsf'):
            with self.subTest(name=name),tempfile.TemporaryDirectory() as directory:
                root=Path(directory);value=snapshot(root)
                raw=value.input_bytes+b'\n[FILES]\nSAVE HOTSTART "'+name.encode()+b'"\n'
                value=replace(value,input_bytes=raw,input_sha256=hashlib.sha256(raw).hexdigest())
                with Builder(root/'saved',value) as builder:
                    saved=builder.finish(native_prefix(value,builder.binding),())
                with self.assertRaisesRegex(ValueError,'alias|ownership conflict'):
                    saved.materialize(root/'rejected')
                self.assertFalse((root/'rejected').exists())

    def test_materialize_failure_cleans_only_created_workspace(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);saved=save(root);target=root/'restored'
            def cancel():raise KeyboardInterrupt('cancel reconstruction')
            with self.assertRaises(KeyboardInterrupt):saved.materialize(target,checkpoint=cancel)
            self.assertFalse(target.exists())
            with patch('os.fsync',side_effect=OSError('disk full')):
                with self.assertRaises(OSError):saved.materialize(target)
            self.assertFalse(target.exists())
            target.mkdir();(target/'keep').write_bytes(b'original')
            with self.assertRaises(FileExistsError):saved.materialize(target)
            self.assertEqual((target/'keep').read_bytes(),b'original')
            self.assertEqual(load(root/'saved').native_state,saved.native_state)
