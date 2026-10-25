"""Independent daytime and overnight quotas, based on durable timestamps."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import copy
import math
TZ = ZoneInfo("Asia/Shanghai")

def enabled(c):
    return c.get("night_activity_enabled", False) is True

def window(c, ts):
    now = datetime.fromtimestamp(ts, TZ)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if not enabled(c):
        return midnight.timestamp(), (midnight + timedelta(days=1)).timestamp(), "daily"
    if 9 <= now.hour < 23:
        return (midnight + timedelta(hours=9)).timestamp(), (midnight + timedelta(hours=23)).timestamp(), "day"
    start = midnight + timedelta(hours=23)
    if now.hour < 9:
        start -= timedelta(days=1)
    return start.timestamp(), (start + timedelta(hours=10)).timestamp(), "night"

def scale(value):
    # Half-up rounding, retaining one opportunity for a positive small limit.
    return 0 if value == 0 else max(1, (value + 2) // 4)

def effective(c, ts):
    if not enabled(c):
        return c
    c = copy.deepcopy(c)
    c["_quota_window"] = list(window(c, ts))
    c["autonomy_period_limit"] = 30
    c["account_period_limit"] = 3
    if c["_quota_window"][2] == "night":
        c["limits"] = {k: scale(v) for k, v in c["limits"].items()}
        c["model_daily"] = scale(c["model_daily"])
        c["autonomy_period_limit"] = scale(30)
        c["account_period_limit"] = scale(3)
        if c.get("engagement"):
            c["engagement"]["limits"] = {k: scale(v) for k, v in c["engagement"]["limits"].items()}
        if c.get("live_chat"):
            for k in ("daily_limit", "room_daily_limit", "visits_daily"):
                c["live_chat"][k] = scale(c["live_chat"][k])
    return c

def validate_reset(c):
    value=c.get("quota_reset_at",0)
    if type(value) not in (int,float) or not math.isfinite(value) or value<0:
        raise ValueError("invalid_quota_reset_at")

def reset_start(c,start,end,ts):
    validate_reset(c)
    reset=c.get("quota_reset_at",0)
    return reset if start<=reset<end and reset<=ts else start

def daily_count(db,c,ts,where="1",args=()):
    midnight=datetime.fromtimestamp(ts,TZ).replace(hour=0,minute=0,second=0,microsecond=0)
    start=midnight.timestamp();end=(midnight+timedelta(days=1)).timestamp()
    start=reset_start(c,start,end,ts)
    return db.execute(f"SELECT count(*) FROM actions WHERE created>=? AND created<? AND ({where})",(start,end,*args)).fetchone()[0]

def count(db, c, ts, table="actions", where="1", args=()):
    column = {"actions": "created", "usage": "created", "live_visits": "started", "account_changes": "created"}[table]
    start, end, _ = window(c, ts)
    start=reset_start(c,start,end,ts)
    return db.execute(f"SELECT count(*) FROM {table} WHERE {column}>=? AND {column}<? AND ({where})", (start, end, *args)).fetchone()[0]

def autonomy_key(c, ts):
    start, end, period = window(c, ts)
    key=("autonomy_count:night:" + datetime.fromtimestamp(start,TZ).date().isoformat()
         if period=="night" else "autonomy_count:" + datetime.fromtimestamp(ts,TZ).date().isoformat())
    reset=reset_start(c,start,end,ts)
    return key+":reset:"+str(reset) if reset>start else key

def activity_end(c, ts, duration):
    if c["active_start"] == 0 and c["active_end"] == 24:
        return ts + duration
    midnight = datetime.fromtimestamp(ts, TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    return (midnight + timedelta(hours=c["active_end"])).timestamp()
