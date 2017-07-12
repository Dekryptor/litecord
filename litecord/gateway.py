"""
gateway.py - Manages a websocket connection

    This file is considered one of the most important, since it loads
    LitecordServer and tells it to initialize the databases.
"""
import json
import logging
import asyncio
import uuid
import random
import zlib
import hashlib
import collections
import urllib.parse as urlparse

import websockets

from voluptuous import Schema, Optional, REMOVE_EXTRA

from .basics import OP, GATEWAY_VERSION, CHANNEL_TO_INTEGER
from .server import LitecordServer
from .utils import chunk_list, strip_user_data
from .err import VoiceError, PayloadLengthExceeded
from .ratelimits import ws_ratelimit

from .ws import WebsocketConnection, handler, StopConnection, get_data_handlers

# Maximum amount of tries to generate a session ID.
MAX_TRIES = 20

# Heartbeating intervals, actual heartbeating interval is random value
# between HB_MIN_MSEC and HB_MAX_MSEC
HB_MIN_MSEC = 40000
HB_MAX_MSEC = 42000

# The maximum amount of events you can lose before your session gets invalidated.
RESUME_MAX_EVENTS = 60

log = logging.getLogger(__name__)

try:
    import earl
except ImportError:
    log.warning(f"Earl-ETF not found, ETF support won't work")

SERVERS = {
    'hello': [f'litecord-hello-{random.randint(1, 99)}'],
    'ready': [f'litecord-session-{random.randint(1, 99)}'],
    'resume': [f'litecord-resumer{random.randint(1, 99)}'],
}


def random_sid():
    """Generate a new random Session ID."""
    return hashlib.md5(str(uuid.uuid4().fields[-1]).encode()).hexdigest()


