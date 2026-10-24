"""Transactional business ledger. All money is integer fen or micro-CNY.

Only trusted channel adapters call this service. External text never selects
administrative authority, payment state, prices or notification recipients.
"""
import contextlib
import functools
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from .afdian import PaymentEvidence, identifier

def js(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()

def identity(value):
    if not isinstance(value, dict) or value.get("channel") not in ("qq", "bilibili_manual"):
        raise ValueError("identity_channel")
    return js({k: identifier(value.get(k)) for k in ("channel", "account", "conversation", "actor")})

def day(now):
    return datetime.fromtimestamp(now, ZoneInfo("Asia/Shanghai")).date().isoformat()

def locked(method):
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self.lock:
            return method(self, *args, **kwargs)
    return wrapper

class Store:
    def __init__(self, root, policy, now=time.time):
        self.root = Path(root)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root.chmod(0o700)
        self.policy, self.now = policy, now
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.root / "state.sqlite", timeout=10,
            isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
        PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL; PRAGMA foreign_keys=ON;
        PRAGMA busy_timeout=10000; PRAGMA user_version=1;
        CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY,v TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS quotes(
          id TEXT NOT NULL,version INTEGER NOT NULL,customer TEXT NOT NULL,snapshot TEXT NOT NULL,
          binding_digest TEXT UNIQUE NOT NULL,created REAL NOT NULL,expires REAL NOT NULL,
          PRIMARY KEY(id,version));
        CREATE TABLE IF NOT EXISTS orders(
          id TEXT PRIMARY KEY,version INTEGER NOT NULL,status TEXT NOT NULL,payment TEXT NOT NULL,
          execution TEXT NOT NULL,accepted REAL,confirmed REAL,reason TEXT NOT NULL DEFAULT '',
          FOREIGN KEY(id,version) REFERENCES quotes(id,version));
        CREATE TABLE IF NOT EXISTS payments(
          provider TEXT NOT NULL,creator TEXT NOT NULL,trade_no TEXT NOT NULL,evidence TEXT NOT NULL,
          provider_paid INTEGER NOT NULL,historical INTEGER NOT NULL,allocation TEXT NOT NULL,
          order_id TEXT UNIQUE,reason TEXT NOT NULL,first_seen REAL NOT NULL,updated REAL NOT NULL,
          PRIMARY KEY(provider,creator,trade_no));
        CREATE TABLE IF NOT EXISTS jobs(
          order_id TEXT PRIMARY KEY,status TEXT NOT NULL,result TEXT NOT NULL DEFAULT '{}',
          started REAL,finished REAL,lease_until REAL,attempts INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS reservations(
          order_id TEXT PRIMARY KEY,micro INTEGER NOT NULL,day TEXT NOT NULL,status TEXT NOT NULL,
          actual_micro INTEGER,created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS ledger(
          event_key TEXT PRIMARY KEY,kind TEXT NOT NULL,order_id TEXT NOT NULL,amount INTEGER,
          unit TEXT NOT NULL,source TEXT NOT NULL,created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS outbox(
          event_key TEXT PRIMARY KEY,order_id TEXT NOT NULL,kind TEXT NOT NULL,payload TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'pending',attempts INTEGER NOT NULL DEFAULT 0,
          next_attempt REAL NOT NULL DEFAULT 0,created REAL NOT NULL,receipt TEXT NOT NULL DEFAULT '{}');
        CREATE TABLE IF NOT EXISTS inbound(
          event_key TEXT PRIMARY KEY,payload TEXT NOT NULL,status TEXT NOT NULL,
          attempts INTEGER NOT NULL DEFAULT 0,next_attempt REAL NOT NULL DEFAULT 0,
          created REAL NOT NULL,reason TEXT NOT NULL DEFAULT '');
        CREATE TABLE IF NOT EXISTS audit(
          id INTEGER PRIMARY KEY,at REAL NOT NULL,actor TEXT NOT NULL,action TEXT NOT NULL,
          target TEXT NOT NULL,evidence TEXT NOT NULL);
        CREATE TRIGGER IF NOT EXISTS frozen_quote_update BEFORE UPDATE ON quotes BEGIN
          SELECT RAISE(ABORT,'immutable_quote'); END;
        CREATE TRIGGER IF NOT EXISTS frozen_quote_delete BEFORE DELETE ON quotes BEGIN
          SELECT RAISE(ABORT,'immutable_quote'); END;
        ''')
        (self.root / "state.sqlite").chmod(0o600)
        with self.tx():
            if self.get("initial_budget_loaded") is None:
                self.put("budget_total", int(policy.get("initial_budget_microyuan", 0)))
                self.put("initial_budget_loaded", True)
            self.put("schema_version", 1)

    @locked
    def close(self):
        self.db.close()

    @contextlib.contextmanager
    def tx(self):
        with self.lock:
            if self.db.in_transaction:
                yield
                return
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield
                self.db.commit()
            except BaseException:
                self.db.rollback()
                raise

    def get(self, key, default=None):
        with self.lock:
            row = self.db.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
            return json.loads(row[0]) if row else default

    def put(self, key, value):
        with self.lock:
            self.db.execute("INSERT INTO meta VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (key, js(value)))

    @locked
    def audit(self, actor, action, target, evidence):
        self.db.execute("INSERT INTO audit(at,actor,action,target,evidence) VALUES(?,?,?,?,?)",
            (self.now(), actor, action, target, js(evidence)))

    @locked
    def emit(self, key, oid, kind, payload):
        self.db.execute("INSERT OR IGNORE INTO outbox(event_key,order_id,kind,payload,created) VALUES(?,?,?,?,?)",
            (key, oid, kind, js(payload), self.now()))

    def notify(self, oid, stage, reason=""):
        self.emit(stage + ":" + oid, oid, "owner", {"stage": stage, "reason": reason})
        # No customer, text, amount, binding code or deliverable enters shared memory.
        self.emit("activity:" + stage + ":" + oid, oid, "activity", {"stage": stage})

    @locked
    def capacity(self):
        return self.db.execute("SELECT count(*) FROM reservations WHERE status IN ('held','unknown')").fetchone()[0]

    @locked
    def available_budget(self):
        used = self.db.execute("SELECT coalesce(sum(CASE WHEN status='settled' THEN actual_micro ELSE micro END),0) FROM reservations WHERE status IN ('held','unknown','settled')").fetchone()[0]
        return int(self.get("budget_total", 0)) - used

    def _intake(self, customer):
        p = self.policy
        if not p.get("intake_enabled") or self.get("paused", False):
            raise ValueError("intake_paused")
        mode = p.get("run_mode")
        allow = p.get("pilot_allowlist", []) if mode == "pilot" else p.get("live_allowlist", [])
        if mode not in ("pilot", "live") or customer not in allow:
            raise PermissionError("customer_not_allowlisted")
        if not p.get("product_verified") or not p.get("owner_notification_verified"):
            raise ValueError("product_or_owner_delivery_unverified")
        mode=p.get('owner_notification_mode','blocked')
        if mode=='passive_window':
            if not p.get('passive_window_accepted') or not 0<=self.now()-self.get('owner_last_inbound',0)<=3000:
                raise ValueError('owner_reply_window_requires_refresh')
        elif mode!='active_probe':raise ValueError('owner_notification_mode_blocked')
        if not self.get("bootstrap_complete", False) or self.get("api_failures", 0) >= 3:
            raise ValueError("payment_reconciliation_not_ready")
        pending = self.db.execute("SELECT min(created) FROM outbox WHERE kind='owner' AND status!='sent'").fetchone()[0]
        if pending and self.now() - pending > p.get("owner_notification_stale_seconds", 600):
            raise ValueError("owner_notification_stale")
        if self.get("budget_total", 0) <= 0:
            raise ValueError("project_budget_not_allocated")

    def quote(self, customer, task, arguments, *, prior=None):
        customer = identity(customer)
        if task not in self.policy.get("allowed_tasks", []):
            raise ValueError("task_not_enabled")
        # Bounds and per-tool schemas are validated before any payment invitation.
        from .executor import validate_task
        validate_task(task, arguments)
        with self.tx():
            self._intake(customer)
            if self.db.execute("SELECT count(*) FROM quotes WHERE customer=? AND expires>?",(customer,self.now())).fetchone()[0]>=10:
                raise ValueError('too_many_recent_quotes')
            if self.db.execute("SELECT count(*) FROM orders").fetchone()[0]>=10000:
                raise ValueError('ledger_capacity_requires_archive_review')
            oid, version = "PT" + secrets.token_hex(8).upper(), 1
            if prior:
                old = self.read_order(prior, customer)
                if old["status"] != "QUOTED":
                    raise ValueError("accepted_quote_cannot_be_revised")
                oid, version = prior, old["version"] + 1
            unit = self.policy["unit_price_fen"]
            amount = self.policy.get("task_prices_fen", {}).get(task, unit)
            if type(amount) is not int or not 100 <= amount <= self.policy["max_order_fen"]:
                raise ValueError("price_policy")
            if amount % unit or amount > unit and not self.policy.get("quantity_verified"):
                raise ValueError("variable_quantity_not_verified")
            token = "PT-" + secrets.token_hex(16).upper()
            budget = min(self.policy["max_job_budget_microyuan"], amount * 2500)
            expiry = self.now() + self.policy["quote_ttl_seconds"]
            snapshot = {"task": task, "arguments": arguments, "scope": "仅处理本次明确输入；AI参与的结果须由客户复核",
                "deliverables": ["结果正文", "可用时的PNG图"], "acceptance": "与确认输入相符、结果可打开",
                "estimated_seconds": 180, "revision_scope": "一次同范围纠错，不含新增需求",
                "amount_fen": amount, "currency": "CNY", "creator_id": self.policy["creator_id"],
                "plan_id": self.policy["plan_id"], "sku_id": self.policy["sku_id"], "quantity": amount // unit,
                "product_url": self.policy["product_url"], "budget_microyuan": budget,
                "pricing_policy_version": self.policy["pricing_policy_version"],
                "service_terms_version": self.policy["service_terms_version"],
                "retention_days": 7, "auto_binding": bool(self.policy.get("remark_verified")),
                "binding_token": None}
            # The immutable quote stores only a digest. The protected delivery
            # outbox temporarily holds the token until its receipt is confirmed.
            self.db.execute("INSERT INTO quotes VALUES(?,?,?,?,?,?,?)", (oid, version, customer, js(snapshot), digest(token), self.now(), expiry))
            self.db.execute("INSERT INTO orders VALUES(?,?,'QUOTED','UNPAID','NOT_STARTED',NULL,NULL,'') ON CONFLICT(id) DO UPDATE SET version=excluded.version", (oid, version))
            return {"order_id": oid, "version": version, "expires": expiry, "quote": snapshot, "binding_token": token}

    @locked
    def read_order(self, oid, customer=None):
        row = self.db.execute("SELECT o.*,q.customer,q.snapshot,q.expires FROM orders o JOIN quotes q ON o.id=q.id AND o.version=q.version WHERE o.id=?", (oid,)).fetchone()
        if not row or customer is not None and row["customer"] != customer:
            raise PermissionError("order_not_accessible")
        value = dict(row)
        value["quote"] = json.loads(value.pop("snapshot"))
        return value

    def accept(self, oid, version, customer, evidence=None):
        customer = identity(customer) if isinstance(customer, dict) else customer
        with self.tx():
            order = self.read_order(oid, customer)
            if order["version"] != version:
                raise ValueError("quote_version_changed")
            if order["accepted"]:
                if order['status']!='ACCEPTED' or order['payment']!='UNPAID':
                    raise ValueError('order_already_progressed_do_not_pay_again')
                return order
            self._intake(customer)
            if order["status"] != "QUOTED" or order["expires"] < self.now():
                raise ValueError("quote_expired")
            reserve = order["quote"]["budget_microyuan"]
            today = self.db.execute("SELECT coalesce(sum(CASE WHEN status='settled' THEN actual_micro ELSE micro END),0) FROM reservations WHERE (day=? OR status IN ('held','unknown')) AND status!='released'", (day(self.now()),)).fetchone()[0]
            if self.capacity() >= self.policy["max_outstanding_orders"]:
                raise ValueError("capacity_unavailable")
            if reserve > self.available_budget() or today + reserve > self.policy["daily_budget_microyuan"]:
                raise ValueError("budget_unavailable")
            self.db.execute("INSERT INTO reservations VALUES(?,?,?,'held',NULL,?)", (oid, reserve, day(self.now()), self.now()))
            self.db.execute("UPDATE orders SET status='ACCEPTED',accepted=?,execution='RESERVED' WHERE id=?", (self.now(), oid))
            self.notify(oid, "accepted")
            self.audit("customer" if evidence is None else "owner", "accept", oid, evidence or {"version": version})
            return self.read_order(oid)

    def payment_hint(self, trade_no, source="webhook"):
        identifier(trade_no)
        with self.tx():
            if self.db.execute("SELECT count(*) FROM inbound WHERE status!='done'").fetchone()[0] >= 1000:
                raise ValueError("verification_queue_full")
            key = "payment:" + trade_no
            self.db.execute("INSERT OR IGNORE INTO inbound VALUES(?,?,'pending',0,0,?,'')", (key, js({"trade_no": trade_no, "source": source}), self.now()))
            # A later real retry can revive an exhausted query, but never resets its history.
            self.db.execute("UPDATE inbound SET status='pending',next_attempt=? WHERE event_key=? AND (status='review' OR (status='done' AND next_attempt<=?))", (self.now()+60, key,self.now()))

    def apply_payment(self, evidence, *, historical=False, owner_order=None, owner_evidence=None):
        if not isinstance(evidence, PaymentEvidence) or evidence.source != "official_query" or evidence.creator_id != self.policy["creator_id"]:
            raise ValueError("untrusted_payment_evidence")
        e = evidence.data()
        key = ("afdian", e["creator_id"], e["trade_no"])
        with self.tx():
            previous = self.db.execute("SELECT * FROM payments WHERE provider=? AND creator=? AND trade_no=?", key).fetchone()
            if previous and previous["order_id"]:
                self.db.execute("UPDATE payments SET evidence=?,updated=? WHERE provider=? AND creator=? AND trade_no=?",(js(e),self.now(),*key))
                # Payment facts can change, but never replace the original allocation.
                if e["status"] != 2:
                    self.db.execute("UPDATE orders SET payment='PAYMENT_REVIEW',status='NEEDS_ATTENTION',reason='provider_status_changed' WHERE id=?", (previous["order_id"],))
                    self.notify(previous["order_id"], "payment_review", "provider_status_changed")
                return {"duplicate": True, "order_id": previous["order_id"]}
            old_history = bool(previous["historical"]) if previous else historical
            self.db.execute("INSERT INTO payments VALUES(?,?,?,?,?,?,?,NULL,?,?,?) ON CONFLICT(provider,creator,trade_no) DO UPDATE SET evidence=excluded.evidence,provider_paid=excluded.provider_paid,updated=excluded.updated",
                (*key, js(e), int(e["status"] == 2), int(old_history), "UNALLOCATED", "unbound", self.now(), self.now()))
            quote = None
            if owner_order:
                if not owner_evidence or len(owner_evidence.get("basis", "")) < 8:
                    raise ValueError("owner_binding_evidence_required")
                order = self.read_order(owner_order)
                quote = self.db.execute("SELECT * FROM quotes WHERE id=? AND version=?", (owner_order, order["version"])).fetchone()
            elif e["binding_digest"] and self.policy.get("remark_verified") and not old_history:
                quote = self.db.execute("SELECT * FROM quotes WHERE binding_digest=?", (e["binding_digest"],)).fetchone()
                order = self.read_order(quote["id"]) if quote else None
            else:
                order = None
            reason = "historical_payment" if old_history and not owner_order else "binding_missing_or_unverified"
            if not quote and e['status']==2 and e['product_type']==0:
                self.db.execute("UPDATE payments SET allocation='SUPPORT',reason='not_task_payment' WHERE provider=? AND creator=? AND trade_no=?",key)
                if not old_history:
                    self.db.execute("INSERT OR IGNORE INTO ledger VALUES(?,?,?,?,?,?,?)",('support:'+e['trade_no'],'support_received','',e['amount_fen'],'CNY_fen','afdian_official',self.now()))
                return {'bound':False,'support':True,'reason':'not_task_payment'}
            if quote:
                q = json.loads(quote["snapshot"])
                tests = [(e["status"] == 2, "not_successful"), (e["product_type"] == 1, "not_sale_product"),
                    (e["plan_id"] == q["plan_id"], "plan_mismatch"),
                    (bool(q["sku_id"]) and e["skus"] == ((q["sku_id"], q["quantity"]),), "sku_or_quantity_mismatch"),
                    (e["amount_fen"] == q["amount_fen"], "amount_mismatch"),
                    (not e["redeem"] and e["discount_fen"] == 0, "redemption_or_discount"),
                    (order["accepted"] is not None and order["version"] == quote["version"], "quote_not_accepted"),
                    (order["status"] == "ACCEPTED", "order_not_waiting"),
                    (self.now() <= quote["expires"] or bool(owner_evidence and owner_evidence.get("timing_reviewed")), "payment_time_unavailable_after_expiry")]
                reason = next((why for ok, why in tests if not ok), "")
                if not reason:
                    oid = quote["id"]
                    if self.db.execute("SELECT 1 FROM payments WHERE order_id=?", (oid,)).fetchone():
                        reason = "already_paid_by_another_transaction"
                    else:
                        self.db.execute("UPDATE payments SET allocation='BOUND',order_id=?,reason='' WHERE provider=? AND creator=? AND trade_no=?", (oid, *key))
                        self.db.execute("UPDATE orders SET payment='PAID_VERIFIED' WHERE id=?", (oid,))
                        held = self.db.execute("SELECT status FROM reservations WHERE order_id=?", (oid,)).fetchone()
                        if held and held[0] == "held":
                            self.db.execute("UPDATE orders SET status='QUEUED' WHERE id=?", (oid,))
                            self.db.execute("INSERT OR IGNORE INTO jobs(order_id,status) VALUES(?,'QUEUED')", (oid,))
                        else:
                            self.db.execute("UPDATE orders SET status='NEEDS_ATTENTION',reason='budget_reservation_missing' WHERE id=?", (oid,))
                        self.db.execute("INSERT OR IGNORE INTO ledger VALUES(?,?,?,?,?,?,?)", ("receipt:"+e["trade_no"], "customer_paid", oid, e["amount_fen"], "CNY_fen", "afdian_official", self.now()))
                        self.notify(oid, "paid")
                        if owner_order:
                            self.audit("owner", "owner_verified_payment_binding", oid, {**owner_evidence, "trade_no": e["trade_no"]})
                        return {"bound": True, "order_id": oid}
            self.db.execute("UPDATE payments SET allocation='PAYMENT_REVIEW',reason=? WHERE provider=? AND creator=? AND trade_no=?", (reason, *key))
            if quote:
                self.notify(quote["id"], "payment_review:" + e["trade_no"], reason)
            elif not old_history:
                self.emit("unallocated:"+e["trade_no"], "", "owner", {"stage": "unallocated", "reason": reason})
            return {"bound": False, "reason": reason}

    def claim_job(self):
        with self.tx():
            if self.get("paused", False):
                return None
            if self.db.execute("SELECT 1 FROM jobs WHERE status='RUNNING'").fetchone():
                return None
            row = self.db.execute("SELECT j.order_id FROM jobs j JOIN orders o ON o.id=j.order_id WHERE j.status='QUEUED' AND o.payment='PAID_VERIFIED' AND o.status='QUEUED' ORDER BY o.accepted LIMIT 1").fetchone()
            if not row:
                return None
            oid = row[0]
            self.db.execute("UPDATE jobs SET status='RUNNING',started=?,lease_until=?,attempts=attempts+1 WHERE order_id=?", (self.now(), self.now()+180, oid))
            self.db.execute("UPDATE orders SET status='RUNNING',execution='RUNNING' WHERE id=?", (oid,))
            return self.read_order(oid)

    def finish_job(self, oid, result, actual_micro=0):
        with self.tx():
            row = self.db.execute("SELECT * FROM jobs WHERE order_id=?", (oid,)).fetchone()
            if not row or row["status"] != "RUNNING":
                raise ValueError("job_not_running")
            reserve = self.db.execute("SELECT micro FROM reservations WHERE order_id=?", (oid,)).fetchone()[0]
            if actual_micro is not None and (type(actual_micro) is not int or not 0 <= actual_micro <= reserve):
                raise ValueError("cost_exceeds_reserved_budget")
            self.db.execute("UPDATE reservations SET status=?,actual_micro=? WHERE order_id=?", ("unknown" if actual_micro is None else "settled", actual_micro, oid))
            self.db.execute("INSERT OR IGNORE INTO ledger VALUES(?,?,?,?,?,?,?)", ("cost:"+oid, "execution_cost", oid, actual_micro, "CNY_micro", "measured" if actual_micro is not None else "unknown_reservation_retained", self.now()))
            self.db.execute("UPDATE jobs SET status='SUCCEEDED',result=?,finished=? WHERE order_id=?", (js(result), self.now(), oid))
            if self.read_order(oid)['payment']!='PAID_VERIFIED':
                self.db.execute("UPDATE orders SET status='NEEDS_ATTENTION',execution='SUCCEEDED',reason='payment_changed_before_delivery' WHERE id=?",(oid,))
                self.notify(oid,'payment_review','payment_changed_before_delivery')
                return
            self.db.execute("UPDATE orders SET status='DELIVERABLE_READY',execution='SUCCEEDED' WHERE id=?", (oid,))
            self.emit("delivery:"+oid, oid, "delivery", {"result": result})

    def fail_job(self, oid, reason):
        with self.tx():
            self.db.execute("UPDATE jobs SET status='FAILED',finished=? WHERE order_id=?", (self.now(), oid))
            self.db.execute("UPDATE orders SET status='NEEDS_ATTENTION',execution='FAILED',reason=? WHERE id=?", (reason, oid))
            self.db.execute("UPDATE reservations SET status='unknown' WHERE order_id=?", (oid,))
            self.notify(oid, "failed", reason)

    def recover(self):
        with self.tx():
            for row in self.db.execute("SELECT order_id FROM jobs WHERE status='RUNNING'").fetchall():
                oid = row[0]
                self.db.execute("UPDATE jobs SET status='INTERRUPTED' WHERE order_id=?", (oid,))
                self.db.execute("UPDATE orders SET status='NEEDS_ATTENTION',reason='interrupted_execution' WHERE id=?", (oid,))
                self.db.execute("UPDATE reservations SET status='unknown' WHERE order_id=?", (oid,))
                self.notify(oid, "interrupted", "check_saved_result_before_resume")
            self.db.execute("UPDATE outbox SET status='unknown' WHERE status='sending'")
            # Queries have no external side effects and may safely resume.
            self.db.execute("UPDATE inbound SET status='pending' WHERE status='checking'")

    def claim_outbox(self):
        with self.tx():
            row = self.db.execute("SELECT * FROM outbox WHERE status IN ('pending','retry') AND next_attempt<=? ORDER BY created LIMIT 1", (self.now(),)).fetchone()
            if not row:
                return None
            self.db.execute("UPDATE outbox SET status='sending',attempts=attempts+1 WHERE event_key=?", (row["event_key"],))
            value = dict(row)
            value["payload"] = json.loads(value["payload"])
            return value

    def outbox_result(self, key, state, receipt=None):
        if state not in ("sent", "rejected", "unknown", "manual_pending"):
            raise ValueError("invalid_delivery_result")
        with self.tx():
            row = self.db.execute("SELECT * FROM outbox WHERE event_key=?", (key,)).fetchone()
            if not row or row["status"] != "sending":
                raise ValueError("outbox_not_sending")
            status = "retry" if state == "rejected" and row["attempts"] < 6 else "failed" if state == "rejected" else state
            self.db.execute("UPDATE outbox SET status=?,receipt=?,next_attempt=? WHERE event_key=?", (status, js(receipt or {}), self.now()+min(3600, 30*2**row["attempts"]), key))
            if state == "sent" and row["kind"] == "customer_reply":
                # Do not retain the raw binding token after confirmed delivery.
                self.db.execute("UPDATE outbox SET payload='{}' WHERE event_key=?", (key,))
            if row["kind"] == "delivery":
                if state == "sent":
                    self._sent(row["order_id"], "platform_receipt")
                else:
                    self.db.execute("UPDATE orders SET status='DELIVERY_PENDING' WHERE id=?", (row["order_id"],))

    def _sent(self, oid, source):
        order = self.read_order(oid)
        self.db.execute("UPDATE orders SET status='SENT' WHERE id=?", (oid,))
        self.db.execute("INSERT OR IGNORE INTO ledger VALUES(?,?,?,?,?,?,?)", ("delivered:"+oid, "delivered_gross", oid, order["quote"]["amount_fen"], "CNY_fen", source, self.now()))
        self.notify(oid, "delivered")

    def manual_delivery(self, oid, evidence):
        with self.tx():
            order = self.read_order(oid)
            if json.loads(order["customer"])["channel"] != "bilibili_manual" or order["execution"] != "SUCCEEDED":
                raise ValueError("manual_delivery_not_applicable")
            if not evidence.get("recipient") or not evidence.get("sent_at") or not evidence.get("artifact_sha256"):
                raise ValueError("manual_delivery_evidence_required")
            result = json.loads(self.db.execute("SELECT result FROM jobs WHERE order_id=?", (oid,)).fetchone()[0])
            if evidence["recipient"] != json.loads(order["customer"])["actor"] or evidence["artifact_sha256"] != result["sha256"]:
                raise ValueError("manual_delivery_identity_or_version")
            self.audit("owner", "manual_delivery", oid, evidence)
            self.db.execute("UPDATE outbox SET status='sent',receipt=? WHERE event_key=?", (js({"source":"owner_attestation",**evidence}), "delivery:"+oid))
            self._sent(oid, "owner_attestation")

    def customer_close(self, oid, customer):
        with self.tx():
            order = self.read_order(oid, identity(customer))
            if order["status"] != "SENT":
                raise ValueError("not_delivered")
            self.db.execute("UPDATE orders SET status='CLOSED',confirmed=? WHERE id=?", (self.now(), oid))

    def cancel(self, oid, customer):
        with self.tx():
            order=self.read_order(oid,identity(customer))
            if order['payment']!='UNPAID' or order['status'] not in ('QUOTED','ACCEPTED'):
                raise ValueError('paid_or_started_order_needs_owner_review')
            state='CANCELLED' if order['status']=='QUOTED' else 'CANCEL_REVIEW'
            self.db.execute("UPDATE orders SET status=?,reason='customer_requested_cancel' WHERE id=?",(state,oid))
            # No authoritative paid_at exists: an accepted reservation is retained
            # until payment/cancellation is reviewed, including delayed callbacks.
            self.notify(oid,'cancel_review')
            self.audit('customer','cancel',oid,{'reservation_retained':order['accepted'] is not None})
            return {'status':state}

    def recover_saved_result(self, oid, basis):
        from .executor import load_saved_result
        if not isinstance(basis, str) or len(basis) < 8:
            raise ValueError("recovery_evidence_required")
        with self.tx():
            order = self.read_order(oid)
            row = self.db.execute("SELECT status FROM jobs WHERE order_id=?", (oid,)).fetchone()
            if not row or row[0] not in ("INTERRUPTED", "FAILED") or order["payment"] != "PAID_VERIFIED":
                raise ValueError("job_not_recoverable")
            result = load_saved_result(self.root, oid)
            self.db.execute("UPDATE jobs SET status='RUNNING' WHERE order_id=?", (oid,))
            self.finish_job(oid, result, result["actual_microyuan"])
            self.audit("owner", "recover_saved_result", oid, {"basis":basis,"sha256":result["sha256"]})
        return {"recovered":True,"regenerated":False}

    def admin(self, action, data):
        with self.tx():
            if action == "pause":
                self.put("paused", True)
            elif action == "resume":
                self.put("paused", False)
            elif action == "add_budget":
                amount = data.get("microyuan")
                if type(amount) is not int or not 0 < amount <= 100000000:
                    raise ValueError("budget_increment_invalid")
                key='budget:'+identifier(data.get('allocation_id'))
                prior=self.db.execute('SELECT amount FROM ledger WHERE event_key=?',(key,)).fetchone()
                if prior and prior[0]!=amount:raise ValueError('allocation_id_amount_conflict')
                if not prior:
                    self.put("budget_total", self.get("budget_total", 0)+amount)
                    self.db.execute("INSERT INTO ledger VALUES(?,?,?,?,?,?,?)", (key, "owner_budget", "", amount, "CNY_micro", "owner_allocation", self.now()))
            elif action == "retry_delivery":
                row = self.db.execute("SELECT status FROM outbox WHERE event_key=?", (data["event_key"],)).fetchone()
                if not row or row[0] not in ("failed", "retry"):
                    raise ValueError("unknown_or_sent_delivery_requires_manual_reconciliation")
                self.db.execute("UPDATE outbox SET status='retry',attempts=0,next_attempt=0 WHERE event_key=?", (data["event_key"],))
            elif action == "reconcile_delivery":
                key=data['event_key']
                row=self.db.execute("SELECT * FROM outbox WHERE event_key=?",(key,)).fetchone()
                if not row or row['status']!='unknown' or len(data.get('basis',''))<8:
                    raise ValueError('uncertain_delivery_and_evidence_required')
                if data.get('observed')=='sent' and data.get('remote_id'):
                    self.db.execute("UPDATE outbox SET status='sending' WHERE event_key=?",(key,))
                    self.outbox_result(key,'sent',{'remote_id':data['remote_id'],'source':'owner_reconciliation'})
                elif data.get('observed')=='not_sent' and data.get('core_reconciled') is True:
                    self.db.execute("UPDATE outbox SET status='retry',attempts=0,next_attempt=0 WHERE event_key=?",(key,))
                else:raise ValueError('reconciliation_result_required')
            elif action == "record_refund":
                order = self.read_order(data["order_id"])
                amount = data.get("amount_fen")
                if type(amount) is not int or not 0 < amount <= order["quote"]["amount_fen"] or not data.get("basis"):
                    raise ValueError("refund_evidence_required")
                if order["payment"] != "PAID_VERIFIED":
                    raise ValueError("refund_requires_verified_payment")
                self.db.execute("UPDATE orders SET payment=?,status='NEEDS_ATTENTION',reason='owner_recorded_refund' WHERE id=?", ("REFUNDED" if amount==order["quote"]["amount_fen"] else "PARTIALLY_REFUNDED", data["order_id"]))
                self.db.execute("INSERT OR IGNORE INTO ledger VALUES(?,?,?,?,?,?,?)", ("refund:"+data["order_id"], "refund", data["order_id"], amount, "CNY_fen", "owner_attestation", self.now()))
            else:
                raise ValueError("admin_action_unsupported")
            self.audit("owner", action, data.get("order_id", ""), data)
        return self.status()

    def status(self):
        with self.lock:
            return {"mode":self.policy["run_mode"],"intake_enabled":self.policy["intake_enabled"],
                "paused":self.get("paused",False),"bootstrap_complete":self.get("bootstrap_complete",False),
                "scan_page":self.get("scan_page",1),"api_failures":self.get("api_failures",0),
                "budget_available_microyuan":self.available_budget(),
                "orders":dict(self.db.execute("SELECT status,count(*) FROM orders GROUP BY status").fetchall()),
                "notifications":dict(self.db.execute("SELECT status,count(*) FROM outbox WHERE kind='owner' GROUP BY status").fetchall()),
                "accounting":self.accounting(),
                "settlement_balance":"unverified","bili_dm_mode":"manual_import"}

    @locked
    def accounting(self):
        sums=dict(self.db.execute("SELECT kind,coalesce(sum(amount),0) FROM ledger GROUP BY kind").fetchall())
        held=self.db.execute("SELECT coalesce(sum(micro),0) FROM reservations WHERE status IN ('held','unknown')").fetchone()[0]
        unknown=self.db.execute("SELECT count(*) FROM reservations WHERE status='unknown'").fetchone()[0]
        return {'customer_paid_fen':sums.get('customer_paid',0),
            'support_received_fen':sums.get('support_received',0),
            'delivered_gross_fen':sums.get('delivered_gross',0),'owner_recorded_refunds_fen':sums.get('refund',0),
            'allocated_budget_microyuan':self.get('budget_total',0),'held_budget_microyuan':held,
            'known_execution_api_cost_microyuan':sums.get('execution_cost',0),'unknown_cost_jobs':unknown,
            'fees_fen':None,'withdrawable_balance_fen':None,'infrastructure_cost_included':False,
            'automatic_budget_reinvestment':False}
