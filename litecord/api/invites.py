import logging

from aiohttp import web
from ..utils import _err, _json

log = logging.getLogger(__name__)

class InvitesEndpoint:
    def __init__(self, server):
        self.server = server
        self.guild_man = server.guild_man

    def register(self, app):
        _r = app.router
        _r.add_get('/api/invites/{invite_code}', self.h_get_invite)
        _r.add_post('/api/invites/{invite_code}', self.h_accept_invite)

    async def h_get_invite(self, request):
        """`GET /invites/{invite_code}`."""

        invite_code = request.match_info['invite_code']
        invite = self.server.guild_man.get_invite(invite_code)

        if invite is None:
            return _err(errno=10006)

        return _json(invite.as_json)

    async def h_accept_invite(self, request):
        """`POST /invites/{invite_code}`.

        Accept an invite. Returns invite object.
        """

        _error = await self.server.check_request(request)
        _error_json = json.loads(_error.text)
        if _error_json['code'] == 0:
            return _error

        invite_code = request.match_info['invite_code']
        user = self.server._user(_error_json['token'])

        invite = self.server.guild_man.get_invite(invite_code)
        if invite is None:
            return _err(errno=10006)

        if not invite.valid:
            return _err('Invalid invite')

        guild = invite.channel.guild

        try:
            success = invite.use()
            if not success:
                return _err('Error using the invite.')

            member = await guild.add_member(user)
            if member is None:
                return _err('Error adding to the guild')

            return _json(invite.as_json)
        except:
            log.error(exc_info=True)
            return _err('Error using the invite.')
