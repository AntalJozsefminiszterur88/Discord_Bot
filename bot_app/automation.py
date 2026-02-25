from bot_app.core import *
from bot_app.alerts import start_internal_server
from bot_app.scheduler_web import load_scheduled_messages, scheduled_message_dispatch_loop
import bot_app.core as core


def is_timestamp_line(line: str) -> bool:
    return bool(QUOTE_TIMESTAMP_REGEX.match(line))


def load_quotes() -> list[str]:
    if not os.path.exists(QUOTES_FILE_PATH):
        print(f"Quotes file not found: {QUOTES_FILE_PATH}")
        return []

    quotes = []
    expecting_quote = False
    with open(QUOTES_FILE_PATH, "r", encoding="utf-8") as quote_file:
        for raw_line in quote_file:
            line = raw_line.strip()
            if is_timestamp_line(line):
                expecting_quote = True
                continue
            if not expecting_quote:
                continue
            if not line or line in {"{Attachments}", "{Reactions}"}:
                continue
            if line.lower().startswith("http"):
                continue
            quotes.append(line)
            expecting_quote = False
    return quotes


def pick_random_quote() -> Optional[str]:
    quotes = load_quotes()
    if not quotes:
        return None
    return random.choice(quotes)


async def send_daily_quote(channel: discord.abc.Messageable) -> None:
    quote = pick_random_quote()
    if quote is None:
        await channel.send("Nincs elérhető mondás a mai napra.")
        return
    await channel.send(f'A nap mondása: "{quote}"')


@tasks.loop(hours=24)
async def daily_quote_task():
    channel = bot.get_channel(QUOTES_CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(QUOTES_CHANNEL_ID)
        except discord.NotFound:
            print("Quotes channel not found.")
            return
        except discord.Forbidden:
            print("Missing permissions to access quotes channel.")
            return

    await send_daily_quote(channel)


@daily_quote_task.before_loop
async def before_daily_quote_task():
    await bot.wait_until_ready()
    now = datetime.datetime.now()
    target = now.replace(hour=12, minute=0, second=0, microsecond=0)
    if now >= target:
        target += datetime.timedelta(days=1)
    wait_seconds = (target - now).total_seconds()
    print(f"Next quote scheduled in {wait_seconds:.0f}s (epoch {time.time():.0f}).")
    await asyncio.sleep(wait_seconds)


# --- BELSŐ API ---


async def prank_loop():
    await bot.wait_until_ready()
    while not bot.is_closed():
        if core.last_prank_date == datetime.date.today():
            now = datetime.datetime.now()
            tomorrow = datetime.datetime.combine(
                now.date() + datetime.timedelta(days=1), datetime.time.min
            )
            wait_until_next_day = max(1, int((tomorrow - now).total_seconds()))
            print(
                f"Daily prank limit reached, waiting {wait_until_next_day}s for the next day."
            )
            await asyncio.sleep(wait_until_next_day)
            continue
        wait_time = random.randint(MIN_TIME, MAX_TIME)
        print(f"Ghost is waiting {wait_time}s before next prank attempt.")
        await asyncio.sleep(wait_time)
        if not core.prank_enabled:
            continue
        if core.last_prank_date == datetime.date.today():
            continue
        selection = select_prank_file()
        if not selection:
            continue
        file_path, selected_file = selection
        target_channel = None
        target_guild = None
        for guild in bot.guilds:
            if guild.voice_client and guild.voice_client.is_playing():
                continue
            candidates = [vc for vc in guild.voice_channels if len(vc.members) > 0]
            if candidates:
                target_channel = random.choice(candidates)
                target_guild = guild
                break
        if not target_channel:
            continue
        print(
            f"Auto prank ({selected_file}) target: {target_guild.name} -> {target_channel.name}"
        )
        prank_played = False
        try:
            voice_client = target_guild.voice_client
            created = False
            if not voice_client:
                voice_client = await target_channel.connect()
                created = True
            elif voice_client.channel != target_channel:
                await voice_client.move_to(target_channel)
            mixer = get_mixer(voice_client)
            mixer.add_sfx(discord.FFmpegPCMAudio(file_path, options="-vn"))
            while mixer.has_sfx():
                await asyncio.sleep(1)
            prank_played = True
            if created and not mixer.main_source:
                await voice_client.disconnect()
        except Exception as e:
            print(f"Prank error: {e}")
        if prank_played:
            core.last_prank_date = datetime.date.today()
            save_last_prank_date(core.last_prank_date)


@bot.event
async def on_ready():
    print(f"Bejelentkezve mint: {bot.user.name}")
    if not getattr(bot, "prank_state_loaded", False):
        core.last_prank_date = load_last_prank_date()
        bot.prank_state_loaded = True
    if not getattr(bot, "prank_task_started", False):
        bot.loop.create_task(prank_loop())
        bot.prank_task_started = True
    if not getattr(bot, "internal_server_started", False):
        bot.loop.create_task(start_internal_server())
        bot.internal_server_started = True
    if not getattr(bot, "daily_quote_task_started", False):
        daily_quote_task.start()
        bot.daily_quote_task_started = True
    if not getattr(bot, "scheduled_messages_loaded", False):
        async with scheduled_messages_lock:
            scheduled_messages.clear()
            scheduled_messages.extend(load_scheduled_messages())
        bot.scheduled_messages_loaded = True
    if not getattr(bot, "scheduled_message_task_started", False):
        bot.loop.create_task(scheduled_message_dispatch_loop())
        bot.scheduled_message_task_started = True


@bot.event
async def on_voice_state_update(member, before, after):
    voice_client = member.guild.voice_client
    if not voice_client or not voice_client.channel:
        return

    bot_channel = voice_client.channel
    guild_id = member.guild.id

    if after.channel == bot_channel and before.channel != bot_channel:
        if guild_id in afktasks:
            afktasks[guild_id].cancel()
            del afktasks[guild_id]
        return

    if before.channel != bot_channel or after.channel == bot_channel:
        return

    if guild_id in afktasks:
        afktasks[guild_id].cancel()

    if not bot.user:
        return

    if any(member != bot.user for member in bot_channel.members):
        return

    async def disconnect_if_empty(channel):
        try:
            await asyncio.sleep(60)
            vc = member.guild.voice_client
            if not vc or vc.channel != channel:
                return
            if not bot.user:
                return
            members_without_bot = [m for m in channel.members if m != bot.user]
            if not members_without_bot:
                clear_guild_queue(guild_id)
                await vc.disconnect()
        finally:
            afktasks.pop(guild_id, None)

    afktasks[guild_id] = bot.loop.create_task(disconnect_if_empty(bot_channel))