class Connection(WebsocketConnection):
    """Represents a websocket connection to Litecord.

    .. _the documentation about it here: https://discordapp.com/developers/docs/topics/gateway
    .. _WebSocketServerProtocol: https://websockets.readthedocs.io/en/stable/api.html#websockets.server.WebSocketServerProtocol

    This connection only handles A mix of Discord's gateway version 5 and 6,
    it adheres with the docs which are v5, but handles some stuff from v6(see :py:meth:`Connection.guild_sync_handler`)
    you can find `the documentation about it here`_.

    Attributes
    ----------
    ws: `WebSocketServerProtocol`_
        The actual websocket connection.
    options: dict
        Websocket options, encoding, gateway version.
    encoder: function
        Encoder function that convers objects to the provided encoding over :attr:`Connection.options`
    decoder: function
        Decoder function that converts messages from the websocket to objects.
    events: dict
        If the connection is identified, this becomes a reference to
        `LitecordServer.event_cache[connection.user.id]`.
    hb_interval: int
        Amount, in milliseconds, of the client's heartbeat period.
    wait_task: `asyncio.Task`
        :meth:`Connection.hb_wait_task`.
    token: str or None
        The token this connection is using.
    session_id: str or None
        The session ID this connection is using.
    identified: bool
        Connection had a successful `IDENTIFY` or not.
    properties: dict
        Connection properties like OS, browser and the large_threshold.
    ratelimit_tasks: dict
        Tasks that clean the specified ratelimit bucket in a period of time.
    request_counter: dict
        A request counter for ratelimit buckets.
    user: :class:`User`
        Becomes a user object if the connection is properly identified.
    raw_user: dict
        Same as :attr:`user`, but it is a raw user object.
    op_handlers: dict
        OP handlers, they get called from :meth:`Connection.process_recv`
    """
    def __init__(self, ws, **kwargs):
        super().__init__(ws)
        self.ws = ws
        self.loop = ws.loop
        self.options = kwargs['config']
        self.server = kwargs['server']

        self._encoder, self._decoder = get_data_handlers(self.options[1])

        # Last sequence sent by the client, last sequence received by it, and a registry of dispatched events are here
        self.events = None

        # Client's heartbeat interval, chose at random between 40 and 42sec
        self.hb_interval = random.randint(HB_MIN_MSEC, HB_MAX_MSEC)
        self.wait_task = None

        # Things that properly identify the client
        self.session_id = None
        self.token = None
        self.session_id = None
        self.compress_flag = False
        self.properties = {}

        # ratelimiting tasks that clean the request counter
        self.ratelimit_tasks = {}
        self.request_counter = {} 

        # some flags for the client etc
        self.identified = False
        self.dispatch_lock = asyncio.Lock()

        # user objects, filled oncce the client is identified
        self.user = None
        self.raw_user = None

        # references to objects
        self.guild_man = self.server.guild_man
        self.presence = self.server.presence
        self.relations = self.server.relations
        self.settings = self.server.settings

        # identify schema
        _o = Optional
        self.identify_schema = Schema({
            'token': str,
            'properties': dict,
            _o('compress'): bool,
            'large_threshold': int,
        }, extra=REMOVE_EXTRA)

    def __repr__(self):
        if getattr(self, 'session_id', None) is None:
            return f'Connection()'
        return f'Connection(sid={self.session_id} u={self.user!r})'

    def get_identifiers(self, module):
        return SERVERS.get(module, ['litecord-general-1'])

    def basic_hello(self) -> dict:
        """Returns a JSON serializable OP 10 Hello packet."""
        return {
            'op': OP.HELLO,
            'd': {
                'heartbeat_interval': self.hb_interval,
                '_trace': self.get_identifiers('hello'),
            }
        }

    def gen_sessid(self) -> str:
        """Generate a new Session ID.
        
        Tries to generate available session ids, if it reaches MAX_TRIES, returns `None`.
        """
        tries = 0

        new_id = random_sid()
        while new_id in self.server.sessions:
            if tries >= MAX_TRIES:
                return None

            new_id = random_sid()
            tries += 1

        return new_id
    
    def _register_payload(self, sent_seq, payload):
        """Register a sent payload.
        
        Ignores certain kinds of payloads and events
        """
        self.events['sent_seq'] = sent_seq

        op = payload['op']
        if op not in (OP.DISPATCH, OP.STATUS_UPDATE):
            return

        t = payload.get('t')
        if t in ('READY', 'RESUMED'):
            return

        self.events['events'][sent_seq] = payload

    async def dispatch(self, evt_name, evt_data=None):
        """Send a DISPATCH packet through the websocket.

        Saves the packet in the `LitecordServer`'s event cache(:meth:`LitecordServer.events`).

        Parameters
        ----------
        evt_name: str
            Follows the same pattern as Discord's event names.
        evt_data: any
            Any JSON serializable object.
            If this has an `as_json` property, it gets called.
        """

        await self.dispatch_lock

        if evt_data is None:
            evt_data = {}

        if hasattr(evt_data, 'as_json'):
            evt_data = evt_data.as_json

        try:
            sent_seq = self.events['sent_seq']
        except:
            log.warning("[dispatch] can't dispatch event to unidentified connection")
            self.dispatch_lock.release()
            return 0

        sent_seq += 1

        payload = {
            'op': OP.DISPATCH,
            's': sent_seq,
            't': evt_name,
            'd': evt_data,
        }

        amount = None

        # dude fuck discord.js (2)
        # This compress_flag is required to be used only on READY
        # because d.js is weird with its compression and ETF at the same time.
        if evt_name == 'READY':
            amount = await self.send(payload, compress=self.compress_flag)
        else:
            amount = await self.send(payload)

        log.info(f'[dispatch] {evt_name}, {amount} bytes, compress: {self.compress_flag}')
        self._register_payload(sent_seq, payload)

        self.dispatch_lock.release()
        return amount

    @property
    def is_atomic(self):
        """Returns boolean."""
        return self.server.atomic_markers.get(self.session_id, False)

    async def hb_wait_task(self):
        """This task automatically closes clients that didn't heartbeat in time."""
        try:
            await asyncio.sleep((self.hb_interval / 1000) + 3)
            raise StopConnection(4000, 'Heartbeat expired')
        except asyncio.CancelledError:
            log.debug("[heartbeat_wait] cancelled")

    @handler(OP.HEARTBEAT)
    async def heartbeat_handler(self, data):
        """Handle OP 1 Heartbeat packets.
        Sends a OP 11 Heartbeat ACK packet.

        Parameters
        ----------
        data: int or :py:const:`None`
            Sequence number to be saved in ``Connection.events['recv_seq']``
        """
        try:
            self.wait_task.cancel()
        except AttributeError: pass

        try:
            self.events['recv_seq'] = data
        except AttributeError: pass

        await self.send_op(OP.HEARTBEAT_ACK, {})
        self.wait_task = self.loop.create_task(self.hb_wait_task())

    async def check_token(self, token: str) -> tuple:
        """Check if a token is valid and can be used for proper authentication.
        
        Returns
        -------
        tuple
            with 3 items:
            - A boolean describing the success of the operation,
            - A raw user object(:py:meth:`None` if operation failed),
            - A :class:`User` object(:py:meth:`None` if operation failed).
        """
        token_user_id = await self.server.token_find(token)
        if token_user_id is None:
            log.warning("Token not found")
            return False, None, None

        raw_user = self.server.get_raw_user(token_user_id)
        if raw_user is None:
            log.warning("(token, user) pair not found")
            return False, None, None

        user = self.server.get_user(raw_user['id'])
        return True, raw_user, user

    @handler(OP.IDENTIFY)
    @ws_ratelimit('identify')
    async def identify_handler(self, data):
        """Handle an OP 2 Identify sent by the client.

        Checks if the token given in the packet is valid, and if it is,
        dispatched a READY event.

        Information on the input payload is at:
        https://discordapp.com/developers/docs/topics/gateway#gateway-identify
        """
        if self.identified:
            await self.ws.close(4005, 'Already authenticated')
            return

        try:
            data = self.identify_schema(data)
        except Exception as err:
            log.warning(f'Erroneous IDENTIFY: {err!r}')
            raise StopConnection(4001, f'Erroneous IDENTIFY: {err!r}')

        token, prop = data['token'], data['properties']
        large = data.get('large_threshold', 50)
        self.compress_flag = data.get('compress', False)

        valid, user_object, user = await self.check_token(token)
        if not valid:
            raise StopConnection(4004, 'Authentication failed...')

        self.raw_user = user_object
        self.user = user

        self.session_id = self.gen_sessid()
        if self.session_id is None:
            # Failed to create an unique session
            # This can happen because of anything
            await self.invalidate(False)
            return

        self.request_counter = self.server.request_counter[self.session_id]
        self.token = token

        # lol properties
        _prop = self.properties
        _prop['token'] = self.token
        _prop['os'] = prop.get('$os')
        _prop['browser'] = prop.get('$browser')
        _prop['large'] = large

        self.server.add_connection(self.user.id, self)

        self.events = self.server.event_cache[self.session_id]

        # set user status before even calculating guild data to be sent
        # if we do it *after* READY, the presence manager errors since it tries
        # to get presence stuff for a member that is still connecting
        await self.presence.global_update(self.user)

        self.identified = True

        # the actual list of guilds to be sent to the client
        guild_list = []

        for guild in self.guild_man.yield_guilds(self.user.id):
            if not self.is_atomic:
                guild.mark_watcher(self.user.id)

            guild_json = guild.as_json

            # Only send online members if the guild is large
            if guild.member_count > large:
                guild_json['members'] = [m.as_json for m in guild.online_members]

            guild_list.append(guild_json)

        log.info("READY: New session %s, sending %d guilds", self.session_id, len(guild_list)) 

        stripped_user = strip_user_data(self.raw_user)
        user_settings = await self.settings.get_settings(self.user.id)
        user_relationships = await self.relations.get_relationships(self.user.id)

        user_guild_settings = await self.settings.get_guild_settings(self.user.id)

        ready_packet = {
            'v': self.options[0],
            'user': stripped_user,
            'private_channels': [],

            # those are specific user stuff
            # that aren't documented in Discord's API Docs.
            'relationships': user_relationships,
            'user_settings': user_settings,
            'user_guild_settings': user_guild_settings,

            'connected_accounts': [],
            'notes': [],
            'friend_suggestion_count': 0,
            'presences': [],
            'read_state': [],
            'analytics_token': 'hahahahahahahahaha lol',
            'experiments': [],
            'guild_experiments': [],
            'required_action': 'die',

            'guilds': guild_list,
            'session_id': self.session_id,
            '_trace': self.get_identifiers('ready')
        }

        # If its a bot, we send unavailable guilds on READY
        # and then dispatch GUILD_CREATE events for every guild
        if self.raw_user['bot']:
            ready_packet['guilds'] =  [{'id': jguild['id'], 'unavailable': True} for jguild in guild_list]

            await self.dispatch('READY', ready_packet)
            for raw_guild in guild_list:
                await self.dispatch('GUILD_CREATE', raw_guild)
        else:
            await self.dispatch('READY', ready_packet)

    @handler(OP.REQUEST_GUILD_MEMBERS)
    async def req_guild_handler(self, data):
        """Handle OP 8 Request Guild Members.

        Dispatches GUILD_MEMBERS_CHUNK (https://discordapp.com/developers/docs/topics/gateway#guild-members-chunk).
        """
        if not self.identified:
            raise StopConnection(4003, 'Not identified to do operation.')

        guild_id = data.get('guild_id')
        query = data.get('query')
        limit = data.get('limit')

        if guild_id is None or query is None or limit is None:
            raise StopConnection(4001, 'Invalid payload')

        if limit > 1000: limit = 1000
        if limit <= 0: limit = 1000

        guild = self.guild_man.get_guild(guild_id)
        if guild is None:
            return

        all_members = [member.as_json for member in guild.members]
        member_list = []

        # NOTE: this is inneficient
        if len(query) > 0:
            for member in all_members:
                if member.user.username.startswith(query):
                    member_list.append(member)
        else:
            # if no query provided, just give it all
            member_list = all_members

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
                'members': chunk,
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
        if not flag:
            try:
                self.server.event_cache.pop(self.session_id or session_id)
              
                # TODO: Make this work with ratelimits
                # since discord sends you OP 9 + ws close
                if flag is None:
                    raise StopConnection(4000, 'Invalidated session')
            except:
                pass

    @handler(OP.RESUME)
    async def resume_handler(self, data):
        """Handler for OP 6 Resume.

        This replays events to the connection.
        """

        log.info('[resume] Resuming a connection')

        token = data.get('token')
        session_id = data.get('session_id')
        replay_seq = data.get('seq')

        if replay_seq is None or session_id is None or token is None:
            raise StopConnection(4001, 'Invalid payload')

        if session_id not in self.server.event_cache:
            log.warning('[resume] invalidated from session_id not found')
            await self.invalidate(False)

        event_data = self.server.event_cache[session_id]

        valid, raw_user, user = await self.check_token(token)
        if not valid:
            log.warning('[resume] invalidated @ check_token')
            await self.invalidate(session_id=session_id)

        # man how can i resume from the future
        sent_seq = event_data['sent_seq']

        if replay_seq > sent_seq:
            log.warning(f'[resume] invalidated from replay_seq > sent_seq {replay_seq} {sent_seq}')
            await self.invalidate(True)
            raise StopConnection(4007, 'Invalid sequence')

        # if the session lost more than RESUME_MAX_EVENTS
        # events while it was offline, invalidate it.
        if abs(replay_seq - sent_seq) > RESUME_MAX_EVENTS:
            log.warning('[resume] invalidated from seq delta')
            await self.invalidate(False, session_id=session_id)

        seqs_to_replay = range(replay_seq, sent_seq + 1)
        total_seqs = len(seqs_to_replay)
        log.info(f'Replaying {total_seqs} events to {user!r}')

        # critical session etc
        await self.dispatch_lock
        try:
            presences = []

            for seq in seqs_to_replay:
                try:
                    evt = event_data['events'][seq]
                except KeyError:
                    continue

                t = evt.get('t')
                if t == 'PRESENCE_UPDATE':
                    presences.append(evt.get('d'))
                else:
                    await self.send(evt)

            log.debug('[resume] dispatching PRESENCES_REPLACE')
            await self.dispatch('PRESENCES_REPLACE', presences)
        finally:
            self.dispatch_lock.release()

        self.raw_user = raw_user
        self.user = user

        self.token = token
        self.session_id = session_id
        self.request_counter = self.server.request_counter[self.session_id]
 
        self.events = self.server.event_cache[self.session_id]
        self.server.add_connection(self.user.id, self)

        self.identified = True

        await self.presence.global_update(self.user)

        await self.dispatch('RESUMED', {
            '_trace': self.get_identifiers('resume')
        })

    @handler(OP.STATUS_UPDATE)
    @ws_ratelimit('presence_updates')
    async def status_handler(self, data):
        """Handle OP 3 Status Update packets

        Checks the payload format and if it is OK, calls `PresenceManager.global_update`
        """

        if not self.identified:
            raise StopConnection(4003, 'Not identified')

        idle_since = data.get('idle_since')

        game = data.get('game')
        if game is None:
            return

        game_name = game.get('name')
        if game_name is None:
            return

        await self.presence.global_update(self.user, {
            'name': game_name,
            'status': 'idle' if idle_since is not None else None
        })

    @handler(OP.GUILD_SYNC)
    async def guild_sync_handler(self, data):
        """Handle OP 12 Guild Sync.

        This is an undocumented OP on Discord's API docs.
        This OP is sent by the client to request member and presence information.
        """

        if not self.identified:
            raise StopConnection(4003, 'Not identified')

        if not isinstance(data, list):
            raise StopConnection(4001, 'Invalid data type')

        # ASSUMPTION: data is a list of guild IDs
        for guild_id in data:
            guild = self.server.guild_man.get_guild(guild_id)
            if guild is None:
                continue

            if self.user.id not in guild.members:
                continue

            if self.is_atomic:
                guild.mark_watcher(self.user.id)

            await self.dispatch('GUILD_SYNC', {
                'id': guild_id,
                'presences': [self.presence.get_presence(guild_id, member.id).as_json \
                    for member in guild.online_members],
                'members': [member.as_json for member in guild.online_members],
            })

        return True

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
            log.warning("[vsu] missing params")
            return

        guild = self.server.guild_man.get_guild(guild_id)
        if guild is None:
            log.warning("[vsu] unknown guild")
            return

        channel = guild.channels.get(channel_id)
        if channel is None:
            log.warning("[vsu] unknown channel")
            return

        if channel.str_type != 'voice':
            log.warning("[vsu] not voice channel")
            return

        # We request a VoiceState from the voice manager
        try:
            v_state = await channel.voice_request(self)
        except VoiceError:
            log.error('error while requesting VoiceState', exc_info=True)
            return

        log.info(f"{self.user!r} => voice => {channel!r} => {channel_vstate!r}")

        await self.dispatch('VOICE_STATE_UPDATE', v_state.as_json)
        await self.dispatch('VOICE_SERVER_UPDATE', v_state.server_as_json)

    @handler(OP.VOICE_SERVER_PING)
    async def v_ping_handler(self, data):
        """Handle OP 5 Voice Server Ping."""
        log.info("Received OP5 VOICE_SERVER_PING what do i do")
        return

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
        await self._run()

    async def cleanup(self):
        """Remove the connection from being found by :class:`LitecordServer` functions.

        The cleanup only happens if the connection is open and identified.
        This method only works in the 1st time it is called.
        """

        self.identified = False
        try:
            self.hb_wait_task.cancel()
        except:
            pass

        if self.ws.open:
            log.warning("Cleaning up a connection while it is open")

        if self.token is not None:
            try:
                self.server.remove_connection(self.session_id)
                log.debug(f'Success cleaning up sid={self.session_id!r}')
            except:
                log.warning("Error while detaching the connection.")

            # client is only offline if there's no connections attached to it
            amount_conns = self.server.count_connections(self.user.id)
            log.info(f"{self.user!r} now with {amount_conns} connections")
            if amount_conns < 1:
                await self.presence.global_update(self.user, self.presence.offline())

            self.token = None


