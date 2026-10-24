"""Only the existing locked Bilibili worker executes claimed QQ plans."""
import json
import base64
import hashlib
from .core import Denied, PlatformError, Target, day, active
from .command_schema import validate
from .content_rules import public_text, blocked


class CommandExecutor:
    def __init__(self,engine):
        self.e=engine
        engine.s.db.execute('''CREATE TABLE IF NOT EXISTS qq_command_runs(
            id TEXT PRIMARY KEY,status TEXT NOT NULL,result TEXT NOT NULL)''')

    def process_one(self):
        e=self.e
        if not e.config().get('qq_commands_enabled'):return
        if not getattr(e.s,'_lock_held',False):raise Denied('single_writer_required')
        claim=e.unified_request({'op':'command_claim'})
        if claim.get('interpretation_required'):
            interpretation_id=claim['interpretation_required']
            try:
                prepared=e.bounded_core({'op':'command_interpret','id':interpretation_id})
            except Denied as exc:
                e.unified_request({'op':'command_result','id':interpretation_id,'result':{'status':'failed','reason':str(exc)}})
                return
            if prepared.get('status')!='ready':return
            claim=e.unified_request({'op':'command_claim'})
        job=claim.get('job')
        if not job:return
        key=job['id']
        row=e.s.db.execute('SELECT status,result FROM qq_command_runs WHERE id=?',(key,)).fetchone()
        if row:
            result=json.loads(row['result'])
            if row['status']=='executing':result={'status':'unknown','reason':'interrupted_unknown'}
            e.unified_request({'op':'command_result','id':key,'result':result});return result
        e.s.db.execute('INSERT INTO qq_command_runs VALUES(?,?,?)',(key,'executing','{}'))
        actions=[]
        try:
            spec=validate(job['spec']);c=e.config()
            if c['paused'] or (e.s.root/'PAUSE').exists() or e.s.get('pause_reason'):raise Denied('paused')
            e.sender.identity(c)
            if spec['target_type']=='short':
                from .links import resolve
                resolved=resolve(spec['target']);spec=validate({**spec,'target_type':resolved['target_type'],'target':resolved['target']})
            if spec['target_type']=='account':
                from .account import AccountSettings
                settings=AccountSettings(e);target=None
                material=json.dumps(settings.snapshot(spec['target'],spec['settings']),ensure_ascii=False)
            elif spec['target_type']=='video':
                identifier=spec['target'][2:] if spec['target'].startswith('av') else spec['target']
                target=e.api.video(identifier,with_subtitle='browse' in spec['kinds'])
                material=target.text
            elif spec['target_type']=='live':
                room=e.live_chat().api.room(spec['target'])
                material=json.dumps(room,ensure_ascii=False)
                target=Target('live',room['room_id'],0,room['uid'],material,'https://live.bilibili.com/'+room['room_id'],[],room['room_id'],'room_metadata_text_only')
            elif spec['target_type']=='user':
                profile=e.api.profile(spec['target'])
                if str(profile['uid'])!=spec['target']:raise Denied('profile_mismatch')
                material=profile['name']+'\n'+profile.get('bio','')
                target=Target('user',spec['target'],0,spec['target'],material,'https://space.bilibili.com/'+spec['target'],[],spec['target'],'public_profile')
            else:target=None;material=spec['text']
            if blocked(material):raise Denied('fixed_topic_restriction')
            review=e.bounded_core({'op':'command_review','id':key,'material':material})
            if review.get('allowed') is not True:raise Denied(review.get('reason') or 'command_review_declined')
            media=[]
            for image_id in spec.get('images',[]):
                asset=e.unified_request({'op':'command_asset','id':key,'image_id':image_id})
                raw=base64.b64decode(asset['image_base64'],validate=True)
                if len(raw)>850000 or hashlib.sha256(raw).hexdigest()!=asset['sha256']:raise Denied('invalid_image_asset')
                media.append({'raw':raw,'width':asset['width'],'height':asset['height']})
            event={'event_id':'qq-command:'+key,'platform':'bilibili','bot_account_id':str(c['bot_uid']),
                'actor_id':str(c['bot_uid']),'origin':'system_task','event_type':'qq_command_execution',
                'conversation_id':target.key if target else 'self:'+str(c['bot_uid']),
                'content':'','source_id':key,'sharing_scope':'public','learning_allowed':False,'occurred_at':e.now()}
            e.outbox.event(event)
            for kind in spec['kinds']:
                # Revocation/policy checks happen again in the shared executors at dispatch.
                if kind=='account':
                    actions.append(settings.execute('qq-command:'+key+':account',spec['target'],spec['settings']))
                elif kind=='browse':
                    if not active(c,e.now()):raise Denied('outside_active_hours')
                    if e.config()!=c:raise Denied('policy_changed')
                    action_id='qq-command:'+key+':browse'
                    if spec['target_type']=='live':
                        visit=e.live_chat().visit(target.oid,spec.get('stay_seconds'),forced=bool(job.get('forced')))
                        detail={'target_type':'live','room_id':visit['room_id'],'ends':visit['ends']}
                    else:
                        detail={'target_type':'video','material_evidence':target.evidence,'summary':review.get('summary','')}
                    read_event=e.event('browse',action_id,target.text,target)
                    e.outbox.event(read_event)
                    e.s.db.execute('INSERT OR IGNORE INTO actions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                        (action_id,str(c['bot_uid']),'browse',target.key,target.author,target.key,day(e.now()),e.now(),'success','',
                         'read:'+target.key,target.url,''))
                    e.s.db.execute('INSERT OR IGNORE INTO action_context VALUES(?,?,?,?)',
                        (action_id,json.dumps(read_event),str(review['persona_version']),'[]'))
                    actions.append({'kind':'browse','status':'success','action_id':action_id,**detail})
                elif kind in ('like','favorite','coin','follow'):
                    items=e.engagement().execute_command(target,[kind],event,job,str(review['persona_version']))
                    actions.extend(items)
                else:
                    text=public_text(spec['text'] or review.get('text',''))
                    action_id='qq-command:'+key+':'+kind
                    e.outbox.event({**event,'action_id':action_id})
                    result=e.sender.deliver('proactive' if kind=='comment' else 'dynamic',target,action_id,lambda:text,**({'media':media} if media else {}))
                    row=e.s.db.execute('SELECT error FROM actions WHERE id=?',(action_id,)).fetchone()
                    actions.append({**result,'kind':kind,'action_id':action_id,'reason':result.get('reason') or (row['error'] if row else '')})
                if any(a.get('status') in ('unknown','paused') for a in actions):break
            succeeded=[a.get('status')=='success' or (a.get('status')=='skipped' and a.get('reason')=='already_present') for a in actions]
            status='success' if actions and all(succeeded) and len(actions)==len(spec['kinds']) else 'unknown' if any(a.get('status')=='unknown' for a in actions) else 'partial' if any(succeeded) else 'failed'
            result={'status':status,'actions':actions}
        except (Denied,ValueError,PlatformError) as exc:
            result={'status':'partial' if actions else 'failed','reason':str(exc) if not isinstance(exc,PlatformError) else 'platform_'+str(exc.code),'actions':actions}
        except Exception:
            # Individual writes persist their own unknown receipt before errors escape.
            result={'status':'unknown' if actions else 'failed','reason':'command_processing_failed','actions':actions}
        e.s.db.execute('UPDATE qq_command_runs SET status=?,result=? WHERE id=?',(result['status'],json.dumps(result,ensure_ascii=False),key))
        e.sync()
        e.unified_request({'op':'command_result','id':key,'result':result})
        return result
