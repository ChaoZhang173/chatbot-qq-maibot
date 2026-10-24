"""Bounded UID mentions from current mutuals or user-specified Bilibili UIDs."""
from __future__ import annotations

import hashlib
import json
import re

from .core import Denied

DEFAULT_POLICY = {"mode": "off", "max_per_action": 1, "manual_uids": [], "cache_seconds": 300}


def policy(config):
    value = {**DEFAULT_POLICY, **config.get("mentions", {})}
    if (value["mode"] not in ("off", "manual", "mutual_and_manual")
            or type(value["max_per_action"]) is not int or value["max_per_action"] != 1):
        raise ValueError("invalid_mention_policy")
    if type(value["cache_seconds"]) is not int or not 60 <= value["cache_seconds"] <= 3600:
        raise ValueError("invalid_mention_cache")
    manual = value["manual_uids"]
    if (not isinstance(manual, list) or len(manual) > 5 or len(set(map(str, manual))) != len(manual)
            or any(not isinstance(uid, str) or not re.fullmatch(r"[1-9]\d{0,19}", uid)
                   or uid == str(config.get("bot_uid")) for uid in manual)):
        raise ValueError("invalid_manual_mention_uid")
    return value


def contact(uid, name, source):
    uid, name = str(uid), str(name)
    if (not re.fullmatch(r"[1-9]\d{0,19}", uid) or not 1 <= len(name) <= 40
            or re.search(r"[@＠\s\x00-\x1f\x7f]|https?://|www\.", name, re.IGNORECASE)
            or name.casefold() in {"all", "everyone", "here", "全体成员", "所有人"}
            or source not in ("mutual", "manual")):
        raise Denied("invalid_mention_contact")
    return {"uid": uid, "name": name, "source": source}


def registry(store, config, api, now, *, force=False):
    settings = policy(config)
    if settings["mode"] == "off":
        return []
    fingerprint = hashlib.sha256(json.dumps([config["bot_uid"], settings], sort_keys=True).encode()).hexdigest()
    cached = store.get("mention_registry", {})
    if not force and cached.get("policy") == fingerprint and cached.get("expires_at", 0) > now:
        return cached.get("contacts", [])
    try:
        found = {}
        # UIDs entered manually are confirmed by the user; live profile lookup
        # obtains their exact current names. Never search an ambiguous nickname.
        for uid in settings["manual_uids"]:
            user = api.profile(uid)
            if str(user["uid"]) != uid:
                raise Denied("mention_profile_mismatch")
            found[uid] = contact(uid, user["name"], "manual")
        if settings["mode"] == "mutual_and_manual":
            for user in api.mutuals(str(config["bot_uid"]))[:20]:
                uid = str(user["uid"])
                if uid != str(config["bot_uid"]) and uid not in found:
                    found[uid] = contact(uid, user["name"], "mutual")
        contacts = list(found.values())[:20]
        store.put("mention_registry", {"policy": fingerprint, "checked_at": now,
                  "expires_at": now + settings["cache_seconds"], "contacts": contacts, "status": "ready"})
        return contacts
    except Exception:  # noqa: BLE001 - any failed contact read disables mentions, with no stale fallback.
        store.put("mention_registry", {"policy": fingerprint, "checked_at": now,
                  "expires_at": now + 60, "contacts": [], "status": "read_failed"})
        return []


class MentionText(str):
    """Exact display text plus one explicit UID; raw body never contains @."""
    def __new__(cls, body, selected):
        from .core import safe_text

        body = safe_text(body)
        if not isinstance(selected, list) or len(selected) != 1:
            raise Denied("mention_count_exceeds_limit")
        chosen = contact(**selected[0])
        rendered = "@" + chosen["name"] + " " + body
        if len(rendered) > 250:
            raise Denied("invalid_generated_text")
        value = super().__new__(cls, rendered)
        value.body, value.mentions = body, [chosen]
        return value

    def checked(self):
        rebuilt = MentionText(self.body, self.mentions)
        if str(rebuilt) != str(self):
            raise Denied("invalid_structured_mention")
        return self


def select(body, uids, candidates, kind):
    if not isinstance(uids, list) or len(uids) > 1 or any(not isinstance(uid, str) for uid in uids):
        raise Denied("invalid_mention_intent")
    if not uids:
        return body
    if kind not in ("dynamic", "proactive", "reply"):
        raise Denied("mentions_not_allowed_for_action")
    known = {item["uid"]: item for item in candidates}
    if uids[0] not in known:
        raise Denied("unapproved_mention_uid")
    return MentionText(body, [known[uids[0]]])


def verify_before_send(text, config, store, api, now, kind):
    if not isinstance(text, MentionText):
        return text
    text.checked()
    if kind not in ("dynamic", "proactive", "reply"):
        raise Denied("mentions_not_allowed_for_action")
    fresh = {row["uid"]: row for row in registry(store, config, api, now, force=True)}
    for row in text.mentions:
        if fresh.get(row["uid"]) != row:
            raise Denied("mention_contact_changed")
        profile = api.profile(row["uid"])
        if str(profile["uid"]) != row["uid"] or profile["name"] != row["name"]:
            raise Denied("mention_profile_changed")
    return text


def dynamic_contents(text):
    if isinstance(text, MentionText):
        text.checked()
        person = text.mentions[0]
        return [{"raw_text": "@" + person["name"] + " ", "type": 2, "biz_id": person["uid"]},
                {"raw_text": text.body, "type": 1, "biz_id": ""}]
    return [{"raw_text": str(text), "type": 1, "biz_id": ""}]


def comment_fields(text):
    if isinstance(text, MentionText):
        text.checked()
        return {"at_name_to_mid": json.dumps({row["name"]: int(row["uid"]) for row in text.mentions}, ensure_ascii=False)}
    return {}
