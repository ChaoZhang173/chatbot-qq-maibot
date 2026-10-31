"""Installation-owned allowlist checked before decoding media and sending."""
import json
from pathlib import Path
SCOPE_PATH=Path(__file__).with_name('scope.json')

def allowed(group_id,user_id,account_id):
    try:
        c=json.loads(SCOPE_PATH.read_text())
        if str(account_id or '')!=c['account']:return False
        group=str(group_id or '')
        return group in c['groups'] if group else str(user_id or '') in c['private']
    except (OSError,ValueError,KeyError,TypeError):return False
