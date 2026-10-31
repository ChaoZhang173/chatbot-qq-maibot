"""Read-only live sampler. Its own ledger never mutates the Bilibili send queue."""
from __future__ import annotations
import argparse, base64, fcntl, hashlib, json, os, re, socket, sqlite3, subprocess, tempfile, time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit
from .api import Bili
from .core import TZ, load_config, active, PlatformError
from .live_media import policy, OBSERVATION_PATH
from .unified import request

def allowed_url(url):
    p = urlsplit(url)
    host = p.hostname or ''
    if p.scheme not in ('https','http') or p.username or p.password or p.port not in (None,80,443): return False
    if not any(host.endswith('.'+suffix) for suffix in ('bilivideo.com','bilivideo.cn','bilivideo.net','bilibili.com')): return False
    try:
        import ipaddress
        addresses=socket.getaddrinfo(host,p.port or 443,type=socket.SOCK_STREAM)
        return bool(addresses) and all(ipaddress.ip_address(x[4][0]).is_global for x in addresses)
    except (OSError,ValueError): return False

def stream_url(api, room):
    data = api.request('/xlive/web-room/v2/index/getRoomPlayInfo', live=True,
                       params={'room_id':room,'protocol':'0','format':'0','codec':'0','qn':80,'platform':'web','ptype':8})
    if data.get('live_status') != 1: raise ValueError('room_offline')
    for stream in ((data.get('playurl_info') or {}).get('playurl') or {}).get('stream',[]):
        for fmt in stream.get('format',[]):
            if fmt.get('format_name') != 'flv': continue
            for codec in fmt.get('codec',[]):
                if codec.get('codec_name') not in ('avc','h264'): continue
                for item in codec.get('url_info',[]):
                    url = str(item.get('host',''))+str(codec.get('base_url',''))+str(item.get('extra',''))
                    if allowed_url(url): return url
    raise ValueError('no_supported_live_stream')

def capture(url, seconds=12):
    if not allowed_url(url): raise ValueError('untrusted_stream_host')
    start = time.time()
    with tempfile.TemporaryDirectory(prefix='dafeiyu-live-') as name:
        root = Path(name)
        command = ['ffmpeg','-hide_banner','-nostdin','-loglevel','error','-threads','1',
                   '-protocol_whitelist','http,https,tcp,tls','-rw_timeout','8000000',
                   '-user_agent','Mozilla/5.0','-headers','Referer: https://live.bilibili.com/\r\n',
                   '-i',url,'-t',str(seconds),'-map','0:v:0','-vf','fps=1/6,scale=640:-2',
                   '-threads','1','-frames:v','2','-q:v','6',str(root/'frame-%02d.jpg'),
                   '-t',str(seconds),'-map','0:a:0?','-vn','-ac','1','-ar','16000',
                   '-c:a','pcm_s16le','-fs','520000',str(root/'audio.wav')]
        try:
            done = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=24)
        except subprocess.TimeoutExpired: raise ValueError('capture_timeout') from None
        # Never put signed CDN URLs or codec stderr into logs or durable state.
        frames = []
        for i, path in enumerate(sorted(root.glob('frame-*.jpg'))[:2]):
            if path.stat().st_size > 200000: continue
            frames.append({'offset':i*6+3, 'jpeg_base64':base64.b64encode(path.read_bytes()).decode()})
        wav = root/'audio.wav'
        audio = base64.b64encode(wav.read_bytes()).decode() if wav.is_file() and 44 < wav.stat().st_size <= 520000 else ''
        if not frames and not audio: raise ValueError('live_decode_failed')
        return {'started_at':start, 'ended_at':time.time(), 'frames':frames, 'audio_base64':audio}

def read_visit(state, config, now=None):
    now = time.time() if now is None else now
    if config.get('paused') or not config.get('live_chat',{}).get('enabled') or (Path(state)/'PAUSE').exists() or not active(config,now): return None
    with sqlite3.connect('file:'+str(Path(state)/'state.sqlite')+'?mode=ro',uri=True) as db:
        rows = {k:json.loads(v) for k,v in db.execute("SELECT k,v FROM meta WHERE k IN ('live_visit','pause_reason','last_live')")}
    visit = rows.get('live_visit')
    if rows.get('pause_reason') or not visit or visit.get('ends',0) <= now: return None
    if now-(rows.get('last_live') or {}).get('at',0)>75: return None
    return visit

