"""Free-form overall emotion; spoken rhythm lives in the reply punctuation."""

RULE = '由你依据当前人格、聊天内容和当下心情自主决定语音语气。voice_style可用一句简短的整体语气描述，也可留空让声音自然表达；只给语音合成使用，不写进回复正文。不要写逐字重读或局部停顿等播音动作指令。mood表示设备表情。'


def normalize(value):
    return value.strip() if isinstance(value, str) else ""
