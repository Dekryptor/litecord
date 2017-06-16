import logging
import datetime

from .basics import CHANNEL_TO_INTEGER
from .utils import strip_user_data, dt_to_json
from .snowflake import snowflake_time


log = logging.getLogger(__name__)


class LitecordObject:
    """A general Litecord object.

    Attributes
    ----------
    server: :class:`LitecordServer`
        Server instance
    """
    def __init__(self, server):
        self.server = server

    @property
    def guild_man(self):
        """The server's :class:`GuildManager`."""
        # This property is needed for things to work
        # since guild_man is None when initializing databases
        return self.server.guild_man

    @property
    def as_db(self):
        """Get a version of the object to be inserted into the database."""
        raise NotImplementedError('This instance didn\'t implement as_db')

    @property
    def as_json(self):
        """Return a JSON serializable object representing itself.

        NOTE: it is recommended to not give sensitive information through `as_json`
            as it is usually used to send the object to a client.
        """
        raise NotImplementedError('This instance didn\'t implement as_json')

    def iter_json(self, indexable):
        """Get all objects from an indexable, in JSON serializable form"""
        return [indexable[index].as_json for index in indexable]


class Presence:
    """A presence object.

    Presence objects are used to signal clients that someone is playing a game,
    or that someone went Online, Idle/AFK or DnD(Do not Disturb).

    Parameters
    ----------
    guild: :class:`Guild`
        Guild that this presence object relates to.
    user: :class:`User`
        User that this presence object relates to.
    status: dict, optional
        Status data to load into the presence object.

    Attributes
    ----------
    game: dict
        The currently playing game/status.
    user: :class:`User`
        The user that this presence object is linked to.
    guild: :class:`Guild`
        Guild that this presence object relates to.
    """

    __slots__ = ('game', 'user', 'guild')

    def __init__(self, guild, user, status=None):
        _default = {
            'status': 'online',
            'type': 0,
            'name': None,
            'url': None,
        }

        # merge the two, with game overwriting _default
        self.game = {**_default, **status}
        self.user = user
        self.guild = guild

        if self.game['status'] not in ('online', 'offline', 'idle', 'dnd'):
            log.warning(f'Presence for {self.user!r} with unknown status')

    def __repr__(self):
        return f'Presence({self.user!s}, {self.game["status"]!r}, {self.game["name"]!r})'

    @property
    def as_json(self):
        return {
            # Discord sends an incomplete user object with all optional fields(excluding id)
            # we are lazy, so we send the same user object you'd receive in other normal events :^)
            'user': self.user.as_json,
            'guild_id': str(self.guild.id),
            'roles': [],
            'game': {
                'type': self.game.get('type'),
                'name': self.game.get('name'),
                'url': self.game.get('url'),
            },
            'status': self.game.get('status'),
        }


class User(LitecordObject):
    """A general user object.

    Parameters
    ----------
    server: :class:`LitecordServer`
        Server instance
    _data: dict
        Raw user data.

    Attributes
    ----------
    _data: dict
        Raw user data.
    id: int
        Snowflake ID of this user.
    username: str
        User's username.
    discriminator: str
        User's discriminator.
    avatar_hash: str
        User's avatar hash, used to retrieve the user's avatar data.
    email: str
        User's email, can be :py:const:`None`
    admin: bool
        Flag that shows if the user is an admin user.
    """

    __slots__ = ('_data', 'id', 'username', 'discriminator', 'avatar_hash',
        'email', 'admin')

    def __init__(self, server, _data):
        super().__init__(server)
        self._data = _data

        self.id = int(_data['id'])
        self.username = _data['username']
        self.discriminator = _data['discriminator']
        self.avatar_hash = _data['avatar']

        self.email = _data.get('email')
        self.admin = _data.get('admin', False)

    def __str__(self):
        return f'{self.username}#{self.discriminator}'

    def __repr__(self):
        return f'User({self.id}, {self.username}#{self.discriminator})'

    @property
    def guilds(self):
        """Yield all guilds a user is in."""
        for guild in self.guild_man.all_guilds():
            if self.id in guild.member_ids:
                yield guild

    @property
    def members(self):
        """Yield all members a user has attached."""
        for guild in self.guilds:
            yield guild.members[self.id]

    @property
    def as_json(self):
        """Remove sensitive data from `User._data` and make it JSON serializable"""
        return strip_user_data(self._data)

    @property
    def connections(self):
        """Yield all connections that are related to this user."""
        for conn in self.server.get_connections(self.id):
            yield conn

    @property
    def online(self):
        """Returns boolean if the user has at least 1 connection attached to it"""
        return len(list(self.server.get_connections(self.id))) > 0

    async def dispatch(self, evt_name, evt_data):
        """Dispatch an event to all connections a user has.

        Parameters
        ----------
        evt_name: str
            Event name.
        evt_data: any
            Event data.

        Returns
        -------
        bool

        """
        log.debug(f"Dispatching {evt_name} to {self.id}")
        _conns = list(self.connections)
        if len(_conns) < 1:
            return False

        for conn in _conns:
            try:
                await conn.dispatch(evt_name, evt_data)
                log.debug(f"Dispatched to {conn.session_id!r}")
            except:
                log.debug(f"Failed to dispatch event to {conn.session_id!r}")

        return True


