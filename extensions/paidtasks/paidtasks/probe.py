"""Read-only site/API evidence; no orders, credentials, request signatures or IDs in output."""
import argparse
import json
import os
from pathlib import Path
import time
import urllib.error
import urllib.request
from .afdian import AfdianClient, ProviderError

def run(envfile=None):
    if envfile:
        for line in Path(envfile).read_text().splitlines():
            if line and not line.startswith('#'):
                key,value=line.split('=',1)
                if key in ('AFDIAN_CREATOR_USER_ID','AFDIAN_API_TOKEN'):os.environ[key]=value
    evidence={'checked_at':time.time(),'real_payment_verified':False,'sites':{}}
    for url in ('https://afdian.com/','https://afdian.com/dashboard/dev','https://guide.afdian.com/creator/developer'):
        try:
            with urllib.request.urlopen(url,timeout=12) as response:
                evidence['sites'][url]={'http_status':response.status}
        except urllib.error.HTTPError as exc:evidence['sites'][url]={'http_status':exc.code}
        except Exception:evidence['sites'][url]={'error':'transport_unavailable'}
    try:
        client=AfdianClient(attempts=1)
        evidence['ping']=client.ping()
        rows,pages=client.list_orders(1)
        evidence['query']={'ok':True,'row_count':len(rows),'total_pages':pages}
    except (ProviderError,ValueError) as exc:
        evidence['api']={'ok':False,'reason':str(exc)}
    return evidence

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--env');p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();result=run(a.env);a.output.write_text(json.dumps(result,indent=2));print(json.dumps(result))
