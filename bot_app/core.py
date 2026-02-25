import asyncio
import audioop
import calendar
import datetime
import json
import os
import random
import re
import threading
import time
from typing import Callable, Optional
from uuid import uuid4

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
RADNAI_ALERT_CHANNEL_ID = 416599669355970560
RADNAI_CHAT_ALERT_REPEAT_COUNT = 5
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PRANK_STATE_FILE = os.path.join(BASE_DIR, "prank_state.json")
SCHEDULED_MESSAGES_FILE = os.path.join(BASE_DIR, "scheduled_messages.json")
SCHEDULER_POLL_INTERVAL_SECONDS = 5

# --- PRANK ÁLLAPOT ---
prank_enabled = True
prank_mode = "normal"  # normal | jimmy | mixed
last_prank_date: Optional[datetime.date] = None

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
play_locks = {}
afktasks = {}
mixers = {}
roulette_games = {}
radnai_alert_lock = asyncio.Lock()
radnai_alert_stop_event: Optional[asyncio.Event] = None
scheduled_messages_lock = asyncio.Lock()
scheduled_messages: list[dict] = []

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
    "extractor_args": {"youtube": {"player_client": ["web", "android", "ios"]}},
    "retries": 4,
    "fragment_retries": 4,
    "extractor_retries": 2,
    "socket_timeout": 15,
    "cachedir": False,
}
YTDL_FETCH_TIMEOUT_SECONDS = 70

ytdl = yt_dlp.YoutubeDL(ytdl_format_options)


def cleanup_audio_source(source: Optional[discord.AudioSource]) -> None:
    if not source:
        return
    cleanup = getattr(source, "cleanup", None)
    if not callable(cleanup):
        return
    try:
        cleanup()
    except Exception as e:
        print(f"Audio source cleanup error: {e}")


def build_ffmpeg_options(*, stream: bool, data: Optional[dict] = None) -> dict:
    options = {"options": "-vn"}
    if not stream:
        return options

    before_options_parts = [
        "-reconnect 1",
        "-reconnect_streamed 1",
        "-reconnect_delay_max 5",
    ]
    if data:
        headers = data.get("http_headers")
        if headers:
            header_blob = "".join(f"{key}: {value}\r\n" for key, value in headers.items())
            escaped_header_blob = header_blob.replace('"', '\\"')
            before_options_parts.append(f'-headers "{escaped_header_blob}"')
    options["before_options"] = " ".join(before_options_parts)
    return options


def clear_guild_queue(guild_id: int) -> None:
    queued_sources = song_queues.pop(guild_id, [])
    titles_queues.pop(guild_id, None)
    for source in queued_sources:
        cleanup_audio_source(source)


def get_play_lock(guild_id: int) -> asyncio.Lock:
    lock = play_locks.get(guild_id)
    if lock is None:
        lock = asyncio.Lock()
        play_locks[guild_id] = lock
    return lock


class MixingAudioSource(discord.AudioSource):
    def __init__(self, main_source: Optional[discord.AudioSource] = None):
        self.main_source = main_source
        self.sfx_sources = []
        self.sample_width = 2
        self._lock = threading.Lock()
        self._on_main_end = None

    def set_main_source(self, source: Optional[discord.AudioSource], on_end=None):
        old_source = None
        with self._lock:
            old_source = self.main_source
            self.main_source = source
            self._on_main_end = on_end
        if old_source and old_source is not source:
            cleanup_audio_source(old_source)

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
        ended_sources = []

        with self._lock:
            if self.main_source:
                main_data = self.main_source.read()
                if not main_data:
                    ended_sources.append(self.main_source)
                    on_end = self._on_main_end
                    self.main_source = None
                    self._on_main_end = None
            sfx_remaining = []
            for source in self.sfx_sources:
                data = source.read()
                if data:
                    sfx_datas.append(data)
                    sfx_remaining.append(source)
                else:
                    ended_sources.append(source)
            self.sfx_sources = sfx_remaining

        for source in ended_sources:
            cleanup_audio_source(source)

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
    def __init__(self, source, *, data, temp_file: Optional[str] = None, volume=0.5):
        super().__init__(source, volume)
        self.data = data
        self.title = data.get("title") or "Ismeretlen cim"
        self.url = data.get("url")
        self.temp_file = temp_file

    @staticmethod
    def _resolve_downloaded_filename(data: dict) -> str:
        requested_downloads = data.get("requested_downloads") or []
        for download in requested_downloads:
            file_path = download.get("filepath")
            if file_path:
                return file_path
        return ytdl.prepare_filename(data)

    @classmethod
    async def from_url(cls, url, *, loop=None, stream=False):
        loop = loop or asyncio.get_event_loop()
        try:
            data = await asyncio.wait_for(
                loop.run_in_executor(None, lambda: ytdl.extract_info(url, download=not stream)),
                timeout=YTDL_FETCH_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError as e:
            raise RuntimeError("A zene letoltese tul sok ideig tartott, probald ujra.") from e
        if not data:
            raise RuntimeError("Nem talaltam lejatszhato forrast.")
        if "entries" in data:
            data = next((entry for entry in data["entries"] if entry), None)
            if not data:
                raise RuntimeError("Nem talaltam lejatszhato forrast.")

        filename = data["url"] if stream else cls._resolve_downloaded_filename(data)
        ffmpeg_source = discord.FFmpegPCMAudio(
            filename, **build_ffmpeg_options(stream=stream, data=data)
        )
        temp_file = None if stream else filename
        return cls(ffmpeg_source, data=data, temp_file=temp_file)

    def cleanup(self):
        try:
            super().cleanup()
        finally:
            if self.temp_file and os.path.exists(self.temp_file):
                try:
                    os.remove(self.temp_file)
                except OSError as e:
                    print(f"Temp audio cleanup failed ({self.temp_file}): {e}")
                finally:
                    self.temp_file = None


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

    async def _shutdown_voice(self) -> None:
        if not self.voice_client:
            return
        guild_id = self.voice_client.guild.id
        if guild_id in afktasks:
            afktasks[guild_id].cancel()
            del afktasks[guild_id]
        clear_guild_queue(guild_id)
        mixers.pop(guild_id, None)
        roulette_games.pop(guild_id, None)
        if self.voice_client.is_playing() or self.voice_client.is_paused():
            self.voice_client.stop()
        await self.voice_client.disconnect()

    async def end_game(self, ctx, result_message: discord.Message) -> None:
        await self._cleanup_messages(ctx, exclude_message_id=result_message.id)
        await self.stop()
        await self._shutdown_voice()

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
            await self.end_game(ctx, result_message)
            return
        if not self.players:
            result_message = await ctx.send("A játék véget ért, nincs több játékos.")
            await self.end_game(ctx, result_message)
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


def load_last_prank_date() -> Optional[datetime.date]:
    if not os.path.exists(PRANK_STATE_FILE):
        return None

    try:
        with open(PRANK_STATE_FILE, "r", encoding="utf-8") as state_file:
            data = json.load(state_file)
    except (OSError, json.JSONDecodeError):
        return None

    raw_date = data.get("last_prank_date")
    if not raw_date:
        return None

    try:
        return datetime.date.fromisoformat(raw_date)
    except ValueError:
        return None


def save_last_prank_date(value: datetime.date) -> None:
    try:
        with open(PRANK_STATE_FILE, "w", encoding="utf-8") as state_file:
            json.dump({"last_prank_date": value.isoformat()}, state_file)
    except OSError as e:
        print(f"Failed to save prank state: {e}")




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