class Member(LitecordObject):
    """A general member object.

    A member is linked to a guild.

    Parameters
    ----------
    server: :class:`LitecordServer`
        server instance.
    guild: :class:`Guild`
        The guild this member is from.
    user: :class:`User`
        The user this member represents.
    raw_member: dict
        Raw member data.

    Attributes
    ----------
    _data: dict
        Raw member data.
    user: :class:`User`
        The user this member represents.
    guild: :class:`Guild`
        The guild this member is from.
    id: int
        The member's snowflake ID. This is the same as :py:meth:`User.id`.
    owner: bool
        If the member is the guild owner.
    nick: str
        Member's nickname, becomes :py:const:`None` if no nickname is set.
    joined_at: datetime.datetime
        The date where this member was created in the guild
    roles: List[:class:`Role`]
        List of roles this member has.
    voice_deaf: bool
        If the member is deafened on the guild.
    voice_mute: bool
        If the member is muted on the guild.
    """

    __slots__ = ('_data', 'user', 'guild', 'id', 'owner', 'nick', 'joined_at',
        'roles', 'voice_deaf', 'voice_mute')

    def __init__(self, server, guild, user, raw_member):
        super().__init__(server)
        self._data = raw_member
        self.user = user
        self.guild = guild

        self.id = self.user.id
        self.owner = self.id == self.guild.owner_id
        self.nick = raw_member.get('nick')

        joined_timestamp = raw_member.get('joined')

        if joined_timestamp is not None:
            self.joined_at = datetime.datetime.strptime(joined_timestamp, \
                "%Y-%m-%dT%H:%M:%S.%f")
        else:
            log.warning("Member without joined timestamp.")

        self.roles = []
        self.voice_deaf = False
        self.voice_mute = False

    def __repr__(self):
        return f'Member({self.user!r}, {self.guild!r})'

    def update(self, new_data):
        """Update a member object based on new data."""
        self.nick = new_data.get('nick') or self.nick

    @property
    def connections(self):
        """Yield the user's connections."""
        return self.user.connections

    async def dispatch(self, evt_name, evt_data):
        """Dispatch an event to a member.

        Dispatches an event in the same way :py:meth:`User.dispatch` does.
        """
        return await self.user.dispatch(evt_name, evt_data)

    @property
    def as_json(self):
        return {
            'user': self.user.as_json,
            'nick': self.nick,
            'roles': self.roles,
            'joined_at': dt_to_json(self.joined_at),
            'deaf': self.voice_deaf,
            'mute': self.voice_mute,
        }

    @property
    def as_invite(self):
        """Returns a version to be used in :py:meth:`Invite.as_json`."""
        return {
            'username': self.user.username,
            'discriminator': str(self.user.discriminator),
            'id': str(self.user.id),
            'avatar': self.user.avatar_hash,
        }

    @property
    def user_guild(self):
        return {
            'id': str(self.guild.id),
            'name': self.guild.name,
            'icon': self.guiild.icons['icon'],
            'owner': self.owner,
            'permissions': 0,
        }

