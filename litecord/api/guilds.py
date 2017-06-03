import json
import logging

from aiohttp import web
from ..utils import _err, _json
from ..decorators import auth_route

log = logging.getLogger(__name__)

class GuildsEndpoint:
    """Manager for guild-related endpoints."""
    def __init__(self, server):
        self.server = server
        self.guild_man = server.guild_man

    def register(self, app):
        self.server.add_get('guilds/{guild_id}', self.h_guilds)
        self.server.add_get('guilds/{guild_id}/channels', self.h_get_guild_channels)
        self.server.add_get('guilds/{guild_id}/members/{user_id}', self.h_guild_one_member)
        self.server.add_get('guilds/{guild_id}/members', self.h_guild_members)
        self.server.add_post('guilds', self.h_post_guilds)
        self.server.add_patch('guilds/{guild_id}/members/@me/nick', self.h_change_nick)

        self.server.add_delete('users/@me/guilds/{guild_id}', self.h_leave_guild)
        self.server.add_delete('guilds/{guild_id}/members/{user_id}', self.h_kick_member)
        self.server.add_put('guilds/{guild_id}/bans/{user_id}', self.h_ban_member)
        self.server.add_delete('guilds/{guild_id}/bans/{user_id}', self.h_unban_member)

        self.server.add_patch('guilds/{guild_id}', self.h_edit_guild)

    @auth_route
    async def h_guilds(self, request, user):
        """`GET /guilds/{guild_id}`.

        Returns a guild object.
        """

        guild_id = request.match_info['guild_id']

        guild = self.guild_man.get_guild(guild_id)
        if guild is None:
            return _err(errno=10004)

        return _json(guild.as_json)

    @auth_route
    async def h_get_guild_channels(self, request, user):
        """`GET /guilds/{guild_id}/channels`.

        Returns a list of channels the guild has.
        """
        guild_id = request.match_info['guild_id']

        guild = self.guild_man.get_guild(guild_id)
        if guild is None:
            return _err(errno=10004)

        return _json([channel.as_json for channel in guild.channels])

    @auth_route
    async def h_guild_one_member(self, request, user):
        """`GET /guilds/{guild_id}/members/{user_id}`.

        Get a specific member in a guild.
        """
        guild_id = request.match_info['guild_id']
        user_id = request.match_info['user_id']

        guild = self.guild_man.get_guild(guild_id)
        if guild is None:
            return _err(errno=10004)

        if user.id not in guild.members:
            return _err(errno=40001)

        if user_id not in guild.members:
            return _err(errno=10004)

        return _json(guild.members[user_id].as_json)

    @auth_route
    async def h_guild_members(self, request, user):
        """`GET /guilds/{guild_id}/members`.

        Returns a list of all the members in a guild.
        """

        guild_id = request.match_info['guild_id']

        guild = self.guild_man.get_guild(guild_id)
        if guild is None:
            return _err(errno=10004)

        if user.id not in guild.members:
            return _err(errno=40001)

        return _json([member.as_json for member in guild.members])

    @auth_route
    async def h_post_guilds(self, request, user):
        """`POST /guilds`.

        Create a guild.
        """

        try:
            _payload = await request.json()
        except:
            return _err('error parsing')

        # we ignore anything else client sends.
        try:
            payload = {
                'name': _payload['name'],
                'region': _payload['region'],
                'icon': _payload['icon'],
                'verification_level': _payload.get('verification_level', -1),
                'default_message_notifications': _payload.get('default_message_notifications', -1),
                'roles': [],
                'channels': [],
                'members': [str(user.id)],
            }
        except KeyError:
            return _err('incomplete payload')

        try:
            new_guild = await self.guild_man.new_guild(user, payload)
        except:
            log.error(exc_info=True)
            return _err('error creating guild')

        return _json(new_guild.as_json)

    @auth_route
    async def h_leave_guild(self, request, user):
        """`DELETE /users/@me/guilds/{guild_id}`.

        Leave a guild.
        Fires GUILD_DELETE event.
        """

        guild_id = request.match_info['guild_id']

        guild = self.guild_man.get_guild(guild_id)
        if guild is None:
            return _err(errno=10004)

        if user.id not in guild.members:
            return _err(errno=10004)

        await self.guild_man.remove_member(guild, user)
        return web.Response(status=204)

    @auth_route
    async def h_kick_member(self, request, user):
        """`DELETE /gulids/{guild_id}/members/{user_id}`.

        Kick a member.
        """

        guild_id = request.match_info['guild_id']
        member_id = request.match_info['user_id']

        try:
            guild_id = int(guild_id)
            member_id = int(member_id)
        except:
            return _err('malformed url')

        guild = self.guild_man.get_guild(guild_id)
        if guild is None:
            return _err(errno=10004)

        if user.id not in guild.members:
            return _err(errno=10004)

        member = guild.members.get(member_id)
        if member is None:
            return _err(errno=10007)

        try:
            res = await self.guild_man.kick_member(member)
            if not res:
                return _err("Kicking failed.")
            return web.Response(status=204)
        except Exception as err:
            log.error("Error kicking member", exc_info=True)
            return _err('Error kicking member: {err!r}')

    @auth_route
    async def h_change_nick(self, request, user):
        """`PATCH /guilds/{guild_id}/members/@me/nick`.

        Modify your nickname.
        Returns a 200.
        Dispatches GUILD_MEMBER_UPDATE to relevant clients.
        """

        guild_id = request.match_info['guild_id']

        guild = self.guild_man.get_guild(guild_id)
        if guild is None:
            return _err(errno=10004)

        if user.id not in guild.members:
            return _err(errno=10004)

        member = guild.members.get(user.id)

        try:
            payload = await request.json()
        except:
            return _err('error parsing payload')

        nickname = str(payload.get('nick', ''))

        if len(nickname) > 32:
            return _err('Nickname is over 32 chars.')

        await self.guild_man.edit_member(member, {
            'nick': nickname,
        })

        return web.Response(status=200, text=nickname)

    @auth_route
    async def h_ban_member(self, request, user):
        """`PUT /guilds/{guild_id}/bans/{user_id}`.

        Ban a member from a guild.
        Dispatches GUILD_BAN_ADD event to relevant clients.
        """

        guild_id = request.match_info['guild_id']
        target_id = request.match_info['user_id']

        try:
            guild_id = int(guild_id)
            target_id = int(target_id)
        except:
            return _err('malformed url')

        guild = self.guild_man.get_guild(guild_id)
        if guild is None:
            return _err(errno=10004)

        target = self.server.get_user(target_id)
        if target is None:
            return _err(errno=10013)

        try:
            await guild.ban(target)
            return web.Response(status=204)
        except Exception as err:
            log.error("Error banning user", exc_info=True)
            return _err('Error banning user: {err!r}')

    @auth_route
    async def h_unban_member(self, request, user):
        """`DELETE /guilds/{guild_id}/bans/{user_id}`.

        Unban a member from a guild.
        Dispatches GUILD_BAN_REMOVE event to relevant clients.
        """

        guild_id = request.match_info['guild_id']
        target_id = request.match_info['user_id']

        try:
            guild_id = int(guild_id)
            target_id = int(target_id)
        except:
            return _err('malformed url')

        guild = self.guild_man.get_guild(guild_id)
        if guild is None:
            return _err(errno=10004)

        target = self.server.get_user(target_id)
        if target is None:
            return _err(errno=10013)

        try:
            await guild.unban(target)
            return web.Response(status=204)
        except Exception as err:
            log.error("Error banning user", exc_info=True)
            return _err('Error banning user: {err!r}')

    @auth_route
    async def h_edit_guild(self, request, user):
        """`PATCH /guilds/{guild_id}`.

        Edit a guild.
        Dispatches GUILD_UPDATE to relevant clients.
        """

        guild_id = request.match_info['guild_id']

        guild = self.guild_man.get_guild(guild_id)
        if guild is None:
            return _err(errno=10004)

        try:
            _payload = await request.json()
        except:
            return _err('error parsing payload')

        if user.id != guild.owner_id:
            return _err(errno=40001)

        _pg = _payload.get

        edit_payload = {
            'name':                             str(_pg('name')),
            'region':                           str(_pg('region')),
            'verification_level':               int(_pg('verification_level')),
            'default_message_notifications':    int(_pg('default_message_notifications')),
            'afk_channel_id':                   str(_pg('afk_channel_id')),
            'afk_timeout':                      int(_pg('afk_timeout')),
            'icon':                             str(_pg('icon')),
            'owner_id':                         str(_pg('owner_id')),
        }

        try:
            new_guild = await guild.edit(edit_payload)
            return _json(new_guild.as_json)
        except Exception as err:
            return _err(f'{err!r}')
