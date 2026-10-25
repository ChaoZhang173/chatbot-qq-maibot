"""Run the self-contained current snapshot tests in separate import namespaces."""
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
env = dict(os.environ)
env['PYTHONDONTWRITEBYTECODE'] = '1'
env['PYTHONPATH'] = os.pathsep.join([str(ROOT / 'extensions'), str(ROOT / 'extensions/paidtasks'), env.get('PYTHONPATH', '')])
suites = [('tests', 'test_*.py'), ('extensions/tests_dafeiyu', 'test_*.py'),
          ('extensions/tests_phase2', 'test_api.py'), ('extensions/tests_phase2', 'test_runtime.py'),
          ('extensions/tests_phase2', 'test_reply_interest.py'), ('extensions/paidtasks/tests', 'test_*.py')]
for directory, pattern in suites:
    print(f'Running {directory}/{pattern}', flush=True)
    subprocess.run([sys.executable, '-m', 'unittest', 'discover', '-s', directory, '-p', pattern],
                   cwd=ROOT, env=env, check=True)
subprocess.run([sys.executable, 'scripts/check_public_files.py'], cwd=ROOT, env=env, check=True)
