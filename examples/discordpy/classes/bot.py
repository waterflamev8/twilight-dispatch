import aio_pika
import asyncio
import config
import inspect
import logging
import orjson
import sys
import traceback
import zangy

from classes.misc import Status, Session
from classes.state import State
from discord import utils
from discord.ext import commands
from discord.ext.commands import DefaultHelpCommand, Context
from discord.ext.commands.core import _CaseInsensitiveDict
from discord.ext.commands.view import StringView
from discord.gateway import DiscordWebSocket
from discord.http import HTTPClient
from discord.utils import parse_time, to_json

log = logging.getLogger(__name__)


class Bot(commands.AutoShardedBot):
    def __init__(self, command_prefix, help_command=DefaultHelpCommand(), description=None, **kwargs):
        self.command_prefix = command_prefix
        self.extra_events = {}
        self._BotBase__cogs = {}
        self._BotBase__extensions = {}
        self._checks = []
        self._check_once = []
        self._before_invoke = None
        self._after_invoke = None
        self._help_command = None
        self.description = inspect.cleandoc(description) if description else ""
        self.owner_id = kwargs.get("owner_id")
        self.owner_ids = kwargs.get("owner_ids", set())
        self._skip_check = lambda x, y: x == y
        self.help_command = help_command
        self.case_insensitive = kwargs.get("case_insensitive", False)
        self.all_commands = _CaseInsensitiveDict() if self.case_insensitive else {}

        self.ws = None
        self.loop = asyncio.get_event_loop()
        self.http = HTTPClient(None, loop=self.loop)

        self._handlers = {"ready": self._handle_ready}
        self._hooks = {}
        self._listeners = {}

        self._connection = None
        self._closed = False
        self._ready = asyncio.Event()

        self._redis = None
        self._amqp = None
        self._amqp_channel = None
        self._amqp_queue = None

    @property
    def config(self):
        return config

    async def user(self):
        return await self._connection.user()

    async def users(self):
        return await self._connection._users()

    async def guilds(self):
        return await self._connection.guilds()

    async def emojis(self):
        return await self._connection.emojis()

    async def cached_messages(self):
        return await self._connection._messages()

    async def private_channels(self):
        return await self._connection.private_channels()

    async def shard_count(self):
        return int(await self._redis.get("gateway_shards"))

    async def started(self):
        return parse_time(str(await self._connection._get("gateway_started").split(".")[0]))

    async def statuses(self):
        return [Status(x) for x in await self._connection._get("gateway_statuses")]

    async def sessions(self):
        return {int(x): Session(y) for x, y in (await self._connection._get("gateway_sessions")).items()}

    async def get_channel(self, channel_id):
        return await self._connection.get_channel(channel_id)

    async def get_guild(self, guild_id):
        return await self._connection._get_guild(guild_id)

    async def get_user(self, user_id):
        return await self._connection.get_user(user_id)

    async def get_emoji(self, emoji_id):
        return await self._connection.get_emoji(emoji_id)

    async def get_all_channels(self):
        for guild in await self.guilds():
            for channel in await guild.channels():
                yield channel

    async def get_all_members(self):
        for guild in await self.guilds():
            for member in await guild.members():
                yield member

    async def _get_state(self, **options):
        return State(
            dispatch=self.dispatch,
            handlers=self._handlers,
            hooks=self._hooks,
            http=self.http,
            loop=self.loop,
            redis=self._redis,
            shard_count=await self.shard_count(),
            **options,
        )

    async def get_context(self, message, *, cls=Context):
        view = StringView(message.content)
        ctx = cls(prefix=None, view=view, bot=self, message=message)

        if self._skip_check((await message.author()).id, (await self.user()).id):
            return ctx

        prefix = await self.get_prefix(message)
        invoked_prefix = prefix

        if isinstance(prefix, str):
            if not view.skip_string(prefix):
                return ctx
        else:
            try:
                if message.content.startswith(tuple(prefix)):
                    invoked_prefix = utils.find(view.skip_string, prefix)
                else:
                    return ctx

            except TypeError:
                if not isinstance(prefix, list):
                    raise TypeError("get_prefix must return either a string or a list of string, "
                                    "not {}".format(prefix.__class__.__name__))

                for value in prefix:
                    if not isinstance(value, str):
                        raise TypeError("Iterable command_prefix or list returned from get_prefix must "
                                        "contain only strings, not {}".format(value.__class__.__name__))

                raise

        invoker = view.get_word()
        ctx.invoked_with = invoker
        ctx.prefix = invoked_prefix
        ctx.command = self.all_commands.get(invoker)
        return ctx

    async def process_commands(self, message):
        if (await message.author()).bot:
            return

        ctx = await self.get_context(message)
        await self.invoke(ctx)

    async def receive_message(self, msg):
        self.ws._dispatch("socket_raw_receive", msg)

        msg = orjson.loads(msg)

        self.ws._dispatch("socket_response", msg)

        op = msg.get("op")
        data = msg.get("d")
        event = msg.get("t")
        old = msg.get("old")

        if op != self.ws.DISPATCH:
            return

        try:
            func = self.ws._discord_parsers[event]
        except KeyError:
            log.debug("Unknown event %s.", event)
        else:
            try:
                await func(data, old)
            except asyncio.CancelledError:
                pass
            except Exception:
                try:
                    await self.on_error(event)
                except asyncio.CancelledError:
                    pass

        removed = []
        for index, entry in enumerate(self.ws._dispatch_listeners):
            if entry.event != event:
                continue

            future = entry.future
            if future.cancelled():
                removed.append(index)
                continue

            try:
                valid = entry.predicate(data)
            except Exception as exc:
                future.set_exception(exc)
                removed.append(index)
            else:
                if valid:
                    ret = data if entry.result is None else entry.result(data)
                    future.set_result(ret)
                    removed.append(index)

        for index in reversed(removed):
            del self.ws._dispatch_listeners[index]

    async def send_message(self, msg):
        data = to_json(msg)
        self.ws._dispatch("socket_raw_send", data)
        await self._amqp_channel.default_exchange.publish(aio_pika.Message(body=data), routing_key="gateway.send")

    async def start(self):
        log.info("Starting...")

        self._redis = await zangy.create_pool(self.config.redis_url, 5)
        self._amqp = await aio_pika.connect_robust(self.config.amqp_url)
        self._amqp_channel = await self._amqp.channel()
        self._amqp_queue = await self._amqp_channel.get_queue("gateway.recv")

        self._connection = await self._get_state()
        self._connection._get_client = lambda: self

        self.ws = DiscordWebSocket(socket=None, loop=self.loop)
        self.ws.token = self.http.token
        self.ws._connection = self._connection
        self.ws._discord_parsers = self._connection.parsers
        self.ws._dispatch = self.dispatch
        self.ws.call_hooks = self._connection.call_hooks

        await self.http.static_login(self.config.token, bot=True)

        for extension in self.config.cogs:
            try:
                self.load_extension("cogs." + extension)
            except Exception:
                log.error(f"Failed to load extension {extension}.", file=sys.stderr)
                log.error(traceback.print_exc())

        async with self._amqp_queue.iterator() as queue_iter:
            async for message in queue_iter:
                async with message.process(ignore_processed=True):
                    await self.receive_message(message.body)
                    message.ack()
