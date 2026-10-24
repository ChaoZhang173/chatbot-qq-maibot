"""Recoverable bounded pagination and durable verification queue."""
import json
from .afdian import normalize, ProviderError

class Reconciler:
    def __init__(self, store, client):
        self.s, self.client = store, client

    def scan(self):
        s=self.s
        page=int(s.get("scan_page",1));baseline=not s.get("bootstrap_complete",False)
        try:
            for _ in range(s.policy.get("scan_pages_per_tick",3)):
                rows,total=self.client.list_orders(page)
                for raw in rows:
                    try:
                        evidence=normalize(self.client.creator,raw,s.now())
                        s.apply_payment(evidence,historical=baseline)
                    except ValueError:
                        # Malformed payment remains visible as a count and bounded review item.
                        trade=raw.get("out_trade_no") if isinstance(raw,dict) else None
                        if isinstance(trade,str):
                            try:s.payment_hint(trade,"malformed_page")
                            except ValueError:pass
                        s.put("schema_review_count",s.get("schema_review_count",0)+1)
                if page>=max(total,1):
                    with s.tx():
                        s.put("bootstrap_complete",True);s.put("scan_page",1)
                        s.put("last_complete_scan",s.now());s.put("api_failures",0)
                    return {"complete":True,"pages":page,"baseline":baseline}
                page+=1;s.put("scan_page",page)
            # Retain a durable page cursor; next cycle overlaps one page.
            s.put("scan_page",max(1,page-1));s.put("api_failures",0)
            return {"complete":False,"next_page":max(1,page-1)}
        except (ProviderError,ValueError):
            s.put("api_failures",s.get("api_failures",0)+1)
            return {"complete":False,"error":"official_query_unavailable"}

    def pending(self):
        s=self.s
        with s.tx():
            row=s.db.execute("SELECT * FROM inbound WHERE status='pending' AND next_attempt<=? ORDER BY created LIMIT 1",(s.now(),)).fetchone()
            if not row:return False
            s.db.execute("UPDATE inbound SET status='checking',attempts=attempts+1 WHERE event_key=?",(row['event_key'],))
        try:
            evidence=self.client.query_order(json.loads(row['payload'])['trade_no'])
            s.apply_payment(evidence,historical=not s.get('bootstrap_complete',False))
            with s.tx():
                s.db.execute("UPDATE inbound SET status='done',next_attempt=?,reason='' WHERE event_key=?",(s.now()+60,row['event_key']))
                s.put('api_failures',0)
        except (ProviderError,ValueError):
            attempts=row['attempts']+1
            with s.tx():
                s.db.execute("UPDATE inbound SET status=?,next_attempt=?,reason='official_verification_unavailable' WHERE event_key=?",
                    ('review' if attempts>=8 else 'pending',s.now()+min(3600,30*2**attempts),row['event_key']))
                s.put('api_failures',s.get('api_failures',0)+1)
        return True
