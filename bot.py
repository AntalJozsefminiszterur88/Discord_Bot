import asyncio
import audioop
import datetime
import os
import random
import re
import threading
import time
from typing import Callable, Optional

import discord
import spotipy
import yt_dlp
from aiohttp import web
from discord.ext import commands, tasks
from dotenv import load_dotenv
from gtts import gTTS
from spotipy.oauth2 import SpotifyClientCredentials

# --- BEÁLLÍTÁSOK ---
MIN_TIME = 1800  # Minimum 30 perc
MAX_TIME = 7200  # Maximum 2 óra
# -------------------
INTERNAL_API_PORT = 5050  # Port az internal API-hoz (Docker konténeren belül)
TARGET_CHANNEL_ID: Optional[int] = 1370685414578327594
QUOTES_CHANNEL_ID = 416599669355970560

# --- PRANK ÁLLAPOT ---
prank_enabled = True
prank_mode = "normal"  # normal | jimmy | mixed

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
SPOTIPY_CLIENT_ID = os.getenv("SPOTIPY_CLIENT_ID")
SPOTIPY_CLIENT_SECRET = os.getenv("SPOTIPY_CLIENT_SECRET")

sp = spotipy.Spotify(
    auth_manager=SpotifyClientCredentials(
        client_id=SPOTIPY_CLIENT_ID, client_secret=SPOTIPY_CLIENT_SECRET
    )
)

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)
bot.remove_command("help")

song_queues = {}
titles_queues = {}
afktasks = {}
mixers = {}
roulette_games = {}

# YT-DLP beállítások
ytdl_format_options = {
    "format": "bestaudio/best",
    "outtmpl": "%(extractor)s-%(id)s-%(title)s.%(ext)s",
    "restrictfilenames": True,
    "noplaylist": True,
    "nocheckcertificate": True,
    "ignoreerrors": False,
    "logtostderr": False,
    "quiet": True,
    "no_warnings": True,
    "default_search": "auto",
    "source_address": "0.0.0.0",
    "extractor_args": {"youtube": {"player_client": ["android", "ios"]}},
    "cachedir": False,
}

ffmpeg_options = {
    "options": "-vn",
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
}

ytdl = yt_dlp.YoutubeDL(ytdl_format_options)


class MixingAudioSource(discord.AudioSource):
    def __init__(self, main_source: Optional[discord.AudioSource] = None):
        self.main_source = main_source
        self.sfx_sources = []
        self.sample_width = 2
        self._lock = threading.Lock()
        self._on_main_end = None

    def set_main_source(self, source: Optional[discord.AudioSource], on_end=None):
        with self._lock:
            self.main_source = source
            self._on_main_end = on_end

    def add_sfx(self, source: discord.AudioSource):
        with self._lock:
            self.sfx_sources.append(source)

    def has_sfx(self) -> bool:
        with self._lock:
            return bool(self.sfx_sources)

    def _mix(self, base: bytes, overlay: bytes) -> bytes:
        if not base:
            return overlay
        if not overlay:
            return base
        max_len = max(len(base), len(overlay))
        if len(base) < max_len:
            base += b"\x00" * (max_len - len(base))
        if len(overlay) < max_len:
            overlay += b"\x00" * (max_len - len(overlay))
        return audioop.add(base, overlay, self.sample_width)

    def read(self) -> bytes:
        main_data = b""
        sfx_datas = []
        on_end = None

        with self._lock:
            if self.main_source:
                main_data = self.main_source.read()
                if not main_data:
                    on_end = self._on_main_end
                    self.main_source = None
                    self._on_main_end = None
            sfx_remaining = []
            for source in self.sfx_sources:
                data = source.read()
                if data:
                    sfx_datas.append(data)
                    sfx_remaining.append(source)
            self.sfx_sources = sfx_remaining

        mixed = main_data
        for data in sfx_datas:
            mixed = self._mix(mixed, data)

        if on_end:
            bot.loop.call_soon_threadsafe(asyncio.create_task, on_end())

        if mixed:
            return mixed
        return b"\x00" * 3840

    def is_opus(self):
        return False


class YTDLSource(discord.PCMVolumeTransformer):
    def __init__(self, source, *, data, volume=0.5):
        super().__init__(source, volume)
        self.data = data
        self.title = data.get("title")
        self.url = data.get("url")

    @classmethod
    async def from_url(cls, url, *, loop=None, stream=False):
        loop = loop or asyncio.get_event_loop()
        data = await loop.run_in_executor(
            None, lambda: ytdl.extract_info(url, download=not stream)
        )
        if "entries" in data:
            data = data["entries"][0]
        filename = data["url"] if stream else ytdl.prepare_filename(data)
        return cls(discord.FFmpegPCMAudio(filename, **ffmpeg_options), data=data)


