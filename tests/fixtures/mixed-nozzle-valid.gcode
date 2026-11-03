; fos_physical_nozzle_diameter = 0.4,0.4,0.2,0.2
; max_layer_height = 0.32,0.32,0.14,0.14
; filament_diameter = 1.75,1.75,1.75,1.75
; filament_max_volumetric_speed = 8,8,4,4
G21
G90
M83
M104 T0 S220
M104 T3 S210
G1 X10 Y10 Z0.1 F600
T0
;TYPE:Support
G1 X20 E0.1
T3
;TYPE:Prime tower
G1 X30 Y30
G1 X40 E0.1
G1 Z0.18
G1 X30 E0.1
