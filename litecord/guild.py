import logging
import asyncio
import datetime

from collections import defaultdict

from .objects import TextChannel, VoiceChannel, Guild, \
    Message, Invite, Role, BareGuild
from .snowflake import get_snowflake, get_invite_code
from .utils import get

log = logging.getLogger(__name__)


class GuildManager:
    """Manager class for guilds, channels, roles, messages and invites..

    .. _LitecordServer: server.html
    .. _AsyncIOMotorCollection: https://motor.readthedocs.io/en/stable/api-asyncio/asyncio_motor_collection.html

    Attributes
    ----------
    server: [`LitecordServer`_]
        Server instance.
    guild_coll: [`AsyncIOMotorCollection`_]
        Guild collection.
    message_coll: [`AsyncIOMotorCollection`_]
        Message collection.
    guilds: list 
        All available :class:`Guild` objects.
    channels: list
        All available :class:`Channel` objects.
    """
    def __init__(self, server):
        self.server = server

        self.role_coll = server.role_coll
        self.channel_coll = server.channel_coll
        self.guild_coll = server.guild_coll
        self.invite_coll = server.invite_coll
        self.message_coll = server.message_coll
        self.member_coll = server.member_coll
        
        self.raw_members = defaultdict(dict)
        self.roles = []
        self.channels = []
        self.guilds = []
        self.invites = []
        self.messages = []

        self.invi_janitor_task = self.server.loop.create_task(self.invite_janitor)

    def get_guild(self, guild_id):
        """Get a :class:`Guild` object by its ID."""
        try:
            guild_id = int(guild_id)
        except:
            return None
        return get(self.guilds, id=guild_id)

    def get_channel(self, channel_id):
        """Get a :class:`Channel` object by its ID."""
        try:
            channel_id = int(channel_id)
        except:
            return None

        channel = get(self.channels, id=channel_id)
        if channel is None:
            return None

        async def _updater():
            # Update a channel's last_message_id property
            if isinstance(channel, VoiceChannel):
                return

            mlist = await channel.last_messages(1)
            try:
                m_id = mlist[0].id
            except:
                m_id = None
            channel.last_message_id = m_id

        asyncio.ensure_future(_updater())
        return channel

    def get_role(self, role_id: int):
        """Get a :class:`Role` by its ID."""
        try:
            role_id = int(role_id)
        except: return
        r = get(self.roles, id=role_id)
        log.debug('[get_role] %d -> %r', role_id, r)
        return r

    def get_message(self, message_id):
        """Get a :class:`Message` object by its ID."""
        try:
            message_id = int(message_id)
        except: return
        m = get(self.messages, id=message_id)
        log.debug('[get_message] %d -> %r', message_id, m)
        return m

    def yield_guilds(self, user_id: int):
        """Yield all :class:`Guild` a user is in.
        
        Parameters
        ----------
        user_id: int
            User ID we want to get the guilds from

        Yields
        ------
        :class:`Guild`
        """
        try:
            user_id = int(user_id)
        except:
            return

        if self.server.get_user(user_id) is None:
            return

        # TODO: maybe change this to an async iterator?
        for guild in self.guilds:
            if user_id in guild.member_ids:
                yield guild

    def get_guilds(self, user_id: int) -> list:
        """Get a list of all guilds a user is on.

        Parameters
        ----------
        user_id: int
            The user ID we want to get the guilds from.

        Returns
        -------
        List of :class:`Guild`
        """
        try:
            user_id = int(user_id)
        except:
            return None
        
        return list(self.yield_guilds(user_id))

    def get_invite(self, invite_code: str):
        """Get an :class:`Invite` object.
        
        Parameters
        ----------
        invite_code: str
            Invite code to search on

        Returns
        -------
        :class:`Invite` or :py:meth:`None`
        """
        return get(self.invites, code=invite_code)

    def get_raw_member(self, guild_id: int, user_id: int) -> dict:
        """Get a raw member.
        
        guild_id: int
            Guild ID from the member.
        user_id: int
            User ID that references the member.

        Returns
        -------
        dict
            Raw member.
        """
        try:
            guild_id = int(guild_id)
            user_id = int(user_id)
        except:
            return None

        try:
            raw_guild_members = self.raw_members[guild_id]
        except:
            return None

        try:
            return raw_guild_members[user_id]
        except:
            return None

    def all_guilds(self):
        """Yield all available guilds."""
        for guild in self.guilds:
            yield guild

    async def all_messages_(self, limit=500):
        """Yield `limit` messages, with the 1st being the most recent one."""
        cursor = self.message_coll.find().sort('message_id')

        for raw_message in reversed(await cursor.to_list(length=limit)):
            message = self.messages[raw_message['message_id']]
            yield message

    async def new_message(self, channel, author, raw):
        """Create a new message and put it in the database.

        Dispatches MESSAGE_CREATE events to respective clients.

        Parameters
        ----------
        channel: :class:`Channel`
            The channel where to put the new message.
        author: :class:`User`
            The author of the message.
        raw: dict
            Raw message object.

        Returns
        -------
        :class:`Message`
            The created message.
        """

        message = Message(self.server, channel, raw)

        result = await self.message_coll.insert_one(message.as_db)
        self.messages.append(message)
        log.info(f'Adding message with id {message.id}')

        await channel.dispatch('MESSAGE_CREATE', message.as_json)
        return message

    async def delete_message(self, message) -> 'None':
        """Delete a message.

        Dispatches MESSAGE_DELETE events to respective clients.

        Parameters
        ----------
        message: :class:`Message`
            Message to delete.

        """

        result = await self.message_coll.delete_one({'message_id': message.id})
        log.info(f"Deleted {result.deleted_count} messages")

        await message.channel.dispatch('MESSAGE_DELETE', {
            'id': str(message.id),
            'channel_id': str(message.channel.id),
        })

    async def edit_message(self, message, payload) -> 'None':
        """Edit a message.

        Dispatches MESSAGE_UPDATE events to respective clients.

        Parameters
        ----------
        message: :class:`Message`
            Message to edit.
        payload: dict
            Message edit payload.
        """

        new_content = payload['content']
        message.edit(new_content)

        result = await self.message_coll.update_one({'message_id': str(message.id)}, {'$set': message.as_db})
        log.info(f"Updated {result.modified_count} messages")

        await message.channel.dispatch('MESSAGE_UPDATE', message.as_json)

    async def reload_guild(self, guild):
        """Update a guild.
        
        Retrieves the raw guild from the database,
        and updates the received guild with the new data.
        
        Since usually :meth:`GuildManager.get_guild`, which
        is the usual method to retrieve guild objects, very
        probably the received guild in this function
        is already a guild from the cache, meaning that
        updating the received guild means the guild in the
        cache is updated as well.

        Parameters
        ----------
        guild : :class:`Guild`
            The guild object to be updated with new data

        Returns
        -------
        :class:`Guild`
            The updated guild object, it is the same object
            as the received guild.
        :py:meth:`None`
            If the guild doesn't exist anymore.
            The guild gets removed from the cache.
        """

        # The strategy here is to query the database
        # with the guild id and check if it exists or not
        # and do the appropiate actions

        assert isinstance(guild, Guild)
        
        query = {'guild_id': guild.id}
        raw_guild = await self.guild_coll.find_one()
        if raw_guild is None:
            log.info('[guild:reload] Guild not found, deleting from cache')
            try:
                self.guilds.remove(guild)
            except ValueError: pass

            for channel in guild.channels:
                try:
                    self.channels.remove(channel)
                except ValueError: pass

            for role in guild.roles:
                try:
                    self.roles.remove(role)
                except ValueError: pass

            del guild
            return None

        guild._raw.update(raw_guild)
        guild._update(guild._raw)
        return guild

    async def reload_channel(self, channel):
        """Reload one channel.
        
        Merges the raw channel the channel object refernces
        with the new data from the database.

        Follows the same strategies as :meth:`GuildManager.reload_guild`.
        """

        query = {'channel_id': channel.id}
        raw_channel = await self.channel_coll.find_one(query)
        if raw_channel is None:
            log.info('[channel:reload] Channel not found, deleting from cache')
            try:
                channel.guild.channels.remove(channel)
            except ValueError: pass

            try:
                self.channels.remove(channel)
            except ValueError: pass

            del channel
            return

        channel._raw.update(raw_channel)
        channel._update(channel.guild, channel._raw)
        return channel

    async def new_guild(self, owner, payload):
        """Create a Guild.

        Dispatches GUILD_CREATE event to the owner of the new guild.

        Parameters
        ----------
        owner: :class:`User`
            The owner of the guild to be created
        payload: dict
            guild payload::
                {
                "name": "Name of the guild",
                "region": "guild voice region, ignored",
                "verification_level": TODO,
                "default_message_notifications": TODO,
                "icon": "base64 128x128 jpeg image for the guild icon",
                }

        Returns
        -------
        :class:`Guild`
        """

        if not owner.online:
            log.warning("User not connected through WS to do this action.")
            return None

        if True:
            log.warning('THIS DOES NOT WORK')
            return None

        payload['owner_id'] = str(owner.id)
        payload['guild_id'] = get_snowflake()
        payload['features'] = []
        payload['roles'] = []
        payload['channels'] = [{
            'id': str(payload['id']),
            'guild_id': str(payload['id']),
            'name': 'general',
            'type': 'text',
            'position': 0,
            'topic': '',
        }]

        for raw_channel in payload['channels']:
            raw_channel['guild_id'] = payload['id']

        # A GIANT HACK
        raw_member_owner = {
            'guild_id': payload['id'],
            'user_id': str(owner.id),
            'nick': '',
            'joined': datetime.datetime.now().isoformat(),
            'deaf': False,
            'mute': False,
        }

        await self.member_coll.insert_one(raw_member_owner)
        self.raw_members[payload['guild_id']][owner.id] = raw_member_owner

        guild = Guild(self.server, payload)
        await self.guild_coll.insert_one(guild._raw)
        self.guilds.append(guild)

        for channel in guild.all_channels():
            self.channels[channel.id] = channel

        await self.server.presence.status_update(guild, owner)
        await guild.dispatch('GUILD_CREATE', guild.as_json)

        return guild

    async def edit_guild(self, guild, guild_edit_payload):
        """Edit a guild.

        Dispatches GUILD_UPDATE events to relevant clients.

        Parameters
        ----------
        guild: :class:`Guild`
            Guild that is going to be updated with new data.
        guild_edit_payload: dict
            New guild data, has 9, all optional, fields. ``name, region, verification_level,
            default_message_notifications, afk_channel_id, afk_timeout, icon, owner_id, splash``.

        Returns
        -------
        The edited :class:`Guild`.
        """

        await self.guild_coll.update_one({'guild_id': str(guild.id)},
            {'$set': guild_edit_payload})

        guild = await self.reload_guild(guild)

        await guild.dispatch('GUILD_UPDATE', guild.as_json)
        return guild

    async def delete_guild(self, guild):
        """Delete a guild.
        
        Dispatches GUILD_DELETE to all guild members.

        Returns
        -------
        None
        """
        res = await self.guild_coll.delete_many({'guild_id': guild_id})

        if res.deleted_count < 1:
            log.warning('[guild_delete] Something went weird (deleted_doc == 0)')
            return

        if res.deleted_count > 1:
            log.warning('[guild_delete] SOMETHING HAS GONE HORRIBLY WRONG(deleted_doc > 1)')

        result = await self.member_coll.delete_many({'guild_id': guild_id})
        log.info(f'[guild_delete] Deleted {result.deleted_count} raw members')

        del self.raw_guilds[guild_id]

        await guild.dispatch('GUILD_DELETE', {
            'id': str(guild.id),
            'unavailable': False
        })

        return await self.reload_guild(guild)

    async def add_member(self, guild, user):
        """Adds a user to a guild.
        Doesn't add if the user is banned from the guild.

        Dispatches GUILD_MEMBER_ADD to relevant clients.

        Parameters
        ----------
        guild: :class:`Guild`
            The guild to add the user to.
        user: :class:`User`
            The user that is going to be added to the guild.

        Returns
        -------
        :class:`Member` on success or :py:const:`None` on failure
        """

        raw_guild = guild._data

        if str(user.id) in guild.bans:
            return None

        raw_guild['members'].append(str(user.id))

        result = await self.guild_coll.replace_one({'guild_id': guild.id}, raw_guild)
        log.info(f"Updated {result.modified_count} guilds")

        raw_member = {
            'guild_id': guild.id,
            'user_id': user.id,
            'nick': None,
            'joined': datetime.datetime.now().isoformat(),
            'deaf': False,
            'mute': False,
        }
        result = await self.member_coll.insert_one(raw_member)
        self.raw_members[guild.id][user.id] = raw_member

        guild = await self.reload_guild(guild)

        new_member = guild.members.get(user.id)
        if new_member is None:
            return None

        to_add = {'guild_id': str(guild.id)}
        payload = {**new_member.as_json, **to_add}

        await guild.dispatch('GUILD_MEMBER_ADD', payload)

        return new_member

    async def edit_member(self, member, new_data):
        """Edit a member.

        Dispatches GUILD_MEMBER_UPDATE to relevant clients.

        Parameters
        ----------
        member: :class:`Member`
            Member to edit data.
        new_data: dict
            Raw member data.
        """

        guild = member.guild
        user = member.user

        await self.member_coll.update_one({'guild_id': str(guild.id), 'user_id': str(user.id)},
            {'$set': new_data})

        raw_member = {**member._raw, **new_data}
        member._update(raw_member)

        # update in cache
        self.raw_members[guild.id][user.id] = raw_member

        await guild.dispatch('GUILD_MEMBER_UPDATE', {
            'guild_id': str(member.guild.id),
            'roles': member.iter_json(member.roles),
            'user': member.user.as_json,
            'nick': member.nick
        })

    async def remove_member(self, guild, user):
        """Remove a user from a guild.

        Dispatches GUILD_MEMBER_REMOVE to relevant clients.
        Dispatches GUILD_DELETE to the user being removed from the guild.

        Parameters
        ----------
        guild: :class:`Guild`
            Guild to remove the user from.
        user: :class:`User`
            User to remove from the guild.
        """

        user_id = str(user.id)

        raw_guild = guild._raw
        raw_guild['member_ids'].remove(user_id)

        await self.guild_coll.update_one({'guild_id': guild.id}, {'$set': raw_guild})

        result = await self.member_coll.delete_many({'guild_id': guild.id, 'user_id': user.id})
        log.info(f'Deleted {result.deleted_count} member objects')

        del self.raw_members[guild.id][user.id]

        guild = await self.reload_guild(guild)
        await guild.dispatch('GUILD_MEMBER_REMOVE', {
            'guild_id': str(guild.id),
            'user': user.as_json,
        })

        await user.dispatch('GUILD_DELETE', {
            'id': str(guild.id),
            'unavailable': False,
        })

    async def _ban_clean(self, guild, user, delete_days):
        """Delete all messages made by a user"""
        for channel in guild.text_channels:
            days_ago = time.time() - (delete_days * 24 * 60 * 60)
            messages = await channel.from_timestamp(days_ago)
            message_ids = [message.id for message in messages if message.author.id == user.id]
            await channel.delete_many(message_ids, bulk=True)

    async def ban_user(self, guild, user, delete_days=None):
        """Ban a user from a guild.

        Dispatches GUILD_BAN_ADD and GUILD_MEMBER_REMOVE to relevant clients.
        Dispatches MESSAGE_DELETE_BULK if `delete_days` is specified.

        Parameters
        ---------
        guild: :meth:`Guild`
            Guild that the user is going to be banned from.
        user: :meth:`User`
            User to be banned.
        delete_days: int or None:
            The amount of days worth of messages to be removed using :meth:`TextChannel.delete_many`.
        """

        bans = guild.banned_ids

        try:
            bans.index(user.id)
            raise Exception("User already banned")
        except ValueError:
            bans.append(user.id)

        await self.guild_coll.update_one({'guild_id': guild.id},
                                        {'$set': {'bans': bans}})

        await guild.dispatch('GUILD_BAN_ADD',
                            {**user.as_json, **{'guild_id': str(guild.id)}})

        try:
            guild.member_ids.index(user.id)
            await self.remove_member(guild, user)
        except ValueError:
            pass

        if delete_days is not None:
            self.loop.create_task(self._ban_clean(guild, user, delete_days))

    async def unban_user(self, guild, user):
        """Unban a user from a guild.

        Dispatches GUILD_BAN_REMOVE to relevant clients.
        """

        bans = guild.banned_ids

        try:
            bans.remove(user.id)
        except ValueError:
            raise Exception('User not banned')

        await self.guild_coll.update_one({'guild_id': guild.id},
                                        {'$set': {'bans': bans}})

        await guild.dispatch('GUILD_BAN_REMOVE',
                            {**user.as_json, **{'guild_id': str(guild.id)}})

    async def kick_member(self, member):
        """Kick a member from a guild.

        Dispatches GUILD_MEMBER_REMOVE to relevant clients.

        Parameters
        ----------
        member: :class:`Member`
            The member to kick.
        """

        guild = member.guild
        try:
            await self.remove_member(guild, member.user)
            return True
        except:
            log.error("Error kicking member.", exc_info=True)
            return False

    async def shard_count(self, user):
        """Give the shard count for a user.

        Since Litecord does not support sharding nor clients
        in a lot of guilds, this usually returns the amazing value of 1.

        The value changes with the user joining/leaving guilds.

        Parameters
        ----------
        user: :class:`User`
            The user to get a shard count from.

        Returns
        -------
        int
            The recommended amount of shards to start the connection.
        """
        guild_count = await self.member_coll.count({'user_id': user.id})
        return max(guild_count / 1200, 1)

    async def create_channel(self, guild, payload):
        """Create a channel in a guild.

        Dispatches CHANNEL_CREATE to relevant clients.

        Parameters
        ----------
        guild: :class:`Guild`
            The guild that is going to have a new channel
        payload: dict
            Channel create payload.

        Returns
        -------
        :class:`Channel`
        """

        if payload['type'] not in ['text', 'voice']:
            raise Exception("Invalid channel type")

        raw_guild = guild._data

        payload['id'] = str(get_snowflake())
        payload['topic'] = ""
        payload['position'] = len(guild.channels) + 1

        raw_guild['channels'].append(payload)

        result = await self.guild_coll.replace_one({'guild_id': guild.id}, raw_guild)
        log.info(f"Updated {result.modified_count} guilds")

        guild = await self.reload_guild(guild)
        new_channel = guild.channels.get(int(payload['id']))

        await guild.dispatch('CHANNEL_CREATE', new_channel.as_json)
        return new_channel

    async def edit_channel(self, channel, payload):
        """Edits a channel in a guild.

        Dispatches CHANNEL_UPDATE to relevant clients.
        """

        raw_guild = channel.guild._data
        new_chan_array = raw_guild['channels']
        for raw_channel in new_chan_array:
            if raw_channel['id'] == str(channel.id):
                raw_channel = {**raw_channel, **payload}

        await self.guild_coll.update_one({'guild_id': channel.guild.id},
            {'$set': {'channels': new_chan_array}})

        guild = await self.reload_guild(guild)
        new_chan = guild.get_channel(channel.id)
        await guild.dispatch('CHANNEL_UPDATE', new_chan.as_json)
        return new_chan

    async def delete_channel(self, channel):
        """Deletes a channel from a guild.

        Dispatches CHANNEL_DELETE to relevant clients

        Returns
        -------
        None
        """
        raw_guild = channel.guild._data
        new_chan_array = []

        for (idx, raw_channel) in enumerate(raw_guild['channels'][:]):
            if raw_channel['id'] != str(channel.id):
                new_chan_array.append(raw_channel)

        await self.guild_coll.update_one({'guild_id': channel.guild.id}, 
            {'$set': {'channels': new_chan_array}})

        guild = await self.reload_guild(guild)
        await guild.dispatch('CHANNEL_DELETE', channel.as_json)
        return

    async def invite_janitor(self):
        """Janitor task for invites.

        This checks every 30 minutes for expired invites and removes them from
        the database.
        """

        try:
            while True:
                cursor = self.invite_coll.find()
                now = datetime.datetime.now()

                deleted, total = 0, 0

                for raw_invite in (await cursor.to_list(length=None)):
                    timestamp = raw_invite.get('timestamp', None)

                    if timestamp is not None:
                        invite_timestamp = datetime.datetime.strptime(self.iso_timestamp, \
                            "%Y-%m-%dT%H:%M:%S")

                        if now > invite_timestamp:
                            await self.invite.db.delete_one({'code': raw_invite['code']})
                            try:
                                self.invites.pop({'code': raw_invite['code']})
                            except:
                                pass

                            deleted += 1
                    total += 1

                log.info("Deleted {deleted}/{total} invites")

                # 30 minutes until next cycle
                await asyncio.sleep(1800)
        except asyncio.CancelledError:
            pass

    async def make_invite_code(self):
        """Generate an unique invite code.

        This uses `snowflake.get_invite_code` and checks if the code already exists
        in the database.
        """

        invi_code = get_invite_code()
        raw_invite = await self.invite_coll.find_one({'code': invi_code})

        while raw_invite is not None:
            invi_code = get_invite_code()
            raw_invite = await self.invite_coll.find_one({'code': invi_code})

        return invi_code

    async def create_invite(self, channel, inviter, invite_payload):
        """Create an invite to a channel.

        Parameters
        ----------
        channel: :class:`Channel`
            The channel to make the invite refer to.
        inviter: :class:`User`
            The user that made the invite.
        invite_payload: dict
            Invite payload.

        Returns
        -------
        :class:`Invite`
        """
        # TODO: something something permissions
        #if not channel.guild.permissions(user, MAKE_INVITE):
        # return None

        age = invite_payload['max_age']
        iso_timestamp = None
        if age > 0:
            now = datetime.datetime.now().timestamp()
            expiry_timestamp = datetime.datetime.fromtimestamp(now + age)
            iso_timestamp = expiry_timestamp.isoformat()

        uses = invite_payload.get('max_uses', -1)
        if uses == 0:
            uses = -1

        invite_code = await self.make_invite_code()
        raw_invite = {
            'code': invite_code,
            'channel_id': str(channel.id),
            'inviter_id': str(inviter.id),
            'timestamp': iso_timestamp,
            'uses': uses,
            'temporary': False,
            'unique': True,
        }

        await self.invite_coll.insert_one(raw_invite)

        invite = Invite(self.server, raw_invite)
        if invite.valid:
            self.invites[invite.code] = invite

        return invite

    async def use_invite(self, user, invite):
        """Uses an invite.

        Adds a user to a guild.

        Parameters
        ----------
        user: :class:`User`
            The user that is going to use the invite.
        invite: :class:`Invite`
            Invite object to be used.

        Returns
        -------
        :class:`Member` or ``None``
        """

        if not invite.sane:
            log.warning(f"Insane invite {invite.code} to {invite.channel.guild.name}")
            return False

        success = invite.use()
        if not success:
            return False

        await invite.update()

        guild = invite.channel.guild
        member = await self.add_member(guild, user)

        if member is None:
            return False

        return member

    async def delete_invite(self, invite):
        """Deletes an invite.

        Removes it from database and cache.
        """

        res = await self.invite_coll.delete_one({'code': invite.code})
        log.info(f"Removed {res.deleted_count} invites")

        try:
            self.invites.pop(invite.code)
        except:
            pass

    async def init(self):
        """Initialize the GuildManager.

        Loads, in order:
         - Members
         - Roles
         - Channels
         - Guilds
         - Invites
         - Messages
        """

        # raw member loading
        cursor = self.member_coll.find()
        member_count = 0
        for raw_member in await cursor.to_list(length=None):
            raw_member.pop('_id')
            self.raw_members[raw_member['guild_id']][raw_member['user_id']] = raw_member
            member_count += 1

        log.info('[guild] loaded %d members', member_count)
        log.debug('raw_members: %r', self.raw_members)

        # role loading
        cursor = self.role_coll.find()
        role_count = 0

        for raw_role in (await cursor.to_list(length=None)):
            bg = BareGuild(raw_role['guild_id'])
            role = Role(self.server, bg, raw_role)
            self.roles.append(role)
            role_count += 1

        log.info('[guild] loaded %d roles', role_count)

        # channel loading
        cursor = self.channel_coll.find()
        chan_count = 0

        for raw_channel in (await cursor.to_list(length=None)):
            ch_type = raw_channel['type']
            channel = None

            bg = BareGuild(raw_channel['guild_id'])

            if ch_type == 'text':
                channel = TextChannel(self.server, raw_channel, bg)
            elif ch_type == 'voice':
                channel = VoiceChannel(self.server, raw_channel, bg)
            else:
                raise Exception(f'Invalid type for channel: {channel_type}')

            self.channels.append(channel)
            chan_count += 1

        log.info('[guild] loaded %d channels', chan_count)

        # guild loading
        cursor = self.guild_coll.find()
        channel_count, guild_count = 0, 0

        for raw_guild in reversed(await cursor.to_list(length=None)):
            guild_id = raw_guild['guild_id']

            raw_guild_members = self.raw_members.get(int(guild_id), {})

            # This loads raw members into mongo if they don't exist
            for user_id in raw_guild['member_ids']:
                if user_id in raw_guild_members:
                    continue

                raw_member = {
                    'guild_id': guild_id,
                    'user_id': user_id,
                    'nick': None,
                    'joined': datetime.datetime.now().isoformat(),
                    'deaf': False,
                    'mute': False,
                }

                await self.member_coll.insert_one(raw_member)
                self.raw_members[guild_id][user_id] = raw_member
                log.debug('Inserting raw member gid=%r uid=%r', guild_id, user_id)

            guild = Guild(self.server, raw_guild)
            self.guilds.append(guild)

            guild_count += 1

        log.info('[guild] Loaded %d guilds', guild_count)

        cursor = self.invite_coll.find()
        invite_count, valid_invites = 0, 0

        for raw_invite in (await cursor.to_list(length=None)):
            invite = Invite(self.server, raw_invite)

            if invite.valid:
                self.invites.append(invite)
                valid_invites += 1
            else:
                await self.delete_invite(invite)

            invite_count += 1

        log.info('[guild] %d valid out of %d invites', valid_invites, invite_count)

        # load messages from database

        cursor = self.message_coll.find().sort('message_id')
        message_count = 0

        for raw_message in reversed(await cursor.to_list(length=200)):
            raw_message['message_id'] = raw_message['message_id']
            channel = self.get_channel(raw_message['channel_id'])

            m = Message(self.server, channel, raw_message)
            self.messages.append(m)
            message_count += 1

        log.info(f'[guild] Loaded %d messages', message_count)