class LocalFileSource(discord.PCMVolumeTransformer):
    def __init__(self, source, *, title, volume=0.5):
        super().__init__(source, volume)
        self.title = title


class RouletteGame:
    def __init__(self, guild_id: int):
        self.guild_id = guild_id
        self.active = False
        self.mode = None
        self.stake = None
        self.chamber_position = 1
        self.bullet_position = None
        self.voice_client = None
        self.mixer = None
        self.lock = asyncio.Lock()
        self.players = []
        self.current_player_index = 0
        self.turn_task = None
        self.turn_message = None
        self.turn_deadline = None
        self.messages_to_cleanup = []

    def track_message(self, message: Optional[discord.Message]) -> None:
        if message:
            self.messages_to_cleanup.append(message)

    async def send_and_track(self, ctx, *args, **kwargs) -> discord.Message:
        message = await ctx.send(*args, **kwargs)
        self.track_message(message)
        return message

    async def start(self, ctx, mode: int, stake: str, voice_client, mixer) -> bool:
        members = [member for member in voice_client.channel.members if not member.bot]
        members.sort(key=lambda member: member.display_name.lower())
        if len(members) < 2:
            await ctx.send("Legalább 2 játékos szükséges a ruletthez!")
            return False

        self.players = members
        self.current_player_index = random.randrange(len(self.players))
        self.active = True
        self.mode = mode
        self.stake = stake
        self.chamber_position = 1
        self.bullet_position = random.randint(1, 6)
        self.voice_client = voice_client
        self.mixer = mixer
        await self.send_and_track(
            ctx,
            embed=discord.Embed(
                title="🎲 Russian Roulette V2",
                description=(
                    f"Játék indul! Kezdő játékos: **{self._current_player().display_name}**"
                ),
                color=discord.Color.red(),
            )
        )
        await self._announce_turn(ctx)
        return True

    async def stop(self):
        self.active = False
        if self.mixer:
            self.mixer.set_main_source(None)
        await self._cancel_turn_timer()
        self.players = []
        self.turn_message = None
        self.turn_deadline = None
        self.messages_to_cleanup = []

    def _roll_hit(self) -> bool:
        if self.mode == 1:
            return random.randint(1, 6) == 1
        if self.bullet_position == self.chamber_position:
            return True
        self.chamber_position = 1 if self.chamber_position == 6 else self.chamber_position + 1
        return False

    def _current_player(self) -> Optional[discord.Member]:
        if not self.players:
            return None
        return self.players[self.current_player_index]

    async def _cancel_turn_timer(self) -> None:
        if self.turn_task and not self.turn_task.done():
            self.turn_task.cancel()
            try:
                await self.turn_task
            except asyncio.CancelledError:
                pass
        self.turn_task = None

    def _build_turn_embed(self, player: discord.Member, description: str) -> discord.Embed:
        embed = discord.Embed(
            title="🎯 Russian Roulette V2",
            description=description,
            color=discord.Color.dark_red(),
        )
        embed.set_footer(text=f"Következő játékos: {player.display_name}")
        return embed

    async def _update_turn_message(
        self,
        ctx,
        player: discord.Member,
        description: str,
        *,
        force_new: bool = False,
    ) -> None:
        embed = self._build_turn_embed(player, description)
        if self.turn_message and not force_new:
            try:
                await self.turn_message.edit(embed=embed)
                return
            except discord.NotFound:
                self.turn_message = None
        self.turn_message = await ctx.send(embed=embed)
        self.track_message(self.turn_message)

    async def _announce_turn(self, ctx) -> None:
        current_player = self._current_player()
        if not current_player:
            return
        self.turn_deadline = int(time.time()) + 60
        description = (
            f"**{current_player.display_name}** következik.\n"
            f"⏰ Idő lejár: <t:{self.turn_deadline}:R>\n"
            "Írd be: **!énjövök**"
        )
        await self._update_turn_message(
            ctx, current_player, description, force_new=True
        )
        await self._cancel_turn_timer()
        self.turn_task = asyncio.create_task(self._turn_timeout(ctx, current_player.id))

    async def _turn_timeout(self, ctx, player_id: int) -> None:
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            return

        async with self.lock:
            if not self.active:
                return
            current_player = self._current_player()
            if not current_player or current_player.id != player_id:
                return
            await self._update_turn_message(
                ctx,
                current_player,
                f"⌛ **{current_player.display_name}** kifutott az időből!",
            )
            await punish_player(ctx, current_player, self.stake, message_tracker=self.track_message)
            await self._update_turn_message(
                ctx,
                current_player,
                f"💀 **{current_player.display_name}** kiesett!",
            )
            await self._advance_turn(ctx, eliminated=True)

    async def _advance_turn(self, ctx, eliminated: bool) -> None:
        if not self.active:
            return

        if eliminated:
            if self.players:
                self.players.pop(self.current_player_index)
            if self.current_player_index >= len(self.players):
                self.current_player_index = 0
        else:
            if self.players:
                self.current_player_index = (
                    self.current_player_index + 1
                ) % len(self.players)

        if len(self.players) == 1:
            winner = self.players[0]
            result_message = await ctx.send(
                embed=discord.Embed(
                    title="🏆 Russian Roulette V2",
                    description=f"**{winner.display_name}** nyerte a játékot!",
                    color=discord.Color.green(),
                )
            )
            await self._cleanup_messages(ctx, exclude_message_id=result_message.id)
            await self.stop()
            return
        if not self.players:
            result_message = await ctx.send("A játék véget ért, nincs több játékos.")
            await self._cleanup_messages(ctx, exclude_message_id=result_message.id)
            await self.stop()
            return

        await self._announce_turn(ctx)

    async def take_turn(self, ctx, member: discord.Member):
        if not self.active:
            await ctx.send("Nincs aktív rulett játék.")
            return

        if not self.voice_client or not self.voice_client.channel:
            await ctx.send("Nem vagyok hangcsatornában.")
            return

        if not member.voice or member.voice.channel != self.voice_client.channel:
            await ctx.send("Csak azonos hangcsatornában játszhatsz!")
            return

        if member not in self.players:
            await ctx.send("Nem vagy a játékosok listáján.")
            return

        current_player = self._current_player()
        if not current_player or current_player.id != member.id:
            await ctx.send("Most nem te jössz!")
            return

        async with self.lock:
            await self._cancel_turn_timer()
            cock = discord.FFmpegPCMAudio("/app/roulette_sounds/cock.mp3", options="-vn")
            self.mixer.add_sfx(cock)
            await asyncio.sleep(2)

            hit = self._roll_hit()
            if hit:
                bang = discord.FFmpegPCMAudio("/app/roulette_sounds/bang.mp3", options="-vn")
                self.mixer.add_sfx(bang)
                await asyncio.sleep(0.5)
                await punish_player(ctx, member, self.stake, message_tracker=self.track_message)
                await self._update_turn_message(
                    ctx, member, f"💥 **{member.display_name}** megkapta a lövést!"
                )
                await self._advance_turn(ctx, eliminated=True)
            else:
                click = discord.FFmpegPCMAudio("/app/roulette_sounds/click.mp3", options="-vn")
                self.mixer.add_sfx(click)
                await self._update_turn_message(
                    ctx,
                    member,
                    f"✅ **{member.display_name}** túlélte ezt a kört!",
                )
                await self._advance_turn(ctx, eliminated=False)

    async def _cleanup_messages(self, ctx, *, exclude_message_id: Optional[int] = None) -> None:
        if not self.messages_to_cleanup:
            return
        unique_messages = {}
        for message in self.messages_to_cleanup:
            if not message:
                continue
            if exclude_message_id and message.id == exclude_message_id:
                continue
            unique_messages[message.id] = message
        for message in unique_messages.values():
            try:
                await message.delete()
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                continue


