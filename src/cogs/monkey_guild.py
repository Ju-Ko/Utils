import discord
import datetime

from src.storage import config, messages
from discord.ext import commands
from main import UtilsBot
from src.checks.role_check import is_staff
from src.checks.user_check import is_owner
from src.checks.guild_check import monkey_check
from src.helpers.storage_helper import DataHelper


class Monkey(commands.Cog):
    def __init__(self, bot: UtilsBot):
        self.bot: UtilsBot = bot
        self.july = datetime.datetime(2020, 7, 1, tzinfo=datetime.timezone.utc)

    def is_og(self, member: discord.Member):
        first_join_date = member.joined_at
        # noinspection SpellCheckingInspection
        first_join_date = first_join_date.replace(tzinfo=datetime.timezone.utc)
        return first_join_date < self.july

    @commands.command(pass_context=True)
    @monkey_check()
    async def check_og(self, ctx, member: discord.Member = None):
        if member is None:
            member = ctx.message.author
        is_og = self.is_og(member)
        message_time = None
        data = DataHelper()
        if str(member.id) in data.get("og_messages", {}).keys():
            is_og = True
            message_time = datetime.datetime.utcfromtimestamp(data.get("og_messages", {}).get(str(member.id), 0))
        embed = discord.Embed(title="OG Check")
        embed.set_author(name=member.name, icon_url=member.avatar_url)
        embed.description = "{} Member {} OG".format(("❌", "✅")[int(is_og)], ("is not", "is")[int(is_og)])
        embed.colour = (discord.Colour.red(), discord.Colour.green())[int(is_og)]
        embed.timestamp = member.joined_at
        if message_time is not None:
            embed.timestamp = message_time
            embed.add_field(name="First Message", value=message_time.strftime("%Y-%m-%d %H:%M"))
        if self.bot.latest_joins == {}:
            await self.bot.get_latest_joins()
        members = self.bot.latest_joins[ctx.guild.id]
        members = [user.id for user in members]
        embed.add_field(name="Position", value="#{}".format(str(members.index(member.id) + 1)))
        await ctx.reply(embed=embed)

    @commands.command(pass_context=True)
    @is_owner()
    @monkey_check()
    async def all_ogs(self, ctx):
        message_member_ids = {}
        start_embed = discord.Embed(title="Doing all OGs.", description="I will now start to process all messages "
                                                                        "until the 1st of July.",
                                    colour=discord.Colour.orange())
        processing_message = await ctx.reply(embed=start_embed)
        last_edit = datetime.datetime.now()
        for channel in ctx.guild.text_channels:
            async for message in channel.history(limit=None, before=self.july.replace(tzinfo=None), oldest_first=True):
                author = message.author
                if (datetime.datetime.now() - last_edit).total_seconds() > 1:
                    embed = discord.Embed(title="Processing messages",
                                          description="Last Message text: {}, from {}, in {}".format(
                                              message.clean_content, message.created_at.strftime("%Y-%m-%d %H:%M"),
                                              channel.mention), colour=discord.Colour.orange())
                    embed.set_author(name=author.name, icon_url=author.avatar_url)
                    embed.timestamp = message.created_at
                    await processing_message.edit(embed=embed)
                    last_edit = datetime.datetime.now()
                if str(author.id) not in message_member_ids.keys():
                    join_date = message.created_at.replace(tzinfo=datetime.timezone.utc)
                    message_member_ids[str(author.id)] = join_date.timestamp()
        messages_done = discord.Embed(title="Finished Messages.", description="Finished messages. I will now start to "
                                                                              "apply the OG role to all deserving "
                                                                              "users.", colour=discord.Colour.orange())
        await processing_message.edit(embed=messages_done)
        last_edit = datetime.datetime.now()
        og_role = ctx.guild.get_role(795873287628128306)
        members_processed = 0
        async for member in ctx.guild.fetch_members(limit=None):
            has_message = str(member.id) in message_member_ids.keys()
            if has_message:
                message_creation_date = datetime.datetime.fromtimestamp(message_member_ids[str(member.id)],
                                                                        datetime.timezone.utc)
                message_creation_message = message_creation_date.strftime("%Y-%m-%d %H:%M")
                join_time = min(message_creation_date,
                                member.joined_at.replace(tzinfo=datetime.timezone.utc))
            else:
                message_creation_message = "No"
                join_time = member.joined_at.replace(tzinfo=datetime.timezone.utc)
            is_og = join_time < self.july
            if (datetime.datetime.now() - last_edit).total_seconds() > 1:
                embed = discord.Embed(title="Processing Members...",
                                      description="Last Member: {}. "
                                                  "Joined at: {}. "
                                                  "Has previous message: {}."
                                                  "Is OG: {}".format(member.name,
                                                                     member.joined_at.strftime("%Y-%m-%d %H:%M"),
                                                                     message_creation_message,
                                                                     is_og))
                embed.set_author(name=member.name, icon_url=member.avatar_url)
                embed.timestamp = join_time.replace(tzinfo=None)
                await processing_message.edit(embed=embed)
                last_edit = datetime.datetime.now()
            if is_og:
                try:
                    await member.add_roles(og_role)
                except Exception as e:
                    print(e)
                members_processed += 1
        embed = self.bot.create_completed_embed("Completed OG addition", "Successfully added all OGs!")
        await processing_message.edit(embed=embed)
        data = DataHelper()
        data["og_messages"] = message_member_ids

    @commands.command(pass_context=True)
    @is_owner()
    @monkey_check()
    async def test(self, ctx, channel: discord.TextChannel):
        async for message in channel.history(limit=1, oldest_first=True):
            embed = discord.Embed(description=message.clean_content)
            embed.set_author(name=message.author.name, icon_url=message.author.avatar_url)
            await ctx.reply(embed=embed)
            return


def setup(bot):
    cog = Monkey(bot)
    bot.add_cog(cog)