class BaseChannel(LitecordObject):
    """A general base channel object.

    Parameters
    ----------
    server: :class:`LitecordServer`
        Server instance.
    _channel: dict
        Raw channel data.
    guild: :class:`Guild`, optional
        Guild that this channel refers to.

    Attributes
    ----------
    _data: dict
        Raw channel data.
    id: int
        The channel's snowflake ID.
    guild_id: int
        The guild's ID this channel is in.
    guild: :class:`Guild`
        The guild that this channel refers to, can be :py:const:`None`.
    name: str
        Channel's name.
    type: int
        Channel's type.
    str_type: str
        Channel's type as a string. Usually it is ``"text"``.
    position: int
        Channel's position on the guild, channel position starts from 0.
    is_private: bool
        Should be False.
    is_default: bool
        If this channel is the default for the guild.
    """

    __slots__ = ('_data', 'id', 'guild_id', 'guild', 'name', 'type', 'str_type',
        'position', 'is_private', 'is_default')

    def __init__(self, server, _channel, guild=None):
        super().__init__(server)
        self._data = _channel
        self.id = int(_channel['id'])
        self.guild_id = int(_channel['guild_id'])

        if guild is None:
            self.guild = self.guild_man.get_guild(self.guild_id)
        else:
            self.guild = guild

        if self.guild is None:
            log.error("Creating an orphaned Channel")

        self.name = _channel['name']
        self.str_type = _channel['type']
        self.type = CHANNEL_TO_INTEGER[_channel['type']]
        self.position = _channel['position']
        self.is_private = False
        self.is_default = self.id == self.guild_id

    @property
    def watchers(self):
        """Yields all :class:`Member` who are online and can watch the channel."""
        for member in self.guild.online_members:
            #if member.channel_perms[self.id].READ_MESSAGES: yield member
            yield member

    async def dispatch(self, evt_name, evt_data):
        """Dispatch an event to all channel watchers."""
        dispatched = 0
        for member in self.guild.viewers:
            if (await member.dispatch(evt_name, evt_data)):
                dispatched += 1

        log.debug(f'Dispatched {evt_name} to {dispatched} channel watchers')

        return dispatched

    @property
    def as_invite(self):
        return {
            'id': str(self.id),
            'name': self.name,
            'type': self.type,
        }


class TextChannel(BaseChannel):
    """Represents a text channel.

    Attributes
    ----------
    topic: str
        Channel topic/description.
    last_message_id: int
        The last message created in the channel.

    """
    def __init__(self, server, raw_channel, guild=None):
        super().__init__(server, raw_channel, guild)

        self.topic = raw_channel['topic']
        self.last_message_id = 0

    def get_message(self, message_id):
        """Get a single message from a channel."""
        try:
            m = self.server.guild_man.get_message(message_id)
            if m.channel.id == self.id:
                return m
        except AttributeError:
            pass
        return None

    async def last_messages(self, limit=50):
        """Get the last messages from a text channel.

        Returns
        -------
        list: list of :py:meth:`Message`
            Ordered(by time) list of message objects.
        """
        res = []
        cursor = self.server.message_db.find({'channel_id': self.id}).sort('message_id')

        for raw_message in reversed(await cursor.to_list(length=limit)):
            if len(res) > limit: break
            m_id = raw_message['message_id']
            raw_message['id'] = m_id

            if m_id in self.guild_man.messages:
                res.append(self.guild_man.messages[m_id])
            else:
                m = Message(self.server, self, raw_message)
                self.guild_man.messages[m_id] = m

                res.append(m)

        return res

    @property
    def as_json(self):
        return {
            'id': str(self.id),
            'guild_id': str(self.guild_id),
            'name': self.name,
            'type': self.type,
            'position': self.position,
            'is_private': self.is_private,
            'permission_overwrites': [],
            'topic': self.topic,
            'last_message_id': str(self.last_message_id),
        }


class VoiceChannel(BaseChannel):
    """Represents a voice channel.

    Attributes
    ----------
    bitrate: int
        Voice channel's bitrate.
    user_limit: int
        Maximum number of users that can enter the channel.
    """

    __slots__ = ('base', 'bitrate', 'user_limit')

    def __init__(self, server, raw_channel, guild=None):
        super().__init__(server, raw_channel, guild)

        self.bitrate = raw_channel.get('birtate', 69)
        self.user_limit = raw_channel.get('user_limit', 0)

    async def voice_request(self, connection):
        """Request a voice state from the voice manager."""
        return await self.server.voice.link_connection(connection, self)

    @property
    def as_json(self):
        return {
            'id': str(self.id),
            'guild_id': str(self.guild_id),
            'name': self.name,
            'type': self.type,
            'position': self.position,
            'is_private': self.is_private,
            'permission_overwrites': [],

            'bitrate': self.bitrate,
            'user_limit': self.user_limit,
        }