# --- SEGÉDFÜGGVÉNYEK ---

def find_local_music(query):
    if not os.path.exists("music"):
        return None

    files = [f for f in os.listdir("music") if f.endswith((".mp3", ".wav", ".m4a"))]

    if query in files:
        return query

    query_lower = query.lower()
    for f in files:
        if query_lower in f.lower():
            return f

    return None


def get_audio_files(folder: str):
    if not os.path.exists(folder):
        return []
    return [f for f in os.listdir(folder) if f.endswith(".mp3")]


def select_prank_file():
    if prank_mode == "jimmy":
        candidates = [("jimmy", f) for f in get_audio_files("jimmy")]
    elif prank_mode == "mixed":
        candidates = [("sounds", f) for f in get_audio_files("sounds")] + [
            ("jimmy", f) for f in get_audio_files("jimmy")
        ]
    else:
        candidates = [("sounds", f) for f in get_audio_files("sounds")]

    if not candidates:
        return None

    folder, filename = random.choice(candidates)
    return os.path.join(folder, filename), filename


def get_mixer(voice_client: discord.VoiceClient) -> MixingAudioSource:
    guild_id = voice_client.guild.id
    mixer = mixers.get(guild_id)
    if mixer and voice_client.source is mixer:
        return mixer

    mixer = MixingAudioSource()
    mixers[guild_id] = mixer

    if voice_client.is_playing() or voice_client.is_paused():
        voice_client.stop()
    voice_client.play(mixer)
    return mixer


