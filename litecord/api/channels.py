import json
import logging
import time
import collections

from aiohttp import web
from voluptuous import Schema, Optional, All, Length, Range, REMOVE_EXTRA

from ..utils import _err, _json
from ..snowflake import get_snowflake, snowflake_time
from ..ratelimits import ratelimit
from ..decorators import auth_route
from ..snowflake import get_snowflake

log = logging.getLogger(__name__)

# 2 weeks
BULK_DELETE_LIMIT = 1209600

class ChannelsEndpoint:
    """Handle channel/message related endpoints.

    Attributes
    ----------
    nonces: Dict[list]
        Cache for used nonces.
    """
    def __init__(self, server):
        self.server = server
        self.guild_man = server.guild_man

        self.nonces = collections.defaultdict(list)

        self.channel_edit_base = Schema({
            'name': All(str, Length(min=2, max=100)),
            'position': int
        })

        self.textchan_editschema = self.channel_edit_base.extend({
            'topic': All(str, Length(min=0, max=1024))
        })

        self.voicechan_editschema = self.channel_edit_base.extend({
            'bitrate': All(int, Range(min=8000, max=96000)),
            'user_limit': All(int, Range(min=0, max=99)),
        })

        self.register()

    def register(self):
        self.server.add_get('channels/{channel_id}', self.h_get_channel)

        self.server.add_get('channels/{channel_id}/messages', self.h_get_messages)
        self.server.add_get('channels/{channel_id}/messages/{message_id}', self.h_get_single_message)

        self.server.add_post('channels/{channel_id}/messages', self.h_post_message)
        self.server.add_patch('channels/{channel_id}/messages/{message_id}',
                       self.h_patch_message)

        self.server.add_delete('channels/{channel_id}/messages/{message_id}',
                        self.h_delete_message)

        self.server.add_post('channels/{channel_id}/typing', self.h_post_typing)

        self.server.add_put('channels/{channel_id}', self.h_edit_channel)
        self.server.add_patch('channels/{channel_id}', self.h_edit_channel)
        self.server.add_delete('channels/{channel_id}', self.h_delete_channel)

        self.server.add_post('channels/{chanel_id}/messages/bulk-delete', self.h_bulk_delete)

    @auth_route
    async def h_get_channel(self, request, user):
        """`GET /channels/{channel_id}`.

        Returns a channel object
        """

        channel_id = request.match_info['channel_id']

        channel = self.guild_man.get_channel(channel_id)
        if channel is None:
            return _err(errno=10003)

        guild = channel.guild

        if user.id not in guild.members:
            return _err('401: Unauthorized')

        return _json(channel.as_json)

    @auth_route
    async def h_post_typing(self, request, user):
        """`POST /channels/{channel_id}/typing`.

        Dispatches TYPING_START events to relevant clients.
        Returns a HTTP empty response with status code 204.
        """

        channel_id = request.match_info['channel_id']

        channel = self.guild_man.get_channel(channel_id)
        if channel is None:
            return _err(errno=10003)

        if user.id not in channel.guild.members:
            return _err(errno=40001)

        self.server.loop.create_task(self.server.presence.typing_start(user.id, channel.id))
        return web.Response(status=204)

    @auth_route
    async def h_post_message(self, request, user):
        """`POST /channels/{channel_id}/messages/`.

        Send a message.
        Dispatches MESSAGE_CREATE events to relevant clients.
        """

        channel_id = request.match_info['channel_id']
        channel = self.guild_man.get_channel(channel_id)

        if channel is None:
            return _err(errno=10003)

        if user.id not in channel.guild.members:
            return _err(errno=40001)

        try:
            payload = await request.json()
        except:
            return _err("error parsing")

        try:
            content = str(payload['content'])
            if len(content) < 1:
                return _err(errno=50006)

            if len(content) > 2000:
                return web.Response(status=400)
        except:
            return _err('no useful content provided')

        used_nonces = self.nonces[user.id]
        try:
            current_nonce = payload['nonces']
            if current_nonce in used_nonces:
                return _err('nonce already used', status_code=409)

            used_nonces.append(current_nonce)
        except KeyError:
            log.warning('No nonce sent!')
            pass
        
        _data = {
            'message_id': get_snowflake(),
            'author_id': user.id,
            'channel_id': channel.id,
            'content': content,
        }

        new_message = await self.guild_man.new_message(channel, user, _data)
        return _json(new_message.as_json)

    @auth_route
    async def h_get_single_message(self, request, user):
        """`GET /channels/{channel_id}/messages/{message_id}`.

        Get a single message by its snowflake ID.
        """

        channel_id = request.match_info['channel_id']
        message_id = request.match_info['message_id']

        channel = self.guild_man.get_channel(channel_id)

        if channel is None:
            return _err(errno=10003)

        if user.id not in channel.guild.members:
            return _err(errno=40001)

        message = channel.get_message(message_id)
        if message is None:
            return _err(errno=10008)

        return _json(message.as_json)

    @auth_route
    async def h_get_messages(self, request, user):
        """`GET /channels/{channel_id}/messages`.

        Returns a list of messages.
        """

        channel_id = request.match_info['channel_id'] 
        channel = self.guild_man.get_channel(channel_id)

        if channel is None:
            return _err(errno=10003)

        if user.id not in channel.guild.members:
            return _err(errno=40001)

        limit = request.query.get('limit', 50)

        try:
            limit = int(limit)
        except:
            return _err('limit is not a integer')

        if not ((limit >= 1) and (limit <= 100)):
            return _err(f'limit not in 1-100 range, {limit}')

        around = request.query.get('around', -1)
        before = request.query.get('before', -1)
        after = request.query.get('after', -1)

        try:
            around = int(around)
            before = int(before)
            after = int(after)
        except:
            return _err('parameters are not integers')

        message_list = await channel.last_messages(limit)

        if around != -1:
            avg = int(limit / 2)
            before = around + avg
            after = around - avg

            message_list = [m for m in message_list if (m.id < before) and (m.id > after)]

        elif before != -1:
            message_list = [m for m in message_list if (m.id < before)]

        elif after != -1:
            message_list = [m for m in message_list if (m.id > after)]

        j = [m.as_json for m in message_list]
        return _json(list(reversed(j)))

    @auth_route
    async def h_delete_message(self, request, user):
        """`DELETE /channels/{channel_id}/messages/{message_id}`.

        Delete a message sent by the user.
        """

        channel_id = request.match_info['channel_id']
        message_id = request.match_info['message_id']

        channel = self.guild_man.get_channel(channel_id)

        if channel is None:
            return _err(errno=10003)

        if user.id not in channel.guild.members:
            return _err(errno=40001)

        message = channel.get_message(message_id)
        if message is None:
            return _err(errno=10008)

        if user.id != message.author.id:
            return _err(errno=40001)

        await self.guild_man.delete_message(message)
        return web.Response(status=204)

    @auth_route
    async def h_patch_message(self, request, user):
        """`PATCH /channels/{channel_id}/messages/{message_id}`.

        Update a message sent by the current user.
        """

        channel_id = request.match_info['channel_id']
        message_id = request.match_info['message_id']

        channel = self.guild_man.get_channel(channel_id)

        if channel is None:
            return _err(errno=10003)

        if user.id not in channel.guild.members:
            return _err(errno=40001)

        message = channel.get_message(message_id)
        if message is None:
            return _err(errno=10008)

        if user.id != message.author.id:
            return _err(errno=50005)

        try:
            payload = await request.json()
        except:
            return _err("error parsing")

        _data = {
            'content': str(payload.get('content', None)),
        }

        if _data['content'] is None:
            return _err('Erroneous payload')

        await self.guild_man.edit_message(message, _data)
        return _json(message.as_json)

    @auth_route
    async def h_bulk_delete(self, request, user):
        """`POST /channels/{channel_id}/messages/bulk-delete`.
        
        Deletes multiple messages.
        Returns 204 empty response on success, fires mutiple MESSAGE_DELETEs.
        """
        channel_id = request.match_info['channel_id']
        channel = self.guild_man.get_channel(channel_id)
        if channel is None:
            return _err(errno=10003)

        payload = await request.json()
        messages = payload['messages']
        if len(messages) < 1:
            # uhh, you sent an empty array... I think this is a success.
            return web.Response(status=204)

        messages = [int(message_id) for message_id in messages]
        current = time.time()
        for message_id in messages:
            timestamp = snowflake_time(message_id)
            delta = current - timestamp
            if delta > BULK_DELETE_LIMIT:
                # do the error
                return _err('Message too old.')

        messages = set(messages)

        # since it can take some time, we create a task
        self.server.loop.create_task(channel.delete_many(messages, fire_multiple=True))

        return web.Response(status=204)

    @auth_route
    async def h_edit_channel(self, request, user):
        """`PUT/PATCH /channels/{channel_id}`.

        Edit a channel. Receives a JSON payload.
        """
        channel_id = request.match_info['channel_id']
        chan = self.guild_man.get_channel(channel_id)
        if chan is None:
            return _err(errno=10003)
        
        if chan.guild.owner_id != user.id:
            return _err(errno=40001)

        payload = await request.json()

        if isinstance(chan, TextChannel):
            # check against text schema
            payload = self.textchan_editschema(payload)
        elif isinstance(chan, VoiceChannel):
            payload = self.voicechan_editschema(payload)

        new_chan = await chan.edit(payload)
        return _json(new_chan.as_json)

    @auth_route
    async def h_delete_channel(self, request, user):
        """`DELETE /channels/{channel_id}`.
        
        Delete a channel.
        Fires CHANNEL_DELETE events to respective clients.
        """
        channel_id = request.match_info['channel_id']
        chan = self.guild_man.get_channel(channel_id)
        if chan is None:
            return _err(errno=10003)

        if chan.guild.owner_id != user.id:
            return _err(errno=40001)

        await chan.delete()
        return _json(chan.as_json)

