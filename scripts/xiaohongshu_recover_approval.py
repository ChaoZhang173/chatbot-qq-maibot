"""OPERATOR ONLY, run on the core host: honour an owner approval that was already given explicitly but missed.

It reads the stored source event itself (never caller-supplied text), validates the exact owner / private chat / current
account / age / draft version and hash / syntax, and approves only that one draft version, once. Repeating it is
harmless. It is not an executor operation and nothing calls it automatically. It prints a bounded JSON result.

  python scripts/xiaohongshu_recover_approval.py --core-root /srv/maibot/unified --event-id evt:<64 hex> --draft-id <ID> --version <N>
"""
import argparse
import json
import sys
from pathlib import Path


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    p.add_argument('--core-root', default='/srv/maibot/unified', help='directory holding the core state.sqlite and policy.json')
    p.add_argument('--extensions-dir', default=str(Path(__file__).resolve().parents[1] / 'extensions'), help='directory containing maibot_unified_core')
    p.add_argument('--event-id', required=True)
    p.add_argument('--draft-id', required=True)
    p.add_argument('--version', required=True, type=int)
    args = p.parse_args(argv)
    sys.path.insert(0, args.extensions_dir)
    from maibot_unified_core.xiaohongshu_review import recover_explicit_approval
    result = recover_explicit_approval(args.core_root, args.event_id, args.draft_id, args.version)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get('ok') else 1


if __name__ == '__main__':
    sys.exit(main())