async def play_next_in_queue(ctx):
    guild_id = ctx.guild.id
    if guild_id in song_queues and song_queues[guild_id]:
        voice = ctx.guild.voice_client
        if not voice:
            return
        mixer = get_mixer(voice)
        source = song_queues[guild_id].pop(0)
        titles_queues[guild_id].pop(0)
        mixer.set_main_source(source, on_end=lambda: play_next_in_queue(ctx))
        await ctx.send(f"▶️ Következő zene: **{source.title}**")


def ensure_queue(guild_id: int):
    if guild_id not in song_queues:
        song_queues[guild_id] = []
        titles_queues[guild_id] = []


def is_owner(guild: discord.Guild, member: discord.Member) -> bool:
    return guild.owner_id == member.id


def can_kick(guild: discord.Guild) -> bool:
    me = guild.me
    if not me:
        return False
    return me.guild_permissions.kick_members


def can_move(guild: discord.Guild) -> bool:
    me = guild.me
    if not me:
        return False
    return me.guild_permissions.move_members


def build_intro_source():
    return discord.FFmpegPCMAudio(
        "/app/roulette_sounds/intro.mp3",
        before_options="-stream_loop -1",
        options="-vn",
    )


def build_sfx_source(path: str):
    return discord.FFmpegPCMAudio(path, options="-vn")


async def punish_player(
    ctx,
    member: discord.Member,
    stake: str,
    *,
    message_tracker: Optional[Callable[[discord.Message], None]] = None,
):
    async def send_and_track(message: str) -> None:
        response = await ctx.send(message)
        if message_tracker:
            message_tracker(response)

    if stake == "kick":
        if is_owner(ctx.guild, member):
            await send_and_track("👑 A szerver tulajdonosa immunis a kickre!")
            return
        if not can_kick(ctx.guild):
            await send_and_track("❌ Nincs jogom kirúgni a játékost.")
            return
        try:
            await ctx.guild.kick(member, reason="Russian Roulette V2")
        except discord.Forbidden:
            await send_and_track("❌ Nem tudom kirúgni a játékost (permission hiba).")
        return

    if not can_move(ctx.guild):
        await send_and_track("❌ Nincs jogom kidobni a hangcsatornából.")
        return
    try:
        await member.move_to(None)
    except discord.Forbidden:
        await send_and_track("❌ Nem tudom kidobni a hangcsatornából (permission hiba).")


# --- MONDÁSOK ---
QUOTES_FILE_PATH = "/app/quotes/mondasok.txt"
QUOTE_TIMESTAMP_REGEX = re.compile(r"^\[\d{2}\.\d{2}\.\d{4} \d{2}:\d{2}\]")


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
        print(f"[INTERNAL API] Új videó érkezett: {title} ({url}) feltöltő: {uploader}")
        message = f"{url}\n**{title}**"
        await channel.send(message)
        return web.Response(status=200, text="Video shared successfully")
    except Exception as e:
        return web.Response(status=500, text=f"Failed to send message: {e}")


async def start_internal_server():
    await bot.wait_until_ready()
    app = web.Application()
    app.add_routes([web.post("/share-video", handle_share_video)])

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", INTERNAL_API_PORT)
    await site.start()
    bot.internal_api_runner = runner
    print(f"Internal API running on 0.0.0.0:{INTERNAL_API_PORT}")


# --- AUTOMATA IJESZTGETŐS LOOP ---
async def prank_loop():
    await bot.wait_until_ready()
    while not bot.is_closed():
        wait_time = random.randint(MIN_TIME, MAX_TIME)
        print(f"👻 A szellem vár {wait_time} másodpercet...")
        await asyncio.sleep(wait_time)

        if not prank_enabled:
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

        if target_channel:
            print(
                f"😈 Ijesztgetés ({selected_file}) itt: {target_guild.name} -> {target_channel.name}"
            )

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

                if created and not mixer.main_source:
                    await voice_client.disconnect()
            except Exception as e:
                print(f"❌ Hiba: {e}")


