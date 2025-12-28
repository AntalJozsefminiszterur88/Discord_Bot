import asyncio
import datetime
import os
import random
import time
from typing import Optional

import discord
import spotipy
import yt_dlp
from discord.ext import commands, tasks
from dotenv import load_dotenv
from gtts import gTTS
from spotipy.oauth2 import SpotifyClientCredentials
from aiohttp import web
import re

# --- BEÁLLÍTÁSOK ---
MIN_TIME = 1800  # Minimum 30 perc
MAX_TIME = 7200  # Maximum 2 óra
# -------------------
INTERNAL_API_PORT = 5050  # Port az internal API-hoz (Docker konténeren belül)
TARGET_CHANNEL_ID: Optional[int] = 1370685414578327594
QUOTES_CHANNEL_ID = 416599669355970560

# --- PRANK ÁLLAPOT ---
prank_enabled = True
prank_mode = 'normal'  # normal | jimmy | mixed

load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')
SPOTIPY_CLIENT_ID = os.getenv('SPOTIPY_CLIENT_ID')
SPOTIPY_CLIENT_SECRET = os.getenv('SPOTIPY_CLIENT_SECRET')

sp = spotipy.Spotify(auth_manager=SpotifyClientCredentials(client_id=SPOTIPY_CLIENT_ID,
                                                           client_secret=SPOTIPY_CLIENT_SECRET))

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

song_queues = {}
titles_queues = {}
afk_tasks = {}

# YT-DLP beállítások
ytdl_format_options = {
    'format': 'bestaudio/best',
    'outtmpl': '%(extractor)s-%(id)s-%(title)s.%(ext)s',
    'restrictfilenames': True,
    'noplaylist': True,
    'nocheckcertificate': True,
    'ignoreerrors': False,
    'logtostderr': False,
    'quiet': True,
    'no_warnings': True,
    'default_search': 'auto',
    'source_address': '0.0.0.0',
    'extractor_args': {'youtube': {'player_client': ['android', 'ios']}},
    'cachedir': False,
}

ffmpeg_options = {
    'options': '-vn',
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5'
}

ytdl = yt_dlp.YoutubeDL(ytdl_format_options)

# Osztály a YouTube streamhez
class YTDLSource(discord.PCMVolumeTransformer):
    def __init__(self, source, *, data, volume=0.5):
        super().__init__(source, volume)
        self.data = data
        self.title = data.get('title')
        self.url = data.get('url')

    @classmethod
    async def from_url(cls, url, *, loop=None, stream=False):
        loop = loop or asyncio.get_event_loop()
        data = await loop.run_in_executor(None, lambda: ytdl.extract_info(url, download=not stream))
        if 'entries' in data:
            data = data['entries'][0]
        filename = data['url'] if stream else ytdl.prepare_filename(data)
        return cls(discord.FFmpegPCMAudio(filename, **ffmpeg_options), data=data)

# ÚJ OSZTÁLY a helyi zenékhez (hogy legyen .title tulajdonsága)
class LocalFileSource(discord.PCMVolumeTransformer):
    def __init__(self, source, *, title, volume=0.5):
        super().__init__(source, volume)
        self.title = title

# --- SEGÉDFÜGGVÉNY A HELYI KERESÉSHEZ ---
def find_local_music(query):
    if not os.path.exists('music'):
        return None
    
    files = [f for f in os.listdir('music') if f.endswith(('.mp3', '.wav', '.m4a'))]
    
    # 1. Pontos egyezés keresése
    if query in files:
        return query
        
    # 2. Részleges keresés (nem számít a kisbetű/nagybetű)
    query_lower = query.lower()
    for f in files:
        if query_lower in f.lower():
            return f
            
    return None

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
    """Fogad egy videó megosztási kérést a webes backendtől."""
    try:
        data = await request.json()
    except Exception:
        return web.Response(status=400, text='Invalid JSON payload')

    url = data.get('url')
    title = data.get('title')
    uploader = data.get('uploader')

    if not all([url, title, uploader]):
        return web.Response(status=400, text='Missing url/title/uploader fields')

    if TARGET_CHANNEL_ID is None:
        return web.Response(status=500, text='TARGET_CHANNEL_ID is not configured')

    channel = bot.get_channel(TARGET_CHANNEL_ID)
    if channel is None:
        return web.Response(status=500, text='Target channel not found')

    try:
        print(f"[INTERNAL API] Új videó érkezett: {title} ({url}) feltöltő: {uploader}")
        message = f"{url}\n**{title}**"
        await channel.send(message)
        return web.Response(status=200, text='Video shared successfully')
    except Exception as e:
        return web.Response(status=500, text=f'Failed to send message: {e}')


