"""Command line for the Xiaohongshu executor. Output is JSON; secrets are never printed."""
import argparse, json, os, signal, sqlite3, sys, threading
from pathlib import Path
from . import config as configuration
from .core_client import CoreClient
from .http_client import McpClient
from .media import PublishableMedia
from .runner import Executor
from .store import AlreadyRunning, Store, beijing_day

DEFAULT_CONFIG = '/etc/dafeiyu-xhs/config.json'
TARGET_TOKEN_ENV = 'XHS_TARGET_TOKEN'


def parser():
    p = argparse.ArgumentParser(prog='xiaohongshu', description='Dafeiyu Xiaohongshu executor (default: everything off, dry-run on).')
    p.add_argument('--config', default=os.environ.get('XHS_CONFIG', DEFAULT_CONFIG), help='JSON config path')
    sub = p.add_subparsers(dest='command', required=True)
    sub.add_parser('run', help='run the scheduler loop (single instance)')
    sub.add_parser('status', help='local counters and pause state; works while the service runs')
    sub.add_parser('doctor', help='check service reachability, login and UID binding')
    q = sub.add_parser('qrcode', help='save the login QR image to a file for a human to scan')
    q.add_argument('--out', required=True, help='absolute output path (task file, mode 0600)')
    sub.add_parser('pause', help='create the PAUSE file; the running service stops all writes')
    sub.add_parser('resume', help='remove PAUSE and clear pause/backoff at the next tick or start')
    for name, text in (('poll-once', 'one notification poll'), ('autonomy-once', 'one browse/post consideration')):
        s = sub.add_parser(name, help=text)
        s.add_argument('--dry-run', action='store_true', help='read and plan only; no platform write, no dedupe mark')
    a = sub.choices['autonomy-once']
    a.add_argument('--force', action='store_true', help='ignore the interval and active hours (still honors switches and quotas)')
    pub = sub.add_parser('publish', help='manual single image post (acceptance)')
    pub.add_argument('--title', required=True)
    pub.add_argument('--content', required=True)
    pub.add_argument('--image-id', required=True, help='id from the publishable manifest')
    pub.add_argument('--tag', action='append', default=[])
    pub.add_argument('--visibility', choices=['公开可见', '仅自己可见'], default=None,
                     help='audience of the draft; autonomous posts always use the configured visibility')
    pub.add_argument('--intro', action='store_true', help='mark the draft as the first self-introduction post')
    pub.add_argument('--self-only-acceptance', action='store_true',
                     help='send a PRIVATE (仅自己可见) acceptance note directly, without owner review; can never be public')
    sub.add_parser('deliver-once', help='claim one owner-approved draft and send exactly that content (the service does this too)')
    pub.add_argument('--dry-run', action='store_true')
    for name in ('comment', 'reply'):
        s = sub.add_parser(name, help='manual single %s; xsec_token comes from $%s' % (name, TARGET_TOKEN_ENV))
        s.add_argument('--feed-id', required=True)
        s.add_argument('--text', required=True)
        s.add_argument('--self-only-acceptance', action='store_true',
                       help="direct send (no owner review) ONLY on a note this executor created as self-only and read back exactly; "
                            'allows replying to our own comment there; recorded self_only. Otherwise refused')
        s.add_argument('--dry-run', action='store_true')
        if name == 'reply':
            s.add_argument('--comment-id', required=True, help='explicit target comment id; user_id is never used')
    return p


def build(cfg, environ=None, *, http=None, core=None):
    store = Store(cfg['state_dir'])
    try:
        limits = cfg['limits']
        if http is None:
            http = McpClient(cfg['service_url'], configuration.load_token(cfg, environ), timeout=limits['http_timeout'],
                             write_timeout=limits['write_timeout'], max_bytes=limits['max_response_bytes'])
        core = core or CoreClient(cfg['core_socket'])
        executor = Executor(cfg, http, core, store, PublishableMedia(cfg['media_dir']))
        executor.apply_requests()
        return executor
    except BaseException:
        store.close()
        raise


