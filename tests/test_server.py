import importlib.util, json, os, tempfile, unittest, zipfile
from pathlib import Path
from unittest.mock import patch
from test_gcode_audit import CONFIG

spec=importlib.util.spec_from_file_location('server',Path(__file__).parents[1]/'server.py')
s=importlib.util.module_from_spec(spec);spec.loader.exec_module(s)

class ToolsTest(unittest.TestCase):
    def setUp(self):
        base=Path(os.environ.get('TEST_STATE_DIRECTORY',Path.cwd()/'.test-work'));base.mkdir(exist_ok=True,parents=True)
        self.tmp=tempfile.TemporaryDirectory(dir=base);self.root=Path(self.tmp.name).resolve()
        self.cfg=patch.object(s,'settings',return_value={'workspace':self.root,'printer_url':'http://127.0.0.1:1'});self.cfg.start()
    def tearDown(self):self.cfg.stop();self.tmp.cleanup()
    def test_project_copy_keeps_geometry_and_refuses_overwrite(self):
        a=self.root/'a.3mf';b=self.root/'b.3mf'
        with zipfile.ZipFile(a,'w') as z:
            z.writestr('Metadata/project_settings.config',json.dumps({'layer_height':'0.1','fan':['60','80']}));z.writestr('3D/3dmodel.model','geometry')
        s.update_project('a.3mf','b.3mf',{'layer_height':'0.08'})
        with zipfile.ZipFile(b) as z:self.assertEqual(z.read('3D/3dmodel.model'),b'geometry')
        self.assertEqual(s.project_config(b)['layer_height'],'0.08')
        with self.assertRaises(FileExistsError):s.update_project('a.3mf','b.3mf',{})
        with self.assertRaises(ValueError):s.update_project('a.3mf','c.3mf',{'fan':['60']})
    def test_workspace_escape_and_symlink(self):
        with self.assertRaises(ValueError):s.path_in_workspace('../outside',False)
        (self.root/'escape').symlink_to('/etc/passwd')
        with self.assertRaises(ValueError):s.path_in_workspace('escape')
    def test_actual_extrusion_audit(self):
        p=self.root/'a.gcode';p.write_text('; total layer number: 2\nM83\nG90\nT3\nG1 X100 Y100 Z0.1\n;TYPE:Outer wall\nG1 X101 E0.1 F600\nG1 X300 E0.1\n')
        r=s.gcode_report(p)
        self.assertEqual(r['tools_zero_based'],[3]);self.assertEqual(r['total_layers'],2)
        self.assertEqual(len(r['out_of_bed_extrusion_lines']),1)
    def test_changed_hash_and_unclear_plate_never_start(self):
        with patch.object(s,'request') as req:
            with self.assertRaises(ValueError):s.start_print('test.gcode','abc',False)
            req.assert_not_called()
        with patch.object(s,'idle'),patch.object(s,'remote_gcode',return_value=b'changed'),patch.object(s,'request') as req:
            with self.assertRaises(ValueError):s.start_print('test.gcode','abc',True)
            req.assert_not_called()
    def test_active_job_blocks_start(self):
        with patch.object(s,'state',return_value={'webhooks':{'state':'ready'},'print_stats':{'state':'printing'}}),patch.object(s,'request') as req:
            with self.assertRaises(ValueError):s.start_print('test.gcode','abc',True)
            req.assert_not_called()
    def test_existing_remote_bytes_are_not_overwritten(self):
        p=self.root/'a.gcode';p.write_text(CONFIG+'T3\n;TYPE:Outer wall\nG1 X20 E0.1\n')
        with patch.object(s,'idle',return_value={'extruder3':{'nozzle_diameter':.2}}),patch.object(s,'remote_gcode',return_value=b'other'),patch.object(s,'request') as req:
            with self.assertRaises(ValueError):s.upload_gcode('a.gcode','test.gcode',s.digest(p.read_bytes()))
            req.assert_not_called()
    def test_pause_does_not_affect_another_job(self):
        with patch.object(s,'state',return_value={'print_stats':{'filename':'other.gcode','state':'printing'}}),patch.object(s,'request') as req:
            with self.assertRaises(ValueError):s.pause_print('expected.gcode')
            req.assert_not_called()

    def good_bytes(self):
        return (CONFIG+'T3\n;TYPE:Prime tower\nG1 X20 E0.1\nG1 Z0.18\nG1 X10 E0.1\n').encode()

    def bad_bytes(self):
        return self.good_bytes().replace(b'Z0.18',b'Z0.4')

    def test_upload_rejects_bad_path_before_network(self):
        data=self.bad_bytes();(self.root/'bad.gcode').write_bytes(data)
        with patch.object(s,'request') as req,patch.object(s,'idle') as idle:
            with self.assertRaisesRegex(ValueError,'layer_height_exceeded'):
                s.upload_gcode('bad.gcode','renamed.gcode',s.digest(data))
            req.assert_not_called();idle.assert_not_called()

    def test_start_reaudits_bad_remote_bytes_even_with_matching_hash(self):
        data=self.bad_bytes()
        with patch.object(s,'idle',return_value={'extruder3':{'nozzle_diameter':.2}}),patch.object(s,'remote_gcode',return_value=data),patch.object(s,'request') as req:
            with self.assertRaisesRegex(ValueError,'layer_height_exceeded'):
                s.start_print('renamed.gcode',s.digest(data),True)
            req.assert_not_called()

    def test_nozzle_mismatch_blocks_upload_and_start(self):
        data=self.good_bytes();(self.root/'a.gcode').write_bytes(data)
        for method,args in [(s.upload_gcode,('a.gcode','a.gcode',s.digest(data))), (s.start_print,('a.gcode',s.digest(data),True))]:
            with self.subTest(method=method.__name__),patch.object(s,'idle',return_value={'extruder3':{'nozzle_diameter':.4}}),patch.object(s,'remote_gcode',return_value=data),patch.object(s,'request') as req:
                with self.assertRaisesRegex(ValueError,'nozzle_mismatch'):method(*args)
                req.assert_not_called()

    def test_file_replaced_during_audit_never_starts(self):
        data=self.good_bytes()
        with patch.object(s,'idle',return_value={'extruder3':{'nozzle_diameter':.2}}),patch.object(s,'remote_gcode',side_effect=[data,b'replaced']),patch.object(s,'request') as req:
            with self.assertRaisesRegex(ValueError,'changed during'):s.start_print('a.gcode',s.digest(data),True)
            req.assert_not_called()

    def test_nozzle_change_during_audit_never_starts(self):
        data=self.good_bytes()
        with patch.object(s,'idle',side_effect=[{'extruder3':{'nozzle_diameter':.2}},{'extruder3':{'nozzle_diameter':.4}}]),patch.object(s,'remote_gcode',return_value=data),patch.object(s,'request') as req:
            with self.assertRaisesRegex(ValueError,'changed during'):s.start_print('a.gcode',s.digest(data),True)
            req.assert_not_called()

    def test_valid_start_calls_commit_once(self):
        data=self.good_bytes()
        with patch.object(s,'idle',return_value={'extruder3':{'nozzle_diameter':.2}}) as idle,patch.object(s,'remote_gcode',return_value=data) as readback,patch.object(s,'request',return_value={'ok':True}) as req,patch.object(s,'state',return_value={}):
            result=s.start_print('a.gcode',s.digest(data),True)
            req.assert_called_once_with('/printer/print/start',b'{"filename": "a.gcode"}')
            self.assertEqual(idle.call_count,2);self.assertEqual(readback.call_count,2)
            self.assertEqual(result['request_result'],{'ok':True})

    def test_valid_existing_upload_is_idempotent(self):
        data=self.good_bytes();(self.root/'a.gcode').write_bytes(data)
        with patch.object(s,'idle',return_value={'extruder3':{'nozzle_diameter':.2}}),patch.object(s,'remote_gcode',return_value=data),patch.object(s,'request') as req:
            self.assertTrue(s.upload_gcode('a.gcode','a.gcode',s.digest(data))['verified_exact_readback'])
            req.assert_not_called()

if __name__=='__main__':unittest.main()
