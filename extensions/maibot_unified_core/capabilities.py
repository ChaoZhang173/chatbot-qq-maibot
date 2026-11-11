"""What she can and cannot actually do right now. Tool registration is static per platform (plugin.py); what varies
is whether each tool is usable here, so every entry below is gated by the same switch or per-conversation check
the tool itself enforces. One source for QQ replies, proactive chat and Passport voice, so no channel invents a
capability another channel lacks, and adding a tool updates every channel at once."""
from __future__ import annotations

# Things no channel has a tool for. Listed explicitly: absence of a tool is not, by itself, proof of inability.
MISSING = ('画新图、做新表情包、修图或做动图：没有任何生图/修图工具，只能发已有的图',
           '写代码、运行程序、改自己的系统或配置',
           '直接看见或听见现实世界：只能通过对方发来的文字图片、或联网查到的信息了解',
           '在工具之外自己计时、盯着时间或到点行动')

RULE = ('能力边界：上面是你此刻真实拥有和没有的能力，以工具回执为准。没有工具的事不答应、不说“在做”“做好了”“明天给你”；'
        '想要又做不到时，怎么反应由你当下的心情和性格决定——照实说、眼馋、跟主人提想要这个功能（只有主人能给你装）、'
        '或者就此打住都可以，不需要每次一样，也不必每次都提。可以嘴硬耍赖，但不编造能力、进度和共同经历。')


def _qq_destination(service, session):
    proactive = getattr(service, 'qq_proactive', None)
    try:
        return next((d for d in proactive.destinations() if d['session'] == session), None) if proactive else None
    except Exception:
        return None


def available(service, session, platform='qq'):
    """Capabilities that are actually wired for this conversation (tool names in brackets)."""
    policy = service.policy
    can = ['发文字消息']
    if platform == 'qq':
        try:
            from .qq_voice import get as qq_voice
            voice = qq_voice(service).enabled(session)      # per-conversation eligibility, not only the global switch
        except Exception:
            voice = False
        if voice:
            can.append('按自己的意愿用语音说话（qq_voice_reply）')
        media = getattr(service, 'media', None)
        try:
            images = bool(media and media.authorized(session))   # the same check the image/gallery tools enforce
        except Exception:
            images = False
        if images:
            can.append('从自己的长期图库挑图或表情包发送、把聊天里的图收藏进图库（gallery_search/gallery_send/gallery_save）')
            can.append('把当前会话最近的图片或网上已有的公开图片发出来（public_image_import/qq_send_image）')
        if _qq_destination(service, session):
            can.append('用qq_agenda设定时提醒（对方提出或答应后）、给自己记“待会儿想找对方”和答应过要做的事')
        can.append('读取自己B站的动态与互动（bilibili_activity）')
    elif platform == 'passport':
        can.append('在这台设备上设语音提醒和叫醒（passport_reminder）')
    if policy.get('web_enabled'):
        can.append('联网搜索和读网页，可以查天气、新闻等（public_web_search/public_web_read）')
    can.append('查自己的记忆和做过的事（unified_memory/unified_activity）')
    return can


def text(service, session, platform='qq'):
    can = available(service, session, platform)
    return ('\n【你现在能做的】' + '；'.join(can) + '。\n【你现在做不到的】' + '；'.join(MISSING) + '。\n' + RULE)