async def start_internal_server():
    """Elindítja a belső HTTP API szervert a Docker konténeren belül."""
    await bot.wait_until_ready()
    app = web.Application()
    app.add_routes([web.post('/share-video', handle_share_video)])

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', INTERNAL_API_PORT)
    await site.start()
    bot.internal_api_runner = runner
    print(f"Internal API running on 0.0.0.0:{INTERNAL_API_PORT}")

# --- PRANK SEGÉDEK ---
def get_audio_files(folder: str):
    if not os.path.exists(folder):
        return []
    return [f for f in os.listdir(folder) if f.endswith('.mp3')]


def select_prank_file():
    if prank_mode == 'jimmy':
        candidates = [('jimmy', f) for f in get_audio_files('jimmy')]
    elif prank_mode == 'mixed':
        candidates = [('sounds', f) for f in get_audio_files('sounds')] + [('jimmy', f) for f in get_audio_files('jimmy')]
    else:
        candidates = [('sounds', f) for f in get_audio_files('sounds')]

    if not candidates:
        return None

    folder, filename = random.choice(candidates)
    return os.path.join(folder, filename), filename

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
            print(f"😈 Ijesztgetés ({selected_file}) itt: {target_guild.name} -> {target_channel.name}")
            
            try:
                voice_client = target_guild.voice_client
                if not voice_client:
                    voice_client = await target_channel.connect()
                elif voice_client.channel != target_channel:
                    await voice_client.move_to(target_channel)
                
                source = discord.FFmpegPCMAudio(file_path)
                voice_client.play(source)
                while voice_client.is_playing():
                    await asyncio.sleep(1)
                await voice_client.disconnect()
            except Exception as e:
                print(f"❌ Hiba: {e}")

# --- ZENELŐ RÉSZ ---

def check_queue(ctx):
    guild_id = ctx.guild.id
    if guild_id in song_queues and len(song_queues[guild_id]) > 0:
        voice = ctx.guild.voice_client
        source = song_queues[guild_id].pop(0)
        titles_queues[guild_id].pop(0)
        voice.play(source, after=lambda e: check_queue(ctx))
        asyncio.run_coroutine_threadsafe(ctx.send(f'▶️ Következő zene: **{source.title}**'), bot.loop)

@bot.event
async def on_ready():
    print(f'Bejelentkezve mint: {bot.user.name}')
    if not getattr(bot, 'prank_task_started', False):
        bot.loop.create_task(prank_loop())
        bot.prank_task_started = True
    if not getattr(bot, 'internal_server_started', False):
        bot.loop.create_task(start_internal_server())
        bot.internal_server_started = True
    if not getattr(bot, 'daily_quote_task_started', False):
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
        if guild_id in afk_tasks:
            afk_tasks[guild_id].cancel()
            del afk_tasks[guild_id]
        return

    if before.channel != bot_channel or after.channel == bot_channel:
        return

    if guild_id in afk_tasks:
        afk_tasks[guild_id].cancel()

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
            afk_tasks.pop(guild_id, None)

    afk_tasks[guild_id] = bot.loop.create_task(disconnect_if_empty(bot_channel))

@bot.command(name='join')
async def join(ctx):
    if not ctx.message.author.voice:
        await ctx.send("Nem vagy bent egy hangcsatornában sem!")
        return
    channel = ctx.message.author.voice.channel
    if ctx.voice_client is None:
        await channel.connect()
    else:
        await ctx.voice_client.move_to(channel)


@bot.command(name='mondd')
async def mondd(ctx, *, text: str):
    if not ctx.message.author.voice:
        await ctx.send("Nem vagy bent egy hangcsatornában sem!")
        return

    voice_client = ctx.voice_client
    channel = ctx.message.author.voice.channel

    if voice_client and (voice_client.is_playing() or voice_client.is_paused()):
        await ctx.send("Várd meg, míg a jelenlegi lejátszás véget ér!")
        return

    if not voice_client:
        voice_client = await channel.connect()
    else:
        await voice_client.move_to(channel)

    tts_file = 'tts_temp.mp3'
    try:
        tts = gTTS(text=text, lang='hu')
        tts.save(tts_file)

        source = discord.FFmpegPCMAudio(tts_file)
        voice_client.play(source)

        while voice_client.is_playing():
            await asyncio.sleep(1)
    finally:
        if os.path.exists(tts_file):
            os.remove(tts_file)

