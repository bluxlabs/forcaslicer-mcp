"""Offline audit of planar, linear FFF G-code, including mixed nozzle jobs.

Feature labels identify paths; numerical layer heights come from deposited Z
planes, never from ;HEIGHT or ;Z comments. This is not a firmware simulator or
an adhesion/collision certificate. All printer writes must remain elsewhere.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import hashlib
import json
import math
from pathlib import Path
import re
import sys
import xml.etree.ElementTree as ET

AUDIT_VERSION = '1.0.0'
EPS = 1e-5
NUMBER = r'[-+]?(?:\d+(?:\.\d*)?|\.\d+)'
WORD = re.compile(r'([A-Za-z])(' + NUMBER + r')')
COMMAND = re.compile(r'^(?:[GMT]\d+(?:\.\d+)?|[A-Z_][A-Z_0-9]*)(?=\s|[A-Za-z]|$)')
# Explicitly recognized U1 calls remain opaque. Listing is NOT a simulation or
# a claim of physical clearance. Unrecognized calls block the linear audit.
U1_OPAQUE = {
    'PRINT_START', 'PRINT_END', 'DEFECT_DETECTION_START',
    'DEFECT_DETECTION_DETECT', 'DEFECT_DETECTION_DETECT_BED',
    'DEFECT_DETECT_NOODLE_FIRST', 'TIMELAPSE_START', 'TIMELAPSE_STOP',
    'TIMELAPSE_TAKE_FRAME', 'SET_PURIFIER_MODE', 'SM_PRINT_CHECK_SWITCH_EXTRUDER',
    'SM_PRINT_EXTRUDER_PREHEAT', 'SM_PRINT_AUTO_FEED', 'SM_PRINT_FLOW_CALIBRATE',
    'SM_PRINT_PREEXTRUDE_FILAMENT', 'MOVE_TO_DISCARD_FILAMENT_POSITION',
    'MOVE_TO_XY_IDLE_POSITION_EXTRUDER', 'ROUGHLY_CLEAN_NOZZLE_WITH_DISCARD',
    'ROUGHLY_CLEAN_NOZZLE', 'FINELY_CLEAN_NOZZLE_STAGE_1',
    'FINELY_CLEAN_NOZZLE_STAGE_2', 'DETECT_BED_PLATE', 'WAIT_CHAMBER_TEMP',
    'BED_MESH_CALIBRATE',
}
STATE_NEUTRAL = {'G4', 'M73', 'M105', 'M114', 'M115', 'M117', 'M118', 'M400',
                 'M204', 'M205', 'SET_VELOCITY_LIMIT', 'SET_PRESSURE_ADVANCE',
                 'SET_PRINT_STATS_INFO', 'EXCLUDE_OBJECT_DEFINE',
                 'EXCLUDE_OBJECT_START', 'EXCLUDE_OBJECT_END'}


def _numbers(cfg, name):
    value = cfg.get(name, '')
    if isinstance(value, (int, float)):
        value = str(value)
    if isinstance(value, list):
        parts = value
    else:
        parts = str(value).strip().strip('"').replace(';', ',').split(',')
    out = []
    for part in parts:
        try:
            number = float(part)
            out.append(number if math.isfinite(number) else None)
        except (TypeError, ValueError):
            out.append(None)
    return out


def _slot(cfg, name, tool):
    values = _numbers(cfg, name)
    return values[tool] if 0 <= tool < len(values) else None


def _range_add(group, name, value):
    if value is not None and math.isfinite(value):
        current = group.setdefault(name, [value, value])
        current[0] = min(current[0], value)
        current[1] = max(current[1], value)


def _overlap(a, b, margin):
    return all(a['min'][i] <= b['max'][i] + margin and
               b['min'][i] <= a['max'][i] + margin for i in range(2))


def analyze_bytes(data: bytes, *, physical_nozzles: dict[int, float] | None = None,
                  include_bands: bool = False) -> dict:
    """Audit exactly these bytes; this function performs no I/O or printer writes.

    Deposition domains are named objects, the prime tower, and start/custom
    paths. Without object labels, connected bounding regions at each Z are
    tracked separately. Cross-material support may provide a preceding plane.
    Predecessors must already have been deposited and overlap in XY. Bounding
    regions infer layer spacing, NOT contact area or physical support strength.
    """
    lines = data.decode('utf-8-sig').splitlines()
    cfg = {}
    for line in lines:
        match = re.match(r'^;\s*([^=]+?)\s*=\s*(.*)$', line)
        if match:
            cfg[match[1].strip()] = match[2].strip()
    issues = {}

    def issue(code, message, *, line=None, tool=None, feature=None, severity='error', **details):
        key = (severity, code, tool, feature)
        item = issues.setdefault(key, {'severity': severity, 'code': code,
            'message': message, 'tool': tool, 'feature': feature, 'occurrences': 0,
            'examples': []})
        item['occurrences'] += 1
        if len(item['examples']) < 8:
            item['examples'].append({'line': line, **details})

    # Nozzle limits are keyed by the physical tool, not the job's first nozzle.
    nozzle_source = 'fos_physical_nozzle_diameter' if cfg.get('fos_physical_nozzle_diameter') else 'nozzle_diameter'
    nozzles = _numbers(cfg, nozzle_source)
    mapping = _numbers(cfg, 'fos_filament_nozzle') if cfg.get('fos_filament_nozzle') else None
    if mapping is not None and any(value != index for index, value in enumerate(mapping)):
        issue('unsupported_material_mapping', 'Non-identity filament-to-tool mapping needs an explicit mapping audit.')
    limits = {}

    def tool_limits(tool, line):
        if tool in limits:
            return limits[tool]
        declared = nozzles[tool] if 0 <= tool < len(nozzles) else None
        actual = None if physical_nozzles is None else physical_nozzles.get(tool)
        if physical_nozzles is not None and (not isinstance(actual, (int, float)) or
                                               not math.isfinite(actual) or actual <= 0):
            issue('missing_physical_nozzle', 'Used tool has no valid live nozzle diameter.', line=line, tool=tool)
            actual = None
        if not declared or declared <= 0:
            issue('missing_nozzle_diameter', 'Used tool has no valid embedded nozzle diameter.', line=line, tool=tool)
            declared = None
        if actual and declared and abs(actual - declared) > 0.001:
            issue('nozzle_mismatch', 'Embedded and live nozzle diameters disagree.', line=line,
                  tool=tool, embedded_mm=declared, physical_mm=actual)
        diameter = actual or declared
        maximum = _slot(cfg, 'max_layer_height', tool)
        if maximum is not None and maximum < 0:
            issue('invalid_layer_limit', 'Negative maximum layer height.', line=line, tool=tool)
        physical_max = 0.8 * diameter if diameter else None
        maximum = min(maximum, physical_max) if maximum and maximum > 0 and physical_max else physical_max
        filament = _slot(cfg, 'filament_diameter', tool)
        if not filament or filament <= 0:
            issue('missing_filament_diameter', 'Cannot calculate volume without a filament diameter.', line=line, tool=tool)
            filament = None
        material_values = cfg.get('filament_type', '').split(';')
        maximum_flow = _slot(cfg, 'filament_max_volumetric_speed', tool)
        if maximum_flow is None or maximum_flow <= 0:
            issue('missing_flow_limit', 'Used tool requires a positive material volumetric speed limit.', line=line, tool=tool)
        result = {'nozzle_diameter_mm': diameter, 'embedded_nozzle_diameter_mm': declared,
                  'physical_nozzle_diameter_mm': actual, 'max_layer_height_mm': maximum,
                  'filament_diameter_mm': filament, 'max_volumetric_speed_mm3_s': _slot(cfg, 'filament_max_volumetric_speed', tool),
                  'material': material_values[tool].strip('" ') if tool < len(material_values) else None}
        limits[tool] = result
        return result

    pos = {axis: None for axis in 'XYZ'}
    epos = 0.0
    debt = defaultdict(float)
    absolute = True
    rel_e = False
    unit = 1.0
    tool = 0
    speed = 0.0
    speed_factor = 1.0
    saved_speed_factor = 1.0
    flow_factor = 1.0
    feature = 'Custom'
    obj = None
    width = None
    declared_height = None
    targets, fans = {}, {}
    aux = None
    groups, bands = {}, []
    band_index = defaultdict(list)
    fan_commands, temperature_commands = [], []
    used_tools, selected_tools, opaque, unknown = set(), set(), set(), set()
    out_of_bed = []
    stationary = {}
    deposition_started = False
    invalid_coordinate_state = False

    for line_no, raw in enumerate(lines, 1):
        if raw.startswith(';TYPE:'):
            feature = raw[6:].strip() or 'Unclassified'
        elif raw.startswith(';WIDTH:'):
            try: width = float(raw[7:])
            except ValueError: width = None
        elif raw.startswith(';HEIGHT:'):
            try: declared_height = float(raw[8:])
            except ValueError: declared_height = None
        elif raw.startswith('; printing object '):
            obj = raw.split('; printing object ', 1)[1].strip()
        elif raw.startswith('; stop printing object '):
            obj = None
        text = raw.split(';', 1)[0].strip()
        if not text:
            continue
        # Parentheses and checksums are rejected instead of partially parsed.
        if any(char in text for char in '()*') or text.startswith('N') and re.match(r'N\d', text):
            issue('unsupported_syntax', 'Unsupported numbered/checksummed or parenthesized command.', line=line_no)
            continue
        match = COMMAND.match(text)
        if not match:
            issue('unsupported_syntax', 'Cannot parse command.', line=line_no)
            continue
        command = match[0]
        rest = text[len(command):]
        if re.fullmatch(r'[GMT]\d+(?:\.\d+)?', command):
            words = list(WORD.finditer(rest))
            invalid_words = False
            tail = WORD.sub('', rest).strip()
            # Bare B/R are the U1/Klipper speed override save/restore flags;
            # bare axes are only supported for homing.
            allowed_tail = set('BR') if command == 'M220' else set('XYZ') if command == 'G28' else set()
            if tail and any(c not in allowed_tail and not c.isspace() for c in tail):
                issue('unsupported_syntax', 'Unparsed G-code argument.', line=line_no, command=text)
                continue
            vals = {}
            for token in words:
                key, value = token[1].upper(), float(token[2]) * (unit if token[1].upper() in 'XYZEF' else 1)
                if key in vals or not math.isfinite(value):
                    issue('invalid_numeric_word', 'Duplicate or nonfinite numeric argument.', line=line_no)
                    invalid_words = True
                vals[key] = value
            if invalid_words:
                continue
        else:
            vals = {}
        if re.fullmatch(r'T\d+', command):
            tool = int(command[1:]); selected_tools.add(tool)
        elif command == 'G90': absolute = True
        elif command == 'G91': absolute = False
        elif command == 'M83': rel_e = True
        elif command == 'M82': rel_e = False
        elif command == 'G20':
            unit = 25.4
        elif command == 'G21': unit = 1.0
        elif command == 'G92':
            if 'E' in vals: epos = vals['E']
            if any(axis in vals for axis in 'XYZ'):
                invalid_coordinate_state = True
                issue('coordinate_reset', 'G92 XYZ requires work-offset interpretation outside this audit.', line=line_no)
        elif command == 'G28':
            if deposition_started:
                issue('homing_during_deposition', 'Homing after deposition invalidates the inferred layer stack.', line=line_no)
            for axis in 'XYZ':
                if not rest.strip() or axis in rest: pos[axis] = None
        elif command in ('M104', 'M109'):
            target_tool = int(vals.get('T', tool))
            value = vals.get('S', vals.get('R'))
            if value is not None: targets[target_tool] = value
            temperature_commands.append({'line': line_no, 'command': text})
        elif command in ('M140', 'M190'):
            temperature_commands.append({'line': line_no, 'command': text})
        elif command in ('M106', 'M107'):
            value = 0.0 if command == 'M107' else vals.get('S', 255.0)
            fan_commands.append({'line': line_no, 'tool': tool, 'command': text})
            if not 0 <= value <= 255:
                issue('invalid_fan', 'Fan PWM must be in [0,255].', line=line_no, tool=tool, value=value)
            channel = vals.get('P', 0)
            if channel == 2: aux = value
            elif channel == 0: fans[tool] = value
            else: issue('unknown_fan_channel', 'Fan channel mapping is not modeled.', line=line_no, severity='warning', channel=channel)
        elif command == 'M220':
            if 'B' in rest: saved_speed_factor = speed_factor
            if 'R' in rest: speed_factor = saved_speed_factor
            if 'S' in vals:
                speed_factor = vals['S'] / 100.0
                if speed_factor <= 0: issue('invalid_speed_factor', 'Speed factor must be positive.', line=line_no)
        elif command == 'M221':
            if 'T' in vals:
                issue('unsupported_flow_factor', 'Per-tool M221 semantics are not modeled.', line=line_no)
            if 'S' in vals:
                flow_factor = vals['S'] / 100.0
                if flow_factor <= 0: issue('invalid_flow_factor', 'Extrusion factor must be positive.', line=line_no)
        elif command in U1_OPAQUE:
            opaque.add(command)
            if command == 'SM_PRINT_PREEXTRUDE_FILAMENT':
                # Firmware owns loading/priming here. Its E amount is excluded.
                # Reset retraction debt only for the explicit primed tool.
                target = re.search(r'\bINDEX=(\d+)\b', rest)
                if target: debt[int(target[1])] = 0.0
                else: issue('unknown_priming_tool', 'Priming macro has no explicit tool.', line=line_no)
        elif command in STATE_NEUTRAL:
            continue
        elif command in ('G0', 'G1'):
            if set(vals) - set('XYZEF'):
                issue('unsupported_motion_axis', 'Motion contains unmodeled axes or arguments.', line=line_no, command=text)
                continue
            previous = pos.copy()
            nxt = pos.copy()
            for axis in 'XYZ':
                if axis in vals:
                    nxt[axis] = vals[axis] if absolute else (None if pos[axis] is None else pos[axis] + vals[axis])
            delta_e = vals.get('E', 0.0) if rel_e else vals.get('E', epos) - epos
            epos += delta_e
            if 'F' in vals: speed = vals['F']
            if delta_e < 0:
                debt[tool] -= delta_e
                deposited_e = 0.0
            else:
                recovered = min(debt[tool], delta_e)
                debt[tool] -= recovered
                deposited_e = (delta_e - recovered) * flow_factor
            pos = nxt
            if deposited_e <= 1e-12:
                continue
            # E-only primes are not planar deposited lines and cannot set Z planes.
            if not any(axis in vals for axis in 'XYZ'):
                item = stationary.setdefault(str(tool), {'commands': 0, 'filament_mm': 0.0, 'first_line': line_no, 'last_line': line_no})
                item['commands'] += 1
                item['filament_mm'] += deposited_e
                item['last_line'] = line_no
                continue
            if any(previous[a] is None or nxt[a] is None for a in 'XYZ'):
                issue('unknown_position', 'Extrusion starts before all XYZ coordinates are known.', line=line_no, tool=tool, feature=feature)
                continue
            distance = math.dist(list(previous.values()), list(nxt.values()))
            if distance <= EPS:
                continue
            if invalid_coordinate_state:
                continue
            deposition_started = True
            used_tools.add(tool)
            limit = tool_limits(tool, line_no)
            gkey = f'T{tool}:{feature}'
            group = groups.setdefault(gkey, {'tool': tool, 'feature': feature, 'segments': 0,
                'filament_mm': 0.0, 'min': [math.inf]*3, 'max': [-math.inf]*3,
                'peak_flow_mm3_s': 0.0, 'first_line': line_no, 'last_line': line_no})
            group['segments'] += 1; group['filament_mm'] += deposited_e; group['last_line'] = line_no
            for i, axis in enumerate('XYZ'):
                group['min'][i] = min(group['min'][i], previous[axis], nxt[axis])
                group['max'][i] = max(group['max'][i], previous[axis], nxt[axis])
            if any(not 0 <= p[a] <= 270 for p in (previous, nxt) for a in 'XYZ'):
                out_of_bed.append(line_no)
                issue('outside_bed_extrusion', 'An extrusion segment leaves the U1 270 mm build volume.', line=line_no, tool=tool, feature=feature)
            if speed <= 0:
                issue('missing_feedrate', 'Deposition has no positive feedrate.', line=line_no, tool=tool, feature=feature)
            if abs(nxt['Z'] - previous['Z']) > EPS:
                issue('nonplanar_deposition', 'Nonplanar extrusion needs a separate surface audit.', line=line_no, tool=tool, feature=feature)
                continue
            temperature = targets.get(tool)
            _range_add(group, 'temperature_target_c', temperature)
            _range_add(group, 'part_fan_pwm', fans.get(tool))
            _range_add(group, 'aux_fan_pwm', aux)
            if temperature is None:
                issue('unknown_temperature', 'No explicit temperature target for the depositing tool.', line=line_no, tool=tool, feature=feature)
            elif temperature <= 0:
                issue('heater_off_during_extrusion', 'Depositing tool has a zero/off temperature target.', line=line_no, tool=tool, feature=feature)
            else:
                lower, upper = (_slot(cfg, k, tool) for k in ('nozzle_temperature_range_low','nozzle_temperature_range_high'))
                if (lower and temperature < lower) or (upper and temperature > upper):
                    issue('temperature_out_of_range', 'Target temperature is outside the embedded material range.', line=line_no,
                          tool=tool, feature=feature, target_c=temperature, range_c=[lower, upper])
            if width is not None:
                _range_add(group, 'declared_width_mm', width)
                if not math.isfinite(width) or width <= 0:
                    issue('invalid_width', 'Invalid declared path width.', line=line_no, tool=tool, feature=feature)
                elif limit['nozzle_diameter_mm'] and not 0.5*limit['nozzle_diameter_mm'] <= width <= 2*limit['nozzle_diameter_mm']:
                    issue('width_nozzle_mismatch', 'Declared width is outside the audit envelope [0.5D, 2D].',
                          line=line_no, tool=tool, feature=feature, severity='warning', width_mm=width, nozzle_mm=limit['nozzle_diameter_mm'])
            filament = limit['filament_diameter_mm']
            cross_section = None
            if filament:
                area = math.pi * filament**2 / 4.0
                cross_section = deposited_e * area / distance
                demand = cross_section * speed * speed_factor / 60.0
                group['peak_flow_mm3_s'] = max(group['peak_flow_mm3_s'], demand)
                maximum_flow = limit['max_volumetric_speed_mm3_s']
                # Account for E's printed decimal precision and a 5% multiplier margin.
                etoken = next((m[2] for m in WORD.finditer(rest) if m[1].upper() == 'E'), '')
                precision = len(etoken.split('.')[1]) if '.' in etoken else 0
                quant = 0.5 * 10**(-precision) * unit * abs(flow_factor) * area * speed * speed_factor / 60.0 / distance
                if maximum_flow and maximum_flow > 0 and demand > maximum_flow*1.05 + min(quant, maximum_flow*0.05) + 1e-5:
                    issue('volumetric_rate_exceeded', 'Commanded volumetric demand exceeds the tool/material limit.',
                          line=line_no, tool=tool, feature=feature, demand_mm3_s=demand, max_mm3_s=maximum_flow)
            domain = 'prime_tower' if feature == 'Prime tower' else ('object:'+obj if obj else ('custom' if feature == 'Custom' else 'unlabelled'))
            z = round(nxt['Z'], 6)
            key = (domain, tool, feature, z)
            # In unlabeled paths, don't merge disconnected regions at a Z plane.
            candidates = band_index[key]
            rect = {'min': [min(previous['X'], nxt['X']), min(previous['Y'], nxt['Y'])],
                    'max': [max(previous['X'], nxt['X']), max(previous['Y'], nxt['Y'])]}
            band = next((b for b in candidates if domain != 'unlabelled' or _overlap(b, rect, limit['nozzle_diameter_mm'] or 0.4)), None)
            if band is None:
                band = {'domain': domain, 'tool': tool, 'feature': feature, 'z_mm': z,
                        'min': rect['min'], 'max': rect['max'], 'first_line': line_no,
                        'last_line': line_no, 'segments': 0, 'length_mm': 0.0, 'volume_mm3': 0.0,
                        'declared_heights_mm': set(), 'declared_widths_mm': set()}
                bands.append(band); candidates.append(band)
            for i in range(2):
                band['min'][i] = min(band['min'][i], rect['min'][i]); band['max'][i] = max(band['max'][i], rect['max'][i])
            band['segments'] += 1; band['last_line'] = line_no; band['length_mm'] += distance
            if cross_section is not None:
                band['volume_mm3'] += cross_section * distance
                _range_add(band, 'cross_section_mm2', cross_section)
            if declared_height is not None and math.isfinite(declared_height): band['declared_heights_mm'].add(declared_height)
            if width is not None and math.isfinite(width): band['declared_widths_mm'].add(width)
        else:
            unknown.add(command)
            issue('unsupported_command', 'Command semantics are not modeled.', line=line_no, command=command)

    # Find prior deposited planes separately for each object/tower. Considering
    # all materials in a domain is essential: the first PLA layer rests on PVA,
    # not at bed Z=0 and not on the previous PLA tower height.
    domains = defaultdict(list)
    support_bands = []
    for band in bands:
        domains[band['domain']].append(band)
        if band['feature'] in ('Support', 'Support interface', 'Raft'): support_bands.append(band)
    for domain_bands in domains.values():
        domain_bands.sort(key=lambda b: b['z_mm'])
    for band in bands:
        tool, feature, z = band['tool'], band['feature'], band['z_mm']
        limit = limits[tool]
        margin = limit['nozzle_diameter_mm'] or 0.4
        candidates = [b for b in domains[band['domain']] if b['z_mm'] < z-EPS and b['first_line'] < band['first_line']
                      and _overlap(b, band, margin)]
        basis = 'prior_deposited_plane_in_domain'
        if band['domain'] not in ('prime_tower', 'custom'):
            candidates += [b for b in support_bands if b['z_mm'] < z-EPS and b['first_line'] < band['first_line'] and _overlap(b, band, margin)]
        previous = max(candidates, key=lambda b: b['z_mm']) if candidates else None
        if previous and previous['domain'] != band['domain']:
            basis = 'prior_support_bounding_region'
        lower_z = previous['z_mm'] if previous else 0.0
        height = round(z-lower_z, 6)
        band.update({'previous_z_mm': lower_z, 'inferred_height_mm': height,
                     'height_basis': basis if previous else 'bed_reference',
                     'previous_tool': previous['tool'] if previous else None,
                     'previous_feature': previous['feature'] if previous else None})
        group = groups[f'T{tool}:{feature}']
        _range_add(group, 'inferred_height_mm', height)
        group.setdefault('deposited_z_mm', set()).add(z)
        if previous and previous['last_line'] >= band['first_line']:
            issue('interleaved_planes', 'Lower and upper deposition planes are interleaved; predecessor is ambiguous.',
                  line=band['first_line'], tool=tool, feature=feature)
        maximum = limit['max_layer_height_mm']
        if height <= EPS:
            issue('invalid_deposition_height', 'Deposition must have positive height above its preceding plane.',
                  line=band['first_line'], tool=tool, feature=feature, height_mm=height)
        elif maximum and height > maximum + 0.002:
            issue('layer_height_exceeded', 'Actual deposition-plane spacing exceeds the depositing nozzle limit.',
                  line=band['first_line'], tool=tool, feature=feature, domain=band['domain'], z_mm=z,
                  previous_z_mm=lower_z, actual_height_mm=height, max_height_mm=maximum,
                  nozzle_diameter_mm=limit['nozzle_diameter_mm'], basis=band['height_basis'])
        # Equivalent rectangular/rounded bead width is a diagnostic, not a
        # measured physical width; bridges/priming may intentionally differ.
        if height > EPS and band['volume_mm3'] and band['length_mm']:
            equivalent_width = band['volume_mm3']/band['length_mm']/height + height*(1-math.pi/4)
            band['equivalent_width_mm'] = equivalent_width
            _range_add(group, 'equivalent_width_mm', equivalent_width)
            for cross in band.get('cross_section_mm2', []):
                _range_add(group, 'segment_equivalent_width_mm', cross/height + height*(1-math.pi/4))
        # Height comments are useful cross-checks, never the source of approval.
        if band['declared_heights_mm'] and any(abs(h-height) > 0.005 for h in band['declared_heights_mm']):
            issue('height_annotation_disagrees', 'Height annotation differs from inferred deposition spacing.',
                  line=band['first_line'], tool=tool, feature=feature, severity='warning',
                  declared=sorted(band['declared_heights_mm']), inferred=height)
        band['declared_heights_mm'] = sorted(band['declared_heights_mm'])
        band['declared_widths_mm'] = sorted(band['declared_widths_mm'])
    for group in groups.values():
        group['deposited_z_mm'] = sorted(group.get('deposited_z_mm', []))
    if not groups:
        issue('no_auditable_deposition', 'No auditable planar extrusion found.')
    if stationary:
        issue('stationary_priming', 'E-only primes are counted separately; their physical location and clearance require firmware review.', severity='warning')
    if opaque:
        issue('opaque_firmware_calls', 'Known U1 firmware calls are listed but not simulated. Runtime state and physical clearance require separate verification.',
              severity='warning', commands=sorted(opaque))
    findings = list(issues.values())
    errors = [item for item in findings if item['severity'] == 'error']
    warnings = [item for item in findings if item['severity'] == 'warning']
    report = {'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data), 'audit_version': AUDIT_VERSION,
              'settings': cfg, 'tools_zero_based': sorted(selected_tools | used_tools),
              'used_deposition_tools': sorted(used_tools), 'extrusion_groups': groups,
              'stationary_priming_by_tool': stationary,
              'fan_commands': fan_commands, 'temperature_commands': temperature_commands,
              'out_of_bed_extrusion_lines': out_of_bed, 'unsupported_motion_commands': sorted(unknown),
              'total_layers': next((int(m[1]) for line in lines if (m:=re.match(r'; total layer number: (\d+)', line))), None),
              'boundary_assumption': 'U1 270 mm volume; emitted linear paths only; firmware motion is not simulated',
              'path_checks': {'passed': not errors, 'scope': 'emitted_planar_linear_extrusion',
                  'physical_nozzles_verified': physical_nozzles is not None and bool(used_tools) and not any(e['code'] in ('missing_physical_nozzle', 'nozzle_mismatch', 'missing_nozzle_diameter') for e in errors),
                  'nozzle_limits': {str(k):v for k,v in limits.items()},
                  'error_count': len(errors), 'errors': errors, 'warnings': warnings,
                  'layer_band_count': len(bands),
                  'limitations': ['Layer spacing uses prior deposited XY bounding regions; it does not prove local support or adhesion.',
                      'Features and object names use slicer labels. Numeric HEIGHT/Z comments are not trusted as motion.',
                      'Temperature and fans are commanded targets, not measured material conditions.',
                      'Equivalent bead width and feedrate-based flow are estimates, not physical measurements.',
                      'Known U1 firmware macro motion, bed mesh and tool offset compensation are not simulated.',
                      'No collision, adhesion, moisture, dimensional accuracy or overall print-safety certification.']}}
    if include_bands: report['path_checks']['layer_bands'] = bands
    return report


def require_pass(report):
    if not report['path_checks']['passed']:
        codes = ', '.join(sorted({e['code'] for e in report['path_checks']['errors']}))
        raise ValueError(f'G-code path audit failed ({codes}); run inspect_gcode for evidence. SHA-256 {report["sha256"]}')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('gcode', type=Path)
    parser.add_argument('--physical-nozzles', help='Explicit tool:diameter pairs, e.g. 0:0.4,3:0.2')
    parser.add_argument('--json-output', type=Path)
    parser.add_argument('--junit-output', type=Path)
    args = parser.parse_args(argv)
    nozzles = None
    if args.physical_nozzles:
        nozzles = {int(k): float(v) for k,v in (pair.split(':') for pair in args.physical_nozzles.split(','))}
    report = analyze_bytes(args.gcode.read_bytes(), physical_nozzles=nozzles, include_bands=True)
    report['path'] = str(args.gcode.resolve())
    if args.json_output:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False)+'\n')
    if args.junit_output:
        suite = ET.Element('testsuite', name='gcode-path-audit', tests='1', failures=str(int(not report['path_checks']['passed'])))
        case = ET.SubElement(suite, 'testcase', name=args.gcode.name, classname='gcode_audit')
        if not report['path_checks']['passed']:
            ET.SubElement(case, 'failure', message='G-code path audit rejected').text = json.dumps(report['path_checks']['errors'], ensure_ascii=False)
        args.junit_output.parent.mkdir(parents=True, exist_ok=True)
        ET.ElementTree(suite).write(args.junit_output, encoding='utf-8', xml_declaration=True)
    print(json.dumps({'sha256':report['sha256'], **{k:report['path_checks'][k] for k in ('passed','error_count','errors','layer_band_count')}}, ensure_ascii=False))
    return 0 if report['path_checks']['passed'] else 2


if __name__ == '__main__':
    sys.exit(main())
