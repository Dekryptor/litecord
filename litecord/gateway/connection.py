import logging
import asyncio
import random
import collections

from voluptuous import Schema, Optional, REMOVE_EXTRA

from ..enums import OP, CloseCodes
from ..utils import chunk_list
from ..err import VoiceError, InvalidateSession
from ..ratelimits import ws_ratelimit

from ..ws import WebsocketConnection, handler, StopConnection, get_data_handlers
from .state import ConnectionState, RESUME_MAX_EVENTS

# Maximum amount of tries to generate a session ID.
MAX_TRIES = 20

# Heartbeating intervals, actual heartbeating interval is random value
# between HB_MIN_MSEC and HB_MAX_MSEC
HB_MIN_MSEC = 40000
HB_MAX_MSEC = 42000

# Amount of server identifiers to be generated
SERVER_AMOUNT = 5

log = logging.getLogger(__name__)

SERVERS = {
    'main': [f'gateway-main-{random.randint(1, 99)}' for i in range(SERVER_AMOUNT)],
    'hello': [f'litecord-hello-{random.randint(1, 99)}' for i in range(SERVER_AMOUNT)],
    'ready': [f'litecord-session-{random.randint(1, 99)}' for i in range (SERVER_AMOUNT)],
    'resume': [f'litecord-resumer{random.randint(1, 99)}' for i in range(SERVER_AMOUNT)],
}


