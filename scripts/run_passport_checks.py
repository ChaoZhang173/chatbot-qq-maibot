"""Run packaged voice/web regressions without live providers or account actions."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', PYTHONPATH=str(ROOT / 'extensions'))
env['PASSPORT_BACKEND_PATCH'] = str(ROOT / 'extensions/maibot_unified_core')
env['PASSPORT_FIRMWARE_SESSION_H'] = str(ROOT / 'contracts/passport/pp_session.h')
env['PASSPORT_FIRMWARE_VECTORS'] = str(ROOT / 'contracts/passport/protocol_vectors.txt')

# Extract the capability source from the published complete patch for the budget-contract check.
# No source is executed here; the test reads its literal timeout bound.
with tempfile.TemporaryDirectory(prefix='passport-check-') as temporary:
    target = Path(temporary) / 'src/plugin_runtime/capabilities/unified_voice.py'
    target.parent.mkdir(parents=True)
    patch = (ROOT / 'patches/maibot-host-current.patch').read_text().splitlines(True)
    selected, lines = False, []
    for line in patch:
        if line.startswith('+++ '):
            selected = line.rstrip() == '+++ b/src/plugin_runtime/capabilities/unified_voice.py'
        elif selected and not line.startswith(('--- ', '@@ ', '\\')):
            if line.startswith(('+', ' ')):
                lines.append(line[1:])
    if not lines:
        raise SystemExit('Host voice capability missing from the complete patch')
    target.write_text(''.join(lines))
    env['PASSPORT_HOST_PATCH'] = temporary
    for package in ('tests_passport_backend', 'tests_passport_gateway'):
        subprocess.run([sys.executable, '-m', 'unittest', 'discover', '-s', 'extensions/' + package,
                        '-t', 'extensions'], cwd=ROOT, env=env, check=True)
