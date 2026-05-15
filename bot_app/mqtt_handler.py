import asyncio
import json
import paho.mqtt.client as mqtt
import discord
import os
from gtts import gTTS
from bot_app.core import (
    bot, logger, get_mixer, play_next_in_queue, song_queues, 
    titles_queues, YTDLSource, get_play_lock, ensure_queue,
    find_local_music, LocalFileSource, sp, ytdl, cleanup_audio_source,
    get_voice_operation_lock, BASE_DIR, TARGET_CHANNEL_ID,
    normalize_voice_runtime_error, settle_voice_connection
)

# MQTT Beállítások
MQTT_BROKER = "192.168.0.19"
MQTT_PORT = 1883
MQTT_USER = "hass"
MQTT_PASS = "Balazs2003"
TOPIC_REQ = "ha/discord/request"

# Alapértelmezett felhasználó (Uram)
DEFAULT_USER_ID = 284011534198374411

class DiscordMQTTBridge:
    def __init__(self):
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1)
        self.client.on_connect = self.on_connect
        self.client.on_message = self.on_message
        if MQTT_USER and MQTT_PASS:
            self.client.username_pw_set(MQTT_USER, MQTT_PASS)

    def on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            logger.info("Discord Bot MQTT Bridge: Sikeres csatlakozás!")
            client.subscribe(TOPIC_REQ)
        else:
            logger.error(f"Discord Bot MQTT Bridge: Csatlakozási hiba! Kód: {rc}")

    def on_message(self, client, userdata, msg):
        try:
            payload = msg.payload.decode('utf-8')
            logger.info(f"MQTT utasítás érkezett: {payload}")
            data = json.loads(payload)
            asyncio.run_coroutine_threadsafe(self.handle_action(data), bot.loop)
        except Exception as e:
            logger.error(f"Hiba az MQTT üzenet feldolgozásakor: {e}")

    async def handle_action(self, data):
        action = data.get("action")
        guild = bot.guilds[0] if bot.guilds else None
        if not guild: return

        if action == "move":
            await self.move_user(guild, data.get("user"), data.get("channel"))
        elif action == "kick":
            await self.kick_user(guild, data.get("user"))
        elif action == "mute":
            await self.mute_user(guild, data.get("user"), data.get("state", True))
        elif action == "play":
            await self.play_music(guild, data.get("url"), data.get("channel"))
        elif action == "join":
            await self.join_voice(guild, data.get("user_id") or data.get("user"))
        elif action == "leave":
            await self.leave_voice(guild)
        elif action == "say":
            await self.say_text(guild, data.get("text"))
        elif action == "chat":
            await self.send_chat(guild, data.get("text"), data.get("channel"))

    async def find_member(self, guild, identifier):
        if not identifier: return None
        if isinstance(identifier, int) or (isinstance(identifier, str) and identifier.isdigit()):
            member = guild.get_member(int(identifier))
            if member: return member
        name = str(identifier).lower()
        for member in guild.members:
            if name in member.name.lower() or (member.nick and name in member.nick.lower()):
                return member
        return None

    async def find_channel(self, guild, name):
        if not name: return None
        name = name.lower()
        for channel in guild.text_channels if "chat" in name or "szoba" in name else guild.voice_channels:
            if name in channel.name.lower():
                return channel
        # Ha nem találtuk a specifikus típusban, nézzük meg az összesben
        for channel in guild.channels:
            if name in channel.name.lower():
                return channel
        return None

    async def resolve_preferred_voice_channel(self, guild, channel_name=None, member_identifier=None):
        if channel_name:
            channel = await self.find_channel(guild, channel_name)
            if isinstance(channel, discord.VoiceChannel):
                return channel

        target_identifier = (
            member_identifier if member_identifier is not None else DEFAULT_USER_ID
        )
        member = await self.find_member(guild, target_identifier)
        if member and member.voice and member.voice.channel:
            return member.voice.channel

        voice_client = guild.voice_client
        if voice_client and voice_client.is_connected() and voice_client.channel:
            return voice_client.channel

        for vc in guild.voice_channels:
            if any(not m.bot for m in vc.members):
                return vc

        return None

    async def ensure_connected(self, guild, preferred_channel=None):
        voice_client = guild.voice_client
        target_channel = preferred_channel or await self.resolve_preferred_voice_channel(guild)
        if not target_channel:
            return voice_client if voice_client and voice_client.is_connected() else None

        lock = get_voice_operation_lock(guild.id)
        async with lock:
            connection_changed = False
            voice_client = guild.voice_client

            if voice_client and not voice_client.is_connected():
                logger.warning("MQTT voice client stale, disconnecting. guild=%s", guild.id)
                try:
                    await voice_client.disconnect(force=True)
                except Exception:
                    logger.exception(
                        "Failed to disconnect stale MQTT voice client. guild=%s",
                        guild.id,
                    )
                voice_client = None

            try:
                if not voice_client:
                    voice_client = await target_channel.connect()
                    connection_changed = True
                    logger.info(
                        "MQTT voice connected. guild=%s channel=%s",
                        guild.id,
                        target_channel.name,
                    )
                elif voice_client.channel != target_channel:
                    from_channel = voice_client.channel.name if voice_client.channel else "none"
                    await voice_client.move_to(target_channel)
                    connection_changed = True
                    logger.info(
                        "MQTT voice moved. guild=%s from=%s to=%s",
                        guild.id,
                        from_channel,
                        target_channel.name,
                    )
            except Exception as exc:
                normalized_exc = normalize_voice_runtime_error(exc)
                logger.exception(
                    "MQTT voice connect/move failed. guild=%s target=%s",
                    guild.id,
                    target_channel.name,
                )
                raise normalized_exc

        await settle_voice_connection(connection_changed)
        return voice_client

    async def join_voice(self, guild, identifier=None):
        target_id = identifier if identifier else DEFAULT_USER_ID
        target_channel = await self.resolve_preferred_voice_channel(
            guild, member_identifier=target_id
        )
        if target_channel:
            await self.ensure_connected(guild, target_channel)

    async def leave_voice(self, guild):
        voice_client = guild.voice_client
        if voice_client:
            lock = get_voice_operation_lock(guild.id)
            async with lock:
                await voice_client.disconnect()

    async def say_text(self, guild, text):
        if not text: return
        target_channel = await self.resolve_preferred_voice_channel(guild)
        voice_client = await self.ensure_connected(guild, target_channel)
        if not voice_client: return
        tts_file = os.path.join(BASE_DIR, f"mqtt_tts_{guild.id}.mp3")
        try:
            tts = gTTS(text=text, lang="hu")
            tts.save(tts_file)
            mixer = get_mixer(voice_client)
            mixer.set_main_source(discord.FFmpegPCMAudio(tts_file, options="-vn"))
            while mixer.has_main_source():
                await asyncio.sleep(0.5)
        finally:
            if os.path.exists(tts_file): os.remove(tts_file)

    async def send_chat(self, guild, text, channel_name=None):
        if not text: return
        target_channel = None
        if channel_name:
            target_channel = await self.find_channel(guild, channel_name)
        
        if not target_channel and TARGET_CHANNEL_ID:
            target_channel = bot.get_channel(TARGET_CHANNEL_ID)
            
        if target_channel and isinstance(target_channel, discord.TextChannel):
            await target_channel.send(text)
            logger.info(f"MQTT Chat üzenet elküldve ide: {target_channel.name}")
        else:
            logger.warning("MQTT Chat: Nem találtam megfelelő szöveges csatornát.")

    async def move_user(self, guild, user_name, channel_name):
        member = await self.find_member(guild, user_name)
        channel = await self.find_channel(guild, channel_name)
        if member and channel: await member.move_to(channel)

    async def kick_user(self, guild, user_name):
        member = await self.find_member(guild, user_name)
        if member: await member.kick(reason="MQTT parancs")

    async def mute_user(self, guild, user_name, state):
        member = await self.find_member(guild, user_name)
        if member: await member.edit(mute=state)

    async def play_music(self, guild, query, channel_name=None):
        if not query:
            logger.warning("MQTT play requested without query. guild=%s", guild.id)
            return

        target_channel = await self.resolve_preferred_voice_channel(
            guild, channel_name=channel_name
        )
        if not target_channel:
            logger.warning(
                "MQTT play skipped: no eligible voice channel found. guild=%s query=%s",
                guild.id,
                query,
            )
            return

        voice_client = await self.ensure_connected(guild, target_channel)
        if not voice_client: return
        logger.info(
            "MQTT play target resolved. guild=%s channel=%s query=%s",
            guild.id,
            target_channel.name,
            query,
        )
        ensure_queue(guild.id)
        player = None
        local_filename = find_local_music(query)
        if local_filename:
            source = discord.FFmpegPCMAudio(f"music/{local_filename}", options="-vn")
            player = LocalFileSource(source, title=local_filename)
        if not player:
            search_query = query
            if "spotify.com" in query:
                try:
                    track = sp.track(query)
                    search_query = f"ytsearch:{track['artists'][0]['name']} - {track['name']}"
                except: pass
            elif not query.startswith("http"):
                search_query = f"ytsearch:{query}"
            try:
                player = await YTDLSource.from_url(search_query, loop=bot.loop, stream=False)
            except:
                try: player = await YTDLSource.from_url(search_query, loop=bot.loop, stream=True)
                except: return
        if player:
            mixer = get_mixer(voice_client)
            if mixer.main_source or voice_client.is_paused():
                song_queues[guild.id].append(player)
                titles_queues[guild.id].append(player.title)
            else:
                from bot_app.core import play_next_in_queue
                class FakeCtx:
                    def __init__(self, guild): self.guild = guild
                    async def send(self, msg): logger.info(f"Bot üzenet: {msg}")
                fake_ctx = FakeCtx(guild)
                mixer.set_main_source(player, on_end=lambda: asyncio.run_coroutine_threadsafe(play_next_in_queue(fake_ctx), bot.loop))

async def start_mqtt():
    bridge = DiscordMQTTBridge()
    bridge.client.connect(MQTT_BROKER, MQTT_PORT, 60)
    bridge.client.loop_start()
    while not bot.is_closed(): await asyncio.sleep(10)
    bridge.client.loop_stop()