class Connection(WebsocketConnection):
    """Represents a websocket connection to Litecord.

    .. _the documentation about it here: https://discordapp.com/developers/docs/topics/gateway
    .. _WebSocketServerProtocol: https://websockets.readthedocs.io/en/stable/api.html#websockets.server.WebSocketServerProtocol

    This connection handles the Gateway API v6,
    you can find `the documentation about it here`_.

    Attributes
    ----------
    ws: `WebSocketServerProtocol`_
        The actual websocket connection.
    loop: `event loop`
        Event loop.
    options: dict
        Websocket options, encoding, gateway version.
    server: :class:`LitecordServer`
        Litecord server instance

    state: :class:`ConnectionState`
        Connection state instance.
    session_id: str
        Connection's session id.

    encoder: function
        Encoder function that convers objects to the provided encoding over :attr:`Connection.options`
    decoder: function
        Decoder function that converts messages from the websocket to objects.

    hb_interval: int
        Amount, in milliseconds, of the client's heartbeat period.
    wait_task: `asyncio.Task` or None
        Check :meth:`Connection.hb_wait_task` for more details.

    ratelimit_tasks: dict
        Tasks that clean the specified ratelimit bucket in a period of time.
    request_counter: dict
        A request counter for ratelimit buckets.

    identified: bool
        Connection had a successful `IDENTIFY` or not.
    properties: dict
        Connection properties like OS, browser and the the ``large_threshold``.
    
    user: :class:`User`
        Becomes a user object if the connection is properly identified.

    """
    def __init__(self, ws, **kwargs):
        super().__init__(ws)
        self.ws = ws
        self.loop = ws.loop
        self.options = kwargs['config']
        self.server = kwargs['server']

        self.state = None
        self.session_id = None

        self._encoder, self._decoder = get_data_handlers(self.options[1])

        # Client's heartbeat interval, chose at random between 40 and 42sec
        self.hb_interval = random.randint(HB_MIN_MSEC, HB_MAX_MSEC)
        self.wait_task = None

        # ratelimiting tasks that clean the request counter
        self.ratelimit_tasks = {}
        self.request_counter = {}

        # some flags for the client etc
        self.identified = False
        self.dispatch_lock = asyncio.Lock()

        # references to objects
        self.guild_man = self.server.guild_man
        self.presence = self.server.presence
        self.relations = self.server.relations
        self.settings = self.server.settings

        _o = Optional
        self.identify_schema = Schema({
            'token': str,
            'properties': dict,
            _o('compress'): bool,
            'large_threshold': int,
            'shard': list,
        }, extra=REMOVE_EXTRA)

        self.resume_schema = Schema({
            'token': str,
            'session_id': str,
            'seq': int
        }, extra=REMOVE_EXTRA)

        self.req_guild_schema = Schema({
            'guild_id': str,
            'query': str,
            'limit': int,
        }, extra=REMOVE_EXTRA)

    def __repr__(self):
        if getattr(self, 'session_id', None) is None:
            return f'<Connection identified={self.identified}>'

        return f'<Connection sid={self.esssion_id} u={self.state.user!r}>'

    def get_identifiers(self, module: str) -> list:
        """Get a server identifier for a module."""
        main = random.choice(SERVERS['main'])
        current = SERVERS.get(module)
        if not current:
            return [main]

        return [random.choice(current), main]

    def basic_hello(self) -> dict:
        """Returns a JSON serializable OP 10 Hello packet."""
        return {
            'op': OP.HELLO,
            'd': {
                'heartbeat_interval': self.hb_interval,
                '_trace': self.get_identifiers('hello'),
            }
        }

    def _register_payload(self, sent_seq, payload):
        """Register a sent payload.

        Calls :meth:`ConnectionState.add`.
        Overwrites :attr:`ConnectionState.sent_seq`.
        """
        self.state.sent_seq = sent_seq

        if payload['op'] not in (OP.DISPATCH,):
            return

        if payload.get('t') in ('READY', 'RESUMED'):
            return

        self.state.add(payload)

    async def dispatch(self, evt_name, evt_data=None):
        """Send a DISPATCH packet through the websocket.

        Parameters
        ----------
        evt_name: str
            Follows the same pattern as Discord's event names.
        evt_data: any
            Any JSON serializable object, ``None`` by default.
            If this has an `as_json` property, it gets used.
        """

        await self.dispatch_lock

        if not self.identified:
            log.warning('[dispatch] cannot dispatch to non-identified')
            self.dispatch_lock.release()
            return -1

        if hasattr(evt_data, 'as_json'):
            evt_data = evt_data.as_json

        self.state.sent_seq += 1

        payload = {
            'op': OP.DISPATCH,
            's': self.state.sent_seq,
            't': evt_name,
            'd': evt_data,
        }

        amount = None

        # dude fuck discord.js (2)
        # This compress_flag is required to be used only on READY
        # because d.js is weird with its compression and ETF at the same time.
        if evt_name == 'READY':
            amount = await self.send(payload, compress=self.state.compress)
        else:
            amount = await self.send(payload)

        log.info(f'[dispatch] {evt_name}, {amount} bytes, compress: {self.state.compress}')
        self._register_payload(self.state.sent_seq, payload)

        self.dispatch_lock.release()
        return amount

    @property
    def is_atomic(self) -> bool:
        """Returns if this connection represents an Atomic Discord client."""
        return self.server.atomic_markers.get(self.session_id, False)

    async def hb_wait_task(self):
        """This task automatically closes clients that didn't heartbeat in time."""
        try:
            log.debug(f'Waiting for heartbeat {(self.hb_interval / 1000) + 3}s')
            await asyncio.sleep((self.hb_interval / 1000) + 3)
            log.info(f'Heartbeat expired for sid=%s', self.session_id)
            await self.ws.close(CloseCodes.UNKNOWN_ERROR, 'Heartbeat expired')
        except asyncio.CancelledError:
            log.debug("[heartbeat_wait] cancelled")

    @handler(OP.HEARTBEAT)
    async def heartbeat_handler(self, data):
        """Handle OP 1 Heartbeat packets.
        Replies with OP 11 Heartbeat ACK packet.

        Creates an `asyncio.Task` with :meth:`Connection.hb_wait_task`
        so the connection gets closed if they don't hearbeat on time.

        Parameters
        ----------
        data: int or :py:const:`None`
            Sequence number to be saved in :attr:`ConnectionState.recv_seq`
        """
        try:
            self.wait_task.cancel()
        except AttributeError: pass

        self.state.recv_seq = int(data)
        self.wait_task = self.loop.create_task(self.hb_wait_task())
        await self.send_op(OP.HEARTBEAT_ACK, None)

    async def check_token(self, token: str) -> 'User':
        """Check if a token is valid and can be used for proper authentication.
        
        Returns
        -------
        :class:`User`
            If the token is valid
        None
            If the token is not valid or it references a non-existing user.
        """
        token_user_id = await self.server.token_find(token)
        if token_user_id is None:
            log.warning("Token not found")
            return

        user = self.server.get_user(token_user_id)
        if user is None:
            log.warning('User not found')
            return

        return user

    def check_shard(self, shard: list):
        """Checks the validity of the shard payload.
        
        Raises
        ------
        StopConnection
            With error code 4001 and a reason for the error.
        """

        ccs = CloseCodes.INVALID_SHARD

        try:
            shard = list(map(int, shard))
        except ValueError:
            raise StopConnection(ccs, 'Invalid shard payload(int).')

        if len(shard) != 2:
            raise StopConnection(ccs, 'Invalid shard payload(length).')

        shard_id, shard_count = shard
        if shard_count < 1:
            raise StopConnection(ccs, 'Invalid shard payload(shard_count=0).')

        if shard_id > shard_count:
            raise StopConnection(ccs, 'Invalid shard payload(id > count).')

    async def make_guild_list(self) -> list:
        """Generate the guild list to be sent over the READY event.
        
        If the guild is large enough(from :attr:`ConnectionState.large`)
        this only puts the online members in the guild object.

        If the connection doesn't represent an `Atomic Discord` connection,
        the client gets subscribed to all guilds the user is in.

        (Actual guild subscribing on Atomic is done by :attr:`Connection.guild_sync_handler`)

        Returns
        -----------
        List[dict]
            List of raw guild objects.
        """
        gm = self.guild_man
        guild_list = []

        async for guild in gm.yield_guilds(self.state.user.id):
            if not self.is_atomic:
                guild.mark_watcher(self.state.user.id)

            jguild = guild.as_json

            if guild.member_count > self.state.large:
                jguild['members'] = [m.as_json for m in guild.online_members]

            guild_list.append(jguild)

        return guild_list

    async def chk_shard_amount(self):
        """Check for the amount of guilds each shard is.
        
        This fills :attr:`ConnecionState.guild_ids` with all
        the Guild IDs this user is in.

        Raises
        ------
        StopConnection
            If the shard being checked has more than 2500 guilds.
        """

        gm = self.guild_man
        self.state.guild_ids = []

        #: List of shard IDs
        shard_guild = []

        async for guild in gm.yield_guilds(self.state.user.id):
            self.state.guild_ids.append(guild.id)
            shard_guild.append(gm.get_shard(guild.id, self.state.shard_count))

        count = collections.Counter(shard_guild)
        for shard_id, guild_count in count.most_common():

            # We don't really check for the other shards amount, just
            # the one we are currently in
            # since checking for the others *could* cause them to crash as well.
            if shard_id != self.state.shard_id:
                continue

            if guild_count > 2500:
                raise StopConnection(CloseCodes.INVALID_SHARD, f'shard {shard_id} is with {guild_count} shards, too many')

    async def dispatch_ready(self, ready_packet: dict, guild_list: list):
        """Dispatch the `READY` event to a client.
        
        If the connection is from a bot, and not a selfbot(user),
        this makes guild streaming, which is overwriting the guild data in `READY`
        for unavailable guilds and dispatching ``GUILD_CREATE`` events
        for every guild the bot is in.
        """

        if self.state.user.bot:
            f = lambda raw_guild: {'id': raw_guild['id'], 'unavailable': True}
            ready_packet['guilds'] = list(map(f, guild_list))

            await self.dispatch('READY', ready_packet)

            for raw_guild in guild_list:
                await self.dispatch('GUILD_CREATE', raw_guild)
            return

        await self.dispatch('READY', ready_packet)

    def ready_payload(self, guild_list: 'List[dict]') -> dict:
        """Get the base READY payload for a client.

        If the client is a bot user, this payload gets extended
        by :meth:`Connection.user_ready_payload`.
        """
        return {
            '_trace': self.get_identifiers('ready'),
            'v': self.options[0],

            'user': self.state.user.as_json,
            'private_channels': [],

            'guilds': guild_list,
            'session_id': self.session_id,
        }

    async def user_ready_payload(self) -> dict:
        """Get a dictionary with keys that are only
        used in user `READY` payloads.
        """
        if self.state.user.bot:
            raise RuntimeError('Bot requesting a user ready')

        f = lambda r: self.presence.get_glpresence(r.u_to.uid)
        friend_presences = list(map(f, self.relationships))

        # TODO: async iterator on User.relationships

        #friend_presences = []
        #async for rel_entry in self.user.relationships:
        #    presence = self.presence.get_glpresence(rel_entry.u_to.id)
        #    friend_presences.append(presence)

        return {
            # the following fields are for user accounts
            # and user accounts only.
            'relationships': self.relationships,
            'user_settings': self.user_settings,
            'user_guild_settings': self.guild_settings,

            # I don't think we are going to have
            # Youtube/Twitch/whatever connections
            'connected_accounts': [],

            # Only you can access those
            # They are handled under another endpoint
            'notes': {},
            'friend_suggestion_count': 0,

            # Assuming this is used for relationships
            # so you get presences for your friends on READY
            # (notice Discord opens your friend list on startup)
            'presences': friend_presences,

            # This might relate with /channels/:id/ack, somehow.
            # I don't know
            'read_state': [],

            # ??????
            'analytics_token': 'insert a token here',
            'experiments': [],
            'guild_experiments': [],
            'required_action': 'do something',
        }

    @handler(OP.IDENTIFY)
    @ws_ratelimit('identify')
    async def identify_handler(self, data):
        """Handle an OP 2 Identify sent by the client.

        Checks if the token given in the packet is valid, and if it is,
        dispatches a READY event.

        Information on the input payload is at:
        https://discordapp.com/developers/docs/topics/gateway#gateway-identify

        Raises
        ------
        StopConnection
            On any invalidation of the current state of the connection
            or the payload.
        """
        if self.identified:
            raise StopConnection(CloseCodes.ALREADY_AUTH)

        try:
            data = self.identify_schema(data)
        except Exception as err:
            log.warning(f'Invalid IDENTIFY: {err!r}')
            raise StopConnection(CloseCodes.DECODE_ERROR)

        token, prop = data['token'], data['properties']
        large = data.get('large_threshold', 50)
        compress_flag = data.get('compress', False)

        user = await self.check_token(token)
        if user is None:
            raise StopConnection(CloseCodes.AUTH_FAILED, 'Authentication failed...')

        shard = data.get('shard', [0, 1])
        self.check_shard(shard)

        shard_id, shard_count = shard
        sharded = shard_count > 1

        if sharded and (not user.bot):
            raise StopConnection(CloseCodes.INVALID_SHARD, 'User accounts cannot shard.')

        session_id = self.server.gen_ssid()
        if session_id is None:
            # NOTE: If we get into a reconnection loop
            # this might be the culprit! check your session ID generation.

            # possible order of events for the loop:
            #  > client identifies
            #  > gateway closes with 4009 
            #  > client reconnects

            #await self.invalidate(False)
            raise StopConnection(CloseCodes.SESSION_TIMEOUT)

        self.session_id = session_id

        guild_count = await self.guild_man.guild_count(user)
        if guild_count > 2500 and user.bot and (not sharded):
            raise StopConnection(CloseCodes.SHARDING_REQUIRED, 'Sharding required')

        self.request_counter = self.server.request_counter[session_id]

        prop = {}
        prop['os'] = prop.get('$os')
        prop['browser'] = prop.get('$browser')
        prop['large'] = large

        # NOTE: Always set user presence before calculating the guild list!
        # If we set presence after sending READY, PresenceManager
        # falls apart because it tries to get presence data(for READY)
        # for a user that is still connecting (the client right now)

        self.state = ConnectionState(session_id, token, \
            user=user, properties=prop, \
            shard_id=shard_id, shard_count=shard_count, \
            large=large, compress=compress_flag)

        self.state.conn = self

        await self.chk_shard_amount()
        log.debug('guild ids: %r', self.state.guild_ids)

        # TODO: maybe store presences between client logon/logoff
        # like idle and dnd?
        await self.presence.global_update(self)

        self.server.add_connection(user.id, self)

        # At this point, the client can receive events
        # and subscribe to guilds through OP 12 Guild Sync
        self.identified = True

        guild_list = await self.make_guild_list()

        log.info('[ready:new_session] %r, guilds=%d', self.state, len(guild_list)) 

        self.user_settings = await self.settings.get_settings(user)
        self.relationships = await self.relations.get_relationships(user)
        self.guild_settings = await self.settings.get_guild_settings(user)

        ready_packet = self.ready_payload(guild_list)

        if not user.bot:
            user_payload = await self.user_ready_payload()
            ready_packet = {**ready_packet, **user_payload}

        await self.dispatch_ready(ready_packet, guild_list)

    @handler(OP.REQUEST_GUILD_MEMBERS)
    async def req_guild_handler(self, data):
        """Handle OP 8 Request Guild Members.

        Dispatches GUILD_MEMBERS_CHUNK (https://discordapp.com/developers/docs/topics/gateway#guild-members-chunk).
        """
        if not self.identified:
            raise StopConnection(CloseCodes.NOT_AUTH)

        try:
            data = self.req_guild_payload(data)
        except:
            raise StopConnection(CloseCodes.DECODE_ERROR, 'Invalid payload')

        guild_id, query, limit = data['guild_id'], data['query'], data['limit']

        if limit > 1000: limit = 1000
        if limit <= 0: limit = 1000

        guild = self.guild_man.get_guild(guild_id)
        if guild is None:
            return

        if self.user.id not in guild.member_ids:
            return

        member_list = []

        if len(query) < 1:
            for member in guild.members.values():
                uname = member.user.username.lower()

                if uname.startswith(qyery):
                    member_list.append(member)
        else:
            member_list = [m for m in guild.members.values()]

        # convert it to as_json madness
        member_list = list(map(lambda o: o.as_json, member_list))

        if len(member_list) > 1000:
            # we split the list into chunks of size 1000
            # and send them all in the event
            for chunk in chunk_list(member_list, 1000):
                await self.dispatch('GUILD_MEMBERS_CHUNK', {
                    'guild_id': guild_id,
                    'members': chunk,
                })
        else:
            await self.dispatch('GUILD_MEMBERS_CHUNK', {
                'guild_id': guild_id,
                'members': chunk[:limit],
            })

    async def invalidate(self, flag=False, session_id=None):
        """Invalidates a session.

        Parameters
        ----------
        flag: bool
            Flags the session as resumable/not resumable.
        session_id: str, optional
            Session ID.
        """
        log.info(f"Invalidated, can resume: {flag}")
        await self.send_op(OP.INVALID_SESSION, flag)

        # flag is False if the session cannot be resumed
        # anymore, requiring a new session to be created.
        if not flag:
            try:
                self.server.remove_state(self.state)
            except Exception:
                log.warning('Failed to invalidate session', exc_info=True)

        raise InvalidateSession(flag)

    async def _resume(self, seqs_to_replay):
        """Send all missing events to the connection.
        Blocks any other events from being dispatched.
        
        Used for resuming.
        """

        await self.dispatch_lock
        try:
            presences = []

            for seq in seqs_to_replay:
                try:
                    evt = self.state[seq]
                except KeyError:
                    continue

                t = evt.get('t')
                if t == 'PRESENCE_UPDATE':
                    presences.append(evt.get('d'))
                else:
                    await self.send(evt)
        except Exception:
            log.error('Error while resuming', exc_info=True)
            await self.invalidate(True)
        finally:
            self.dispatch_lock.release()

        # Intuition here... Litecord wasn't supposed to be
        # as fault-tolerant as Discord is, Litecord sure
        # doesn't crash on any error, but if the server
        # crash, eventually everything crashes(single point of failure).
        
        # I don't think PRESENCES_REPLACE is even supposed to be
        # used in this non-fault-tolerant scenario... but I added it anyways
        # so whatever.
        if presences:
            await self.dispatch('PRESENCES_REPLACE', presences)

    @handler(OP.RESUME)
    async def resume_handler(self, data):
        """Handler for OP 6 Resume.

        This replays events to the connection.
        """

        log.info('[resume] Resuming a connection')

        try:
            data = self.resume_schema(data)
        except Exception:
            log.warning('Invalid payload, invalidating session', exc_info=True)
            await self.invalidate(False)

        token = data['token']
        session_id = data['session_id']
        replay_seq = data['seq']

        state = self.server.get_state(session_id)
        if state is None:
            log.warning('[resume] State not found')
            await self.invalidate(False)

        if token != state.token:
            log.warning('[resume] Invalid token')
            await self.invalidate(False)

        self.state = state
        self.state.conn = self

        sent_seq = state.sent_seq
        if replay_seq > sent_seq:
            log.warning(f'[resume] invalidated from replay_seq > sent_seq {replay_seq} {sent_seq}')
            await self.invalidate(False)

        seqs_to_replay = range(replay_seq, sent_seq + 1)
        total_seqs = len(seqs_to_replay)

        if total_seqs > RESUME_MAX_EVENTS:
            log.warning('[resume] invalidated from seq delta')
            await self.invalidate(False, session_id=session_id)

        log.info(f'Replaying {total_seqs} events to {self.state.user!r}')

        # NOTE: DON'T CALL self.dispatch in this function. DON'T. EVER.
        # it will actually hang the dispatch call indefinetly
        # because the dispatch_lock is well... locked
        # and self.dispatch waits for the lock to be released.
        await self._resume(seqs_to_replay)

        self.state.guild_ids = []
        async for guild in self.guild_man.yield_guilds(state.user.id):
            self.state.guild_ids.append(guild.id)

        self.request_counter = self.server.request_counter[self.session_id]
        self.server.add_connection(state.user.id, self)

        self.identified = True

        await self.presence.global_update(self)
        await self.dispatch('RESUMED', {
            '_trace': self.get_identifiers('resume')
        })

    @handler(OP.STATUS_UPDATE)
    @ws_ratelimit('presence_updates')
    async def status_handler(self, data):
        """Handle OP 3 Status Update."""

        if not self.identified:
            raise StopConnection(CloseCodes.NOT_AUTH, 'Not Identified')

        try:
            status = data['status']
            afk = data['afk']
        except KeyError:
            return

        idle_since = data.get('since')

        game = data.get('game')
        game_name = None
        if game is not None:
            game_name = game.get('name')

        if afk or idle_since:
            status = 'afk'

        await self.presence.global_update(self, {
            'name': game_name,
            'status': status,
        })

    @handler(OP.GUILD_SYNC)
    async def guild_sync_handler(self, data):
        """Handle OP 12 Guild Sync.

        This is an undocumented OP on Discord's API docs.
        This OP is sent by the client to request member and presence information.
        """

        if not self.identified:
            raise StopConnection(CloseCodes.NOT_AUTH, 'Not identified')

        if not isinstance(data, list):
            log.warning('[gateway:guild_sync] Invalid data type')
            return

        # ASSUMPTION: data is a list of guild IDs
        for guild_id in data:
            guild = self.server.guild_man.get_guild(guild_id)
            if guild is None:
                continue

            if self.state.user.id not in guild.members:
                continue

            if self.is_atomic:
                guild.mark_watcher(self.state.user.id)

            await self.dispatch('GUILD_SYNC', {
                'id': str(guild_id),
                'presences': [p.as_json for p in guild.presences],
                'members': [m.as_json for m in guild.online_members],
            })

    @handler(OP.VOICE_STATE_UPDATE)
    async def v_state_update_handler(self, data):
        """Handle OP 4 Voice State Update.

        Requests VoiceServer to generate a VoiceState for the connection.
        Dispatches VOICE_STATE_UPDATE and VOICE_SERVER_UPDATE events to the connection.
        """

        guild_id = data.get('guild_id')
        channel_id = data.get('channel_id')
        self_mute = data.get('self_mute', False)
        self_deaf = data.get('self_deaf', False)

        if guild_id is None or channel_id is None:
            return log.warning('[vsu] missing params')

        guild = self.server.guild_man.get_guild(guild_id)
        if guild is None:
            return log.warning('[vsu] unknown guild')

        channel = guild.channels.get(channel_id)
        if channel is None:
            return log.warning('[vsu] unknown channel')

        if channel.type != ChannelTypes.GUILD_VOICE:
            return log.warning('[vsu] not voice channel')

        v_state = await guild.voice_manager.link(self, channel)
        log.info('%r -voice> %r, %r', self.state.user, channel, v_state)

        await self.dispatch('VOICE_STATE_UPDATE', v_state.as_json)
        await self.dispatch('VOICE_SERVER_UPDATE', guild.region.server.as_json)

    @handler(OP.VOICE_SERVER_PING)
    async def v_ping_handler(self, data):
        """Handle OP 5 Voice Server Ping."""
        log.info(f'VOICE_SERVER_PING with {data!r}')

    @ws_ratelimit('all')
    async def process(self, payload):
        """Process a payload sent by the client.

        Parameters
        ----------
        payload: dict
            https://discordapp.com/developers/docs/topics/gateway#gateway-op-codespayloads
        """
        return await self._process(payload) 

    async def run(self):
        """Starts basic handshake with the client

        The server sends an OP 10 Hello packet to the client and then
        waits in an infinite loop for payloads sent by the client.
        """
        await self.send(self.basic_hello())
        self.wait_task = self.loop.create_task(self.hb_wait_task())
        await self._run()

    async def cleanup(self):
        """Remove the connection from being found by :class:`LitecordServer` functions.

        The cleanup only happens if the connection is open and identified.
        This method only works in the 1st time it is called.
        """

        self.identified = False
        try:
            self.hb_wait_task.cancel()
        except AttributeError:
            pass

        if self.ws.open:
            log.warning("Cleaning up a connection while it is open")

        if self.state is None:
            return

        try:
            self.server.remove_connection(self.session_id)
            log.debug(f'Success cleaning up sid={self.session_id!r}')
        except Exception:
            log.warning('Error while detaching the connection.', exc_info=True)

        # client is only offline if there's no connections attached to it
        amount_conns = self.server.count_connections(self.state.user.id)
        log.info(f"{self.state.user!r} now with {amount_conns} connections")
        if amount_conns < 1:
            await self.presence.global_update(self, self.presence.offline())

        self.state.token = None
