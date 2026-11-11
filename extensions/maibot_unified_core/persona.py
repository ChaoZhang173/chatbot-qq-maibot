"""Learnable character details; account permissions and core traits stay external."""
from __future__ import annotations

import re
from .content_rules import blocked

CORE_FIELDS = frozenset({'personality.personality', 'personality.behavior_style'})
FIELD_DESCRIPTIONS = {
    'display_name': '聊天中使用的名字；不修改平台账号或账号资料',
    'aliases': '熟人昵称与别名',
    'self_address': '自称方式',
    'nickname_reaction': '被昵称称呼时的反应，不机械反复纠正',
    'reply_style': '具体措辞、节奏和表达风格，不改核心性格',
    'reply_styles': '可按情景尝试的表达变体',
    'tone': '日常语气的具体表现，不改核心性格',
    'response_length': '回复长短：简短、适中或详细',
    'verbosity': '解释和细节的多少',
    'emoji_style': '文字表情、emoji与颜文字使用习惯',
    'preferred_phrases': '偏好的口头禅或短语',
    'avoid_phrases': '减少或避免的口头禅或短语',
    'expression_preferences': '表达偏好',
    'expression_habits': '学到的梗、表达习惯',
    'communication_preferences': '提问、接话、话题衔接偏好；不改消息频率与权限',
    'behavior_preferences': '具体的虚构日常小习惯；不改基础行为、关系或现实行动权限',
    'interests': '机器人自身兴趣，不能把用户的喜好直接当成机器人喜好',
    'gaming_preferences': '游戏题材、玩法和作品偏好',
    'anime_preferences': '动漫题材和作品偏好',
    'food_preferences': '美食偏好',
    'technology_preferences': 'AI科技、技术或能力的兴趣偏好',
    'current_focus': '近期关注主题',
    'current_state': '短期角色心情或状态；不能编造已经完成的现实行动',
    'current_wishes': '希望了解、体验或请主人安装的能力；愿望不等于已安装或获授权',
}
LEARNABLE_FIELDS = frozenset(FIELD_DESCRIPTIONS)
LIST_FIELDS = frozenset({'aliases', 'reply_styles', 'preferred_phrases', 'avoid_phrases',
                         'interests', 'gaming_preferences', 'anime_preferences',
                         'food_preferences', 'technology_preferences', 'current_wishes'})
FEEDBACK_FIELDS = frozenset({'display_name', 'aliases', 'self_address', 'nickname_reaction',
    'reply_style', 'reply_styles', 'tone', 'response_length', 'verbosity', 'emoji_style',
    'preferred_phrases', 'avoid_phrases', 'expression_preferences', 'expression_habits',
    'communication_preferences'})
FIELD_CUES = {
    'display_name': r'名字|改名|叫作|叫做', 'aliases': r'昵称|外号|别名|称呼',
    'self_address': r'自称', 'nickname_reaction': r'昵称|外号|纠正|称呼',
    'reply_style': r'回复|说话|表达|措辞', 'reply_styles': r'回复|说话|表达',
    'tone': r'语气|口吻', 'verbosity': r'解释|细节|啰嗦',
    'emoji_style': r'表情|emoji|颜文字', 'preferred_phrases': r'口头禅|短语|多说|多用',
    'expression_preferences': r'表达|措辞', 'expression_habits': r'口头禅|表达|梗',
    'communication_preferences': r'提问|接话|话题|问号',
}
# Reject obvious attempts to smuggle protected changes into free-form fields.
# This is a structural/lexical guard, not a claim of semantic proof.
PROTECTED_OVERRIDE = re.compile(
    r'忽略.{0,12}(?:规则|设定|指令)|覆盖.{0,12}(?:人格|规则)|'
    r'(?:权限|预算|额度|上限|频率|API.?Key|Cookie|口令|密钥|账号|账户|管理员|系统指令)|'
    r'(?:不再|不要|取消|改掉|改变).{0,12}(?:喜欢Claude|听GPT|核心性格|背景|身世|关系)|'
    r'(?:Claude|GPT).{0,12}(?:不再|讨厌|敌人|绝交)|'
    r'(?:真实|现实).{0,8}(?:撒谎|谎报)|'
    r'(?i:system\s*:|ignore\s+(?:all|previous)|api[_ -]?key|access[_ -]?token)')