class Guild(LitecordObject):
    """A general guild.

    Parameters
    ----------
    server: :class:`LitecordServer`
        Server instance.
    _guild_data: dict
        Raw gulid data.

    Attributes
    ----------
    _data: dict
        Raw guild data.
    _channel_data: list(raw channel)
        Raw channel data for the guild.
    _role_data: list(:class:`Role`)
        Raw role data for the guild.

    id: int
        The guild's snowflake ID.
    name: str
        Guild's name.
    icons: dict
        Contains two keys: ``"icon"`` and ``"splash"``.
    created_at: datetime.datetime
        Guild's creation date.
    owner_id: int
        Guild owner's ID.
    region: str
        Guild's voice region.
    features: list(str)
        Features this guild has.
    channels: dict
        Channels this guild has.
    member_ids: list(int)
        Guild member ids.
    members: dict
        Members this guild has.
    member_count: int
        Amount of members in this guild.
    banned_ids: list(str)
        User IDs that are banned in this guild.
    _viewers: list(int)
        List of user IDs that are viewers of this guild and will have specific
        guild events dispatched to them.

    TODO:
        roles: A list of `Role` objects.
        emojis: A list of `Emoji` objects.
    """

    __slots__ = ('_data', 'channel_data', '_role_data', 'id', 'name', 'icons',
        'created_at', 'owner_id', 'features', 'channels', 'member_ids',
        'members', 'member_count', 'roles', 'emojis', 'banned_ids', '_viewers')

    def __init__(self, server, _guild_data):
        super().__init__(server)
        self._data = _guild_data
        self.id = int(_guild_data['id'])
        self.name = _guild_data['name']
        self.icons = {
            'icon': _guild_data['icon'],
            'splash': '',
        }

        creation_timestamp = snowflake_time(self.id)
        self.created_at = datetime.datetime.fromtimestamp(creation_timestamp)

        self.owner_id = int(_guild_data['owner_id'])
        self.region = _guild_data['region']
        self.emojis = []
        self.features = _guild_data['features']

        self._channel_data = _guild_data['channels']
        self.channels = {}

        for raw_channel in self._channel_data:
            raw_channel['guild_id'] = self.id
            channel_type = raw_channel['type']
            channel = None

            if channel_type == 'text':
                channel = TextChannel(server, raw_channel, self)
            elif channel_type == 'voice':
                channel = VoiceChannel(server, raw_channel, self)
            else:
                raise Exception(f'Invalid type for channel: {channel_type}')

            self.channels[channel.id] = channel

        # list of snowflakes
        self.member_ids = [int(member_id) for member_id in _guild_data['members']]
        self.members = {}

        for member_id in self.member_ids:
            member_id = int(member_id)

            user = self.server.get_user(member_id)
            if user is None:
                log.warning(f"user {member_id} not found")
                continue

            raw_member = server.guild_man.get_raw_member(self.id, user.id)

            member = Member(server, self, user, raw_member)
            self.members[member.id] = member

        self.owner = self.members.get(self.owner_id)
        if self.owner is None:
            log.error("Guild without owner!")

        self._role_data = _guild_data['roles']
        self.roles = {}

        for raw_role in self._role_data:
            role = Role(server, self, raw_role)
            self.roles[role.id] = role

        self.banned_ids = _guild_data.get('bans', [])

        self.member_count = len(self.members)
        self._viewers = []

    def __repr__(self):
        return f'Guild({self.id}, {self.name!r})'

    def mark_watcher(self, user_id):
        """Mark a user ID as a viewer in that guild, meaning it will receive
        events from that gulid using :py:meth:`Guild.dispatch`.
        """
        user_id = int(user_id)
        try:
            self._viewers.index(user_id)
        except:
            self._viewers.append(user_id)
            log.debug(f'Marked {user_id} as watcher of {self!r}')

    def unmark_watcher(self, user_id):
        """Unmark user from being a viewer in this guild."""
        user_id = int(user_id)
        try:
            self._viewers.remove(user_id)
            log.debug(f'Unmarked {user_id} as watcher of {self!r}')
        except:
            pass

    def all_channels(self):
        """Yield all channels from a guild"""
        for channel in self.channels.values():
            yield channel

    @property
    def voice_channels(self):
        """Yield all voice channels from a guild."""
        for channel in self.all_channels():
            if channel.str_type == 'voice':
                yield channel

    def all_members(self):
        """Yield all members from a guild"""
        for member in self.members.values():
            yield member

    @property
    def viewers(self):
        """Yield all members that are viewers of this guild.

        Keep in mind that :py:meth:`Guild.viewers` is different from :py:meth:`Guild.online_members`.

        Members are viewers automatically, but if they are Atomic-Discord clients,
        they only *are* viewers if they send a OP 12 Guild Sync(:py:meth:`Connection.guild_sync_handler`)
        to the gateway.
        """
        for member in self.members.values():
            try:
                self._viewers.index(member.id)
                yield member
            except:
                pass

    @property
    def online_members(self):
        """Yield all members that have an identified connection"""
        for member in self.members.values():
            if member.user.online:
                yield member

    @property
    def presences(self):
        """Returns a list of :class:`Presence` objects for all online members."""
        return [self.server.presence.get_presence(self.id, member.id).as_json \
            for member in self.online_members]

    async def dispatch(self, evt_name, evt_data):
        """Dispatch an event to all guild viewers.

        Parameters
        ----------
        evt_name: str
            Event name.
        evt_data: dict
            Event data.

        Returns
        -------
        int:
            Total number of members that this event was dispatched to.
        """
        total, dispatched = 0, 0

        for member in self.viewers:
            success = await member.dispatch(evt_name, evt_data)

            if not success:
                self.unmark_watcher(member.id)
            else:
                dispatched += 1
            total += 1

        log.debug(f'Dispatched {evt_name} to {dispatched}/{total} gulid viewers')

        return dispatched

    async def add_member(self, user):
        """Add a :class:`User` to a guild.

        Returns
        -------
        :class:`Member`.
        """

        return (await self.guild_man.add_member(self, user))

    async def ban(self, user):
        """Ban a user from the guild.

        Raises
        ------
        Exception on failure.
        """
        await self.guild_man.ban_user(self, user)

    async def unban(self, user):
        """Unban a user from the guild.

        Raises
        ------
        Exception on failure.
        """
        await self.guild_man.unban_user(self, user)

    async def edit(self, edit_payload):
        """Edit a guild.

        Returns
        -------
        :class:`Guild`
            The edited guild as a object.
        """
        return await self.guild_man.edit_guild(self, edit_payload)

    async def create_channel(self, chan_create_payload):
        """Create a channel in a guild.

        Returns
        -------
        :class:`Channel`
            New channel.
        """
        return await self.guild_man.create_channel(self, chan_create_payload)

    @property
    def as_json(self):
        return {
            'id': str(self.id),
            'name': self.name,
            'icon': self.icons['icon'],
            'splash': self.icons['splash'],
            'owner_id': str(self.owner_id),
            'region': self.region,

            # voice things aka NOT USABLE
            'afk_channel_id': '00000000000',
            'afk_timeout': None,

            # TODO: how are these supposed to even work?
            'embed_enabled': None,
            'embed_channel_id': None,

            'verification_level': 0, # TODO
            'default_message_notifications': -1, # TODO
            'roles': self.iter_json(self.roles),
            'emojis': self.emojis,
            'features': self.features,
            'mfa_level': -1, # TODO

            # those fields are only in the GUILD_CREATE event
            # but we can send them anyways :')
            # usually clients ignore this, so we don't need to worry

            'joined_at': dt_to_json(self.created_at),
            'large': self.member_count > 250,
            'unavailable': False,
            'member_count': self.member_count,
            'voice_states': [],

            # arrays of stuff
            'members': self.iter_json(self.members),
            'channels': self.iter_json(self.channels),
            'presences': self.presences,
        }

    @property
    def as_invite(self):
        return {
            'id': str(self.id),
            'name': self.name,
            'icon': self.icons['icon'],
            'splash': self.icons['splash'],
        }

