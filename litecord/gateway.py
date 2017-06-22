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

from .basics import OP, GATEWAY_VERSION, CHANNEL_TO_INTEGER
from .server import LitecordServer
from .utils import chunk_list, strip_user_data
from .err import VoiceError, PayloadLengthExceeded
from .ratelimits import ws_ratelimit

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


async def decode_dict(data):
    if isinstance(data, bytes):
        return str(data, 'utf-8')
    elif isinstance(data, dict):
        _copy = dict(data)
        for key in _copy:
            data[await decode_dict(key)] = await decode_dict(data[key])
        return data
    else:
        return data


async def json_encoder(obj):
    return json.dumps(obj)

async def json_decoder(raw_data):
    return json.loads(raw_data)

async def etf_encoder(obj):
    return earl.pack(obj)

async def etf_decoder(raw_data):
    data = earl.unpack(raw_data)

    # Earl makes all keys and values bytes object.
    # We convert them into UTF-8
    if isinstance(data, dict):
        data = await decode_dict(data)

    return data

ENCODING_FUNCS = {
    'json': (json_encoder, json_decoder),
    'etf': (etf_encoder, etf_decoder),
}


class Connection:
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
    def __init__(self, server, ws, options):
        self.ws = ws
        self.options = options

        # Encoder and decoder for JSON/ETF, JSON by default
        self.encoder = json_encoder
        self.decoder = json_decoder

        # Last sequence sent by the client, last sequence received by it, and a registry of dispatched events are here
        self.events = None

        # Client's heartbeat interval, chose at random between 40 and 42sec
        self.hb_interval = random.randint(HB_MIN_MSEC, HB_MAX_MSEC)
        self.wait_task = None

        # Things that properly identify the client
        self.token = None
        self.session_id = None
        self.compress_flag = False
        self.properties = {}

        # ratelimiting tasks that clean the request counter
        self.ratelimit_tasks = {}
        self.request_counter = {} 

        # some flags for the client etc
        self.identified = False
        # TODO: self.replay_lock = asyncio.Lock()

        # user objects, filled oncce the client is identified
        self.user = None
        self.raw_user = None

        # reference to LitecordServer, GuildManager and PresenceManager
        self.server = server
        self.guild_man = server.guild_man
        self.presence = server.presence

        # OP handlers
        self.op_handlers = {
            OP.HEARTBEAT: self.heartbeat_handler,
            OP.IDENTIFY: self.identify_handler,
            OP.STATUS_UPDATE: self.status_handler,

            OP.VOICE_STATE_UPDATE: self.v_state_update_handler,
            OP.VOICE_SERVER_PING: self.v_ping_handler,

            OP.RESUME: self.resume_handler,
            OP.REQUEST_GUILD_MEMBERS: self.req_guild_handler,

            # Undocumented.
            OP.GUILD_SYNC: self.guild_sync_handler,
        }

    def __repr__(self):
        return f'Connection(sid={self.session_id} u={self.user!r})'

    def get_identifiers(self, module):
        return SERVERS.get(module, ['litecord-general-1'])

    def basic_hello(self):
        """Returns a JSON serializable OP 10 Hello packet."""
        return {
            'op': OP.HELLO,
            'd': {
                'heartbeat_interval': self.hb_interval,
                '_trace': self.get_identifiers('hello'),
            }
        }

    def gen_sessid(self):
        """Generate a new Session ID."""
        tries = 0

        new_id = random_sid()
        while new_id in self.server.sessions:
            if tries >= MAX_TRIES:
                return None

            new_id = random_sid()
            tries += 1

        return new_id

    async def send_payload(self, payload, compress=False):
        """Send a payload through the websocket. Will be encoded in JSON or ETF before sending(default JSON).

        Returns
        -------
        int
            The amount of bytes transmitted.
        """
        data = await self.encoder(payload)

        if compress and self.properties['browser'] != 'discord.js':
            if isinstance(data, str):
                data = data.encode()
            data = zlib.compress(data)

        await self.ws.send(data)
        return len(data)

    async def recv_payload(self):
        """Receive a payload from the websocket. Will be decoded using JSON or ETF to a Python object.

        Returns
        -------
        any
            The payload received.
        """

        raw_data = await self.ws.recv()
        if len(raw_data) > 4096:
            raise PayloadLengthExceeded()

        return await self.decoder(raw_data)

    async def send_op(self, op, data=None):
        """Send a packet through the websocket.

        Parameters
        ----------
        op: int
            Packet's OP code.
        data: any
            Any JSON serializable object.
        """

        if data is None:
            data = {}

        payload = {
            # op is always an int
            # data can be a dict, int or bool
            'op': op,
            'd': data,
        }
        return (await self.send_payload(payload))

    def _register(self, sent_seq, payload):
        """Register a sent payload."""
        self.events['events'][sent_seq] = payload
        self.events['sent_seq'] = sent_seq

    async def dispatch(self, evt_name, evt_data=None):
        """Send a DISPATCH packet through the websocket.

        Saves the packet in the `LitecordServer`'s event cache(:meth:`LitecordServer.events`).

        Parameters
        ----------
        evt_name: str
            Follows the same pattern as Discord's event names.
        evt_data: any
            Any JSON serializable object.
            If this has an `as_json` attribute, it gets called.
        """

        if evt_data is None:
            evt_data = {}

        if hasattr(evt_data, 'as_json'):
            evt_data = evt_data.as_json

        try:
            sent_seq = self.events['sent_seq']
        except:
            log.warning("[dispatch] can't dispatch event to unidentified connection")
            return

        sent_seq += 1

        payload = {
            'op': OP.DISPATCH,

            # always an int
            's': sent_seq,

            # always a str
            't': evt_name,
            'd': evt_data,
        }

        amount = None

        # dude fuck discord.js (2)
        # This compress_flag is required to be used only on READY
        # because d.js is weird with its compression and ETF at the same time.
        if evt_name == 'READY':
            amount = await self.send_payload(payload, self.compress_flag)
        else:
            amount = await self.send_payload(payload)

        log.info(f'[dispatch] {evt_name}: {amount} bytes, compress={self.compress_flag}')
        self._register(sent_seq, payload)
        return amount

    @property
    def is_atomic(self):
        """Returns boolean."""
        return self.server.atomic_markers.get(self.session_id, False)

    async def hb_wait_task(self):
        """This task automatically closes clients that didn't heartbeat in time."""
        try:
            await asyncio.sleep((self.hb_interval) / 1000)
            await asyncio.sleep(3)
            log.info("Closing client for lack of heartbeats")
            await self.ws.close(4000)
        except asyncio.CancelledError:
            log.debug("[hb_wait_task] Cancelled")

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
        except:
            pass

        try:
            self.events['recv_seq'] = data
        except:
            log.warning("Received OP 1 Heartbeat from unidentified connection")

        await self.send_op(OP.HEARTBEAT_ACK, {})
        self.wait_task = self.server.loop.create_task(self.hb_wait_task())
        return True

    async def check_token(self, token):
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

        token = data.get('token')
        prop = data.get('properties')
        large = data.get('large_threshold')
        self.compress_flag = data.get('compress', False)

        # check if the client isn't trying to fuck us over
        if (token is None) or (prop is None) or (large is None):
            log.warning('Erroneous IDENTIFY')
            await self.ws.close(4001, 'Erroneous IDENTIFY')
            return

        valid, user_object, user = await self.check_token(token)
        if not valid:
            await self.ws.close(4004, 'Authentication failed...')
            return False

        self.raw_user = user_object
        self.user = user

        self.session_id = self.gen_sessid()
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

        all_guild_list = self.guild_man.get_guilds(self.user.id)

        # the actual list of guilds to be sent to the client
        guild_list = []

        for guild in all_guild_list:
            if not self.is_atomic:
                guild.mark_watcher(self.user.id)

            guild_json = guild.as_json

            # Only send online members if the guild is large
            if guild.member_count > large:
                guild_json['members'] = [m.as_json for m in guild.online_members]

            guild_list.append(guild_json)

        stripped_user = strip_user_data(self.raw_user)

        log.info("New session %s, sending %d guilds", self.session_id, len(guild_list)) 

        ready_packet = {
            'v': self.options['v'],
            'user': stripped_user,
            'private_channels': [],

            # discord.js why u use undocumented shit
            'relationships': await self.relations.get_relationships(self.user.id),
            'user_settings': await self.settings.get_settings(self.user.id),

            'guilds': guild_list,
            'session_id': self.session_id,
            '_trace': self.get_identifiers('ready')
        }

        # If its a bot, we send unavailable guilds on READY
        # and then dispatch GUILD_CREATE events for every guild
        if self.raw_user['bot']:
            ready_packet['guilds'] =  [{'id': jguild['id'], 'unavailable': True} for jguild in guild_list],

            await self.dispatch('READY', ready_packet)
            for raw_guild in guild_list:
                await self.dispatch('GUILD_CREATE', raw_guild)
        else:
            await self.dispatch('READY', ready_packet)

        return True

    async def req_guild_handler(self, data):
        """Handle OP 8 Request Guild Members.

        Dispatches GUILD_MEMBERS_CHUNK (https://discordapp.com/developers/docs/topics/gateway#guild-members-chunk).
        """
        if not self.identified:
            log.warning("Client not identified to do OP 8, closing with 4003")
            await self.ws.close(4003)
            return False

        guild_id = data.get('guild_id')
        query = data.get('query')
        limit = data.get('limit')

        if guild_id is None or query is None or limit is None:
            log.info("Invalid OP 8")
            await self.ws.close(4001)
            return False

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
        return True

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
                await self.ws.close(4001)
            except:
                pass

    async def resume_handler(self, data):
        """Handler for OP 6 Resume.

        This replays events to the connection.
        """

        log.info('[resume] Resuming a connection')

        token = data.get('token')
        session_id = data.get('session_id')
        replay_seq = data.get('seq')

        if replay_seq is None or session_id is None or token is None:
            await self.ws.close(4001)
            return False

        if session_id not in self.server.event_cache:
            log.warning("[resume] invalidated from session_id")
            await self.invalidate(True)
            return True

        event_data = self.server.event_cache[session_id]

        valid, raw_user, user = await self.check_token(token)
        if not valid:
            log.warning("[resume] invalidated @ check_token")
            await self.invalidate(session_id=session_id)
            return False

        # man how can i resume from the future
        sent_seq = event_data['sent_seq']

        if replay_seq > sent_seq:
            log.warning(f"[resume] invalidated from replay_seq > sent_set {replay_seq} {sent_seq}")
            await self.invalidate(True)
            return True

        # if the session lost more than RESUME_MAX_EVENTS
        # events while it was offline, invalidate it.
        if abs(replay_seq - sent_seq) > RESUME_MAX_EVENTS:
            log.warning("[resume] invalidated from seq delta")
            await self.invalidate(False, session_id=session_id)
            return

        seqs_to_replay = range(replay_seq, sent_seq + 1)
        log.info(f"Replaying {len(seqs_to_replay)} events to {user!r}")

        for seq in seqs_to_replay:
            try:
                await self.send_payload(event_data['events'][seq])
            except KeyError:
                log.info(f"Event {seq} not found")

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

        return True

    @ws_ratelimit('presence_updates')
    async def status_handler(self, data):
        """Handle OP 3 Status Update packets

        Checks the payload format and if it is OK, calls `PresenceManager.global_update`
        """

        if not self.identified:
            log.error('Not identified to do operation, closing: 4003')
            await self.ws.close(4003, 'Not identified')
            return False

        idle_since = data.get('idle_since')

        game = data.get('game')
        if game is None:
            return True

        game_name = game.get('name')
        if game_name is None:
            return True

        await self.presence.global_update(self.user, {
            'name': game_name,
            'status': 'idle' if idle_since is not None else None
        })
        return True

    async def guild_sync_handler(self, data):
        """Handle OP 12 Guild Sync packets

        This is an undocumented OP on Discord's API docs.
        This OP is sent by the client to request member and presence information.
        """

        if not self.identified:
            log.error("Client not identified to do OP 12, closing with 4003")
            await self.ws.close(4003)
            return False

        if not isinstance(data, list):
            log.error('[guild_sync] client didn\'t send a list')
            await self.ws.close(4001)
            return False

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
            return True

        guild = self.server.guild_man.get_guild(guild_id)
        if guild is None:
            log.warning("[vsu] unknown guild")
            return True

        channel = guild.channels.get(channel_id)
        if channel is None:
            log.warning("[vsu] unknown channel")
            return True

        if channel.str_type != 'voice':
            log.warning("[vsu] not voice channel")
            return True

        # We request a VoiceState from the voice manager
        try:
            v_state = await channel.voice_request(self)
        except VoiceError:
            log.error('error while requesting VoiceState', exc_info=True)
            return True

        log.info(f"{self.user!r} => voice => {channel!r} => {channel_vstate!r}")

        await self.dispatch('VOICE_STATE_UPDATE', v_state.as_json)
        await self.dispatch('VOICE_SERVER_UPDATE', v_state.server_as_json)

        return True

    async def v_ping_handler(self, data):
        """Handle OP 5 Voice Server Ping."""
        log.info("Received OP5 VOICE_SERVER_PING what do i do")
        return True

    @ws_ratelimit('all')
    async def process_recv(self, payload):
        """Process a payload sent by the client.

        Parameters
        ----------
        payload: dict
            https://discordapp.com/developers/docs/topics/gateway#gateway-op-codespayloads
        """

        op = payload.get('op')
        data = payload.get('d')
        if op not in self.op_handlers:
            log.info("opcode not found, closing with 4001")
            await self.ws.close(4001)
            return False

        handler = self.op_handlers[op]
        return (await handler(data))

    async def run(self):
        """Starts basic handshake with the client

        This only starts when the websocket server notices a new client.
        The server sends an OP 10 Hello packet to the client, and after that
        it relays payloads sent by the client to `Connection.process_recv`
        """
        log.info(f'[conn.run] v={self.options["v"]} encoding={self.options["encoding"]}')
        await self.send_payload(self.basic_hello())

        try:
            while True:
                try:
                    payload = await self.recv_payload()
                except (PayloadLengthExceeded, earl.DecodeError):
                    await self.ws.close(4002)
                    await self.cleanup()
                    break

                # if process_recv tells us to stop, we clean everything
                # process_recv will very probably close the websocket already
                if not (await self.process_recv(payload)):
                    log.info("Stopped processing")
                    await self.cleanup()
                    break
        except asyncio.CancelledError:
            # I try.
            log.info(f"[ws] Cancelled, cleaning {self!r}")
            await self.ws.close(1006)
            await self.cleanup()
            return
        except websockets.ConnectionClosed as err:
            log.info(f"[ws] closed, code {err.code!r}")
            await self.cleanup()
        except Exception as err:
            # if any error we just close with 4000
            log.error('Error while running the connection', exc_info=True)
            await self.ws.close(4000, f'Unknown error: {err!r}')
            await self.cleanup()
            return

        await self.ws.close(1000)

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

    # server initialized, release HTTP to load pls
    _load_lock.release()

    async def henlo(websocket, path):
        log.info(f'[ws] opening')

        parsed = urlparse.urlparse(path)
        params = urlparse.parse_qs(parsed.query)

        gateway_version = params.get('v', ['6'])[0]
        encoding = params.get('encoding', ['json'])[0]

        try:
            gateway_version = int(gateway_version)
        except:
            gateway_version = 6

        if encoding not in ['json', 'etf']:
            await websocket.close(4000, f'{encoding} not supported.')
            return

        if gateway_version != 6:
            await websocket.close(4000, f'gateway v{gateway_version} not supported')
            return

        conn = Connection(server, websocket, {
            'v': gateway_version,
            'encoding': encoding,
        })

        conn.encoder, conn.decoder = ENCODING_FUNCS[encoding]

        await conn.run()
        await conn.cleanup()

    # start WS
    ws = flags['server']['ws']
    log.info(f'[ws] running at {ws[0]}:{ws[1]}')

    ws_server = websockets.serve(henlo, host=ws[0], port=ws[1])
    await ws_server

    # we don't really care about the sentry task lul
    loop.create_task(server_sentry(server))
    return True
