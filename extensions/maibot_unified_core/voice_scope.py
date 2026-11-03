"""Per-RPC voice context; simultaneous QQ calls keep their ordinary context."""
from contextvars import ContextVar
from functools import wraps

CURRENT = ContextVar('unified_voice_message', default=None)

# Voice turn time management (seconds before the Host turn deadline). A new network request starts only while at least
# MIN_REQUEST_SECONDS remain after reserving REPLY_RESERVE_SECONDS for the final Planner round + replyer; inside the last
# FINISH_REMAINING_SECONDS the Planner is offered only the existing reply tool (see plugin.py).
REPLY_RESERVE_SECONDS = 8.0
MIN_REQUEST_SECONDS = 2.0
FINISH_REMAINING_SECONDS = 12.0


class VoiceAwareMessages(dict):
    def get(self, key, default=None):
        current = CURRENT.get()
        if current is not None and current['session'] == key:
            return current['message']
        return super().get(key, default)


def voice_scoped(fn=None, *, friend=False):
    """Bind a hook/tool to the server's record of the active voice turn.

    Authority comes from that record (`scope['role']`, set from the resolved device -> profile route), never from
    the Host, the model or a display name. Default is OWNER ONLY: a non-owner voice scope is refused unless the
    entry point is explicitly marked `friend=True` (context hooks and the ordinary read/search tools)."""
    def decorate(fn):
        @wraps(fn)
        async def wrapper(self, *args, **kwargs):
            key = kwargs.pop('_unified_voice_key', None)
            if key is None:
                return await fn(self, *args, **kwargs)
            session = str(kwargs.get('session_id') or kwargs.get('stream_id') or '')
            voice = getattr(self.service, 'voice', None)
            scope = voice.scope_for(key, session) if voice else None
            if scope is None:
                raise PermissionError('voice_scope_not_active')
            if scope.get('role') != 'owner' and not friend:
                raise PermissionError('voice_role_not_permitted')
            token = CURRENT.set(scope)
            try:
                return await fn(self, *args, **kwargs)
            finally:
                CURRENT.reset(token)
        return wrapper
    return decorate(fn) if fn is not None else decorate