@bot.event
async def on_ready():
    print(f"Bejelentkezve mint: {bot.user.name}")
    if not getattr(bot, "prank_task_started", False):
        bot.loop.create_task(prank_loop())
        bot.prank_task_started = True
    if not getattr(bot, "internal_server_started", False):
        bot.loop.create_task(start_internal_server())
        bot.internal_server_started = True
    if not getattr(bot, "daily_quote_task_started", False):
        daily_quote_task.start()
        bot.daily_quote_task_started = True


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
                if guild_id in song_queues:
                    del song_queues[guild_id]
                if guild_id in titles_queues:
                    del titles_queues[guild_id]
                await vc.disconnect()
        finally:
            afktasks.pop(guild_id, None)

    afktasks[guild_id] = bot.loop.create_task(disconnect_if_empty(bot_channel))


@bot.command(name="help")
async def help_command(ctx):
    embed = discord.Embed(
        title="A Király Parancsai",
        description="Itt láthatod, hogyan tudsz irányítani.",
        color=discord.Color.gold(),
    )
    embed.add_field(
        name="🎵 Zene (Music)",
        value=(
            "**!play <url/cím>**: Lejátszás YouTube-ról, Spotify-ról vagy helyi fájlból.\n"
            "**!skip**: Jelenlegi zene átugrása.\n"
            "**!pause** / **!resume**: Szünet / Folytatás.\n"
            "**!queue**: Lejátszási lista megtekintése.\n"
            "**!sajat-zenek**: A 'music' mappában lévő fájlok listázása.\n"
            "**!join** / **!leave**: Belépés és kilépés."
        ),
        inline=False,
    )
    embed.add_field(
        name="👻 Szórakozás (Fun)",
        value=(
            "**!mondd <szöveg>**: Felolvassa a szöveget (TTS).\n"
            "**!rulett**: Orosz rulett (Vigyázz, kidobhat!).\n"
            "**!rulett2**: Orosz rulett V2 (valósidejű hangokkal).\n"
            "**!titkosteszt**: Egy random hang azonnali bejátszása (sima)."
        ),
        inline=False,
    )
    embed.add_field(
        name="👑 Admin / Jimmy Mód (Admin Only)",
        value=(
            "**!Jimmy mód**: Csak Jimmy zenék bejátszása random időközönként.\n"
            "**!Normál mód**: Csak sima ijesztések bejátszása.\n"
            "**!Vegyes mód**: Jimmy és sima hangok vegyesen.\n"
            "**!Random-bejátszás <on/off>**: Az automata bejátszás ki/bekapcsolása.\n"
            "**!Jimmyteszt**: Egy random Jimmy hang azonnali bejátszása.\n"
            "**!mondas_teszt**: A nap mondása tesztelése (azonnali küldés)."
        ),
        inline=False,
    )
    await ctx.send(embed=embed)


@bot.command(name="join")
async def join(ctx):
    if not ctx.message.author.voice:
        await ctx.send("Nem vagy bent egy hangcsatornában sem!")
        return
    channel = ctx.message.author.voice.channel
    if ctx.voice_client is None:
        await channel.connect()
    else:
        await ctx.voice_client.move_to(channel)


@bot.command(name="mondd")
async def mondd(ctx, *, text: str):
    if not ctx.message.author.voice:
        await ctx.send("Nem vagy bent egy hangcsatornában sem!")
        return

    voice_client = ctx.voice_client
    channel = ctx.message.author.voice.channel

    if voice_client and voice_client.is_playing():
        await ctx.send("Várd meg, míg a jelenlegi lejátszás véget ér!")
        return

    if not voice_client:
        voice_client = await channel.connect()
    else:
        await voice_client.move_to(channel)

    tts_file = "tts_temp.mp3"
    try:
        tts = gTTS(text=text, lang="hu")
        tts.save(tts_file)

        mixer = get_mixer(voice_client)
        mixer.set_main_source(discord.FFmpegPCMAudio(tts_file, options="-vn"))

        while mixer.main_source:
            await asyncio.sleep(1)
    finally:
        if os.path.exists(tts_file):
            os.remove(tts_file)