def publish(value, path=OBSERVATION_PATH):
    path = Path(path)
    raw = json.dumps(value,ensure_ascii=False).encode()
    if len(raw)>16000: raise ValueError('observation_too_large')
    fd, temp = tempfile.mkstemp(prefix='.live-',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as dst: dst.write(raw); dst.flush(); os.fsync(dst.fileno())
        os.replace(temp,path)
    finally:
        if os.path.exists(temp): os.unlink(temp)

def budget_window(now):
    d = datetime.fromtimestamp(now,TZ)
    if d.hour>=23: return d.date().isoformat()+':night', True
    if d.hour<9:
        from datetime import timedelta
        return (d.date()-timedelta(days=1)).isoformat()+':night', True
    return d.date().isoformat()+':day', False

def run(args):
    os.umask(0o077)
    root = Path(args.observer_state); root.mkdir(parents=True,exist_ok=True)
    lock = (root/'instance.lock').open('a'); fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    db = sqlite3.connect(root/'usage.sqlite',isolation_level=None)
    db.execute('CREATE TABLE IF NOT EXISTS samples(id TEXT PRIMARY KEY,window TEXT,created REAL,status TEXT)')
    api = Bili(args.credentials); due = 0; previous_status = ''
    while True:
        now = time.time()
        try:
            c=load_config(args.config); p=policy(c); visit=read_visit(args.state,c)
            if not p['enabled'] or not visit:
                if previous_status!='inactive': publish({'status':'inactive','at':now})
                previous_status='inactive'; time.sleep(3); continue
            if now<due: time.sleep(min(3,due-now)); continue
            window, night = budget_window(now)
            limit = p['night_samples'] if night else p['daily_samples']
            if db.execute('SELECT count(*) FROM samples WHERE window=?',(window,)).fetchone()[0]>=limit:
                publish({'status':'sample_budget_exhausted','at':now}); due=now+60; continue
            room = visit['room']['room_id']; vid = visit['id']
            due = now+p['interval_seconds']
            sid = hashlib.sha256((vid+':'+str(now)).encode()).hexdigest()
            db.execute('INSERT INTO samples VALUES(?,?,?,?)',(sid,window,now,'reserved'))
            db.execute('DELETE FROM samples WHERE created<?',(now-7*86400,))
            url = stream_url(api,room)
            sample = capture(url,p['sample_seconds']); del url
            current = read_visit(args.state,load_config(args.config))
            if not current or current['id']!=vid or not policy(load_config(args.config))['enabled']: db.execute('UPDATE samples SET status=? WHERE id=?',('visit_changed',sid)); continue
            response = request({'op':'live_observe','room_id':room,'visit_id':vid,**sample},timeout=40)
            current = read_visit(args.state,load_config(args.config))
            if not current or current['id']!=vid or not policy(load_config(args.config))['enabled']: db.execute('UPDATE samples SET status=? WHERE id=?',('visit_changed',sid)); continue
            observation = response['observation']; publish(observation)
            db.execute('UPDATE samples SET status=? WHERE id=?',('observed',sid)); previous_status='observed'
        except KeyboardInterrupt: break
        except Exception as exc:
            reason = str(exc) if isinstance(exc,ValueError) and re.fullmatch(r'[a-z_]{1,50}',str(exc)) else type(exc).__name__
            publish({'status':'unavailable','reason':reason,'at':time.time()})
            print(json.dumps({'event':'live_observer','status':'unavailable','reason':reason}),flush=True)
            due = time.time()+(300 if isinstance(exc,PlatformError) else 90)
            previous_status='unavailable'
        time.sleep(1)

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--config',default='/srv/bilibot/private/config.json')
    parser.add_argument('--credentials',default='/srv/bilibot/private/bili-credentials.json')
    parser.add_argument('--state',default='/srv/bilibot/state')
    parser.add_argument('--observer-state',default='/srv/bilibot/live-observer')
    run(parser.parse_args())