_load_lock = asyncio.Lock()

# Modification of
# https://github.com/Rapptz/discord.py/blob/bed2e90e825f9cf90fc1ecbae3f49472de05ad3c/discord/client.py#L520
def _stop(loop):
    pending = asyncio.Task.all_tasks(loop=loop)
    gathered = asyncio.gather(*pending, loop=loop)
    try:
        gathered.cancel()
        loop.run_until_complete(gathered)
        gathered.exception()
    except:
        pass

async def server_sentry(server):
    log.info('Starting sentry')
    try:
        while True:
            check_data = await server.check()

            if not check_data.get('good', False):
                log.warning('[sentry] we are NOT GOOD.')

            log.info(f"[sentry] Mongo ping: {check_data['mongo_ping']}msec")

            #log.info(f"[sentry] HTTP throughput: {check_data['http_throughput']}requests/s")
            #log.info(f"[sentry] WS throughput: {check_data['ws_throughput']}packets/s")

            await asyncio.sleep(10)
    except:
        log.error(exc_info=True)
        pass

async def http_server(app, flags):
    """Main function to start the HTTP server.

    This function waits for `gateway_server` to finish(using asyncio locks).

    That is needed since `gateway_server` initializes server state and registers
    all API routes, and in aiohttp, you need to register
    routes **before** the app starts.
    """
    await _load_lock.acquire()
    http = flags['server']['http']

    handler = app.make_handler()
    f = app.loop.create_server(handler, http[0], http[1])
    await f

    log.info(f'[http] running at {http[0]}:{http[1]}')


