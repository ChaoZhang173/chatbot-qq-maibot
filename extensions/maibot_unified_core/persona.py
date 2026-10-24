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


CONTEXT_RULE = (
    '人格固定层只有核心性格、背景关系和基础行为风格。dynamic是当前可学习的人格细节，'
    '包括聊天名字、别名、自称、兴趣、愿望、习惯和具体回复风格；在这些细节上优先采用dynamic，'
    'Host中的默认名字和回复风格仅作缺省，不自动修改平台账号资料。'
    '动态细节不能覆盖固定层，不能改变权限、预算、隐私范围或真实能力。'
    '装懂、夸海口和被抓包属于无伤大雅的角色喜剧；真实执行、费用、记忆证据和认真求助仍须如实。'
)


def feedback_change(feedback, content):
    field, value = feedback.get('field'), feedback.get('value')
    quote = feedback.get('quote')
    if field not in FEEDBACK_FIELDS or not valid_value(field, value):
        return {}
    if not isinstance(quote, str) or not 4 <= len(quote) <= 200 or quote not in content:
        return {}
    if not re.search(FIELD_CUES.get(field, r'回复|口头禅'), quote):
        return {}
    if field in {'display_name', 'self_address'} and value not in quote:
        return {}
    if field in {'aliases', 'preferred_phrases'} and any(v not in quote for v in value):
        return {}
    return {field: value}
