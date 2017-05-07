import json
import logging
from aiohttp import web
from ..utils import _err, _json, strip_user_data

log = logging.getLogger(__name__)

class ChannelsEndpoint:
    """Handle channel/message related endpoints"""
    def __init__(self, server):
        self.server = server

    def register(self, app):
        _r = app.router
        _r.add_get('/api/channels/{channel_id}', self.h_get_channel)

        # NOTE: needs message stuff in GuildManager
        #_r.add_get('/api/channels/{channel_id}/messages', self.h_get_messages)
        #_r.add_get('/api/channels/{channel_id}/messages/{message_id}', self.h_get_single_message)

        #_r.add_post('/api/channels/{channel_id}/messages', self.h_post_message)
        #_r.add_patch('/api/channels/{channel_id}/messages/{message_id}',
        #               self.h_patch_message)

        #_r.add_delete('/api/channels/{channel_id}/messages/{message_id}',
        #                self.h_delete_message)

        _r.add_post('/api/channels/{channel_id}/typing', self.h_post_typing)

    async def h_get_channel(self, request):
        """`GET /channels/{channel_id}`.

        Returns a channel object
        """

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

    async def h_post_typing(self, request):
        """`POST /channels/{channel_id}/typing`.

        Dispatches TYPING_START events to relevant clients.
        Returns a HTTP empty response with status code 204.
        """

        _error = await self.server.check_request(request)
        _error_json = json.loads(_error.text)
        if _error_json['code'] == 0:
            return _error

        channel_id = request.match_info['channel_id']
        user = self.server._user(_error_json['token'])

        channel = self.server.guild_man.get_channel(channel_id)
        if channel is None:
            return _err('404: Not found')

        if user.id not in channel.guild.members:
            return _err('401: Unauthorized')

        await self.server.presence.typing_start(user.id, channel_id)
        return web.Response(status=204)
