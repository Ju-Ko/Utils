import asyncio
from functools import partial
from io import BytesIO
from statistics import mean

import discord
import gc
from discord.ext import commands
from concurrent.futures import ProcessPoolExecutor

from main import UtilsBot
from src.helpers.graph_helper import plot_multiple


class Skyblock(commands.Cog):
    def __init__(self, bot: UtilsBot):
        self.bot: UtilsBot = bot
        self.skyblock_db = self.bot.mongo.client.skyblock

    @commands.group(case_insensitive=True)
    async def skyblock(self, ctx):
        if ctx.invoked_subcommand is None:
            await ctx.reply(embed=self.bot.create_error_embed("Invalid format! "
                                                              "Please specify a subcommand. Valid "
                                                              "subcommands: `history`, `average`, `minimum`, `book`"))

    @skyblock.group(case_insensitive=True)
    async def book(self, ctx):
        if ctx.invoked_subcommand is None:
            await ctx.reply(embed=self.bot.create_error_embed("Invalid format! "
                                                              "Please specify a subcommand. Valid "
                                                              "subcommands: `history`, `average`, `minimum`"))

    @book.command(name="history")
    async def book_history(self, ctx, *, query):
        query = query.lower()
        async with ctx.typing():
            minimum_prices = []
            average_prices = []
            maximum_prices = []
            for timestamp, all_auctions in await self.get_bin_auctions(query, book=True):
                gc.collect()
                known_auctions = [x.get("starting_bid") for x in all_auctions]
                if len(known_auctions) == 0:
                    print("no known auctions")
                    continue
                minimum_prices.append((timestamp, min(known_auctions)))
                average_prices.append((timestamp, mean(known_auctions)))
                maximum_prices.append((timestamp, max(known_auctions)))
            print(len(minimum_prices))
            with ProcessPoolExecutor() as pool:
                data = await self.bot.loop.run_in_executor(pool, partial(plot_multiple,
                                                                         title=f"Prices for {query} books",
                                                                         x_label="Date",
                                                                         y_label="Price in coins",
                                                                         Minimum=minimum_prices,
                                                                         Average=average_prices,
                                                                         Maximum=maximum_prices))
            file = BytesIO(data)
            file.seek(0)
            discord_file = discord.File(fp=file, filename="image.png")
            await ctx.reply(file=discord_file)

    async def auctions_from_parent(self, auction, query, item_lore=None):
        pipeline = [
            {
                "$match": {
                    "_id.auction_id": auction["_id"]
                }
            },
            {
                "$project": {
                    "_id": 0,
                    "auctions": 1
                }
            },
            {
                "$unwind": "$auctions"
            },
            {
                "$replaceWith": "$auctions"
            }
        ]
        final_match = {
            "$match": {
                "bin": True,
                "item_name": {
                    "$regex": f".*{query}.*",
                    "$options": 'i'
                }
            }
        }
        if item_lore is not None:
            final_match = {
                "$match": {
                    "bin": True,
                    "item_name": {
                        "$regex": f".*{query}.*",
                        "$options": 'i'
                    },
                    "item_lore": {
                        "$regex": f".*{item_lore}.*",
                        "$options": 'i'
                    }
                }
            }
        pipeline.append(final_match)
        auctions = await self.skyblock_db.auction_pages.aggregate(pipeline=pipeline).to_list(length=None)
        return auction["timestamp"], auctions

    async def get_bin_auctions(self, query, book=False):
        coroutines = []
        futures = []
        if book:
            query = "enchanted book"
            lore_query = query
            async for auction in self.skyblock_db.auctions.find().sort("timestamp", 1):
                coroutines.append(self.auctions_from_parent(auction, query, item_lore=lore_query))
        else:
            async for auction in self.skyblock_db.auctions.find().sort("timestamp", 1):
                coroutines.append(self.auctions_from_parent(auction, query))
        for coroutine in coroutines:
            futures.append(self.bot.loop.create_task(coroutine))
        results = await asyncio.gather(*futures)
        return results

    @skyblock.command()
    async def history(self, ctx, *, query):
        async with ctx.typing():
            minimum_prices = []
            average_prices = []
            maximum_prices = []
            for timestamp, all_auctions in await self.get_bin_auctions(query.lower()):
                gc.collect()
                known_auctions = [x.get("starting_bid") for x in all_auctions]
                if len(known_auctions) == 0:
                    continue
                minimum_prices.append((timestamp, min(known_auctions)))
                average_prices.append((timestamp, mean(known_auctions)))
                maximum_prices.append((timestamp, max(known_auctions)))
            with ProcessPoolExecutor() as pool:
                data = await self.bot.loop.run_in_executor(pool, partial(plot_multiple,
                                                                         title=f"Prices for {query}",
                                                                         x_label="Date",
                                                                         y_label="Price in coins",
                                                                         Minimum=minimum_prices,
                                                                         Average=average_prices,
                                                                         Maximum=maximum_prices))
            file = BytesIO(data)
            file.seek(0)
            discord_file = discord.File(fp=file, filename="image.png")
            await ctx.reply(file=discord_file)

    @skyblock.command()
    async def average(self, ctx, *, query):
        async with ctx.typing():
            minimum_prices = []
            average_prices = []
            for timestamp, all_auctions in await self.get_bin_auctions(query.lower()):
                gc.collect()
                known_auctions = [x.get("starting_bid") for x in all_auctions]
                if len(known_auctions) == 0:
                    continue
                minimum_prices.append((timestamp, min(known_auctions)))
                average_prices.append((timestamp, mean(known_auctions)))
            with ProcessPoolExecutor() as pool:
                data = await self.bot.loop.run_in_executor(pool, partial(plot_multiple,
                                                                         title=f"Average prices for {query}",
                                                                         x_label="Date",
                                                                         y_label="Price in coins",
                                                                         Minimum=minimum_prices,
                                                                         Average=average_prices))
            file = BytesIO(data)
            file.seek(0)
            discord_file = discord.File(fp=file, filename="image.png")
            await ctx.reply(file=discord_file)

    @skyblock.command()
    async def minimum(self, ctx, *, query):
        async with ctx.typing():
            minimum_prices = []
            for timestamp, all_auctions in await self.get_bin_auctions(query.lower()):
                gc.collect()
                known_auctions = [x.get("starting_bid") for x in all_auctions]
                if len(known_auctions) == 0:
                    continue
                minimum_prices.append((timestamp, min(known_auctions)))
            with ProcessPoolExecutor() as pool:
                data = await self.bot.loop.run_in_executor(pool, partial(plot_multiple,
                                                                         title=f"Minimum prices for {query}",
                                                                         x_label="Date",
                                                                         y_label="Price in coins",
                                                                         Minimum=minimum_prices))
            file = BytesIO(data)
            file.seek(0)
            discord_file = discord.File(fp=file, filename="image.png")
            await ctx.reply(file=discord_file)


def setup(bot):
    cog = Skyblock(bot)
    bot.add_cog(cog)
