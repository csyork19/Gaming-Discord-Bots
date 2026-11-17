"""
Discord member-count bot.

Renames a channel to show the server's total member count, e.g. "members-1234".

Setup:
  1. pip install -U discord.py
  2. In the Discord Developer Portal -> your app -> Bot:
       - turn ON "Server Members Intent"
  3. Invite the bot with the "Manage Channels" permission
     (or grant it Manage Channel on the one channel you want renamed).
  4. Set environment variables and run:
       DISCORD_TOKEN=your-bot-token
       CHANNEL_ID=123456789012345678
       python member_count_bot.py

Optional environment variables:
  NAME_FORMAT   Channel name template. Default: "members-{count}"
                Example: "👥-members-{count}"
  COUNT_BOTS    "true" (default) counts everyone; "false" counts humans only.

Note: Discord only allows a channel to be renamed about 2 times per 10 minutes,
so the bot updates right away on the first change, then waits at least
5 minutes before the next rename. Joins/leaves during that wait are batched
into one update.
"""

import asyncio
import logging
import os

import discord

TOKEN = os.environ["DISCORD_TOKEN"]
CHANNEL_ID = int(os.environ["CHANNEL_ID"])
NAME_FORMAT = os.environ.get("NAME_FORMAT", "members-{count}")
COUNT_BOTS = os.environ.get("COUNT_BOTS", "true").lower() in ("1", "true", "yes")
MIN_SECONDS_BETWEEN_RENAMES = 300  # stays under Discord's 2-per-10-minutes limit

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("member-count-bot")

intents = discord.Intents.default()
intents.members = True  # needed for join/leave events and human-only counting


class MemberCountBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self._update_needed = asyncio.Event()

    async def setup_hook(self):
        self.loop.create_task(self._rename_worker())

    async def on_ready(self):
        log.info("Logged in as %s", self.user)
        self.request_update()

    async def on_member_join(self, member: discord.Member):
        if member.guild.id == self._guild_id():
            self.request_update()

    async def on_member_remove(self, member: discord.Member):
        if member.guild.id == self._guild_id():
            self.request_update()

    def request_update(self):
        self._update_needed.set()

    def _guild_id(self):
        channel = self.get_channel(CHANNEL_ID)
        return channel.guild.id if channel else None

    def _count(self, guild: discord.Guild) -> int:
        if COUNT_BOTS:
            return guild.member_count or len(guild.members)
        return sum(1 for m in guild.members if not m.bot)

    async def _rename_worker(self):
        await self.wait_until_ready()
        while not self.is_closed():
            await self._update_needed.wait()
            self._update_needed.clear()
            await self._update_channel()
            # Cool down; any joins/leaves meanwhile set the event again
            # and get handled in one rename after the wait.
            await asyncio.sleep(MIN_SECONDS_BETWEEN_RENAMES)

    async def _update_channel(self):
        channel = self.get_channel(CHANNEL_ID)
        if channel is None:
            log.error("Channel %s not found. Check CHANNEL_ID and bot access.", CHANNEL_ID)
            return

        guild = channel.guild
        if not COUNT_BOTS and not guild.chunked:
            await guild.chunk()  # load the full member list so bots can be excluded

        new_name = NAME_FORMAT.format(count=self._count(guild))
        if channel.name == new_name:
            return

        try:
            await channel.edit(name=new_name, reason="Update member count")
            log.info("Renamed channel to %r", new_name)
        except discord.Forbidden:
            log.error("Missing 'Manage Channels' permission for channel %s.", CHANNEL_ID)
        except discord.HTTPException as e:
            log.error("Failed to rename channel: %s", e)


if __name__ == "__main__":
    MemberCountBot().run(TOKEN, log_handler=None)
