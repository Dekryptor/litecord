import datetime

from .base import LitecordObject
from ..snowflake import snowflake_time


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
    _raw: dict
        Raw message data.
    id: int
        Message's snowflake ID.
    author_id: int
        Message author's snowflake ID.
    channel_id: int
        Message channel's snowflake ID.

    created_at: `datetime.datetime`
        Message's creation time.
    channel: :class:`Channel`
        Channel where this message comes from.
    author: :class:`Member`
        Member that made the message.
    content: str
        Message content.
    edited_at: `datetime.datetime`
        Default is :py:const:`None`.
        If the message was edited, this is set to the time at which this message was edited.
    pinned: bool
        If the message is pinned in the channel.
    """

    __slots__ = ('_raw', 'id', 'author_id', 'channel_id', 'timestamp', 'channel',
        'author', 'member', 'content', 'edited_at')

    def __init__(self, server, channel, author, raw):
        super().__init__(server)
        self._raw = raw

        self.id = int(_raw['message_id'])
        self.author_id = int(_raw['author_id'])

        self._update(channel, author, raw)

    def _update(self, channel, author, raw):
        self.channel = channel
        self.author = author
        self.guild = channel.guild

        self.created_at = self.to_timestamp(self.id)

        self.content = raw['content']
        self.pinned = raw.get('pinned', False)
        self.edited_timestamp = raw.get('edited_timestamp')

        if self.edited_timestamp is not None:
            self.edited_at = datetime.datetime.strptime(self.edited_timestamp, \
                "%Y-%m-%dT%H:%M:%S.%f")

    def edit_content(self, new_content, timestamp=None):
        """Edit a message object"""
        if timestamp is None:
            timestamp = datetime.datetime.now()

        self._raw['content'] = new_content
        self._raw['edited_timestamp'] = timestamp.isoformat()
        self._update(self.channel, self.author, self._raw)

    @property
    def as_db(self):
        return {
            'message_id': int(self.id),
            'channel_id': int(self.channel_id),
            'author_id': int(self.author.id),

            'edited_timestamp': dt_to_json(self.edited_at),

            'content': str(self.content),
            'attachments': [],
            'embeds': [],

            'pinned': self.pinned,
        }

    @property
    def as_json(self):
        # TODO: mention detection
        mentions = []
        mention_roles = []

        # TODO: attachments
        attachments = []

        # TODO?: reactions
        reactions = []

        return {
            'id': str(self.id),
            'channel_id': str(self.channel_id),
            'author': self.author.as_json,
            'content': self.content,
            'timestamp': dt_to_json(self.timestamp),
            'edited_timestamp': dt_to_json(self.edited_at),
            'tts': False,

            'mention_everyone': f'<@{self.guild.id}>' in self.content,
            'mentions': mentions,
            'mention_roles': mention_roles,

            'attachments': attachments,
            'embeds': self.embeds,
            'reactions': reactions,
            'pinned': self.pinned,
            #'webhook_id': '',
        }
