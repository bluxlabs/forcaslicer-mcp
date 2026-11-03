"""FOrcaSlicer project and Moonraker tools over MCP stdio."""
from __future__ import annotations
import asyncio, fcntl, hashlib, json, math, os, re, time, urllib.error, urllib.parse, urllib.request, uuid, zipfile
from pathlib import Path
from mcp.server.fastmcp import FastMCP, Image

from gcode_audit import analyze_bytes, require_pass

VERSION = '0.2.0'
mcp = FastMCP('FOrcaSlicer', instructions='Edit copies, slice and inspect G-code in the configured workspace. Printer writes require explicit user authorization. Before starting, inspect the exact reviewed file, verify hardware/materials and a clear build plate. Camera timestamps establish freshness, not a clear build plate. Physical actions are never retried automatically.')
CONFIG = Path(os.environ.get('FORCASLICER_MCP_CONFIG', Path.home()/'.config/forcaslicer-mcp/config.json'))

def settings():
    d = json.loads(CONFIG.read_text())
    d['workspace'] = Path(d['workspace']).expanduser().resolve()
    d['workspace'].mkdir(parents=True, exist_ok=True)
    return d

def path_in_workspace(name, exists=True):
    root = settings()['workspace']
    p = Path(name).expanduser()
    p = (p if p.is_absolute() else root/p).resolve()
    if not p.is_relative_to(root): raise ValueError('Path must remain in configured workspace')
    if exists and not p.is_file(): raise ValueError('File does not exist')
    return p

def digest(data): return hashlib.sha256(data).hexdigest()

