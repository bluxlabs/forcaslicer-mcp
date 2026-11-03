"""Run the same offline regression gates locally and in CI."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile

root = Path(__file__).resolve().parents[1]
p = argparse.ArgumentParser()
p.add_argument('--output', type=Path, required=True)
a = p.parse_args()
out = a.output.resolve()
out.mkdir(parents=True, exist_ok=True)
env = dict(os.environ, TEST_STATE_DIRECTORY=str(out/'test-state'))
subprocess.run([sys.executable,'-m','unittest','discover','-s','tests','-v'], cwd=root, env=env, check=True)
for name, expected in [('mixed-nozzle-valid',0),('mixed-nozzle-invalid',2)]:
    result = subprocess.run([sys.executable,str(root/'gcode_audit.py'),str(root/'tests/fixtures'/f'{name}.gcode'),
        '--physical-nozzles','0:0.4,3:0.2','--json-output',str(out/f'{name}.json'),
        '--junit-output',str(out/f'{name}.xml')], capture_output=True, text=True)
    (out/f'{name}.log').write_text(result.stdout+result.stderr)
    if result.returncode != expected:
        raise SystemExit(f'{name}: expected exit {expected}, got {result.returncode}')
    report=json.loads((out/f'{name}.json').read_text())
    if expected and not any(e['code']=='layer_height_exceeded' and e['tool']==3 and e['feature']=='Prime tower' for e in report['path_checks']['errors']):
        raise SystemExit('Negative fixture did not prove the mixed-nozzle tower gate')
# Explicit source allowlist: local evidence, printer credentials and outputs stay local.
files=[root/f for f in ['server.py','gcode_audit.py','flake.nix','flake.lock','LICENSE','README.md']]
for folder in ['nix','ci','tests','.github']:
    files += [f for f in (root/folder).rglob('*') if f.is_file() and '__pycache__' not in f.parts]
manifest={str(f.relative_to(root)):hashlib.sha256(f.read_bytes()).hexdigest() for f in sorted(files)}
archive=out/'forcaslicer-mcp-source.tar.gz'
with tarfile.open(archive,'w:gz') as tar:
    for f in sorted(files):tar.add(f,arcname='forcaslicer-mcp/'+str(f.relative_to(root)))
(out/'source-manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
(out/'SHA256SUMS').write_text(hashlib.sha256(archive.read_bytes()).hexdigest()+'  '+archive.name+'\n')
(out/'ci-result.json').write_text(json.dumps({'passed':True,'fixtures':2,'printer_access':False})+'\n')
print('Offline gates passed; source package and audit reports:',out)