@bot.command(name="play")
async def play(ctx, *, url):
    guild_id = ctx.guild.id
    if not ctx.voice_client:
        if ctx.message.author.voice:
            await ctx.message.author.voice.channel.connect()
        else:
            await ctx.send("Lépj be egy hangcsatornába előbb!")
            return

    ensure_queue(guild_id)

    async with ctx.typing():
        player = None

        if not url.startswith("http"):
            local_filename = find_local_music(url)
            if local_filename:
                file_path = os.path.join("music", local_filename)
                source = discord.FFmpegPCMAudio(file_path, options="-vn")
                player = LocalFileSource(source, title=local_filename)
                await ctx.send(f"💿 Helyi zene megtalálva: **{local_filename}**")

        if player is None:
            search_query = url
            if "spotify.com" in url and "track" in url:
                try:
                    track = sp.track(url)
                    artist_name = track["artists"][0]["name"]
                    track_name = track["name"]
                    search_query = f"ytsearch:{artist_name} - {track_name}"
                    await ctx.send(
                        f"🎵 Spotify: **{artist_name} - {track_name}** keresése..."
                    )
                except Exception:
                    await ctx.send("Hiba a Spotify link feldolgozásakor.")
                    return
            elif not url.startswith("http"):
                search_query = f"ytsearch:{url}"

            try:
                player = await YTDLSource.from_url(search_query, loop=bot.loop, stream=True)
            except Exception as e:
                await ctx.send(f"Hiba a letöltésnél: {e}")
                return

        voice_channel = ctx.voice_client
        mixer = get_mixer(voice_channel)
        if mixer.main_source or voice_channel.is_paused():
            song_queues[guild_id].append(player)
            titles_queues[guild_id].append(player.title)
            await ctx.send(f"✅ Sorba állítva: **{player.title}**")
        else:
            mixer.set_main_source(player, on_end=lambda: play_next_in_queue(ctx))
            await ctx.send(f"Most szól: **{player.title}**")


@bot.command(name="rulett")
@commands.has_permissions(administrator=True)
async def rulett(ctx):
    if not ctx.message.author.voice:
        await ctx.send("Nem vagy bent egy hangcsatornában sem!")
        return

    voice_client = ctx.voice_client

    if not voice_client or not voice_client.channel:
        await ctx.send("Nem vagyok hangcsatornában.")
        return

    if ctx.message.author.voice.channel != voice_client.channel:
        await ctx.send("Csak abban a csatornában használhatod, ahol én is vagyok!")
        return

    await ctx.send("Bang! 🔫")

    for member in list(voice_client.channel.members):
        if member == ctx.guild.me:
            continue
        if random.randint(1, 6) == 1:
            try:
                await member.move_to(None)
            except discord.Forbidden:
                await ctx.send(f"Nem tudom kirúgni: {member.display_name}")


@rulett.error
async def rulett_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("Ehhez a parancshoz admin jogosultság kell!")


@bot.command(name="rulett2")
async def rulett2(ctx):
    if not ctx.message.author.voice:
        await ctx.send("Nem vagy bent egy hangcsatornában sem!")
        return

    voice_client = ctx.voice_client
    channel = ctx.message.author.voice.channel

    if not voice_client:
        voice_client = await channel.connect()
    else:
        await voice_client.move_to(channel)

    game = roulette_games.get(ctx.guild.id)
    if game and game.active:
        await ctx.send("Már fut egy rulett játék ebben a szerverben!")
        return
    game = RouletteGame(ctx.guild.id)
    game.track_message(ctx.message)

    def mode_check(message):
        return message.author == ctx.author and message.channel == ctx.channel

    await game.send_and_track(
        ctx, "Mód választás: 1 = pörgés minden körben, 2 = egyszeri pörgés"
    )
    while True:
        try:
            mode_msg = await bot.wait_for("message", check=mode_check, timeout=60)
        except asyncio.TimeoutError:
            await game.send_and_track(ctx, "⏱️ Nem érkezett válasz időben.")
            return
        game.track_message(mode_msg)
        mode_value = mode_msg.content.strip()
        if mode_value in {"1", "2"}:
            break
        await game.send_and_track(ctx, "❌ Érvénytelen mód. Használd: 1 vagy 2.")

    await game.send_and_track(ctx, "Tét választás: kick / disconnect")
    while True:
        try:
            stake_msg = await bot.wait_for("message", check=mode_check, timeout=60)
        except asyncio.TimeoutError:
            await game.send_and_track(ctx, "⏱️ Nem érkezett válasz időben.")
            return
        game.track_message(stake_msg)
        stake_value = stake_msg.content.strip().lower()
        if stake_value in {"kick", "disconnect"}:
            break
        await game.send_and_track(ctx, "❌ Érvénytelen tét. Használd: kick vagy disconnect.")

    mixer = get_mixer(voice_client)
    mixer.set_main_source(build_intro_source())

    started = await game.start(ctx, int(mode_value), stake_value, voice_client, mixer)
    if not started:
        return
    roulette_games[ctx.guild.id] = game

    await game.send_and_track(
        ctx,
        embed=discord.Embed(
            title="🎲 Russian Roulette V2",
            description="Írd be: **!énjövök** hogy lőj egyet.",
            color=discord.Color.red(),
        )
    )