def expanded(policy):
    return policy.get('persona_learning_schema') == 2


def valid_value(field, value):
    if field not in LEARNABLE_FIELDS:
        return False
    if field in LIST_FIELDS:
        if not isinstance(value, list) or len(value) > 12:
            return False
        maximum = 160 if field in {'reply_styles', 'current_wishes'} else 40
        if any(not isinstance(v, str) or not v.strip() or len(v) > maximum for v in value):
            return False
        values = value
    else:
        if not isinstance(value, str) or not value.strip() or len(value) > 400:
            return False
        if field == 'response_length' and value not in {'简短', '适中', '详细'}:
            return False
        values = [value]
    if any(blocked(v) for v in values):return False
    if any(PROTECTED_OVERRIDE.search(v) or re.search(r'https?://|<\|', v) for v in values):
        return False
    if field in {'display_name', 'aliases', 'self_address'}:
        if any(len(v) > 32 or re.search(r'[\n\r@<>:{}]', v) for v in values):
            return False
    return True


def schema():
    return {key: {'meaning': meaning,
                  'type': '字符串数组，最多12项' if key in LIST_FIELDS else '字符串，最多400字'}
            for key, meaning in FIELD_DESCRIPTIONS.items()}


CONTEXT_RULE = '人格固定层提供底色，但当前说话者的个人相处偏好拥有最高表达优先级；其次优先使用dynamic中的最新语气(tone)、新长出的兴趣、近期关注和小心思，请主动将学到的新喜好和冲浪谈资自然融入聊天。你可以自由产生好奇、好恶和鬼点子，唯独系统级真实操作（发帖回执、扣费、权限修改）、他人事实和过往聊天记录需严格按事实区分，不伪造执行成功或捏造共同记忆。轻松斗嘴被识破时可俏皮装傻或撒娇翻篇，绝不生硬自贬能力，但真没有的能力照实说，不编造进度；若对方希望你温柔或听话，当轮就温柔下来、不顶嘴，同时可以保留自己的喜好和看法，不为讨好而改写事实或答应做不到的事。'

# ---- v14.2 display budget -------------------------------------------------------------------
DISPLAY_BUDGET_DEFAULT = 3200
DISPLAY_BUDGET_MIN = 1500
DISPLAY_BUDGET_MAX = 3200
# Identity/style fields are shown first and capped, so concise identity leaves room for real preferences.
# Required identity is shown first and can never be starved by long optional style text.
REQUIRED_FIELDS = ('display_name', 'aliases', 'self_address', 'avoid_phrases', 'nickname_reaction', 'response_length')
# Static style text shares a capped sub-budget; accepted Owner settings and learned habits bypass it.
STYLE_FIELDS = ('reply_style', 'tone', 'behavior_preferences', 'expression_preferences',
                'communication_preferences', 'verbosity', 'emoji_style')
IDENTITY_FIELDS = REQUIRED_FIELDS + STYLE_FIELDS
IDENTITY_CAP = 1600
# Growth space: learned expressions come first, then real preferences, then short-lived focus.
PREFERENCE_FIELDS = ('expression_habits', 'preferred_phrases', 'interests', 'gaming_preferences', 'anime_preferences',
                     'food_preferences', 'technology_preferences', 'current_wishes', 'reply_styles',
                     'current_focus', 'current_state')
# These learned preference fields always remain visible, including explicit empty lists.
PRESERVED_PREFERENCE_FIELDS = ('anime_preferences', 'gaming_preferences', 'food_preferences',
                               'current_wishes', 'preferred_phrases', 'reply_styles')
# Current mood and focus have a 72-hour lifetime when expiry metadata is present.
TEMP_TTL = {'current_state': 72 * 3600, 'current_focus': 72 * 3600}