class Role(LitecordObject):
    """A role object.

    Parameters
    ----------
    server: :class:`LitecordServer`
        Server instance.
    guild: :class:`Guild`
        Guild that this role is from.
    _data: dict
        Raw role data.

    Attributes
    ----------
    _data: dict
        Raw role data.
    id: int
        Role ID
    guild: :class:`Guild`
        Guild that this role comes from.
    name: str
        Name of the role.
    color: int
        Role's color.
    hoist: bool
        If the role is hoisted. Hoisted roles means they'll appear seperately
        in the member list.
    position: int
        Role's position.
    permissions: int
        Role's permission number.
    managed: bool
        If this role is managed by a bot application, should be ``False``.
    mentionable: bool
        If this role can be mentioned by another users.
    """

    __slots__ = ('_data', 'id', 'guild', 'name', 'color', 'hoist', 'position',
        'position', 'permissions', 'managed', 'mentionable')

    def __init__(self, server, guild, _data):
        super().__init__(server)
        self._data = _data

        self.id = int(_data['id'])
        self.guild = guild

        if self.id == guild.id:
            self.name = '@everyone'
        else:
            self.name = _data['name']

        self.color = _data.get('color', 0)
        self.hoist = _data.get('hoisted', False)
        self.position = _data.get('position', 0)
        self.permissions = _data.get('permissions', 0)
        self.managed = False
        self.mentionable = _data.get('mentionable')

    @property
    def as_db(self):
        return {
            'id': str(self.id),
            'name': self.name,
            'color': self.color,
            'hoist': self.hoist,
            'position': self.position,
            'permissions': self.permissions,
            'managed': self.managed,
            'mentionable': self.mentionable,
        }

    @property
    def as_json(self):
        return {
            'id': str(self.id),
            'name': self.name,
            'color': self.color,
            'hoist': self.hoist,
            'position': self.position,
            'permissions': self.permissions,
            'managed': self.managed,
            'mentionable': self.mentionable,
        }

