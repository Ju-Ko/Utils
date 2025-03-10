import asyncio
import concurrent.futures
import datetime
from functools import partial
from io import BytesIO
from typing import Optional

import aiohttp
import aiohttp.client_exceptions
import unidecode
from discord.ext import commands, tasks

from main import UtilsBot
from src.checks.role_check import is_high_staff, is_staff
from src.checks.user_check import is_owner
from src.helpers.api_helper import *
from src.helpers.graph_helper import pie_chart_from_amount_and_labels, file_from_timestamps
from src.helpers.storage_helper import DataHelper
from src.helpers.sync_mongo_helper import get_guild_score, get_user_score
from src.storage import config

exceptions = (asyncio.exceptions.TimeoutError, aiohttp.client_exceptions.ServerDisconnectedError,
              aiohttp.client_exceptions.ClientConnectorError)
waiting_exceptions = (aiohttp.client_exceptions.ClientOSError, aiohttp.client_exceptions.ContentTypeError)
default_timeout = 45


class Statistics(commands.Cog):
    def __init__(self, bot: UtilsBot):
        self.bot = bot
        self.bot.database_handler = self
        self.session = aiohttp.ClientSession()
        self.restarting = False
        self.data = DataHelper()
        self.last_update = self.bot.create_processing_embed("Working...", "Starting processing!")
        self.last_ping = datetime.datetime.now()
        self.active_channel_ids = []
        self.running = False
        self.channel_lock = asyncio.Lock()
        self.update_motw.start()
        self.bot.loop.create_task(self.startup_check())

    @commands.command()
    @is_owner()
    async def add_discrims(self, ctx):
        await ctx.reply("Starting...")
        async for user_document in self.bot.mongo.discord_db.users.find():
            user_id = user_document.get("_id")
            user = self.bot.get_user(user_id)
            if user is None:
                try:
                    user = await self.bot.fetch_user(user_id)
                except discord.errors.NotFound:
                    continue
            name, discriminator = user.name, user.discriminator
            await self.bot.mongo.discord_db.users.update_one({"_id": user_id}, {"$set": {"name": name,
                                                                                         "discriminator": discriminator}})
        await ctx.reply("Done.")

    async def startup_check(self):
        query = self.bot.mongo.discord_db.loading_stats.find({"active": True})
        async for channel_document in query:
            channel = self.bot.get_channel(channel_document.get("_id"))
            sent_message_id = channel_document.get("sent_message_id", None)
            self.bot.loop.create_task(self.load_channel(channel, sent_message_id))
        if not self.running:
            self.bot.loop.create_task(self.update_embeds())

    async def update_embeds(self):
        self.running = True
        pipeline = [
            {
                "$project": {"_id": "$guild_id"}
            }
        ]
        aggregation = self.bot.mongo.discord_db.loading_stats.aggregate(pipeline=pipeline)
        unique_guild_ids = set(x.get("_id") for x in await aggregation.to_list(length=None))
        done_guild_ids = []
        if len(unique_guild_ids) == 0:
            self.running = False
            return
        while True:
            for guild_id in unique_guild_ids:
                lowest_percent = 100
                sent_message_id = None
                sent_message_channel_id = None
                query = self.bot.mongo.discord_db.loading_stats.find({"guild_id": guild_id})
                channel_documents = await query.to_list(length=None)
                for channel_document in channel_documents:
                    possible_sent = channel_document.get("sent_message_id", None)
                    if possible_sent is not None:
                        sent_message_id = possible_sent
                        sent_message_channel_id = channel_document.get("_id")
                    if not channel_document.get("active"):
                        continue
                    latest_time = channel_document.get("latest_time")
                    percent = 1 - (
                            (latest_time - channel_document.get("message_time")).total_seconds()
                            /
                            (latest_time - channel_document.get("earliest_time")).total_seconds())
                    percent = percent * 100
                    if percent < lowest_percent:
                        lowest_percent = percent
                if sent_message_id is None or sent_message_channel_id is None:
                    continue
                if lowest_percent == 100:
                    done_guild_ids.append(guild_id)
                update_channel = self.bot.get_channel(sent_message_channel_id)
                if update_channel is not None:
                    update_message = await update_channel.fetch_message(sent_message_id)
                else:
                    update_message = None
                stars = int(round(lowest_percent / 10))
                stars_string = "\\*" * stars
                dashes = 10 - stars
                if lowest_percent != 100:
                    if update_message is not None:
                        await update_message.edit(embed=self.bot.create_processing_embed(
                            "Back-Dating Statistics",
                            f"Progress: {stars_string}{'-' * dashes} ({lowest_percent:.2f}%)"))
                else:
                    if update_message is not None:
                        await update_message.edit(embed=self.bot.create_completed_embed("Back-Dated Statistics!",
                                                                                    "Finished back-dating statistics!"))
            for guild_id in done_guild_ids:
                unique_guild_ids.remove(guild_id)
            done_guild_ids = []
            if len(unique_guild_ids) == 0:
                self.running = False
                return
            await asyncio.sleep(1)

    @commands.command()
    @is_staff()
    async def load_stats(self, ctx):
        sent_message = await ctx.reply(embed=self.bot.create_processing_embed("Back-dating Statistics",
                                                                              "Progress: Starting..."))
        for channel in ctx.guild.text_channels:
            print(channel.id)
            channel_doc = await self.bot.mongo.discord_db.channels.find_one({"_id": channel.id})
            if channel_doc is not None and channel_doc.get("nostore", False):
                if channel == ctx.channel:
                    loading_doc = {"_id": channel.id, "guild_id": channel.guild.id, "active": False,
                                   "sent_message_id": sent_message.id}
                    await self.bot.mongo.force_insert(self.bot.mongo.discord_db.loading_stats, loading_doc)
                continue
            if channel == ctx.channel:
                self.bot.loop.create_task(self.load_channel(channel, sent_message.id))
            else:
                self.bot.loop.create_task(self.load_channel(channel))
        await asyncio.sleep(1)
        if not self.running:
            self.bot.loop.create_task(self.update_embeds())

    async def load_channel(self, channel: discord.TextChannel, sent_message_id=None):
        channel_doc = await self.bot.mongo.discord_db.channels.find_one({"_id": channel.id})
        if channel_doc is not None and channel_doc.get("nostore", False):
            return
        after = datetime.datetime(2015, 1, 1)
        most_recent_message = channel.history(limit=1)
        most_recent_message = await most_recent_message.flatten()
        try:
            most_recent_message = most_recent_message[0]
        except IndexError:
            return
        earliest_message = channel.history(oldest_first=True, limit=1)
        earliest_message = await earliest_message.flatten()
        earliest_message = earliest_message[0]
        stored_channel = await self.bot.mongo.discord_db.loading_stats.find_one({"_id": channel.id})
        if sent_message_id is not None and stored_channel is None:
            loading_doc = {"_id": channel.id, "guild_id": channel.guild.id, "active": True,
                           "sent_message_id": sent_message_id}
            await self.bot.mongo.force_insert(self.bot.mongo.discord_db.loading_stats, loading_doc)
        if stored_channel is not None:
            last_message_id = stored_channel.get("message_id")
            try:
                last_message = await channel.fetch_message(last_message_id)
                after = last_message
            except discord.errors.NotFound:
                after = stored_channel.get("message_time")
        history_iterator = channel.history(after=after, limit=None)
        while True:
            if history_iterator.messages.empty():
                await history_iterator.fill_messages()
            if history_iterator.messages.empty():
                break
            last_message = await self.add_messages_to_db(history_iterator.messages)
            if last_message is not None:
                if last_message == most_recent_message:
                    await self.bot.mongo.discord_db.loading_stats.update_one({"_id": channel.id},
                                                                             {"$set": {"active": False}})
                    return True
                loading_doc = {"_id": channel.id, "message_id": last_message.id,
                               "message_time": last_message.created_at, "guild_id": channel.guild.id, "active": True,
                               "sent_message_id": sent_message_id, "latest_time": most_recent_message.created_at,
                               "earliest_time": earliest_message.created_at}
                await self.bot.mongo.force_insert(self.bot.mongo.discord_db.loading_stats, loading_doc)

    async def add_messages_to_db(self, message_queue: asyncio.Queue):
        this_batch = []
        while not message_queue.empty():
            this_batch.append(message_queue.get_nowait())
        await self.bot.mongo.insert_channel_messages(this_batch)
        return this_batch[-1] if len(this_batch) != 0 else None

    @tasks.loop(seconds=1800, count=None)
    async def update_motw(self):
        monkey_guild: discord.Guild = self.bot.get_guild(config.monkey_guild_id)
        motw_role = monkey_guild.get_role(config.motw_role_id)
        motw_channel: discord.TextChannel = self.bot.get_channel(config.motw_channel_id)
        with concurrent.futures.ProcessPoolExecutor() as pool:
            results = await self.bot.loop.run_in_executor(pool, partial(get_guild_score, config.monkey_guild_id))
        results = results[:12]
        members = []
        for user in results:
            member = monkey_guild.get_member(user[0])
            if member is None:
                await self.bot.mongo.discord_db.members.update_one({"_id": {"user_id": user[0],
                                                                            "guild_id": monkey_guild.id}},
                                                                   {'$set': {"deleted": True}})
                continue
            members.append(member)
        for member in monkey_guild.members:
            if motw_role in member.roles and member not in members:
                await member.remove_roles(motw_role)
                await motw_channel.send(f"Goodbye {member.display_name}! You will be missed!")
        for member in members:
            if motw_role not in member.roles:
                await member.add_roles(motw_role)
                await motw_channel.send(f"Welcome {member.display_name}! I hope you enjoy your stay!")

    async def _compile_snipe(self, message_found, channel):
        user_id = message_found.get("user_id")
        content = message_found.get("content")
        try:
            embed_json = message_found.get("embeds", [])[0]
        except IndexError:
            embed_json = None
        timestamp = message_found.get("created_at")
        user = self.bot.get_user(user_id)
        if user is None:
            user = await self.bot.fetch_user(user_id)
        embed = discord.Embed(title="Sniped Message", colour=discord.Colour.red())
        embed.set_author(name=user.name, icon_url=user.avatar_url)
        embed.set_footer(text=f"Message ID: {message_found.get('_id')}")
        preceding_message = (await channel.history(before=timestamp, limit=1).flatten())[0] or None
        if embed_json is None:
            embed.description = content
        else:
            embed = discord.Embed.from_dict(embed_json)
            if len(embed.fields) == 0 or not embed.fields[0].name.startswith("Previous Title"):
                embed.insert_field_at(0, name="Previous Title", value=embed.title, inline=False)
            embed.title = "Sniped Message!"
        if preceding_message is not None:
            embed.add_field(name="\u200b", value=f"[Previous Message]({preceding_message.jump_url})",
                            inline=False)
        embed.timestamp = timestamp
        return embed

    @commands.command()
    async def snipe(self, ctx, amount=1):
        sent = await ctx.reply(embed=self.bot.create_processing_embed("Processing...", "Getting sniped message..."))
        cursor = self.bot.mongo.discord_db.messages.find({"deleted": True, "channel_id": ctx.channel.id})
        cursor.sort("created_at", -1).limit(1).skip(amount - 1)
        messages_found = await cursor.to_list(length=1)
        if len(messages_found) == 0:
            await sent.edit(embed=self.bot.create_error_embed("There aren't that many deleted messages!"))
            return
        message_found = messages_found[0]
        embed = await self._compile_snipe(message_found, ctx.channel)
        await sent.edit(embed=embed)

    @commands.command()
    @is_owner()
    async def nostore(self, ctx, channel: Optional[discord.TextChannel]):
        if channel is None:
            channel = ctx.channel
        await self.bot.mongo.discord_db.channels.update_one({"_id": channel.id}, {"$set": {"nostore": True}})
        await ctx.reply("nostore set.")

    @commands.command(aliases=["ghostping", "ghost"])
    async def ghost_ping(self, ctx, member: Optional[discord.Member]):
        if member is None:
            member = ctx.author
        sent = await ctx.reply(embed=self.bot.create_processing_embed("Searching...",
                                                                      "Looking for your last ghost ping!"))
        role_ids = [x.id for x in member.roles]
        cursor = self.bot.mongo.discord_db.messages.find({"$or": [{"mentions": member.id},
                                                                  {"role_mentions": {"$in": role_ids}},
                                                                  {"mention_everyone": True}],
                                                          "deleted": True,
                                                          "channel_id": ctx.channel.id})
        cursor.sort("created_at", -1).limit(1)
        ghost_ping = await cursor.to_list(length=1)
        if len(ghost_ping) == 0:
            await sent.edit(embed=self.bot.create_error_embed("I couldn't find a ghost ping for you!"))
            return
        ghost_ping = ghost_ping[0]
        embed = await self._compile_snipe(ghost_ping, ctx.channel)
        await sent.edit(embed=embed)

    @commands.command()
    async def edits(self, ctx, message_id: Optional[int]):
        if ctx.message.reference is None and message_id is None:
            await ctx.reply(embed=self.bot.create_error_embed("Please reply to a message with this command!"))
            return
        if message_id is None:
            message_id = ctx.message.reference.message_id
        cursor = self.bot.mongo.discord_db.messages.find({"_id": message_id, "channel_id": ctx.channel.id})
        message = await cursor.to_list(length=1)
        if len(message) == 0:
            await ctx.reply(embed=self.bot.create_error_embed("I couldn't find that message!"))
            return
        message = message[0]
        sent = await ctx.reply(embed=self.bot.create_processing_embed("Processing...", "Getting message edits..."))
        edits = sorted(message.get("edits"), key=lambda x: x.get("timestamp"))
        original_message = message
        original_timestamp_string = message.get("created_at").strftime("%Y-%m-%d %H:%M:%S")
        if len(edits) == 0:
            await sent.edit(embed=self.bot.create_error_embed("That message has no known edits."))
            return
        embed = discord.Embed(title="Edits for Message", colour=discord.Colour.gold())
        if len(original_message.get("content")) > 1024:
            content = original_message.get("content")[:1021] + "..."
        else:
            content = original_message.get("content")
        first_three = edits[:3]
        last_edits = edits[3:]
        last_edits = last_edits[::-1]
        if content != "" and not any(len(x.get("embeds")) > 0 for x in [*edits, original_message]):
            embed.add_field(name=f"Original Message ({original_timestamp_string})",
                            value=content, inline=False)

            for index, edit in enumerate(first_three):
                edited_timestamp_string = edit.get("timestamp").strftime("%Y-%m-%d %H:%M:%S")
                if len(edit.get("content")) > 1024:
                    content = edit.get("content")[:1021] + "..."
                else:
                    content = edit.get("content")
                embed.add_field(name=f"Edit {index + 1} ({edited_timestamp_string})", value=content, inline=False)
            for index, edit in enumerate(last_edits):
                if len(embed) >= 5000 or len(embed.fields) > 23:
                    break
                edited_timestamp_string = edit.get("timestamp").strftime("%Y-%m-%d %H:%M:%S")
                if len(edit.get("content")) > 1024:
                    content = edit.get("content")[:1021] + "..."
                else:
                    content = edit.get("content")
                embed.insert_field_at(index=4, name=f"Edit {len(edits) - index} ({edited_timestamp_string})",
                                      value=content, inline=False)
        elif any(len(x.get("embeds")) > 0 for x in [*edits, original_message]):
            if len(original_message.get("embeds", [])) > 0:
                original_embed = discord.Embed.from_dict(original_message.get("embeds")[0])
                embed.title = "Edits for Embed Message"
                embed.description = f"Original Embed Title: {original_embed.title}\nOriginal Embed Description: "
                embed.description += original_embed.description[:2048 - len(embed.description)]
                last_edit_title = original_embed.title
                last_edit_description = original_embed.description
                last_edit_fields = original_embed.fields
            elif content != "":
                embed.title = "Edits for Embed Message"
                embed.description = f"Original Content: {content}"
                last_edit_title = ""
                last_edit_description = ""
                last_edit_fields = []
            else:
                last_edit_title = ""
                last_edit_description = ""
                last_edit_fields = []
            for index, edit in enumerate(edits):
                if len(embed) >= 5000 or len(embed.fields) > 23:
                    break
                edited_timestamp_string = edit.get("timestamp").strftime("%Y-%m-%d %H:%M:%S")
                field_title = f"Edit {index + 1} ({edited_timestamp_string})"
                if len(edit.get("embeds")) == 0:
                    field_value = f"New Content: {edit.get('content')}"
                else:
                    edit_embed = discord.Embed.from_dict(edit.get("embeds")[0])
                    field_value = ""
                    if edit_embed.title != last_edit_title and edit_embed.title != discord.Embed.Empty:
                        last_edit_title = edit_embed.title
                        field_value += f"New Title: **{last_edit_title}**\n"
                    if edit_embed.description != last_edit_description and \
                            edit_embed.description != discord.Embed.Empty:
                        last_edit_description = edit_embed.description
                        field_value += (last_edit_description + "\n")[:1024 - len(field_value)]
                    if edit_embed.fields != last_edit_fields:
                        for field_index, field in enumerate(edit_embed.fields):
                            if field != edit_embed.fields[field_index]:
                                field_value += (f"Field {field_index} name: **{field.name}**, "
                                                f"value: {field.value}\n"[:1024 - len(field_value)])
                        last_edit_fields = edit_embed.fields
                    if len(field_value) == 1024:
                        field_value = field_value[:1021] + "..."
                embed.add_field(name=field_title, value=field_value, inline=False)
        else:
            await ctx.reply(embed=self.bot.create_error_embed("No content or embed found!"))
        author = await self.bot.mongo.find_by_id(self.bot.mongo.discord_db.users, message.get("user_id"))
        discord_author = self.bot.get_user(author.get("_id"))
        embed.set_author(name=author.get("name"), url=discord_author.avatar_url)
        embed.add_field(name="\u200b", value=f"[Jump to Message](https://discord.com/channels/{message.get('guild_id')}"
                                             f"/{message.get('channel_id')}/{message.get('_id')})",
                        inline=False)
        await sent.edit(embed=embed)

    @commands.command(description="Get leaderboard pie!")
    async def leaderpie(self, ctx):
        sent = await ctx.reply(embed=self.bot.create_processing_embed("Generating leaderboard",
                                                                      "Processing messages for leaderboard..."))
        with concurrent.futures.ProcessPoolExecutor() as pool:
            results = await self.bot.loop.run_in_executor(pool, partial(get_guild_score, ctx.guild.id))
            labels = []
            amounts = []
            for user_id, score in results[:30]:
                member = await self.bot.mongo.find_by_id(self.bot.mongo.discord_db.members, {"user_id": user_id,
                                                                                             "guild_id": ctx.guild.id})
                nickname = member.get("nick", None)
                if nickname is None:
                    user = await self.bot.mongo.find_by_id(self.bot.mongo.discord_db.users, user_id)
                    nickname = user.get("name", "Unknown")
                labels.append(nickname)
                amounts.append(score)
            smaller_amounts = amounts[15:]
            labels = labels[:15]
            amounts = amounts[:15]
            amounts.append(sum(smaller_amounts))
            labels.append("Other")
            await sent.edit(embed=self.bot.create_processing_embed("Got leaderboard!", "Generating pie chart."))
            data = await self.bot.loop.run_in_executor(pool, partial(pie_chart_from_amount_and_labels,
                                                                     labels, amounts))
        file = BytesIO(data)
        file.seek(0)
        discord_file = discord.File(fp=file, filename="image.png")
        await ctx.reply(file=discord_file)
        await sent.delete()

    @commands.command()
    async def score(self, ctx, member: Optional[discord.Member]):
        if member is None:
            member = ctx.author
        with concurrent.futures.ProcessPoolExecutor() as pool:
            score = await self.bot.loop.run_in_executor(pool, partial(get_user_score, member.id, member.guild.id))
        embed = self.bot.create_completed_embed(f"Score for {member.nick or member.name} - past 7 days",
                                                str(score))
        if ctx.guild.id == config.monkey_guild_id:
            embed.set_footer(text="More information about this in #role-assign (monkeys of the week!)")
        else:
            embed.set_footer(text="Score is based off of your active time this week.")
        await ctx.reply(embed=embed)

    @commands.command(description="Count how many times a phrase has been said!")
    async def count(self, ctx, *, phrase):
        if len(phrase) > 223:
            await ctx.reply(embed=self.bot.create_error_embed("That phrase was too long!"))
            return
        sent = await ctx.reply(embed=self.bot.create_processing_embed("Counting...",
                                                                      f"Counting how many times \"{phrase}\" "
                                                                      f"has been said..."))

        amount = await self.bot.mongo.discord_db.messages.count_documents({"$text": {"$search": phrase},
                                                                           "guild_id": ctx.guild.id,
                                                                           "deleted": False})
        embed = self.bot.create_completed_embed(
            f"Number of times \"{phrase}\" has been said:", f"**{amount}** times!")
        embed.set_footer(text="If you entered a phrase, remember to surround it in **straight** quotes ("
                              "\"\")!")
        await sent.edit(embed=embed)

    @commands.command(description="Count how many times a user has said a phrase!", aliases=["countuser", "usercount"])
    async def count_user(self, ctx, member: Optional[discord.Member], *, phrase):
        if member is None:
            member = ctx.author
        if len(phrase) > 180:
            await ctx.reply(embed=self.bot.create_error_embed("That phrase was too long!"))
            return
        sent = await ctx.reply(embed=self.bot.create_processing_embed("Counting...",
                                                                      f"Counting how many times {member.display_name} "
                                                                      f"said: \"{phrase}\""))
        amount = await self.bot.mongo.discord_db.messages.count_documents({"$text": {"$search": phrase},
                                                                           "guild_id": ctx.guild.id,
                                                                           "user_id": member.id,
                                                                           "deleted": False})
        embed = self.bot.create_completed_embed(
            f"Number of times {member.display_name} said: \"{phrase}\":", f"**{amount}** times!")
        embed.set_footer(text="If you entered a phrase, remember to surround it in **straight** quotes (\"\")!")
        await sent.edit(embed=embed)

    @commands.command(aliases=["ratio", "percentage"])
    async def percent(self, ctx, member: Optional[discord.User]):
        if member is None:
            member = ctx.author
        sent = await ctx.reply(embed=self.bot.create_processing_embed("Counting...",
                                                                      f"Counting {member.name}'s amount of "
                                                                      f"messages!"))
        guild_count = await self.bot.mongo.discord_db.messages.count_documents({"guild_id": ctx.guild.id})
        member_count = await self.bot.mongo.discord_db.messages.count_documents({"user_id": member.id,
                                                                                 "guild_id": ctx.guild.id})
        percentage = (member_count / guild_count) * 100
        embed = self.bot.create_completed_embed(f"Amount of messages {member.name} has sent!",
                                                f"{member.name} has sent {member_count:,} messages. "
                                                f"That's {percentage:.3f}% "
                                                f"of the server's total!")
        await sent.edit(embed=embed)

    @commands.command(description="Count how many messages have been sent in this guild!")
    async def messages(self, ctx):
        sent = await ctx.reply(embed=self.bot.create_processing_embed("Counting...", "Counting all messages sent..."))
        amount = await self.bot.mongo.discord_db.messages.count_documents({"guild_id": ctx.guild.id, "deleted": False})
        await sent.edit(embed=self.bot.create_completed_embed(
            title="Total Messages sent in this guild!", text=f"**{amount:,}** messages!"
        ))

    # noinspection DuplicatedCode
    @commands.command(description="Plots a graph of word usage over time.", aliases=["wordstats, wordusage",
                                                                                     "word_stats", "phrase_usage",
                                                                                     "phrasestats", "phrase_stats",
                                                                                     "phraseusage"])
    async def word_usage(self, ctx, phrase, group: Optional[str] = "m"):
        async with ctx.typing():
            if len(phrase) > 180:
                await ctx.reply(embed=self.bot.create_error_embed("That phrase was too long!"))
                return
            pipeline = [
                {
                    "$match": {"guild_id": ctx.guild.id, "deleted": False, "$text": {"$search": phrase}}
                },
                {
                    "$project": {"_id": "$created_at"}
                }
            ]
            aggregation = self.bot.mongo.discord_db.messages.aggregate(pipeline=pipeline)
            times = [x.get("_id") for x in await aggregation.to_list(length=None)]
            with concurrent.futures.ProcessPoolExecutor() as pool:
                data = await self.bot.loop.run_in_executor(pool, partial(file_from_timestamps, times, group))
            file = BytesIO(data)
            file.seek(0)
            discord_file = discord.File(fp=file, filename="image.png")
            embed = discord.Embed(title=f"Number of times \"{phrase}\" has been said:")
            embed.set_image(url="attachment://image.png")
            await ctx.reply(embed=embed, file=discord_file)

    @commands.command()
    async def leaderboard(self, ctx):
        sent = await ctx.reply(embed=self.bot.create_processing_embed("Generating leaderboard",
                                                                      "Processing messages for leaderboard..."))
        with concurrent.futures.ProcessPoolExecutor() as pool:
            results = await self.bot.loop.run_in_executor(pool, partial(get_guild_score, ctx.guild.id))
        results = results[:12]
        embed = discord.Embed(title="Activity Leaderboard - Past 7 Days", colour=discord.Colour.green())
        embed.description = "```"
        if ctx.guild.id == config.monkey_guild_id:
            embed.set_footer(text="More information about this in #role-assign (monkeys of the week!)")
        else:
            embed.set_footer(text="Most active users this week! Score is based off of your active time this week.")
        lengthening = []
        for index, user in enumerate(results):
            name = await self.name_from_id(user[0], ctx.guild)
            name = unidecode.unidecode(name)
            name_length = len(name)
            lengthening.append(name_length + len(str(index + 1)))
        max_length = max(lengthening)
        for i in range(len(results)):
            name = await self.name_from_id(results[i][0], ctx.guild)
            name = unidecode.unidecode(name)
            text = f"{i + 1}. {name}" + " " * (max_length - lengthening[i]) + f" | Score: {results[i][1]}\n"
            embed.description += text
        embed.description += "```"
        await sent.edit(embed=embed)

    @commands.command()
    async def first_message(self, ctx, member: Optional[discord.Member]):
        async with ctx.typing():
            if member is None:
                member = ctx.author
            first_message = await self.get_first_message(ctx.guild.id, member.id)
            embed = discord.Embed(title=f"{member.display_name}'s first message",
                                  description=first_message.get("content", ""),
                                  colour=discord.Colour.green(),
                                  timestamp=first_message.get("created_at", datetime.datetime(2015, 1, 1)))
            embed.set_footer(text=first_message.get("created_at",
                                                    datetime.datetime(2015, 1, 1)).strftime("%Y-%m-%d %H:%M:%S"))
            embed.set_author(name=member.display_name, icon_url=member.avatar_url)
            await ctx.reply(embed=embed)

    async def get_first_message(self, guild_id, user_id):
        query = self.bot.mongo.discord_db.messages.find({"user_id": user_id, "guild_id": guild_id})
        query.sort("created_at", 1).limit(1)
        first_message = await query.to_list(length=1)
        try:
            return first_message[0]
        except IndexError:
            return {}

    @commands.command()
    async def stats(self, ctx, member: Optional[discord.Member], group: Optional[str] = "m"):
        group = group.lower()
        if member is None:
            member = ctx.author
        if group not in ['d', 'w', 'm', 'y']:
            await ctx.reply(embed=self.bot.create_error_embed("Valid grouping options are d, w, m, y"))
            return
        english_group = {'d': "Day", 'w': "Week", 'm': "Month", 'y': "Year"}
        sent = await ctx.reply(embed=self.bot.create_processing_embed("Processing messages", "Compiling graph for all "
                                                                                             "your messages..."))
        pipeline = [
            {
                "$match": {"guild_id": ctx.guild.id, "user_id": member.id}
            },
            {
                "$project": {"_id": "$created_at"}
            }
        ]
        aggregation = self.bot.mongo.discord_db.messages.aggregate(pipeline)
        times = [x.get("_id") for x in await aggregation.to_list(length=None)]
        with concurrent.futures.ProcessPoolExecutor() as pool:
            data = await self.bot.loop.run_in_executor(pool, partial(file_from_timestamps, times, group))
        file = BytesIO(data)
        file.seek(0)
        discord_file = discord.File(fp=file, filename="image.png")
        embed = discord.Embed(title=f"{member.display_name}'s stats, grouped by {english_group[group]}:")
        embed.set_image(url="attachment://image.png")
        await sent.delete()
        await ctx.reply(embed=embed, file=discord_file)

    @commands.command()
    @is_high_staff()
    async def exclude_channel(self, ctx, channel: Optional[discord.TextChannel]):
        if channel is None:
            channel = ctx.channel
        sent = await ctx.reply(embed=self.bot.create_processing_embed("Excluding channel...",
                                                                      "Sending exclusion request..."))
        channel = await self.bot.mongo.find_by_id(self.bot.mongo.discord_db.channels, channel.id)
        await self.bot.mongo.discord_db.channels.update_one({"_id": channel["_id"]},
                                                            {'$set': {"excluded": not channel.get("excluded", False)}})
        await sent.edit(embed=self.bot.create_completed_embed("Changed excluded status!",
                                                              f"Channel has been "
                                                              f"{'un' if channel.get('excluded', False) else ''}"
                                                              f"excluded!"))

    @commands.command()
    async def server_stats(self, ctx, group: Optional[str] = "m"):
        group = group.lower()
        if group not in ['d', 'w', 'm', 'y']:
            await ctx.reply(embed=self.bot.create_error_embed("Valid grouping options are d, w, m, y"))
            return
        english_group = {'d': "Day", 'w': "Week", 'm': "Month", 'y': "Year"}
        sent = await ctx.reply(embed=self.bot.create_processing_embed("Processing messages",
                                                                      "Fetching all server messages..."))
        pipeline = [
            {
                "$match": {"guild_id": ctx.guild.id}
            },
            {
                "$project": {"_id": "$created_at"}
            }
        ]
        aggregation = self.bot.mongo.discord_db.messages.aggregate(pipeline)
        message_list = [x.get("_id") for x in await aggregation.to_list(length=None)]
        await sent.edit(embed=self.bot.create_processing_embed("Processing messages",
                                                               "Creating graph of all server messages..."))
        with concurrent.futures.ProcessPoolExecutor() as pool:
            raw_data = await self.bot.loop.run_in_executor(pool, partial(file_from_timestamps, message_list, group))
        file = BytesIO(raw_data)
        file.seek(0)
        discord_file = discord.File(fp=file, filename="image.png")
        embed = discord.Embed(title=f"{ctx.guild.name}'s stats, grouped by {english_group[group]}:")
        embed.set_image(url="attachment://image.png")
        await sent.delete()
        await ctx.reply(embed=embed, file=discord_file)

    @commands.group()
    async def transcript(self, ctx):
        if ctx.invoked_subcommand is None:
            await ctx.reply(embed=self.bot.create_error_embed("Invalid subcommand. Valid subcommands: `last`, "
                                                              "`deleted`, `live`"))

    async def get_earliest_time(self, channel, amount):
        message_cursor = self.bot.mongo.discord_db.messages.find({"channel_id": channel.id}).sort(
            "created_at", -1).skip(amount)
        earliest_message_list = await message_cursor.to_list(length=1)
        if len(earliest_message_list) == 0:
            earliest_message_list = await channel.history(oldest_first=True, limit=1).flatten()
            earliest_time = earliest_message_list[0].created_at
        else:
            earliest_time = earliest_message_list[0].get("created_at")
        return earliest_time

    @transcript.command(description="Generates a sharable LIVE transcript (only with others in the chat) "
                                    "of the current channel, including up to [amount] messages ago.")
    async def live(self, ctx, amount: Optional[int] = 25):
        if amount < 1:
            await ctx.reply(embed=self.bot.create_error_embed("Please choose an amount > 1."))
            return
        earliest_time = await self.get_earliest_time(ctx.channel, amount)
        now = datetime.datetime.now()
        before = now + datetime.timedelta(hours=24)
        url = (f"https://utils.thom.club/chat_logs?after={earliest_time.isoformat()}"
               f"&before={before.isoformat()}&channel_id={ctx.channel.id}")
        embed = discord.Embed(title=f"LIVE Chat Transcript for the last {amount} messages and any from this point on, "
                                    f"until 24 hours from now.",
                              url=url, colour=discord.Colour.green())
        await ctx.reply(embed=embed)

    @transcript.command(description="Generates a sharable transcript (only with others in the chat) "
                                    "of the current channel up to [amount] messages ago.")
    async def last(self, ctx, amount: Optional[int] = 25):
        if amount < 1:
            await ctx.reply(embed=self.bot.create_error_embed("Please choose an amount > 1."))
            return
        earliest_time = await self.get_earliest_time(ctx.channel, amount)
        url = (f"https://utils.thom.club/chat_logs?after={earliest_time.isoformat()}"
               f"&before={datetime.datetime.now().isoformat()}&channel_id={ctx.channel.id}")
        embed = discord.Embed(title=f"Chat Transcript for the last {amount} messages.", url=url,
                              colour=discord.Colour.green())
        await ctx.reply(embed=embed)

    @transcript.command(description="Generates a sharable transcript (only with others in the chat) "
                                    "of deleted messages in the current channel up to [amount] deleted messages ago.")
    async def deleted(self, ctx, amount: Optional[int] = 25):
        if amount < 1:
            await ctx.reply(embed=self.bot.create_error_embed("Please choose an amount > 1."))
            return
        message_cursor = self.bot.mongo.discord_db.messages.find({"channel_id": ctx.channel.id, "deleted": True}).sort(
            "created_at", -1).skip(amount)
        earliest_message_list = await message_cursor.to_list(length=1)
        if len(earliest_message_list) == 0:
            earliest_message_list = await ctx.channel.history(oldest_first=True, limit=1).flatten()
            earliest_time = earliest_message_list[0].created_at
        else:
            earliest_time = earliest_message_list[0].get("created_at")
        url = (f"https://utils.thom.club/chat_logs?after={earliest_time.isoformat()}"
               f"&before={datetime.datetime.now().isoformat()}&channel_id={ctx.channel.id}&deleted=1")
        embed = discord.Embed(title=f"Chat Transcript for the last {amount} deleted messages.", url=url,
                              colour=discord.Colour.green())
        await ctx.reply(embed=embed)

    async def name_from_id(self, user_id, guild):
        member = guild.get_member(user_id)
        if member is None:
            member = await self.bot.fetch_user(user_id)
            if member is None:
                name = "Unknown Member"
            else:
                name = member.name
        else:
            name = (member.nick or member.name)
        return name


def setup(bot: UtilsBot):
    cog = Statistics(bot)
    bot.add_cog(cog)

