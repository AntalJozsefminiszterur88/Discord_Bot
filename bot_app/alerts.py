from bot_app.core import *
from bot_app.scheduler_web import (
    handle_create_scheduled_message,
    handle_delete_scheduled_message,
    handle_list_scheduled_messages,
    handle_scheduler_page,
    handle_update_scheduled_message,
)


async def handle_share_video(request):
    try:
        data = await request.json()
    except Exception:
        return web.Response(status=400, text="Invalid JSON payload")

    url = data.get("url")
    title = data.get("title")
    uploader = data.get("uploader")

    if not all([url, title, uploader]):
        return web.Response(status=400, text="Missing url/title/uploader fields")

    if TARGET_CHANNEL_ID is None:
        return web.Response(status=500, text="TARGET_CHANNEL_ID is not configured")

    channel = bot.get_channel(TARGET_CHANNEL_ID)
    if channel is None:
        return web.Response(status=500, text="Target channel not found")

    try:
        logger.info(
            "[INTERNAL API] share-video received. title=%s url=%s uploader=%s",
            title,
            url,
            uploader,
        )
        message = f"{url}\n**{title}**"
        await channel.send(message)
        return web.Response(status=200, text="Video shared successfully")
    except Exception as e:
        logger.exception("Failed to process /share-video request.")
        return web.Response(status=500, text=f"Failed to send message: {e}")


def stop_radnai_alert() -> bool:
    if radnai_alert_stop_event and not radnai_alert_stop_event.is_set():
        radnai_alert_stop_event.set()
        return True
    return False


async def send_radnai_chat_alert(channel, alert_message: str, stop_event: asyncio.Event):
    chat_messages_sent = 0
    warnings = []

    if channel is None:
        return False, chat_messages_sent, warnings

    try:
        for _ in range(RADNAI_CHAT_ALERT_REPEAT_COUNT):
            if stop_event.is_set():
                break
            await channel.send(
                alert_message,
                allowed_mentions=discord.AllowedMentions(everyone=False),
            )
            chat_messages_sent += 1
            if chat_messages_sent < RADNAI_CHAT_ALERT_REPEAT_COUNT:
                await asyncio.sleep(1)
    except Exception as e:
        warnings.append(f"Failed to send chat alert: {e}")

    return chat_messages_sent > 0, chat_messages_sent, warnings