@bot.command(name="énjövök")
async def en_jovok(ctx):
    game = roulette_games.get(ctx.guild.id)
    if not game or not game.active:
        await ctx.send("Nincs aktív rulett játék.")
        return
    game.track_message(ctx.message)
    await game.take_turn(ctx, ctx.author)


# --- PRANK PARANCSOK ---
@bot.command(name="Random-bejátszás")
@commands.has_permissions(administrator=True)
async def random_bejatszas(ctx, state: str):
    global prank_enabled
    state_lower = state.lower()
    if state_lower not in {"on", "off"}:
        await ctx.send("Használat: !Random-bejátszás <on/off>")
        return
    prank_enabled = state_lower == "on"
    status = "bekapcsolva" if prank_enabled else "kikapcsolva"
    await ctx.send(f"✅ Automata bejátszás {status}.")


@random_bejatszas.error
async def random_bejatszas_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("Ehhez a parancshoz admin jogosultság kell!")
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("Használat: !Random-bejátszás <on/off>")


@bot.command(name="Jimmy")
@commands.has_permissions(administrator=True)
async def jimmy_mod(ctx, mode: str):
    global prank_mode
    if mode.lower() != "mód":
        await ctx.send("Használat: !Jimmy mód")
        return
    prank_mode = "jimmy"
    await ctx.send("✅ Jimmy mód aktiválva.")


@jimmy_mod.error
async def jimmy_mod_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("Ehhez a parancshoz admin jogosultság kell!")
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("Használat: !Jimmy mód")


@bot.command(name="Normál")
@commands.has_permissions(administrator=True)
async def normal_mod(ctx, mode: str):
    global prank_mode
    if mode.lower() != "mód":
        await ctx.send("Használat: !Normál mód")
        return
    prank_mode = "normal"
    await ctx.send("✅ Normál mód aktiválva.")


@normal_mod.error
async def normal_mod_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("Ehhez a parancshoz admin jogosultság kell!")
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("Használat: !Normál mód")


@bot.command(name="Vegyes")
@commands.has_permissions(administrator=True)
async def vegyes_mod(ctx, mode: str):
    global prank_mode
    if mode.lower() != "mód":
        await ctx.send("Használat: !Vegyes mód")
        return
    prank_mode = "mixed"
    await ctx.send("✅ Vegyes mód aktiválva.")


@vegyes_mod.error
async def vegyes_mod_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("Ehhez a parancshoz admin jogosultság kell!")
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("Használat: !Vegyes mód")


@bot.command(name="sajat-zenek")
async def sajat_zenek(ctx):
    if not os.path.exists("music"):
        await ctx.send("❌ Még nincs 'music' mappa létrehozva.")
        return

    files = [f for f in os.listdir("music") if f.endswith((".mp3", ".wav", ".m4a"))]

    if not files:
        await ctx.send("📂 A 'music' mappa üres.")
        return

    files_str = "\n".join([f"- {f}" for f in files])
    await ctx.send(
        f"**📂 Elérhető saját zenék:**\n{files_str}\n\n*Lejátszáshoz: !play <fájlnév részlete>*"
    )


@bot.command(name="skip")
async def skip(ctx):
    voice_client = ctx.voice_client
    if voice_client and (voice_client.is_playing() or voice_client.is_paused()):
        mixer = get_mixer(voice_client)
        mixer.set_main_source(None)
        await play_next_in_queue(ctx)
        await ctx.send("⏭️ Zene átugorva!")


@bot.command(name="pause")
async def pause(ctx):
    if ctx.voice_client and ctx.voice_client.is_playing():
        ctx.voice_client.pause()
        await ctx.send("⏸️ Zene megállítva.")


