"""Bounded same-provider correction for experience replies, without another planner reference."""
import asyncio
import json
from .experience_policy import prompt


async def correct(service, draft, evidence, question='', group_audience=None, timeout=3.4):
    if not isinstance(draft, str) or not draft.strip():
        return None
    data={'draft':draft,'evidence':evidence,'question':question}
    if group_audience is not None:data['group_audience']=group_audience
    try:
        answer=await asyncio.wait_for(service.generate([
            {'role':'system','content':prompt('experience_reply_check')},
            {'role':'user','content':json.dumps(data,ensure_ascii=False)}],max_tokens=650,temperature=0.2),timeout)
        if not isinstance(answer, dict):return None
        text=answer.get('text');ids=answer.get('used_ids')
        known={i['id'] for i in evidence.get('items',[]) if isinstance(i,dict) and isinstance(i.get('id'),str)}
        if answer.get('supported') is not True or not isinstance(text,str) or not 1<=len(text.strip())<=1200:return None
        if not isinstance(ids,list) or any(not isinstance(i,str) or i not in known for i in ids):return None
        if evidence.get('status')=='found' and evidence.get('items') and not ids and group_audience is None:return None
        return text.strip()
    except Exception:
        return None


def fallback(evidence):
    """A timeout must not send the unchecked draft or invent an absence of activity."""
    for item in evidence.get('items',[]):
        if item.get('kind')=='read' and item.get('title'):
            return '我能确认读过《'+item['title'][:100]+'》的文字材料，具体细节还不能说得太满。'
    return '这段具体经历，我暂时没法确定。'
