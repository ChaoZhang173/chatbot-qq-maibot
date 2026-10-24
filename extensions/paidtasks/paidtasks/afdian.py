"""Official OpenAPI contract. Never log request signatures or response debug data."""
from dataclasses import dataclass, asdict
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
import random
import re
import time
import urllib.request
import urllib.error

class ProviderError(Exception):
    pass

def fen(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{1,9}(?:\.\d{1,2})?", value):
        raise ValueError("invalid_amount")
    try:
        n = Decimal(value) * 100
        return int(n)
    except (InvalidOperation, ValueError):
        raise ValueError("invalid_amount") from None

def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        raise ValueError("invalid_identifier")
    return value

def signed_body(creator, token, params, timestamp):
    raw = json.dumps(params, ensure_ascii=False, separators=(",", ":"))
    wire = f"{token}params{raw}ts{timestamp}user_id{creator}"
    return {"user_id": creator, "params": raw, "ts": timestamp,
            "sign": hashlib.md5(wire.encode()).hexdigest()}

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None

def http_transport(endpoint, body):
    # Fixed origin, TLS verification, no redirect carrying credentials to other hosts.
    request = urllib.request.Request("https://afdian.com/api/open/" + endpoint,
        data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=12) as response:
            raw = response.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                raise ProviderError("response_too_large")
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise ProviderError("response_schema")
            return result
    except (OSError, ValueError, urllib.error.HTTPError):
        raise ProviderError("transport_unavailable") from None

@dataclass(frozen=True)
class PaymentEvidence:
    creator_id: str
    trade_no: str
    payer_id: str
    plan_id: str
    amount_fen: int
    status: int
    product_type: int
    skus: tuple
    redeem: bool
    discount_fen: int
    binding_digest: str
    verified_at: float
    # Public contract has no trustworthy paid_at field: do not invent one.
    source: str = "official_query"

    def data(self):
        return asdict(self)

def normalize(creator, raw, now):
    if not isinstance(raw, dict):
        raise ValueError("order_schema")
    for name in ("status", "product_type"):
        if type(raw.get(name)) is not int:
            raise ValueError("missing_" + name)
    details = raw.get("sku_detail")
    if details is None and raw['product_type']==0:
        details=[]  # Conventional support has no sale SKU; never a task payment.
    if not isinstance(details, list):
        raise ValueError("missing_quantity_evidence")
    skus = []
    for sku in details:
        if not isinstance(sku, dict) or type(sku.get("count")) is not int or not 1 <= sku["count"] <= 10000:
            raise ValueError("invalid_sku_quantity")
        skus.append((identifier(sku.get("sku_id")), sku["count"]))
    # Only an entire, documented remark token. No instructions, nicknames, time or amount matching.
    remark = raw.get("remark", "")
    match = re.fullmatch(r"PT-[A-F0-9]{32}", remark.strip()) if isinstance(remark, str) else None
    digest = hashlib.sha256(match[0].encode()).hexdigest() if match else ""
    plan = raw.get("plan_id")
    if not isinstance(plan, str):
        raise ValueError("missing_plan")
    return PaymentEvidence(identifier(creator), identifier(raw.get("out_trade_no")),
        identifier(raw.get("user_id")), plan, fen(raw.get("total_amount")),
        raw["status"], raw["product_type"], tuple(skus), bool(raw.get("redeem_id")),
        fen(raw.get("discount", "0.00")), digest, now)

class AfdianClient:
    def __init__(self, creator=None, token=None, transport=http_transport, now=time.time,
                 sleep=time.sleep, attempts=3):
        self.creator = creator or os.environ.get("AFDIAN_CREATOR_USER_ID", "")
        self._token = token or os.environ.get("AFDIAN_API_TOKEN", "")
        if not self.creator or not self._token:
            raise ProviderError("credentials_missing")
        self.transport, self.now, self.sleep, self.attempts = transport, now, sleep, attempts

    def call(self, endpoint, params):
        for attempt in range(self.attempts):
            try:
                result = self.transport(endpoint, signed_body(self.creator, self._token, params, int(self.now())))
                code = result.get("ec")
                if code != 200:
                    # Deliberately discard em/debug/request: they can contain reflected secrets.
                    if code in (400001, 400002, 400003, 400004, 400005):
                        raise ValueError("configuration_rejected")
                    raise ProviderError("provider_rejected")
                if not isinstance(result.get("data"), dict):
                    raise ProviderError("response_schema")
                return result["data"]
            except ValueError:
                raise ProviderError("configuration_rejected") from None
            except ProviderError:
                if attempt + 1 == self.attempts:
                    raise
                self.sleep(min(8, 2 ** attempt) + random.random())

    def ping(self):
        self.call("ping", {})
        return {"api_authenticated": True}

    def query_order(self, trade_no):
        identifier(trade_no)
        data = self.call("query-order", {"out_trade_no": trade_no})
        rows = data.get("list")
        if not isinstance(rows, list):
            raise ProviderError("missing_order_list")
        exact = [r for r in rows if isinstance(r, dict) and r.get("out_trade_no") == trade_no]
        if len(exact) != 1:
            raise ProviderError("order_not_uniquely_found")
        return normalize(self.creator, exact[0], self.now())

    def list_orders(self, page):
        data = self.call("query-order", {"page": page})
        if not isinstance(data.get("list"), list) or type(data.get("total_page")) is not int:
            raise ProviderError("page_schema")
        if not 0 <= data["total_page"] <= 100000:
            raise ProviderError("page_count")
        return data["list"], data["total_page"]
