import unittest
from gcode_audit import analyze_bytes

CONFIG = '''; fos_physical_nozzle_diameter = 0.4,0.4,0.2,0.2
; max_layer_height = 0.32,0.32,0.14,0.14
; filament_diameter = 1.75,1.75,1.75,1.75
; filament_max_volumetric_speed = 8,8,4,4
G21
G90
M83
M104 T0 S220
M104 T3 S210
G1 X10 Y10 Z0.1 F600
'''

def audit(body, **kwargs):
    return analyze_bytes((CONFIG + body).encode(), include_bands=True, **kwargs)

def errors(report, code):
    return [x for x in report['path_checks']['errors'] if x['code'] == code]

class PathAuditTests(unittest.TestCase):
    def test_mixed_tower_uses_executing_nozzle_limit(self):
        r=audit('T3\n;TYPE:Prime tower\nG1 X20 E0.1\nG1 Z0.4\nG1 X10 E0.3\n')
        bad=errors(r,'layer_height_exceeded')
        self.assertEqual(len(bad),1)
        self.assertEqual(bad[0]['tool'],3)
        self.assertAlmostEqual(bad[0]['examples'][0]['actual_height_mm'],.3)
        self.assertAlmostEqual(bad[0]['examples'][0]['max_height_mm'],.14)

    def test_large_nozzle_three_tenths_is_allowed(self):
        r=audit('T0\n;TYPE:Support\nG1 X20 E0.1\nG1 Z0.4\nG1 X10 E0.3\n')
        self.assertTrue(r['path_checks']['passed'],r['path_checks']['errors'])

    def test_pla_first_layer_above_pva(self):
        r=audit('; printing object A id:1 copy 0\nT0\n;TYPE:Support\nG1 X20 E0.1\nG1 Z0.4\nG1 X10 E0.3\nT3\nG1 Z0.48\n;TYPE:Outer wall\nG1 X20 E0.1\n')
        self.assertTrue(r['path_checks']['passed'],r['path_checks']['errors'])
        self.assertEqual(r['path_checks']['layer_bands'][-1]['inferred_height_mm'],.08)

    def test_hop_and_retraction_are_not_layers(self):
        r=audit('T3\n;TYPE:Prime tower\nG1 X20 E0.1\nG1 E-1\nG1 Z2\nG1 X10\nG1 Z0.18\nG1 E1\nG1 X20 E0.1\n')
        self.assertTrue(r['path_checks']['passed'])
        self.assertEqual([x['z_mm'] for x in r['path_checks']['layer_bands']],[.1,.18])

    def test_height_comment_does_not_mask_large_step(self):
        r=audit('T3\n;TYPE:Prime tower\n;HEIGHT:0.08\nG1 X20 E0.1\nG1 Z0.4\nG1 X10 E0.3\n')
        self.assertTrue(errors(r,'layer_height_exceeded'))

    def test_other_object_does_not_mask_tower_step(self):
        r=audit('T3\n;TYPE:Prime tower\nG1 X20 E0.1\n; printing object Other id:2 copy 0\n;TYPE:Support\nT0\nG1 X100 Y100 Z0.3\nG1 X110 E0.1\nT3\n;TYPE:Prime tower\nG1 X20 Y10 Z0.4\nG1 X10 E0.3\n')
        self.assertTrue(errors(r,'layer_height_exceeded'))

    def test_relative_compact_commands(self):
        r=audit('T3\n;TYPE:Outer wall\nG91\nG1X10E.1\nG1Z.08\nG1X-10E.1\n')
        self.assertTrue(r['path_checks']['passed'],r['path_checks']['errors'])

    def test_absolute_e_reset_preserves_retraction(self):
        r=audit('T3\nM82\n;TYPE:Outer wall\nG1 X20 E1\nG1 E0\nG92 E0\nG1 X10 E1\nG1 Z.18\nG1 X20 E1.1\n')
        self.assertEqual(r['extrusion_groups']['T3:Outer wall']['segments'],2)
        self.assertTrue(r['path_checks']['passed'],r['path_checks']['errors'])

    def test_live_nozzle_disagrees(self):
        r=audit('T3\n;TYPE:Outer wall\nG1 X20 E0.1\n', physical_nozzles={3:.4})
        self.assertTrue(errors(r,'nozzle_mismatch'))

    def test_unknown_or_arc_cannot_pass(self):
        for command in ['G2 X20 E.1','MY_CUSTOM_MOVE','G92 Z0']:
            with self.subTest(command=command):
                r=audit('T3\n;TYPE:Outer wall\nG1 X20 E.1\n'+command+'\n')
                self.assertFalse(r['path_checks']['passed'])

    def test_volumetric_demand(self):
        r=audit('T3\n;TYPE:Outer wall\nG1 X20 E10 F6000\n')
        self.assertTrue(errors(r,'volumetric_rate_exceeded'))

    def test_missing_diameter_cannot_pass(self):
        r=analyze_bytes(b'G90\nM83\nG1 X10 Y10 Z.1 F600\nG1 X20 E.1\n')
        self.assertTrue(errors(r,'missing_nozzle_diameter'))

    def test_missing_live_nozzle_is_not_verified(self):
        r=audit('T3\n;TYPE:Outer wall\nG1 X20 E0.1\n', physical_nozzles={})
        self.assertTrue(errors(r,'missing_physical_nozzle'))
        self.assertFalse(r['path_checks']['physical_nozzles_verified'])

    def test_nonidentity_mapping_fails_closed(self):
        r=audit('; fos_filament_nozzle = 3,1,2,0\nT3\n;TYPE:Outer wall\nG1 X20 E0.1\n')
        self.assertTrue(errors(r,'unsupported_material_mapping'))

    def test_unmodeled_axis_fails_closed(self):
        r=audit('T3\n;TYPE:Outer wall\nG1 X20 E0.1 A90\n')
        self.assertTrue(errors(r,'unsupported_motion_axis'))

    def test_fan_out_of_range_and_heater_off(self):
        r=audit('T3\nM104 T3 S0\nM106 S999\n;TYPE:Outer wall\nG1 X20 E0.1\n')
        self.assertTrue(errors(r,'invalid_fan'))
        self.assertTrue(errors(r,'heater_off_during_extrusion'))

    def test_low_profile_limit_cannot_be_bypassed_by_integer_e(self):
        r=audit('; filament_max_volumetric_speed = 8,8,4,2\nT3\n;TYPE:Outer wall\nG1 X20 E1 F600\n')
        self.assertTrue(errors(r,'volumetric_rate_exceeded'))

    def test_e_only_prime_does_not_invent_a_layer(self):
        r=audit('T3\nG1 E4 F1800\n;TYPE:Outer wall\nG1 X20 E0.1 F600\n')
        self.assertEqual(r['stationary_priming_by_tool']['3']['filament_mm'],4)
        self.assertEqual(len(r['path_checks']['layer_bands']),1)

if __name__=='__main__':unittest.main()
