from bot_app.core import *
from bot_app.alerts import stop_radnai_alert
from bot_app.automation import send_daily_quote
import bot_app.core as core


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
            "**!join** / **!leave**: Belépés és kilépés.\n"
            "**!lábhoz**: Minden folyamat leállítása és leválasztás."
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
    embed.add_field(
        name="🧹 Moderation",
        value=(
            "**!consuela**: Bot uzenetek torlese az elmult 5 percbol az aktualis "
            "csatornaban."
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
    author_voice = ctx.message.author.voice
    if not author_voice or not author_voice.channel:
        await ctx.send("Lepj be egy hangcsatornaba elobb!")
        return

    target_channel = author_voice.channel
    me = ctx.guild.me
    if me:
        permissions = target_channel.permissions_for(me)
        if not permissions.connect:
            await ctx.send("Nem tudok csatlakozni ehhez a hangcsatornahoz (Connect hianyzik).")
            return
        if not permissions.speak:
            await ctx.send("Nincs beszed jogom ebben a hangcsatornaban (Speak hianyzik).")
            return

    if not ctx.voice_client:
        await target_channel.connect()
    elif ctx.voice_client.channel != target_channel:
        await ctx.voice_client.move_to(target_channel)

    ensure_queue(guild_id)

    play_lock = get_play_lock(guild_id)
    if play_lock.locked():
        await ctx.send("Mar folyamatban van egy zene betoltese, varj egy kicsit.")

    async with play_lock:
        async with ctx.typing():
            player = None

            if not url.startswith("http"):
                local_filename = find_local_music(url)
                if local_filename:
                    file_path = os.path.join("music", local_filename)
                    source = discord.FFmpegPCMAudio(file_path, options="-vn")
                    player = LocalFileSource(source, title=local_filename)
                    await ctx.send(f"Helyi zene megtalalva: **{local_filename}**")

            if player is None:
                search_query = url
                if "spotify.com" in url and "track" in url:
                    try:
                        track = sp.track(url)
                        artist_name = track["artists"][0]["name"]
                        track_name = track["name"]
                        search_query = f"ytsearch:{artist_name} - {track_name}"
                        await ctx.send(
                            f"Spotify: **{artist_name} - {track_name}** keresese..."
                        )
                    except Exception:
                        await ctx.send("Hiba a Spotify link feldolgozasakor.")
                        return
                elif not url.startswith("http"):
                    search_query = f"ytsearch:{url}"

                candidate_queries = [search_query]
                if search_query.startswith("ytsearch:"):
                    expanded_query = search_query.replace("ytsearch:", "ytsearch5:", 1)
                    try:
                        search_data = await asyncio.wait_for(
                            bot.loop.run_in_executor(
                                None, lambda: ytdl.extract_info(expanded_query, download=False)
                            ),
                            timeout=25,
                        )
                        entries = search_data.get("entries") if isinstance(search_data, dict) else None
                        extracted_candidates = []
                        for entry in entries or []:
                            if not entry:
                                continue
                            candidate = entry.get("webpage_url") or entry.get("url")
                            if candidate and candidate not in extracted_candidates:
                                extracted_candidates.append(candidate)
                        if extracted_candidates:
                            candidate_queries = extracted_candidates[:5]
                    except Exception as e:
                        print(f"Search expansion failed for '{expanded_query}': {e}")

                download_errors = []
                for candidate in candidate_queries:
                    try:
                        player = await YTDLSource.from_url(candidate, loop=bot.loop, stream=False)
                        break
                    except Exception as e:
                        download_errors.append(e)
                        print(f"Download mode failed for '{candidate}': {e}")

                if player is None:
                    for candidate in candidate_queries:
                        try:
                            player = await YTDLSource.from_url(candidate, loop=bot.loop, stream=True)
                            await ctx.send("A letoltes nem sikerult, stream modra valtottam.")
                            break
                        except Exception as stream_error:
                            print(f"Stream fallback failed for '{candidate}': {stream_error}")

                if player is None:
                    base_error = download_errors[-1] if download_errors else "Nincs lejatszhato forras."
                    short_error = str(base_error).replace("\n", " ").strip()
                    if len(short_error) > 220:
                        short_error = short_error[:220].rstrip() + "..."
                    await ctx.send(f"Hiba a lejatsszasnal: {short_error}")
                    return

            voice_channel = ctx.voice_client
            mixer = get_mixer(voice_channel)
            if mixer.main_source or voice_channel.is_paused():
                song_queues[guild_id].append(player)
                titles_queues[guild_id].append(player.title)
                await ctx.send(f"Sorba allitva: **{player.title}**")
            else:
                mixer.set_main_source(player, on_end=lambda: play_next_in_queue(ctx))
                await ctx.send(f"Most szol: **{player.title}**")


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
        await ctx.send("Már fut egy játék!")
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
        mode_content = mode_msg.content.strip()
        if mode_content.startswith("!") or mode_content.lower() == "mégse":
            await game.send_and_track(ctx, "❌ Beállítás megszakítva.")
            return
        game.track_message(mode_msg)
        mode_value = mode_content
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
        stake_content = stake_msg.content.strip()
        if stake_content.startswith("!") or stake_content.lower() == "mégse":
            await game.send_and_track(ctx, "❌ Beállítás megszakítva.")
            return
        game.track_message(stake_msg)
        stake_value = stake_content.lower()
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
    state_lower = state.lower()
    if state_lower not in {"on", "off"}:
        await ctx.send("Használat: !Random-bejátszás <on/off>")
        return
    core.prank_enabled = state_lower == "on"
    status = "bekapcsolva" if core.prank_enabled else "kikapcsolva"
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
    if mode.lower() != "mód":
        await ctx.send("Használat: !Jimmy mód")
        return
    core.prank_mode = "jimmy"
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
    if mode.lower() != "mód":
        await ctx.send("Használat: !Normál mód")
        return
    core.prank_mode = "normal"
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
    if mode.lower() != "mód":
        await ctx.send("Használat: !Vegyes mód")
        return
    core.prank_mode = "mixed"
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
        clear_guild_queue(guild_id)
        mixers.pop(guild_id, None)
        roulette_games.pop(guild_id, None)
        await voice_client.disconnect()
        await ctx.send("👋 Most már ez vagyok én, egy süllyedő hajó.")


@bot.command(name="lábhoz")
async def labhoz(ctx):
    guild_id = ctx.guild.id
    radnai_stopped = stop_radnai_alert()
    game = roulette_games.get(guild_id)
    if game and game.active:
        await game.stop()
    if guild_id in afktasks:
        afktasks[guild_id].cancel()
        del afktasks[guild_id]
    clear_guild_queue(guild_id)
    mixers.pop(guild_id, None)
    roulette_games.pop(guild_id, None)
    voice_client = ctx.voice_client
    if voice_client:
        if voice_client.is_playing() or voice_client.is_paused():
            voice_client.stop()
        await voice_client.disconnect()
    message = "🐕 Igenis, gazdám! (Minden folyamat leállítva, memória törölve)."
    if radnai_stopped:
        message = f"{message} Radnai riadó is leállítva."
    await ctx.send(message)


@bot.command(name="consuela")
async def consuela(ctx):
    if not bot.user:
        await ctx.send("❌ A bot allapota nem elerheto, probald ujra.")
        return

    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=5)

    try:
        deleted_messages = await ctx.channel.purge(
            limit=None,
            after=cutoff,
            check=lambda message: message.author.id == bot.user.id,
            bulk=True,
        )
        await ctx.send(
            f"🧹 Torolve: {len(deleted_messages)} bot-uzenet az elmult 5 percbol.",
            delete_after=5,
        )
    except discord.Forbidden:
        await ctx.send("❌ Nincs jogosultsagom uzenetek torlesere ebben a csatornaban.")
    except discord.HTTPException as e:
        await ctx.send(f"❌ Torles kozben hiba tortent: {e}")


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
        await ctx.send("Ehhez a parancshoz admin jogosultsĂˇg kell!")
