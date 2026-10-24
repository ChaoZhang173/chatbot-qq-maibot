"""Bounded natural-language commands; targets and authority are never guessed."""
import re
from urllib.parse import urlsplit
from .content_rules import blocked,public_text
from .account_schema import parse_settings,validate_settings
PREFIX='【命令】'
KINDS={'点赞':'like','收藏':'favorite','投币':'coin','关注':'follow','留言':'comment','评论':'comment','发动态':'dynamic','发布动态':'dynamic','浏览':'browse'}
VIDEO=re.compile(r'(?<![A-Za-z0-9])(?:BV[A-Za-z0-9]{10}|av[0-9]{1,20})(?![A-Za-z0-9])')
# Supported target paths are ASCII. App share copy may append Chinese prose
# (e.g. "观看直播") without a space; it is not part of the URL.
URL=re.compile(r'https?://[^\s<>“”「」【】\[\]（）()，。！？\u3400-\u9fff]+',re.I)
BROWSE=re.compile(r'看(?:看|一下|一看)?|观看|浏览|读(?:一下)?|进入|进(?:入)?(?:这个|那个|一下|去)?直播间|逛|蹲|待|呆|聊|听听')


def link_target(url):
    p=urlsplit(url.rstrip('.,;!'))
    if p.scheme not in ('https','http') or p.username or p.password or p.port not in (None,443):raise ValueError('只支持普通B站公开视频或直播链接')
    host=(p.hostname or '').lower();path=p.path.rstrip('/')
    if host in ('www.bilibili.com','bilibili.com','m.bilibili.com'):
        m=re.fullmatch(r'/video/(BV[A-Za-z0-9]{10}|av[0-9]{1,20})',path)
        if m:return {'target_type':'video','target':m[1],'url':'https://www.bilibili.com/video/'+m[1]}
    if host=='live.bilibili.com':
        m=re.fullmatch(r'/(?:blanc/|h5/)?([1-9][0-9]{0,15})',path)
        if m:return {'target_type':'live','target':m[1],'url':'https://live.bilibili.com/'+m[1]}
    if host=='space.bilibili.com' and re.fullmatch(r'/[1-9][0-9]{0,19}',path):
        return {'target_type':'user','target':path[1:],'url':'https://space.bilibili.com'+path}
    if host=='b23.tv' and re.fullmatch(r'/[A-Za-z0-9]{1,32}',path):
        return {'target_type':'short','target':'https://b23.tv'+path,'url':'https://b23.tv'+path}
    raise ValueError('链接不是支持的B站视频、直播间、主页或b23.tv短链')


def targets(text,strict=True):
    found=[]
    for match in URL.finditer(text):
        try:found.append(link_target(match[0]))
        except ValueError:
            if strict:raise
    plain=URL.sub(' ',text)
    found += [{'target_type':'video','target':v,'url':'https://www.bilibili.com/video/'+v} for v in VIDEO.findall(plain)]
    found += [{'target_type':'user','target':v,'url':'https://space.bilibili.com/'+v} for v in re.findall(r'UID\s*[：:=]?\s*([1-9][0-9]{0,19})(?![0-9])',plain,re.I)]
    found += [{'target_type':'live','target':v,'url':'https://live.bilibili.com/'+v} for v in re.findall(r'(?:直播间(?:号|ID)?|房间号)\s*[：:=]?\s*([1-9][0-9]{0,15})(?![0-9])',plain,re.I)]
    return list({(r['target_type'],r['target']):r for r in found}.values())


