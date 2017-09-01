
class SettingsManager:
    """User settings manager.
    
    Provides functions for users to change their settings and retrieve them back.

    Attributes
    ----------
    server: :class:`LitecordServer`
        Litecord server instance.
    settings_coll: `mongo collection`
        User settings MongoDB collection.
    """
    def __init__(self, server):
        self.server = server
        self.guild_man = server.guild_man

        self.settings_coll = self.server.settings_coll

    async def get_settings(self, user):
        """Get a settings object from a User ID.
        
        Parameters
        ----------
        user_id: :class:`User`
            User ID to be get settings from.
        """
        if user.bot:
            return {}

        settings = await self.settings_coll.find_one({'user_id': user.id})
        if settings is None:
            settings = {
                'timezone_offset': 0,
                'theme': 'dark',
                'status': 'online',
                'show_current_game': False,
                'restricted_guilds': [],
                'render_reactions': True,
                'render_embeds:': True,
                'message_display_compact': True,
                'locale': 'en-US',
                'inline_embed_media': False,
                'inline_attachment_media': False,
                'guild_positions': [],
                'friend_source_flags': {
                    'all': True,
                },
                'explicit_content_filter': 1,
                'enable_tts_command': False,
                'developer_mode': False,
                'detect_platform_accounts': False,
                'default_guilds_restricted': False,
                'convert_emoticons': True,
                'afk_timeout': 600,
            }
        return settings

    async def get_guild_settings(self, user):
        """Get a User Guild Settings object to be used
        in READY payloads.
        
        Parameters
        ----------
        user_id: :class:`User`
            User ID to get User Guild Settings payload for.

        Returns
        -------
        list
            The User Guild Settings payload.
        """
        if user.bot:
            return []

        res = []

        async for guild in self.guild_man.yield_guilds(user.id):
            res.append(guild.default_settings)

        return res
