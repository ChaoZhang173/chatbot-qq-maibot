"""Allowlisted release builder. Private runtime files are never candidates."""
import ast
import hashlib
import json
from pathlib import Path
import re
import zipfile

ROOT=Path(__file__).resolve().parents[1]
top=['README.md','BASELINE.zh.md','RUNBOOK.zh.md','ACCEPTANCE.zh.md','REPORT.zh.md',
     'CAPABILITIES.json','config.example.json','.gitignore','商品说明建议.txt','动态草稿_未发布.txt']
files=[ROOT/name for name in top]
for folder,pattern in [('paidtasks','*.py'),('integration','*.py'),('integration','*.patch'),
        ('deploy','*.py'),('deploy','*.service'),('deploy','*.example'),('tests','*.py'),
        ('licenses','*'),('assets/fonts','*')]:
    files.extend(p for p in (ROOT/folder).glob(pattern) if p.is_file())
files.extend(ROOT/'scripts'/name for name in ['build_integration.py','fetch_font.py','package_release.py'])
files.extend(ROOT/'evidence'/name for name in ['server-parity.json','afdian-live-probe.json',
    'afdian-live-probe-final.json','browser-probe.json','local-tests.txt','regression-tests.txt',
    'local-render-resource.json','server-sdk.json','deployment.json','sample-chart.png','sample-formula.png'])
files.extend(p for p in (ROOT/'build/integration').rglob('*') if p.is_file())
files=sorted(set(files))

# Compare only against the explicitly supplied protected API environment. No
# credential discovery, keychain access, transcript scraping, or value printing.
known=[]
envfile=ROOT/'private/afdian.env'
if envfile.exists():
    for line in envfile.read_text().splitlines():
        if line.startswith(('AFDIAN_API_TOKEN=','AFDIAN_CREATOR_USER_ID=')):
            value=line.split('=',1)[1]
            if value:known.append(value.encode())
patterns=[re.compile(rb'-----BEGIN '+rb'(?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----'),
    re.compile(rb'\b(?:sk-|gh[pousr]_|github_pat_)[A-Za-z0-9_-]{20,}'),
    re.compile(rb'https?://[^\s/:]+:[^\s/@]+@')]
manifest={};failures=[]
for path in files:
    rel=path.relative_to(ROOT).as_posix();raw=path.read_bytes()
    if path.is_symlink() or 'private' in path.relative_to(ROOT).parts or path.suffix in ('.env','.sqlite','.db','.key','.pem'):
        failures.append({'file':rel,'rule':'private_runtime_file'})
    if any(value in raw for value in known):failures.append({'file':rel,'rule':'supplied_credential_or_identity'})
    if path.suffix not in ('.otf','.png') and any(pattern.search(raw) for pattern in patterns):
        failures.append({'file':rel,'rule':'credential_pattern'})
    if path.suffix=='.py':ast.parse(raw)
    manifest[rel]={'sha256':hashlib.sha256(raw).hexdigest(),'bytes':len(raw)}
if failures:
    print(json.dumps({'safe_to_publish':False,'findings':failures}));raise SystemExit(1)

result={'files_scanned':len(files),'known_credentials_absent':True,'credential_patterns_absent':True,
        'allowlist_only':True,'private_runtime_included':False,'real_payment_verified':False}
scan=ROOT/'evidence/sanitization.json';scan.write_text(json.dumps(result,indent=2)+'\n');files.append(scan)
manifest_path=ROOT/'RELEASE_MANIFEST.json';manifest_path.write_text(json.dumps(manifest,indent=2)+'\n');files.append(manifest_path)
release=ROOT/'release';release.mkdir(exist_ok=True)
archive=release/'QQ-Bilibili-PaidTasks-20261024.zip'
with zipfile.ZipFile(archive,'w',compression=zipfile.ZIP_DEFLATED) as z:
    for path in files:z.write(path,'QQ-Bilibili-PaidTasks-20261024/'+path.relative_to(ROOT).as_posix())

public=[]
for path in files:
    rel=path.relative_to(ROOT)
    if path.suffix in ('.png','.otf') or rel.parts[0]=='build' or path.name=='RELEASE_MANIFEST.json':continue
    public.append({'path':'extensions/paidtasks/'+rel.as_posix(),'mode':'100644','type':'blob','content':path.read_text()})
(ROOT/'build/publish.json').write_text(json.dumps(public,ensure_ascii=False))
print(json.dumps({**result,'public_source_files':len(public),'archive_bytes':archive.stat().st_size}))
