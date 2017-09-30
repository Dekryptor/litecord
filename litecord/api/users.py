'''
users.py - All handlers under /users/*
'''

import logging

from aiohttp import web

from ..utils import _err, _json
from ..snowflake import get_snowflake
from ..decorators import auth_route

log = logging.getLogger(__name__)

class UsersEndpoint:
    def __init__(self, server):
        self.server = server
        self.guild_man = server.guild_man

        self.register()

    def register(self):
        self.server.add_get('users/{user_id}', self.h_users)
        self.server.add_get('users/{user_id}/profile', self.h_users_profile)
        self.server.add_patch('users/@me', self.h_patch_me)
        self.server.add_get('users/@me/settings', self.h_get_me_settings)
        self.server.add_get('users/@me/guilds', self.h_users_me_guild)

        self.server.add_delete('users/@me/guilds/{guild_id}', self.h_leave_guild)
        
        # dm when
        #self.server.add_get('users/@me/channels', self.h_get_dm)
        #self.server.add_post('users/@me/channels', self.h_open_dm)

    @auth_route
    async def h_users_profile(self, request, user):
        """Handle `GET /users/{user_id}`.

        Grab the profile of a specific user.
        """
        user_id = request.match_info['user_id']
        log.debug(f'grabbing profile for {user_id}')

        if user.bot:
            return _err(errno=40001)  # no lol

        words = ('memework', 'makes', 'the', 'dreamwork')

        accounts = [{
            'id': index + 1,
            'name': word,
            'type': 'twitch',
            'verified': True,
        } for index, word in enumerate(words)]

        return _json({
            'user': user.as_json,
            'connected_accounts': accounts,
            'premium_since': '2000-01-01T01:00:00+00:00',
            'mutual_guilds': []  # TODO: do this
        })

    @auth_route
    async def h_users(self, request, user):
        """Handle `GET /users/{user_id}`.

        Get a specific user.
        """

        user_id = request.match_info['user_id']
        log.debug(f"user={user}")

        if user_id == '@me':
            return _json(user.as_json)
        else:
            if not user.bot:
                return _err(errno=40001)

            log.debug(f'searching for user {user_id!r}')

            user_to_find = self.server.get_user(user_id)
            if user_to_find is None:
                return _err(errno=10013)

            return _json(user_to_find.as_json)

    @auth_route
    async def h_patch_me(self, request, user):
        """`PATCH /users/@me`.

        Changes a user.
        Returns the new user object.
        """

        payload = await request.json()

        new_raw_user = {}

        new_username = payload.get('username', user.username)
        if new_username != user.username:
            new_raw_user['discriminator'] = await self.server.get_discrim(new_username)

        new_raw_user['username'] = new_username

        new_avatar_hash = await self.server.images.avatar_register(payload.get('avatar'))
        new_raw_user['avatar'] = new_avatar_hash or user._data['avatar']

        user._raw.update(new_raw_user)

        await self.server.user_coll.update_one({'user_id': str(user.id)}, {'$set': new_raw_user})
        await self.server.reload_user(user)

        return _json(user.as_json)

    @auth_route
    async def h_get_me_settings(self, request, user):
        """`GET /users/@me/settings`.

        Dummy handler.
        """
        return _json({})

    @auth_route
    async def h_users_me_guild(self, request, user):
        """`GET /users/@me/guilds`.

        Returns a list of user guild objects.

        TODO: before, after, limit parameters
        """

        return _json([g.as_user(user.id) for g in user.guilds])

    @auth_route
    async def h_leave_guild(self, request, user):
        """`DELETE /users/@me/guilds/{guild_id}`.

        Leave guild.
        Returns empty 204 response.
        """

        guild_id = request.match_info['guild_id']

        guild = self.guild_man.get_guild(guild_id)
        if guild is None:
            return _err(errno=10004)

        await self.guild_man.remove_member(guild, user)
        return web.Response(status=204)