# --- MÓDOSÍTOTT PLAY PARANCS ---
@bot.command(name='play')
async def play(ctx, *, url):
    guild_id = ctx.guild.id
    if not ctx.voice_client:
        if ctx.message.author.voice:
            await ctx.message.author.voice.channel.connect()
        else:
            await ctx.send("Lépj be egy hangcsatornába előbb!")
            return
            
    if guild_id not in song_queues:
        song_queues[guild_id] = []
        titles_queues[guild_id] = []

    async with ctx.typing():
        player = None
        
        # 1. Ellenőrzés: Ez egy helyi fájl?
        # Csak akkor keressen helyben, ha nem URL-t kapott
        if not url.startswith('http'):
            local_filename = find_local_music(url)
            if local_filename:
                # Találtunk helyi fájlt!
                file_path = os.path.join('music', local_filename)
                source = discord.FFmpegPCMAudio(file_path)
                player = LocalFileSource(source, title=local_filename)
                await ctx.send(f"💿 Helyi zene megtalálva: **{local_filename}**")

        # 2. Ha nem helyi fájl, akkor YouTube/Spotify
        if player is None:
            search_query = url
            if 'spotify.com' in url and 'track' in url:
                try:
                    track = sp.track(url)
                    artist_name = track['artists'][0]['name']
                    track_name = track['name']
                    search_query = f"ytsearch:{artist_name} - {track_name}"
                    await ctx.send(f'🎵 Spotify: **{artist_name} - {track_name}** keresése...')
                except:
                    await ctx.send("Hiba a Spotify link feldolgozásakor.")
                    return
            elif not url.startswith('http'):
                search_query = f"ytsearch:{url}"
            
            try:
                player = await YTDLSource.from_url(search_query, loop=bot.loop, stream=True)
            except Exception as e:
                await ctx.send(f"Hiba a letöltésnél: {e}")
                return

        # 3. Lejátszás vagy Queue
        voice_channel = ctx.voice_client
        if voice_channel.is_playing() or voice_channel.is_paused():
            song_queues[guild_id].append(player)
            titles_queues[guild_id].append(player.title)
            await ctx.send(f'✅ Sorba állítva: **{player.title}**')
        else:
            voice_channel.play(player, after=lambda e: check_queue(ctx))
            await ctx.send(f'Most szól: **{player.title}**')


@bot.command(name='rulett')
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


# --- PRANK PARANCSOK ---
@bot.command(name='Random-bejátszás')
@commands.has_permissions(administrator=True)
async def random_bejatszas(ctx, state: str):
    global prank_enabled
    state_lower = state.lower()
    if state_lower not in {'on', 'off'}:
        await ctx.send("Használat: !Random-bejátszás <on/off>")
        return
    prank_enabled = state_lower == 'on'
    status = "bekapcsolva" if prank_enabled else "kikapcsolva"
    await ctx.send(f"✅ Automata bejátszás {status}.")


@random_bejatszas.error
async def random_bejatszas_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("Ehhez a parancshoz admin jogosultság kell!")
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("Használat: !Random-bejátszás <on/off>")


@bot.command(name='Jimmy')
@commands.has_permissions(administrator=True)
async def jimmy_mod(ctx, mode: str):
    global prank_mode
    if mode.lower() != 'mód':
        await ctx.send("Használat: !Jimmy mód")
        return
    prank_mode = 'jimmy'
    await ctx.send("✅ Jimmy mód aktiválva.")


@jimmy_mod.error
async def jimmy_mod_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("Ehhez a parancshoz admin jogosultság kell!")
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("Használat: !Jimmy mód")


@bot.command(name='Normál')
@commands.has_permissions(administrator=True)
async def normal_mod(ctx, mode: str):
    global prank_mode
    if mode.lower() != 'mód':
        await ctx.send("Használat: !Normál mód")
        return
    prank_mode = 'normal'
    await ctx.send("✅ Normál mód aktiválva.")


@normal_mod.error
async def normal_mod_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("Ehhez a parancshoz admin jogosultság kell!")
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("Használat: !Normál mód")


@bot.command(name='Vegyes')
@commands.has_permissions(administrator=True)
async def vegyes_mod(ctx, mode: str):
    global prank_mode
    if mode.lower() != 'mód':
        await ctx.send("Használat: !Vegyes mód")
        return
    prank_mode = 'mixed'
    await ctx.send("✅ Vegyes mód aktiválva.")


@vegyes_mod.error
async def vegyes_mod_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("Ehhez a parancshoz admin jogosultság kell!")
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("Használat: !Vegyes mód")

# --- ÚJ PARANCS: SAJÁT ZENÉK LISTÁZÁSA ---
@bot.command(name='sajat-zenek')
async def sajat_zenek(ctx):
    if not os.path.exists('music'):
        await ctx.send("❌ Még nincs 'music' mappa létrehozva.")
        return
        
    files = [f for f in os.listdir('music') if f.endswith(('.mp3', '.wav', '.m4a'))]
    
    if not files:
        await ctx.send("📂 A 'music' mappa üres.")
        return
        
    # Szép lista formázása
    files_str = "\n".join([f"- {f}" for f in files])
    await ctx.send(f"**📂 Elérhető saját zenék:**\n{files_str}\n\n*Lejátszáshoz: !play <fájlnév részlete>*")