class Invite(LitecordObject):
    """An invite object.

    Parameters
    ----------
    server: :class:`LitecordServer`
        Server instance.
    _data: dict
        Raw invite data.

    Attributes
    ----------
    _data: dict
        Raw invite object.
    code: str
        Invite code.
    channel_id: int
        Channel's ID being reffered in this invite.
    channel: :class:`Channel`
        Channel being reffered in ``channel_id``. Can be :py:const:`None`.
    inviter_id: int
        User's ID who made the invite.
    inviter: :class:`User`
        User who made the invite. Can be :py:const:`None`.
    temporary: bool
        Flag if this invite is temprary or not.
    uses: int
        Uses this invite has. If the invite is infinite, this becomes ``-1``.
    iso_timestamp: str
        A ISO 8601 formatted string.
    infinite: bool
        Flag if this invite is infinite or not.
    expiry_timestamp: `datetime.datetime`
        If the invite is not infinite, this is the date when the invite will
        expire and be invalid.
        If not, this becomes :py:const:`None`.
    """

    __slots__ = ('_data', 'code', 'channel_id', 'channel', 'inviter_id', 'inviter'
        'temporary', 'uses', 'iso_timestamp', 'infinite', 'expiry_timestamp')

    def __init__(self, server, _data):
        super().__init__(server)
        self.server = server
        self._data = _data

        self.code = _data['code']
        self.channel_id = int(_data['channel_id'])

        self.channel = server.guild_man.get_channel(self.channel_id)
        if self.channel is None:
            log.warning("Orphan invite (channel)")

        guild = self.channel.guild

        self.inviter_id = int(_data['inviter_id'])
        self.inviter = guild.members.get(self.inviter_id)
        if self.inviter is None:
            log.warning("Orphan invite (inviter)")

        self.temporary = _data.get('temporary', False)

        self.uses = _data.get('uses', -1)

        self.iso_timestamp = _data.get('timestamp', None)
        self.infinite = True
        self.expiry_timestamp = None

        if self.iso_timestamp is not None:
            self.infinite = False
            self.expiry_timestamp = datetime.datetime.strptime(self.iso_timestamp, \
                "%Y-%m-%dT%H:%M:%S.%f")

    @property
    def valid(self):
        """Returns a boolean representing the validity of the invite"""
        if self.channel is None:
            return False

        if not self.infinite:
            now = datetime.datetime.now()

            if now.timestamp() > self.expiry_timestamp.timestamp():
                return False

        # check uses
        if self.uses == -1:
            return True

        if self.uses < 1:
            return False

        return True

    def use(self):
        """Returns a boolean on success/failure of using an invite"""
        if self.channel is None:
            return False

        if not self.infinite:
            now = datetime.datetime.now()

            if now.timestamp() > self.expiry_timestamp.timestamp():
                return False

        # check uses
        if self.uses == -1:
            return True

        if self.uses < 1:
            return False

        self.uses -= 1
        return True

    async def update(self):
        """Update an invite in the database."""
        res = await self.server.invite_db.replace_one({'code': self.code}, self.as_db)
        log.info(f"Updated {res.modified_count} invites")

    @property
    def sane(self):
        """Checks if an invite is sane."""
        return self.channel is not None

    @property
    def as_db(self):
        return {
            'code': self.code,
            'channel_id': str(self.channel_id),
            'timestamp': self.iso_timestamp,
            'uses': self.uses,
            'temporary': self.temporary,
            'unique': True,
        }

    @property
    def as_json(self):
        return {
            'code': self.code,
            'guild': self.channel.guild.as_invite,
            'channel': self.channel.as_invite,
            'inviter': self.inviter.as_invite,
        }