async def gateway_server(app, flags, loop=None):
    """Main function to start the websocket server

    This function initializes a LitecordServer object, which
    initializes databases, fills caches, etc.

    When running, for each new websocket client, a `Connection` object is
    created to represent it, its `.run()` method is called and the
    connection will stay alive forever until it gets closed or the client
    stops heartbeating with us.
    """
    await _load_lock.acquire()

    if loop is None:
        loop = asyncio.get_event_loop()

    try:
        server = LitecordServer(flags, loop)
    except Exception as err:
        log.error(f'We had an error loading the litecord server. {err!r}')
        _stop(loop)
        return

    if not (await server.init(app)):
        log.error('We had an error initializing the Litecord Server.')
        _stop(loop)
        return

    app.litecord_server = server

    # server initialized, release HTTP to load pls
    _load_lock.release()

    async def henlo(ws, path):
        """Handles a new connection to the Gateway."""
        if not server.accept_clients:
            await self.ws.close(4069, 'Server is not accepting new clients.')
            return

        log.info(f'[ws] New client at {path!r}')

        params = urlparse.parse_qs(urlparse.urlparse(path).query)

        gw_version = params.get('v', [6])[0]
        encoding = params.get('encoding', ['json'])[0]

        try:
            gw_version = int(gw_version)
        except ValueError:
            gw_version = 6

        if encoding not in ('json', 'etf'):
            await ws.close(4000, f'encoding not supported: {encoding!r}')
            return

        if gw_version != 6:
            await ws.close(4000, f'gw version not supported: {gw_version}')
            return

        conn = Connection(ws, config=(gw_version, encoding), server=server)

        # this starts an infinite loop waiting for payloads from the client
        await conn.run()

    ws = flags['server']['ws']
    log.info(f'[ws] running at {ws[0]}:{ws[1]}')

    ws_server = websockets.serve(henlo, host=ws[0], port=ws[1])
    await ws_server

    loop.create_task(server_sentry(server))
    return True
