"""Account capabilities are an explicit field allowlist, never arbitrary API calls."""
import re
from .content_rules import blocked,public_text
SPACE_FIELDS={'fav_video','bangumi','coins_video','likes_video','played_game','user_info','tags','disable_following','disable_show_fans','disable_show_school','live_playback'}
MESSAGE_FIELDS={'show_unfollowed_msg':{0,1},'msg_notify':{1,3},'set_like':{0,5},'set_comment':{0,1,2},'set_at':{0,1,2},'is_group_fold':{0,1},'should_receive_group':{0,1},'receive_unfollow_msg':{0,1},'ai_intercept':{0,1},'set_recv_reply':{0,1,2},'set_recv_like':{0,2},'set_new_follow':{0,2}}
HELP='可修改profile(sign签名≤70字、notice空间公告≤149字)、space(展示/隐私0隐藏1展示：fav_video/bangumi/coins_video/likes_video/played_game/user_info/tags/live_playback；disable_following/disable_show_fans/disable_show_school则1隐藏0展示；index_order保留现有板块ID仅改顺序)、folder(现有media_id、title、intro、privacy:1私密0公开)、messages(字段和值见schema)。昵称、头像和登录安全不是可用字段。'

def validate_settings(section,patch):
    if not isinstance(patch,dict) or not patch or len(patch)>12:raise ValueError('设置须为非空字段对象')
    if section=='profile':allowed={'sign','notice'}
    elif section=='space':allowed=SPACE_FIELDS|{'index_order'}
    elif section=='messages':allowed=set(MESSAGE_FIELDS)
    elif section=='folder':allowed={'media_id','title','intro','privacy'}
    else:raise ValueError('仅支持profile、space、folder、messages设置')
    if set(patch)-allowed:raise ValueError('包含未开放的账户字段；昵称和头像不可修改')
    for k,v in patch.items():
        if k in ('sign','notice','title','intro'):
            limit={'sign':70,'notice':149,'title':40,'intro':200}[k]
            if not isinstance(v,str) or not 0<len(v)<=limit or blocked(v):raise ValueError('设置文案长度或话题不符合要求')
            # An own-profile contact boundary discloses no private conversation.
            checked=v
            if section=='profile':
                checked=re.sub(r'(^|[。，！？；、\s|｜])(?:我)?(?:目前|暂时|暂)?不(?:接受|回复|接收)私聊(?=$|[。，！？；、\s|｜])',r'\1不接受单独联系',v)
            public_text(checked)
        elif k=='media_id':
            if not isinstance(v,str) or not re.fullmatch(r'[1-9][0-9]{0,19}',v):raise ValueError('需要准确的现有收藏夹media_id')
        elif k=='index_order':
            if not isinstance(v,list) or not 1<=len(v)<=25 or len(set(v))!=len(v) or any(type(i) is not int or not 1<=i<=100 for i in v):raise ValueError('invalid_space_order')
        elif type(v) is not int or v not in (MESSAGE_FIELDS[k] if section=='messages' else {0,1}):raise ValueError('invalid_setting_value')
    if section=='profile' and len(patch)!=1:raise ValueError('每次只调整签名或公告中的一项')
    if section=='space' and 'index_order' in patch and len(patch)!=1:raise ValueError('空间布局请单独调整')
    if section=='folder' and ('media_id' not in patch or len(patch)<2):raise ValueError('收藏夹设置需要media_id和至少一项修改')
    return dict(patch)

def spec(section,patch):
    return {'kinds':['account'],'target_type':'account','target':section,'text':'','prompt':'','settings':validate_settings(section,patch)}

def parse_settings(text):
    m=re.fullmatch(r'(?:请|帮我|把|将|你的|自己的|B站|b站|个人|主页|空间|\s)*(?:修改|设置|更新)?(?P<label>签名|简介|个性签名|公告)(?:修改|设置|更新|改成|改为|设为|为|成)?[：:\s]*(?P<value>[\s\S]+)',text)
    if m and re.search(r'签名|简介|公告',text):return spec('profile',{'notice' if m['label']=='公告' else 'sign':m['value'].strip('“”「」')})
    m=re.fullmatch(r'(?:设置|修改)(?:B站)?(隐私|空间|消息|收藏夹)\s*[：:]\s*(\{.*\})',text,re.S)
    if m:
        import json
        return spec({'隐私':'space','空间':'space','消息':'messages','收藏夹':'folder'}[m[1]],json.loads(m[2]))
    return None
