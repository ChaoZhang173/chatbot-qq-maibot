"""Bounded public read probe without account cookies, model calls, or writes."""

import json, requests
from .api import Bili
from .core import PlatformError, dynamic_target


def main():
    b = Bili.__new__(Bili)
    b.http = requests.Session()
    b.http.headers.update(
        {"User-Agent": "Mozilla/5.0", "Referer": "https://www.bilibili.com/"}
    )
    b.nav = {}
    result = {"authenticated": False, "writes": 0, "model_calls": 0}
    try:
        d = b.request("/x/web-interface/popular", {"ps": 1, "pn": 1})
        items = d.get("list") or []
        if items:
            v = b.video(items[0]["bvid"])
            result["video"] = {
                "target": v.key,
                "url": v.url,
                "text": v.text[:500],
                "evidence": v.evidence,
            }
            try:
                feed = b.request(
                    "/x/polymer/web-dynamic/v1/feed/space", {"host_mid": v.author}
                )
                result["dynamic_candidates"] = []
                for item in (feed.get("items") or [])[:2]:
                    t = dynamic_target(item)
                    if t:
                        result["dynamic_candidates"].append(
                            {
                                "target": t.key,
                                "kind": t.kind,
                                "url": t.url,
                                "text_chars": len(t.text),
                                "images": len(t.images or []),
                                "platform_type": t.evidence,
                            }
                        )
            except PlatformError as e:
                result["dynamic_read_error"] = {"platform_code": e.code}
    except PlatformError as e:
        result["error"] = {"platform_code": e.code}
    except Exception as e:
        result["error"] = {"type": type(e).__name__}
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