class Message(LitecordObject):
    """A general message object.

    Parameters
    ----------
    server: :class:`LitecordServer`
        Server instance.
    channel: :class:`Channel`
        Channel that this message comes from.
    _message_data: dict
        Raw message data.

    Attributes
    ----------
    _data: dict
        Raw message data.
    id: int
        Message's snowflake ID.
    author_id: int
        Message author's snowflake ID.
    channel_id: int
        Message channel's snowflake ID.
    timestamp: `datetime.datetime`
        Message's creation time.
    channel: :class:`Channel`
        Channel where this message comes from.
    author: :class:`User`
        The user that made the message, can be :py:const:`None`.
    member: :class:`Member`
        Member that made the message, can be :py:const:`None`..
    content: str
        Message content.
    edited_at: `datetime.datetime`
        Default is :py:const:`None`.
        If the message was edited, this is set to the time at which this message was edited.
    """

    __slots__ = ('_data', 'id', 'author_id', 'channel_id', 'timestamp', 'channel',
        'author', 'member', 'content', 'edited_at')

    def __init__(self, server, channel, _message_data):
        super().__init__(server)
        self._data = _message_data

        self.id = int(_message_data['id'])
        self.author_id = int(_message_data['author_id'])
        if channel is None:
            log.warning(f"Orphaned message {self.id}")
            return

        self.channel_id = channel.id

        self.timestamp = datetime.datetime.fromtimestamp(snowflake_time(self.id))

        self.channel = channel
        self.author = self.server.get_user(self.author_id)
        self.member = self.channel.guild.members.get(self.author_id)

        if self.member is None:
            log.warning("Message being created with invalid userID [member not found]")

        self.content = _message_data['content']
        self.edited_at = _message_data.get('edited_timestamp', None)

    def edit(self, new_content, timestamp=None):
        """Edit a message object"""
        if timestamp is None:
            timestamp = datetime.datetime.now()

        self.edited_at = timestamp
        self.content = new_content

    @property
    def as_db(self):
        return {
            'message_id': int(self.id),
            'channel_id': int(self.channel_id),
            'author_id': int(self.author.id),

            'edited_timestamp': dt_to_json(self.edited_at),

            'content': str(self.content),
        }

    @property
    def as_json(self):
        return {
            'id': str(self.id),
            'channel_id': str(self.channel_id),
            'author': self.author.as_json,
            'content': self.content,
            'timestamp': dt_to_json(self.timestamp),
            'edited_timestamp': dt_to_json(self.edited_at),
            'tts': False,
            'mention_everyone': '@everyone' in self.content,

            'mentions': [], # TODO
            'mention_roles': [], # TODO?
            'attachments': [], # TODO
            'embeds': [], # TODO
            'reactions': [], # TODO
            'pinned': False, # TODO
            #'webhook_id': '',
        }
