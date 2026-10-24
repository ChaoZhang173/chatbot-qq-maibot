"""Fetch the OFL font from its publisher and verify the reviewed digest."""
import hashlib
from pathlib import Path
import urllib.request

ROOT=Path(__file__).resolve().parents[1]/'assets/fonts'
FILES={
    'NotoSansCJKsc-Regular.otf':('OTF/SimplifiedChinese/NotoSansCJKsc-Regular.otf','2c76254f6fc379fddfce0a7e84fb5385bb135d3e399294f6eeb6680d0365b74b'),
    'LICENSE':('LICENSE','6a73f9541c2de74158c0e7cf6b0a58ef774f5a780bf191f2d7ec9cc53efe2bf2')}
ROOT.mkdir(parents=True,exist_ok=True)
for name,(remote,expected) in FILES.items():
    target=ROOT/name
    if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest()==expected:continue
    with urllib.request.urlopen('https://raw.githubusercontent.com/notofonts/noto-cjk/main/Sans/'+remote,timeout=30) as response:
        data=response.read(20000000)
    if hashlib.sha256(data).hexdigest()!=expected:raise RuntimeError('font_source_changed_review_required')
    target.write_bytes(data)
print('font_and_license_verified')