def parse(text,recent=()):
    if not isinstance(text,str) or not 0<len(text)<=1600:raise ValueError('命令过长或为空')
    text=text.strip()
    if text.startswith(PREFIX):text=text[len(PREFIX):].lstrip('：: ')
    setting=parse_settings(text)
    if setting:return validate(setting)
    text=re.sub(r'发(?:布)?(?:一条|条|个)?图文动态','发动态',text)
    text=re.sub(r'发(?:布)?(?:一条|条|个)动态','发动态',text)
    text=re.sub(r'留(?:下)?(?:一条|条|个)(?:评论|言)','留言',text)
    text=re.sub(r'发(?:布)?(?:一条|条|个)?评论','评论',text)
    split=re.search(r'(?:发布动态|发动态|留言|评论|说一句|说)(?:内容)?[：:]\s*',text)
    quoted=re.search(r'(?:发布动态|发动态|留言|评论|说一句|说)\s*[“「](.+)[”」]\s*$',text) if not split else None
    body=text[split.end():].strip().strip('“”「」') if split else quoted[1] if quoted else ''
    head=text[:split.end()] if split else text[:quoted.start(1)] if quoted else text
    if blocked(text):raise ValueError('触及固定话题限制，不能执行')
    clean=URL.sub(' [B站链接] ',head)
    bare_reference=not re.sub(r'\[B站链接\]|'+VIDEO.pattern,'',clean).strip()
    clean=re.sub(r'别忘了|不要忘记','记得',clean)
    if re.search(r'取消|取关|删除|撤销|(?:如果|假如|比如|例如|示例)',clean):raise ValueError('暂不支持条件执行、撤销或示例命令，请分开明确说明')
    # A negative action is not an affirmative request. Tracking-query '?' and
    # words such as '特别' are not negation or a reason to reject a link.
    clean=re.sub(r'(?:不要|(?<!特)别|不需要|不用|禁止)[^，,。；;\n]*', ' ',clean)
    kinds=[]
    for label,kind in KINDS.items():
        if label in clean and kind not in kinds:kinds.append(kind)
    if re.search(r'点(?:个|一下|一?个)?赞|赞一下',clean) and 'like' not in kinds:kinds.append('like')
    if re.search(r'投(?:一|1)(?:枚|个)?(?:硬币|币)',clean) and 'coin' not in kinds:kinds.append('coin')
    if body and re.search(r'说一句|说[：:]|说\s*[“「]',clean) and 'comment' not in kinds:kinds.append('comment')
    if 'dynamic' in kinds:
        if len(kinds)!=1:raise ValueError('请单独发送发动态命令')
        return validate({'kinds':kinds,'target_type':'self','target':'','text':public_text(body) if body else '', 'prompt':head[:200] if not body else ''})
    refs=targets(head)
    if not refs and 'follow' in kinds:
        m=re.search(r'关注(?:一下|用户|账号|UP主|up主)?\s*([1-9][0-9]{0,19})(?![0-9])',clean)
        if m:refs=[{'target_type':'user','target':m[1]}]
    if not refs and re.search(r'刚才|刚发|上面|这个|那个|该链接',clean):
        refs=list(recent)
        if '直播' in clean:refs=[r for r in refs if r['target_type'] in ('live','short')]
        elif '视频' in clean:refs=[r for r in refs if r['target_type'] in ('video','short')]
    if len(refs)!=1:raise ValueError('请指定一个视频/直播链接、BV号、直播间号或关注UID；当前群引用不唯一时请重贴链接')
    ref=refs[0];typ=ref['target_type']
    if not kinds and (BROWSE.search(clean) or bare_reference):kinds=['browse']
    elif BROWSE.search(clean) and not kinds==['follow'] and typ!='user':kinds.insert(0,'browse') if 'browse' not in kinds else None
    if not kinds:raise ValueError('支持看视频、进入直播间、关注、留言、发动态、点赞、投币、收藏，请写明动作')
    if body and 'comment' not in kinds:raise ValueError('该操作不需要公开文案')
    if 'coin' in kinds:
        amount=re.search(r'投(?:币)?\s*([0-9]+|[二两三四五六七八九十])\s*(?:枚|个|币)',clean)
        if amount and amount[1]!='1':raise ValueError('每次命令每个视频投1枚，且仍受余额和每日上限限制')
    spec={'kinds':kinds,'target_type':typ,'target':ref['target'],'text':public_text(body) if body else '', 'prompt':head[:200] if 'comment' in kinds and not body else ''}
    minutes=re.search(r'([0-9]{1,3})\s*分钟',clean)
    if minutes and typ in ('live','short'):spec['stay_seconds']=int(minutes[1])*60
    return validate(spec)


def validate(spec):
    if not isinstance(spec,dict) or set(spec)-{'images','stay_seconds','settings'}!={'kinds','target_type','target','text','prompt'}:raise ValueError('invalid_command_spec')
    kinds=spec['kinds'];target=spec['target'];typ=spec['target_type']
    if kinds==['account'] and typ=='account':
        if set(spec)!={'kinds','target_type','target','text','prompt','settings'} or spec['text'] or spec['prompt']:raise ValueError('invalid_account_spec')
        validate_settings(target,spec['settings']);return spec
    if 'settings' in spec:raise ValueError('unexpected_settings')
    if not isinstance(kinds,list) or not kinds or len(set(kinds))!=len(kinds) or any(k not in KINDS.values() for k in kinds):raise ValueError('invalid_command_kinds')
    if typ=='self':
        if kinds!=['dynamic'] or target:raise ValueError('invalid_self_command')
    elif typ=='user':
        if kinds!=['follow'] or not re.fullmatch(r'[1-9][0-9]{0,19}',str(target)):raise ValueError('主页仅支持明确关注该用户')
    elif typ=='video':
        if not VIDEO.fullmatch(str(target)) or 'dynamic' in kinds:raise ValueError('invalid_video_command')
    elif typ=='live':
        if kinds!=['browse'] or not re.fullmatch(r'[1-9][0-9]{0,15}',str(target)):raise ValueError('直播链接目前支持进入文字聊天室，不支持礼物、投币或视频操作')
    elif typ=='short':
        if link_target(str(target))['target_type']!='short' or 'dynamic' in kinds:raise ValueError('invalid_short_command')
    else:raise ValueError('invalid_command_target')
    if 'stay_seconds' in spec and (typ not in ('live','short') or kinds!=['browse'] or type(spec['stay_seconds']) is not int or not 600<=spec['stay_seconds']<=3600):raise ValueError('直播停留时间请设为10至60分钟')
    if not isinstance(spec['prompt'],str) or len(spec['prompt'])>200 or blocked(spec['prompt']):raise ValueError('invalid_command_prompt')
    if 'dynamic' in kinds or 'comment' in kinds:
        if spec['text']:public_text(spec['text'])
        elif not spec['prompt']:raise ValueError('missing_public_content')
    elif spec['text']:raise ValueError('unexpected_command_text')
    images=spec.get('images',[])
    if not isinstance(images,list) or len(images)>1 or any(not isinstance(i,str) or not re.fullmatch(r'[a-f0-9]{64}',i) for i in images):raise ValueError('invalid_command_images')
    if images and kinds!=['dynamic']:raise ValueError('图片目前仅支持图文动态')
    return spec
