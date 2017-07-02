import logging
import asyncio
import datetime

from collections import defaultdict

from .objects import Guild, Message, Invite
from .snowflake import get_snowflake, get_invite_code

log = logging.getLogger(__name__)

class GuildManager:
    """Manage guild, channel and message data.

    .. _LitecordServer: server.html
    .. _AsyncIOMotorCollection: https://motor.readthedocs.io/en/stable/api-asyncio/asyncio_motor_collection.html

    Attributes
    ----------
    server : [`LitecordServer`_]
        Server instance.
    guild_db : [`AsyncIOMotorCollection`_]
        Guild database. Handles raw guild data.
    message_db : [`AsyncIOMotorCollection`_]
        Message database. Handles raw message data.
    guilds : dict
        All available :class:`Guild` objects.
    channels : dict
        All available :class:`Channel` objects.
    """
    def __init__(self, server):
        self.server = server

        self.guild_db = server.guild_db
        self.member_db = server.member_db
        self.message_db = server.message_db
        self.invite_db = server.invite_db

        self.guilds = {}
        self.channels = {}
        self.messages = {}
        self.invites = {}
        self.raw_members = defaultdict(dict)

        self.invi_janitor_task = self.server.loop.create_task(self.invite_janitor)

    def get_guild(self, guild_id):
        """Get a :class:`Guild` object by its ID."""
        try:
            guild_id = int(guild_id)
        except:
            return None
        return self.guilds.get(guild_id)

    def get_channel(self, channel_id):
        """Get a :class:`Channel` object by its ID."""
        try:
            channel_id = int(channel_id)
        except:
            return None

        channel = self.channels.get(channel_id)
        if channel is None:
            return None

        async def _updater():
            # Update a channel's last_message_id property
            mlist = await channel.last_messages(1)
            try:
                m_id = mlist[0].id
            except:
                m_id = None
            channel.last_message_id = m_id

        asyncio.async(_updater())
        return channel

    def get_message(self, message_id):
        """Get a :class:`Message` object by its ID."""
        try:
            message_id = int(message_id)
        except:
            return None
        return self.messages.get(message_id)

    def get_guilds(self, user_id):
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
        return [self.guilds[guild_id] for guild_id in self.guilds \
            if user_id in self.guilds[guild_id].member_ids]

    def yield_guilds(self, user_id):
        """Yield all Guilds a user is on."""
        for guild_id, guild in self.guilds.items():
            if user_id in guild.member_ids:
                yield guild

    def get_invite(self, invite_code):
        """Get an :class:`Invite` object."""
        return self.invites.get(invite_code)

    def get_raw_member(self, guild_id, user_id):
        """Get a raw member."""
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
        for guild_id in self.guilds:
            yield self.guilds[guild_id]

    async def all_messages(self, limit=500):
        """Yield `limit` messages, with the 1st being the most recent one."""
        cursor = self.message_db.find().sort('message_id')

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

        Returns a `Message` object.
        """

        message = Message(self.server, channel, raw)

        result = await self.message_db.insert_one(message.as_db)
        log.info(f'Adding message with id {message.id}')

        self.messages[message.id] = message
        await channel.dispatch('MESSAGE_CREATE', message.as_json)

        return message

    async def delete_message(self, message):
        """Delete a message.

        Dispatches MESSAGE_DELETE events to respective clients.

        Parameters
        ----------
        message: :class:`Message`
            Message to delete.

        Returns
        -------
        bool
        """

        result = await self.message_db.delete_one({'message_id': message.id})
        log.info(f"Deleted {result.deleted_count} messages")

        await message.channel.dispatch('MESSAGE_DELETE', {
            'id': str(message.id),
            'channel_id': str(message.channel.id),
        })

        return True

    async def edit_message(self, message, payload):
        """Edit a message.

        Dispatches MESSAGE_UPDATE events to respective clients.

        Parameters
        ----------
        message: :class:`Message`
            Message to edit.

        Returns
        -------
        bool
        """

        new_content = payload['content']
        message.edit(new_content)

        result = await self.message_db.update_one({'message_id': str(message.id)}, {'$set': message.as_db})
        log.info(f"Updated {result.modified_count} messages")

        await message.channel.dispatch('MESSAGE_UPDATE', message.as_json)
        return True

    async def reload_guild(self, old_guild):
        """Reload one guild.

        Used normally after a updating the guild dataabse.
        Updates cache objects.

        Parameters
        ----------
        old_guild: :class:`Guild`
            Guild to be reloaded.

        Returns
        -------
        :class:`Guild`, the new, updated guild object.
        """
        guild_id = old_guild.id

        raw_guild = await self.guild_db.find_one({'id': str(guild_id)})

        guild = Guild(self.server, raw_guild)

        try:
            guild._viewers = old_guild._viewers
        except:
            pass

        self.guilds[guild.id] = guild

        for channel in guild.all_channels():
            self.channels[channel.id] = channel

        return guild

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

        payload['owner_id'] = str(owner.id)
        payload['id'] = str(get_snowflake())
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

        await self.member_db.insert_one(raw_member_owner)
        self.raw_members[int(payload['id'])][owner.id] = raw_member_owner

        guild = Guild(self.server, payload)
        await self.guild_db.insert_one(payload)
        self.guilds[guild.id] = guild

        for channel in guild.all_channels():
            self.channels[channel.id] = channel

        await self.server.presence.status_update(guild, owner)
        await guild.dispatch('GUILD_CREATE', guild.as_json)

        return guild

    async def edit_guild(self, guild, guild_edit_payload):
        """Edit a guild.

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

        Dispatches GUILD_UPDATE events to relevant clients.
        """

        await self.guild_db.update_one({'guild_id': str(guild.id)},
            {'$set': guild_edit_payload})

        guild = await self.reload_guild(guild)

        await guild.dispatch('GUILD_UPDATE', guild.as_json)
        return guild

    async def delete_guild(self, guild):
        pass

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

        result = await self.guild_db.replace_one({'id': str(guild.id)}, raw_guild)
        log.info(f"Updated {result.modified_count} guilds")

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

        await self.member_db.update_one({'guild_id': str(guild.id), 'user_id': str(user.id)},
            {'$set': new_data})

        member.update(new_data)

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

        raw_guild = guild._data
        raw_guild['members'].remove(user_id)

        result = await self.guild_db.update_one({'id': str(guild.id)}, {'$set': raw_guild})
        log.info(f"Updated {result.modified_count} guilds")

        guild = await self.reload_guild(guild)
        await guild.dispatch('GUILD_MEMBER_REMOVE', {
            'guild_id': str(guild.id),
            'user': user.as_json,
        })

        await user.dispatch('GUILD_DELETE', {
            'id': str(guild.id),
            'unavailable': False,
        })

    async def ban_user(self, guild, user):
        """Ban a user from a guild.

        Dispatches GUILD_BAN_ADD and GUILD_MEMBER_REMOVE to relevant clients.
        """

        if user.id in guild.members:
            await self.remove_member(guild, user)

        bans = guild.banned_ids

        try:
            bans.index(str(user.id))
            raise Exception("User already banned")
        except:
            bans.append(str(user.id))

        await self.guild_db.update_one({'id': str(guild.id)},
                                        {'$set': {'bans': bans}})

        await guild.dispatch('GUILD_BAN_ADD',
                            {**user.as_json, **{'guild_id': str(guild.id)}})

    async def unban_user(self, guild, user):
        """Unban a user from a guild.

        Dispatches GUILD_BAN_REMOVE to relevant clients.
        """

        bans = guild.banned_ids

        try:
            bans.remove(str(user.id))
        except:
            raise Exception("User not banned")

        await self.guild_db.update_one({'id': str(guild.id)},
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

        result = await self.guild_db.replace_one({'id': str(guild.id)}, raw_guild)
        log.info(f"Updated {result.modified_count} guilds")

        guild = await self.reload_guild(guild)
        new_channel = guild.channels.get(int(payload['id']))

        await guild.dispatch('CHANNEL_CREATE', new_channel.as_json)
        return new_channel

    async def edit_channel(self, guild, new_payload):
        """Edits a channel in a guild.

        Dispatches CHANNEL_UPDATE to relevant clients.
        """
        pass

    async def delete_channel(self, channel):
        """Deletes a channel from a guild.

        Dispatches CHANNEL_DELETE to relevant clients
        """
        pass

    async def invite_janitor(self):
        """Janitor task for invites.

        This checks every 30 minutes for expired invites and removes them from
        the database.
        """

        try:
            while True:
                cursor = self.invite_db.find()
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
        raw_invite = await self.invite_db.find_one({'code': invi_code})

        while raw_invite is not None:
            invi_code = get_invite_code()
            raw_invite = await self.invite_db.find_one({'code': invi_code})

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

        await self.invite_db.insert_one(raw_invite)

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

        res = await self.invite_db.delete_one({'code': invite.code})
        log.info(f"Removed {res.deleted_count} invites")

        try:
            self.invites.pop(invite.code)
        except:
            pass

    async def init(self):
        """Initialize the GuildManager.

        Loads member data, guild data and messages into memory.
        """
        cursor = self.member_db.find()
        member_count = 0

        for raw_member in await cursor.to_list(length=None):
            self.raw_members[int(raw_member['guild_id'])][int(raw_member['user_id'])] = raw_member
            member_count += 1

        log.info(f'[guild] loaded {member_count} members')

        cursor = self.guild_db.find()
        guild_count = 0

        for raw_guild in reversed(await cursor.to_list(length=None)):
            _guild_id = raw_guild['id']
            guild_id = int(_guild_id)

            raw_guild_members = self.raw_members.get(int(guild_id), {})

            for _member_id in raw_guild['members']:
                member_id = int(_member_id)

                if member_id in raw_guild_members:
                    continue

                raw_member = {
                    'guild_id': _guild_id,
                    'user_id': _member_id,
                    'nick': '',
                    'joined': datetime.datetime.now().isoformat(),
                    'deaf': False,
                    'mute': False,
                }

                await self.member_db.insert_one(raw_member)
                self.raw_members[guild_id][member_id] = raw_member

            guild = Guild(self.server, raw_guild)
            self.guilds[guild.id] = guild

            for channel in guild.all_channels():
                self.channels[channel.id] = channel
            guild_count += 1

        log.info(f'[guild] Loaded {guild_count} guilds')

        cursor = self.invite_db.find()
        invite_count = 0
        valid_invites = 0

        for raw_invite in (await cursor.to_list(length=None)):
            invite = Invite(self.server, raw_invite)

            if invite.valid:
                self.invites[invite.code] = invite
                valid_invites += 1
            else:
                await self.delete_invite(invite)

            invite_count += 1

        log.info(f'[guild] Loaded {valid_invites} valid out of {invite_count} invites')

        # load messages from database

        cursor = self.message_db.find().sort('message_id')
        message_count = 0

        for raw_message in reversed(await cursor.to_list(length=200)):
            raw_message['id'] = raw_message['message_id']
            channel = self.get_channel(raw_message['channel_id'])

            m = Message(self.server, channel, raw_message)
            self.messages[m.id] = m
            message_count += 1

        log.info(f'[guild] Loaded {message_count} messages')
