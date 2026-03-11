from bot_app.core import *
from bot_app.alerts import start_internal_server
from bot_app.scheduler_web import load_scheduled_messages, scheduled_message_dispatch_loop
import bot_app.core as core


def _voice_channel_name(channel: Optional[discord.VoiceChannel]) -> str:
    if channel is None:
        return "none"
    return f"{channel.guild.name}/{channel.name}({channel.id})"


def _actor_name(member: discord.Member) -> str:
    return f"{member}({member.id})"


def is_timestamp_line(line: str) -> bool:
    return bool(QUOTE_TIMESTAMP_REGEX.match(line))


def load_quotes() -> list[str]:
    if not os.path.exists(QUOTES_FILE_PATH):
        logger.warning("Quotes file not found: %s", QUOTES_FILE_PATH)
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


def _cancel_gateway_disconnect_watchdog() -> None:
    watchdog_task = getattr(bot, "gateway_disconnect_watchdog_task", None)
    if watchdog_task and not watchdog_task.done():
        watchdog_task.cancel()
    bot.gateway_disconnect_watchdog_task = None


def _get_gateway_disconnect_started_at() -> Optional[float]:
    started_at = getattr(bot, "gateway_disconnect_started_at", None)
    if isinstance(started_at, (int, float)):
        return float(started_at)
    return None


def _mark_gateway_recovered(recovery_event: str) -> None:
    disconnect_started_at = _get_gateway_disconnect_started_at()
    _cancel_gateway_disconnect_watchdog()
    bot.gateway_disconnect_started_at = None
    if disconnect_started_at is None:
        return
    disconnected_for = max(0.0, time.monotonic() - disconnect_started_at)
    logger.info(
        "Discord gateway recovered via %s after %.1fs disconnected.",
        recovery_event,
        disconnected_for,
    )


async def _gateway_disconnect_watchdog(disconnect_started_at: float) -> None:
    if GATEWAY_RECOVERY_TIMEOUT_SECONDS <= 0:
        return

    try:
        await asyncio.sleep(GATEWAY_RECOVERY_TIMEOUT_SECONDS)
        if bot.is_closed():
            return
        active_disconnect_started_at = _get_gateway_disconnect_started_at()
        if active_disconnect_started_at != disconnect_started_at:
            return
        logger.error(
            "Discord gateway did not recover within %ss. Closing the bot process so the supervisor can restart it.",
            GATEWAY_RECOVERY_TIMEOUT_SECONDS,
        )
        await bot.close()
    except asyncio.CancelledError:
        return
    except Exception:
        logger.exception("Gateway disconnect watchdog failed.")


