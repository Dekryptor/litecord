import json
import logging
from ..utils import _err, _json, strip_user_data

log = logging.getLogger(__name__)

class ChannelsEndpoint:
    '''
    Handle stuff over /channels/*
    '''
    def __init__(self, server):
        self.server = server

    def register(self, app):
        _r = app.router
        _r.add_get('/api/channels/{channel_id}', self.h_get_channel)

    async def h_get_channel(self, request):
        '''
        ChannelsEndpoint.h_get_channel

        Handle `GET /channels/{channel_id}`
        Returns a channel object
        '''

        _error = await self.server.check_request(request)
        _error_json = json.loads(_error.text)
        if _error_json['code'] == 0:
            return _error

        channel_id = request.match_info['channel_id']
        user = self.server._user(_error_json['token'])

        channel = self.server.guild_man.get_channel(channel_id)
        guild = channel.guild

        if user.id not in guild.members:
            return _err('401: Unauthorized')

        return _json(channel.as_json)