def save_new(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('xb') as f: f.write(data)

def request(endpoint, data=None, content_type='application/json', raw=False):
    cfg = settings(); base = cfg.get('printer_url','').rstrip('/')
    u = urllib.parse.urlsplit(base)
    if u.scheme not in ('http','https') or not u.hostname or u.query or u.fragment or u.path not in ('','/'):
        raise ValueError('Configure printer_url as a direct Moonraker HTTP(S) origin')
    headers = {}
    if data is not None: headers['Content-Type'] = content_type
    if cfg.get('api_key_file'): headers['X-Api-Key'] = Path(cfg['api_key_file']).expanduser().read_text().strip()
    req = urllib.request.Request(base+endpoint, data=data, headers=headers)
    # LAN calls bypass shell/browser proxy settings. Do not follow redirected writes.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs): return None
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with op.open(req, timeout=30) as r: b=r.read()
    return b if raw else json.loads(b)

def state():
    objects = ['print_stats','webhooks','virtual_sdcard','heater_bed','extruder','extruder1','extruder2','extruder3','toolhead','fan','fan_generic e1_fan','fan_generic e2_fan','fan_generic e3_fan','fan_generic cavity_fan']
    return request('/printer/objects/query?'+'&'.join(urllib.parse.quote(x,safe='') for x in objects))['result']['status']

def idle():
    s=state()
    if s['webhooks']['state']!='ready' or s['print_stats']['state'] in ('printing','paused'):
        raise ValueError('Printer must be ready and idle')
    return s

def physical_lock():
    p=settings()['workspace']/'.printer.lock';f=p.open('a')
    try: fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError: f.close();raise ValueError('Another adapter operation is using the printer')
    return f

def project_config(p):
    with zipfile.ZipFile(p) as z:
        info=z.getinfo('Metadata/project_settings.config')
        if info.file_size>8*1024*1024: raise ValueError('Project settings exceed 8 MiB')
        return json.loads(z.read(info))

def copy_project(src,dst,changes):
    d=project_config(src)
    for k,v in changes.items():
        if k not in d: raise ValueError(f'Unknown project setting: {k}')
        if isinstance(v,(dict,type(None))): raise ValueError('Use the existing setting value shape')
        if isinstance(d[k],list)!=isinstance(v,list): raise ValueError(f'Preserve array/scalar shape for {k}')
        if isinstance(v,list) and len(v)!=len(d[k]): raise ValueError(f'Preserve array length for {k}')
        d[k]=v
    dst.parent.mkdir(parents=True,exist_ok=True)
    with dst.open('xb') as f,zipfile.ZipFile(src) as zin,zipfile.ZipFile(f,'w') as out:
        for info in zin.infolist():
            b=json.dumps(d,ensure_ascii=False,indent=2).encode() if info.filename=='Metadata/project_settings.config' else zin.read(info)
            out.writestr(info,b)
    return d

@mcp.tool(annotations={'readOnlyHint':True})
def inspect_project(project: str, keys: list[str] | None = None) -> dict:
    """Read embedded project settings. Names are paths inside the configured workspace."""
    p=path_in_workspace(project);d=project_config(p)
    return {'path':str(p),'sha256':digest(p.read_bytes()),'settings':d if keys is None else {k:d.get(k) for k in keys}}

@mcp.tool(annotations={'readOnlyHint':False,'destructiveHint':False})
def update_project(project: str, output: str, changes: dict) -> dict:
    """Copy a 3MF and change existing embedded settings; output must not already exist. Geometry and model overrides are preserved."""
    src=path_in_workspace(project);dst=path_in_workspace(output,False)
    copy_project(src,dst,changes)
    return {'path':str(dst),'sha256':digest(dst.read_bytes()),'changes':changes,'note':'Per-object overrides may take precedence; inspect generated G-code.'}

def gcode_report(p, physical_nozzles=None):
    report = analyze_bytes(p.read_bytes(), physical_nozzles=physical_nozzles)
    return {'path': str(p), **report}


def live_nozzles(status, used_tools):
    result = {}
    for tool in used_tools:
        name = 'extruder' if tool == 0 else f'extruder{tool}'
        result[tool] = status.get(name, {}).get('nozzle_diameter')
    return result


@mcp.tool(annotations={'readOnlyHint':True})
def inspect_gcode(gcode: str) -> dict:
    """Audit exact bytes per physical tool and path feature: deposited plane spacing, width, volumetric demand, temperature and cooling. Returns blocking findings. Firmware macros and adhesion are not simulated."""
    return gcode_report(path_in_workspace(gcode))

@mcp.tool(annotations={'readOnlyHint':False,'destructiveHint':False})
async def slice_project(project: str, output_directory: str, preserve_support_tower: bool = True) -> dict:
    """Slice a self-contained 3MF in a private Xvfb display. Creates a new job directory, log, 3MF, G-code and review; never starts a print. Version-specific support tower workaround is recorded in manifest."""
    src=path_in_workspace(project);out=path_in_workspace(output_directory,False);out.mkdir(parents=True,exist_ok=False)
    job=out/'input.3mf';d=project_config(src);changes={}
    if preserve_support_tower and str(d.get('enable_prime_tower'))=='1' and str(d.get('enable_support'))=='1':
        # v2.3.2-fos.8.6.8 CLI excludes support-only filament from normalize_fdm_2.
        # Smooth mode preserves the requested tower and may add park moves.
        if str(d.get('timelapse_type'))!='1':changes['timelapse_type']='1'
    copy_project(src,job,changes)
    cli=os.environ.get('FORCASLICER_CLI','forcaslicer-headless')
    cmd=[cli,'--datadir',str(out/'data'),'--allow-newer-file','--arrange','0','--orient','0','--slice','0','--export-3mf','sliced.3mf','--outputdir',str(out),str(job)]
    with (out/'slice.log').open('wb') as log:
        proc=await asyncio.create_subprocess_exec(*cmd,stdout=log,stderr=asyncio.subprocess.STDOUT,start_new_session=True)
        try:code=await asyncio.wait_for(proc.wait(),1800)
        except BaseException:
            import signal
            try:os.killpg(proc.pid,signal.SIGTERM)
            except ProcessLookupError:pass
            await proc.wait();raise
    result={'input':str(src),'input_sha256':digest(src.read_bytes()),'effective_project_sha256':digest(job.read_bytes()),'workaround_changes':changes,'exit_code':code,'log':str(out/'slice.log'),'gcodes':[]}
    for p in sorted(out.glob('*.gcode')):
        r=gcode_report(p);(out/(p.stem+'-review.json')).write_text(json.dumps(r,ensure_ascii=False,indent=2))
        result['gcodes'].append({'path':str(p),'sha256':r['sha256'],'layers':r['total_layers'],'time':r['settings'].get('estimated printing time (normal mode)'),'prime_tower':r['settings'].get('enable_prime_tower'),'boundary_violations':len(r['out_of_bed_extrusion_lines']),'path_checks_passed':r['path_checks']['passed'],'path_error_count':r['path_checks']['error_count']})
    (out/'manifest.json').write_text(json.dumps(result,indent=2))
    if code or not result['gcodes']:raise ValueError(f'Slicing failed; inspect {out}/slice.log')
    if any(not g['path_checks_passed'] for g in result['gcodes']):
        raise ValueError(f'Generated G-code failed path audit; inspect {out}/manifest.json and review JSON')
    if str(d.get('enable_prime_tower'))=='1' and any(g['prime_tower']!='1' for g in result['gcodes']):raise ValueError('Requested prime tower missing; inspect review before use')
    return result

@mcp.tool(annotations={'readOnlyHint':True})
def printer_status() -> dict:
    """Read live printer status without desktop input."""
    return {'time_utc_epoch':time.time(),'status':state()}

@mcp.tool(annotations={'readOnlyHint':True})
def printer_camera() -> list:
    """Return the native U1 camera image and server file timestamp/age. Reject frames older than 15 seconds. Build plate may be outside the camera view."""
    files=request('/server/files/list?root=camera')['result'];entry=next(x for x in files if x['path']=='monitor.jpg')
    age=time.time()-entry['modified']
    if not -5<=age<=15:raise ValueError(f'Stale camera frame ({age:.1f}s)')
    b=request('/server/files/camera/monitor.jpg',raw=True)
    if not b.startswith(b'\xff\xd8'):raise ValueError('Camera did not return JPEG')
    return [json.dumps({'modified':entry['modified'],'age_seconds':age,'sha256':digest(b)}),Image(data=b,format='jpeg')]

def remote_gcode(filename):return request('/server/files/gcodes/'+urllib.parse.quote(filename,safe=''),raw=True)

def filename_ok(name):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*\.gcode',name):raise ValueError('Use a plain ASCII .gcode filename')

@mcp.tool(annotations={'readOnlyHint':False,'destructiveHint':False,'idempotentHint':True})
def upload_gcode(gcode: str, filename: str, expected_sha256: str) -> dict:
    """Upload authorized, reviewed G-code while idle. Refuse overwrites with different content; read back exact bytes and SHA-256. Does not start printing."""
    filename_ok(filename);p=path_in_workspace(gcode);b=p.read_bytes();sha=digest(b)
    if sha!=expected_sha256:raise ValueError('Local file changed since review')
    r=analyze_bytes(b)
    require_pass(r)
    with physical_lock():
        status=idle()
        require_pass(analyze_bytes(b, physical_nozzles=live_nozzles(status, r['used_deposition_tools'])))
        try:existing=remote_gcode(filename)
        except urllib.error.HTTPError as e:
            if e.code!=404:raise
            existing=None
        if existing is not None:
            if existing!=b:raise ValueError('Destination already contains different bytes')
        else:
            boundary='fos-'+uuid.uuid4().hex
            body=(f'--{boundary}\r\nContent-Disposition: form-data; name="root"\r\n\r\ngcodes\r\n--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode()+b+f'\r\n--{boundary}--\r\n'.encode())
            request('/server/files/upload',body,'multipart/form-data; boundary='+boundary)
        rb=remote_gcode(filename)
        if rb!=b:raise ValueError('Remote readback mismatch; do not print')
    return {'filename':filename,'sha256':sha,'bytes':len(b),'verified_exact_readback':True}

@mcp.tool(annotations={'readOnlyHint':False,'destructiveHint':True,'idempotentHint':False})
def start_print(filename: str, expected_sha256: str, build_plate_clear: bool) -> dict:
    """Start a specifically user-authorized print after the plate is confirmed clear, correct nozzles/materials verified, and exact remote bytes reviewed. Never retry after uncertain outcome: query printer_status."""
    filename_ok(filename)
    if not build_plate_clear:raise ValueError('Confirm the build plate is clear and seated')
    with physical_lock():
        status=idle();b=remote_gcode(filename)
        if digest(b)!=expected_sha256:raise ValueError('Remote file differs from reviewed hash')
        report=analyze_bytes(b)
        require_pass(report)
        require_pass(analyze_bytes(b, physical_nozzles=live_nozzles(status, report['used_deposition_tools'])))
        # Audit may take seconds. Refresh hardware/idle state before committing.
        current = idle()
        if live_nozzles(current, report['used_deposition_tools']) != live_nozzles(status, report['used_deposition_tools']):
            raise ValueError('Nozzle configuration changed during path audit')
        # Re-read after analysis; never start bytes replaced during the audit.
        if remote_gcode(filename)!=b:raise ValueError('Remote file changed during path audit')
        result=request('/printer/print/start',json.dumps({'filename':filename}).encode())
    return {'request_result':result,'status':state()}

@mcp.tool(annotations={'readOnlyHint':False,'destructiveHint':False,'idempotentHint':True})
def pause_print(expected_filename: str) -> dict:
    """Pause only the exact active job when user-authorized or when an observed damaging print fault warrants it."""
    with physical_lock():
        s=state()['print_stats']
        if s['filename']!=expected_filename or s['state']!='printing':raise ValueError('Expected file is not actively printing')
        return request('/printer/print/pause',b'{}')

if __name__=='__main__':mcp.run(transport='stdio')
