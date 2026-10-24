"""Local Unix-socket administrator; never exposes a TCP management port."""
import argparse,json,socket
from pathlib import Path

def call(data,path='/srv/maibot/unified/core.sock'):
    with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as s:
        s.settimeout(110);s.connect(path);s.sendall(json.dumps(data,ensure_ascii=False).encode()+b'\n')
        f=s.makefile('rb');raw=f.readline(3*1024*1024);return json.loads(raw)

def main():
    p=argparse.ArgumentParser();p.add_argument('op',choices=['status','activities','context','learn','reflect','rollback','memory_admin','policy_reload'])
    p.add_argument('--platform');p.add_argument('--session');p.add_argument('--query',default='');p.add_argument('--version',type=int);p.add_argument('--args-file',type=Path);a=p.parse_args()
    data={'op':a.op}
    if a.args_file:data.update(json.loads(a.args_file.read_text()))
    if a.platform:data['platform']=a.platform
    if a.session:data['conversation_id']=a.session
    if a.query:data['query']=a.query
    if a.version is not None:data['version']=a.version
    print(json.dumps(call(data),ensure_ascii=False,indent=2))
if __name__=='__main__':main()
