"""Local operator switches. This module cannot send or edit account bindings."""

import argparse, json, os
from .core import load_config, Store
from .login import atomic_json


def main():
    os.umask(0o077)
    p = argparse.ArgumentParser()
    p.add_argument("--config", required=True)
    p.add_argument("--state", required=True)
    p.add_argument("feature", choices=["all", "dynamic", "proactive", "reply", "dm"])
    p.add_argument("value", choices=["off", "on"])
    a = p.parse_args()
    c = load_config(a.config)
    s = Store(a.state)
    if a.feature == "all":
        if a.value == "off":
            (s.root / "PAUSE").touch(mode=0o600)
            c["paused"] = True
        else:
            if (
                not s.get("baseline_at")
                or not s.get("bound_uid")
                or s.get("pause_reason")
            ):
                raise SystemExit("先完成绑定/基线，并处理平台暂停原因")
            c["paused"] = False
            (s.root / "PAUSE").unlink(missing_ok=True)
    else:
        if a.feature == "dm" and a.value == "on" and not str(c["owner_uid"]).isdigit():
            raise SystemExit("需要明确 OWNER_UID")
        c["enabled"][a.feature] = a.value == "on"
    atomic_json(a.config, c)
    print(
        json.dumps({"paused": c["paused"], "enabled": c["enabled"]}, ensure_ascii=False)
    )


if __name__ == "__main__":
    main()
