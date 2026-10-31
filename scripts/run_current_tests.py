"""Run the self-contained current snapshot tests in separate import namespaces."""
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
env = dict(os.environ)
env['PYTHONDONTWRITEBYTECODE'] = '1'
env['PYTHONPATH'] = os.pathsep.join([str(ROOT / 'extensions'), str(ROOT / 'extensions/paidtasks'), str(ROOT / 'extensions/qq_channel'), str(ROOT / 'extensions/tests_dafeiyu'), env.get('PYTHONPATH', '')])
suites = [('tests', 'test_*.py'), ('extensions/tests_dafeiyu', 'test_*.py'),
          ('extensions/tests_phase2', 'test_api.py'), ('extensions/tests_phase2', 'test_runtime.py'),
          ('extensions/tests_phase2', 'test_reply_interest.py'), ('extensions/tests_current', 'test_*.py'), ('extensions/paidtasks/tests', 'test_*.py')]
# 候选 Token 优化的新增测试。其中加载 Host 源码的部分需要 DAFEIYU_HOST_SOURCE_ROOT 指向
# “旧 host-current 补丁 + 候选 token-optimizations 补丁”都已应用的 Host src 目录；
# 未设置时这些测试会被明确标为 skipped（见输出），不是通过。其余（P0 费用、P2/P3 分层）不需要 Host。
if not os.environ.get('DAFEIYU_HOST_SOURCE_ROOT'):
    print('NOTE: DAFEIYU_HOST_SOURCE_ROOT is not set; Host-dependent token tests will be reported as skipped, '
          'not as passed.', flush=True)
suites.append(('extensions/tests_token_optimizations', 'test_*.py'))
for directory, pattern in suites:
    print(f'Running {directory}/{pattern}', flush=True)
    subprocess.run([sys.executable, '-m', 'unittest', 'discover', '-s', directory, '-p', pattern],
                   cwd=ROOT, env=env, check=True)
subprocess.run([sys.executable, 'scripts/check_public_files.py'], cwd=ROOT, env=env, check=True)
