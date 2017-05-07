GATEWAY_VERSION = 69

OP = {
    'DISPATCH': 0,
    'HEARTBEAT': 1,
    'IDENTIFY': 2,
    'STATUS_UPDATE': 3,

    # Those OPs are used for VOICE
    # Therefore they are only here as a matter of documentation
    # They won't be used at the current state of LiteCord.
    'VOICE_STATE_UPDATE': 4,
    'VOICE_SERVER_PING': 5,

    'RESUME': 6,
    'RECONNECT': 7,
    'REQUEST_GUILD_MEMBERS': 8,
    'INVALID_SESSION': 9,
    'HELLO': 10,
    'HEARTBEAT_ACK': 11,

    # Undocumented OP code
    'GUILD_SYNC': 12,

    # TODO: meme op code because we can do that here :^)
    #'MEME': 69,
}