def snapshot(state_dir):
    """Read-only view for `status` while the service owns the write lock."""
    db_path = Path(state_dir) / 'state.sqlite'
    if not db_path.exists():
        return {'state': 'uninitialized'}
    db = sqlite3.connect('file:%s?mode=ro' % db_path, uri=True, timeout=5)
    try:
        day = beijing_day(__import__('time').time())
        counts = {k: db.execute('SELECT count(*) FROM actions WHERE kind=? AND day=?', (k, day)).fetchone()[0] for k in ('post', 'comment', 'reply')}
        meta = dict(db.execute("SELECT k,v FROM meta WHERE k IN ('paused','backoff_until','baseline_at')").fetchall())
        return {'today': counts, 'paused': json.loads(meta.get('paused', '""')) or (Path(state_dir) / 'PAUSE').exists(),
                'backoff_until': json.loads(meta.get('backoff_until', '0')), 'baseline_at': json.loads(meta['baseline_at']) if 'baseline_at' in meta else None,
                'unknown_actions': db.execute("SELECT count(*) FROM actions WHERE status='unknown'").fetchone()[0],
                'pending_verification': db.execute("SELECT count(*) FROM actions WHERE status='accepted' AND verification!='own_page_exact'").fetchone()[0]}
    finally:
        db.close()


def main(argv=None, environ=None, factory=None, out=print):
    args = parser().parse_args(argv)
    environ = os.environ if environ is None else environ
    try:
        cfg = configuration.load(args.config)
    except configuration.ConfigError as exc:
        out(json.dumps({'error': str(exc)}))
        return 2
    root = Path(cfg['state_dir'])
    if args.command in ('pause', 'resume'):
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if args.command == 'pause':
            (root / 'PAUSE').write_text('paused by operator\n')
        else:
            (root / 'PAUSE').unlink(missing_ok=True)
            (root / 'RESUME').write_text('resume requested\n')
        out(json.dumps({'status': args.command + '_requested'}))
        return 0
    try:
        executor = (factory or build)(cfg, environ)
    except AlreadyRunning:
        if args.command == 'status':
            out(json.dumps(snapshot(cfg['state_dir']), ensure_ascii=False))
            return 0
        out(json.dumps({'error': 'executor_already_running', 'hint': 'stop the service before this command, or use status/pause/resume'}))
        return 3
    except configuration.ConfigError as exc:
        out(json.dumps({'error': str(exc)}))
        return 2
    try:
        result = dispatch(executor, args, cfg, environ)
    finally:
        executor.store.close()
    if result is not None:
        out(json.dumps(result, ensure_ascii=False))
    return 0


def dispatch(executor, args, cfg, environ):
    cmd = args.command
    if cmd == 'run':
        stop = threading.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: stop.set())
        executor.run(stop, log=lambda r: print(json.dumps(r, ensure_ascii=False), flush=True))
        return None
    if cmd == 'status':
        return executor.status()
    if cmd == 'doctor':
        return executor.doctor(os.path.exists(cfg['core_socket']))
    if cmd == 'qrcode':
        return executor.qrcode(args.out)
    dry = args.dry_run or cfg['dry_run']
    if cmd == 'poll-once':
        return executor.poll_notifications(dry)
    if cmd == 'autonomy-once':
        return executor.autonomy_step(dry, force=args.force)
    if cmd == 'deliver-once':
        return executor.deliver_approved()
    if cmd == 'publish':
        return executor.publish(args.title, args.content, args.image_id, args.tag, dry_run=dry, manual=True, visibility=args.visibility,
                                purpose='intro' if args.intro else 'general', self_acceptance=args.self_only_acceptance)
    token = environ.get(TARGET_TOKEN_ENV, '')
    if cmd == 'comment':
        return executor.manual_comment(args.feed_id, token, args.text, dry_run=dry, self_acceptance=args.self_only_acceptance)
    return executor.manual_reply(args.feed_id, token, args.comment_id, args.text, dry_run=dry, self_acceptance=args.self_only_acceptance)


if __name__ == '__main__':
    sys.exit(main())
