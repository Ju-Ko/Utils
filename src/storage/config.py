import os
import src.storage.token as token

version_number = "1.29.0dev2"

bot_prefix = "!"
description = "Discord Utility Bot"
owner_id = 230778630597246983
power_id = 739600091480064010
zex_id = 734597893624692778
lexi_id = 280843294508974103
lexibot_id = 730015197980262424

extensions = ["suggestions", "restart", "audit", "purge", "misc", "manage_command", "text_to_speech", "monkey_guild",
              "hypixel", "api", "og_checker", "games", "blacklist"]

dev = False
if dev:
    token.token = token.dev_token
    extensions.remove("suggestions")
    extensions.remove("games")

monkey_guild_id = 725886999646437407
apollo_guild_id = 770972021487304714
error_channel_id = 795057163768037376
staff_role_id = 726453086331338802
mod_role_id = 725894916839964682
mod_dev_role_id = 759431719748501575
mod_god_role_id = 750406434180563055
dep_mod_role_id = 740871749956009984
head_mod_role_id = 727294744777982003
lexi_role_id = 725895255198531675
high_staff = [mod_role_id, mod_dev_role_id, mod_god_role_id, dep_mod_role_id, head_mod_role_id, lexi_role_id]

# Settings for the Suggestions cog
suggestions_channel_id = 798972358878167080
archive_channel_id = 725920625956225055
# suggestions_decisions_id = 727563806762598450
suggestions_decisions_id = 798972358878167080
counting_channel_id = 773952078404911123

# Settings for audit/general reactions cog
fast_forward_emoji = u"\u23E9"
rewind_emoji = u"\u23EA"
robot_emoji = u"\U0001F916"
computer_emoji = u"\U0001F4BB"
forward_arrow = u"\u25B6"
backwards_arrow = u"\u25C0"
both_arrow = u"\u2194"
heart_emoji = u"\u2764"
discord_emoji = "<:discord:784309400524292117>"
c4_yellow = "<:c4yellow:806008963803381810>"
c4_red = "<:c4red:806009125497864204>"
c4_none = ":black_large_square:"


chess_difficulties = {"easiest": 0.1,
                      "easier": 0.25,
                      "easy": 0.5,
                      "medium": 3,
                      "hard": 10,
                      "grandmaster": 30}


# Settings for purge
purge_max = 40
purge_all = -1  # DO NOT CHANGE THIS FOR FEAR OF DEATH
confirm_amount = 10

data_path = os.path.join(os.getcwd(), "data.json")  # src/storage/data.json
