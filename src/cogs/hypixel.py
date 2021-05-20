import concurrent.futures
import secrets
import traceback
from functools import partial
from typing import Optional

import discord
import mcuuid.api
import mcuuid.tools
from aiohttp import web
from discord.ext import commands, tasks

from src.checks.role_check import is_staff
from src.checks.user_check import is_owner
from src.helpers.graph_helper import plot_stats
from src.helpers.hypixel_helper import *
from src.helpers.hypixel_stats import HypixelStats, create_delta_embeds
from src.helpers.paginator import EmbedPaginator
from src.storage.token import hypixel_token


def equate_uuids(uuid, other_uuid):
    return uuid.replace("-", "") == other_uuid.replace("-", "")


class Hypixel(commands.Cog):
    def __init__(self, bot: UtilsBot):
        self.bot: UtilsBot = bot
        self.hypixel_db = self.bot.mongo.client.hypixel
        self.last_reset = datetime.datetime.now()
        # noinspection PyUnresolvedReferences
        self.hypixel_api = HypixelAPI(self.bot, key=hypixel_token)
        self.update_hypixel_info.add_exception_type(discord.errors.DiscordServerError)
        self.update_hypixel_info.add_exception_type(discord.errors.HTTPException)
        self.update_hypixel_info.start()
        self.user_to_files = {}
        self.token_last_used = {}
        self.latest_tokens = []
        self.head_images = {}
        self.external_ip = None
        self.app = web.Application()
        self.app.add_routes(
            [web.get("/ping", self.website_ping),
             web.get('/{user}-{uid}.png', self.request_image), web.get('/{user}.png', self.request_image),
             web.get('/{user}', self.request_image), web.get('/{user}-{uid}', self.request_image)])
        self.bot.loop.create_task(self.setup_website())
        self.bot.loop.create_task(self.hypixel_api.queue_loop())

    async def setup_website(self):
        """Sets up the website in the bot's loop.
        It would be simpler to do web.run(app), but that creates a new event loop and asyncio hates that
        """
        runner = web.AppRunner(self.app)
        await runner.setup()
        # Internally it is port 2052, but my reverse proxy proxies this to hypixel.thom.club port 80
        self.site = web.TCPSite(runner, "0.0.0.0", 2052)
        self.bot.loop.create_task(self.site.start())
        return  # literally useless return but has previously been useful

    async def shutdown_website(self):
        """Useful for when updating this cog - otherwise the port gets stuck open"""
        await self.site.stop()

    @staticmethod
    async def website_ping(_):
        """Responds to a ping regardless of whether the hypixel part of the API is working - signifies whether the
        bot is actually running."""
        return web.Response(text="Pong!", status=200)

    @staticmethod
    def offline_player(player, experience, user_uuid, threat_index, fkdr):
        """Turns the file into a neater dictionary with known variables,
        so it can be pickled and accessed by the file creation process. """
        return {"name": player.get("displayname"),
                "last_logout": datetime.datetime.fromtimestamp(player.get("lastLogout").timestamp(),
                                                               datetime.timezone.utc),
                "online": False,
                "bedwars_level": get_level_from_xp(experience),
                "bedwars_winstreak": player.get("stats").get("Bedwars", {}).get("winstreak", 0), "uuid": user_uuid,
                "threat_index": threat_index, "fkdr": fkdr, "stats": player["stats"]}

    async def online_player(self, player, experience, user_uuid, threat_index, fkdr):
        """Same as offline_player, but also returns their game, mode and map."""
        status = await self.hypixel_api.get_status(user_uuid)
        # Double checks they're actually online, in case their lastLogout/Login glitched
        if not status.get("online"):
            return self.offline_player(player, experience, user_uuid, threat_index, fkdr)
        return {"name": player.get("displayname"),
                "last_logout": datetime.datetime.fromtimestamp(player.get("lastLogout").timestamp(),
                                                               datetime.timezone.utc),
                "online": True,
                "bedwars_level": get_level_from_xp(experience),
                "bedwars_winstreak": player.get("stats").get("Bedwars", {}).get("winstreak", 0),
                "game": status.get("gameType"),
                "mode": status.get("mode"), "map": status.get("map"), "uuid": user_uuid, "threat_index": threat_index,
                "fkdr": fkdr, "stats": player["stats"]}

    async def get_user_stats(self, user_uuid, prioritize=False):
        """Gets the actual information from hypixel, determines whether the member is online or not, and also fetches
        the member's game-mode and map if they are online.
        :param prioritize: If the request should be prioritized
        :param user_uuid: The uuid of the user.
        :return: A dictionary with known keys which contains information about the player's statistics.
        """
        # Gets raw information from the API via my rate limit abiding queue in hypixel_helper
        player = await self.hypixel_api.get_player(user_uuid, prioritize)
        # They are online if they last logged in after they last logged out
        member_online = bool(player.get("lastLogout") < player.get("lastLogin"))
        experience = player.get("stats").get("Bedwars", {}).get("Experience", 0)
        try:
            # fkdr = bedwars final kills over bedwars final deaths
            fkdr = player.get("stats")['Bedwars']['final_kills_bedwars'] / player.get("stats")['Bedwars'][
                'final_deaths_bedwars']
        # KeyError = they have no final kills or no final deaths
        except KeyError:
            # set it to 0 so i dont get another error in threat index calculation
            fkdr = 0
        # hypixel_helper.py, turns experience into decimal level
        bedwars_level = get_level_from_xp(experience)
        # fkdr = level * fkdr squared, all divided by 10 (thanks statsify)
        threat_index = (bedwars_level * (fkdr ** 2)) / 10
        if member_online:
            return await self.online_player(player, experience, user_uuid, threat_index, fkdr)
        else:
            return self.offline_player(player, experience, user_uuid, threat_index, fkdr)

    async def get_expanded_player(self, user_uuid, pool, reset=False, prioritize=False):
        """

        :param prioritize: Whether to prioritize this request (normally, if it is a user command).
        :param user_uuid: The minecraft uuid of the player in question.
        :param pool: Instance of concurrent.futures.ProcessPoolExecutor
        :param reset: Whether to still update the embeds (later) even if the image hasn't changed
        :return: player dictionary with player["file"] being the generated image.
        """
        player = await self.get_user_stats(user_uuid, prioritize)
        # If the head image has been cached less than 5 mins ago, used the cached version
        if player["uuid"] in self.head_images and (datetime.datetime.now() -
                                                   self.head_images[player["uuid"]][1]).total_seconds() < 300:
            player["head_image"] = self.head_images[player["uuid"]][0]
        else:
            # Else fetch it from cravatar, cache it and use that version
            async with aiohttp.ClientSession() as session:
                async with session.get("http://cravatar.eu/helmavatar/{}/64.png".format(player["uuid"])) as response:
                    head_image = await response.read()
                    self.head_images[player["uuid"]] = (head_image, datetime.datetime.now())
                    player["head_image"] = head_image
        # Run the get_file_for_member function in another process and await its completion
        member_file = await self.bot.loop.run_in_executor(pool, partial(get_file_for_member, player))
        last_file = None
        if not reset:
            # Check whether the image has changed.
            if player["name"].lower() in self.user_to_files:
                last_file = BytesIO(self.user_to_files[player["name"].lower()][0])
            if last_file is None:
                same_file = False
            else:
                same_file = await self.bot.loop.run_in_executor(pool, partial(are_equal, last_file, member_file))
                if same_file:
                    member_file.close()
                    member_file = last_file
                else:
                    last_file.close()
        else:
            # If we're resetting, mark the image to be changed in the embed.
            same_file = False
        player["file"] = member_file.read()
        player["unchanged"] = same_file
        # Remember to close the file since we're only storing the raw bytes.
        member_file.close()
        return player

    @staticmethod
    async def get_user_embed(member):
        """
        Formats an embed for a user. Notably doesn't include the image - just the embed itself without any references
        to the member's image.
        :param member: member dictionary.
        :return: formatted embed with online colour, username, and timestamp of either last update or last online time.
        """
        member_embed = discord.Embed(title=member["name"], color=((discord.Colour.red(),
                                                                   discord.Colour.green())[int(member["online"])]),
                                     timestamp=datetime.datetime.utcnow())
        if not member["online"]:
            member_embed.timestamp = member["last_logout"]
        return member_embed

    async def request_image(self, request: web.Request):
        """
        Called function when hypixel.thom.club/* is called. The routes are defined in the __init__ method of this
        class. This returns (either from cache or by generation) the hypixel image of the user.
        :param request: web request from browser (aiohttp)
        :return: web response to send to the browser
        """
        # The username as specified in the routes.
        username = request.match_info['user']
        # The current time.
        now = datetime.datetime.now()
        # Checks cache for member, if not in cache data is None and last_timestamp is 0.
        data, last_timestamp = self.user_to_files.get(username.lower(), (None, datetime.datetime(1970, 1, 1)))
        # If the user is not cached or the cached version is more than 5 minutes old...
        if data is None or (now - last_timestamp).total_seconds() > 300:
            # Convert username into minecraft uuid
            uuid = await self.uuid_from_identifier(username)
            # Returns 404 (not found) if the minecraft user doesn't exist.
            if uuid is None:
                return web.Response(status=404)
            # Returns 404 if the user has missing or no bedwars stats.
            valid = await self.check_valid_player(uuid)
            if not valid:
                return web.Response(status=404)
            # Calls get_expanded_player to get the player dictionary, with a new ProcessPoolExecutor to put the process
            # in, separate from the embeds so it can execute independently.
            with concurrent.futures.ProcessPoolExecutor() as pool:
                player = await self.get_expanded_player(uuid, pool, True)
            data = player["file"]
            last_timestamp = datetime.datetime.now()
            # Caches the image and timestamp
            self.user_to_files[username.lower()] = (data, last_timestamp)
        response = web.StreamResponse()
        # Specifies it's a png.
        response.content_type = "image/png"
        response.content_length = len(data)
        # Tells browsers to delete from cache after 15 seconds (45 / 3)
        response.headers["Cache-Control"] = "max-age=15"
        # Sends browser headers.
        await response.prepare(request)
        # Sends image.
        await response.write(data)
        # Closes connection.
        return response

    @commands.command(aliases=["hinfo", "hypixelinfo"])
    async def hypixel_info(self, ctx, username: str):
        """Runs the hinfo command.

        Essentially, just sends the bedwars image as a file independent of the webhost."""
        now = datetime.datetime.now()
        async with ctx.typing():
            """Checks cache for file. Can probably be extrapolated into a method, but this replies to the calling
            command with information about why it failed if it does, rather than web status codes.
            
            Read request_image() for more detailed comments. This is essentially that function but as a 
            discord command rather than a webpage."""
            data, last_timestamp = self.user_to_files.get(username.lower(), (None, datetime.datetime(1970, 1, 1)))
            if data is None or (now - last_timestamp).total_seconds() > 300:
                uuid = await self.uuid_from_identifier(username)
                if uuid is None:
                    await ctx.reply(embed=self.bot.create_error_embed("That Minecraft user doesn't exist."))
                    return
                valid = await self.check_valid_player(uuid)
                if not valid:
                    await ctx.reply(embed=self.bot.create_error_embed("That user hasn't played on hypixel. Get them to "
                                                                      "log in (and out!) at least once."))
                    return
                with concurrent.futures.ProcessPoolExecutor() as pool:
                    player = await self.get_expanded_player(uuid, pool, True)
                data = player["file"]
                self.user_to_files[username.lower()] = (data, datetime.datetime.now())
            # Wraps the data (bytes) in file-like object so discord.py can take it as a file.
            file = BytesIO(data)
            discord_file = discord.File(fp=file, filename=f"{username}.png")
            await ctx.reply(file=discord_file)

    @commands.command(pass_context=True)
    @is_staff()
    async def hypixel_channel(self, ctx, channel: Optional[discord.TextChannel]):
        """Allows a user to set up a "Hypixel Channel", for automatic tracking of players.

        These update once every (roughly) 45 seconds, API allowing.
        As more players are tracked, expect that interval to decrease.

        If this bot gets into too many servers,
        this will almost certainly become a premium feature to limit hypixel api load."""
        overwrites = {ctx.guild.default_role: discord.PermissionOverwrite(read_messages=True, send_messages=False),
                      ctx.guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True)}
        if channel is None:
            sent = await self.bot.ask_boolean(ctx, ctx.author, self.bot.create_processing_embed(
                "Confirm", "Are you sure you want to make a NEW hypixel channel?"))
            if not sent:
                return
            channel = await ctx.guild.create_text_channel("hypixel-tracking", overwrites=overwrites)
        else:
            sent = await self.bot.ask_boolean(ctx, ctx.author, self.bot.create_processing_embed(
                "Confirm", "Are you sure you want to make {}  the text channel for hypixel updates? "
                           "\n(THIS DELETES ALL CONTENTS) \nType \"yes\" if you're sure.".format(channel.mention)))
            # If there is no confirmation in 15 seconds, give up.
            if not sent:
                return
            await sent.edit(embed=self.bot.create_processing_embed(
                "Converting {}".format(channel.name), "Deleting all prior messages."))
            async for message in channel.history(limit=None):
                await message.delete()
            await channel.edit(overwrites=overwrites)
        await sent.edit(embed=self.bot.create_processing_embed(
            "Converting {}".format(channel.name), "Completed all prior messages. Adding channel to database."))
        channel_collection = self.hypixel_db.channels
        async for channel in channel_collection.find({"guild_id": ctx.guild.id}):
            await self.delete_channel_from_all_users(channel.get("_id"))
        await channel_collection.delete_many({"guild_id": ctx.guild.id})
        channel_document = {"_id": channel.id, "guild_id": ctx.guild.id}
        await self.bot.mongo.force_insert(channel_collection, channel_document)
        await sent.edit(embed=self.bot.create_completed_embed("Added Channel!",
                                                              "Channel {} added for hypixel info.".format(
                                                                  channel.mention)))

    async def delete_channel_from_all_users(self, channel_id):
        async for player in self.hypixel_db.players.find({"channels": channel_id}):
            channels = player.get("channels")
            channels.remove(channel_id)
            await self.hypixel_db.players.update_one({"_id": player.get("_id")}, {"$set": {"channels": channels}})

    @staticmethod
    async def uuid_from_identifier(identifier):
        """Makes a request to playerdb.co (higher ratelimit than mojang) to get the UUID from a username.
        :param identifier: Either a username or uuid of a player. Could be neither then will return None.
        :return: The player's UUID or None, if it is not a valid UUID.
        """
        try:
            if mcuuid.tools.is_valid_mojang_uuid(identifier):
                return identifier.replace("-", "")
            else:
                async with aiohttp.ClientSession() as session:
                    request = await session.get("https://playerdb.co/api/player/minecraft/" + identifier)
                    if request.status != 200:
                        return None
                    json_response = await request.json()
                    if not json_response.get("success", False):
                        return None
                    prospective_uuid = json_response.get("data", {}).get("player", {}).get("id", None)
                    if prospective_uuid is not None:
                        return prospective_uuid.replace("-", "")
                    return prospective_uuid
        except AttributeError:
            return None

    @staticmethod
    async def username_from_uuid(uuid):
        """Returns a username from a UUID.
        :param uuid: A player's UUID. If not, it will return None if valid form or "Unknown Player" if not.
        :return: The player's username.
        """
        stripped_uuid = uuid.replace("-", "")
        if not mcuuid.tools.is_valid_mojang_uuid(stripped_uuid):
            return "Unknown Player"
        async with aiohttp.ClientSession() as session:
            request = await session.get("https://playerdb.co/api/player/minecraft/" + uuid)
            if request.status != 200:
                return None
            json_response = await request.json()
            if not json_response.get("success", False):
                return None
            username = json_response.get("data", {}).get("player", {}).get("username", "Unknown Player")
        return username

    async def check_valid_player(self, uuid, prioritize=False):
        """Checks whether the player is a valid hypixel player by seeing if all keys needed to generate a player
        file are present in their stats. This will return False for people with API hidden.
        :param prioritize: If the lookup should be prioritized
        :param uuid: The player to check.
        :return: True if they are a valid BedWars player, False if not or undetermined.
        """
        try:
            # noinspection PyUnboundLocalVariable
            await self.get_user_stats(uuid, prioritize)
        except (TypeError, KeyError):
            return False
        return True

    @commands.command(pass_context=True, name="add", description="Adds a player to your server's hypixel channel!",
                      aliases=["hadd", "hypixel_add", "hypixeladd"])
    @is_staff()
    async def add(self, ctx, username: str):
        """Adds a user to the server's hypixel info channel to be updated regularly.

        This is currently a JSON file, but I'll move it to a DB when I get a database solution working
        that doesn't freeze the whole python process even when it's in a different thread."""
        async with ctx.typing():
            uuid = await self.uuid_from_identifier(username)
            if uuid is None:
                await ctx.reply(embed=self.bot.create_error_embed("Invalid username or uuid {}!".format(username)),
                                delete_after=10)
                await ctx.message.delete()
                return
            valid = await self.check_valid_player(uuid, prioritize=True)
            if not valid:
                await ctx.reply(embed=self.bot.create_error_embed("That user is not a valid hypixel player. "
                                                                  "Get them to log in (and out!) first!"))
                return
            channel_collection = self.hypixel_db.channels
            async for channel in channel_collection.find({"guild_id": ctx.guild.id}):
                player = await self.hypixel_db.players.find_one({"_id": uuid})
                if player is not None and channel.get("_id") in player.get("channels", []):
                    await ctx.reply(embed=self.bot.create_error_embed("Player already in channel! \n"
                                                                      "It can take a while for the channel to update "
                                                                      "after adding a player, so please wait a little "
                                                                      "longer :)"))
                    return
                if player is None:
                    player = {"_id": uuid, "tracked": False}
                channels = player.get("channels", [])
                channels.append(channel.get("_id"))
                player["channels"] = channels
                await self.bot.mongo.force_insert(self.hypixel_db.players, player)
                await ctx.reply(embed=self.bot.create_completed_embed("User Added!",
                                                                      "User {} has been added to {}.".format(
                                                                          await self.username_from_uuid(uuid),
                                                                          f"<#{channel.get('_id')}>")))
                return
            await ctx.reply(embed=self.bot.create_error_embed("You don't have a hypixel info channel in this guild.\n"
                                                              "Please create one before adding players."))

    @commands.command(pass_context=True, name="remove", description="Removes a player from your server's "
                                                                    "hypixel channel!",
                      aliases=["hremove", "hypixel_remove", "hypixelremove"])
    @is_staff()
    async def remove(self, ctx, username: str):
        """Removes a user from the server's hypixel info channel so they won't be updated regularly, at least not
        in that channel anymore.

        This is currently a JSON file, but I'll move it to a DB when I get a database solution working
        that doesn't freeze the whole python process even when it's in a different thread."""
        async with ctx.typing():
            uuid = await self.uuid_from_identifier(username)
            if uuid is None:
                await ctx.reply(embed=self.bot.create_error_embed("Invalid username or uuid {}!".format(username)),
                                delete_after=10)
                await ctx.message.delete()
                return
            channel_collection = self.hypixel_db.channels
            async for channel in channel_collection.find({"guild_id": ctx.guild.id}):
                player = await self.hypixel_db.players.find_one({"_id": uuid})
                if player is None:
                    await ctx.reply(embed=self.bot.create_error_embed("That player is not in your hypixel channel!"))
                    return
                channels = player.get("channels")
                if channel.get("_id") not in channels:
                    await ctx.reply(embed=self.bot.create_error_embed("That player is not in your hypixel channel!"))
                    return
                channels.remove(channel.get("_id"))
                await self.hypixel_db.players.update_one({"_id": player.get("_id")}, {"$set": {"channels": channels}})
                await ctx.reply(embed=self.bot.create_completed_embed(
                    "User Removed!", "User {} has been removed from {}.".format(
                        await self.username_from_uuid(uuid),
                        f"<#{channel.get('_id')}>")))
                return
            await ctx.reply(embed=self.bot.create_error_embed("You don't have a hypixel channel!"))

    async def send_embeds(self, channel_id, our_members):
        """Sends all embeds to a channel that the channel is requesting.
        :param channel_id: The channel id in question
        :param our_members: The member dictionaries that the channel is requesting
        """
        i = 0
        try:
            channel = await self.bot.fetch_channel(channel_id)
        except discord.errors.NotFound:
            channel = None
        if channel is None:
            await self.hypixel_db.channels.delete_many({"_id": channel_id})
            await self.delete_channel_from_all_users(channel_id)
            return
        history = await channel.history(limit=None, oldest_first=True).flatten()
        editable_messages = [message for message in history if message.author == self.bot.user]
        member_files = [member["file"] for member in our_members]
        if (len(editable_messages) != len(our_members) or
                len([message for message in editable_messages if len(message.embeds) == 1]) != len(our_members)):
            await channel.purge(limit=None)
            new_messages = True
        else:
            new_messages = False
        for member, file in zip(our_members, member_files):
            self.user_to_files[member["name"].lower()] = (file, datetime.datetime.now())
            token = secrets.token_urlsafe(6).replace("-", "")
            embed = await self.get_user_embed(member)
            embed.set_image(url="https://hypixel.thom.club/{}-{}.png".format(member["name"], token))
            if new_messages:
                await channel.send(embed=embed)
            else:
                embed_member_name = editable_messages[i].embeds[0].title
                if embed_member_name != member["name"] or not member["unchanged"]:
                    await editable_messages[i].edit(embed=embed)
                i += 1

    async def get_with_storage(self, player_dictionary, pool, reset):
        player_data = await self.get_expanded_player(player_dictionary.get("_id"), pool, reset)
        if player_dictionary.get("tracked", False):
            stats = player_data.get("stats")
            bedwars = stats.get("Bedwars")
            uuid = player_data.get("uuid")
            try:
                hypixel_stats = HypixelStats.from_stats(bedwars)
            except KeyError:
                print(f"Stats attempted to add for player {player_data.get('name')} but there were none.")
                return player_data
            last_document_query = self.hypixel_db.statistics.find({"uuid": uuid}).sort("timestamp", -1).limit(1)
            last_document_list = await last_document_query.to_list(length=1)
            if len(last_document_list) != 0:
                last_document = last_document_list[0]
                last_stats_dict = last_document["stats"]
                last_stats = HypixelStats.from_dict(last_stats_dict)
                if last_stats.games_played == hypixel_stats.games_played:
                    return player_data
            player_document = {"uuid": uuid, "stats": hypixel_stats.to_dict(),
                               "timestamp": datetime.datetime.now()}
            await self.hypixel_db.statistics.insert_one(player_document)
        return player_data

    @commands.command()
    @is_owner()
    async def track_player(self, ctx, username: str):
        async with ctx.typing():
            uuid = await self.uuid_from_identifier(username)
            if uuid is None:
                await ctx.reply(embed=self.bot.create_error_embed("Invalid username or uuid {}!".format(username)),
                                delete_after=10)
                await ctx.message.delete()
                return
            tracked_player = await self.hypixel_db.players.find_one({"_id": uuid})
            if tracked_player is None or not tracked_player.get("tracked", False):
                if not await self.check_valid_player(uuid, True):
                    await ctx.reply(embed=self.bot.create_error_embed("That player has never played on Hypixel. "
                                                                      "Get them to log in and out at least once!"))
                    return
                await self.hypixel_db.players.update_one({"_id": uuid}, {"$set": {"tracked": True}}, upsert=True)
                await ctx.reply(embed=self.bot.create_completed_embed("Tracking Player!",
                                                                      "Added player to tracking."))
            else:
                await self.hypixel_db.players.update_one({"_id": uuid}, {"$set": {"tracked": False}}, upsert=True)
                await ctx.reply(embed=self.bot.create_completed_embed("Not Tracking Player!",
                                                                      "Removed player from tracking."))

    @tasks.loop(seconds=45, count=None)
    async def update_hypixel_info(self):
        """Constant task loop that updates all the hypixel channels with the new member info."""
        try:
            # Gets a list of players.
            players_query = self.hypixel_db.players.find()
            all_players = await players_query.to_list(length=None)
            now = datetime.datetime.now()
            # Completely refresh the embeds every 3 minutes. Just so last update time isn't more than 3 mins ago.
            reset = (now - self.last_reset).total_seconds() > 180
            # Fetches hypixel data in the main thread, then
            # runs a pool of processes (machine core count simultaneously) to generate the player images.
            member_futures = []
            if reset:
                self.last_reset = datetime.datetime.now()
            with concurrent.futures.ProcessPoolExecutor() as pool:
                for player_dict in all_players:
                    member_futures.append(self.bot.loop.create_task(self.get_with_storage(player_dict, pool,
                                                                                          reset)))
                member_dicts = await asyncio.gather(*member_futures)
            # Sort offline members before online members, regardless of threat index.
            offline_members = [member for member in member_dicts if not member["online"]]
            online_members = [member for member in member_dicts if member["online"]]
            offline_members.sort(key=lambda x: float(x["threat_index"]))
            online_members.sort(key=lambda x: float(x["threat_index"]))
            member_dicts = offline_members + online_members
            # Runs send_embeds task for all known hypixel channels.
            pending_tasks = []
            all_channel_ids = await self.hypixel_db.players.distinct("channels")
            for channel_id in all_channel_ids:
                find_query = self.hypixel_db.players.find({"channels": channel_id})
                channel_uuids = [x.get("_id") for x in await find_query.to_list(length=None)]
                channel_members = []
                for member in member_dicts:
                    if member["uuid"] in channel_uuids:
                        channel_members.append(member)
                pending_tasks.append(self.bot.loop.create_task(
                    self.send_embeds(channel_id, channel_members)))
            # Runs them simultaneously so we can send/edit (5 * channel_count) messages at once rather than just 5 at
            # a time (very slow)
            await asyncio.gather(*pending_tasks)
        # Bad practice, but catches ALL errors here since we don't want this to stop for all channels,
        # even in case of error.
        except Exception as e:
            print("hypixel error")
            print(e)
            print(traceback.format_exc())

    @commands.Cog.listener()
    async def on_message(self, message):
        """Keeps the hypixel channels clear of all messages except the bot's, otherwise it
        would have to clear the channel every time someone sent a message (it still does if it's bad timing)."""
        if message.author == self.bot.user:
            return
        if message.channel.id in await self.hypixel_db.players.distinct("channels"):
            await message.delete()

    @commands.group(aliases=["hstats"])
    async def hypixel_stats(self, ctx):
        if ctx.invoked_subcommand is None:
            await ctx.reply(embed=self.bot.create_error_embed("Invalid format! "
                                                              "Please specify a date or statistic."))

    async def get_stats_from_before(self, uuid, timedelta: datetime.timedelta):
        before = datetime.datetime.now() - timedelta
        earlier_document_query = self.hypixel_db.statistics.find({"uuid": uuid,
                                                                  "timestamp": {"$lt": before}}).sort(
            "timestamp", -1).limit(1)
        earlier_document_list = await earlier_document_query.to_list(length=1)
        return earlier_document_list[0] if len(earlier_document_list) != 0 else None

    async def get_most_recent_stats(self, uuid):
        last_document_query = self.hypixel_db.statistics.find({"uuid": uuid}).sort("timestamp", -1).limit(1)
        last_document_list = await last_document_query.to_list(length=None)
        return last_document_list[0] if len(last_document_list) != 0 else None

    @hypixel_stats.command()
    async def daily(self, ctx, username: str):
        async with ctx.typing():
            uuid = await self.uuid_from_identifier(username)
            if uuid is None:
                await ctx.reply(embed=self.bot.create_error_embed("Invalid username or uuid {}!".format(username)),
                                delete_after=10)
                await ctx.message.delete()
                return
            yesterday = datetime.datetime.now() - datetime.timedelta(hours=24)
            last_document = await self.get_most_recent_stats(uuid)
            if last_document is None:
                await ctx.reply(embed=self.bot.create_error_embed("That player is not being tracked."))
                return
            if last_document.get("timestamp") < yesterday:
                await ctx.reply(embed=self.bot.create_error_embed(f"{username} has not played Bedwars today."))
                return
            earlier_document = await self.get_stats_from_before(uuid, datetime.timedelta(hours=24))
            if earlier_document is None:
                await ctx.reply(embed=self.bot.create_error_embed("I don't have statistics for that player from before "
                                                                  "today!"))
                return
            today_stats = HypixelStats.from_dict(last_document["stats"])
            yesterday_stats = HypixelStats.from_dict(earlier_document["stats"])
            todays_date_string = datetime.datetime.now().strftime("%A, %B %d %Y")
            all_embeds = create_delta_embeds(f"{username}'s Stats - {todays_date_string}", yesterday_stats, today_stats)
            paginator = EmbedPaginator(self.bot, None, all_embeds, ctx)
            await paginator.start()

    async def get_game_stats(self, ctx, username, num_games):
        uuid = await self.uuid_from_identifier(username)
        if uuid is None:
            await ctx.reply(embed=self.bot.create_error_embed("Invalid username or uuid {}!".format(username)),
                            delete_after=10)
            await ctx.message.delete()
            return
        document_query = self.hypixel_db.statistics.find({"uuid": uuid}).sort("timestamp", -1).limit(num_games)
        all_documents = await document_query.to_list(length=None)
        if len(all_documents) == 0:
            await ctx.reply(embed=self.bot.create_error_embed("That player is not being tracked."))
            return
        # Oldest -> Newest list of HypixelStats objects, each representing stats after a game.
        all_stats = [HypixelStats.from_dict(x.get("stats")) for x in all_documents[::-1]]
        return all_stats

    async def graph_stats(self, ctx, username, num_games, attribute, nice_name):
        all_stats = await self.get_game_stats(ctx, username, num_games)
        if all_stats is None:
            return
        all_important = [getattr(x, attribute) for x in all_stats]
        with concurrent.futures.ProcessPoolExecutor() as pool:
            data = await self.bot.loop.run_in_executor(pool, partial(plot_stats, all_important, x_label="Games",
                                                                     y_label=nice_name))
        file = BytesIO(data)
        discord_file = discord.File(file, filename="image.png")
        embed = discord.Embed(title=f"{username}'s {nice_name} over the last {len(all_important)} games")
        embed.set_image(url="attachment://image.png")
        await ctx.reply(embed=embed, file=discord_file)

    internal_names = {"fkdr": "fkdr", "finals": "total_kills", "kills": "total_kills", "deaths": "total_deaths",
                      "beds_broken": "beds_broken", "brokenbeds": "beds_broken", "bedsdestroyed": "beds_broken",
                      "beds_destroyed": "beds_broken", "beds_lost": "beds_lost", "bedslost": "beds_lost",
                      "bblr": "bblr", "level": "level", "xp": "level", "wins": "wins", "losses": "losses",
                      "fails": "losses", "winrate": "win_rate", "win_rate": "win_rate", "wr": "win_rate",
                      "ti": "threat_index", "threat_index": "threat_index", "threatindex": "threat_index"}
    pretty_names = {"fkdr": "FKDR", "total_kills": "Final Kills", "total_deaths": "Final Deaths",
                    "beds_broken": "Beds Broken", "beds_lost": "Beds Lost", "bblr": "Bed Break/Loss Ratio",
                    "level": "Level", "wins": "Wins", "losses": "Losses", "win_rate": "Win Rate",
                    "threat_index": "Threat Index"}

    @hypixel_stats.command(name="fkdr", aliases=["finals", "kills", "deaths", "beds_broken", "brokenbeds",
                                                 "bedsdestroyed", "beds_destroyed", "beds_lost", "bedslost", "bblr",
                                                 "level", "xp", "wins", "losses", "winrate", "win_rate", "wr", "ti",
                                                 "threat_index", "threatindex"])
    async def graph_statistic_command(self, ctx, username: str, num_games: int = 25):
        invoking_name = ctx.invoked_with
        attribute_name = self.internal_names[invoking_name]
        pretty_name = self.pretty_names[attribute_name]
        async with ctx.typing():
            await self.graph_stats(ctx, username, num_games, attribute_name, pretty_name)

    @hypixel_stats.group()
    async def predict(self, ctx):
        if ctx.invoked_subcommand is None:
            await ctx.reply(embed=self.bot.create_error_embed("Invalid format! "
                                                              "Please specify a stat to predict!"))

    async def predict_games(self, ctx, username, amount, attribute, attribute_name):
        all_stats = await self.get_game_stats(ctx, username, 100)
        if all_stats is None:
            return
        elif len(all_stats) == 1:
            await ctx.reply(embed=self.bot.create_error_embed("I can't extrapolate your data. I have only tracked "
                                                              "one game!"))
            return
        all_important = [getattr(x, attribute) for x in all_stats]
        if attribute == "threat_index":
            with concurrent.futures.ProcessPoolExecutor() as pool:
                games_estimated = await self.bot.loop.run_in_executor(pool, partial(extrapolate_threat_index,
                                                                                    all_important, amount))
        else:
            first = all_important[0]
            last = all_important[-1]
            average_change_per_game = (last - first) / len(all_important)
            change_required = amount - last
            games_estimated = (round(change_required / average_change_per_game, 2) if average_change_per_game != 0 else
                               float("inf"))
        append = ("(if the estimate is negative, I predict you will never get there!)" if games_estimated < 0
                  else "")
        if games_estimated == float("inf"):
            games_estimated = str("Infinite")
        else:
            games_estimated = str(games_estimated)
        await ctx.reply(embed=self.bot.create_completed_embed(f"{games_estimated} Games Remaining",
                                                              f"Based on your last {len(all_important)} games, \n"
                                                              f"I predict it will take roughly **{games_estimated}** "
                                                              f"games for your {attribute_name} to be {amount}!\n\n" +
                                                              append))

    @predict.command(name="fkdr", aliases=["finals", "kills", "deaths", "beds_broken", "brokenbeds",
                                           "bedsdestroyed", "beds_destroyed", "beds_lost", "bedslost", "bblr",
                                           "level", "xp", "wins", "losses", "winrate", "win_rate", "wr", "ti",
                                           "threat_index", "threatindex"])
    async def predict_statistic(self, ctx, username: str, amount: float):
        invoking_name = ctx.invoked_with
        attribute_name = self.internal_names[invoking_name]
        pretty_name = self.pretty_names[attribute_name]
        async with ctx.typing():
            await self.predict_games(ctx, username, amount, attribute_name, pretty_name)


def setup(bot):
    cog = Hypixel(bot)
    bot.add_cog(cog)


def teardown(bot):
    cog = bot.get_cog("Hypixel")
    bot.loop.create_task(cog.shutdown_website())
