import logging
from .objects import Guild

log = logging.getLogger(__name__)

class GuildManager:
    """Manage guild data."""
    def __init__(self, server):
        self.server = server
        self.guild_db = server.db['guilds']
        self.guilds = {}
        self.channels = {}

    def get_guild(self, guild_id):
        """Get a `Guild` object by its ID."""
        return self.guilds.get(guild_id)

    def get_channel(self, channel_id):
        """Get a `Channel` object by its ID."""
        return self.channels.get(channel_id)

    def get_guilds(self, user_id):
        """Get a list of all `Guild`s a user is on"""
        return [self.guilds[guild_id] for guild_id in self.guilds \
            if user_id in self.guilds[guild_id].member_ids]

    def all_guilds(self):
        """Yield all available guilds"""
        for guild_id in self.guilds:
            yield self.guilds[guild_id]

    def init(self):
        for guild_id in self.guild_db:
            guild_data = self.guild_db[guild_id]

            guild = Guild(self.server, guild_data)
            self.guilds[guild_id] = guild

            for channel_id in guild.channels:
                self.channels[channel_id] = guild.channels[channel_id]

        return True