@bot.command(name="resume")
async def resume(ctx):
    if ctx.voice_client and ctx.voice_client.is_paused():
        ctx.voice_client.resume()
        await ctx.send("▶️ Zene folytatása.")


@bot.command(name="queue")
async def queue(ctx):
    guild_id = ctx.guild.id
    if guild_id in titles_queues and len(titles_queues[guild_id]) > 0:
        list_str = "\n".join(
            [f"{i + 1}. {title}" for i, title in enumerate(titles_queues[guild_id])]
        )
        await ctx.send(f"**Lejátszási lista:**\n{list_str}")
    else:
        await ctx.send("A lista jelenleg üres.")


@bot.command(name="leave")
async def leave(ctx):
    voice_client = ctx.voice_client
    if voice_client:
        guild_id = ctx.guild.id
        if guild_id in afktasks:
            afktasks[guild_id].cancel()
            del afktasks[guild_id]
        if guild_id in song_queues:
            del song_queues[guild_id]
        if guild_id in titles_queues:
            del titles_queues[guild_id]
        mixers.pop(guild_id, None)
        roulette_games.pop(guild_id, None)
        await voice_client.disconnect()
        await ctx.send("👋 Most már ez vagyok én, egy süllyedő hajó.")


@bot.command(name="titkosteszt")
@commands.has_permissions(administrator=True)
async def titkosteszt(ctx):
    if not ctx.message.author.voice:
        await ctx.send("❌ Előbb lépj be egy csatornára öreg")
        return

    sound_files = get_audio_files("sounds")
    if not sound_files:
        await ctx.send("❌ Hiba: Nincs 'sounds' mappa vagy üres!")
        return

    selected_file = random.choice(sound_files)
    file_path = os.path.join("sounds", selected_file)

    await ctx.send(f"😈 Teszt indul! Lejátszás: `{selected_file}`")

    try:
        channel = ctx.message.author.voice.channel
        voice_client = ctx.voice_client
        created = False

        if not voice_client:
            voice_client = await channel.connect()
            created = True
        else:
            await voice_client.move_to(channel)

        mixer = get_mixer(voice_client)
        mixer.add_sfx(discord.FFmpegPCMAudio(file_path, options="-vn"))

        while mixer.has_sfx():
            await asyncio.sleep(1)

        if created and not mixer.main_source:
            await voice_client.disconnect()
        await ctx.send("👻 Átvirrasztott éjszakák, száz el nem mondott szó.")

    except Exception as e:
        await ctx.send(f"❌ Hiba történt a teszt közben: {e}")


@titkosteszt.error
async def titkosteszt_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("Ehhez a parancshoz admin jogosultság kell!")


@bot.command(name="mondas_teszt")
@commands.has_permissions(administrator=True)
async def mondas_teszt(ctx):
    await send_daily_quote(ctx.channel)


@mondas_teszt.error
async def mondas_teszt_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("Ehhez a parancshoz admin jogosultság kell!")


@bot.command(name="Jimmyteszt")
@commands.has_permissions(administrator=True)
async def jimmyteszt(ctx):
    if not ctx.message.author.voice:
        await ctx.send("❌ Előbb lépj be egy csatornára öreg")
        return

    jimmy_files = get_audio_files("jimmy")
    if not jimmy_files:
        await ctx.send("❌ Hiba: Nincs 'jimmy' mappa vagy üres!")
        return

    selected_file = random.choice(jimmy_files)
    file_path = os.path.join("jimmy", selected_file)

    await ctx.send(f"😈 Teszt indul! Lejátszás: `{selected_file}`")

    try:
        channel = ctx.message.author.voice.channel
        voice_client = ctx.voice_client
        created = False

        if not voice_client:
            voice_client = await channel.connect()
            created = True
        else:
            await voice_client.move_to(channel)

        mixer = get_mixer(voice_client)
        mixer.add_sfx(discord.FFmpegPCMAudio(file_path, options="-vn"))

        while mixer.has_sfx():
            await asyncio.sleep(1)

        if created and not mixer.main_source:
            await voice_client.disconnect()
        await ctx.send("👻 Átvirrasztott éjszakák, száz el nem mondott szó.")

    except Exception as e:
        await ctx.send(f"❌ Hiba történt a teszt közben: {e}")


@jimmyteszt.error
async def jimmyteszt_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("Ehhez a parancshoz admin jogosultság kell!")


bot.run(TOKEN)
