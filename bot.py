import discord
from discord.ext import commands
import yt_dlp
import asyncio
import os
import random
from typing import Optional
from dotenv import load_dotenv
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials
from aiohttp import web

# --- BEÁLLÍTÁSOK ---
MIN_TIME = 1800  # Minimum 30 perc
MAX_TIME = 7200  # Maximum 2 óra
# -------------------
INTERNAL_API_PORT = 5050  # Port az internal API-hoz (Docker konténeren belül)
TARGET_CHANNEL_ID: Optional[int] = 1370685414578327594

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
disconnect_timers = {}

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
    'cookiefile': 'cookies.txt',
    'http_headers': {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    },
    'extractor_args': {'youtube': {'player_client': ['web']}},
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

# --- AUTOMATA IJESZTGETŐS LOOP ---
async def prank_loop():
    await bot.wait_until_ready()
    while not bot.is_closed():
        wait_time = random.randint(MIN_TIME, MAX_TIME)
        print(f"👻 A szellem vár {wait_time} másodpercet...")
        await asyncio.sleep(wait_time)

        if not os.path.exists('sounds'):
            continue
        sound_files = [f for f in os.listdir('sounds') if f.endswith('.mp3')]
        if not sound_files:
            continue

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
            selected_file = random.choice(sound_files)
            file_path = os.path.join('sounds', selected_file)
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

async def disconnect_timer(ctx):
    await asyncio.sleep(300)
    guild_id = ctx.guild.id
    if ctx.voice_client and ctx.voice_client.is_connected() and not ctx.voice_client.is_playing():
        await ctx.voice_client.disconnect()
        if guild_id in song_queues: del song_queues[guild_id]
        if guild_id in titles_queues: del titles_queues[guild_id]
        await ctx.send("Leléptem csicskák!")

def check_queue(ctx):
    guild_id = ctx.guild.id
    if guild_id in song_queues and len(song_queues[guild_id]) > 0:
        if guild_id in disconnect_timers:
            disconnect_timers[guild_id].cancel()
            del disconnect_timers[guild_id]
        voice = ctx.guild.voice_client
        source = song_queues[guild_id].pop(0)
        titles_queues[guild_id].pop(0)
        voice.play(source, after=lambda e: check_queue(ctx))
        asyncio.run_coroutine_threadsafe(ctx.send(f'▶️ Következő zene: **{source.title}**'), bot.loop)
    else:
        disconnect_timers[guild_id] = bot.loop.create_task(disconnect_timer(ctx))

@bot.event
async def on_ready():
    print(f'Bejelentkezve mint: {bot.user.name}')
    if not getattr(bot, 'prank_task_started', False):
        bot.loop.create_task(prank_loop())
        bot.prank_task_started = True
    if not getattr(bot, 'internal_server_started', False):
        bot.loop.create_task(start_internal_server())
        bot.internal_server_started = True

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
            if guild_id in disconnect_timers:
                disconnect_timers[guild_id].cancel()
                del disconnect_timers[guild_id]
            voice_channel.play(player, after=lambda e: check_queue(ctx))
            await ctx.send(f'Most szól: **{player.title}**')

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
        if guild_id in song_queues: del song_queues[guild_id]
        if guild_id in titles_queues: del titles_queues[guild_id]
        await voice_client.disconnect()
        await ctx.send("👋 Most már ez vagyok én, egy süllyedő hajó.")

@bot.command(name='titkosteszt')
async def titkosteszt(ctx):
    if not ctx.message.author.voice:
        await ctx.send("❌ Előbb lépj be egy csatornára öreg")
        return

    if ctx.voice_client and ctx.voice_client.is_playing():
        await ctx.send("❌ Jelenleg megy a zene, mit képzelsz?")
        return

    if not os.path.exists('sounds') or not os.listdir('sounds'):
        await ctx.send("❌ Hiba: Nincs 'sounds' mappa vagy üres!")
        return
    
    sound_files = [f for f in os.listdir('sounds') if f.endswith('.mp3')]
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

bot.run(TOKEN)