async def play_radnai_voice_alert(alert_sound_path: str, stop_event: asyncio.Event):
    voice_alerts_played = 0
    warnings = []

    if not os.path.exists(alert_sound_path):
        warnings.append(f"Alert sound file not found: {alert_sound_path}")
        logger.warning("Radnai alert sound file not found: %s", alert_sound_path)
        return voice_alerts_played, warnings

    for guild in bot.guilds:
        if stop_event.is_set():
            break

        try:
            lock = get_voice_operation_lock(guild.id)
            candidates = [
                voice_channel
                for voice_channel in guild.voice_channels
                if any(not member.bot for member in voice_channel.members)
            ]
            if not candidates:
                continue

            target_channel = candidates[0]
            voice_client = guild.voice_client
            if (
                voice_client
                and voice_client.channel
                and voice_client.channel in candidates
            ):
                target_channel = voice_client.channel

            created = False
            async with lock:
                if voice_client and not voice_client.is_connected():
                    logger.warning(
                        "Radnai alert found stale voice client, disconnecting. guild=%s",
                        guild.id,
                    )
                    try:
                        await voice_client.disconnect(force=True)
                    except Exception:
                        logger.exception(
                            "Failed to disconnect stale voice client in radnai alert. guild=%s",
                            guild.id,
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
                    logger.info(
                        "Radnai alert connected to voice. guild=%s channel=%s",
                        guild.id,
                        target_channel.id,
                    )
                elif voice_client.channel != target_channel:
                    logger.info(
                        "Radnai alert moved voice. guild=%s from=%s to=%s",
                        guild.id,
                        voice_client.channel.id if voice_client.channel else "none",
                        target_channel.id,
                    )
                    await voice_client.move_to(target_channel)

            mixer = get_mixer(voice_client)
            mixer.add_sfx(discord.FFmpegPCMAudio(alert_sound_path, options="-vn"))

            while mixer.has_sfx() and not stop_event.is_set():
                await asyncio.sleep(1)

            if stop_event.is_set() and (voice_client.is_playing() or voice_client.is_paused()):
                voice_client.stop()

            if created and not mixer.main_source:
                async with lock:
                    await voice_client.disconnect()
                logger.info(
                    "Radnai alert disconnected from voice. guild=%s channel=%s",
                    guild.id,
                    target_channel.id,
                )

            voice_alerts_played += 1
        except Exception as e:
            warnings.append(f"{guild.name}: {e}")
            logger.exception(
                "Radnai voice alert failed in guild=%s(%s)",
                guild.name,
                guild.id,
            )

    return voice_alerts_played, warnings


async def handle_radnai_alert(request):
    global radnai_alert_stop_event

    if radnai_alert_lock.locked():
        return web.Response(status=409, text="Radnai alert already running")

    try:
        data = await request.json()
    except Exception:
        data = {}

    if data is None:
        data = {}
    if not isinstance(data, dict):
        return web.Response(status=400, text="Invalid JSON payload")

    alert_type = str(data.get("type") or "change").lower()
    alert_error = data.get("error")
    logger.info(
        "Internal API /alert-radnai received. type=%s has_error=%s",
        alert_type,
        alert_error is not None,
    )
    type_warning = None
    if alert_type not in {"change", "outage"}:
        type_warning = f"Unknown alert type '{alert_type}', defaulted to change"
        alert_type = "change"

    change_alert_message = (
        "\U0001F6A8 Magyar P\u00e9ter riad\u00f3!! Friss\u00fclt a radnaimark.hu!!!"
        "(HTML hossz v\u00e1ltoz\u00e1s) \U0001F6A8"
    )
    outage_alert_message = (
        "\u26A0\uFE0F **Figyelem!** A radnaimark.hu jelenleg nem el\u00e9rhet\u0151 vagy "
        "r\u00f6vid ideig nem volt el\u00e9rhet\u0151"
    )
    alert_sound_path = "/app/radnai_alert/radnai_alert.mp3"

    chat_alert_sent = False
    chat_messages_sent = 0
    voice_alerts_played = 0
    stopped_by_labhoz = False
    warnings = []
    if type_warning:
        warnings.append(type_warning)

    async with radnai_alert_lock:
        channel = bot.get_channel(RADNAI_ALERT_CHANNEL_ID)
        if channel is None:
            try:
                channel = await bot.fetch_channel(RADNAI_ALERT_CHANNEL_ID)
            except Exception as e:
                warnings.append(f"Target channel unavailable: {e}")
                channel = None

        if alert_type == "outage":
            if channel is not None:
                try:
                    message = outage_alert_message
                    error_text = str(alert_error).strip() if alert_error is not None else ""
                    if error_text:
                        message = f"{message}\nHiba: {error_text}"
                    await channel.send(
                        message,
                        allowed_mentions=discord.AllowedMentions(everyone=False),
                    )
                    chat_alert_sent = True
                    chat_messages_sent = 1
                except Exception as e:
                    warnings.append(f"Failed to send outage alert: {e}")
        else:
            stop_event = asyncio.Event()
            radnai_alert_stop_event = stop_event
            try:
                chat_task = asyncio.create_task(
                    send_radnai_chat_alert(channel, change_alert_message, stop_event)
                )
                voice_task = asyncio.create_task(
                    play_radnai_voice_alert(alert_sound_path, stop_event)
                )
                chat_result, voice_result = await asyncio.gather(chat_task, voice_task)

                chat_alert_sent, chat_messages_sent, chat_warnings = chat_result
                voice_alerts_played, voice_warnings = voice_result
                warnings.extend(chat_warnings)
                warnings.extend(voice_warnings)
                stopped_by_labhoz = stop_event.is_set()
            finally:
                radnai_alert_stop_event = None

    if alert_type == "outage":
        if not chat_alert_sent:
            details = "; ".join(warnings) if warnings else "No alert target available"
            logger.warning("Radnai outage alert failed: %s", details)
            return web.Response(status=500, text=f"Radnai outage alert failed: {details}")

        details = f"chat_messages={chat_messages_sent}"
        if warnings:
            details = f"{details}, warnings={'; '.join(warnings)}"
        logger.info("Radnai outage alert sent. %s", details)
        return web.Response(status=200, text=f"Radnai outage alert sent ({details})")

    if stopped_by_labhoz and not chat_alert_sent and voice_alerts_played == 0:
        details = "; ".join(warnings) if warnings else "Stopped by !l\u00e1bhoz"
        logger.info("Radnai alert stopped: %s", details)
        return web.Response(status=200, text=f"Radnai alert stopped ({details})")

    if not chat_alert_sent and voice_alerts_played == 0:
        details = "; ".join(warnings) if warnings else "No alert targets available"
        logger.warning("Radnai alert failed: %s", details)
        return web.Response(status=500, text=f"Radnai alert failed: {details}")

    details = (
        f"chat_sent={chat_alert_sent}, "
        f"chat_messages={chat_messages_sent}, "
        f"voice_alerts={voice_alerts_played}"
    )
    if stopped_by_labhoz:
        details = f"{details}, stopped_by_labhoz=True"
    if warnings:
        details = f"{details}, warnings={'; '.join(warnings)}"
    logger.info("Radnai alert triggered. %s", details)
    return web.Response(status=200, text=f"Radnai alert triggered ({details})")


async def start_internal_server():
    await bot.wait_until_ready()
    app = web.Application()
    app.add_routes(
        [
            web.get("/scheduler", handle_scheduler_page),
            web.get("/scheduled-messages", handle_list_scheduled_messages),
            web.post("/scheduled-messages", handle_create_scheduled_message),
            web.put("/scheduled-messages/{message_id}", handle_update_scheduled_message),
            web.delete("/scheduled-messages/{message_id}", handle_delete_scheduled_message),
            web.post("/share-video", handle_share_video),
            web.post("/alert-radnai", handle_radnai_alert),
        ]
    )

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", INTERNAL_API_PORT)
    await site.start()
    bot.internal_api_runner = runner
    logger.info("Internal API running on 0.0.0.0:%s", INTERNAL_API_PORT)


# --- AUTOMATA IJESZTGETŐS LOOP ---
