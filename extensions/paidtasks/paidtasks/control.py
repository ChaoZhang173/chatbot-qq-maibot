"""Protected local operator CLI. Use a mode-600 JSON file for private requests."""
import argparse
import json
from pathlib import Path
import sys
from .runtime import rpc

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--socket',default='/srv/maibot/paid/paid.sock')
    parser.add_argument('--request-file',type=Path)
    parser.add_argument('operation',nargs='?',default='status',choices=('status','review','owner_probe'))
    args=parser.parse_args()
    request=json.loads(args.request_file.read_text()) if args.request_file else {'op':args.operation}
    try:
        result=rpc(request,path=args.socket,timeout=60)
    except (OSError,ValueError):
        print(json.dumps({'ok':False,'error':'local_service_unavailable_or_request_rejected'}))
        return 1
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0

if __name__=='__main__':sys.exit(main())
