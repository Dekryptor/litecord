import datetime
import logging
import re
import asyncio

from .base import LitecordObject
from .member import Member
#from ..snowflake import snowflake_time
from ..utils import dt_to_json
from ..enums import MessageType

log = logging.getLogger(__name__)

MENTION = re.compile(r'<@(\d+)>')


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
        If the message was edited, this is set to the time at
        which this message was edited.
    pinned: bool
        If the message is pinned in the channel.
    """

    __slots__ = ('_raw', 'id', 'author_id', 'channel_id',
                 'timestamp', 'channel',
                 'author', 'member', 'content', 'edited_at')

    def __init__(self, server, channel, author, raw):
        super().__init__(server)
        self._raw = raw

        self.id = int(raw['message_id'])
        self.author_id = int(raw['author_id'])
        self.channel_id = int(raw['channel_id'])
        self.type = raw.get('type', MessageType.DEFAULT)
        self.edited_at = None
        self.embeds = []

        self.attachments = []

        self._update(channel, author, raw)

    def _update(self, channel, author, raw):
        assert isinstance(author, Member)

        self.channel = channel
        self.author = author
        self.guild = channel.guild

        self.nonce = raw.get('nonce')
        self.created_at = self.to_timestamp(self.id)

        self.content = raw['content']
        self.pinned = raw.get('pinned', False)
        self.edited_timestamp = raw.get('edited_timestamp')

        if self.edited_timestamp is not None:
            self.edited_at = datetime.datetime.strptime(self.edited_timestamp,
                                                        "%Y-%m-%dT%H:%M:%S.%f")

        # load attachment data
        attachments = raw.get('attachments', [])
        for ihash in attachments:
            """
            async def _updater():
                image = await self.server.images.raw_image_get(ihash)
                if not image:
                    return

                self.attachments.append(Attachment(image))

            asyncio.ensure_future(_updater())
            """
            img = self.server.images.force_get_cache(ihash)
            if not img:
                return

            self.attachments.append(Attachment(img))

    def __repr__(self):
        return f'<Message id={self.id} pinned={self.pinned} author={self.author}>'

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

            'type': self.type,
            'edited_timestamp': dt_to_json(self.edited_at),

            'content': str(self.content),
            'attachments': [a.id for a in self.attachments],
            'embeds': self.embeds,

            'pinned': self.pinned,
        }

    @property
    def as_json(self):
        # TODO: mention detection
        mentions = []
        mention_roles = []
        uids = MENTION.findall(self.content)

        for uid in uids:
            try:
                uid = int(uid)
            except ValueError:
                continue

            user = self.server.get_user(uid)
            if user:
                mentions.append(user.as_json)

        # TODO?: reactions
        reactions = []

        return {
            'id': str(self.id),
            'channel_id': str(self.channel_id),

            'author': self.author.user.as_json,
            'content': self.content,
            'timestamp': dt_to_json(self.created_at),
            'edited_timestamp': dt_to_json(self.edited_at),
            'tts': False,

            'mention_everyone': f'<@{self.guild.id}>' in self.content,
            'mentions': mentions,
            'mention_roles': mention_roles,

            'attachments': [a.as_json for a in self.attachments],
            'embeds': self.embeds,
            'reactions': reactions,
            'pinned': self.pinned,
            'type': self.type,
            # 'webhook_id': '',
            'nonce': self.nonce,
        }


class Attachment:
    """Attachment object."""
    def __init__(self, raw):
        self.id = raw['hash']
        self.data = raw['data']
        self.filename = raw['metadata']['filename']

        self.size = raw['metadata']['size']
        self.url = raw['metadata']['url']
        # TODO: proxy
        self.proxy_url = raw['metadata']['url']

        # hardcoded yes i know
        self.dimensions = (800, 600)

    @property
    def as_json(self):
        raw = {
            'id': self.id,

            'filename': self.filename,
            'url': self.url,
            'proxy_url': self.proxy_url,
            'size': self.size,

            # oof
            'height': self.dimensions[0],
            'width': self.dimensions[1]
        }

        return raw
