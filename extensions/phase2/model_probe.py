"""Two bounded real model probes, with synthetic content; never posts to Bilibili."""

import argparse, json
from pathlib import Path
from .unified import UnifiedModel, initialize
from .core import Store, load_config, safe_text, Denied


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--private", required=True)
    p.add_argument("--state", required=True)
    a = p.parse_args()
    private = Path(a.private)
    s = Store(a.state)
    c = load_config(private / "config.json")
    initialize(s)
    m = UnifiedModel(s, c)
    fixtures = [
        (
            "video",
            {
                "title": "用小实验理解大模型上下文窗口",
                "description": "作者用多轮问答比较保留上下文与清空上下文的输出差异。",
                "evidence": "synthetic_title_and_description_only",
            },
        ),
        (
            "dynamic",
            {
                "text": "周末尝试做草莓布丁，第一次脱模没成功，第二次终于完整啦。",
                "evidence": "synthetic_public_dynamic_text",
            },
        ),
    ]
    result = []
    for kind, data in fixtures:
        try:
            event = {"event_id": "bilibili:synthetic-probe:" + kind,
                     "platform": "bilibili", "bot_account_id": str(c["bot_uid"]),
                     "conversation_id": "debug:synthetic-probe", "actor_id": "system",
                     "origin": "system_task", "event_type": "debug_probe",
                     "content": json.dumps(data, ensure_ascii=False), "source_id": kind,
                     "source_url": "", "sharing_scope": "conversation",
                     "learning_allowed": False, "action_id": "", "occurred_at": 0}
            answer = safe_text(m.generate("proactive", data, event=event))
            result.append({"kind": kind, "status": "generated", "text": answer})
        except Denied as e:
            result.append({"kind": kind, "status": "skipped", "reason": str(e)})
    print(
        json.dumps(
            {
                "fixture_notice": "合成材料，仅模型与人设验证，不是B站真实读取/发送证据",
                "results": result,
                "usage": s.summary()["usage"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