@tasks.loop(hours=24)
async def daily_quote_task():
    channel = bot.get_channel(QUOTES_CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(QUOTES_CHANNEL_ID)
        except discord.NotFound:
            logger.warning("Quotes channel not found: %s", QUOTES_CHANNEL_ID)
            return
        except discord.Forbidden:
            logger.warning("Missing permissions to access quotes channel: %s", QUOTES_CHANNEL_ID)
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
    logger.info(
        "Next quote scheduled in %.0fs (target_epoch %.0f).",
        wait_seconds,
        target.timestamp(),
    )
    await asyncio.sleep(wait_seconds)


# --- BELSŐ API ---


def _seconds_until_next_day(now: datetime.datetime) -> int:
    tomorrow = datetime.datetime.combine(
        now.date() + datetime.timedelta(days=1), datetime.time.min
    )
    return max(1, int((tomorrow - now).total_seconds()))


def _reset_prank_state_if_needed(now: datetime.datetime) -> None:
    if core.prank_state_date == now.date():
        return
    core.prank_state_date = now.date()
    core.pranks_played_today = 0
    save_prank_state(core.prank_state_date, core.pranks_played_today)
    logger.info("Prank day reset. date=%s", core.prank_state_date)


def _compute_prank_wait_seconds(now: datetime.datetime) -> int:
    remaining_pranks = max(0, DAILY_PRANK_TARGET_COUNT - core.pranks_played_today)
    if remaining_pranks <= 0:
        return _seconds_until_next_day(now)

    day_remaining = _seconds_until_next_day(now)
    reserved_spacing = max(0, remaining_pranks - 1) * MIN_TIME
    available_window = max(1, day_remaining - reserved_spacing)
    wait_ceiling = max(1, available_window // remaining_pranks)
    wait_floor = 1 if wait_ceiling <= MIN_TIME else MIN_TIME
    return random.randint(wait_floor, wait_ceiling)


def _pick_random_occupied_voice_channel():
    candidates = []
    for guild in bot.guilds:
        if guild.voice_client and (
            guild.voice_client.is_playing() or guild.voice_client.is_paused()
        ):
            logger.info(
                "Skipping guild %s(%s): existing voice_client is active.",
                guild.name,
                guild.id,
            )
            continue

        occupied_channels = [
            voice_channel
            for voice_channel in guild.voice_channels
            if any(not member.bot for member in voice_channel.members)
        ]
        for channel in occupied_channels:
            candidates.append((guild, channel))

    if not candidates:
        return None, None
    return random.choice(candidates)


async def prank_loop():
    await bot.wait_until_ready()
    logger.info("Prank loop started. enabled=%s mode=%s", core.prank_enabled, core.prank_mode)
    while not bot.is_closed():
        now = datetime.datetime.now()
        _reset_prank_state_if_needed(now)
        if core.pranks_played_today >= DAILY_PRANK_TARGET_COUNT:
            wait_until_next_day = _seconds_until_next_day(now)
            logger.info(
                "Daily prank target reached (%s/%s), waiting %ss for next day.",
                core.pranks_played_today,
                DAILY_PRANK_TARGET_COUNT,
                wait_until_next_day,
            )
            await asyncio.sleep(wait_until_next_day)
            continue

        wait_time = _compute_prank_wait_seconds(now)
        logger.info(
            "Prank loop sleeping for %ss before next attempt. progress=%s/%s",
            wait_time,
            core.pranks_played_today,
            DAILY_PRANK_TARGET_COUNT,
        )
        await asyncio.sleep(wait_time)

        if not core.prank_enabled:
            logger.info("Prank attempt skipped because prank mode is disabled.")
            continue

        now = datetime.datetime.now()
        _reset_prank_state_if_needed(now)
        if core.pranks_played_today >= DAILY_PRANK_TARGET_COUNT:
            continue

        selection = select_prank_file()
        if not selection:
            logger.warning("Prank attempt skipped: no prank audio files found.")
            continue

        file_path, selected_file = selection
        if not os.path.exists(file_path):
            logger.warning("Prank file does not exist: %s", file_path)
            continue

        target_guild, target_channel = _pick_random_occupied_voice_channel()
        if not target_channel or not target_guild:
            logger.info("Prank attempt skipped: no eligible voice channel with human members.")
            continue

        logger.info(
            "Auto prank selected file=%s target=%s/%s(%s)",
            selected_file,
            target_guild.name,
            target_channel.name,
            target_channel.id,
        )

        prank_played = False
        created = False
        voice_client = None
        lock = get_voice_operation_lock(target_guild.id)
        connection_changed = False
        try:
            async with lock:
                voice_client = target_guild.voice_client
                if voice_client and not voice_client.is_connected():
                    logger.warning(
                        "Prank loop found stale voice client, disconnecting. guild=%s",
                        target_guild.id,
                    )
                    try:
                        await voice_client.disconnect(force=True)
                    except Exception:
                        logger.exception(
                            "Prank loop failed to disconnect stale voice client. guild=%s",
                            target_guild.id,
                        )
                    voice_client = None

                if not voice_client:
                    try:
                        voice_client = await target_channel.connect()
                    except Exception as exc:
                        normalized_exc = normalize_voice_runtime_error(exc)
                        if normalized_exc is exc:
                            raise
                        raise normalized_exc from exc
                    created = True
                    connection_changed = True
                    logger.info(
                        "Prank loop connected to voice channel: %s",
                        _voice_channel_name(target_channel),
                    )
                elif voice_client.channel != target_channel:
                    logger.info(
                        "Prank loop moved voice client from %s to %s",
                        _voice_channel_name(voice_client.channel),
                        _voice_channel_name(target_channel),
                    )
                    await voice_client.move_to(target_channel)
                    connection_changed = True

            await settle_voice_connection(connection_changed)
            mixer = get_mixer(voice_client)
            mixer.add_sfx(build_sfx_source(file_path))
            while mixer.has_sfx():
                await asyncio.sleep(1)
            prank_played = True
            logger.info(
                "Auto prank playback finished. file=%s guild=%s(%s)",
                selected_file,
                target_guild.name,
                target_guild.id,
            )
            if created and not mixer.main_source:
                async with lock:
                    await voice_client.disconnect()
                logger.info(
                    "Prank loop disconnected after playback from %s",
                    _voice_channel_name(target_channel),
                )
        except Exception:
            logger.exception(
                "Prank loop error. guild=%s(%s) channel=%s(%s) file=%s",
                target_guild.name if target_guild else "unknown",
                target_guild.id if target_guild else "unknown",
                target_channel.name if target_channel else "unknown",
                target_channel.id if target_channel else "unknown",
                selected_file,
            )
            if (
                created
                and voice_client
                and voice_client.is_connected()
                and not voice_client.is_playing()
            ):
                try:
                    async with lock:
                        await voice_client.disconnect()
                    logger.info("Disconnected prank voice client after error.")
                except Exception:
                    logger.exception("Failed to disconnect prank voice client after error.")

        if prank_played:
            core.prank_state_date = datetime.date.today()
            core.pranks_played_today = min(
                DAILY_PRANK_TARGET_COUNT, core.pranks_played_today + 1
            )
            save_prank_state(core.prank_state_date, core.pranks_played_today)
            logger.info(
                "Prank progress saved. progress=%s/%s date=%s",
                core.pranks_played_today,
                DAILY_PRANK_TARGET_COUNT,
                core.prank_state_date,
            )


@bot.event
async def on_ready():
    _mark_gateway_recovered("ready")
    logger.info(
        "Bot ready as %s(%s). guilds=%s",
        bot.user.name if bot.user else "unknown",
        bot.user.id if bot.user else "unknown",
        len(bot.guilds),
    )
    if not getattr(bot, "prank_state_loaded", False):
        core.prank_state_date, core.pranks_played_today = load_prank_state()
        bot.prank_state_loaded = True
        logger.info(
            "Prank state loaded. date=%s played=%s/%s",
            core.prank_state_date,
            core.pranks_played_today,
            DAILY_PRANK_TARGET_COUNT,
        )
    if not getattr(bot, "prank_task_started", False):
        bot.loop.create_task(prank_loop())
        bot.prank_task_started = True
        logger.info("Prank loop task started.")
    if not getattr(bot, "internal_server_started", False):
        bot.loop.create_task(start_internal_server())
        bot.internal_server_started = True
        logger.info("Internal API startup task started.")
    if not getattr(bot, "daily_quote_task_started", False):
        daily_quote_task.start()
        bot.daily_quote_task_started = True
        logger.info("Daily quote task started.")
    if not getattr(bot, "scheduled_messages_loaded", False):
        async with scheduled_messages_lock:
            scheduled_messages.clear()
            scheduled_messages.extend(load_scheduled_messages())
        bot.scheduled_messages_loaded = True
        logger.info("Scheduled messages loaded. count=%s", len(scheduled_messages))
    if not getattr(bot, "scheduled_message_task_started", False):
        bot.loop.create_task(scheduled_message_dispatch_loop())
        bot.scheduled_message_task_started = True
        logger.info("Scheduled message dispatch loop started.")


@bot.event
async def on_connect():
    logger.info("Discord gateway connected.")


@bot.event
async def on_disconnect():
    logger.warning("Discord gateway disconnected.")
    if GATEWAY_RECOVERY_TIMEOUT_SECONDS <= 0:
        return

    disconnect_started_at = _get_gateway_disconnect_started_at()
    if disconnect_started_at is None:
        disconnect_started_at = time.monotonic()
        bot.gateway_disconnect_started_at = disconnect_started_at

    watchdog_task = getattr(bot, "gateway_disconnect_watchdog_task", None)
    if watchdog_task and not watchdog_task.done():
        return

    bot.gateway_disconnect_watchdog_task = bot.loop.create_task(
        _gateway_disconnect_watchdog(disconnect_started_at)
    )
    logger.warning(
        "Gateway recovery watchdog armed. timeout=%ss",
        GATEWAY_RECOVERY_TIMEOUT_SECONDS,
    )


@bot.event
async def on_resumed():
    _mark_gateway_recovered("session resume")
    logger.info("Discord gateway session resumed.")


@bot.event
async def on_command(ctx):
    command_name = ctx.command.qualified_name if ctx.command else "unknown"
    logger.info(
        "Command invoked. command=%s guild=%s channel=%s user=%s",
        command_name,
        ctx.guild.id if ctx.guild else "dm",
        ctx.channel.id if ctx.channel else "unknown",
        ctx.author.id if ctx.author else "unknown",
    )


@bot.event
async def on_command_completion(ctx):
    command_name = ctx.command.qualified_name if ctx.command else "unknown"
    logger.info(
        "Command completed. command=%s guild=%s channel=%s user=%s",
        command_name,
        ctx.guild.id if ctx.guild else "dm",
        ctx.channel.id if ctx.channel else "unknown",
        ctx.author.id if ctx.author else "unknown",
    )


@bot.event
async def on_command_error(ctx, error):
    if ctx.command and ctx.command.has_error_handler():
        return
    cog = ctx.cog
    if cog and cog.has_error_handler():
        return
    command_name = ctx.command.qualified_name if ctx.command else "unknown"
    exc_info = (type(error), error, error.__traceback__)
    logger.error(
        "Unhandled command error. command=%s guild=%s channel=%s user=%s error=%s",
        command_name,
        ctx.guild.id if ctx.guild else "dm",
        ctx.channel.id if ctx.channel else "unknown",
        ctx.author.id if ctx.author else "unknown",
        error,
        exc_info=exc_info,
    )


@bot.event
async def on_voice_state_update(member, before, after):
    if member.bot:
        return

    voice_client = member.guild.voice_client
    if not voice_client or not voice_client.channel:
        return

    bot_channel = voice_client.channel
    guild_id = member.guild.id
    logger.info(
        "Voice state update. member=%s before=%s after=%s bot_channel=%s",
        _actor_name(member),
        _voice_channel_name(before.channel),
        _voice_channel_name(after.channel),
        _voice_channel_name(bot_channel),
    )

    if after.channel == bot_channel and before.channel != bot_channel:
        if guild_id in afktasks:
            afktasks[guild_id].cancel()
            del afktasks[guild_id]
            logger.info(
                "AFK disconnect timer cancelled because user joined bot channel. guild=%s",
                guild_id,
            )
        return

    if before.channel != bot_channel or after.channel == bot_channel:
        return

    if guild_id in afktasks:
        afktasks[guild_id].cancel()
        logger.info("AFK disconnect timer reset. guild=%s", guild_id)

    if not bot.user:
        return

    if any(channel_member != bot.user for channel_member in bot_channel.members):
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
                logger.info(
                    "Voice channel empty for 60s, disconnecting. guild=%s channel=%s",
                    guild_id,
                    _voice_channel_name(channel),
                )
                lock = get_voice_operation_lock(guild_id)
                async with lock:
                    await vc.disconnect()
        finally:
            afktasks.pop(guild_id, None)
            logger.info("AFK disconnect timer cleared. guild=%s", guild_id)

    afktasks[guild_id] = bot.loop.create_task(disconnect_if_empty(bot_channel))
    logger.info(
        "AFK disconnect timer started for guild=%s channel=%s",
        guild_id,
        _voice_channel_name(bot_channel),
    )