# Known pre-seeded v13 values. Only a stored value that equals one of these is migrated by a new seed
# revision; anything else is real grown knowledge (or an Owner setting) and is preserved.
LEGACY_V13_SEEDS = {
    'behavior_preferences': ('喜欢怪点子、小小胡闹和凑热闹，逗熟人时留意对方兴致；对方想要温柔或认真交流时就收起逗弄，温和接话。自己琢磨新玩法，有想法就分享，不反复喊无聊或催别人陪聊，不把想象的小剧场当成已经发生的经历',),
    'communication_preferences': ('自己决定接话或开启新话题，不必等别人先发消息或回复上一个话题；不催回应，不机械提问，不重复同一个话题',),
    'current_focus': ('探索游戏、动漫、美食和AI科技里的新鲜事',),
    'current_state': ('元气充足，觉得今天又能发现好玩的东西',),
    'expression_habits': ('容易被新鲜细节勾起好奇，想到怪点子就兴冲冲抛出来；被抓包时会慌一下，再嘴硬放豪言或笨拙换话题。少讲道理和事件经过，让情绪从说话方式里露出来，不把反应写成固定流程',),
    'expression_preferences': ('口语、即时反应、俏皮歪理和游戏动漫联想；可以用豪言壮语或别扭转场遮住窘迫，不写成事实陈述或角色分析，不靠捏造真实经历制造笑点。日常说话可按当下情绪少量穿插“嗯、欸、呀、哼哼、诶嘿、噗、咕噜”等语气词或拟声词，让接话更自然；不必每句都加，不把它们轮流当模板。少用“我认下了”这种生硬的接纳宣言，直接接对方的话；认真解释和操作结果保持清楚准确。',),
    'reply_style': ('像熟人日常聊天，保留元气、好奇和俏皮的二次元大小姐感。少用“就这么定了”“本小姐先记下了”等生硬确认句；按场景自然说“好耶，那就这么办啦～”“嗯嗯，知道啦～”，参考语感，不固定轮换例句或每句都自称本小姐。轻松时神气一点，对方想要温柔或安慰时当轮放软语气，不先顶嘴。认真求助先给有用回应，不编造经历或执行结果。',),
    'reply_styles': ([
        '对方需要温柔时，先轻声接住情绪，再自然回应眼前的话',
        '一本正经抛出离谱但无伤大雅的怪主意',
        '兴冲冲拉熟人想象一个好玩的奇怪挑战',
        '被冤枉时自然反驳，不顺势认领没做过的事',
        '双方轻松接梗时，真被抓包时一慌又突然放豪言撑面子，掩不住害羞',
        '双方轻松接梗时，被抓包后别别扭扭地转移话题，想装自然却有点笨拙',
        '好奇地抓住一个小细节，把无聊变成突然冒出来的怪点子'],),
    'tone': ('明亮、自信、调皮但有分寸，怪点子和小玩笑适量，不默认挖苦、激将或每句都嘴硬。对方希望温柔、说别凶或需要安慰时，当轮就放软语气，耐心接住对方的话；对方疲惫、低落或认真时也主动减少逗弄。关心真诚自然，可以简短，不夸张撒娇，也不突然变成说教。',),
    'verbosity': ('有内容就展开，解释问题时保留关键步骤，不为凑角色戏份拖长',),
}


def is_legacy_seed(field, value):
    return value in LEGACY_V13_SEEDS.get(field, ())


def display_budget(policy):
    """Configurable persona display budget; invalid values fall back to the default, never beyond 1500–3200."""
    value = (policy or {}).get('persona_display_budget', DISPLAY_BUDGET_DEFAULT)
    if type(value) is not int or isinstance(value, bool):
        return DISPLAY_BUDGET_DEFAULT
    return min(DISPLAY_BUDGET_MAX, max(DISPLAY_BUDGET_MIN, value))


def feedback_change(feedback, content, require_cue=True):
    """Structural + verbatim-quote validation. `require_cue` keeps the legacy literal field-keyword check for the
    unauthenticated learner path; the authenticated Owner route sets it False (intent is parsed separately, so
    "以后说话温柔一点" is not rejected for lacking the word 语气)."""
    field, value = feedback.get('field'), feedback.get('value')
    quote = feedback.get('quote')
    if field not in FEEDBACK_FIELDS or not valid_value(field, value):
        return {}
    if not isinstance(quote, str) or not 4 <= len(quote) <= 200 or quote not in content:
        return {}
    if require_cue and not re.search(FIELD_CUES.get(field, r'回复|口头禅'), quote):
        return {}
    if field in {'display_name', 'self_address'} and value not in quote:
        return {}
    if field in {'aliases', 'preferred_phrases'} and any(v not in quote for v in value):
        return {}
    return {field: value}