@bot.command(name='skip')
async def skip(ctx):
    voice_client = ctx.voice_client
    if voice_client and (voice_client.is_playing() or voice_client.is_paused()):
        voice_client.stop()
        await ctx.send("⏭️ Zene átugorva!")

@bot.command(name='pause')
async def pause(ctx):
    if ctx.voice_client and ctx.voice_client.is_playing():
        ctx.voice_client.pause()
        await ctx.send("⏸️ Zene megállítva.")

@bot.command(name='resume')
async def resume(ctx):
    if ctx.voice_client and ctx.voice_client.is_paused():
        ctx.voice_client.resume()
        await ctx.send("▶️ Zene folytatása.")

@bot.command(name='queue')
async def queue(ctx):
    guild_id = ctx.guild.id
    if guild_id in titles_queues and len(titles_queues[guild_id]) > 0:
        list_str = "\n".join([f"{i+1}. {title}" for i, title in enumerate(titles_queues[guild_id])])
        await ctx.send(f"**Lejátszási lista:**\n{list_str}")
    else:
        await ctx.send("A lista jelenleg üres.")

@bot.command(name='leave')
async def leave(ctx):
    voice_client = ctx.voice_client
    if voice_client:
        guild_id = ctx.guild.id
        if guild_id in afk_tasks:
            afk_tasks[guild_id].cancel()
            del afk_tasks[guild_id]
        if guild_id in song_queues: del song_queues[guild_id]
        if guild_id in titles_queues: del titles_queues[guild_id]
        await voice_client.disconnect()
        await ctx.send("👋 Most már ez vagyok én, egy süllyedő hajó.")

@bot.command(name='titkosteszt')
@commands.has_permissions(administrator=True)
async def titkosteszt(ctx):
    if not ctx.message.author.voice:
        await ctx.send("❌ Előbb lépj be egy csatornára öreg")
        return

    if ctx.voice_client and ctx.voice_client.is_playing():
        await ctx.send("❌ Jelenleg megy a zene, mit képzelsz?")
        return

    sound_files = get_audio_files('sounds')
    if not sound_files:
        await ctx.send("❌ Hiba: Nincs 'sounds' mappa vagy üres!")
        return

    selected_file = random.choice(sound_files)
    file_path = os.path.join('sounds', selected_file)

    await ctx.send(f"😈 Teszt indul! Lejátszás: `{selected_file}`")

    try:
        channel = ctx.message.author.voice.channel
        voice_client = ctx.voice_client
        
        if not voice_client:
            voice_client = await channel.connect()
        else:
            await voice_client.move_to(channel)
        
        source = discord.FFmpegPCMAudio(file_path)
        voice_client.play(source)

        while voice_client.is_playing():
            await asyncio.sleep(1)
        
        await voice_client.disconnect()
        await ctx.send("👻 Átvirrasztott éjszakák, száz el nem mondott szó.")

    except Exception as e:
        await ctx.send(f"❌ Hiba történt a teszt közben: {e}")


@titkosteszt.error
async def titkosteszt_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("Ehhez a parancshoz admin jogosultság kell!")


@bot.command(name='mondas_teszt')
@commands.has_permissions(administrator=True)
async def mondas_teszt(ctx):
    await send_daily_quote(ctx.channel)


@mondas_teszt.error
async def mondas_teszt_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("Ehhez a parancshoz admin jogosultság kell!")


@bot.command(name='Jimmyteszt')
@commands.has_permissions(administrator=True)
async def jimmyteszt(ctx):
    if not ctx.message.author.voice:
        await ctx.send("❌ Előbb lépj be egy csatornára öreg")
        return

    if ctx.voice_client and ctx.voice_client.is_playing():
        await ctx.send("❌ Jelenleg megy a zene, mit képzelsz?")
        return

    jimmy_files = get_audio_files('jimmy')
    if not jimmy_files:
        await ctx.send("❌ Hiba: Nincs 'jimmy' mappa vagy üres!")
        return

    selected_file = random.choice(jimmy_files)
    file_path = os.path.join('jimmy', selected_file)

    await ctx.send(f"😈 Teszt indul! Lejátszás: `{selected_file}`")

    try:
        channel = ctx.message.author.voice.channel
        voice_client = ctx.voice_client

        if not voice_client:
            voice_client = await channel.connect()
        else:
            await voice_client.move_to(channel)

        source = discord.FFmpegPCMAudio(file_path)
        voice_client.play(source)

        while voice_client.is_playing():
            await asyncio.sleep(1)

        await voice_client.disconnect()
        await ctx.send("👻 Átvirrasztott éjszakák, száz el nem mondott szó.")

    except Exception as e:
        await ctx.send(f"❌ Hiba történt a teszt közben: {e}")


@jimmyteszt.error
async def jimmyteszt_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("Ehhez a parancshoz admin jogosultság kell!")

bot.run(TOKEN)



