from distutils.util import split_quoted
import os
import sys
import time
import shlex
import shutil
import random
import inspect
import logging
import asyncio
import pathlib
import traceback
import math
import re
import textwrap
import json
import csv

from subprocess import check_output

import aiohttp
import discord
from discord.ext import tasks
import colorlog

from io import BytesIO, StringIO
from functools import wraps
from textwrap import dedent
from datetime import timedelta, datetime, timezone
from collections import defaultdict
from quantumrandom import get_data

from discord.enums import ChannelType

from . import exceptions
from . import downloader

from .playlist import Playlist
from .player import MusicPlayer
from .entry import StreamPlaylistEntry
from .opus_loader import load_opus_lib
from .config import Config, ConfigDefaults
from .permissions import Permissions, PermissionsDefaults
from .aliases import Aliases, AliasesDefault
from .constructs import SkipState, Response
from .utils import load_file, write_file, fixg, ftimedelta, _func_, _get_variable
from .spotify import Spotify
from .json import Json
from .scorekeeper import Scorekeeper

from .constants import VERSION as BOTVERSION
from .constants import DISCORD_MSG_CHAR_LIMIT, AUDIO_CACHE_PATH


from .emojipasta.generator import EmojipastaGenerator
from .robertdowneyjr.rdj import rdj


load_opus_lib()

log = logging.getLogger(__name__)

class MusicBot(discord.Client):
            
    def __init__(self, config_file=None, perms_file=None, aliases_file=None):
        try:
            sys.stdout.write("\x1b]2;MusicBot {}\x07".format(BOTVERSION))
        except:
            pass

        print()

        if config_file is None:
            config_file = ConfigDefaults.options_file

        if perms_file is None:
            perms_file = PermissionsDefaults.perms_file

        if aliases_file is None:
            aliases_file = AliasesDefault.aliases_file

        self.load_configs() #loads the custom configs. TODO -- can this be moved to the config.py file and integrated into the existing config parser?
        self.set_secret_word()
        self.next_reminder = self.get_next_reminder()
        self.scorekeeper = Scorekeeper()
        self.days_until_reboot = random.randint(5,10)
        print("Days until reboot: "+str(self.days_until_reboot))

        self.players = {}
        self.exit_signal = None
        self.init_ok = False
        self.cached_app_info = None
        self.last_status = None

        self.config = Config(config_file)
        
        self._setup_logging()
        
        self.permissions = Permissions(perms_file, grant_all=[self.config.owner_id])
        self.str = Json(self.config.i18n_file)

        if self.config.usealias:
            self.aliases = Aliases(aliases_file)

        self.blacklist = set(load_file(self.config.blacklist_file))
        self.autoplaylist = load_file(self.config.auto_playlist_file)

        self.aiolocks = defaultdict(asyncio.Lock)
        self.downloader = downloader.Downloader(download_folder='audio_cache')

        log.info('Starting MusicBot {}'.format(BOTVERSION))

        if not self.autoplaylist:
            log.warning("Autoplaylist is empty, disabling.")
            self.config.auto_playlist = False
        else:
            log.info("Loaded autoplaylist with {} entries".format(len(self.autoplaylist)))

        if self.blacklist:
            log.debug("Loaded blacklist with {} entries".format(len(self.blacklist)))

        # TODO: Do these properly
        ssd_defaults = {
            'last_np_msg': None,
            'auto_paused': False,
            'availability_paused': False
        }
        self.server_specific_data = defaultdict(ssd_defaults.copy)

        #run discord.Client init and enable all intents
        super().__init__(intents=discord.Intents.all(), activity=discord.Game(name="Emotion: "+self.get_daily_emotion()))

        self.http.user_agent += ' MusicBot/%s' % BOTVERSION
        self.aiosession = aiohttp.ClientSession()

        self.spotify = None
        if self.config._spotify:
            try:
                self.spotify = Spotify(self.config.spotify_clientid, self.config.spotify_clientsecret, aiosession=self.aiosession, loop=self.loop)
                if not self.spotify.token:
                    log.warning('Spotify did not provide us with a token. Disabling.')
                    self.config._spotify = False
                else:
                    log.info('Authenticated with Spotify successfully using client ID and secret.')
            except exceptions.SpotifyError as e:
                log.warning('There was a problem initialising the connection to Spotify. Is your client ID and secret correct? Details: {0}. Continuing anyway in 5 seconds...'.format(e))
                self.config._spotify = False
                time.sleep(5)  # make sure they see the problem


        
        # FIXME: Load cogs here.


    # TODO: Add some sort of `denied` argument for a message to send when someone else tries to use it
    def owner_only(func):
        @wraps(func)
        async def wrapper(self, *args, **kwargs):
            # Only allow the owner to use these commands
            orig_msg = _get_variable('message')

            if not orig_msg or orig_msg.author.id == self.config.owner_id:
                # noinspection PyCallingNonCallable
                return await func(self, *args, **kwargs)
            else:
                raise exceptions.PermissionsError("THOUGHT CRIME!! Only the owner can use this command.", expire_in=30)
        wrapper.owner_cmd = True
        return wrapper

    def dev_only(func):
        @wraps(func)
        async def wrapper(self, *args, **kwargs):
            orig_msg = _get_variable('message')

            if not orig_msg or str(orig_msg.author.id) in self.config.dev_ids:
                # noinspection PyCallingNonCallable
                return await func(self, *args, **kwargs)
            else:
                raise exceptions.PermissionsError("THOUGHT CRIME!! Only dev users can use this command.", expire_in=30)

        wrapper.dev_cmd = True
        return wrapper

    def ensure_appinfo(func):
        @wraps(func)
        async def wrapper(self, *args, **kwargs):
            await self._cache_app_info()
            # noinspection PyCallingNonCallable
            return await func(self, *args, **kwargs)

        return wrapper

    def _get_owner(self, *, server=None, voice=False):
            return discord.utils.find(
                lambda m: m.id == self.config.owner_id and (m.voice if voice else True),
                server.members if server else self.get_all_members()
            )

    def _delete_old_audiocache(self, path=AUDIO_CACHE_PATH):
        try:
            shutil.rmtree(path)
            return True
        except:
            try:
                os.rename(path, path + '__')
            except:
                return False
            try:
                shutil.rmtree(path)
            except:
                os.rename(path + '__', path)
                return False

        return True

    def _setup_logging(self):
        if len(logging.getLogger(__package__).handlers) > 1:
            log.debug("Skipping logger setup, already set up")
            return

        shandler = logging.StreamHandler(stream=sys.stdout)
        shandler.setFormatter(colorlog.LevelFormatter(
            fmt = {
                'DEBUG': '{log_color}[{levelname}:{module}] {message}',
                'INFO': '{log_color}{message}',
                'WARNING': '{log_color}{levelname}: {message}',
                'ERROR': '{log_color}[{levelname}:{module}] {message}',
                'CRITICAL': '{log_color}[{levelname}:{module}] {message}',

                'EVERYTHING': '{log_color}[{levelname}:{module}] {message}',
                'NOISY': '{log_color}[{levelname}:{module}] {message}',
                'VOICEDEBUG': '{log_color}[{levelname}:{module}][{relativeCreated:.9f}] {message}',
                'FFMPEG': '{log_color}[{levelname}:{module}][{relativeCreated:.9f}] {message}'
            },
            log_colors = {
                'DEBUG':    'cyan',
                'INFO':     'white',
                'WARNING':  'yellow',
                'ERROR':    'red',
                'CRITICAL': 'bold_red',

                'EVERYTHING': 'white',
                'NOISY':      'white',
                'FFMPEG':     'bold_purple',
                'VOICEDEBUG': 'purple',
        },
            style = '{',
            datefmt = ''
        ))
        shandler.setLevel(self.config.debug_level)
        logging.getLogger(__package__).addHandler(shandler)

        log.debug("Set logging level to {}".format(self.config.debug_level_str))

        if self.config.debug_mode:
            dlogger = logging.getLogger('discord')
            dlogger.setLevel(logging.DEBUG)
            dhandler = logging.FileHandler(filename='logs/discord.log', encoding='utf-8', mode='w')
            dhandler.setFormatter(logging.Formatter('{asctime}:{levelname}:{name}: {message}', style='{'))
            dlogger.addHandler(dhandler)

    @staticmethod
    def _check_if_empty(vchannel: discord.abc.GuildChannel, *, excluding_me=True, excluding_deaf=False):
        def check(member):
            if excluding_me and member == vchannel.guild.me:
                return False

            if excluding_deaf and any([member.deaf, member.self_deaf]):
                return False

            if member.bot:
                return False

            return True
        return not sum(1 for m in vchannel.members if check(m))

    async def _join_startup_channels(self, channels, *, autosummon=True):
        joined_servers = set()
        channel_map = {c.guild: c for c in channels}

        def _autopause(player):
            if self._check_if_empty(player.voice_client.channel):
                log.info("Initial autopause in empty channel")

                player.pause()
                self.server_specific_data[player.voice_client.channel.guild]['auto_paused'] = True

        for guild in self.guilds:
            if guild.unavailable or guild in channel_map:
                continue

            if guild.me.voice:
                log.info("Found resumable voice channel {0.guild.name}/{0.name}".format(guild.me.voice.channel))
                channel_map[guild] = guild.me.voice.channel

            if autosummon:
                owner = self._get_owner(server=guild, voice=True)
                if owner:
                    log.info("Found owner in \"{}\"".format(owner.voice.channel.name))
                    channel_map[guild] = owner.voice.channel

        for guild, channel in channel_map.items():
            if guild in joined_servers:
                log.info("Already joined a channel in \"{}\", skipping".format(guild.name))
                continue

            if channel and isinstance(channel, discord.VoiceChannel):
                log.info("Attempting to join {0.guild.name}/{0.name}".format(channel))

                chperms = channel.permissions_for(guild.me)

                if not chperms.connect:
                    log.info("Cannot join channel \"{}\", no permission.".format(channel.name))
                    continue

                elif not chperms.speak:
                    log.info("Will not join channel \"{}\", no permission to speak.".format(channel.name))
                    continue

                try:
                    player = await self.get_player(channel, create=True, deserialize=self.config.persistent_queue)
                    joined_servers.add(guild)

                    log.info("Joined {0.guild.name}/{0.name}".format(channel))

                    if player.is_stopped:
                        player.play()

                    if self.config.auto_playlist:
                        if self.config.auto_pause:
                            player.once('play', lambda player, **_: _autopause(player))
                        if not player.playlist.entries:
                            await self.on_player_finished_playing(player)

                except Exception:
                    log.debug("Error joining {0.guild.name}/{0.name}".format(channel), exc_info=True)
                    log.error("Failed to join {0.guild.name}/{0.name}".format(channel))

            elif channel:
                log.warning("Not joining {0.guild.name}/{0.name}, that's a text channel.".format(channel))

            else:
                log.warning("Invalid channel thing: {}".format(channel))

    async def _wait_delete_msg(self, message, after):
        await asyncio.sleep(after)
        await self.safe_delete_message(message, quiet=True)

    # TODO: Check to see if I can just move this to on_message after the response check
    async def _manual_delete_check(self, message, *, quiet=False):
        if self.config.delete_invoking:
            await self.safe_delete_message(message, quiet=quiet)

    async def _check_ignore_non_voice(self, msg):
        if msg.guild.me.voice:
            vc = msg.guild.me.voice.channel
        else:
            vc = None

        # If we've connected to a voice chat and we're in the same voice channel
        if not vc or (msg.author.voice and vc == msg.author.voice.channel) or msg.author.bot : #(I added the bot case for the Siri-conrtolled webhook)
            return True
        else:
            raise exceptions.PermissionsError(
                "you cannot use this command when not in the voice channel (%s)" % vc.name, expire_in=30)

    async def _cache_app_info(self, *, update=False):
        if not self.cached_app_info and not update and self.user.bot:
            log.debug("Caching app info")
            self.cached_app_info = await self.application_info()

        return self.cached_app_info


    async def remove_from_autoplaylist(self, song_url:str, *, ex:Exception=None, delete_from_ap=False):
        if song_url not in self.autoplaylist:
            log.debug("URL \"{}\" not in autoplaylist, ignoring".format(song_url))
            return

        async with self.aiolocks[_func_()]:
            self.autoplaylist.remove(song_url)
            log.info("Removing unplayable song from session autoplaylist: %s" % song_url)

            with open(self.config.auto_playlist_removed_file, 'a', encoding='utf8') as f:
                f.write(
                    '# Entry removed {ctime}\n'
                    '# Reason: {ex}\n'
                    '{url}\n\n{sep}\n\n'.format(
                        ctime=time.ctime(),
                        ex=str(ex).replace('\n', '\n#' + ' ' * 10), # 10 spaces to line up with # Reason:
                        url=song_url,
                        sep='#' * 32
                ))

            if delete_from_ap:
                log.info("Updating autoplaylist")
                write_file(self.config.auto_playlist_file, self.autoplaylist)

    @ensure_appinfo
    async def generate_invite_link(self, *, permissions=discord.Permissions(70380544), guild=None):
        return discord.utils.oauth_url(self.cached_app_info.id, permissions=permissions, guild=guild)

    async def get_voice_client(self, channel: discord.abc.GuildChannel):
        if isinstance(channel, discord.Object):
            channel = self.get_channel(channel.id)

        if not isinstance(channel, discord.VoiceChannel):
            raise AttributeError('Channel passed must be a voice channel')

        if channel.guild.voice_client:
            return channel.guild.voice_client
        else:
            #print("CON")
            return await channel.connect(self_deaf=True) #PROBLEM ZONE

    async def disconnect_voice_client(self, guild):
        vc = self.voice_client_in(guild)
        if not vc:
            return

        if guild.id in self.players:
            self.players.pop(guild.id).kill()

        await vc.disconnect()

    async def disconnect_all_voice_clients(self):
        for vc in list(self.voice_clients).copy():
            await self.disconnect_voice_client(vc.channel.guild)

    async def set_voice_state(self, vchannel, *, mute=False, deaf=False):
        if isinstance(vchannel, discord.Object):
            vchannel = self.get_channel(vchannel.id)

        if getattr(vchannel, 'type', ChannelType.text) != ChannelType.voice:
            raise AttributeError('Channel passed must be a voice channel')

        await self.ws.voice_state(vchannel.guild.id, vchannel.id, mute, deaf)
        # I hope I don't have to set the channel here
        # instead of waiting for the event to update it

    def get_player_in(self, guild:discord.Guild) -> MusicPlayer:
        return self.players.get(guild.id)

    async def get_player(self, channel, create=False, *, deserialize=False) -> MusicPlayer:
        guild = channel.guild
        #print("GETTING PLAYER")

        async with self.aiolocks[_func_() + ':' + str(guild.id)]:
            #print("A")
            if deserialize:
                #print("AA")
                voice_client = await self.get_voice_client(channel)
                #print("AAB")
                player = await self.deserialize_queue(guild, voice_client)
                #print("BB")
                if player:
                    log.debug("Created player via deserialization for guild %s with %s entries", guild.id, len(player.playlist))
                    # Since deserializing only happens when the bot starts, I should never need to reconnect
                    return self._init_player(player, guild=guild)
                #print("CC")
            #print("B")
            if guild.id not in self.players:
                if not create:
                    raise exceptions.CommandError(
                        'The bot is not in a voice channel.  '
                        'Use %ssummon to summon it to your voice channel.' % self.config.command_prefix)

                voice_client = await self.get_voice_client(channel)

                playlist = Playlist(self)
                player = MusicPlayer(self, voice_client, playlist)
                self._init_player(player, guild=guild)
            #print("C")
        #print("RET")
        return self.players[guild.id]

    def _init_player(self, player, *, guild=None):
        player = player.on('play', self.on_player_play) \
                       .on('resume', self.on_player_resume) \
                       .on('pause', self.on_player_pause) \
                       .on('stop', self.on_player_stop) \
                       .on('finished-playing', self.on_player_finished_playing) \
                       .on('entry-added', self.on_player_entry_added) \
                       .on('error', self.on_player_error)

        player.skip_state = SkipState()

        if guild:
            self.players[guild.id] = player

        return player

    async def on_player_play(self, player, entry):
        log.debug('Running on_player_play')
        await self.update_now_playing_status(entry)
        player.skip_state.reset()

        # This is the one event where its ok to serialize autoplaylist entries
        await self.serialize_queue(player.voice_client.channel.guild)

        if self.config.write_current_song:
            await self.write_current_song(player.voice_client.channel.guild, entry)

        channel = entry.meta.get('channel', None)
        author = entry.meta.get('author', None)

        if channel and author:
            author_perms = self.permissions.for_user(author)

            if author not in player.voice_client.channel.members and author_perms.skip_when_absent:
                newmsg = 'Skipping next song in `%s`: `%s` added by `%s` as queuer not in voice' % (
                    player.voice_client.channel.name, entry.title, entry.meta['author'].name)
                player.skip()
            elif self.config.now_playing_mentions:
                displayname = "Robot friend" if entry.meta['author'].bot else entry.meta['author'].nick if entry.meta['author'].nick else entry.meta['author'].name
                newmsg = '%s - your song `%s` is now playing in `%s`!' % (
                    displayname, entry.title, player.voice_client.channel.name)
            else:
                newmsg = 'Now playing in `%s`: `%s` added by `%s`' % (
                    player.voice_client.channel.name, entry.title, entry.meta['author'].name)
        else:
            # no author (and channel), it's an autoplaylist (or autostream from my other PR) entry.
            newmsg = 'Now playing automatically added entry `%s` in `%s`' % (
                entry.title, player.voice_client.channel.name)

        if newmsg:
            if self.config.dm_nowplaying and author:
                await self.safe_send_message(author, newmsg)
                return

            if self.config.no_nowplaying_auto and not author:
                return

            guild = player.voice_client.guild
            last_np_msg = self.server_specific_data[guild]['last_np_msg']

            if self.config.nowplaying_channels:
                for potential_channel_id in self.config.nowplaying_channels:
                    potential_channel = self.get_channel(potential_channel_id)
                    if potential_channel and potential_channel.guild == guild:
                        channel = potential_channel
                        break

            if channel:
                pass
            elif not channel and last_np_msg:
                channel = last_np_msg.channel
            else:
                log.debug('no channel to put now playing message into')
                return

            # send it in specified channel
            self.server_specific_data[guild]['last_np_msg'] = await self.safe_send_message(channel, newmsg)

        # TODO: Check channel voice state?

    async def on_player_resume(self, player, entry, **_):
        log.debug('Running on_player_resume')
        await self.update_now_playing_status(entry)

    async def on_player_pause(self, player, entry, **_):
        log.debug('Running on_player_pause')
        await self.update_now_playing_status(entry, True)
        # await self.serialize_queue(player.voice_client.channel.guild)

    async def on_player_stop(self, player, **_):
        log.debug('Running on_player_stop')
        await self.update_now_playing_status()

    async def on_player_finished_playing(self, player, **_):
        log.debug('Running on_player_finished_playing')

        # delete last_np_msg somewhere if we have cached it
        if self.config.delete_nowplaying:
            guild = player.voice_client.guild
            last_np_msg = self.server_specific_data[guild]['last_np_msg']
            if last_np_msg:
                await self.safe_delete_message(last_np_msg)

        def _autopause(player):
            if self._check_if_empty(player.voice_client.channel):
                log.info("Player finished playing, autopaused in empty channel")

                player.pause()
                self.server_specific_data[player.voice_client.channel.guild]['auto_paused'] = True

        if not player.playlist.entries and not player.current_entry and self.config.auto_playlist:
            if not player.autoplaylist:
                if not self.autoplaylist:
                    # TODO: When I add playlist expansion, make sure that's not happening during this check
                    log.warning("No playable songs in the autoplaylist, disabling.")
                    self.config.auto_playlist = False
                else:
                    log.debug("No content in current autoplaylist. Filling with new music...")
                    player.autoplaylist = list(self.autoplaylist)

            while player.autoplaylist:
                if self.config.auto_playlist_random:
                    random.shuffle(player.autoplaylist)
                    song_url = random.choice(player.autoplaylist)
                else:
                    song_url = player.autoplaylist[0]
                player.autoplaylist.remove(song_url)

                info = {}

                try:
                    info = await self.downloader.extract_info(player.playlist.loop, song_url, download=False, process=False)
                except downloader.youtube_dl.utils.DownloadError as e:
                    if 'YouTube said:' in e.args[0]:
                        # url is bork, remove from list and put in removed list
                        log.error("Error processing youtube url:\n{}".format(e.args[0]))

                    else:
                        # Probably an error from a different extractor, but I've only seen youtube's
                        log.error("Error processing \"{url}\": {ex}".format(url=song_url, ex=e))

                    await self.remove_from_autoplaylist(song_url, ex=e, delete_from_ap=self.config.remove_ap)
                    continue

                except Exception as e:
                    log.error("Error processing \"{url}\": {ex}".format(url=song_url, ex=e))
                    log.exception()

                    self.autoplaylist.remove(song_url)
                    continue

                if info.get('entries', None):  # or .get('_type', '') == 'playlist'
                    log.debug("Playlist found but is unsupported at this time, skipping.")
                    # TODO: Playlist expansion

                # Do I check the initial conditions again?
                # not (not player.playlist.entries and not player.current_entry and self.config.auto_playlist)

                if self.config.auto_pause:
                    player.once('play', lambda player, **_: _autopause(player))

                try:
                    await player.playlist.add_entry(song_url, channel=None, author=None)
                except exceptions.ExtractionError as e:
                    log.error("Error adding song from autoplaylist: {}".format(e))
                    log.debug('', exc_info=True)
                    continue

                break

            if not self.autoplaylist:
                # TODO: When I add playlist expansion, make sure that's not happening during this check
                log.warning("No playable songs in the autoplaylist, disabling.")
                self.config.auto_playlist = False

        else: # Don't serialize for autoplaylist events
            await self.serialize_queue(player.voice_client.channel.guild)

        if not player.is_stopped and not player.is_dead:
            player.play(_continue=True)

    async def on_player_entry_added(self, player, playlist, entry, **_):
        log.debug('Running on_player_entry_added')
        if entry.meta.get('author') and entry.meta.get('channel'):
            await self.serialize_queue(player.voice_client.channel.guild)

    async def on_player_error(self, player, entry, ex, **_):
        if 'channel' in entry.meta:
            await self.safe_send_message(
                entry.meta['channel'],
                "```\nError from FFmpeg:\n{}\n```".format(ex)
            )
        else:
            log.exception("Player error", exc_info=ex)

    async def update_now_playing_status(self, entry=None, is_paused=False):
        game = None

        if not self.config.status_message:
            if self.user.bot:
                activeplayers = sum(1 for p in self.players.values() if p.is_playing)
                if activeplayers > 1:
                    game = discord.Game(type=0, name="music on %s guilds" % activeplayers)
                    entry = None

                elif activeplayers == 1:
                    player = discord.utils.get(self.players.values(), is_playing=True)
                    entry = player.current_entry

            if entry:
                prefix = u'\u275A\u275A ' if is_paused else ''

                name = u'{}{}'.format(prefix, entry.title)[:128]
                game = discord.Game(type=0, name=name)
        else:
            game = discord.Game(type=0, name=self.config.status_message.strip()[:128])

        async with self.aiolocks[_func_()]:
            if game != self.last_status:
                await self.change_presence(activity=game)
                self.last_status = game

    async def update_now_playing_message(self, guild, message, *, channel=None):
        lnp = self.server_specific_data[guild]['last_np_msg']
        m = None

        if message is None and lnp:
            await self.safe_delete_message(lnp, quiet=True)

        elif lnp:  # If there was a previous lp message
            oldchannel = lnp.channel

            if lnp.channel == oldchannel:  # If we have a channel to update it in
                async for lmsg in lnp.channel.history(limit=1):
                    if lmsg != lnp and lnp:  # If we need to resend it
                        await self.safe_delete_message(lnp, quiet=True)
                        m = await self.safe_send_message(channel, message, quiet=True)
                    else:
                        m = await self.safe_edit_message(lnp, message, send_if_fail=True, quiet=False)

            elif channel: # If we have a new channel to send it to
                await self.safe_delete_message(lnp, quiet=True)
                m = await self.safe_send_message(channel, message, quiet=True)

            else:  # we just resend it in the old channel
                await self.safe_delete_message(lnp, quiet=True)
                m = await self.safe_send_message(oldchannel, message, quiet=True)

        elif channel: # No previous message
            m = await self.safe_send_message(channel, message, quiet=True)

        self.server_specific_data[guild]['last_np_msg'] = m


    async def serialize_queue(self, guild, *, dir=None):
        """
        Serialize the current queue for a server's player to json.
        """

        player = self.get_player_in(guild)
        if not player:
            return

        if dir is None:
            dir = 'data/%s/queue.json' % guild.id

        async with self.aiolocks['queue_serialization' + ':' + str(guild.id)]:
            log.debug("Serializing queue for %s", guild.id)

            with open(dir, 'w', encoding='utf8') as f:
                f.write(player.serialize(sort_keys=True))

    async def serialize_all_queues(self, *, dir=None):
        coros = [self.serialize_queue(s, dir=dir) for s in self.guilds]
        await asyncio.gather(*coros, return_exceptions=True)

    async def deserialize_queue(self, guild, voice_client, playlist=None, *, dir=None) -> MusicPlayer:
        """
        Deserialize a saved queue for a server into a MusicPlayer.  If no queue is saved, returns None.
        """

        if playlist is None:
            playlist = Playlist(self)

        if dir is None:
            dir = 'data/%s/queue.json' % guild.id

        async with self.aiolocks['queue_serialization' + ':' + str(guild.id)]:
            if not os.path.isfile(dir):
                return None

            log.debug("Deserializing queue for %s", guild.id)

            with open(dir, 'r', encoding='utf8') as f:
                data = f.read()

        return MusicPlayer.from_json(data, self, voice_client, playlist)

    async def write_current_song(self, guild, entry, *, dir=None):
        """
        Writes the current song to file
        """
        player = self.get_player_in(guild)
        if not player:
            return

        if dir is None:
            dir = 'data/%s/current.txt' % guild.id

        async with self.aiolocks['current_song' + ':' + str(guild.id)]:
            log.debug("Writing current song for %s", guild.id)

            with open(dir, 'w', encoding='utf8') as f:
                f.write(entry.title)

    @ensure_appinfo
    async def _on_ready_sanity_checks(self):
        # Ensure folders exist
        await self._scheck_ensure_env()

        # Server permissions check
        await self._scheck_server_permissions()

        # playlists in autoplaylist
        await self._scheck_autoplaylist()

        # config/permissions async validate?
        await self._scheck_configs()


    async def _scheck_ensure_env(self):
        log.debug("Ensuring data folders exist")
        for guild in self.guilds:
            pathlib.Path('data/%s/' % guild.id).mkdir(exist_ok=True)

        with open('data/server_names.txt', 'w', encoding='utf8') as f:
            for guild in sorted(self.guilds, key=lambda s:int(s.id)):
                f.write('{:<22} {}\n'.format(guild.id, guild.name))

        if not self.config.save_videos and os.path.isdir(AUDIO_CACHE_PATH):
            if self._delete_old_audiocache():
                log.debug("Deleted old audio cache")
            else:
                log.debug("Could not delete old audio cache, moving on.")


    async def _scheck_server_permissions(self):
        log.debug("Checking server permissions")
        pass # TODO

    async def _scheck_autoplaylist(self):
        log.debug("Auditing autoplaylist")
        pass # TODO

    async def _scheck_configs(self):
        log.debug("Validating config")
        await self.config.async_validate(self)

        log.debug("Validating permissions config")
        await self.permissions.async_validate(self)



#######################################################################################################################


    async def safe_send_message(self, dest, content, **kwargs):
        tts = kwargs.pop('tts', False)
        quiet = kwargs.pop('quiet', False)
        expire_in = kwargs.pop('expire_in', 0)
        allow_none = kwargs.pop('allow_none', True)
        also_delete = kwargs.pop('also_delete', None)

        msg = None
        lfunc = log.debug if quiet else log.warning

        try:
            if content is not None or allow_none:
                if isinstance(content, discord.Embed):
                    msg = await dest.send(embed=content)
                else:
                    msg = await dest.send(content, tts=tts)

        except discord.Forbidden:
            lfunc("Cannot send message to \"%s\", no permission", dest.name)

        except discord.NotFound:
            lfunc("Cannot send message to \"%s\", invalid channel?", dest.name)

        except discord.HTTPException:
            if len(content) > DISCORD_MSG_CHAR_LIMIT:
                lfunc("Message is over the message size limit (%s)", DISCORD_MSG_CHAR_LIMIT)
            else:
                lfunc("Failed to send message")
                log.noise("Got HTTPException trying to send message to %s: %s", dest, content)

        finally:
            if msg and expire_in:
                asyncio.ensure_future(self._wait_delete_msg(msg, expire_in))

            if also_delete and isinstance(also_delete, discord.Message):
                asyncio.ensure_future(self._wait_delete_msg(also_delete, expire_in))

        return msg

    async def safe_delete_message(self, message, *, quiet=False):
        lfunc = log.debug if quiet else log.warning

        try:
            return await message.delete()

        except discord.Forbidden:
            lfunc("Cannot delete message \"{}\", no permission".format(message.clean_content))

        except discord.NotFound:
            lfunc("Cannot delete message \"{}\", message not found".format(message.clean_content))

    async def safe_edit_message(self, message, new, *, send_if_fail=False, quiet=False):
        lfunc = log.debug if quiet else log.warning

        try:
            return await message.edit(content=new)

        except discord.NotFound:
            lfunc("Cannot edit message \"{}\", message not found".format(message.clean_content))
            if send_if_fail:
                lfunc("Sending message instead")
                return await self.safe_send_message(message.channel, new)

    async def send_typing(self, destination):
        try:
            # 2023-04-21: Now only sends typing for 10 seconds (consider with typing()?)
            return await destination.typing() # 2023-04-21: Changed from trigger_typing()
        except discord.Forbidden:
            log.warning("Could not send typing to {}, no permission".format(destination))

    async def restart(self):
        self.exit_signal = exceptions.RestartSignal()
        await self.logout()

    def restart_threadsafe(self):
        asyncio.run_coroutine_threadsafe(self.restart(), self.loop)

    # 2023-04-22: Hacked this in to see if I can close the session properly.
    async def close(self):
        print("hi")
        await self.aiosession.close()
        await super().close()

    def _cleanup(self):
        try:
            self.loop.run_until_complete(self.close()) # katie: 2023-02-18: changed from self.logout() to self.close() due to deprecation
            #self.loop.run_until_complete(self.aiosession.close())
        except: pass

        # 2023-04-22: This is now handled by asyncio.run().
        #pending = asyncio.all_tasks(self.loop) # katie: 2023-02-18: changed from asyncio.Task.all_tasks() due to deprecation
        #gathered = asyncio.gather(*pending)

        #try:
            #gathered.cancel()
            #self.loop.run_until_complete(gathered)
            #gathered.exception()
        #except: pass

    # noinspection PyMethodOverriding
    def run(self):
        try:
            #super().run(*self.config.auth)
            asyncio.run(self.start(*self.config.auth))
            #self.loop.run_until_complete(self.start(*self.config.auth))

        except discord.errors.LoginFailure:
            # Add if token, else
            raise exceptions.HelpfulError(
                "Bot cannot login, bad credentials.",
                "Fix your token in the options file.  "
                "Remember that each field should be on their own line."
            )  #     ^^^^ In theory self.config.auth should never have no items

        finally:
            #try:
                #self._cleanup()
            #except Exception:
                #log.error("Error in cleanup", exc_info=True)

            if self.exit_signal:
                raise self.exit_signal # pylint: disable=E0702

    async def logout(self):
        await self.disconnect_all_voice_clients()
        return await super().logout()

    async def on_error(self, event, *args, **kwargs):
        ex_type, ex, stack = sys.exc_info()

        if ex_type == exceptions.HelpfulError:
            log.error("Exception in {}:\n{}".format(event, ex.message))

            await asyncio.sleep(2)  # don't ask
            await self.close()

        elif issubclass(ex_type, exceptions.Signal):
            self.exit_signal = ex_type
            await self.close()

        else:
            log.error("Exception in {}".format(event), exc_info=True)

    async def on_resumed(self):
        log.info("\nReconnected to discord.\n")

    async def on_ready(self):
        dlogger = logging.getLogger('discord')
        for h in dlogger.handlers:
            if getattr(h, 'terminator', None) == '':
                dlogger.removeHandler(h)
                print()

        log.debug("Connection established, ready to go.")

        self.ws._keep_alive.name = 'Gateway Keepalive'

        if self.init_ok:
            log.debug("Received additional READY event, may have failed to resume")
            return

        await self._on_ready_sanity_checks()

        self.init_ok = True

        ################################

        log.info("Connected: {0}/{1}#{2}".format(
            self.user.id,
            self.user.name,
            self.user.discriminator
        ))

        owner = self._get_owner(voice=True) or self._get_owner()
        if owner and self.guilds:
            log.info("Owner:     {0}/{1}#{2}\n".format(
                owner.id,
                owner.name,
                owner.discriminator
            ))

            log.info('Guild List:')
            unavailable_servers = 0
            for s in self.guilds:
                ser = ('{} (unavailable)'.format(s.name) if s.unavailable else s.name)
                log.info(' - ' + ser)
                if self.config.leavenonowners:
                    if s.unavailable:
                        unavailable_servers += 1
                    else:
                        check = s.get_member(owner.id)
                        if check == None:
                            await s.leave()
                            log.info('Left {} due to bot owner not found'.format(s.name))
            if unavailable_servers != 0:
                log.info('Not proceeding with checks in {} servers due to unavailability'.format(str(unavailable_servers))) 

        elif self.guilds:
            log.warning("Owner could not be found on any guild (id: %s)\n" % self.config.owner_id)

            log.info('Guild List:')
            for s in self.guilds:
                ser = ('{} (unavailable)'.format(s.name) if s.unavailable else s.name)
                log.info(' - ' + ser)

        else:
            log.warning("Owner unknown, bot is not on any guilds.")
            if self.user.bot:
                log.warning(
                    "To make the bot join a guild, paste this link in your browser. \n"
                    "Note: You should be logged into your main account and have \n"
                    "manage server permissions on the guild you want the bot to join.\n"
                    "  " + await self.generate_invite_link()
                )

        print(flush=True)

        if self.config.bound_channels:
            chlist = set(self.get_channel(i) for i in self.config.bound_channels if i)
            chlist.discard(None)

            invalids = set()
            invalids.update(c for c in chlist if isinstance(c, discord.VoiceChannel))

            chlist.difference_update(invalids)
            self.config.bound_channels.difference_update(invalids)

            if chlist:
                log.info("Bound to text channels:")
                [log.info(' - {}/{}'.format(ch.guild.name.strip(), ch.name.strip())) for ch in chlist if ch]
            else:
                print("Not bound to any text channels")

            if invalids and self.config.debug_mode:
                print(flush=True)
                log.info("Not binding to voice channels:")
                [log.info(' - {}/{}'.format(ch.guild.name.strip(), ch.name.strip())) for ch in invalids if ch]

            print(flush=True)

        else:
            log.info("Not bound to any text channels")

        if self.config.autojoin_channels:
            chlist = set(self.get_channel(i) for i in self.config.autojoin_channels if i)
            chlist.discard(None)

            invalids = set()
            invalids.update(c for c in chlist if isinstance(c, discord.TextChannel))

            chlist.difference_update(invalids)
            self.config.autojoin_channels.difference_update(invalids)

            if chlist:
                log.info("Autojoining voice channels:")
                [log.info(' - {}/{}'.format(ch.guild.name.strip(), ch.name.strip())) for ch in chlist if ch]
            else:
                log.info("Not autojoining any voice channels")

            if invalids and self.config.debug_mode:
                print(flush=True)
                log.info("Cannot autojoin text channels:")
                [log.info(' - {}/{}'.format(ch.guild.name.strip(), ch.name.strip())) for ch in invalids if ch]

            self.autojoin_channels = chlist

        else:
            log.info("Not autojoining any voice channels")
            self.autojoin_channels = set()
        
        if self.config.show_config_at_start:
            print(flush=True)
            log.info("Options:")

            log.info("  Command prefix: " + self.config.command_prefix)
            log.info("  Default volume: {}%".format(int(self.config.default_volume * 100)))
            log.info("  Skip threshold: {} votes or {}%".format(
                self.config.skips_required, fixg(self.config.skip_ratio_required * 100)))
            log.info("  Now Playing @mentions: " + ['Disabled', 'Enabled'][self.config.now_playing_mentions])
            log.info("  Auto-Summon: " + ['Disabled', 'Enabled'][self.config.auto_summon])
            log.info("  Auto-Playlist: " + ['Disabled', 'Enabled'][self.config.auto_playlist] + " (order: " + ['sequential', 'random'][self.config.auto_playlist_random] + ")")
            log.info("  Auto-Pause: " + ['Disabled', 'Enabled'][self.config.auto_pause])
            log.info("  Delete Messages: " + ['Disabled', 'Enabled'][self.config.delete_messages])
            if self.config.delete_messages:
                log.info("    Delete Invoking: " + ['Disabled', 'Enabled'][self.config.delete_invoking])
            log.info("  Debug Mode: " + ['Disabled', 'Enabled'][self.config.debug_mode])
            log.info("  Downloaded songs will be " + ['deleted', 'saved'][self.config.save_videos])
            if self.config.status_message:
                log.info("  Status message: " + self.config.status_message)
            log.info("  Write current songs to file: " + ['Disabled', 'Enabled'][self.config.write_current_song])
            log.info("  Author insta-skip: " + ['Disabled', 'Enabled'][self.config.allow_author_skip])
            log.info("  Embeds: " + ['Disabled', 'Enabled'][self.config.embeds])
            log.info("  Spotify integration: " + ['Disabled', 'Enabled'][self.config._spotify])
            log.info("  Legacy skip: " + ['Disabled', 'Enabled'][self.config.legacy_skip])
            log.info("  Leave non owners: " + ['Disabled', 'Enabled'][self.config.leavenonowners])

        print(flush=True)

        #await self.update_now_playing_status()

        # maybe option to leave the ownerid blank and generate a random command for the owner to use
        # wait_for_message is pretty neato

        await self._join_startup_channels(self.autojoin_channels, autosummon=self.config.auto_summon)

        # we do this after the config stuff because it's a lot easier to notice here
        if self.config.missing_keys:
            log.warning('Your config file is missing some options. If you have recently updated, '
                        'check the example_options.ini file to see if there are new options available to you. '
                        'The options missing are: {0}'.format(self.config.missing_keys))
            print(flush=True)

        #Start custom loop(s)
        self.midnight_loop.start()
        #await asyncio.sleep(1.-(1000*datetime.now().microsecond)) #this would actually work if the raspi's internal clock was synchronized properly - TODO?
        self.second_loop.start()
        print("INITIALIZATION FINISHED!")

        # t-t-th-th-that's all folks!


    def _gen_embed(self):
        """Provides a basic template for embeds"""
        e = discord.Embed()
        e.colour = 7506394
        e.set_footer(text='Just-Some-Bots/MusicBot ({})'.format(BOTVERSION), icon_url='https://i.imgur.com/gFHBoZA.png')
        e.set_author(name=self.user.name, url='https://github.com/Just-Some-Bots/MusicBot', icon_url=self.user.display_avatar.url)
        return e

    async def cmd_resetplaylist(self, player, channel):
        """
        Usage:
            {command_prefix}resetplaylist

        Resets all songs in the server's autoplaylist
        """
        player.autoplaylist = list(set(self.autoplaylist))
        return Response(self.str.get('cmd-resetplaylist-response', '\N{OK HAND SIGN}'), delete_after=15)

    async def cmd_help(self, message, channel, command=None):
        """
        Usage:
            {command_prefix}help
            {command_prefix}help all
            {command_prefix}help [command]

        Prints a help message.
        If a command is specified, it prints a help message for that command.
        Otherwise, it lists the available commands.
        """
        self.commands = []
        self.is_all = False
        prefix = self.config.command_prefix

        if command:
            if command.lower() == 'all':
                self.is_all = True
                await self.gen_cmd_list(message, list_all_cmds=True)

            else:
                if self.config.usealias:
                    alias = self.aliases.get(command)
                    command = alias if alias else command #haha! by making the code harder to read I was able to combine two lines into one!
                cmd = getattr(self, 'cmd_' + command, None)

                if cmd and (not hasattr(cmd, 'dev_cmd') or str(message.author.id) in self.config.dev_ids) and (not hasattr(cmd, 'owner_cmd') or message.author.id==self.config.owner_id):
                    return Response(
                        "```\n{}```".format(
                            dedent(cmd.__doc__)
                        ).format(command_prefix=self.config.command_prefix),
                        delete_after=60
                    )
                elif (hasattr(cmd, 'dev_cmd') and not str(message.author.id) in self.config.dev_ids) or (hasattr(cmd, 'owner_cmd') and not message.author.id==self.config.owner_id):
                    await channel.send("This is forbidden knowledge.",delete_after=5)
                    raise exceptions.CommandError(self.str.get('cmd-help-invalid', "This is forbidden knowledge."), expire_in=10)
                else:
                    raise exceptions.CommandError(self.str.get('cmd-help-invalid', "No such command"), expire_in=10)

        #elif message.author.id == self.config.owner_id: #disabling this for Bnuuybot (personal preference) (when enabled, defaults owner's 'help' to 'help all')
        #    await self.gen_cmd_list(message, list_all_cmds=True)

        else:
            await self.gen_cmd_list(message)

        desc = '```\n' + ', '.join(self.commands) + '\n```\n' + \
            'For information about a particular command, run `{}help [command]`\n \
            Bnuuy\'s code is an extensively modified version of https://just-some-bots.github.io/MusicBot/'.format(prefix) #TODO - figure out why this doesn't display
        #if not self.is_all:
        #    desc += self.str.get('cmd-help-all', '\nOnly showing commands you can use, for a list of all commands, run `{}help all`').format(prefix)

        if isinstance(channel, discord.abc.PrivateChannel):
            desc += "\n\nThis is a DM channel, so only the commands you can use here are displayed.\nFor a full list of commands, run `"+prefix+"help all`"
        elif not channel.id in self.config.bound_channels:
            desc += "\n\nThis is not the designated BnuuyBot channel for this server, so only the commands you can use in this channel are displayed.\nFor a full list of commands, run `"+prefix+"help all`"

        return Response(desc, reply=True, delete_after=60)

    async def cmd_blacklist(self, message, user_mentions, option, something):
        """
        Usage:
            {command_prefix}blacklist [ + | - | add | remove ] @UserName [@UserName2 ...]

        Add or remove users to the blacklist.
        Blacklisted users are forbidden from using bot commands.
        """

        if not user_mentions:
            raise exceptions.CommandError("No users listed.", expire_in=20)

        if option not in ['+', '-', 'add', 'remove']:
            raise exceptions.CommandError(
                self.str.get('cmd-blacklist-invalid', 'Invalid option "{0}" specified, use +, -, add, or remove').format(option), expire_in=20
            )

        for user in user_mentions.copy():
            if user.id == self.config.owner_id:
                print("[Commands:Blacklist] The owner cannot be blacklisted.")
                user_mentions.remove(user)

        old_len = len(self.blacklist)

        if option in ['+', 'add']:
            self.blacklist.update(user.id for user in user_mentions)

            write_file(self.config.blacklist_file, self.blacklist)

            return Response(
                self.str.get('cmd-blacklist-added', '{0} users have been added to the blacklist').format(len(self.blacklist) - old_len),
                reply=True, delete_after=10
            )

        else:
            if self.blacklist.isdisjoint(user.id for user in user_mentions):
                return Response(self.str.get('cmd-blacklist-none', 'None of those users are in the blacklist.'), reply=True, delete_after=10)

            else:
                self.blacklist.difference_update(user.id for user in user_mentions)
                write_file(self.config.blacklist_file, self.blacklist)

                return Response(
                    self.str.get('cmd-blacklist-removed', '{0} users have been removed from the blacklist').format(old_len - len(self.blacklist)),
                    reply=True, delete_after=10
                )

    async def cmd_id(self, author, user_mentions):
        """
        Usage:
            {command_prefix}id [@user]

        Tells the user their id or the id of another user.
        """
        if not user_mentions:
            return Response(self.str.get('cmd-id-self', 'Your ID is `{0}`').format(author.id), reply=True, delete_after=35)
        else:
            usr = user_mentions[0]
            return Response(self.str.get('cmd-id-other', '**{0}**s ID is `{1}`').format(usr.name, usr.id), reply=True, delete_after=35)

    async def cmd_save(self, player, url=None):
        """
        Usage:
            {command_prefix}save [url]

        Saves the specified song or current song if not specified to the autoplaylist.
        """
        if url or (player.current_entry and not isinstance(player.current_entry, StreamPlaylistEntry)):
            if not url:
                url = player.current_entry.url

            if url not in self.autoplaylist:
                self.autoplaylist.append(url)
                write_file(self.config.auto_playlist_file, self.autoplaylist)
                log.debug("Appended {} to autoplaylist".format(url))
                return Response(self.str.get('cmd-save-success', 'Added <{0}> to the autoplaylist.').format(url))
            else:
                raise exceptions.CommandError(self.str.get('cmd-save-exists', 'This song is already in the autoplaylist.'))
        else:
            raise exceptions.CommandError(self.str.get('cmd-save-invalid', 'There is no valid song playing.'))

    @owner_only
    async def cmd_joinserver(self, message, server_link=None):
        """
        Usage:
            {command_prefix}joinserver invite_link

        Asks the bot to join a server.  Note: Bot accounts cannot use invite links.
        """

        url = await self.generate_invite_link()
        return Response(
            self.str.get('cmd-joinserver-response', "Click here to add me to a server: \n{}").format(url),
            reply=True, delete_after=30
        )

    async def cmd_karaoke(self, player, channel, author):
        """
        Usage:
            {command_prefix}karaoke

        Activates karaoke mode. During karaoke mode, only groups with the BypassKaraokeMode
        permission in the config file can queue music.
        """
        player.karaoke_mode = not player.karaoke_mode
        return Response("\N{OK HAND SIGN} Karaoke mode is now " + ['disabled', 'enabled'][player.karaoke_mode], delete_after=15)

    async def _do_playlist_checks(self, permissions, player, author, testobj):
        num_songs = sum(1 for _ in testobj)

        # I have to do exe extra checks anyways because you can request an arbitrary number of search results
        if not permissions.allow_playlists and num_songs > 1:
            raise exceptions.PermissionsError(self.str.get('playlists-noperms', "You are not allowed to request playlists"), expire_in=30)

        if permissions.max_playlist_length and num_songs > permissions.max_playlist_length:
            raise exceptions.PermissionsError(
                self.str.get('playlists-big', "Playlist has too many entries ({0} > {1})").format(num_songs, permissions.max_playlist_length),
                expire_in=30
            )

        # This is a little bit weird when it says (x + 0 > y), I might add the other check back in
        if permissions.max_songs and player.playlist.count_for_user(author) + num_songs > permissions.max_songs:
            raise exceptions.PermissionsError(
                self.str.get('playlists-limit', "Playlist entries + your already queued songs reached limit ({0} + {1} > {2})").format(
                    num_songs, player.playlist.count_for_user(author), permissions.max_songs),
                expire_in=30
            )
        return True

    async def cmd_play(self, message, player, channel, author, permissions, leftover_args, song_url):
        """
        Usage:
            {command_prefix}play song_link
            {command_prefix}play text to search for
            {command_prefix}play spotify_uri

        Adds the song to the playlist.  If a link is not provided, the first
        result from a youtube search is added to the queue.

        If enabled in the config, the bot will also support Spotify URIs, however
        it will use the metadata (e.g song name and artist) to find a YouTube
        equivalent of the song. Streaming from Spotify is not possible.
        """

        await channel.send("Working... (Pwease be patient while I locate the cassette tapes, this can sometimes take a few minutes)\n(Longer songs will take longer to download)", delete_after=30)

        song_url = song_url.strip('<>')

        async with channel.typing(): # awaitself.send_typing(channel)

            if leftover_args:
                song_url = ' '.join([song_url, *leftover_args])
            leftover_args = None  # prevent some crazy shit happening down the line

            # Make sure forward slashes work properly in search queries
            linksRegex = '((http(s)*:[/][/]|www.)([a-z]|[A-Z]|[0-9]|[/.]|[~])*)'
            pattern = re.compile(linksRegex)
            matchUrl = pattern.match(song_url)
            song_url = song_url.replace('/', '%2F') if matchUrl is None else song_url

            # Rewrite YouTube playlist URLs if the wrong URL type is given
            playlistRegex = r'watch\?v=.+&(list=[^&]+)'
            matches = re.search(playlistRegex, song_url)
            groups = matches.groups() if matches is not None else []
            song_url = "https://www.youtube.com/playlist?" + groups[0] if len(groups) > 0 else song_url

            if self.config._spotify:
                if 'open.spotify.com' in song_url:
                    song_url = 'spotify:' + re.sub('(http[s]?:\/\/)?(open.spotify.com)\/', '', song_url).replace('/', ':')
                    # remove session id (and other query stuff)
                    song_url = re.sub('\?.*', '', song_url)
                if song_url.startswith('spotify:'):
                    parts = song_url.split(":")
                    try:
                        if 'track' in parts:
                            res = await self.spotify.get_track(parts[-1])
                            song_url = res['artists'][0]['name'] + ' ' + res['name'] 

                        elif 'album' in parts:
                            res = await self.spotify.get_album(parts[-1])
                            await self._do_playlist_checks(permissions, player, author, res['tracks']['items'])
                            procmesg = await self.safe_send_message(channel, self.str.get('cmd-play-spotify-album-process', 'Processing album `{0}` (`{1}`)').format(res['name'], song_url))
                            for i in res['tracks']['items']:
                                song_url = i['name'] + ' ' + i['artists'][0]['name']
                                log.debug('Processing {0}'.format(song_url))
                                await self.cmd_play(message, player, channel, author, permissions, leftover_args, song_url)
                            await self.safe_delete_message(procmesg)
                            return Response(self.str.get('cmd-play-spotify-album-queued', "Enqueued `{0}` with **{1}** songs.").format(res['name'], len(res['tracks']['items'])))
                        
                        elif 'playlist' in parts:
                            res = []
                            r = await self.spotify.get_playlist_tracks(parts[-1])
                            while True:
                                res.extend(r['items'])
                                if r['next'] is not None:
                                    r = await self.spotify.make_spotify_req(r['next'])
                                    continue
                                else:
                                    break
                            await self._do_playlist_checks(permissions, player, author, res)
                            procmesg = await self.safe_send_message(channel, self.str.get('cmd-play-spotify-playlist-process', 'Processing playlist `{0}` (`{1}`)').format(parts[-1], song_url))
                            for i in res:
                                song_url = i['track']['name'] + ' ' + i['track']['artists'][0]['name']
                                log.debug('Processing {0}'.format(song_url))
                                await self.cmd_play(message, player, channel, author, permissions, leftover_args, song_url)
                            await self.safe_delete_message(procmesg)
                            return Response(self.str.get('cmd-play-spotify-playlist-queued', "Enqueued `{0}` with **{1}** songs.").format(parts[-1], len(res)))
                        
                        else:
                            raise exceptions.CommandError(self.str.get('cmd-play-spotify-unsupported', 'That is not a supported Spotify URI.'), expire_in=30)
                    except exceptions.SpotifyError:
                        raise exceptions.CommandError(self.str.get('cmd-play-spotify-invalid', 'You either provided an invalid URI, or there was a problem.'))

            # This lock prevent spamming play command to add entries that exceeds time limit/ maximum song limit
            async with self.aiolocks[_func_() + ':' + str(author.id)]:
                if permissions.max_songs and player.playlist.count_for_user(author) >= permissions.max_songs:
                    raise exceptions.PermissionsError(
                        self.str.get('cmd-play-limit', "You have reached your enqueued song limit ({0})").format(permissions.max_songs), expire_in=30
                    )

                if player.karaoke_mode and not permissions.bypass_karaoke_mode:
                    raise exceptions.PermissionsError(
                        self.str.get('karaoke-enabled', "Karaoke mode is enabled, please try again when its disabled!"), expire_in=30
                    )

                # Try to determine entry type, if _type is playlist then there should be entries
                while True:
                    try:
                        info = await self.downloader.extract_info(player.playlist.loop, song_url, download=False, process=False)
                        # If there is an exception arise when processing we go on and let extract_info down the line report it
                        # because info might be a playlist and thing that's broke it might be individual entry
                        try:
                            info_process = await self.downloader.extract_info(player.playlist.loop, song_url, download=False)
                        except:
                            info_process = None

                        log.debug(info)

                        if info_process and info and info_process.get('_type', None) == 'playlist' and 'entries' not in info and not info.get('url', '').startswith('ytsearch'):
                            use_url = info_process.get('webpage_url', None) or info_process.get('url', None)
                            if use_url == song_url:
                                log.warning("Determined incorrect entry type, but suggested url is the same.  Help.")
                                break # If we break here it will break things down the line and give "This is a playlist" exception as a result

                            log.debug("Assumed url \"%s\" was a single entry, was actually a playlist" % song_url)
                            log.debug("Using \"%s\" instead" % use_url)
                            song_url = use_url
                        else:
                            break

                    except Exception as e:
                        if 'unknown url type' in str(e):
                            song_url = song_url.replace(':', '')  # it's probably not actually an extractor
                            info = await self.downloader.extract_info(player.playlist.loop, song_url, download=False, process=False)
                        else:
                            raise exceptions.CommandError(e, expire_in=30)

                if not info:
                    raise exceptions.CommandError(
                        self.str.get('cmd-play-noinfo', "That video cannot be played. Try using the {0}stream command.").format(self.config.command_prefix),
                        expire_in=30
                    )

                if info.get('extractor', '') not in permissions.extractors and permissions.extractors:
                    raise exceptions.PermissionsError(
                        self.str.get('cmd-play-badextractor', "You do not have permission to play media from this service."), expire_in=30
                    )

                # abstract the search handling away from the user
                # our ytdl options allow us to use search strings as input urls
                if info.get('url', '').startswith('ytsearch'):
                    # print("[Command:play] Searching for \"%s\"" % song_url)
                    info = await self.downloader.extract_info(
                        player.playlist.loop,
                        song_url,
                        download=False,
                        process=True,    # ASYNC LAMBDAS WHEN
                        on_error=lambda e: asyncio.ensure_future(
                            self.safe_send_message(channel, "```\n%s\n```" % e, expire_in=120), loop=self.loop),
                        retry_on_error=True
                    )

                    if not info:
                        raise exceptions.CommandError(
                            self.str.get('cmd-play-nodata', "Error extracting info from search string, youtubedl returned no data. "
                                                            "You may need to restart the bot if this continues to happen."), expire_in=30
                        )

                    if not all(info.get('entries', [])):
                        # empty list, no data
                        log.debug("Got empty list, no data")
                        return

                    # TODO: handle 'webpage_url' being 'ytsearch:...' or extractor type
                    song_url = info['entries'][0]['webpage_url']
                    info = await self.downloader.extract_info(player.playlist.loop, song_url, download=False, process=False)
                    # Now I could just do: return await self.cmd_play(player, channel, author, song_url)
                    # But this is probably fine

                # If it's playlist
                if 'entries' in info:
                    await self._do_playlist_checks(permissions, player, author, info['entries'])

                    num_songs = sum(1 for _ in info['entries'])

                    if info['extractor'].lower() in ['youtube:playlist', 'soundcloud:set', 'bandcamp:album']:
                        try:
                            return await self._cmd_play_playlist_async(player, channel, author, permissions, song_url, info['extractor'])
                        except exceptions.CommandError:
                            raise
                        except Exception as e:
                            log.error("Error queuing playlist", exc_info=True)
                            raise exceptions.CommandError(self.str.get('cmd-play-playlist-error', "Error queuing playlist:\n`{0}`").format(e), expire_in=30)

                    t0 = time.time()

                    # My test was 1.2 seconds per song, but we maybe should fudge it a bit, unless we can
                    # monitor it and edit the message with the estimated time, but that's some ADVANCED SHIT
                    # I don't think we can hook into it anyways, so this will have to do.
                    # It would probably be a thread to check a few playlists and get the speed from that
                    # Different playlists might download at different speeds though
                    wait_per_song = 1.2

                    procmesg = await self.safe_send_message(
                        channel,
                        self.str.get('cmd-play-playlist-gathering-1', 'Gathering playlist information for {0} songs{1}').format(
                            num_songs,
                            self.str.get('cmd-play-playlist-gathering-2', ', ETA: {0} seconds').format(fixg(
                                num_songs * wait_per_song)) if num_songs >= 10 else '.'))

                    # We don't have a pretty way of doing this yet.  We need either a loop
                    # that sends these every 10 seconds or a nice context manager.
                    await self.send_typing(channel)

                    # TODO: I can create an event emitter object instead, add event functions, and every play list might be asyncified
                    #       Also have a "verify_entry" hook with the entry as an arg and returns the entry if its ok

                    entry_list, position = await player.playlist.import_from(song_url, channel=channel, author=author)

                    tnow = time.time()
                    ttime = tnow - t0
                    listlen = len(entry_list)
                    drop_count = 0

                    if permissions.max_song_length:
                        for e in entry_list.copy():
                            if e.duration > permissions.max_song_length:
                                player.playlist.entries.remove(e)
                                entry_list.remove(e)
                                drop_count += 1
                                # Im pretty sure there's no situation where this would ever break
                                # Unless the first entry starts being played, which would make this a race condition
                        if drop_count:
                            print("Dropped %s songs" % drop_count)

                    log.info("Processed {} songs in {} seconds at {:.2f}s/song, {:+.2g}/song from expected ({}s)".format(
                        listlen,
                        fixg(ttime),
                        ttime / listlen if listlen else 0,
                        ttime / listlen - wait_per_song if listlen - wait_per_song else 0,
                        fixg(wait_per_song * num_songs))
                    )

                    await self.safe_delete_message(procmesg)

                    if not listlen - drop_count:
                        raise exceptions.CommandError(
                            self.str.get('cmd-play-playlist-maxduration', "No songs were added, all songs were over max duration (%ss)") % permissions.max_song_length,
                            expire_in=30
                        )

                    reply_text = self.str.get('cmd-play-playlist-reply', "Enqueued **%s** songs to be played. Position in queue: %s")
                    btext = str(listlen - drop_count)

                # If it's an entry
                else:
                    # youtube:playlist extractor but it's actually an entry
                    if info.get('extractor', '').startswith('youtube:playlist'):
                        try:
                            info = await self.downloader.extract_info(player.playlist.loop, 'https://www.youtube.com/watch?v=%s' % info.get('url', ''), download=False, process=False)
                        except Exception as e:
                            raise exceptions.CommandError(e, expire_in=30)

                    if permissions.max_song_length and info.get('duration', 0) > permissions.max_song_length:
                        raise exceptions.PermissionsError(
                            self.str.get('cmd-play-song-limit', "Song duration exceeds limit ({0} > {1})").format(info['duration'], permissions.max_song_length),
                            expire_in=30
                        )

                    entry, position = await player.playlist.add_entry(song_url, channel=channel, author=author)

                    reply_text = self.str.get('cmd-play-song-reply', "Enqueued `%s` to be played. Position in queue: %s")
                    btext = entry.title

                if position == 1 and player.is_stopped:
                    position = self.str.get('cmd-play-next', 'Up next!')
                    reply_text %= (btext, position)

                else:
                    try:
                        time_until = await player.playlist.estimate_time_until(position, player)
                        reply_text += self.str.get('cmd-play-eta', ' - estimated time until playing: %s')
                    except:
                        traceback.print_exc()
                        time_until = ''

                    reply_text %= (btext, position, ftimedelta(time_until))

            return Response(reply_text, delete_after=30)

    async def _cmd_play_playlist_async(self, player, channel, author, permissions, playlist_url, extractor_type):
        """
        Secret handler to use the async wizardry to make playlist queuing non-"blocking"
        """

        #await self.send_typing(channel)
        async with channel.typing(): # 2023-04-22: error handling?
            info = await self.downloader.extract_info(player.playlist.loop, playlist_url, download=False, process=False)

        if not info:
            raise exceptions.CommandError(self.str.get('cmd-play-playlist-invalid', "That playlist cannot be played."))

        num_songs = sum(1 for _ in info['entries'])
        t0 = time.time()

        busymsg = await self.safe_send_message(
            channel, self.str.get('cmd-play-playlist-process', "Processing {0} songs...").format(num_songs))  # TODO: From playlist_title
        await self.send_typing(channel)

        entries_added = 0
        if extractor_type == 'youtube:playlist':
            try:
                entries_added = await player.playlist.async_process_youtube_playlist(
                    playlist_url, channel=channel, author=author)
                # TODO: Add hook to be called after each song
                # TODO: Add permissions

            except Exception:
                log.error("Error processing playlist", exc_info=True)
                raise exceptions.CommandError(self.str.get('cmd-play-playlist-queueerror', 'Error handling playlist {0} queuing.').format(playlist_url), expire_in=30)

        elif extractor_type.lower() in ['soundcloud:set', 'bandcamp:album']:
            try:
                entries_added = await player.playlist.async_process_sc_bc_playlist(
                    playlist_url, channel=channel, author=author)
                # TODO: Add hook to be called after each song
                # TODO: Add permissions

            except Exception:
                log.error("Error processing playlist", exc_info=True)
                raise exceptions.CommandError(self.str.get('cmd-play-playlist-queueerror', 'Error handling playlist {0} queuing.').format(playlist_url), expire_in=30)


        songs_processed = len(entries_added)
        drop_count = 0
        skipped = False

        if permissions.max_song_length:
            for e in entries_added.copy():
                if e.duration > permissions.max_song_length:
                    try:
                        player.playlist.entries.remove(e)
                        entries_added.remove(e)
                        drop_count += 1
                    except:
                        pass

            if drop_count:
                log.debug("Dropped %s songs" % drop_count)

            if player.current_entry and player.current_entry.duration > permissions.max_song_length:
                await self.safe_delete_message(self.server_specific_data[channel.guild]['last_np_msg'])
                self.server_specific_data[channel.guild]['last_np_msg'] = None
                skipped = True
                player.skip()
                entries_added.pop()

        await self.safe_delete_message(busymsg)

        songs_added = len(entries_added)
        tnow = time.time()
        ttime = tnow - t0
        wait_per_song = 1.2
        # TODO: actually calculate wait per song in the process function and return that too

        # This is technically inaccurate since bad songs are ignored but still take up time
        log.info("Processed {}/{} songs in {} seconds at {:.2f}s/song, {:+.2g}/song from expected ({}s)".format(
            songs_processed,
            num_songs,
            fixg(ttime),
            ttime / num_songs if num_songs else 0,
            ttime / num_songs - wait_per_song if num_songs - wait_per_song else 0,
            fixg(wait_per_song * num_songs))
        )

        if not songs_added:
            basetext = self.str.get('cmd-play-playlist-maxduration', "No songs were added, all songs were over max duration (%ss)") % permissions.max_song_length
            if skipped:
                basetext += self.str.get('cmd-play-playlist-skipped', "\nAdditionally, the current song was skipped for being too long.")

            raise exceptions.CommandError(basetext, expire_in=30)

        return Response(self.str.get('cmd-play-playlist-reply-secs', "Enqueued {0} songs to be played in {1} seconds").format(
            songs_added, fixg(ttime, 1)), delete_after=30)

    async def cmd_stream(self, player, channel, author, permissions, song_url):
        """
        Usage:
            {command_prefix}stream song_link

        Enqueue a media stream.
        This could mean an actual stream like Twitch or shoutcast, or simply streaming
        media without predownloading it.  Note: FFmpeg is notoriously bad at handling
        streams, especially on poor connections.  You have been warned.
        """

        song_url = song_url.strip('<>')

        if permissions.max_songs and player.playlist.count_for_user(author) >= permissions.max_songs:
            raise exceptions.PermissionsError(
                self.str.get('cmd-stream-limit', "You have reached your enqueued song limit ({0})").format(permissions.max_songs), expire_in=30
            )

        if player.karaoke_mode and not permissions.bypass_karaoke_mode:
            raise exceptions.PermissionsError(
                self.str.get('karaoke-enabled', "Karaoke mode is enabled, please try again when its disabled!"), expire_in=30
            )

        await self.send_typing(channel)
        await player.playlist.add_stream_entry(song_url, channel=channel, author=author)

        return Response(self.str.get('cmd-stream-success', "Streaming."), delete_after=6)

    async def cmd_search(self, message, player, channel, author, permissions, leftover_args):
        """
        Usage:
            {command_prefix}search [service] [number] query

        Searches a service for a video and adds it to the queue.
        - service: any one of the following services:
            - youtube (yt) (default if unspecified)
            - soundcloud (sc)
            - yahoo (yh)
        - number: return a number of video results and waits for user to choose one
          - defaults to 3 if unspecified
          - note: If your search query starts with a number,
                  you must put your query in quotes
            - ex: {command_prefix}search 2 "I ran seagulls"
        The command issuer can use reactions to indicate their response to each result.
        """

        if permissions.max_songs and player.playlist.count_for_user(author) > permissions.max_songs:
            raise exceptions.PermissionsError(
                self.str.get('cmd-search-limit', "You have reached your playlist item limit ({0})").format(permissions.max_songs),
                expire_in=30
            )

        if player.karaoke_mode and not permissions.bypass_karaoke_mode:
            raise exceptions.PermissionsError(
                self.str.get('karaoke-enabled', "Karaoke mode is enabled, please try again when its disabled!"), expire_in=30
            )

        def argcheck():
            if not leftover_args:
                # noinspection PyUnresolvedReferences
                raise exceptions.CommandError(
                    self.str.get('cmd-search-noquery', "Please specify a search query.\n%s") % dedent(
                        self.cmd_search.__doc__.format(command_prefix=self.config.command_prefix)),
                    expire_in=60
                )

        argcheck()

        try:
            leftover_args = shlex.split(' '.join(leftover_args))
        except ValueError:
            raise exceptions.CommandError(self.str.get('cmd-search-noquote', "Please quote your search query properly."), expire_in=30)

        service = 'youtube'
        items_requested = 3
        max_items = permissions.max_search_items
        services = {
            'youtube': 'ytsearch',
            'soundcloud': 'scsearch',
            'yahoo': 'yvsearch',
            'yt': 'ytsearch',
            'sc': 'scsearch',
            'yh': 'yvsearch'
        }

        if leftover_args[0] in services:
            service = leftover_args.pop(0)
            argcheck()

        if leftover_args[0].isdigit():
            items_requested = int(leftover_args.pop(0))
            argcheck()

            if items_requested > max_items:
                raise exceptions.CommandError(self.str.get('cmd-search-searchlimit', "You cannot search for more than %s videos") % max_items)

        # Look jake, if you see this and go "what the fuck are you doing"
        # and have a better idea on how to do this, i'd be delighted to know.
        # I don't want to just do ' '.join(leftover_args).strip("\"'")
        # Because that eats both quotes if they're there
        # where I only want to eat the outermost ones
        if leftover_args[0][0] in '\'"':
            lchar = leftover_args[0][0]
            leftover_args[0] = leftover_args[0].lstrip(lchar)
            leftover_args[-1] = leftover_args[-1].rstrip(lchar)

        search_query = '%s%s:%s' % (services[service], items_requested, ' '.join(leftover_args))

        search_msg = await self.safe_send_message(channel, self.str.get('cmd-search-searching', "Searching for videos..."))
        await self.send_typing(channel)

        try:
            info = await self.downloader.extract_info(player.playlist.loop, search_query, download=False, process=True)

        except Exception as e:
            await self.safe_edit_message(search_msg, str(e), send_if_fail=True)
            return
        else:
            await self.safe_delete_message(search_msg)

        if not info:
            return Response(self.str.get('cmd-search-none', "No videos found."), delete_after=30)

        for e in info['entries']:
            result_message = await self.safe_send_message(channel, self.str.get('cmd-search-result', "Result {0}/{1}: {2}").format(
                info['entries'].index(e) + 1, len(info['entries']), e['webpage_url']))

            def check(reaction, user):
                return user == message.author and reaction.message.id == result_message.id  # why can't these objs be compared directly?

            reactions = ['\u2705', '\U0001F6AB', '\U0001F3C1']
            for r in reactions:
                await result_message.add_reaction(r)

            try:
                reaction, user = await self.wait_for('reaction_add', timeout=30.0, check=check)
            except asyncio.TimeoutError:
                await self.safe_delete_message(result_message)
                return

            if str(reaction.emoji) == '\u2705':  # check
                await self.safe_delete_message(result_message)
                await self.cmd_play(message, player, channel, author, permissions, [], e['webpage_url'])
                return Response(self.str.get('cmd-search-accept', "Alright, coming right up!"), delete_after=30)
            elif str(reaction.emoji) == '\U0001F6AB':  # cross
                await self.safe_delete_message(result_message)
                continue
            else:
                await self.safe_delete_message(result_message)
                break

        return Response(self.str.get('cmd-search-decline', "Oh well :("), delete_after=30)

    async def cmd_np(self, player, channel, guild, message):
        """
        Usage:
            {command_prefix}np

        Displays the current song in chat.
        """

        if player.current_entry:
            if self.server_specific_data[guild]['last_np_msg']:
                await self.safe_delete_message(self.server_specific_data[guild]['last_np_msg'])
                self.server_specific_data[guild]['last_np_msg'] = None

            # TODO: Fix timedelta garbage with util function
            song_progress = ftimedelta(timedelta(seconds=player.progress))
            song_total = ftimedelta(timedelta(seconds=player.current_entry.duration))

            streaming = isinstance(player.current_entry, StreamPlaylistEntry)
            prog_str = ('`[{progress}]`' if streaming else '`[{progress}/{total}]`').format(
                progress=song_progress, total=song_total
            )
            prog_bar_str = ''

            # percentage shows how much of the current song has already been played
            percentage = 0.0
            if player.current_entry.duration > 0:
                percentage = player.progress / player.current_entry.duration

            # create the actual bar
            progress_bar_length = 30
            for i in range(progress_bar_length):
                if (percentage < 1 / progress_bar_length * i):
                    prog_bar_str += '□'
                else:
                    prog_bar_str += '■'

            action_text = self.str.get('cmd-np-action-streaming', 'Streaming') if streaming else self.str.get('cmd-np-action-playing', 'Playing')

            if player.current_entry.meta.get('channel', False) and player.current_entry.meta.get('author', False):
                np_text = self.str.get('cmd-np-reply-author', "Now {action}: **{title}** added by **{author}**\nProgress: {progress_bar} {progress}\n\N{WHITE RIGHT POINTING BACKHAND INDEX} <{url}>").format(
                    action=action_text,
                    title=player.current_entry.title,
                    author=player.current_entry.meta['author'].name,
                    progress_bar=prog_bar_str,
                    progress=prog_str,
                    url=player.current_entry.url
                )
            else:

                np_text = self.str.get('cmd-np-reply-noauthor', "Now {action}: **{title}**\nProgress: {progress_bar} {progress}\n\N{WHITE RIGHT POINTING BACKHAND INDEX} <{url}>").format(

                    action=action_text,
                    title=player.current_entry.title,
                    progress_bar=prog_bar_str,
                    progress=prog_str,
                    url=player.current_entry.url
                )

            self.server_specific_data[guild]['last_np_msg'] = await self.safe_send_message(channel, np_text)
            await self._manual_delete_check(message)
        else:
            return Response(
                self.str.get('cmd-np-none', 'There are no songs queued! Queue something with {0}play.') .format(self.config.command_prefix),
                delete_after=30
            )

    async def cmd_summon(self, channel, guild, author, voice_channel):
        """
        Usage:
            {command_prefix}summon

        Call the bot to the summoner's voice channel.
        """
        #print("EEEE")
        if not author.voice:
            raise exceptions.CommandError(self.str.get('cmd-summon-novc', 'You are not connected to voice. Try joining a voice channel!'))

        voice_client = self.voice_client_in(guild)
        if voice_client and guild == author.voice.channel.guild: #When switching from one channel to another in the same guild
            #print("CCCC")
            await voice_client.move_to(author.voice.channel)

        else:   #When joining a channel after not being in a channel in that guild
            #print("DDDD")
            # move to _verify_vc_perms?
            chperms = author.voice.channel.permissions_for(guild.me)
            #print("WWWWW")

            if not chperms.connect:
                #print("DA")
                log.warning("Cannot join channel '{0}', no permission.".format(author.voice.channel.name))
                raise exceptions.CommandError(
                    self.str.get('cmd-summon-noperms-connect', "Cannot join channel `{0}`, no permission to connect.").format(author.voice.channel.name),
                    expire_in=25
                )

            elif not chperms.speak:
                #print("DB)")
                log.warning("Cannot join channel '{0}', no permission to speak.".format(author.voice.channel.name))
                raise exceptions.CommandError(
                    self.str.get('cmd-summon-noperms-speak', "Cannot join channel `{0}`, no permission to speak.").format(author.voice.channel.name),
                    expire_in=25
                )
            #print("GGGGG")
            player = await self.get_player(author.voice.channel, create=True, deserialize=self.config.persistent_queue)
            #print("RRRRR")
            if player.is_stopped:
                player.play()

            if self.config.auto_playlist:
                await self.on_player_finished_playing(player)

        #print("AAA")
        log.info("Joining {0.guild.name}/{0.name}".format(author.voice.channel))
        #print("BBB")
        return Response(self.str.get('cmd-summon-reply', 'Connected to `{0.name}`').format(author.voice.channel))

    async def cmd_pause(self, player):
        """
        Usage:
            {command_prefix}pause

        Pauses playback of the current song.
        """

        if player.is_playing:
            player.pause()
            return Response(self.str.get('cmd-pause-reply', 'Paused music in `{0.name}`').format(player.voice_client.channel))

        else:
            raise exceptions.CommandError(self.str.get('cmd-pause-none', 'Player is not playing.'), expire_in=30)

    async def cmd_resume(self, player):
        """
        Usage:
            {command_prefix}resume

        Resumes playback of a paused song.
        """

        if player.is_paused:
            player.resume()
            return Response(self.str.get('cmd-resume-reply', 'Resumed music in `{0.name}`').format(player.voice_client.channel), delete_after=15)

        else:
            raise exceptions.CommandError(self.str.get('cmd-resume-none', 'Player is not paused.'), expire_in=30)

    async def cmd_shuffle(self, channel, player):
        """
        Usage:
            {command_prefix}shuffle

        Shuffles the server's queue.
        """

        player.playlist.shuffle()

        cards = ['\N{BLACK SPADE SUIT}', '\N{BLACK CLUB SUIT}', '\N{BLACK HEART SUIT}', '\N{BLACK DIAMOND SUIT}']
        random.shuffle(cards)

        hand = await self.safe_send_message(channel, ' '.join(cards))
        await asyncio.sleep(0.6)

        for x in range(4):
            random.shuffle(cards)
            await self.safe_edit_message(hand, ' '.join(cards))
            await asyncio.sleep(0.6)

        await self.safe_delete_message(hand, quiet=True)
        return Response(self.str.get('cmd-shuffle-reply', "Shuffled `{0}`'s queue.").format(player.voice_client.channel.guild), delete_after=15)

    async def cmd_clear(self, player, author):
        """
        Usage:
            {command_prefix}clear

        Clears the playlist.
        """

        player.playlist.clear()
        return Response(self.str.get('cmd-clear-reply', "Cleared `{0}`'s queue").format(player.voice_client.channel.guild), delete_after=20)

    async def cmd_remove(self, user_mentions, message, author, permissions, channel, player, index=None):
        """
        Usage:
            {command_prefix}remove [# in queue]

        Removes queued songs. If a number is specified, removes that song in the queue, otherwise removes the most recently queued song.
        """

        if not player.playlist.entries:
            raise exceptions.CommandError(self.str.get('cmd-remove-none', "There's nothing to remove!"), expire_in=20)

        if user_mentions:
            for user in user_mentions:
                if permissions.remove or author == user:
                    try:
                        entry_indexes = [e for e in player.playlist.entries if e.meta.get('author', None) == user]
                        for entry in entry_indexes:
                            player.playlist.entries.remove(entry)
                        entry_text = '%s ' % len(entry_indexes) + 'item'
                        if len(entry_indexes) > 1:
                            entry_text += 's'
                        return Response(self.str.get('cmd-remove-reply', "Removed `{0}` added by `{1}`").format(entry_text, user.name).strip())

                    except ValueError:
                        raise exceptions.CommandError(self.str.get('cmd-remove-missing', "Nothing found in the queue from user `%s`") % user.name, expire_in=20)

                raise exceptions.PermissionsError(
                    self.str.get('cmd-remove-noperms', "You do not have the valid permissions to remove that entry from the queue, make sure you're the one who queued it or have instant skip permissions"), expire_in=20)

        if not index:
            index = len(player.playlist.entries)

        try:
            index = int(index)
        except (TypeError, ValueError):
            raise exceptions.CommandError(self.str.get('cmd-remove-invalid', "Invalid number. Use {}queue to find queue positions.").format(self.config.command_prefix), expire_in=20)

        if index > len(player.playlist.entries):
            raise exceptions.CommandError(self.str.get('cmd-remove-invalid', "Invalid number. Use {}queue to find queue positions.").format(self.config.command_prefix), expire_in=20)

        if permissions.remove or author == player.playlist.get_entry_at_index(index - 1).meta.get('author', None):
            entry = player.playlist.delete_entry_at_index((index - 1))
            await self._manual_delete_check(message)
            if entry.meta.get('channel', False) and entry.meta.get('author', False):
                return Response(self.str.get('cmd-remove-reply-author', "Removed entry `{0}` added by `{1}`").format(entry.title, entry.meta['author'].name).strip())
            else:
                return Response(self.str.get('cmd-remove-reply-noauthor', "Removed entry `{0}`").format(entry.title).strip())
        else:
            raise exceptions.PermissionsError(
                self.str.get('cmd-remove-noperms', "You do not have the valid permissions to remove that entry from the queue, make sure you're the one who queued it or have instant skip permissions"), expire_in=20
            )

    async def cmd_skip(self, player, channel, author, message, permissions, voice_channel, param=''):
        """
        Usage:
            {command_prefix}skip [force/f]

        Skips the current song when enough votes are cast.
        Owners and those with the instaskip permission can add 'force' or 'f' after the command to force skip.
        """

        if player.is_stopped:
            raise exceptions.CommandError(self.str.get('cmd-skip-none', "Can't skip! The player is not playing!"), expire_in=20)

        if not player.current_entry:
            if player.playlist.peek():
                if player.playlist.peek()._is_downloading:
                    return Response(self.str.get('cmd-skip-dl', "The next song (`%s`) is downloading, please wait.") % player.playlist.peek().title)

                elif player.playlist.peek().is_downloaded:
                    print("The next song will be played shortly.  Please wait.")
                else:
                    print("Something odd is happening.  "
                          "You might want to restart the bot if it doesn't start working.")
            else:
                print("Something strange is happening.  "
                      "You might want to restart the bot if it doesn't start working.")
        
        current_entry = player.current_entry

        if (param.lower() in ['force', 'f']) or self.config.legacy_skip:
            if permissions.instaskip \
                or (self.config.allow_author_skip and author == player.current_entry.meta.get('author', None)):

                player.skip()  # TODO: check autopause stuff here
                await self._manual_delete_check(message)
                return Response(self.str.get('cmd-skip-force', 'Force skipped `{}`.').format(current_entry.title), reply=True, delete_after=30)
            else:
                raise exceptions.PermissionsError(self.str.get('cmd-skip-force-noperms', 'You do not have permission to force skip.'), expire_in=30)

        # TODO: ignore person if they're deaf or take them out of the list or something?
        # Currently is recounted if they vote, deafen, then vote

        num_voice = sum(1 for m in voice_channel.members if not (
            m.voice.deaf or m.voice.self_deaf or m == self.user))
        if num_voice == 0: num_voice = 1 # incase all users are deafened, to avoid divison by zero

        num_skips = player.skip_state.add_skipper(author.id, message)

        skips_remaining = min(
            self.config.skips_required,
            math.ceil(self.config.skip_ratio_required / (1 / num_voice))  # Number of skips from config ratio
        ) - num_skips

        if skips_remaining <= 0:
            player.skip()  # check autopause stuff here
            # @TheerapakG: Check for pausing state in the player.py make more sense
            return Response(
                self.str.get('cmd-skip-reply-skipped-1', 'Your skip for `{0}` was acknowledged.\nThe vote to skip has been passed.{1}').format(
                    current_entry.title,
                    self.str.get('cmd-skip-reply-skipped-2', ' Next song coming up!') if player.playlist.peek() else ''
                ),
                reply=True,
                delete_after=20
            )

        else:
            # TODO: When a song gets skipped, delete the old x needed to skip messages
            return Response(
                self.str.get('cmd-skip-reply-voted-1', 'Your skip for `{0}` was acknowledged.\n**{1}** more {2} required to vote to skip this song.').format(
                    current_entry.title,
                    skips_remaining,
                    self.str.get('cmd-skip-reply-voted-2', 'person is') if skips_remaining == 1 else self.str.get('cmd-skip-reply-voted-3', 'people are')
                ),
                reply=True,
                delete_after=20
            )

    async def cmd_volume(self, message, player, new_volume=None):
        """
        Usage:
            {command_prefix}volume (+/-)[volume]

        Sets the playback volume. Accepted values are from 1 to 100.
        Putting + or - before the volume will make the volume change relative to the current volume.
        """

        if not new_volume:
            return Response(self.str.get('cmd-volume-current', 'Current volume: `%s%%`') % int(player.volume * 100), reply=True, delete_after=20)

        relative = False
        if new_volume[0] in '+-':
            relative = True

        try:
            new_volume = int(new_volume)

        except ValueError:
            raise exceptions.CommandError(self.str.get('cmd-volume-invalid', '`{0}` is not a valid number').format(new_volume), expire_in=20)

        vol_change = None
        if relative:
            vol_change = new_volume
            new_volume += (player.volume * 100)

        old_volume = int(player.volume * 100)

        if 0 < new_volume <= 100:
            player.volume = new_volume / 100.0

            return Response(self.str.get('cmd-volume-reply', 'Updated volume from **%d** to **%d**') % (old_volume, new_volume), reply=True, delete_after=20)

        else:
            if relative:
                raise exceptions.CommandError(
                    self.str.get('cmd-volume-unreasonable-relative', 'Unreasonable volume change provided: {}{:+} -> {}%.  Provide a change between {} and {:+}.').format(
                        old_volume, vol_change, old_volume + vol_change, 1 - old_volume, 100 - old_volume), expire_in=20)
            else:
                raise exceptions.CommandError(
                    self.str.get('cmd-volume-unreasonable-absolute', 'Unreasonable volume provided: {}%. Provide a value between 1 and 100.').format(new_volume), expire_in=20)

    @owner_only
    async def cmd_option(self, player, option, value):
        """
        Usage:
            {command_prefix}option [option] [on/y/enabled/off/n/disabled]

        Changes a config option without restarting the bot. Changes aren't permanent and
        only last until the bot is restarted. To make permanent changes, edit the
        config file.

        Valid options:
            autoplaylist, save_videos, now_playing_mentions, auto_playlist_random, auto_pause,
            delete_messages, delete_invoking, write_current_song

        For information about these options, see the option's comment in the config file.
        """

        option = option.lower()
        value = value.lower()
        bool_y = ['on', 'y', 'enabled']
        bool_n = ['off', 'n', 'disabled']
        generic = ['save_videos', 'now_playing_mentions', 'auto_playlist_random',
                   'auto_pause', 'delete_messages', 'delete_invoking',
                   'write_current_song']  # these need to match attribute names in the Config class
        if option in ['autoplaylist', 'auto_playlist']:
            if value in bool_y:
                if self.config.auto_playlist:
                    raise exceptions.CommandError(self.str.get('cmd-option-autoplaylist-enabled', 'The autoplaylist is already enabled!'))
                else:
                    if not self.autoplaylist:
                        raise exceptions.CommandError(self.str.get('cmd-option-autoplaylist-none', 'There are no entries in the autoplaylist file.'))
                    self.config.auto_playlist = True
                    await self.on_player_finished_playing(player)
            elif value in bool_n:
                if not self.config.auto_playlist:
                    raise exceptions.CommandError(self.str.get('cmd-option-autoplaylist-disabled', 'The autoplaylist is already disabled!'))
                else:
                    self.config.auto_playlist = False
            else:
                raise exceptions.CommandError(self.str.get('cmd-option-invalid-value', 'The value provided was not valid.'))
            return Response("The autoplaylist is now " + ['disabled', 'enabled'][self.config.auto_playlist] + '.')
        else:
            is_generic = [o for o in generic if o == option]  # check if it is a generic bool option
            if is_generic and (value in bool_y or value in bool_n):
                name = is_generic[0]
                log.debug('Setting attribute {0}'.format(name))
                setattr(self.config, name, True if value in bool_y else False)  # this is scary but should work
                attr = getattr(self.config, name)
                res = "The option {0} is now ".format(option) + ['disabled', 'enabled'][attr] + '.'
                log.warning('Option overriden for this session: {0}'.format(res))
                return Response(res)
            else:
                raise exceptions.CommandError(self.str.get('cmd-option-invalid-param' ,'The parameters provided were invalid.'))

    async def cmd_queue(self, channel, player):
        """
        Usage:
            {command_prefix}queue

        Prints the current song queue.
        """

        lines = []
        unlisted = 0
        andmoretext = '* ... and %s more*' % ('x' * len(player.playlist.entries))

        if player.is_playing:
            # TODO: Fix timedelta garbage with util function
            song_progress = ftimedelta(timedelta(seconds=player.progress))
            song_total = ftimedelta(timedelta(seconds=player.current_entry.duration))
            prog_str = '`[%s/%s]`' % (song_progress, song_total)

            if player.current_entry.meta.get('channel', False) and player.current_entry.meta.get('author', False):
                lines.append(self.str.get('cmd-queue-playing-author', "Currently playing: `{0}` added by `{1}` {2}\n").format(
                    player.current_entry.title, player.current_entry.meta['author'].name, prog_str))
            else:
                lines.append(self.str.get('cmd-queue-playing-noauthor', "Currently playing: `{0}` {1}\n").format(player.current_entry.title, prog_str))


        for i, item in enumerate(player.playlist, 1):
            if item.meta.get('channel', False) and item.meta.get('author', False):
                nextline = self.str.get('cmd-queue-entry-author', '{0} -- `{1}` by `{2}`').format(i, item.title, item.meta['author'].name).strip()
            else:
                nextline = self.str.get('cmd-queue-entry-noauthor', '{0} -- `{1}`').format(i, item.title).strip()

            currentlinesum = sum(len(x) + 1 for x in lines)  # +1 is for newline char

            if (currentlinesum + len(nextline) + len(andmoretext) > DISCORD_MSG_CHAR_LIMIT) or (i > self.config.queue_length):
                if currentlinesum + len(andmoretext):
                    unlisted += 1
                    continue

            lines.append(nextline)

        if unlisted:
            lines.append(self.str.get('cmd-queue-more', '\n... and %s more') % unlisted)

        if not lines:
            lines.append(
                self.str.get('cmd-queue-none', 'There are no songs queued! Queue something with {}play.').format(self.config.command_prefix))

        message = '\n'.join(lines)
        return Response(message, delete_after=30)

    async def cmd_clean(self, message, channel, guild, author, search_range=50):
        """
        Usage:
            {command_prefix}clean [range]

        Removes up to [range] messages the bot has posted in chat. Default: 50, Max: 1000
        """

        try:
            float(search_range)  # lazy check
            search_range = min(int(search_range), 1000)
        except:
            return Response(self.str.get('cmd-clean-invalid', "Invalid parameter. Please provide a number of messages to search."), reply=True, delete_after=8)

        await self.safe_delete_message(message, quiet=True)

        def is_possible_command_invoke(entry):
            valid_call = any(
                entry.content.startswith(prefix) for prefix in [self.config.command_prefix])  # can be expanded
            return valid_call and not entry.content[1:2].isspace()

        delete_invokes = True
        delete_all = channel.permissions_for(author).manage_messages or self.config.owner_id == author.id

        def check(message):
            if is_possible_command_invoke(message) and delete_invokes:
                return delete_all or message.author == author
            return (message.author == self.user and not message.content.endswith("_ _"))

        if self.user.bot:
            if channel.permissions_for(guild.me).manage_messages:
                deleted = await channel.purge(check=check, limit=search_range, before=message)
                return Response(self.str.get('cmd-clean-reply', 'Cleaned up {0} message{1}.').format(len(deleted), 's' * bool(deleted)), delete_after=10)

    async def cmd_pldump(self, channel, author, song_url):
        """
        Usage:
            {command_prefix}pldump url

        Dumps the individual urls of a playlist
        """

        try:
            info = await self.downloader.extract_info(self.loop, song_url.strip('<>'), download=False, process=False)
        except Exception as e:
            raise exceptions.CommandError("Could not extract info from input url\n%s\n" % e, expire_in=25)

        if not info:
            raise exceptions.CommandError("Could not extract info from input url, no data.", expire_in=25)

        if not info.get('entries', None):
            # TODO: Retarded playlist checking
            # set(url, webpageurl).difference(set(url))

            if info.get('url', None) != info.get('webpage_url', info.get('url', None)):
                raise exceptions.CommandError("This does not seem to be a playlist.", expire_in=25)
            else:
                return await self.cmd_pldump(channel, info.get(''))

        linegens = defaultdict(lambda: None, **{
            "youtube":    lambda d: 'https://www.youtube.com/watch?v=%s' % d['id'],
            "soundcloud": lambda d: d['url'],
            "bandcamp":   lambda d: d['url']
        })

        exfunc = linegens[info['extractor'].split(':')[0]]

        if not exfunc:
            raise exceptions.CommandError("Could not extract info from input url, unsupported playlist type.", expire_in=25)

        with BytesIO() as fcontent:
            for item in info['entries']:
                fcontent.write(exfunc(item).encode('utf8') + b'\n')

            fcontent.seek(0)
            await author.send("Here's the playlist dump for <%s>" % song_url, file=discord.File(fcontent, filename='playlist.txt'))

        return Response("Sent a message with a playlist file.", delete_after=20)

    async def cmd_listids(self, guild, author, leftover_args, cat='all'):
        """
        Usage:
            {command_prefix}listids [categories]

        Lists the ids for various things.  Categories are:
           all, users, roles, channels
        """

        cats = ['channels', 'roles', 'users']

        if cat not in cats and cat != 'all':
            return Response(
                "Valid categories: " + ' '.join(['`%s`' % c for c in cats]),
                reply=True,
                delete_after=25
            )

        if cat == 'all':
            requested_cats = cats
        else:
            requested_cats = [cat] + [c.strip(',') for c in leftover_args]

        data = ['Your ID: %s' % author.id]

        for cur_cat in requested_cats:
            rawudata = None

            if cur_cat == 'users':
                data.append("\nUser IDs:")
                rawudata = ['%s #%s: %s' % (m.name, m.discriminator, m.id) for m in guild.members]

            elif cur_cat == 'roles':
                data.append("\nRole IDs:")
                rawudata = ['%s: %s' % (r.name, r.id) for r in guild.roles]

            elif cur_cat == 'channels':
                data.append("\nText Channel IDs:")
                tchans = [c for c in guild.channels if isinstance(c, discord.TextChannel)]
                rawudata = ['%s: %s' % (c.name, c.id) for c in tchans]

                rawudata.append("\nVoice Channel IDs:")
                vchans = [c for c in guild.channels if isinstance(c, discord.VoiceChannel)]
                rawudata.extend('%s: %s' % (c.name, c.id) for c in vchans)

            if rawudata:
                data.extend(rawudata)

        with BytesIO() as sdata:
            sdata.writelines(d.encode('utf8') + b'\n' for d in data)
            sdata.seek(0)

            # TODO: Fix naming (Discord20API-ids.txt)
            await author.send(file=discord.File(sdata, filename='%s-ids-%s.txt' % (guild.name.replace(' ', '_'), cat)))

        return Response("Sent a message with a list of IDs.", delete_after=20)


    async def cmd_perms(self, author, user_mentions, channel, guild, message, permissions, target=None):
        """
        Usage:
            {command_prefix}perms [@user]
        Sends the user a list of their permissions, or the permissions of the user specified.
        """

        if user_mentions:
            user = user_mentions[0]
            
        if not user_mentions and not target:
            user = author
            
        if not user_mentions and target:
            user = guild.get_member_named(target)
            if user == None:
                try:
                    user = await self.fetch_user(target)
                except discord.NotFound:
                    return Response("Invalid user ID or server nickname, please double check all typing and try again.", reply=False, delete_after=30)

        permissions = self.permissions.for_user(user)    
                    
        if user == author:
            lines = ['Command permissions in %s\n' % guild.name, '```', '```']
        else:
            lines = ['Command permissions for {} in {}\n'.format(user.name, guild.name), '```', '```']

        for perm in permissions.__dict__:
            if perm in ['user_list'] or permissions.__dict__[perm] == set():
                continue
            lines.insert(len(lines) - 1, "%s: %s" % (perm, permissions.__dict__[perm]))

        await self.safe_send_message(author, '\n'.join(lines))
        return Response("\N{OPEN MAILBOX WITH RAISED FLAG}", delete_after=20)


    @owner_only
    async def cmd_setname(self, leftover_args, name):
        """
        Usage:
            {command_prefix}setname name

        Changes the bot's username.
        Note: This operation is limited by discord to twice per hour.
        """

        name = ' '.join([name, *leftover_args])

        try:
            await self.user.edit(username=name)

        except discord.HTTPException:
            raise exceptions.CommandError(
                "Failed to change name. Did you change names too many times?  "
                "Remember name changes are limited to twice per hour.")

        except Exception as e:
            raise exceptions.CommandError(e, expire_in=20)

        return Response("Set the bot's username to **{0}**".format(name), delete_after=20)

    async def cmd_setnick(self, guild, channel, leftover_args, nick):
        """
        Usage:
            {command_prefix}setnick nick

        Changes the bot's nickname.
        """

        if not channel.permissions_for(guild.me).change_nickname:
            raise exceptions.CommandError("Unable to change nickname: no permission.")

        nick = ' '.join([nick, *leftover_args])

        try:
            await guild.me.edit(nick=nick)
        except Exception as e:
            raise exceptions.CommandError(e, expire_in=20)

        return Response("Set the bot's nickname to `{0}`".format(nick), delete_after=20)

    @owner_only
    async def cmd_setavatar(self, message, url=None):
        """
        Usage:
            {command_prefix}setavatar [url]

        Changes the bot's avatar.
        Attaching a file and leaving the url parameter blank also works.
        """

        if message.attachments:
            thing = message.attachments[0].url
        elif url:
            thing = url.strip('<>')
        else:
            raise exceptions.CommandError("You must provide a URL or attach a file.", expire_in=20)

        try:
            timeout = aiohttp.ClientTimeout(total=10)
            async with self.aiosession.get(thing, timeout=timeout) as res:
                await self.user.edit(avatar=await res.read())

        except Exception as e:
            raise exceptions.CommandError("Unable to change avatar: {}".format(e), expire_in=20)

        return Response("Changed the bot's avatar.", delete_after=20)


    async def cmd_disconnect(self, guild):
        """
        Usage:
            {command_prefix}disconnect
        
        Forces the bot leave the current voice channel.
        """
        await self.disconnect_voice_client(guild)
        return Response("Disconnected from `{0.name}`".format(guild), delete_after=20)

    async def cmd_restart(self, channel):
        """
        Usage:
            {command_prefix}restart
        
        Restarts the bot.
        Will not properly load new dependencies or file updates unless fully shutdown
        and restarted.
        """
        await self.safe_send_message(channel, "\N{WAVING HAND SIGN} Restarting. If you have updated your bot "
            "or its dependencies, you need to restart the bot properly, rather than using this command.")

        player = self.get_player_in(channel.guild)
        if player and player.is_paused:
            player.resume()

        await self.disconnect_all_voice_clients()
        raise exceptions.RestartSignal()

    @dev_only
    async def cmd_shutdown(self, message, channel):
        """
        Usage:
            {command_prefix}shutdown
        
        Disconnects from voice channels and closes the bot process.
        """
        await self.safe_send_message(channel, "\N{WAVING HAND SIGN}")
        
        player = self.get_player_in(channel.guild)
        if player and player.is_paused:
            player.resume()
        
        await self.disconnect_all_voice_clients()
        await self.cmd_autosave(None)
        await message.add_reaction("❤️")
        raise exceptions.TerminateSignal()

    async def cmd_leaveserver(self, val, leftover_args):
        """
        Usage:
            {command_prefix}leaveserver <name/ID>

        Forces the bot to leave a server.
        When providing names, names are case-sensitive.
        """
        if leftover_args:
            val = ' '.join([val, *leftover_args])

        t = self.get_guild(val)
        if t is None:
            t = discord.utils.get(self.guilds, name=val)
            if t is None:
                raise exceptions.CommandError('No guild was found with the ID or name as `{0}`'.format(val))
        await t.leave()
        return Response('Left the guild: `{0.name}` (Owner: `{0.owner.name}`, ID: `{0.id}`)'.format(t))

    @dev_only
    async def cmd_breakpoint(self, message):
        log.critical("Activating debug breakpoint")
        return

    @dev_only
    async def cmd_objgraph(self, channel, func='most_common_types()'):
        import objgraph

        await self.send_typing(channel)

        if func == 'growth':
            f = StringIO()
            objgraph.show_growth(limit=10, file=f)
            f.seek(0)
            data = f.read()
            f.close()

        elif func == 'leaks':
            f = StringIO()
            objgraph.show_most_common_types(objects=objgraph.get_leaking_objects(), file=f)
            f.seek(0)
            data = f.read()
            f.close()

        elif func == 'leakstats':
            data = objgraph.typestats(objects=objgraph.get_leaking_objects())

        else:
            data = eval('objgraph.' + func)

        return Response(data, codeblock='py')

    @dev_only
    async def cmd_debug(self, message, _player, *, data):
        codeblock = "```py\n{}\n```"
        result = None

        if data.startswith('```') and data.endswith('```'):
            data = '\n'.join(data.rstrip('`\n').split('\n')[1:])

        code = data.strip('` \n')

        scope = globals().copy()
        scope.update({'self': self})

        try:
            result = eval(code, scope)
        except:
            try:
                exec(code, scope)
            except Exception as e:
                traceback.print_exc(chain=False)
                return Response("{}: {}".format(type(e).__name__, e))

        if asyncio.iscoroutine(result):
            result = await result

        return Response(codeblock.format(result))

    async def user_has_reacted(self, message, reaction_emoji, user_id):
        """
        Checks if a message has recieved reaction_emoji from the user specified by user_id.
        """
        for reaction in message.reactions:
            if reaction.emoji == reaction_emoji:
                async for user in reaction.users():
                    if user.id == user_id:
                        return True
                break
        return False

    async def on_raw_message_edit(self, payload):
        """
        Called whenever a message is edited.
        The 'raw' means it works regardless of internal message chache, but also that we need to grab the message ourselves.
        """
        #Allow typo correction for commands
        def is_valid_command(content): #Checks if the content represents a valid command
            content = content.strip().split(" ")[0]
            if content.startswith(self.config.command_prefix):
                command = content.replace(self.config.command_prefix, "", 1)
                if self.config.usealias:
                    alias = self.aliases.get(command)
                    command = alias if alias else command
                cmd = getattr(self, 'cmd_' + command, None)
                if cmd:
                    return True
            return False

        old_message = payload.cached_message
        if not old_message or (datetime.now(timezone.utc)-old_message.created_at).total_seconds()+14400>60 or old_message.edited_at or is_valid_command(old_message.content):#Check if the old message fits the criteria
            #print("NOPE")
            return
        
        #fetch the new edited message
        try:
            channel = self.get_channel(payload.channel_id)
            channel = channel if channel else await self.fetch_channel(payload.channel_id)
            new_message = await channel.fetch_message(payload.message_id)
        except:
            print("There was an issue in on_raw_message_edit while fetching the old message stuff")
            return

        #if new message is a valid command, run it through on_message
        if is_valid_command(new_message.content):
            await self.on_message(new_message) #run it through on_message
        

        

    async def on_raw_reaction_add(self, payload):
        """
        Called when a reaction is added to any message. 
        The 'raw' means it works regardless of internal message cache, but also that we need to grab more data ourselves.
        Handles all global reaction-related stuff.
        """
        #raw_reaction_add only gives us a payload, so we need to do extra work to get the info we need
        if payload.event_type=="REACTION_REMOVE": #ignore reaction removals (for now at least)
            return
        react_guild = self.get_guild(payload.guild_id)
        #if not react_guild: #This happens when a reaction is added to a DM
            #return

        react_channel = self.get_channel(payload.channel_id)
        if not react_channel:
            try:
                react_channel = await self.fetch_channel(payload.channel_id)
            except discord.errors.NotFound: #Something weird has happened
                print("Couldn't find the reacted channel :(")
                return

        try:
            message = await react_channel.fetch_message(payload.message_id) #(there is not method get_message, so an API call is unavoidable afaik)
        except discord.errors.NotFound: #Message has been removed already
            print("Couldn't find the reacted message :(")
            return
        user = payload.member if payload.member else self.get_user(payload.user_id) #get the user the easy way or the harder way
        reaction = None
        for r in message.reactions:
            if str(r.emoji) == str(payload.emoji):
                reaction = r
                break

        if user.bot or user == self.user: #ignore self and other bots
            return

        #Ignore reminder reactions for users not mentioned in the reaction (does not apply to devs)
        if message.author==self.user and message.content.startswith("Reminder for") and len(message.embeds)==1 and not (user in message.mentions or str(user.id) in self.config.dev_ids):
            return

        if reaction.emoji == "🔫" and message.content==("💩") and message.author==self.user and reaction.count<=2 and (await self.user_has_reacted(message, "🔫", self.user.id)): #bnuuy took a massive shit!
            newscore = self.changeScoreboard(user.id, "pooper_scooper")
            await message.delete()
            if newscore == 1:
                try:
                    dm_channel = await user.create_dm()
                    await dm_channel.send("Congrats on cleaning your first poop!\nEvery time you clean up my poop, you get a poop point! Use &poops in the bots channel to view the leaderboard!")
                except:
                    pass #don't try too hard
            return
        elif reaction.emoji == "🔫": #don't do anything for gun emojis that are not for a valid poop.
            return

        if reaction.emoji == "💾" and message.author == self.user and reaction.count<=2 and not message.content.endswith("_ _"):
            embeds = message.embeds
            content = message.content #.replace("_","\_")#make sure all the underscores stay there
            if len(embeds)<=0: #no embeds
                await message.channel.send(content+"_ _")
            else:
                await message.channel.send(content+"_ _",embed=embeds[0])
                embeds.pop(0)
            for embed in embeds: #just in case there was somehow more than one embed
               await message.channel.send(content+"_ _",embed=embed)

            await message.delete() #delete the original last to improve responsiveness
            return

        if reaction.emoji == "❌" and message.author == self.user:
            await message.delete()
            return

        if reaction.emoji == "🧹" and str(user.id) in self.config.dev_ids:
            await message.clear_reactions()
            return

        if str(reaction.emoji) in "❤️🏳️‍🌈🏴‍☠️🏳️‍⚧️": #copy these reactions always
            await message.add_reaction(reaction.emoji)
            return

        if reaction.emoji == "💤" and message.author==self.user and message.content.startswith("Reminder for") and len(message.embeds)==1 and user in message.mentions: #Snooze button for reminders
            dictionary = message.embeds[0].to_dict()
            #print(dictionary)
            remind_message = dictionary["fields"][0]["name"]
            if remind_message=="_ _":
                remind_message = dictionary["fields"][0]["value"]
            self.set_reminder(datetime.now()+timedelta(minutes=9),react_channel.id,user.id,remind_message+" (snoozed)")
            await message.delete() #prevents spamming of snooze button
            return

        if reaction.emoji == "📷" and not await self.user_has_reacted(message, "📸", self.user.id):
            await self.cmd_addquote(message, message.channel, reactedMessage=message)
            await message.add_reaction("📸")
            return

        if random.random()<=0.075 and not message.author==self.user: #Sometime copy a reaction
            if not reaction.emoji in "1️⃣2️⃣3️⃣4️⃣5️⃣6️⃣7️⃣8️⃣9️⃣🔟0️⃣": #exclude some emojis
                await message.add_reaction(reaction.emoji)
                return
        return

    async def on_member_update(self, before, after):
        oldname = before.name if before.nick==None else before.nick
        newname = after.name if after.nick==None else after.nick

        #{ig} tag used to prevent an infinite loop, since changing a nickname in here will call the method again.
        #Not ideal or foolproof, but it's the only way I could come up with.

        try:
            if oldname.startswith("{ig}"): 
                return
            elif newname.startswith("{ig}"):
                await after.edit(nick=newname.replace("{ig}",""))
                return
            elif before.id in self.margaret_ids and oldname != newname and datetime.today().weekday()>4: #Katie cannot change username on weekends
                await after.edit(nick="{ig}"+oldname)
        except discord.errors.Forbidden:
            print("Exception: Could not prevent name change to: "+newname)
        return


    def keymash_test(self,input_text):
        """
        Runs two tests to determine how confident the bot is that an input string is keymashing.
        Outputs a value from 0.0 to 1.0, where 1.0 is 100% confidence the input is keymashing.
        """
        characters = "abcdefghijklmnopqrstuvwxyz. "
        def get_number(text):
            number = 0
            for i in range(0,len(text)):
                number *= len(characters)
                number += characters.find(text[i])
            return number

        def chi_squared(o,e):
            sum = 0
            for i in range(0,len(o)):
                sum += ((o[i]-e[i])**2)/max(e[i],0.00001)
            return sum

        def get_list(text):
            text = text.lower()
            list = []
            singles = len(characters)
            doubles = len(characters)**2
            for i in range (0, singles+doubles):
                list.append(0)
            length = len(text)
    
            for i in range(0,length):
                f = text[i]
                s = text[min(i+1,length-1)]

                f = " " if not f in characters else f
                s = " " if not s in characters else s

                single = get_number(f)
                double = get_number(f+s)
                list[single] += 1
                if i<=length-2:
                    list[double+singles] += 1
            for i in range(0,len(list)):
                list[i] /= length
            return list

        def test_input(input_list, data_list, text_length):
            return chi_squared(input_list, data_list)/text_length

        #save memory by re-using the same lists for both tests
        data_list = []
        input_list = get_list(input_text)

        with open(os.path.abspath("keymash_data/data_eng.txt"),"r") as f:
            for line in f.readlines():
                data_list.append(float(line))
        eng_score = test_input(input_list, data_list, len(input_text))

        with open(os.path.abspath("keymash_data/data_kma.txt"),"r") as f:
            lines = f.readlines()
            for i in range(0,len(lines)):
                data_list[i]=float(lines[i])
        kma_score = test_input(input_list, data_list, len(input_text))

        return (eng_score/(eng_score+kma_score))

    def scramble(self, input):
        """
        Returns a scrambled version of input.
        """
        output = ""
        while len(output)<=len(input)*0.8:
            start = random.randint(0,len(input)-1)
            end = random.randint(start,min(len(input)-1,start+4))
            output += input[start:end]
        return output

    def add_typos(self, input):
        """
        Inserts realistic typos into an input string. 
        Does its best not to affect newlines and anything inside curly-braces (i.e. won't break response tags)
        """
        keyboard = ["qwertyuiop[","aasdfghjkl;","zzxcvbnm,./"] #approximation of a qwerty-keyboard layout
        output = ""
        inkey = False #used to prevent the typos from affecting any keys that may have made it this far
        for i in range(len(input)):
            letter = input[i]
            if letter=="{":
                inkey=True
            if len(output)>0 and output[-1]=="}":
                inkey=False
            if not letter in "\n" and any(letter in bar for bar in keyboard) and not inkey: #don't fuck with newlines or extra characters or keys
                while True:
                    if random.random()>=self.sloppiness_multiplier or self.sloppiness_multiplier>=1: #keep looping until this is true
                        break
                    y = 0 if letter in keyboard[0] else (1 if letter in keyboard[1] else 2)
                    x = keyboard[y].index(letter)
                    if random.random()<=0.5:
                        x += random.randint(-1,1)
                    else:
                        y += random.randint(-1,1)
                    y = max(0,min(y,2))
                    x = max(0,min(x,len(keyboard[y])-1))
                    letter = keyboard[y][x] if not keyboard[y][x]=="}" else "" #just in case
            elif not inkey and letter=="!" and random.random()<=self.sloppiness_multiplier:
                letter="1"
            elif not inkey and letter==" " and random.random()<=self.sloppiness_multiplier:
                letter = random.choice(["","  "])
            output+=letter
        return output + ("'" if random.random()<=self.sloppiness_multiplier else "") #sometimes add an apostrophe to the end because of a missed return key
                    

    #parses the trailing and leading phrases from a message around a given keyword. Returns response with the {trail} and {lead} tags filled in
    def insert_trailing_and_leading(self, message, keyword, response):

        split_characters = ".!?" #characters that mark the edges of the trailing/leading text

        if "{trail}" in response or "{trail1}" in response:
            trailing = message.split(keyword)[1]
            for i in range(len(split_characters)):
                trailing = trailing.split(split_characters[i])[0]
            if trailing:
               response = response.replace("{trail}",trailing.strip())
               response = response.replace("{trail1}", trailing.strip().split(" ")[0])

        if "{lead}" in response or "{lead1}" in response:
            leading = message.split(keyword)[0]
            for i in range(len(split_characters)):
                leading = leading.split(split_characters[i])[-1]
            if leading:
                response = response.replace("{lead}",leading.strip())
                response = response.replace("{lead1}", leading.strip().split(" ")[-1])

        return response

    #tells you if a key in a given message is "alone" (not a part of any other words)
    def key_is_alone(selfd,message,key):
        break_characters = ".!?,;:\"'"
        for i in range(len(break_characters)): #remove common punctuation
            message = message.replace(break_characters[i]," ")
            key = key.replace(break_characters[i]," ")

        if len(key.split(" "))==1: #simplest case - key is one word long
            return (key in message.split(" "))
        return (message==key or message.startswith(key+" ") or message.endswith(" "+key) or (" "+key+" ") in message) #all the situations I can think of where key is alone

    #converts an input string into a mocking tone by alternating upper and lowercase characters
    def mocking_tone(self,text):
        output = ""
        for i in range(len(text)):
            if i%2==0:
                output+=text[i].lower()
            else:
                output+=text[i].upper()
        return output

    #parses out reaction tags (format: "{react}{XXX}") from a response string. Returns a string list. 
    #The first element is the cleaned response. All consecutive elements are emojis to react with.
    def get_response_reactions(self, text):
        remaining_text = text
        cleaned_response = ""
        output_list = []
        reaction_list = []
        loop_counter = 0
        while ("{react}" in remaining_text):
            split_text = remaining_text.split("{react}",1)
            cleaned_response+=split_text[0]
            split_text = split_text[1].split("}",1)
            reaction = split_text[0].replace("{","")
            reaction = reaction.replace(" ","") #I keep making this mistake, so I'll hardcode in a solution.
            reaction_list.append(reaction)
            remaining_text = split_text[1]
            loop_counter +=1
            if loop_counter>20:
                print("INFINITE_LOOP_WARNING!! in get_response_reactions for :"+text)
                break
        cleaned_response+=remaining_text
        output_list.append(cleaned_response)
        for r in reaction_list:
            output_list.append(r)
        return output_list

    #extracts the urls from an input string. there's probably many edge cases that aren't considered here
    def extract_urls(self, input):
        split = input.split(" ")
        urls = []
        for s in split:
            if s.startswith("https://") or s.startswith("http://") or s.startswith("www."):
                urls.append(s)
        return urls


    def portmanteau(self, first, second):
        """combines two words into one word - written by Katie"""

        leading_vowel = re.compile(r'^[aeiou]', re.IGNORECASE)
        pattern = re.compile(r'(\w*?)([aeiou]\w*)', re.IGNORECASE)
        
        # Error check: The function won't take words that are too short (1 letter).
        if (len(first) < 2 or len(second) < 2):
            #print("Arguments not long enough!")
            return ""
                               
        # Error check: The first word can't start with a vowel.
        if (leading_vowel.match(first)):
            #print("First word can't start with vowel!")
            return ""
                                                  
        # Error check: Both words must actually have vowels in them for this to actually work.
        if not (pattern.match(first) and pattern.match(second)):
            #print("No vowels found!")
            return ""
                                                                            
        port = pattern.match(first).group(1)
        manteau = pattern.match(second).group(2)
                                                                                    
        return (port + manteau)
        #combines two words into one word

    def portmanteau_old(first, second):
        """Older version of portmanteau - Ignores the letter Y. Raises exceptions instead of returning empty string"""
        leading_vowel = re.compile(r'^[aeiou]', re.IGNORECASE)
        pattern = re.compile(r'(\w+?)([aeiou]\w*)', re.IGNORECASE)
        if (len(first) < 2 or len(second) < 2):
            return None
        if (leading_vowel.match(first)):
            return None
        if not (pattern.match(first) and pattern.match(second)):
            return None
        port = pattern.match(first).group(1)
        manteau = pattern.match(second).group(2)
        return (port + manteau)
        
    def portmanthree(self, first, second, third):
        """Combines two of three words using the portmanteau method - Written by Kate"""
        options = []
        words = [first,second,third]
        try:
            port12 = self.portmanteau(first,second)
            if not port12 in words and port12.strip() and not any(((pair.split(";;",1)[0]==words[0].lower() or pair.split(";;",1)[0]=="*") and (pair.split(";;",1)[1]==words[1].lower() or pair.split(";;",1)[1]=="*")) for pair in self.portmanteau_excludes):
                options.append(port12+" "+third)
        except (AttributeError,IndexError):
            print("There was an error in portmanthree on 23 of: "+" ".join(words))
        
        try:
            port23 = self.portmanteau(second,third)
            if not port23 in words and port23.strip() and not any(((pair.split(";;",1)[0]==words[1].lower() or pair.split(";;",1)[0]=="*") and (pair.split(";;",1)[1]==words[2].lower() or pair.split(";;",1)[1]=="*")) for pair in self.portmanteau_excludes):
                options.append(first+" "+port23)
        except (AttributeError,IndexError):
            print("There was an error in portmanthree on 23 of: "+" ".join(words))

        #These are options I decided to leave out
        #options.append(portmanteau(first,portmanteau(second,third)))
        #options.append(portmanteau(portmanteau(first,second),third))

        if len(options)>0:
            return random.choice(options)
        return None


    async def on_message(self, message):    #The big one. Processes all incoming messages.
        await self.wait_until_ready()
        message_content = message.content.strip()
        message_content = message_content.replace(self.config.command_prefix+"am i goku", self.config.command_prefix+"goku") #for annie

        reference_dict = self.replies_to_whisper(message) #check if the message is a reply to a whisper. If so, we will skip all the keyword/portmanteau stuff

        if message_content != None and message.author != self.user and not message.content.startswith(self.config.command_prefix) and not message.author.id in self.blacklist and (isinstance(message.channel, discord.abc.PrivateChannel) or not any(gid==message.guild.id for gid in self.unfun_guilds)) and not reference_dict:
            
            #portmanteau stuff
            words = message_content.split()
            if ((len(words)==2 or len(words)==3) and not message_content.startswith(self.config.command_prefix)):
                if not any(((pair.split(";;",1)[0]==words[0].lower() or pair.split(";;",1)[0]=="*") and (pair.split(";;",1)[1]==words[1].lower() or pair.split(";;",1)[1]=="*")) for pair in self.portmanteau_excludes) or len(words)==3: #Excludes are applied to portmanthrees in the portmanthree method
                    try:
                        if len(words)==2: #portmanteau
                            combo_word = self.portmanteau(words[0], words[1])
                            if not (combo_word == words[0] or combo_word == words[1]) and combo_word.strip():
                                own_message = await message.channel.send(combo_word, delete_after=15)
                                await own_message.add_reaction("\U0001F4BE")

                        if len(words)==3 and random.random()<0.5: #portmanthree  - only 50% chance so it's less spammy
                            combo_word = self.portmanthree(words[0], words[1], words[2])
                            if combo_word:
                                own_message = await message.channel.send(combo_word, delete_after=10)
                                await own_message.add_reaction("\U0001F4BE")
                    except AttributeError:
                        print("Portmanteau threw an AttributeError for "+message_content)
                        with open(os.path.abspath("portmanteau_fails.txt"),"a") as f:
                            f.write("Attribute Error: "+message_content+"\n")
                    except IndexError:
                        print("Portmanteau threw an IndexError for "+message_content)
                        with open(os.path.abspath("portmanteau_fails.txt"),"a") as f:
                            f.write("Index Error: "+message_content+"\n")

            #keymash recognition stuff
            if len(message_content)>=11 and not message_content.startswith(self.config.command_prefix) and not message.author==self.user and not "goku" in message_content.lower():
                keymash_confidence = self.keymash_test(message_content)
                if keymash_confidence > 0.85:
                        await message.channel.send(self.scramble(message_content.replace(" ","")),delete_after=20)

            #image reading stuff
            from_image = False #used for certain response tags
            if not isinstance(message.channel, discord.abc.PrivateChannel):
                image_types = ["png","jpeg","gif","jpg"]
                cached_images = []
                for attachment in message.attachments:
                    if any(attachment.filename.lower().endswith(image) for image in image_types):
                        try:
                            await attachment.save(os.path.abspath("image_cache/"+attachment.filename))
                            cached_images.append(os.path.abspath("image_cache/"+attachment.filename))
                        except:
                            pass

                if len(cached_images)>0:
                    hash_code = str(message.id) #prevents race conditions and stuff
                    with open(os.path.abspath("image_cache/image_list"+hash_code+".txt"),"w") as f:
                        for path in cached_images:
                            f.write(path)
                            f.write("\n")
                    await self.run_cli("tesseract "+os.path.abspath("image_cache/image_list"+hash_code+".txt")+" "+os.path.abspath("image_cache/out"+hash_code)) #run tesseract
                    with open(os.path.abspath("image_cache/out"+hash_code+".txt"),"r") as f:
                        message_content += f.read().strip()
                        from_image = True
                    for path in cached_images:
                        await self.run_cli("rm "+path)
                    await self.run_cli("rm "+os.path.abspath("image_cache/image_list"+hash_code+".txt"))

            #start_time = time.time()

            #keyword recognition stuff
            with open(os.path.abspath("responses.txt"),'r') as f: #TODO - make this server-specific and configurable
                lines = f.readlines()

            with open(os.path.abspath("emotes/default.txt"),'r') as f: #TODO - make this server-specific and configurable
                lines.extend(f.readlines())
            try:
                with open(os.path.abspath("emotes/{}.txt".format(message.channel.guild.id)),'r') as f:
                    lines.extend(f.readlines())
            except:
                pass

            #read_time = round(time.time() - start_time,4)
            #start_time = time.time()

            #tags to remove from the message, keys, and responses when cleaning
            cleaning_tags_message = [",","’","'","\""]
            cleaning_tags_key = ["{noimg}","{deleteorig}","{jeff}","{nodel}","{only}","{a}","{alone}","{gokuattempt}","{fast}","{vfast}","{rare}","{vrare}"]
            cleaning_tags_response = ["{nodel}","{deleteorig}","{goku}","{fast}","{vfast}","{rare}","{vrare}"]

            contains_only = False

            cleaned_message = message_content.lower().strip()
            message_words = cleaned_message.split(" ")
            for tag in cleaning_tags_message:
                cleaned_message = cleaned_message.replace(tag,"")

            response_cache = []          #contains all responses to be sent
            delete_flags_cache = []      #contains the time until deletion of the corresponding response (-1 indicated no deletion)
            #^^(these should be replaced by a single dict list)

            for line in lines:

                if not line.strip() or line.startswith("##"):
                    continue
                keys = re.split(r"(?<!\\),",re.split(r"(?<!\\);",line)[0])
                #keys = line.split(';',1)[0].split(',')
                for key in keys:

                    key = key.replace("{emote}","{a}{noimg}{nodel}")
                    cleaned_key = key.lower().strip()
                    for tag in cleaning_tags_key:
                        cleaned_key = cleaned_key.replace(tag,"")

                    if cleaned_key in cleaned_message:
                        if "{alone}" in key and not self.key_is_alone(cleaned_message,cleaned_key): #cleaned_key in cleaned_message.split(" "):
                            continue

                        if not (from_image and "{noimg}" in key) and (not "{jeff}" in key or message.author.id == 0000000):

                            #witty_response = random.choice(line.split(';')[1].split(','))
                            witty_response = random.choice(re.split(r"(?<!\\),",re.split(r"(?<!\\);",line)[1])).replace("\\","")

                            #madlib tags
                            now = datetime.now()
                            witty_response = witty_response.replace("{author}",message.author.name)
                            witty_response = witty_response.replace("{atauthor}",message.author.mention)
                            witty_response = witty_response.replace("{time}",now.strftime("%-I:%M %p"))
                            witty_response = witty_response.replace("{date}",now.strftime("%B %-d, %Y"))
                            witty_response = witty_response.replace("{weekday}",now.strftime("%A"))

                            # random tag
                            witty_response = witty_response.replace("{random}", random.choice(message_words))

                            #boolean-like tags
                            deleteorig = ("{deleteorig}" in key or "{deleteorig}" in witty_response)
                            nodel = ("{nodel}" in key or "{nodel}" in witty_response)
                            fast_delete = ("{fast}" in key or "{fast}" in witty_response)
                            vfast_delete = ("{vfast}" in key or "{vfast}" in witty_response)
                            if ("{rare}" in key or "{rare}" in witty_response) and random.random()>=0.5:
                                continue
                            if ("{vrare}" in key or "{vrare}" in witty_response) and random.random()>=0.1:
                                continue

                            if ("{only}" in key or "{only}" in witty_response):
                                contains_only = True
                                witty_response = witty_response + "{only}"
                            if "{a}" in key:
                                witty_response = witty_response + "{a}"

                            if "{gokuattempt}" in key:
                                daily_attempts = self.changeScoreboard(message.author.id, "goku_attempts") #TODO - figure out how to resolve this potential race condition
                                if daily_attempts>self.max_daily_gokus:
                                    await message.channel.send("YOU HAVE USED UP YOUR DAILY GOKU ATTEMPTS!",delete_after = 60)
                                    continue

                            if "{goku}" in witty_response:
                                self.changeScoreboard(message.author.id, "goku")


                            for tag in cleaning_tags_response:
                                witty_response = witty_response.replace(tag,"")

                            try:
                                witty_response = self.insert_trailing_and_leading(cleaned_message, cleaned_key, witty_response)
                                if "{trail" in witty_response or "{lead" in witty_response: #ignore response when there is no trail or lead
                                    continue
                            except:
                                #raise
                                print("There was an issue with leading/trailing parsing")
                                continue

                            if deleteorig and not message_content.startswith(self.config.command_prefix):
                                try:
                                    await message.delete() #might cause issues later on?
                                except Forbidden:
                                    print("Tried to delete a message but don't have permission: "+message_content)
                                    continue

                            reaction_emoji_data = self.get_response_reactions(witty_response)
                            witty_response = reaction_emoji_data.pop(0) #grab cleaned response - i do it this way just to be confusing
                            for emoji in reaction_emoji_data:
                                await message.add_reaction(emoji)

                            #if not witty_response.strip().replace("{break}",""): #there's nothing left after all the tags are gone
                                #continue

                            response_cache.append(witty_response)
                            if nodel:
                                delete_flags_cache.append(-1)
                            elif vfast_delete:
                                delete_flags_cache.append(1)
                            elif fast_delete:
                                delete_flags_cache.append(5)
                            else:
                                delete_flags_cache.append(15)

                            break
            #parse_time = time.time()-start_time
            #start_time = time.time()

            #Postprocess response cache
            response_count = len(response_cache)
            for i in range(len(response_cache)-1,-1,-1):
                if contains_only and not "{only}" in response_cache[i] and not "{a}" in response_cache[i]: #there's an {only} tag somewhere, so remove all nonessential responses
                    response_cache.pop(i)
                    delete_flags_cache.pop(i)
                elif not "{a}" in response_cache[i] and not "{only}" in response_cache[i] and (response_count>self.max_responses or random.random()>self.response_frequency): #prune out some nonessential responses
                    response_cache.pop(i)
                    delete_flags_cache.pop(i)
                elif not response_cache[i].replace("{break}","").replace("{a}","").replace("{only}","").replace("💩","").strip(): #remove blank messages (such as reaction-only responses). Also patch for pooper exploit.
                    response_cache.pop(i)
                    delete_flags_cache.pop(i)
                    continue
                elif not "{a}" in response_cache[i] or "{only}" in response_cache[i]: #message is allowed to be typoed
                    response_cache[i]=self.add_typos(response_cache[i])

            #Send cached responses
            response_count = len(response_cache) #update count in case some were removed
            for i in range(len(response_cache)):
                response_block = response_cache[i].replace("{a}","").replace("{only}","")
                delete_time = delete_flags_cache[i]
                for response in response_block.split("{break}"):
                    if delete_time < 0:
                        await message.channel.send(response)
                    else:
                        await message.channel.send(response, delete_after=delete_time)

            #send_time = round(time.time()-start_time,4)
            #print(str(read_time)+"  "+str(parse_time)+"  "+str(send_time)+" : "+str(read_time+parse_time+send_time))

            #Sometimes mock people
            if response_count==0 and len(message_content)>8 and not "http" in message_content and random.random()<=0.003:
                await message.channel.send(self.mocking_tone(message_content),delete_after=5)

            #Sometimes react to things
            if response_count==0 and not "http" in message_content and random.random()<=0.003:
                random_react = random.choice("👍💀❤️😳☺️😂🤣🙃😎👆😹😏🙌💯")
                try:
                    await message.add_reaction(random_react)
                except:
                    print("Bad emoji?: "+random_react) #Sometimes this happens. idk why though. One of these is probably bad or something.

            #Sometimes say "your mom" to questions - probability increases when the question ends in "doing?" or "do?" - this doesn't happen often because kids these days don't use punctuation
            if response_count==0 and ((message_content.endswith("doing?") and random.random()<0.1) or (message_content.endswith("do?") and random.random()<0.05) or (message_content.endswith("?") and random.random()<0.001)):
                await message.channel.send(random.choice(["your","you're","ur","u're","your","you","ya","your","yo"])+" "+random.choice(["mom","mother","mama","mome","mom","mom"]), delete_after=10)


        #fix embed fails (media to cdn)
        bad_embed_formats = [".mov",".webm"] #mp4s work for some reason now
        embed_fixed = False
        urls = self.extract_urls(message_content)
        urls = list(set(urls)) #remove duplicates
        for url in urls:
            if url.startswith("https://media.discordapp.net") and any(url.endswith(form) for form in bad_embed_formats) and not isinstance(message.channel, discord.abc.PrivateChannel):
                mymessage = await message.channel.send("I fixed your embed for you: "+url.replace("media.discordapp.net","cdn.discordapp.com"))
                await mymessage.add_reaction("❌")
                embed_fixed = True
        if embed_fixed:
            return

        if not (message_content.startswith(self.config.command_prefix) or isinstance(message.channel, discord.abc.PrivateChannel)):
            return

        if message.author == self.user:
            #log.warning("Ignoring command from myself (or this is just me sliding into your DMs) ({})".format(message.content))
            return

        #actually, allowing other bots to control the bot would be kinda funny (and is necessary for webhook control)
        #if message.author.bot and not message.author.id not in self.config.bot_exception_ids:
        #    log.warning("Ignoring command from other bot ({})".format(message.content))
        #    return

        if (not isinstance(message.channel, discord.abc.GuildChannel)) and (not isinstance(message.channel, discord.abc.PrivateChannel)):
            return

        command, *args = message_content.split(' ')  # Uh, doesn't this break prefixes with spaces in them (it doesn't, config parser already breaks them)
        command = command[len(self.config.command_prefix):].lower().strip()

        # [] produce [''] which is not what we want (it break things)
        if args:
            args = ' '.join(args).lstrip(' ').split(' ')
        else:
            args = []

        handler = getattr(self, 'cmd_' + command, None)
        if not handler: #(handler or isinstance(message.channel, discord.abc.PrivateChannel)):
            # alias handler
            if self.config.usealias:
                command = self.aliases.get(command)
                handler = getattr(self, 'cmd_' + command, None)
                if not handler and not isinstance(message.channel, discord.abc.PrivateChannel): #don't return for private channels - need to accept bnuuy submissions
                    return
            else:
                if not isinstance(message.channel, discord.abc.PrivateChannel): #not sure what this does. Hopefully it will never run
                    return

        if isinstance(message.channel, discord.abc.PrivateChannel): #In the DMS
            
            if not (message.author.id == self.config.owner_id and command == 'joinserver') and not any(command == dmcommand for dmcommand in self.dm_commands):

                #If message is response to a whisper, send a reply whisper
                if reference_dict: #this was from way earlier, rember?
                    sender = message.author
                    receiver = self.get_user(reference_dict["sender"])
                    if not receiver:
                        receiver = await self.fetch_user(reference_dict["sender"]) #Should never run. If it does, the next line will certainly raise an exception.
                    try:
                        await self.send_whisper(sender, receiver, message.content, replyToMessage = await message.channel.fetch_message(message.reference.message_id))
                        print("[Whisper]Sent Reply Whisper: Sender: "+str(message.author.id)+" Message: "+message.content)
                        await message.add_reaction("✉️")
                        await message.add_reaction("✅")
                    except:
                        await message.channel.send("Sorry, something went wrong while sending that reply. The text may have been too long.")
                        raise

                    return

                #if message contains attached videos/images, add them to the betabnuuy list
                for attachment in message.attachments:
                    new_url = attachment.url.replace("media.discordapp.net","cdn.discordapp.com")
                    with open(os.path.abspath("bunny_submissions.txt"),"r") as f:
                        bnuuy_count = len(f.readlines())
                    with open(os.path.abspath("bunny_submissions.txt"),"a") as f: #don't question my methods!
                        f.write("\n")
                        f.write(new_url+","+message.author.name)
                    embed = discord.Embed(title="Your submission has been added to the list!",color=discord.Color.from_rgb(255, 191, 0))
                    embed.set_image(url=new_url)
                    embed.set_footer(text="Your submission's ID number is "+str(bnuuy_count+1))
                    await message.channel.send(embed=embed)
                return
        



        if self.config.bound_channels and message.channel.id not in self.config.bound_channels and not isinstance(message.channel, discord.abc.PrivateChannel) and not any(command==anywherecommand for anywherecommand in self.anywhere_commands):
            if self.config.unbound_servers:
                for channel in message.guild.channels:
                    if channel.id in self.config.bound_channels:
                        return
            else:
                return  # if I want to log this I just move it under the prefix check

        if message.author.id in self.blacklist and message.author.id != self.config.owner_id:
            log.warning("User blacklisted: {0.id}/{0!s} ({1})".format(message.author, command))
            return

        else:
            log.info("{0.id}/{0!s}: {1}".format(message.author, message_content.replace('\n', '\n... ')))

        user_permissions = self.permissions.for_user(message.author)

        argspec = inspect.signature(handler)
        params = argspec.parameters.copy()

        sentmsg = response = None



        # noinspection PyBroadException
        try:
            if command=="play" and message.guild.id not in self.players: #auto-summon feature (it's in here to allow error-catching)
                print("AUTO SUMMONING")
                await self.cmd_summon(message.channel, message.guild, message.author,message.guild.me.voice.channel if message.guild.me.voice else None) #kinda hacky, but it works



            if user_permissions.ignore_non_voice and command in user_permissions.ignore_non_voice:
                await self._check_ignore_non_voice(message)

            handler_kwargs = {}
            if params.pop('message', None):
                handler_kwargs['message'] = message

            if params.pop('channel', None):
                handler_kwargs['channel'] = message.channel

            if params.pop('author', None):
                handler_kwargs['author'] = message.author

            if params.pop('guild', None):
                handler_kwargs['guild'] = message.guild

            if params.pop('player', None):
                handler_kwargs['player'] = await self.get_player(message.channel)

            if params.pop('_player', None):
                handler_kwargs['_player'] = self.get_player_in(message.guild)

            if params.pop('permissions', None):
                handler_kwargs['permissions'] = user_permissions

            if params.pop('user_mentions', None):
                handler_kwargs['user_mentions'] = list(map(message.guild.get_member, message.raw_mentions))

            if params.pop('channel_mentions', None):
                handler_kwargs['channel_mentions'] = list(map(message.guild.get_channel, message.raw_channel_mentions))

            if params.pop('voice_channel', None):
                handler_kwargs['voice_channel'] = message.guild.me.voice.channel if message.guild.me.voice else None

            if params.pop('leftover_args', None):
                handler_kwargs['leftover_args'] = args

            args_expected = []
            for key, param in list(params.items()):

                # parse (*args) as a list of args
                if param.kind == param.VAR_POSITIONAL:
                    handler_kwargs[key] = args
                    params.pop(key)
                    continue

                # parse (*, args) as args rejoined as a string
                # multiple of these arguments will have the same value
                if param.kind == param.KEYWORD_ONLY and param.default == param.empty:
                    handler_kwargs[key] = ' '.join(args)
                    params.pop(key)
                    continue

                doc_key = '[{}={}]'.format(key, param.default) if param.default is not param.empty else key
                args_expected.append(doc_key)

                # Ignore keyword args with default values when the command had no arguments
                if not args and param.default is not param.empty:
                    params.pop(key)
                    continue

                # Assign given values to positional arguments
                if args:
                    arg_value = args.pop(0)
                    handler_kwargs[key] = arg_value
                    params.pop(key)

            if message.author.id != self.config.owner_id:
                if user_permissions.command_whitelist and command not in user_permissions.command_whitelist:
                    raise exceptions.PermissionsError(
                        "This command is not enabled for your group ({}).".format(user_permissions.name),
                        expire_in=20)

                elif user_permissions.command_blacklist and command in user_permissions.command_blacklist:
                    raise exceptions.PermissionsError(
                        "This command is disabled for your group ({}).".format(user_permissions.name),
                        expire_in=20)

            # Invalid usage, return docstring
            if params:
                docs = getattr(handler, '__doc__', None)
                if not docs:
                    docs = 'Usage: {}{} {}'.format(
                        self.config.command_prefix,
                        command,
                        ' '.join(args_expected)
                    )

                docs = dedent(docs)
                await self.safe_send_message(
                    message.channel,
                    '```\n{}\n```'.format(docs.format(command_prefix=self.config.command_prefix)),
                    expire_in=60
                )
                return

            response = await handler(**handler_kwargs)
            if response and isinstance(response, Response):
                if not isinstance(response.content, discord.Embed) and self.config.embeds:
                    content = self._gen_embed()
                    content.title = command
                    content.description = response.content
                else:
                    content = response.content

                if response.reply:
                    if isinstance(content, discord.Embed):
                        content.description = '{} {}'.format(message.author.mention, content.description if content.description is not None else '') # 2023-04-21: discord.Embed.Empty removed for None
                    else:
                        content = '{}: {}'.format(message.author.mention, content)

                sentmsg = await self.safe_send_message(
                    message.channel, content,
                    expire_in=response.delete_after if self.config.delete_messages else 0,
                    also_delete=message if self.config.delete_invoking else None
                )

        except (exceptions.CommandError, exceptions.HelpfulError, exceptions.ExtractionError) as e:
            log.error("Error in {0}: {1.__class__.__name__}: {1.message}".format(command, e), exc_info=True)

            expirein = e.expire_in if self.config.delete_messages else None
            alsodelete = message if self.config.delete_invoking else None

            if self.config.embeds:
                content = self._gen_embed()
                content.add_field(name='Error', value=e.message, inline=False)
                content.colour = 13369344
            else:
                content = '```\n{}\n```'.format(e.message)

            await self.safe_send_message(
                message.channel,
                content,
                expire_in=expirein,
                also_delete=alsodelete
            )

        except exceptions.Signal:
            raise

        except Exception:
            log.error("Exception in on_message", exc_info=True)
            if self.config.debug_mode:
                await self.safe_send_message(message.channel, '```\n{}\n```'.format(traceback.format_exc()))

        finally:
            if not sentmsg and not response and self.config.delete_invoking:
                await asyncio.sleep(5)
                await self.safe_delete_message(message, quiet=True)

    async def gen_cmd_list(self, message, list_all_cmds=False):
        for att in dir(self):
            # This will always return at least cmd_help, since they needed perms to run this command
            if att.startswith('cmd_') and (not hasattr(getattr(self, att), 'dev_cmd') or str(message.author.id) in self.config.dev_ids) and (not hasattr(getattr(self, att), 'owner_cmd') or message.author.id==self.config.owner_id):
                user_permissions = self.permissions.for_user(message.author)
                command_name = att.replace('cmd_', '').lower()
                whitelist = user_permissions.command_whitelist
                blacklist = user_permissions.command_blacklist

                #Add a dagger mark to commands only accessible to devs/owner
                

                #list all commands (that the user has permissions for)
                if list_all_cmds:
                    #if a command is not usually useable in the current channel, add an asterisk to the name
                    if (isinstance(message.channel,discord.abc.PrivateChannel) and not command_name in self.dm_commands) or (not isinstance(message.channel,discord.abc.PrivateChannel) and command_name not in self.anywhere_commands):
                        if self.config.bound_channels and message.channel.id not in self.config.bound_channels:
                            command_name = command_name + "*"
                    if hasattr(getattr(self, att), 'dev_cmd') or hasattr(getattr(self, att), 'owner_cmd'):
                        command_name = command_name + "†"
                    self.commands.append('{}{}'.format(self.config.command_prefix, command_name)) #why does it use an instance variable for this??

                #im not what blacklist and whitelist are, but I'm leaving them in (-Kate)
                elif blacklist and command_name in blacklist:
                    pass
                elif whitelist and command_name not in whitelist:
                    pass

                #only include command if it's useable in the current channel
                elif (isinstance(message.channel,discord.abc.PrivateChannel) and command_name in self.dm_commands) or (not isinstance(message.channel,discord.abc.PrivateChannel) and command_name in self.anywhere_commands) or (self.config.bound_channels and message.channel.id in self.config.bound_channels):
                    if hasattr(getattr(self, att), 'dev_cmd') or hasattr(getattr(self, att), 'owner_cmd'):
                        command_name = command_name + "†"
                    self.commands.append("{}{}".format(self.config.command_prefix, command_name))

    async def on_voice_state_update(self, member, before, after):
        if not self.init_ok:
            return  # Ignore stuff before ready

        if before.channel:
            channel = before.channel
        elif after.channel:
            channel = after.channel
        else:
            return

        if not self.config.auto_pause:
            return

        autopause_msg = "{state} in {channel.guild.name}/{channel.name} {reason}"

        auto_paused = self.server_specific_data[channel.guild]['auto_paused']

        try:
            player = await self.get_player(channel)
        except exceptions.CommandError:
            return

        def is_active(member):
            if not member.voice:
                return False
                
            if any([member.voice.deaf, member.voice.self_deaf, member.bot]):
                return False

            return True

        if not member == self.user and is_active(member):  # if the user is not inactive
            if player.voice_client.channel != before.channel and player.voice_client.channel == after.channel:  # if the person joined
                if auto_paused and player.is_paused:
                    log.info(autopause_msg.format(
                        state = "Unpausing",
                        channel = player.voice_client.channel,
                        reason = ""
                    ).strip())

                    self.server_specific_data[player.voice_client.guild]['auto_paused'] = False
                    player.resume()
            elif player.voice_client.channel == before.channel and player.voice_client.channel != after.channel:
                if not any(is_active(m) for m in player.voice_client.channel.members):  # channel is empty
                    if not auto_paused and player.is_playing:
                        log.info(autopause_msg.format(
                            state = "Pausing",
                            channel = player.voice_client.channel,
                            reason = "(empty channel)"
                        ).strip())

                        self.server_specific_data[player.voice_client.guild]['auto_paused'] = True
                        player.pause()
            elif player.voice_client.channel == before.channel and player.voice_client.channel == after.channel:  # if the person undeafen
                if auto_paused and player.is_paused:
                    log.info(autopause_msg.format(
                        state = "Unpausing",
                        channel = player.voice_client.channel,
                        reason = "(member undeafen)"
                    ).strip())

                    self.server_specific_data[player.voice_client.guild]['auto_paused'] = False
                    player.resume()
        else:
            if any(is_active(m) for m in player.voice_client.channel.members):  # channel is not empty
                if auto_paused and player.is_paused:
                    log.info(autopause_msg.format(
                        state = "Unpausing",
                        channel = player.voice_client.channel,
                        reason = ""
                    ).strip())
 
                    self.server_specific_data[player.voice_client.guild]['auto_paused'] = False
                    player.resume()

            else:
                if not auto_paused and player.is_playing:
                    log.info(autopause_msg.format(
                        state = "Pausing",
                        channel = player.voice_client.channel,
                        reason = "(empty channel or member deafened)"
                    ).strip())

                    self.server_specific_data[player.voice_client.guild]['auto_paused'] = True
                    player.pause()

    async def on_guild_update(self, before:discord.Guild, after:discord.Guild):
        if before.region != after.region:
            log.warning("Guild \"%s\" changed regions: %s -> %s" % (after.name, before.region, after.region))

    async def on_guild_join(self, guild:discord.Guild):
        log.info("Bot has been added to guild: {}".format(guild.name))
        owner = self._get_owner(voice=True) or self._get_owner()
        if self.config.leavenonowners:
            check = guild.get_member(owner.id)
            if check == None:
                await guild.leave()
                log.info('Left {} due to bot owner not found.'.format(guild.name))
                await owner.send(self.str.get('left-no-owner-guilds', 'Left `{}` due to bot owner not being found in it.'.format(guild.name)))

        log.debug("Creating data folder for guild %s", guild.id)
        pathlib.Path('data/%s/' % guild.id).mkdir(exist_ok=True)

    async def on_guild_remove(self, guild:discord.Guild):
        log.info("Bot has been removed from guild: {}".format(guild.name))
        log.debug('Updated guild list:')
        [log.debug(' - ' + s.name) for s in self.guilds]

        if guild.id in self.players:
            self.players.pop(guild.id).kill()


    async def on_guild_available(self, guild:discord.Guild):
        if not self.init_ok:
            return # Ignore pre-ready events

        log.debug("Guild \"{}\" has become available.".format(guild.name))

        player = self.get_player_in(guild)

        if player and player.is_paused:
            av_paused = self.server_specific_data[guild]['availability_paused']

            if av_paused:
                log.debug("Resuming player in \"{}\" due to availability.".format(guild.name))
                self.server_specific_data[guild]['availability_paused'] = False
                player.resume()


    async def on_guild_unavailable(self, guild:discord.Guild):
        log.debug("Guild \"{}\" has become unavailable.".format(guild.name))

        player = self.get_player_in(guild)

        if player and player.is_playing:
            log.debug("Pausing player in \"{}\" due to unavailability.".format(guild.name))
            self.server_specific_data[guild]['availability_paused'] = True
            player.pause()

    def voice_client_in(self, guild):
        for vc in self.voice_clients:
            if vc.guild == guild:
                return vc
        return None

    async def cmd_ping(self, channel):
        """
        Usage:
            {command_prefix}ping

        Checks if BnuuyBot is listening to you. Also gives latency.
        """
        embed = discord.Embed(title="Pong!",color=discord.Color.from_rgb(3, 252, 53))
        embed.set_footer(text="Latency: "+str(round(self.latency*100,2))+" ms")
        await channel.send(embed=embed)
        return

    async def cmd_bnuuy(self, channel, message):
        """
        Usage:
            {command_prefix}bnuuy
            {command_prefix}bnuuy [ID number]

        Aliases:
            {command_prefix}bny

        Provides a random image of a bnuuy from a curated list.
        """
        try:
            number = int(message.content.split(" ")[1])
        except:
            number = 0
        with open(os.path.abspath("bunny_links.txt"),"r") as f:
            random_url=""
            lines = f.readlines()

            if number-1 >= len(lines):
                await channel.send("That ID number was too large")
                return
            elif number<0:
                await channel.send("That ID number was too low")
                return
            elif number==0:
                while not random_url.strip():
                    line_number = random.randint(0,len(lines)-1)
                    random_url=lines[line_number]
            else:
                line_number = number-1
                random_url = lines[number-1]
        embed = discord.Embed(title="Bnuuy",color=discord.Color.from_rgb(181, 255, 181))
        embed.set_image(url=random_url)
        embed.set_footer(text="(Bunny number: "+str(line_number+1)+")")
        await channel.send(embed=embed,delete_after=3600)
        #await channel.send(random_url, delete_after=120)
        return

    async def cmd_betabnuuy(self, channel,message):
        """
        Usage:
            {command_prefix}betabnuuy
            {command_prefix}betabnuuy [ID number]

        Aliases:
            {command_prefix}bbny

        Provides a random user-provided image that may or may not be a bnuuy. You may submit images to the list by DMing them to BnuuyBot. These images are not necessarily curated. Use at own risk.
        """
        try:
            number = int(message.content.split(" ")[1])
        except:
            number = 0

        video_formats = [".mp4",".mov"]
        with open(os.path.abspath("bunny_submissions.txt"),"r") as f:
            random_line = ""
            lines = f.readlines()
            if number-1 >= len(lines):
                await channel.send("That ID number was too large")
                return
            elif number<0:
                await channel.send("That ID number was too low")
                return
            elif number==0:
                while not random_line.strip() or random_line.startswith("##"): #TODO - fix infinite loop when there are no betabnuuys available
                    line_number = random.randint(0,len(lines)-1)
                    random_line=lines[line_number]
            else:
                line_number = number-1
                random_line = lines[number-1]
        random_info = random_line.split(",",1)
        random_url, poster = random_info[0], random_info[1]
        if any(random_url.endswith(format) for format in video_formats): #Videos don't work in Embeds
            await channel.send(random_url,delete_after=120)
            await channel.send("This image was provided by: "+poster+"\nYou can send your own pictures to me through DMs!   (Bunny Number: "+str(line_number+1)+")", delete_after=3600)
        else:
            embed = discord.Embed(title="Bnuuy provided by: "+poster,color=discord.Color.from_rgb(255, 199, 234))
            embed.set_image(url=random_url)
            embed.set_footer(text="You can add more Bnuuys by DMing me your images!        (Bunny number: "+str(line_number+1)+")")
            await channel.send(embed=embed,delete_after=3600)
        return

    @dev_only
    async def cmd_removebnuuy(self,message,channel):
        """
        Usage:
            {command_prefix}removebnuuy #

        Removes a specified bnuuy from the betabnuuy list, given its number.
        Note: Doesn't actually delete the line - just comments it out, so the id number of the other bnuuys are not altered
        """
        try:
            number = int(message.content.split(" ",1)[1])
            
        except TypeError:
            await channel.send("That was not a valid betabnuuy number!",delete_after=30)
            return
        except IndexError:
            await channel.send("Please specify the number of the bnuuy to remove",delete_after=30)
            return

        with open(os.path.abspath("bunny_submissions.txt"),"r") as f:
            lines = f.readlines()

        if number<0 or number>=len(lines):
            await channel.send("That number is not valid (too high or too low)",delete_after=30)
            return
        if not lines[number].strip() or lines[number].startswith("##"):
            await channel.send("That bnuuy is either already deleted, or it does not exist",delete_after=30)
            return

        lines[number] = "##"+lines[number]

        with open(os.path.abspath("bunny_submissions.txt"),"w") as f:
            f.writelines(lines)

        await message.add_reaction("✅")
        return
        

    async def cmd_hug(self, channel):
        """
        Usage:
            {command_prefix}hug

        Sends a virtual hug!
        """
        await channel.send("_Hug_")
        return

    async def cmd_ratbastard(self, channel):
        """
        Usage:
            {command_prefix}ratbastard

        Please, for the love of god, don't use this command.
        """

        with open(os.path.abspath("misc_data/ratatouille.txt"),"r") as f:
            lines = f.readlines()
            lifetime=70
            for line in lines:
                await channel.send(line, delete_after=lifetime)
                lifetime+=70
        return

    async def cmd_listlens(self, channel):
        """
        Usage:
            {command_prefix}listlens

        Lists the current sizes of the various lists.
        """
        key_count = 0
        answer_count = 0
        with open(os.path.abspath("bunny_links.txt"),"r") as f:
            bunnies = len(f.readlines())
        with open(os.path.abspath("bunny_submissions.txt"),"r") as f:
            submissions = len(f.readlines())
        with open(os.path.abspath("responses.txt"),"r") as f: #TODO - count individual keys and responses
            lines = f.readlines()
            response_pairs = 0
            key_count = 0
            response_count = 0
            for line in lines:
                if line.startswith("##") or not line.strip():
                    continue
                response_pairs+=1
                key_count += len(line.split(";")[0].split(","))
                response_count += len(line.split(";")[1].split(","))
        with open(os.path.abspath("misc_data/dadjokes.txt"),"r") as f:
            dadjokes = len(f.readlines())
        with open(os.path.abspath("misc_data/quotes.txt"),"r") as f:
            quotes = len(f.readlines())
        with open(os.path.abspath("misc_data/reminders.txt"),"r") as f:
            reminders = len(f.readlines())
        with open(os.path.abspath("misc_data/whispers.txt"),"r") as f:
            whispers = len(f.readlines())
        await channel.send("Regular Bnuuys: "+str(bunnies)+"\nBetaBnuuy Submissions: "+str(submissions)+"\nResponseGroups: "+str(response_pairs)+"   (Keys: "+str(key_count)+" , Responses: "+str(response_count)+")\nQuotes: "+str(quotes)+"\nReminders: "+str(reminders)+"\nWhispers: "+str(whispers)+"\nDadJokes: "+str(dadjokes), delete_after=120)
        return


    async def cmd_bnuuyboard(self,channel):
        """
        Usage:
            {command_prefix}bnuuyboard

        Aliases:
            {command_prefix}bboard


        Gives a list of all the top bnuuy contributors!
        """
        names = []
        counts = []
        with open(os.path.abspath("bunny_submissions.txt"),"r") as f:
            lines = f.readlines()
        for line in lines:
            if line.startswith("##") or not line.strip():
                continue
            sender = line.split(",")[1].replace("\n","").strip()

            match_found = False
            for i in range(len(names)):
                if names[i]==sender:
                    match_found = True
                    counts[i] = counts[i]+1
                    break
            if not match_found:
                names.append(sender)
                counts.append(1)

        for i in range(len(names)):
            names[i] = names[i].replace("\n","")+"     #$#$#"+str(counts[i]) #using a bad delimiter because im too tired to think of a better way

        ordered_names = sorted(names, key=lambda name: int(name.split("#$#$#",1)[1])) #It is what it is

        DISPLAY_COUNT = 7
        output_string = "TOP BNUUY CONTRIBUTORS!\n"
        number = 1
        for i in range(len(ordered_names)-1,max(-1,len(ordered_names)-DISPLAY_COUNT-1),-1):
            output_string += "\n"+str(number)+".  "+ordered_names[i].replace("#$#$#","") #clean up this stupid mess i've made
            number +=1

        embed = discord.Embed(color=discord.Color.from_rgb(196, 249, 255))
        embed.add_field(name = output_string, value = "_ _")
        embed.set_footer(text="You can contribute more bnuuys by DMing me your own fun images!")
        await channel.send(embed=embed, delete_after = 30)


    async def cmd_minesweeper(self, message, channel): #TODO - make more compact
        """
        Usage:
            {command_prefix}minesweeper [size]

        Aliases:
            {command_prefix}msw

        Generates a square minesweeper board of a specified size. Size cannot exceed 9 because discord character limit is kinda weird, sorry.
        """
        emojis = [":zero:",":one:",":two:",":three:",":four:",":five:",":six:",":seven:",":eight:",":nine:"]
        bomb = ":bomb:"
        bomblines = []
        try:
            linecount = int(message.content.split(" ")[1])
        except:
            await channel.send("Sorry, I couldn't parse that number :/",delete_after=20)
            return
        if linecount >30:
            await channel.send("That number is too high. The limit is 30.",delete_after=15)
            return
        if linecount <1:
            await channel.send("Haha, very funny. But you won't be laughing when I deduct a goku point from you.",delete_after=15)
            return
        totalbombs = linecount**2 * 0.2
        bombs = 0
        bombprob = (totalbombs-bombs)/(linecount**2)


        #bomb-laying pass
        for i in range(0,linecount):
            line = ""
            for j in range(0,linecount):
                bombprob = (totalbombs-bombs)/(linecount**2-i*linecount-j)
                if random.random()<bombprob:
                    line = line+"@"
                    bombs+=1
                else:
                    line = line+"#"
            bomblines.append(line)

        #counting pass
        lines = []
        for i in range(0,linecount):
            line = ""
            for j in range(0,linecount):
                if bomblines[i][j] == "#":
                    count = 0
                    if i>0 and j>0 and bomblines[i-1][j-1]=="@":
                        count+=1
                    if i>0 and bomblines[i-1][j]=="@":
                        count+=1
                    if i>0 and j<linecount-1 and bomblines[i-1][j+1]=="@":
                        count+=1
                    if j>0 and bomblines[i][j-1]=="@":
                        count+=1
                    if j<linecount-1 and bomblines[i][j+1]=="@":
                        count+=1
                    if i<linecount-1 and j>0 and bomblines[i+1][j-1]=="@":
                        count+=1
                    if i<linecount-1 and bomblines[i+1][j]=="@":
                        count+=1
                    if i<linecount-1 and j<linecount-1 and bomblines[i+1][j+1]=="@":
                        count+=1
                    line = line+"||"+emojis[count]+"||"
                else:
                    line = line+"||"+bomb+"||"
            lines.append(line)

        #combine lines into one message and send
        output_text = ""
        for line in lines:
            output_text += line+"\n"
        
        output_splits = self.split_long_text(output_text,MAX_LEN = 600)
        for split in output_splits:
            await channel.send(split,delete_after=linecount**2*15)
            if len(output_splits)>5:
                await asyncio.sleep(0.5)
        return

    async def cmd_someone(self, message, channel):
        """
        Usage:
            {command_prefix}somebody

        Pings someone. Anyone.
        """
        somebody_random = random.choice(message.guild.members)
        await channel.send(somebody_random.mention)
        await message.delete()

    @dev_only
    async def cmd_repeat(self, message, channel):
        """
        Usage:
            {command_prefix}repeat

        Repeats the content of the previous message. Written as a test of the channel.history() funciton.
        """
        i = 0
        #okay, don't judge me, this wasn't working the normal way and I'm too impatient to figure out why 
        async for elem in channel.history(limit=2):
            if i==0:
                i+=1
                continue
            await channel.send(elem.content)
            return

    @dev_only
    async def cmd_echo(self, message, channel):
        """
        Usage:
            {command_prefix}echo [text]
            {command_prefix}echo [channel ID] [text]

        Use the bot as a puppet to say whatever you want. Can even send messages to specific channels remotely by specifying the ID.
        Add emojis reactions to the message using the "react" tag, just as you would in a response.txt entry.
        """

        try:
            input_text = message.content.split(" ",1)[1].strip()
        except IndexError:
            input_text=""

        reaction_info = self.get_response_reactions(input_text)
        input_text = reaction_info.pop(0)
        #input_text = input_text.replace("_","\_")

        if not input_text.strip():
            input_text = "_ _"

        if not isinstance(channel, discord.abc.PrivateChannel):
            await message.delete()

        try:
            idstring = input_text.split(" ",1)[0]
            if not len(idstring) == 18:
                raise ValueError
            channel_id = int(idstring)
            new_input_text = input_text.split(" ",1)[1]
            if not new_input_text.strip():
                raise ValueError

            channel_dest = self.get_channel(channel_id)
            mymessage = await channel_dest.send(new_input_text+"_ _")

        except (TypeError, ValueError, AttributeError):
            mymessage = await channel.send(input_text+"_ _")

        for emoji in reaction_info:
            await mymessage.add_reaction(emoji)

        if isinstance(channel, discord.abc.PrivateChannel):
            await message.add_reaction("✅")

        return


    async def cmd_profile(self, message, channel):
        """
        Usage:
            {command_prefix}profile
            {command_prefix}profile [user ID]
            {command_prefix}profile [user ping/mention]
            {command_prefix}profile [user] [user] [user] [...]

        Aliases:
            {command_prefix}pro
            {command_prefix}prof
            {command_prefix}avatar
            {command_prefix}ava
            {command_prefix}av

        Displays information about the user. If no user id is given, it will default to the command sender. Multiple users may be input at once.
        The displayed information includes avatar, username, discord ID, account creation date, and guild join date (if applicable), blacklist status, and goku status (if applicable).
        """
        if "@everyone" in message.content or "@here" in message.content:
            await channel.send("That doesn't seem like a good idea.",delete_after=40)
            return
        
        search_users = []
        if len(message.mentions)>0:
            for m in message.mentions:
                search_users.append(m)
        args = message.content.strip().split(" ")
        args = list(filter(lambda x:x, args))
        args.pop(0) #will always just be "&profile"
        for arg in args:
            if self.is_valid_id(arg):
                try:
                    searched_user = self.get_user(arg)
                    searched_user = searched_user if searched_user else await self.fetch_user(arg)
                    search_users.append(searched_user)
                except:
                    continue

        if len(search_users)==0 and len(args)>0:
            await channel.send("Sorry, but none of those were valid inputs. Please use the help command if you need more info.",delete_after=40)
            return
        elif len(search_users)<len(args):
            await channel.send("Some of those users were not valid inputs, but here's what I was able to find")
        elif len(search_users)==0 and len(args)==0:
            search_users.append(message.author)

        search_users = list(set(search_users)) #remove duplicates

        for user in search_users:
            member, guild = None, None
            if not isinstance(channel,discord.abc.PrivateChannel): #channel.guild throws exception in DM channel
                guild = channel.guild
                member = guild.get_member(user.id) #might need to add fetch_user if returns None incorrectly
                #member = guild.fetch_member

            text_title = user.name if not member else member.name +(" AKA "+member.nick if member.nick else "") #the things I do to save a line
            text_title += " [OWNER]" if user.id == self.config.owner_id else (" [DEV]" if str(user.id) in self.config.dev_ids else (" [JEFF]" if user.id==0000000 else ("[BNUUY]" if user==self.user else ("[BOT]" if user.bot else ""))))

            #add scoreboard leader tags
            top_gokus = self.getScoreboardTop("goku",guild_id=(None if not guild else guild.id))
            top_scoopers = self.getScoreboardTop("pooper_scooper",guild_id=(None if not guild else guild.id))
            top_wordles = self.getScoreboardTop("wordle_wins",guild_id=(None if not guild else guild.id))
            text_title += " [GOKU]" if any(user.id==u.id for u in top_gokus) else ""
            text_title += " [💩]" if any(user.id==u.id for u in top_scoopers) else ""
            text_title += " [🟩]" if any(user.id==u.id for u in top_wordles) else ""

            embed = discord.Embed(title = text_title, color=user.color)
            embed.set_image(url = user.avatar_url)
            if user.id in self.blacklist:
                embed.add_field(name="Blacklisted!",value="_ _",inline=False)
            embed.add_field(name="User ID:",value=str(user.id),inline=False)
            embed.add_field(name="Account created on:",value = user.created_at.strftime("%B %-d, %Y, at %-I:%m:%S %p"), inline=False)
            if member:
                embed.add_field(name="Server joined on:",value = member.joined_at.strftime("%B %-d, %Y, at %-I:%m:%S %p"), inline=False)
                embed.add_field(name="Goku status:",value = "Points: "+str(self.getScoreboard(member.id, "goku"))+"\nToday's Attempts: "+str(self.getScoreboard(member.id, "goku_attempts")), inline=True)
                embed.add_field(name="Poops Scooped:",value = str(self.getScoreboard(member.id, "pooper_scooper")), inline=True)
            #embed.add_field(name="Blacklisted?",value = "Yes" if user.id in self.blacklist else "No", inline=True)
            embed.set_footer(text="User ID again: "+str(user.id))
            await channel.send(embed=embed,delete_after=180)

        return



    @dev_only
    async def cmd_kmaconf(self, message, channel):
        """
        Usage:
            {command_prefix}kmaconf

        Checks the previous message and gives you the confidence that it is keymashing. Used for debugging.
        """
        await message.delete()
        test_text = ""
        async for elem in channel.history(limit=1):
            test_text = elem.content
            break
        if not test_text.strip():
            await channel.send("Sorry, no text was found",delete_after=15)
            return

        start_time = time.time()
        score = self.keymash_test(test_text)
        end_time = time.time()
        await channel.send("KmaConfidence: "+str(round(score*100,2))+"%, Time: "+str(round(end_time-start_time,3))+" seconds")
        return

    async def cmd_starwars(self, message, channel):
        """
        Usage:
            {command_prefix}starwars

        Plays starwars ASCII. Don't ever use this command. (Can be cancelled by reacting with the "X" emoji)
        """
        lines_per_frame = 14
        delay = 120

        frame_message = await channel.send("Loading...")

        with open(os.path.abspath("misc_data/starwars.txt"),"r") as f:
            lines = f.readlines()

        for i in range(0,len(lines),lines_per_frame):
            next_frame = ""
            for j in range(1,lines_per_frame):
                next_frame += lines.pop(1)+"\n"
            try:
                await frame_message.edit(content=next_frame.replace("_","\_"))
                await asyncio.sleep(int(lines.pop(0))*delay*0.001)
            except discord.errors.HTTPException:
                await frame_message.edit(content="_ _")
                await asyncio.sleep(int(lines.pop(0))*delay*0.001)
            except discord.errors.NotFound:
                return
        await frame_message.edit(content="The End!")
        return

    #Parses the data for a given roll command into a list of integers. If a given value is unspecified, default values are used.
    #The order and default values are teh following:
    #[rolls=1, sides=6, delete_lowest=0, keep_lowest=0, delete_highest=0, keep_highest=0, add_modifier=0, multiply_modifier=1, quantum_roll=0]
    #raises ValueError when something is invalid
    #TODO - return a dictionary instead of a list to make it easier to decypher
    #TODO - make the token parsing actually decent
    def parse_roll(self, input):
        tokens=[]
        values=[]
        #remove any q's that represent a quantum roll
        quant=False
        if "q" in input:
            input=input.replace("q","",1)
            quant=True

        #number of rolls - outlier because it goes first and has no leading string
        if input[0].isdigit():
            i = 0
            while i<len(input) and input[i].isdigit():
                i+=1
            tokens.append("r") #placeholder token for first number
            values.append(int(input[0:i]))
            input = input[i:]

        while len(input)>0:
            #get leading string:
            i = 0
            while not input[i].isdigit():
                i+=1
            tokens.append(input[0:i])
            input=input[i:]
            #get following number
            i=0
            while i<len(input) and input[i].isdigit():
                i+=1
            values.append(int(input[0:i]))
            input=input[i:]

        if len(values) != len(list(set(tokens))): #a token was used more than once, or something else went wrong.
            raise IndexError

        #sort out the tokens and values
        output = [1,6,0,0,0,0,0,1,(1 if quant else 0)]
        for i in range(len(tokens)):
            if tokens[i]=="r":
                output[0]=values[i]
            elif tokens[i]=="d":
                output[1]=values[i]
            elif tokens[i]=="dl":
                output[2]=values[i]
            elif tokens[i]=="kl":
                output[3]=values[i]
            elif tokens[i]=="dh":
                output[4]=values[i]
            elif tokens[i]=="kh":
                output[5]=values[i]
            elif tokens[i]=="+":
                output[6]+=values[i]
            elif tokens[i]=="-":
                output[6]-=values[i]
            elif tokens[i]=="*":
                output[7]*=values[i]
            elif tokens[i]=="/":
                output[7]/=values[i]
            else:
                print("BAD TOKEN: "+tokens[i])
                raise IndexError
        return output

    #I was having difficulties with list.copy(), so I just did it myself. Clones the contents of a 1D list (probably) into a new list.
    def myclone1d(self, list):
        newlist = []
        for i in range(len(list)):
            newlist.append(0)
            newlist[i] = list[i]
        return newlist

    #gets a list of [count] lowest values in the input list
    def minimums(self, input, count):
        minis = []
        if count==0:
            return minis
        input = self.myclone1d(input) #prevent this method from modifying the list, probably
        for i in range(count):
            mini = min(input)
            minis.append(mini)
            input.remove(mini)
        return minis

    #gets a list of [count] largest values in the input list
    def maximums(self, input, count):
        maxis = []
        if count==0:
            return maxis
        input = self.myclone1d(input) #prevent this method from modifying the list, probably
        for i in range(count):
            maxi = max(input)
            maxis.append(maxi)
            input.remove(maxi)
        return maxis

    async def cmd_roll(self, message, channel):
        """
        Usage:
            {command_prefix}roll #
            {command_prefix}roll d#
            {command_prefix}roll #d#
            {command_prefix}roll #d#[other tokens w/ numbers (see below)]
            {command_prefix}roll [roll1] [roll2] [...]

        Aliases:
            {command_prefix}r

        Rolls a d# die a specified number of times and gives the total.
        Defaults to one roll when the roll count is not specified. Defaults to a d6 when the die number is not specified.
        Multiple dice rolls can be done in a single command by putting spaces between them.
        You may specify additional operations using tokens and numbers. Every token must be followed by a number!
        You can even split the multiverse by using a quantum random number as the rng seed with the q token! (learn more at qrng.anu.edu.au)
        ===Supported Tokens:===
            dl -> deletes lowest rolls
            kl -> keep only lowest rolls
            dh -> deletes highest rolls
            kh -> keep only highest rolls
            (* or /) -> scales the sum by a constant
            (+ or -) -> offsets the sum by a constant (performed after scaling)
            q -> Can go anywhere in the input. Seeds the rolls with a quantum random number (may run slower)
        Examples:
            {command_prefix}roll 3d10kh2 -> rolls 3 d10s and keeps the highest 2 rolls
            {command_prefix}roll 20d100dl2dh5 -> rolls 20 d100s and removes the lowest 2 and highest 5 rolls
            {command_prefix}roll 5d6+10 -> rolls 5 d6 and adds 10 to the result
            {command_prefix}roll d8*5 -> rolls a d8 and multiplies the result by 5
            {command_prefix}roll 3d10q -> rolls 3 d10s with a quantum random number as a seed

        TODO - make the input parsing code less of a nightmare
        """
        simplify_output = False #determines if the final output is simplified (to remove clutter)
        inputs = message.content.strip().split(" ")
        inputs.pop(0)
        if len(inputs)==0:
            inputs.append("1d6")
            simplify_output = True
        elif len(inputs)==1:
            simplify_output = True

        for i in range(len(inputs)): #Because it bothers me: Denote the default 'd6' when it's not mentioned. Only affects clarity of output.
            if not "d" in inputs[i].replace("dl","").replace("dh",""):
                inputs[i] = inputs[i]+"d6"
                simplify_output = False

        parsed = []
        try:
            for input in inputs:
                if ("dl" in input and "kl" in input) or ("dh" in input and "kh" in input) or ("kl" in input and "kh" in input):
                    raise AttributeError #incompatible tokens are present - (e.g. cannot keep highest and keep lowest simultaneously)
                parse = self.parse_roll(input)
                if parse[2]+parse[4]>parse[0] or parse[3]>parse[0] or parse[5]>parse[0]:
                    raise AttributeError #there's not enough total rolls to work properly
                if parse[0]>500:
                    raise ValueError #roll limit
                parsed.append(parse)
        except ValueError:
            await channel.send("Sorry, but the limit on the number of rolls you can make at once is 500",delete_after=30)
            return
        except AttributeError:
            await channel.send("Those values are incompatible, sorry",delete_after=30)
            return
        except IndexError:
            await channel.send("I had trouble parsing your input. Use `&help roll` to learn how the command should look.",delete_after=30)
            return

        output = ""
        for i in range(len(parsed)):
            p = parsed[i]
            rolls = []
            if p[8]==1: #quantum flag is True
                #print("QTM")
                qrng = get_data()[0] #seed the rng with a quantum random number
                #print(qrng)
                random.seed(qrng)
            for j in range(p[0]):
                
                roll = random.randint(1,p[1])
                rolls.append(roll)

            dl = p[2] if p[2]>0 else (p[0]-p[5] if p[5]>0 else 0) #number of minimums to delete
            dh = p[4] if p[4]>0 else(p[0]-p[3] if p[3]>0 else 0) #number of maximums to delete

            minimums = self.minimums(rolls, dl)
            maximums = self.maximums(rolls, dh)
            line = ""
            sum = 0
            for roll in rolls:
                if roll in minimums:
                    line+="~~"+str(roll)+"~~, "
                    minimums.remove(roll)
                elif roll in maximums:
                    line+="~~"+str(roll)+"~~, "
                    maximums.remove(roll)
                else:
                    line+=str(roll)+", "
                    sum+=roll

            line = line[:-2] #this works apparently - removes trailing comma
            if p[6]!=0 or p[7]!=1 or len(rolls)>2 or (len(rolls)>1 and sum>20): #is it necessary to display a total?
                line += "   Total: "+str(sum) + (" * "+str(p[7]) if p[7]!=1 else "") + (" + "+str(p[6]) if p[6]!=0 else "")
                if p[7]!=1 or p[6]!=0:
                    line+= " = "+str(sum*p[7]+p[6])
            if simplify_output:
                output+=line+"\n"
            else:
                output+="_"+inputs[i]+":_   "+line+"\n"
        await channel.send(output)
        return

    async def cmd_addquote(self, message, channel, reactedMessage=None):
        """
        Usage:
            {command_prefix}addquote

        Aliases:
            {command_prefix}addquo
            {command_prefix}addq

        Saves a message to the quote database. The message using the command must be a reply to the message you wish to quote. The quotes can be retrieved with the {command_prefix}quo command.
        """
        if not reactedMessage:
            if not (message.reference and message.reference.resolved):
                await channel.send("Sorry, you need to reply to a message when using that command",delete_after=15)
                return
            old_message = await channel.fetch_message(message.reference.message_id)
        else:
            old_message = reactedMessage #for when adding quotes based on the camera emoji reaction

        old_author = old_message.author
        guildID = str(old_message.channel.guild.id)
        timestamp = (old_message.created_at-timedelta(hours=5)).strftime("%m/%d/%Y at %H:%M:%S (ET)")
        new_quote = old_message.content.replace("\n"," ").strip()
        #if old_author == self.user:
        #    await channel.send("Nice try.",delete_after=10)
        #    return
        if len(new_quote)<3:
            await channel.send("Sorry, that quote is too short.",delete_after=10)
            return
        name = old_author.name #old_author.name if not old_author.nick else old_author.nick #uncomment to save nicknames instead of usernames
        quote_dict = {"author":name, "avatar_url":str(old_author.avatar_url), "guild_id":guildID, "timestamp":timestamp, "quote":new_quote}
        with open(os.path.abspath("misc_data/quotes.txt"),"a") as f:
            f.write("\n")
            f.write(json.dumps(quote_dict))
        await message.add_reaction("✅")
        return

    async def cmd_quote(self, message, channel):
        """
        Usage:
            {command_prefix}quote [search text]
            {command_prefix}quote

        Aliases:
            {command_prefix}quo

        Retrieves all saved quotes containing [search text]. If no search text is provided, retrieves a random quote.
        """
        try: #this is the only way I could think of doing this
            search_text = message.content.split(" ",1)[1].replace("\n"," ").strip().lower()
        except IndexError:
            search_text=""
        if len(search_text)<3 and len(search_text)>0:
            await channel.send("Please use a longer search string.",delete_after=10)
            return
        with open(os.path.abspath("misc_data/quotes.txt"),"r") as f:
            lines = f.readlines()
        matches = []
        if len(search_text)==0: #no quote specified - find a random one from the same guild
            while len(lines)>0:
                line = lines.pop(0)
                if not line.strip() or line.startswith("##"):
                    continue
                quote_dict = json.loads(line)
                if channel.guild.id == int(quote_dict["guild_id"]):
                    matches.append(quote_dict)
            matches = [random.choice(matches)]
        else:
            while len(lines)>0:
                line = lines.pop(0)
                if not line.strip() or line.startswith("##"):
                    continue
                quote_dict = json.loads(line)
                if search_text in quote_dict["quote"].lower() and channel.guild.id == int(quote_dict["guild_id"]):
                    matches.append(quote_dict)

        counter = 0
        for match in matches:
            if counter>100: #the limit for quote matches sent
                await channel.send("The maximum number of matching quotes has been reached! ("+str(counter-1)+" quotes)")
                break
            quote = match["quote"]
            #author_name = quote_info[0]
            #author_imgurl = quote_info[1]
            #guildID = int(
            #timestamp = quote_info[3]
            if len(quote)<=250:
                embed = discord.Embed(title=quote,color=discord.Color.from_rgb(136, 36, 242))
            else:
                embed = discord.Embed(color=discord.Color.from_rgb(136, 36, 242))
                embed.add_field(name="_ _",value=quote)
            embed.set_author(name="A Quote from "+match["author"],icon_url=match["avatar_url"])
            embed.set_footer(text=match["timestamp"])
            await channel.send(embed=embed)
            counter += 1
        if len(matches)==0:
            await channel.send("Sorry, there are no quotes that contain that substring. Maybe you mistyped?",delete_after=15)
            return
        return

    #converts a user with a given id into a margaret across all guilds. Also records their previous username to the margaret file.
    #will not prevent the user from changing their username unless it's the weekend and they are listed in the margaret id list (in custom configs)
    async def margaret(self,id):
        print("Marging: "+str(id))
        count = 0
        margarets = []
        for g in self.guilds:
            try:
                member = await g.fetch_member(id)
            except: #If you can't find them, just skip and move on (I'm too lazy to do real error handling)
                print("Problem getting member: {} from guild {}".format(str(id),g.name)) 
                continue
            
            old_nick = member.nick
            if old_nick==None:
                old_nick = member.name
            save_string = str(g.id)+";;"+str(member.id)+";;"+old_nick
            margarets.append(save_string)
            try:
                await member.edit(nick="{ig}Margaret")
                count+=1
            except:
                margarets.pop()

        if len(margarets)==0:
            print("    No marging possible")
            return

        with open(os.path.abspath("misc_data/margs.txt"),"a") as f:
            for marg in margarets:
                f.write("\n")
                f.write(marg)
        print("    Marging done in servers: "+str(count))
        return

    #undoes all current margarets and deletes the margaret save file
    async def unmargaret_all(self):
        print("Starting unmarging")
        count = 0
        with open(os.path.abspath("misc_data/margs.txt"),"r") as f:
            lines = f.readlines()
        for line in lines:
            if not line.strip():
                continue
            info = line.split(";;",2)
            try:
                search_guild = self.get_guild(int(info[0]))
                search_member = await search_guild.fetch_member(int(info[1]))
                try:
                    await search_member.edit(nick=info[2])
                    count +=1 
                except:
                    print("THERE WAS AN UNMARGING ERROR!: Could not reset name: "+line)
            except:
                print("THERE WAS AN UNMARGING ERROR!: Could not find guild and/or member from: "+line)
        await self.run_cli("rm "+os.path.abspath("misc_data/margs.txt")) #cleanup afterwards

        print("Finished unmarging members: "+str(count))
        return


    def load_configs(self):
        """
        Reloads the custom config variables from MusicBot/config/custom_configs.txt.
        TODO - use ConfigParser to make this much much easier.
        """
        print("RELOADING CUSTOM CONFIGS")
        with open(os.path.abspath("config/custom_configs.txt"),"r") as f:
            lines = f.readlines()
        for line in lines:
            if not line.strip() or line.startswith("##"): #comment line
                continue

            before = line.split("::")[0].strip()
            try:
                after = line.split("::")[1].strip()
            except IndexError:
                after = ""
            inputs = after.split(",")
            if len(inputs)==1 and not inputs[0]: #this is necessary when there's no input values
                inputs=[]

            if before=="ANYWHERE_COMMANDS":
                self.anywhere_commands = []
                for input in inputs:
                    self.anywhere_commands.append(input)
                #print(self.anywhere_commands)

            elif before=="DM_COMMANDS":
                self.dm_commands = []
                for input in inputs:
                    self.dm_commands.append(input)
                #print(self.dm_commands)

            elif before=="MARGARET_IDS":                
                self.margaret_ids = []
                for input in inputs:
                    self.margaret_ids.append(int(input))
                #print(self.margaret_ids)

            elif before=="PORTMANTEAU_EXCLUDES":
                self.portmanteau_excludes = []
                for input in inputs:
                    self.portmanteau_excludes.append(input)
                #print(self.portmanteau_excludes)

            elif before=="UNFUN_GUILDS":                
                self.unfun_guilds = []
                for input in inputs:
                    self.unfun_guilds.append(int(input))
                #print(self.unfun_guilds)

            elif before=="MAX_DAILY_GOKUS":
                self.max_daily_gokus = int(after)
                #print(self.max_daily_gokus)

            elif before=="RESPONSE_FREQUENCY":
                self.response_frequency = float(after)
                #print(self.response_frequency)

            elif before=="MAX_RESPONSES":
                self.max_responses = int(after)
                #print(self.max_responses)

            elif before=="POOP_PROBABILITY":
                self.poop_probability = float(after)
                #print(self.poop_probability)

            elif before=="SLOPPINESS_MULTIPLIER":
                self.sloppiness_multiplier = float(after)
                #print(self.sloppiness_multiplier)

            else:
                print("I have no idea what this means: "+before)
        return

    def clean_audio_cache(self):
        """
        Iterates through the audio_cache folder and removes all songs that haven't been played recently.
        DAY_LIMIT is the maximum number of days a song can remain inactive.
        """
        DAY_LIMIT = 15
        print("CLEANING AUDIO CACHE NOW")
        file_list = os.listdir(os.path.abspath("audio_cache"))
        for file in file_list:
            days_since_edit = (int(time.time()) - os.path.getctime(os.path.abspath("audio_cache/"+file)))/24/3600
            #print(str(days_since_edit)+" "+file)
            if days_since_edit>DAY_LIMIT:
                print("    REMOVING: "+str(days_since_edit)+"   "+os.path.abspath("audio_cache/"+file))
                os.remove(os.path.abspath("audio_cache/"+file))
        print("DONE")
        return

    #clears out the text files parsed from images throughout the day
    def clean_image_cache(self):
        print("CLEANING IMAGE CACHE NOW")
        file_list = os.listdir(os.path.abspath("image_cache"))
        for file in file_list:
                os.remove(os.path.abspath("image_cache/"+file))
        print("DONE")
        return

    @dev_only
    async def cmd_reboot(self,message):
        """
        Usage:
            {command_prefix}reboot
        Reboots the bot's computer. The bot should automatically restart afterwards if systemd is configured properly.
        """
        await message.add_reaction("✅")
        try:
            await self.alert_owner("Rebooting now!")
            self.scorekeeper.save(force_all=True)
            await self.cmd_autosave(None)
            await self.logout()
            await self.run_cli("sudo reboot now")
        except:
            await message.add_reaction("❗️") #when something goes wrong

    @dev_only 
    async def cmd_cancelreboot(self,message):
        """
        Usage:
            {command_prefix}cancelreboot
        If the bnuuy has started a reboot sequence, it will cancel it. Otherwise, idk. Maybe it starts a reboot, who knows?
        """
        await self.alert_owner("Naptime is cancelled!")
        await self.run_cli("sudo shutdown -c")
        await message.add_reaction("✅")


    #notifies the owner, then starts the reboot process (30 minutes until reboot)
    #TODO - figure out how to make this safer
    async def start_reboot(self):
        await self.alert_owner("Rebooting in 30 minutes!")
        await self.run_cli("sudo shutdown -r +30")
        self.scorekeeper.save(force_all=True)

    #checks if the time has come to reboot, and starts the reboot process if so.
    async def reboot_maybe(self):
        self.days_until_reboot -= 1
        print("REBOOT CHECK: Days to Reboot: "+str(self.days_until_reboot))
        #await self.alert_owner("Shutdown in "+str(self.days_until_reboot)+" days")
        if self.days_until_reboot <= 0:
            await self.start_reboot()

    #Tasks that run at midnight every night. Includes conditional statements for every day of the week.
    @tasks.loop(seconds=30.0,minutes=59.0,hours=23.0)
    async def midnight_loop(self):

        tomorrow = datetime.now() + timedelta(days=1)
        midnight = datetime(year=tomorrow.year, month=tomorrow.month, day=tomorrow.day, hour=0, minute=0, second=0)
        seconds_to_midnight = (midnight-datetime.now()).seconds
        print("Midnight Loop: Waiting for "+str(seconds_to_midnight)+" seconds")
        await asyncio.sleep(seconds_to_midnight+2)
        print("Done waiting")
        print("Midnight Loop starting at "+datetime.now().strftime("%m/%d/%Y at %H:%M:%S ET"))

        if datetime.today().weekday()==0: #Monday
            print("=====MONDAY=====")
            await self.unmargaret_all()
        if datetime.today().weekday()==1: #Tuesday
            print("=====TUESDAY=====")
        if datetime.today().weekday()==2: #Wednesday
            print("=====WEDNESDAY=====")
        if datetime.today().weekday()==3: #Thursday
            print("=====THURSDAY=====")
        if datetime.today().weekday()==4: #Friday
            print("=====FRIDAY=====")
        if datetime.today().weekday()==5: #Saturday
            print("=====SATURDAY=====")
            for user_id in self.margaret_ids:
                await self.margaret(user_id)
        if datetime.today().weekday()==6: #Sunday
            print("=====SUNDAY=====")

        #Daily stuff
        await self.cmd_shower(None)     #take a shower to clean out the caches and stuff
        await self.cmd_autosave(None)   #make an autosave of the misc_data in case something fucky happens
        #await self.send_birthdays()     #Send bday reminders
        await self.reboot_maybe()       #check if we should reboot in 30 minutes

    @dev_only
    async def cmd_midnighttest(self, message):
        """
        Usage:
            {command_prefix}midnighttest

        Manually triggers the midnight loop once.
        """


    @dev_only
    async def cmd_shower(self, message):
        """
        Usage:
            {command_prefix}shower

        Manually runs the nighttime cleanup methods. (cleans: audio_cache, image_cache, goku_attempts, daily emotion)
        """
        self.clean_audio_cache()
        self.clean_image_cache()
        print("CLEANING SCOREBOARDS NOW")
        await self.run_cli("rm "+os.path.abspath("misc_data/scoreboards/goku_attempts.json"))
        await self.run_cli("rm "+os.path.abspath("misc_data/scoreboards/wordle_attempts.json"))
        await self.run_cli("rm "+os.path.abspath("misc_data/scoreboards/wordle_finished.json"))
        self.scorekeeper.load()
        print("DONE")
        #print(self.scorekeeper.scoreboards)
        self.set_secret_word()
        await self.new_emotion()
        print("RESEEDING QRNG")
        try:
            random.seed(get_data()[0]) #reseed the RNG with a new quantum random number (takes some time)
        except:
            pass
        print("DONE")
        if message:
            await message.add_reaction("✅")
        return

    @dev_only
    async def cmd_autosave(self, message):
        """
        Usage:
            {command_prefix}autosave
        
        Zips up a couple of important files/directories.
        Runs automaticallly every midnight and just before shutdown or reboot is run.
        I understand the irony of "autosave" being a manual command, but the function is mostly used internally.
        """
        print("AUTOSAVING STUFF")
        autosave_files = ["misc_data"] #add more files/directories here as necessary
        for file in autosave_files:
            command_string = "zip -r "+os.path.abspath(file)+"-autosave "+os.path.abspath(file)
            #print(command_string)
            result = await self.run_cli(command_string)
            print("DONE")
            #print(result)
        if message:
            await message.add_reaction("✅")
        return

    @dev_only
    async def cmd_margtest(self,message,channel):
        """Margs everyone in the margaret_id list"""
        for user_id in self.margaret_ids:
            try:
                await self.margaret(user_id)
            except:
                pass
        return

    @dev_only
    async def cmd_unmargtest(self,message,channel):
        """Unmargs everyone who is currently marg-ed"""
        await self.unmargaret_all()
        return

    @dev_only
    async def cmd_clearytcache(self, message, channel):
        """
        Usage:
            {command_prefix}clearytcache

        Clears the youtube-dl download cache. Use this when you suspect youtube-dl may be acting up. (Specifically in the case of HTTPException: 403 Forbidden).
        """
        output = await self.run_cli("youtube-dl --rm-cache-dir")
        if output[1].strip():
            await message.add_reaction("😳")
            print(output[1])
        else:
            await message.add_reaction("✅")
        return

    @dev_only
    async def cmd_reloadconfigs(self, message, channel):
        """
        Usage:
            {command_prefix}reloadconfigs

        Reloads the custom config variables. NOTE: Doesn't reload any of the regular configs, only the custom ones I added.
        """
        #try:
        self.load_configs()
        await message.add_reaction("✅")
        #except:
            #await message.add_reaction("😳")
        return

    async def cmd_dadjoke(self, channel):
        """
        Usage:
            {command_prefix}dadjoke

        Gives you a random dad joke!
        """
        with open(os.path.abspath("misc_data/dadjokes.txt"),"r") as f:
            random_joke=""
            while not random_joke.strip():
                random_joke=random.choice(f.readlines())
        lines = random_joke.split(";;")
        for line in lines:
            await channel.send(line,delete_after=60)
            await asyncio.sleep(4)
        return


    def changeScoreboard(self, id, scoreboard_name, value=1, increment=True):
        """
        Used to save/alter an abstracted scoreboard for users. Scoreboard files are saved under misc_data/scoreboards/. If no file is found, a new one is created.
        id -- user's discord ID (integer)
        scoreboard_name -- the name for the scoreboard
        number -- the number used to set/alter the scoreboard
        increment -- if True, increments an existing value for the user. if False, overwrites an existing value. (if a value doesn't exist yet, this will make no difference)

        TODO - remove this method, as it is just a scorekeeper wrapper at this point
        """
        return self.scorekeeper.change_score(id, scoreboard_name, value=value, increment=increment)


    #wrapper for setting a scoreboard to a specific value
    def setScoreboard(self, id, scoreboard_name, value):
        return self.scorekeeper.set_score(id, scoreboard_name, value=value)

    #wrapper for getting the scoreboard value for a specific user. If no value exists, this will also assign them a value of 0.
    def getScoreboard(self, id, scoreboard_name):
        return self.scorekeeper.get_score(id, scoreboard_name, show_none=False)


    def getScoreboardList(self, scoreboard_name, count=10, guild_id=None):
        """
        retrieves the [count] top scoreboard lines from a scoreboard file (returns as list of Strings)
        if guild_id is specified, it will ignore users not in the specified guild
        returns a sorted list of strings, but the format is weird: [user_id];;[score]
        TODO - actually implement dict processing
        """

        scores_dict = self.scorekeeper.get_scoreboard(scoreboard_name, show_none=False)
        lines=[]
        for key in scores_dict:
            if key=="__saved__":
                continue
            try:
                lines.append(key+";;"+str(int(scores_dict[key]))) #this is a hacky fix - only allows int scores
            except TypeError: #in case a score is not an int or something
                continue

        lines = sorted(lines, key=lambda line: int(line.split(";;",1)[1])) #Sorts list in order of score. Not sure how I would do this to a dict.

        guild = self.get_guild(guild_id) if guild_id else None
        board = []
        for i in range(len(lines)-1,-1,-1):
            line = lines[i].replace("\n","")
            if len(board)>=count:
                break
            if guild and not guild.get_member(int(line.split(";;",1)[0])):
                continue
            board.append(line)

        #remove trailing zeros - done at the end in case you want a scoreboard that can go negative
        for i in range(len(board)-1,-1,-1):
            count = int(board[i].split(";;",1)[1])
            if count != 0:
                break
            else:
                board.pop(i)
        return board

    
    def getScoreboardListPretty(self, scoreboard_name, count=10, guild_id=None):
        """
        wrapper for getScoreboardList. Formats the lines into a readable ranked list (rank, username, score)
        returns a list of Strings
        """
        board = self.getScoreboardList(scoreboard_name, count, guild_id)
        nice_board = []
        counter = 0
        last_score = 0
        for line in board:
            line_info = line.split(";;",1)
            if int(line_info[1])<last_score or last_score==0:
                counter += 1
                last_score = int(line_info[1])
            user = self.get_user(int(line_info[0]))
            user = user if user else self.fetch_user(int(line_info[0])) #hopefully it doesn't come down to this
            nice_board.append(str(counter)+".   "+user.name+":    "+line_info[1])

        return nice_board

    #grabs the User object(s) for the top user(s) of the specified scoreboard. Returns them in a list.
    #if guild_id is specified, will only search for users in the given guild
    def getScoreboardTop(self, scoreboard_name, guild_id=None):
        board = self.getScoreboardList(scoreboard_name, count=1, guild_id=guild_id)
        if len(board)==0:
            return None
        users = []
        top_score = int(board[0].split(";;",1)[1])
        for b in board:
            user_id = int(b.split(";;",1)[0])
            user_score = int(b.split(";;",1)[1])
            if user_score<top_score:
                break
            user = self.get_user(user_id)
            user = user if user else self.fetch_user(user_id)
            users.append(user)
        
        return users

    async def cmd_goku(self, message, channel):
        """
        Usage:
            {command_prefix}goku

        Gives you a ranked list of the top Gokus on your server.
        """

        board = self.getScoreboardListPretty("goku", count=10, guild_id=channel.guild.id)
        output_string=""
        for line in board:
            output_string+=line+"\n"
        output_string = output_string if output_string else "There are no Gokus :("

        embed = discord.Embed(color=discord.Color.from_rgb(255, 152, 56))
        embed.set_author(name="TOP GOKUS",icon_url="https://cdn.discordapp.com/attachments/886765649827872820/903364619370909706/JyhZpbtu_400x400.jpg")
        embed.add_field(name = output_string, value = "_ _")
        embed.set_footer(text="Goku says 'Say no to drugs!'")
        await channel.send(embed=embed, delete_after = 60)
        return

    async def cmd_pooperscooper(self, message, channel):
        """
        Usage:
            {command_prefix}pooperscooper
        Aliases:
            {command_prefix}poops
        Gives you a ranked list of the top pooper scoopers.
        """

        board = self.getScoreboardListPretty("pooper_scooper", count=10, guild_id=channel.guild.id)
        output_string=""
        for line in board:
            output_string+=line+"\n"
        output_string = output_string if output_string else "There are no pooper scoopers here :("

        embed = discord.Embed(color=discord.Color.from_rgb(148, 80, 34))
        embed.set_author(name="🔫🏆",icon_url="https://cdn.discordapp.com/attachments/886765649827872820/930575206215475240/poop-emojis-archives-jason-graham-4.png")
        embed.add_field(name = output_string, value = "_ _")
        embed.set_footer(text="Thank you for cleaning up my shit")
        await channel.send(embed=embed, delete_after = 60)
        return

    async def cmd_wordletop(self, message, channel):
        """
        Usage:
            {command_prefix}wordletop

        Gives you a ranked list of the top wordle scorers on your server.
        """

        board = self.getScoreboardListPretty("wordle_wins", count=10, guild_id=channel.guild.id)
        output_string=""
        for line in board:
            output_string+=line+"\n"
        output_string = output_string if output_string else "There are no wordle winners :("

        embed = discord.Embed(color=discord.Color.from_rgb(16, 232, 110))
        embed.set_author(name="TOP WORDLERS")#,icon_url="https://cdn.discordapp.com/attachments/886765649827872820/903364619370909706/JyhZpbtu_400x400.jpg")
        embed.add_field(name = output_string, value = "_ _")
        embed.set_footer(text="Goku says 'Say no to drugs!'")
        await channel.send(embed=embed, delete_after = 60)
        return

    #grabs a random emotion
    def get_daily_emotion(self):
        seed_number = (datetime.now()-datetime(day=1, month=1, year=1)).days #ensures the word doesn't change when bot restarts
        with open(os.path.abspath("misc_data/emotions.txt"),"r") as f:
            random.seed(seed_number)
            return random.choice(f.readlines())

    #updates the current emotion status
    async def new_emotion(self):
        await self.change_presence(activity=discord.Game(name="Emotion: "+self.get_daily_emotion()))

    @dev_only
    async def cmd_cum(self, message, channel):
        """
        Usage:
            {command_prefix}cum
        Displays the cum leaderboard counter. TODO - make cum leaderboard counter.
        """
        return

    @dev_only
    async def cmd_regenemotion(self,message):
        """
        Usage:
            {command_prefix}regenemotion
        Regenerates the daily emotion. Happens automatically at midnight.
        """
        await self.new_emotion()
        await message.add_reaction("✅")
        return

    @dev_only
    async def cmd_svoe(self, message, channel):
        """
        Usage:
            {command_prefix}esvoe
        Runs the ESVOE (enhanced subject verb object extraction) stuff on a message to see the subject, verb, and object.
        Very slow (takes ~72 seconds to load the model). Not practical for use :\.
        """
        await message.delete()
        test_text = ""
        async for elem in channel.history(limit=1):
            test_text = elem.content
            break
        if not test_text.strip():
            await channel.send("Sorry, no text was found",delete_after=15)
            return
        test_text = test_text.replace("‘","'").replace("'s"," is").replace("'t"," not")
        start_time = time.time()
        file_text = "import sys\nfrom subject_verb_object_extract import findSVOs, nlp\nstring = \""+test_text+".\"\nsys.stdout.write(str(findSVOs(nlp(string))))"
        with open("/home/pi/ESVOE/test.py","w") as f:
            f.write(file_text)
        out = await self.run_cli("python3 /home/pi/ESVOE/test.py")
        await channel.send("SVOs: "+str(out[0])+" , Time: "+str(round(time.time()-start_time,3))+" seconds")
        return


    async def run_cli(self,cmd):
        """
        Runs a shell command without blocking async tasks.
        Returns a list of strings containing the stdout, stderr, and exit code (in that order)
        """
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)

        stdout, stderr = await proc.communicate()
        output = ["","",""]
        if stdout:
            output[0]= str(f'{stdout.decode()}')
        if stderr:
            output[1]= str(f'{stderr.decode()}')
        output[2]= str(f'[{cmd!r} exited with {proc.returncode}]')
        return output

    @dev_only
    async def cmd_summary(self, message, channel):
        """
        Usage:
            {command_prefix}summary [#] [url]
            {command_prefix}summary [url]

        Summarizes the contents of some webpage in a specified number of sentences (defaults to 10 if unspecified).
        Results may vary.
        """
        contents = message.content.split(" ")
        url = ""
        sentence_count = 10
        if len(contents)==1:
            await channel.send("You need to specify a url! Use \"&help summary\" for help.",delete_after=15)
            return
        elif len(contents)==2:
            url = contents[1]
        elif len(contents)==3:
            try:
                sentence_count = int(contents[1])
            except:
                await channel.send(contents[1]+" is not a valid sentence number",delete_after=15)
                return
            if sentence_count<0 or sentence_count>40:
                await channel.send("Please choose a more reasonable number",delete_after=15)
                return
            url = contents[2]
        else:
            await channel.send("You're using the command wrong! Use \"&help summary\" for help.",delete_after=15)
            return

        wait_msg = await channel.send("Processing... (may take a minute)",delete_after=3600)

        start_time = time.time()
        output = await self.run_cli("sumy lex-rank --length="+str(sentence_count+1)+" --url="+url)
        time_elapsed = time.time()-start_time
        await message.add_reaction("✅")

        output_text = ""
        if output[1]:
            output_text = "There was an error while reading that url!\nYour url is bad and you should feel bad!"
            print("There was an exception while summarizing a url! Here's the error:\n"+output[1])
        else:
            output_text = output[0]

        embed = discord.Embed(title="Summary",color=discord.Color.from_rgb(143, 175, 227))
        output_lines = output_text.split(".")
        embed.add_field(name = "Here's what I got:", value = output_lines[0]+".", inline=False)
        if len(output_lines)>1:
            for i in range(1,len(output_lines)):
                if output_lines[i].strip():
                    embed.add_field(name = "_ _", value = output_lines[i]+".",inline=False)
        embed.set_footer(text="Completed in "+str(round(time_elapsed,1))+" seconds")
        try:
            await wait_msg.delete()
        except:
            print("Wha? Where did that message go?")
        await channel.send(embed=embed)
        return


    async def cmd_choose(self, message, channel):
        """
        Usage:
            {command_prefix}choose [choices]

        Chooses one option from a list of semicolon-separated values.

        Example:
            {command_prefix}choose be a normal person;amogus sus imposter vent!
        """
        # Sanity-check the arguments.
        argv = message.content.split(" ", 1) # Split the first argument off.
        if (len(argv) == 1):
            # We can't choose if there's only one argument (the command name)
            await channel.send("I can't choose from that!", delete_after=10)
            return
        
        # Create the list, and send a random selection from it.
        choices = argv[1].split(";") # Choices are semicolon-delimited.
        choice = random.choice(choices)
        if not choice:
            choice = "` `"
        
        await channel.send(choice, delete_after=15)
        return

    @dev_only
    async def cmd_shell(self, message, channel):
        """
        Usage:
            {command_prefix}shell [command]

        Literally just runs the command in shell. No error checking whatsoever. Don't abuse this. Seriously.
        Returns stdout, stderror, and exit codes after running the command.
        """
        command = message.content.split(" ",1)[1]
        output = await self.run_cli(command)
        output_text = "Output:\n"+output[0]+"\n\nErrors:\n"+output[1]+"\n\nExit Code:\n"+output[2]
        if len(output_text)>1000:
            output_splits = self.split_long_text(output_text)
            for split in output_splits:
                await channel.send(split)
        else:
            await channel.send(output_text)
        return

    @dev_only
    async def cmd_echodm(self, message, channel):
        """
        Usage:
            {command_prefix}echo [User ID] [text]

        Functions similar to {command_prefix}echo, but this sends messages to the DM channels of the specified user.
        As with the {command_prefix}echo command, you can include "react" tags in the message.
        Note: Doesn't work with users who do not share a guild with the bot (I think).
        """
        try:
            input_text = message.content.split(" ",1)[1].strip()
        except IndexError:
            input_text=""
        try:
            idstring = input_text.split(" ",1)[0]
            if not len(idstring) == 18:
                raise TypeError
            user_id = int(idstring)

            dm_message_text = input_text.split(" ",1)[1]
            react_info = self.get_response_reactions(dm_message_text)
            dm_message_text = react_info.pop(0)
            if not dm_message_text.strip():
                raise ValueError

            search_user = self.get_user(int(idstring)) #avoid API call if possible
            if not search_user:
                search_user = await self.fetch_user(int(idstring))

            dm_channel = await search_user.create_dm()
            sent_message = await dm_channel.send(dm_message_text)
            for emoji in react_info:
                print(emoji)
                await sent_message.add_reaction(emoji)

        except TypeError:
            await channel.send("That was not a valid user ID!",delete_after=30)
            return
        except ValueError:
            await channel.send("Please specify the message you wish to send to the user.",delete_after=30)
            return
        except IndexError:
            await channel.send("You are missing a required field! Please specify the user ID and then the message",delete_after=30)
            return

        await message.add_reaction("✅")
        return

    @dev_only
    async def cmd_santa(self, message, channel):
        """
        Usage:
            {command_prefix}santa [(name:)person1;person2;person3...]

        Generates a named Secret Santa order, and records it to a list for later.
            Name defaults to "Santa".

        Each person can be a user id or a ping.
        """
        # Sanity-check the arguments.
        argv = message.content.split(" ", 1) # Split the arguments.
        if len(argv) < 2:
            # We can't make a list if there's only one argument (the command name)
            await channel.send("You have to provide people to choose from!", delete_after=10)
            return
        
        name = "Santa"

        if len(argv[1].split(":", 1)) > 1:
            # The user provided a name.
            name = argv[1].split(":", 1)[0]
            argv[1] = argv[1].split(":", 1)[1]
        
        # People are mean and bad.
        sanitized_name = name.lower().replace(" ", "")

        # Get a list of the users provided. The input should come as "<@!{id}>", and we only care about {id}.
        userIds = argv[1].split(";") # Users are semicolon-delimited.
        # If the input is ok, the strings should all convert fine into ints.
        try:
            userIds = [int(userId.strip("<@! >")) for userId in userIds]
        except ValueError:
            await channel.send("Hey, that's not a valid user!", delete_after=10)
            return
        # Now convert them into users. If we've gotten here, they should all be valid IDs.
        users = [self.get_user(userId) for userId in userIds]
        if None in users:
            await channel.send("Hey, that's not a valid user!", delete_after=10)
            return
        
        # Decouple the shuffle logic from the writing logic.
        originalOrder = users
        # Pair block checking implementation goes here.
        blocks = [(311910109049782274, 225717202609897472), (225717202609897472, 311910109049782274), (160146100588773377, 742124665148801056)]
        blockPresent = True
        while (blockPresent):
            blockPresent = False
            random.shuffle(users) # Shuffle the first time.
            print(users)
            for block in blocks:
                for i in range(0, len(users) - 1):
                    if (users[i].id == block[0] and users[i+1].id == block[1]):
                        blockPresent = True
                if (users[-1].id == block[0] and users[0].id == block[1]):
                    blockPresent = True
        
        # We have a list of users now. Shuffle them, save the order, and DM the next person.
        now = datetime.now().strftime('%Y-%m-%dT%H.%M.%S')
        with open(os.path.abspath("misc_data/{}-{}.txt".format(sanitized_name, now)), "w") as f:
            f.write("Participants: \n")
            [f.write("{name}#{discriminator} ({id})\n".format(name = user.name, discriminator = user.discriminator, id = user.id)) for user in originalOrder]
            f.write("\nOrder:\n")
            for i in range(0, len(users) - 1):
                f.write("{}#{} -> {}#{}\n".format(users[i].name, users[i].discriminator, users[i + 1].name, users[i + 1].discriminator))
                await users[i].send("{}: Your target is {}#{}".format(name, users[i + 1].name, users[i + 1].discriminator))
            f.write("{}#{} -> {}#{}\n".format(users[-1].name, users[-1].discriminator, users[0].name, users[0].discriminator))
            await users[-1].send("{}: Your target is {}#{}".format(name, users[0].name, users[0].discriminator))
        print("Finished generating " + name)
        await message.add_reaction("✅")
        return

    async def cmd_balloon(self, channel):
        """
        Usage:
            {command_prefix}balloon
        Spawns a balloon in the channel! See how long you can keep it from hitting the floor!
        """
        def check(reaction, user):
            return str(reaction.emoji)=="✋" and not user==self.user

        #Run the game
        count = 0
        message = await channel.send("🎈 0!")
        await message.add_reaction("✋")
        while True:
            try:
                reaction, user = await self.wait_for('reaction_add', timeout=3.0, check=check) #Wait for reaction
                await message.delete()
                count += 1
                message = await channel.send("🎈 "+str(count)+"!")

                if random.random()<0.2:
                    await message.add_reaction("❌") #sometimes add this to trick players
                await message.add_reaction("✋")

            except asyncio.TimeoutError: #Out of time error
                try:
                    await message.delete()
                    await channel.send("Oops, the balloon hit the ground!")
                except discord.errors.NotFound:
                    await channel.send("Oops, the balloon got deleted!")
                break
            except: #Some other error
                await channel.send("Oops, the balloon got deleted! (I think)")
                raise
                break
        
        #Send score report
        await channel.send("Your score was "+str(count))
        oldscore = self.scorekeeper.get_score(channel.id, "balloon_scores", show_none=False)
        if oldscore < count:
            newscore = self.scorekeeper.change_score(channel.id, "balloon_scores", value=count, increment=False)
            await channel.send("💯 That's higher than this server's previous high score of "+str(oldscore)+"! 🎉")
        elif oldscore == count:
            await channel.send("That ties this server's record! 🎉")
        else:
            await channel.send("Unfortunately that doesn't beat ther server highscore of "+str(oldscore))
        return


    #determines if an input string or integer could be a discord user id
    def is_valid_id(self,input):
        if not len(str(input))==18:
            return False
        try:
            test = int(input)
            return True
        except:
            return False
            

    def text_to_datetime(self,input, recursive=False, timezone_offset=0):
        """
        Used to convert a variety of natural-language strings into datetime objects.
        Some inputs formats operate under the assumption that the input time comes after the current system time.
        input - string - the input text
        timezone_offset - int - the timeszone offset for the current user (relative to the system's timezone). (currently unused) TODO - timezone stuff
        recursive - bool - used internally to prevent endless recursion. Don't use this.
        """
        #hacky fix - convert "on" format to "at" format
        if len(input.split(" "))>=3 and input.split(" ")[1]=="on":
            on_split = input.split(" on ",1)
            input = on_split[1]+" at "+on_split[0]

        #start parsing
        input_split = input.split(" at ",1)
        if len(input_split)>2: #sanity check
            raise IndexError
        input = input_split[0].strip() #the part before the "at" (or the whole thing if there is not "at")

        input = input.replace("today",datetime.now().strftime("%m/%d/%Y"))
        input = input.replace("nexterday", "tomorrow").replace("tomorrow",(datetime.now()+timedelta(days=1)).strftime("%m/%d/%Y"))
        input = input.replace("overmorrow",(datetime.now()+timedelta(days=2)).strftime("%m/%d/%Y"))
        input = input.replace("noon","12:00").replace("bonky","16:00").replace("midnight","23:59:59") #in case of format 3

        if "/" in input: #Format 1 - [date] at [time] - this section just parses [date]
            numbers_split = input.split("/",2)
            if len(numbers_split)==3:    #length 3-number date
                month = int(numbers_split[0])
                day = int(numbers_split[1])
                year = int(numbers_split[2])
                if year<100:
                    year+=2000
                time_day = datetime(year,month,day)

            elif len(numbers_split)==2:  #length 2-number date - assume this year (or next year if already past)
                now = datetime.now()
                month = int(numbers_split[0])
                day = int(numbers_split[1])
                year = now.year
                time_day = datetime(year,month,day)
                if time_day < datetime.now():
                    time_day = datetime(year+1,month,day)
            else:
                raise ValueError #invalid number of inputs

        elif any(signifier in input for signifier in "YyMDWwdhms") and not any(sig in input.lower() for sig in ["am","pm",":"]): #Format 2 - time until - TODO- improve parsing to allow numbers in any order and throw exception on repeats
            if not len(input_split)==1:
                raise IndexError
            input = input.replace("d","D").replace("y","Y").replace("w","W") #only uppercase these letters
            years = 0 if not "Y" in input else int(input.split("Y",1)[0])
            input = input.split("Y",1)[-1]
            months = 0 if not "M" in input else int(input.split("M",1)[0])
            input = input.split("M",1)[-1]
            weeks = 0 if not "W" in input else int(input.split("W",1)[0])
            input = input.split("W",1)[-1]
            days = 0 if not "D" in input else int(input.split("D",1)[0])
            input = input.split("D",1)[-1]
            hours = 0 if not "h" in input else int(input.split("h",1)[0])
            input = input.split("h",1)[-1] 
            minutes = 0 if not "m" in input else int(input.split("m",1)[0])
            input = input.split("m",1)[-1]
            seconds = 0 if not "s" in input else int(input.split("s",1)[0])
            input = input.split("s",1)[-1]

            now = datetime.now()
            time_day = datetime(now.year+years+int(math.floor((now.month+months-1)/12)),(now.month+months-1)%12+1,now.day,hour=now.hour,minute=now.minute,second=now.second)
            time_day = time_day + timedelta(days=days+weeks*7,hours=hours,minutes=minutes,seconds=seconds)

        elif ":" in input or "pm" in input or "am" in input or input.isnumeric(): #Format 3 - time without date - assumes today/tomorrow
            if recursive:
                raise IndexError #prevent infinite recursion
            recursive_time = self.text_to_datetime("today at "+input,recursive=True)
            if recursive_time < datetime.now():
                recursive_time = self.text_to_datetime("tomorrow at "+input,recursive=True)
            time_day = recursive_time

        else:
            raise ValueError

        #parse the 'at' for format 1 if it exists
        if len(input_split)==2:
            at_input = input_split[1].lower().replace(" am","am").replace(" pm","pm")
            use_pm = False
            use_am = False
            if at_input.endswith("am"):
                at_input = at_input.replace("am","")
                use_am = True
            elif at_input.endswith("pm"):
                at_input = at_input.replace("pm","")
                use_pm = True
            at_input = at_input.replace("noon","12:00").replace("bonky","16:00").replace("midnight","23:59:59")
            numbers_split = at_input.split(":")
            if len(numbers_split)<1 or len(numbers_split)>3: #sanity check
                raise ValueError
            hour = int(numbers_split[0])
            if use_am:
                if hour==12:
                    hour = 0
            elif use_pm:
                if hour!=12:
                    hour+=12
            minute = 0 if len(numbers_split)==1 else int(numbers_split[1])
            second = 0 if len(numbers_split)<=2 else int(numbers_split[2])
            time_day = time_day + timedelta(hours=hour,minutes=minute,seconds=second)
        return time_day

    async def cmd_remind(self, message, channel):
        """
        Usage:
            {command_prefix}remind [time] [message]

        Sets a reminder for [time]. When [time] is reached, the bot will send your message and ping you.
        If you mention other users in the reminder, the reminder will ping them as well. If the reminder was set in a DM channel with the bnuuy, a copy of the reminder will be sent to them.

        There are three accepted time formats:
            MM/DD/YYYY [at hh:mm:ss]
                - sets the clock for a specified date and time
                - the date may be replaced with "today" or "tomorrow"
                - the date may be replaced with "noon" or "midnight"
                - the part in brackets is optional. It defaults to midnight (00:00:00).
                - replace the letters with the appropriate number value.
                - two-digit years are also accepted (e.g. 1/1/20 refers to Jan 1, 2021)
                - the year input can be omitted. It will default to the current year or next year if the day has already past.
                - if AM or PM is not specified, 24-hour time will be used
                - you may also switch the order to "[time] on [date]".
            hh:mm:ss
                - same as previous format, but assumes the date is either today or tomorrow, whichever hasn't happened yet
                - IMPORTANT: be careful to specify "am" or "pm" to avoid accidentally setting a reminder for tomorrow morning instead of this afternoon.
            #Y#M#W#D#h#m#s 
                - sets the clock for a specified time duration after the current time
                - the letters stand for years, months, weeks, days, hours, minutes, seconds
                - replace the #-signs with numerical values, but keep the letters
                - any value may be omitted - will default to zero
                - numbers must be input in this order (for now)

        Usage Examples:
            {command_prefix}remind 9/12 bnuuy's birthday
            {command_prefix}remind 3/14/15 at 9:26:54 pi day
            {command_prefix}remind today at 21:30 commit war crimes
            {command_prefix}remind tomorrow at 21:30 commit more war crimes
            {command_prefix}remind 10m do homework
            {command_prefix}remind 3Y1W3d7h2s welcome back @everyone (will ping everyone)
        """
        #split up inputs into the parts we want
        #TODO - make this not a mess and allow message-less reminders
        try:
            message_content = message.content.strip().split(" ",1)[1]
            time_string = message_content.split(" ",1)[0]
            remind_message = message_content.split(" ",1)[1]
            if remind_message.startswith("at "):
                time_string += " at " + remind_message.split(" ",2)[1]
                remind_message = remind_message.split(" ",2)[2]
            elif remind_message.startswith("on "):
                time_string += " on " + remind_message.split(" ",2)[1]
                remind_message = remind_message.split(" ",2)[2]
            if remind_message.startswith("am ") or remind_message.startswith("pm "):
                time_string += remind_message[0:2]
                remind_message = remind_message.split(" ",1)[1]
        except IndexError:
            await channel.send("Sorry, I couldn't parse that. Make sure you entered all the necessary information.",delete_after=30)
            return

        #get datetime object
        try:
            remind_time = self.text_to_datetime(time_string)
        except ValueError:
            await channel.send("That time could not be parsed! Try '&help remind' for information on accepted input formats.", delete_after=30)
            return
        except IndexError:
            await channel.send("What you entered confuses and scares me. I cannot parse it.", delete_after=30)
            return
        if remind_time < datetime.now():
            await channel.send("Please specify a time that hasn't already happened. I cannot break causality (yet).",delete_after=30)
            return


        #set the reminder
        #print(remind_time.strftime("%YY%mM%dD%Hh%Mm%Ss")) #debug print
        self.set_reminder(remind_time, channel.id, message.author.id, remind_message)
        await message.add_reaction(random.choice("⏲⏰⏱🕰"))
        await message.add_reaction("✅")
        return

    def set_reminder(self, time, channelID, userID, text):
        """
        Adds a reminder to the reminders list
        time is a datetime object
        """
        #reminder_string = time.strftime("%m/%d/%Y at %H:%M:%S")+";"+str(channelID)+";"+str(userID)+";"+text
        reminder_dict = {"time":time.strftime("%m/%d/%Y at %H:%M:%S"), "channel_id": channelID, "user_id": userID, "text":text}
        with open(os.path.abspath("misc_data/reminders.txt"),"a") as f:
            f.write(json.dumps(reminder_dict)+"\n")
        if time < self.next_reminder: #check if this is the new next_reminder
            self.next_reminder = time

    async def cmd_reminders(self,message,channel):
        """
        Usage:
            {command_prefix}reminders
            {command_prefix}reminders [count]
            {command_prefix}reminders all

        Lists your currently active reminders (across all servers) in chronological order.
        Your most recently set reminder will be indicated with an asterisk (*).
        If no number is specified, defaults to 7. 'all' will list all reminders.
        Regardless of what you enter, the maximum number that will be displayed is 30.
        TODO - list reminder number
        """
        inputs = message.content.strip().split(" ",1)
        count=7
        if len(inputs)==2:
            if inputs[1].isnumeric():
                count = int(inputs[1])
                if count>30:
                    await channel.send("Sorry, that number is too big!",delete_after=30)
                    return
                elif count<1:
                    await channel.send("Sorry, that number is too small!",delete_after=30)
                    return
            elif inputs[1].lower() == "all":
                count = -1
            else:
                await channel.send("Sorry, I don't know what "+inputs[1]+" means.",delete_after=30)
                return
        elif len(inputs)>2:
            await channel.send("Sorry, I don't understand that.",delete_after=30)
            return

        with open(os.path.abspath("misc_data/reminders.txt"),"r") as f:
            lines = f.readlines()
        user_matches = []
        for line in lines:
            if not line.strip() or line.startswith("##"):
                continue
            dicty = json.loads(line.strip())
            if dicty["user_id"] == message.author.id:
                user_matches.append(dicty)
        if len(user_matches)==0:
            await channel.send("You do not have any reminders set at the moment",delete_after=60)
            return

        user_matches = sorted(user_matches, key=lambda date: (self.text_to_datetime(date["time"])-datetime.now()).total_seconds()) #Sort the list chronologically
        try:
            most_recent_reminder = self.get_recent_reminder(message.author.id)
        except ValueError:
            most_recent_reminder = None

        nowtime = datetime.now()
        today_string = nowtime.strftime("%m/%d/%Y")
        tomorrow_string = (nowtime+timedelta(days=1)).strftime("%m/%d/%Y")
        embed = discord.Embed(title = "Your Reminders:",color=discord.Color.from_rgb(252, 160, 131))
        i=0
        for match in user_matches:
            i += 1
            is_recent = False
            if match == most_recent_reminder:
                is_recent = True

            match["time"] = match["time"].replace(today_string,"Today").replace(tomorrow_string,"Tomorrow")
            embed.add_field(name="["+str(i)+("*] " if is_recent else "] ")+match["time"],value=match["text"],inline=False)
            if (i>=count and count>0) or i>=30:
                break
        embed.title = "Your Next "+str(i)+" Reminders:"
        embed.set_footer(text="Note: Each reminder will only be sent in the channel where you set them.")

        await channel.send(embed=embed,delete_after=120)
        return

    async def cmd_removereminder(self, message, channel):
        """
        Usage:
            {command_prefix}removereminder --> no input defaults to "recent"
            {command_prefix}removereminder [reminder number]
            {command_prefix}removereminder recent
            {command_prefix}removereminder next
            {command_prefix}removereminder last
            {command_prefix}removereminder clearall
            {command_prefix}removereminder [selection 1] [selection 2] [selection 3] ...

        [reminder number] --> Removes a reminder given the reminder number (the number in the {command_prefix}reminders list).
        recent --> removes your most recently set reminder (includes recently snoozed reminders).
        next --> removes your next upcoming reminder (reminder #1)
        last --> removes your last reminder chronologically. Note: if you have more than 30 reminders set, this will remove the 30th reminder (UNTESTED). 
        clearall --> removes ALL of your reminders.

        You may chain multiple inputs to remove several reminders at once. 
        Duplicate selections will only remove one reminder. e.g. specifying "next" and then "1" will only remove the first reminder, not the first two.
        
        You cannot remove reminders set by other users.
        Once a reminder is removed, it is gone forever!

        Aliases: removerm, rmvreminder, rmvrm, rmrm
        """
        #parse out the inputs
        inputs = message.content.strip().lower().split(" ")[1:]
        inputs = list(filter(lambda x:x, inputs))
        if len(inputs)==0:
            inputs.append("recent")
        inputs = list(set(inputs)) #remove duplicates

        #Get list of reminders for the user sorted chronologically -- literally just copy-pasted from the function before this one
        with open(os.path.abspath("misc_data/reminders.txt"),"r") as f:
            lines = f.readlines()
        user_matches = []
        for line in lines:
            if not line.strip() or line.startswith("##"):
                continue
            dicty = json.loads(line.strip())
            if dicty["user_id"] == message.author.id:
                user_matches.append(dicty)
        if len(user_matches)==0:
            await channel.send("You do not have any reminders set at the moment",delete_after=60)
            return
        user_matches = sorted(user_matches, key=lambda date: (self.text_to_datetime(date["time"])-datetime.now()).total_seconds()) #Sort the list chronologically

        #collect the reminders to be removed
        to_remove = []
        for input in inputs:
            if input == "next":
                to_remove.append(user_matches[0])
            elif input == "last":
                if len(user_matches)>30:
                    to_remove.append(user_matches[29])
                else:
                    to_remove.append(user_matches[-1])
            elif input=="recent":
                try:
                    to_remove.append(self.get_recent_reminder(message.author.id))
                except ValueError:
                    await channel.send("I'm sorry, but I couldn't find your most recent reminder. This may be a bug.\n(No reminders were removed)",delete_after=30) #Should never run
                    return
            elif input.isnumeric():
                try:
                    to_remove.append(user_matches[int(input)-1])
                except IndexError:
                    await channel.send("I'm sorry but this number is too large!: "+input+" (You don't have that many reminders set!)\n(No reminders were removed)",delete_after=30)
                    return
            elif input=="clearall":
                to_remove = user_matches
                break
            else:
                await channel.send("One of those inputs was invalid. Use `&help removereminder` if you need more information.\n(No reminders were removed)",delete_after=30)
                return

        #remove duplicates (can't cast to set because dicts aren't hashable or something)
        seen = set()
        new_l = []
        for d in to_remove:
            t = tuple(d.items())
            if t not in seen:
                seen.add(t)
                new_l.append(d)
        to_remove = new_l

        #Generate confirmation embed (Doing this first because self.remove_matching_reminders(...) messes with the passed-in list)
        output_string = ""
        if "clearall" in inputs:
            output_string="((all of them))"
        else:
            for rm in to_remove:
                output_string += "\n"
                output_string += (rm["text"][0:20]+"...") if len(rm["text"])>20 else rm["text"]
            output_string = output_string[1:] #cut out first newline
        embed = discord.Embed(title = "Removed the following reminders:",color=discord.Color.from_rgb(252, 160, 131))
        embed.add_field(name="_ _",value=output_string,inline=False)
        embed.set_footer(text="(Removed "+str(len(to_remove))+" reminder"+("s" if len(to_remove)>1 else "")+" in total)") 

        #finally try to remove the reminders
        try:
            self.remove_matching_reminders(to_remove)
        except ValueError:
            await channel.send("I'm sorry, but I couldn't find some of the reminders you listed.\n(No reminders were removed)",delete_after=30)
            return
        
        #Send the confirmation embed if everything worked out
        await channel.send(embed=embed, delete_after=120)
        return
            
    def remove_matching_reminders(self, reminder_dicts):
        """
        Removes the first occurance of every dict in reminder_dicts from reminders.txt.
        Raises ValueError and removes nothing if no matches found for a particular input.
        NOTE - This will remove stuff from the passed-in list.
        TODO - Make that ^ not happen.
        """
        with open(os.path.abspath("misc_data/reminders.txt"),"r") as f:
            lines = f.readlines()
        for i in range(len(lines)-1,-1,-1):
            dicty = json.loads(lines[i].strip())
            if any(dicty==rd for rd in reminder_dicts):
                lines.pop(i)
                reminder_dicts.remove(dicty)
        if len(reminder_dicts)>0:
            raise ValueError
        with open(os.path.abspath("misc_data/reminders.txt"),"w") as f:
            f.writelines(lines)

    def get_recent_reminder(self, user_id):
        """
        Retrieves the dict of the lastest reminder with matching "user_id".
        Raises ValueError if no matches found.
        """
        with open(os.path.abspath("misc_data/reminders.txt"),"r") as f:
            lines = f.readlines()
        match_found = False
        for i in range(len(lines)-1,-1,-1):
            dicty = json.loads(lines[i].strip())
            if str(dicty["user_id"])==str(user_id):
                return dicty
        raise ValueError


    #used for reminders, but could be used elsewhere as well
    #extracts all user mentions from a string and returns them as a list of strings - ex: ["<@!######>","<@!######>"]
    #does not change the order or remove duplicates
    def extract_mentions(self, input):
        mentions = []
        while "<@" in input:
            input = input.split("<@",1)[1]
            if input.startswith("!"):
                input = input[1:len(input)]
            if not ">" in input or len(input)<19:
                return
            #print(input[0:18])
            if self.is_valid_id(input[0:18]) and input[18]==">":
                mentions.append("<@!"+input[0:19])
                input = input.split(">",1)[1]
        return mentions
        

    #Checks through all reminders and sends them out when they expire
    async def check_reminders(self):
        now = datetime.now()
        try:
            with open(os.path.abspath("misc_data/reminders.txt"),"r") as f:
                lines = f.readlines()
        except FileNotFoundError:
            with open(os.path.abspath("misc_data/reminders.txt"),"w") as f:
                print("Creating reminders.txt")
            return

        update_reminders = False #determines whether the remind.txt file should be rewritten afterwards
        for i in range(len(lines)-1,-1,-1):
            line = lines[i].strip()
            if line.startswith("##"):
                continue
            if not line: #idk where this whitespace keeps coming from, but this should clear it
                lines.pop(i)
                update_reminders = True
                continue

            line_dict = json.loads(line)
            time = self.text_to_datetime(line_dict["time"])
            if not now>time: #not time yet
                continue

            #====SEND REMINDER(S)====#

            try:
                channel = self.get_channel(line_dict["channel_id"])
                user = self.get_user(line_dict["user_id"])
                channel = channel if channel else await self.fetch_channel(line_dict["channel_id"])
                user = user if user else await self.fetch_user(line_dict["user_id"])
                if not channel or not user:
                    raise AttributeError
            except AttributeError:
                print("Error retrieving channel or user for reminder: "+line)
                #raise
                lines.pop(i)
                update_reminders=True
                continue

            remind_message = line_dict["text"]

            #TODO - regex to sub @e->@everyone and @h->@here if the user has appropriate server priviledges
            #TODO - if user does not have appropriate permissions, make sure we don't @everyone or @here

            remind_mentions = self.extract_mentions(remind_message)
            try:
                remind_mentions = list(set(remind_mentions)) #temporarily casting to set removes any duplicates

            except TypeError: #not sure why, but this happens if sometimes if someone intentionally types an invalid mention
                #print(":(")
                remind_mentions = []
            if user.mention.replace("<@","<@!") in remind_mentions: #remove original user if they mentioned themselves
                remind_mentions.remove(user.mention.replace("<@","<@!"))

            lines.pop(i) #remove the reminder from the list
            update_reminders = True

            embed = discord.Embed(title = "Time: "+time.strftime("%c"),color=discord.Color.from_rgb(255, 128, 43))
            if len(remind_message)<200:
                embed.add_field(name=remind_message, value = "_ _")
            else:
                embed.add_field(name="_ _", value = remind_message)
            embed.set_footer(text="Use reactions to snooze (9 min) or delete this reminder.")

            output_text = "Reminder for "+user.mention
            if len(remind_mentions)>0:
                output_text += " as well as"
            for m in remind_mentions:
                output_text += " "+m
            if "@everyone" in remind_message:
                output_text += " oh yeah and @everyone"
            elif "@here" in remind_message:
                output_text += " oh yeah and @here"

            mymessage = await channel.send(content=output_text+"_ _",embed=embed)
            await mymessage.add_reaction("💤")
            await mymessage.add_reaction("❌")

            #try to send DM reminders to the other mentioned users if necessary
            if isinstance(channel, discord.abc.PrivateChannel) and len(remind_mentions)>0: 
                count = 0
                failcount = 0 #currently unused
                for m in remind_mentions:
                    try:
                        mentioned_user = self.get_user(int(m.replace("<@","").replace("!","").replace(">",""))) #try doing it without an API call first
                        if not mentioned_user:
                            mentioned_user = await self.fetch_user(int(m.replace("<@","").replace("!","").replace(">","")))
                        dm_channel = await mentioned_user.create_dm()
                        await dm_channel.send("A DM reminder for "+user.name+" has mentioned you. Here is a copy of their reminder. (Responding to this message will do nothing)_ _",embed=embed)
                        count += 1
                    except:
                        failcount += 1
                        pass
                await channel.send("Your reminder has been forwarded to "+str(count)+" mentioned user(s).")

        #rewrite reminders.txt file if anything has been changed
        if update_reminders:
            with open(os.path.abspath("misc_data/reminders.txt"),"w") as f:
                f.writelines(lines)
            self.next_reminder = self.get_next_reminder() #this could be optimized by finding the min_time in the pass above, but that isn't really necessary

        return

    #gives a random text channel
    def get_random_channel(self):
        channel = None
        while not isinstance(channel, discord.TextChannel):
            channel = random.choice(random.choice(self.guilds).channels)
        return channel

    #gives a random text channel that is also not in the unfun_guilds list
    def get_random_fun_channel(self):
        channel = None
        if len(self.unfun_guilds)>=len(self.guilds): #prevent an infinite loop
            return None
        while not isinstance(channel, discord.TextChannel) and not channel in self.unfun_guilds:
            channel = random.choice(random.choice(self.guilds).channels)
        return channel

    #checks if it's time to poop and poops if necessary
    async def dumpy_check(self):
        if random.random()<self.poop_probability:
            if len(self.unfun_guilds)+1>=len(self.guilds): #there are no eligible guilds to shit in (the +1 is to exclude the dev guild)
                return
            channel = self.get_random_fun_channel()
            if not channel: #just being safe
                return
            while channel.guild.id==886765649018380349: #keep the dev server clean
                channel = self.get_random_fun_channel()
            mymessage = await channel.send("💩")
            await mymessage.add_reaction("🔫")

    @dev_only
    async def cmd_dumpy(self, channel):
        """
        Usage:
            {command_prefix}dumpy

        Makes the rabbit take a dump. Used for debugging purposes only.
        """
        mymessage = await channel.send("💩")
        await mymessage.add_reaction("🔫")
        return

    #scans the reminders.txt file and retrieves the datetime object of the reminder scheduled to occur next. 
    #If there are no reminders, it will return None.
    def get_next_reminder(self):
        try:
            with open(os.path.abspath("misc_data/reminders.txt"),"r") as f:
                lines = f.readlines()
        except FileNotFoundError:
            return

        min_time = None

        for line in lines:
            if not line.strip() or line.startswith("##"):
                continue
            line_dict = json.loads(line.strip())
            line_time = self.text_to_datetime(line_dict["time"])
            if not min_time or min_time > line_time:
                min_time = line_time
        return min_time

    #Tasks that are run every second
    @tasks.loop(seconds=1)
    async def second_loop(self):
        #start_time = time.time()
        if datetime.now()>self.next_reminder:
            await self.check_reminders()

        await self.dumpy_check() #you gotta do what you gotta do

        if not self.scorekeeper.is_saved:
            self.scorekeeper.save()

        #if datetime.now().second == 0: #stuff to do every minute
        #    pass
        #print("LOOP!: "+str(round(time.time()-start_time,4))+" s")


    #used to send a message to the bot owner in case of debugging or emergency
    async def alert_owner(self, text):
        print("[Notifying Owner]: "+text)
        owner = self.get_user(self.config.owner_id)
        owner = owner if owner else await self.fetch_user(self.config.owner_id)
        dm = await owner.create_dm()
        await dm.send("ALERT FOR OWNER!\n"+text)
        return

    #same as alert_owner, but this time alerts all possible developers
    async def alert_devs(self, text):
        print("[Notifying Developers]: "+text)
        for devid in self.config.dev_ids:
            try:
                dev = self.get_user(int(devid))
                dev = dev if dev else await self.fetch_user(int(devid))
                dm = await dev()
                await dm.send("ALERT FOR DEVELOPER!\n"+text)
            except:
                continue
        return


    async def cmd_whisper(self, message, channel):
        """
        Usage:
            {command_prefix}whisper [user id] [message]

        Sends an anonymous whisper to the designated user. The user may reply to the whisper, but your identity will be hidden from them.
        """
        try:
            inputs = message.content.split(" ",2)
            inputs[1] = inputs[1].strip()
        except IndexError:
            await channel.send("You didn't include enough input fields. Try using `{}help whisper` for more information.".format(self.config.command_prefix),delete_after = 60)
            return
        try:
            sender = message.author
            if not self.is_valid_id(inputs[1].strip()):
                raise ValueError("Invalid id input")
            receiver = self.get_user(int(inputs[1]))
            receiver = receiver if receiver else await self.fetch_user(int(inputs[1]))
            if not receiver:
                raise TypeError("Cannot find that user")
            elif receiver.bot:
                raise TypeError("Cannot whisper to bots")
            if len(inputs[2])>1000: #It cannot go much higher than this
                raise IndexError("Whisper text is too long")

            await self.send_whisper(sender, receiver, inputs[2])
            await message.add_reaction("✉️")
            await message.add_reaction("✅")
        except ValueError:
            await channel.send("That was not a valid user ID.",delete_after=60)
            return
        except TypeError:
            await channel.send("Sorry, but I could not find that person (you either mistyped or they are a bot).",delete_after=60)
            return
        except IndexError:
            await channel.send("Sorry, but that whisper is too long.",delete_after=60)
            return
        except:
            print("WHISPER EXCEPTION:")
            await channel.send("Sorry, but I could not send a message to that user. Something went horribly wrong.",delete_after=60)
            raise #TODO - remove this when we know it's stable
            return
        return

    #if message replies to a whisper, returns the log dict referencing the whisper (String). Otherwise returns None.
    def replies_to_whisper(self, message):
        if not (message.reference and message.reference.resolved):
            return None
        reference_id = message.reference.message_id
        with open(os.path.abspath("misc_data/whispers.txt"),"r") as f:
            lines = f.readlines()
        for line in lines:
            if not line.strip() or line.startswith("##"):
                continue
            dicty = json.loads(line.strip())
            if dicty["message_id"] == reference_id:
                return dicty
        return None

    #if message is a whisper, returns the log dict referencing the whisper (String). Otherwise returns None.
    def is_whisper(self, message):
        reference_id = message.id
        with open(os.path.abspath("misc_data/whispers.txt"),"r") as f:
            lines = f.readlines()
        for line in lines:
            if not line.strip() or line.startswith("##"):
                continue
            dicty = json.loads(line)
            if dicty["message_id"] == reference_id:
                return dicty
        return None


    #sends a new whisper to a person and logs the whisper's data. Format: [senderID;receiverID;whisperMessageID;replyToMessageID/None;message_content]
    #sender, receiver -- User objects, the sender and receiver
    #text -- string, the message to send as a whisper
    #replyToMessage -- Message object, the message object of the original whisper. Will be None if the whisper is not a reply to another whisper.
    #returns the message object of the sent whisper
    async def send_whisper(self, sender, receiver, text, replyToMessage=None):

        dm_channel = await receiver.create_dm()

        if not replyToMessage:
            if len(text)<200:
                embed = discord.Embed(title=text,color=discord.Color.from_rgb(179, 179, 179))
            else:
                embed = discord.Embed(title="_ _",color=discord.Color.from_rgb(179, 179, 179))
                embed.add_field(name="_ _",value=text)
            embed.set_author(name="Whisper:",icon_url="https://cdn.discordapp.com/attachments/886765649827872820/926492273745723402/PsPsPsPs.jpeg")
            #embed.add_field(name = text, value = "(This text was sent to you anonymously)")
            embed.set_footer(text="This message was sent to you anonymously. Reply to this message to respond to the sender. (Note: All whispers are recorded, so behave)")
        else:
            reference_dict = self.is_whisper(replyToMessage)
            sample_text = reference_dict["message"]
            if len(text)<200:
                embed = discord.Embed(title=text,color=discord.Color.from_rgb(130, 130, 130))
            else:
                embed = discord.Embed(title="_ _",color=discord.Color.from_rgb(130, 130, 130))
                embed.add_field(name="_ _",value=text,inline=False)
            embed.set_author(name="Message:",icon_url="https://cdn.discordapp.com/attachments/886765649827872820/926492212970287195/PsPsPsPs2.jpeg")
            embed.add_field(name = "\n(In response to your previous whisper:)", value = sample_text[0:10]+"..." if len(sample_text)>10 else sample_text, inline=False)
            embed.set_footer(text="Reply to this message to respond to the sender. (Note: All whispers are logged, so behave)")

        mymessage = await dm_channel.send("Pspspspsps!",embed=embed)

        #Log the whisper
        log_dict = {"sender":sender.id, "receiver":receiver.id, "message_id":mymessage.id, "reply_id":"None" if not replyToMessage else replyToMessage.id, "message":text}
        with open(os.path.abspath("misc_data/whispers.txt"),"a") as f:
            f.write(json.dumps(log_dict)+"\n")
        return mymessage


    def update_horse_plinko(self, board):
        nohorse = True
        newboard=list(board) #this actually does nothing, so ill just iterate in reverse
        for i in range(len(board)-1,-1,-1):
            line = board[i]
            for j in range(len(line)-1,-1,-1):
                if not board[i][j]==2: #ignore non-horses
                    continue
                #---horse can be assumed after this point---
                nohorse=False
                if i==len(board)-1: #horse dies at the bottom
                    newboard[i][j]=0
                    continue
                #---not on bottom row can be assumed after this point---
                if board[i+1][j]==0: #horse over empty space - drops down
                    newboard[i+1][j]=2
                    newboard[i][j]=0
                elif board[i+1][j]==1: #horse above peg - do peg logic
                    left = True if (j>0 and board[i+1][j-1]==0) else False              #horse can fall left
                    right = True if (j<len(line)-1 and board[i+1][j+1]==0) else False   #horse can fall right
                    if left and right: #if both, pick one
                        chance = random.choice([True, False])
                        left,right = chance, not chance
                    if left and not right:
                        newboard[i][j]=0
                        newboard[i+1][j-1]=2
                    elif right and not left:
                        newboard[i][j]=0
                        newboard[i+1][j+1]=2

        return None if nohorse else newboard #signal that all horses are dead by returning None


    async def cmd_plinko(self, message, channel):
        """
        Usage:
            {command_prefix}plinko
            {command_prefix}plinko [horse emoji] [peg emoji] [fire emoji] [title text]

        does horse plinko
        optionally add your favorite emojis to plinko them too
        """
        #boolean for if an input is valid for horse plinko. Works for single characters, emojis, and guild custom emojis.
        def is_valid_plinko_emoji(text):
            return len(text)==1 or (text.startswith(":") and text.endswith(":")) or (text.startswith("<") and text.endswith(">") and ":" in text)

        #converts numerical plinko board into a text message with selected emojis
        def board_to_text(board, horse_emoji=":racehorse:", peg_emoji=":white_circle:", fire_emoji=":fire:", title="HORSE PLINKO"):
            output = " ==="+title.upper()+"===\n "
            for line in board:
                for n in line:
                    output+="     _ _" if n==0 else (peg_emoji if n==1 else horse_emoji)
                output+="\n_ _"
            output+" "
            for i in range(7):
                output+=fire_emoji
            return output

        def update_horse_plinko(board):
            nohorse = True
            newboard=list(board) #this actually does nothing, so ill just iterate in reverse
            for i in range(len(board)-1,-1,-1):
                line = board[i]
                for j in range(len(line)-1,-1,-1):
                    if not board[i][j]==2: #ignore non-horses
                        continue
                    #---horse can be assumed after this point---
                    nohorse=False
                    if i==len(board)-1: #horse dies at the bottom
                        newboard[i][j]=0
                        continue
                    #---not on bottom row can be assumed after this point---
                    if board[i+1][j]==0: #horse over empty space - drops down
                        newboard[i+1][j]=2
                        newboard[i][j]=0
                    elif board[i+1][j]==1: #horse above peg - do peg logic
                        left = True if (j>0 and board[i+1][j-1]==0) else False              #horse can fall left
                        right = True if (j<len(line)-1 and board[i+1][j+1]==0) else False   #horse can fall right
                        if left and right: #if both, pick one
                            chance = random.choice([True, False])
                            left,right = chance, not chance
                        if left and not right:
                            newboard[i][j]=0
                            newboard[i+1][j-1]=2
                        elif right and not left:
                            newboard[i][j]=0
                            newboard[i+1][j+1]=2
            return None if nohorse else newboard #signal that all horses are dead by returning None


        info = list(filter(lambda x: x, message.content.split(" ")))

        #print(info)
        horse_emoji = ":racehorse:"
        peg_emoji = ":white_circle:"
        fire_emoji = ":fire:"
        title = "HORSE PLINKO"
        all_parsed = False
        if len(info)>1 and is_valid_plinko_emoji(info[1]):
            horse_emoji = info[1]
        elif len(info)>1:
            title=" ".join(info[1:])
            all_parsed=True
        if len(info)>2 and is_valid_plinko_emoji(info[2]) and not all_parsed:
            peg_emoji = info[2]
        elif len(info)>2 and not all_parsed:
            title=" ".join(info[2:])
            all_parsed=True
        if len(info)>3 and is_valid_plinko_emoji(info[3]) and not all_parsed:
            fire_emoji = info[3]
        elif len(info)>2 and not all_parsed:
            title=" ".join(info[3:])
            all_parsed=True
        if len(info)>4 and not all_parsed:
            title = " ".join(info[4:])

        #0-space, 1-peg, 2-horse
        start = random.choice([[2,0,0,0,0,0,0],[0,2,0,0,0,0,0],[0,0,2,0,0,0,0],[0,0,0,2,0,0,0],[0,0,0,0,2,0,0],[0,0,0,0,0,2,0],[0,0,0,0,0,0,2],[2,0,2,0,2,0,2],[0,2,0,2,0,2,0],[2,2,2,2,2,2,2]])
        board = [start,[0,1,0,1,0,1,0],[0,0,0,0,0,0,0],[1,0,1,0,1,0,1],[0,0,0,0,0,0,0],[0,1,0,1,0,1,0],[0,0,0,0,0,0,0],[1,0,1,0,1,0,1],[0,0,0,0,0,0,0]]
        mymessage = await channel.send(board_to_text(board, horse_emoji=horse_emoji, peg_emoji=peg_emoji, fire_emoji=fire_emoji, title=title))
        while board:
            try:
                await mymessage.edit(content=board_to_text(board, horse_emoji=horse_emoji, peg_emoji=peg_emoji, fire_emoji=fire_emoji, title=title))
            except discord.errors.NotFound: #the message was deleted or something 
                return
            await asyncio.sleep(1)
            board = update_horse_plinko(board)
        await asyncio.sleep(5)
        await mymessage.delete()
        return

    @dev_only
    async def cmd_weather(self, message, channel):
        """
        Usage:
            {command_prefix}weather
            {command_prefix}weather [location name]
        Gives you the current weather. Defaults to Westford if no location is specified.
        TODO - give more specific location info in output. Currently, it may give you a different place with the same name and you'll never know.
        TODO - more data and stuff idk
        """
        mymessage = await channel.send("Fetching weather...")
        place = "Westford"
        if len(message.content.strip().split(" "))>1:
            place = message.content.split(" ")[1]
        weather_info = (await self.run_cli("curl wttr.in/"+place+"?0T"))[0] #TODO - change output format to get other cool stuff
        #print(weather_info)
        title = weather_info.split("\n",1)[0] #it's pretty safe to assume there will be a \n, at least for now
        weather = weather_info.split("\n",1)[1]
        try:
            embed = discord.Embed()
            embed.add_field(name=title, value = "```"+weather+"```")
            embed.set_footer(text="(sourced from wttr.in)")
            await mymessage.edit(content=None,embed=embed)
        except: #happens when the input location is bad, causing wttr.in to default to Oymyakon, creating an embed that exceeds discord's character limit.
            await channel.send("Sorry, something went wrong.",delete_after=60)
        return
        

    @dev_only
    async def cmd_archive(self, message, channel):
        """
        Usage:
            {command_prefix}archive

        Saves a snapshot of the current channel into the bnuuy's archive.
        Does not save images or attachments, but does save their urls.
        This command may take several minutes to finish running.
        """    
        await message.add_reaction("💬")
        start_time = time.time()
        try:
            print("ARCHIVING HISTORY FOR: "+str(channel.id))
            lines = []
            lines.append("##======================================================##\n")
            lines.append("##Archive for "+str(channel.id)+" AKA "+channel.name+"\n")
            lines.append("##Guild: "+str(channel.guild.id)+" AKA "+channel.guild.name+"\n")
            lines.append("##Date: "+datetime.now().strftime("%m/%d/%Y at %H:%M:%S")+"\n")
            lines.append("##======================================================##\n##\n")
            lines.append("##MEMBERS:\n")
            for m in channel.members:
                mdict = {}
                mdict["name"] = m.name
                mdict["nick"] = m.nick
                mdict["id"] = str(m.id)
                mdict["avatar_url"] = str(m.avatar_url)
                lines.append(json.dumps(mdict)+"\n")

            lines.append("##\n##MESSAGES:\n")
            async for m in channel.history(limit=None,oldest_first=True):
                mdict = {}
                mdict["date"] = m.created_at.strftime("%m/%d/%Y at %H:%M:%S")
                mdict["author"] = str(m.author.id)
                mdict["id"] = str(m.id)
                mdict["content"] = m.content
                if len(m.attachments)>0:
                    mdict["attachments"] = [att.url for att in m.attachments]
                if len(m.embeds)>0:
                    mdict["embeds"] = [str(embed.to_dict()) for embed in m.embeds]
                if len(m.reactions)>0:
                    mdict["reactions"] = [str(r.emoji)+";"+str(r.count) for r in m.reactions]
                if m.reference and m.reference.resolved:
                    mdict["reference"] = str(m.reference.message_id)
                lines.append(json.dumps(mdict)+"\n")
            with open(os.path.abspath("channel_archives/"+str(channel.id)+".txt"),"w") as f:
                f.writelines(lines)
            print("DONE")
        except:
            print("ERROR WHILE ARCHIVING")
            await message.add_reaction("‼️")
            raise
            return
        await message.clear_reactions()
        await message.add_reaction("✅")
        await channel.send("Channel archived in "+str(round(time.time()-start_time,2))+" seconds")
  
        return

    async def cmd_emojify(self, message, channel):
        """
        Usage:
            {command_prefix}emojify [text]
            {command_prefix}emojify (replies to another message)

        Emojifies the input text.
        If you use the command as a reply to another message, it will emojify the text of that message instead.
        Generation algorithm is copied straight from https://github.com/Kevinpgalligan/EmojipastaBot
        The emoji list is also from there, but it's been manually trimmed down slightly.
        The emojis are trained from reddit posts - so they're kinda erratic.

        Experimental:
        If you use the command as a reply to another message that contains just images, it will emojify any text that was parsed from the images 
        Note: Image parsed text only works if the images were sent in the last day, and the images were not sent as urls. 
        You may need to wait a minute for the rabbit to read the image.
        """

        if not (message.reference and message.reference.resolved): #parse the input text regularly
            try:
                message_text = message.content.strip().split(" ",1)[1]
            except IndexError:
                await channel.send("Give me some text to emojify!",delete_after=30)
                return

        else: #use text from a replied message
            old_message = message.reference.cached_message
            old_message = old_message if old_message else (await channel.fetch_message(message.reference.message_id))
            message_text = old_message.content
            if not message_text: #try to find attachments that may have been parsed
                file_list = os.listdir(os.path.abspath("image_cache"))
                cache_file = "out"+str(old_message.id)+".txt"
                if cache_file in file_list:
                    with open(os.path.abspath("image_cache/"+cache_file),"r") as f:
                        message_text += "".join(f.readlines())
                else:
                    if len(old_message.attachments)>0:
                        await channel.send("I couldn't find anything to emojify.\nIf you're trying to emojify text from an image, make sure the image was sent *today* (and not by a bot).",delete_after=30)
                    else:
                        await channel.send("I couldn't find anything to emojify.",delete_after=30)
                    return


        emojified_text = EmojipastaGenerator.of_default_mappings().generate_emojipasta(message_text)
        if not emojified_text.strip():
            await channel.send("I couldn't find any text to emojify, sorry.", delete_after=30)
            return
        splits = self.split_long_text(emojified_text)
        try:
            for split in splits:
                await channel.send(split)
        except discord.errors.HTTPException:
            await channel.send("Something went wrong. The emojified text was probably too long to send through discord. Try breaking it up into several smaller messages for me.",delete_after=30)
        return

    @dev_only
    async def cmd_reloadscores(self, message=None):
        """
        Usage:
            {command_prefix}reloadscores

        Force-reloads all scoreboards from disk.
        """
        self.scorekeeper.load()
        if message:
            await message.add_reaction("✅")
        return

    @dev_only
    async def cmd_editscore(self, message, channel):
        """
        Usage:
            {command_prefix}editscore [user id] [scoreboard] [number]
            {command_prefix}editscore [user id] [scoreboard] [number] inc
            {command_prefix}editscore self [scoreboard] [number]

        Manually edits the specified scoreboard value for a user. 
        Including "inc" at the end will increment the existing score by the specified value. Otherwise the previous score will be overwritten.
        Make sure you spell the scoreboard name correctly or it will make a new scoreboard file.
        """
        #await self.cmd_reloadscores()
        info = list(filter(lambda x: x, message.content.lower().split(" ")))
        info[1] = str(message.author.id) if info[1]=="self" else info[1]
        increment = info[-1].startswith("inc")
        if not self.is_valid_id(info[1]):
            await channel.send("That's not a valid user id",delete_after=30)
            return
        try:
            id = int(info[1])
            value = info[3]
            if value.isdigit():
                value = int(info[3]) #cast to int if possible
            else:
                try:
                    value = float(value) #cast to float if possible
                except:
                    pass
        except:
            await channel.send("Those inputs couldn't be parsed, sorry", delete_after=30)
            return
        old_score = self.getScoreboard(id, info[2])
        try:
            new_score = self.changeScoreboard(id, info[2], value=value, increment=increment)
        except TypeError:
            await channel.send("That score could not be changed due to incompatable incrementables. (old score type: "+str(type(old_score))+" )")
            return
        await channel.send("The user's "+info[2]+" score has been changed from "+str(old_score)+" to "+str(new_score) + "  (type : "+str(type(new_score))+" )")
        return

    @dev_only
    async def cmd_createwebhook(self, message, channel):
        """
        Usage:
            {command_prefix}createwebhook
            {command_prefix}createwebhook [name]
            {command_prefix}createwebhook [name] [avatar url] - CURRENTLY BROKEN

        Creates a webhook for the current channel with name and avatar. Default name is "Default Name". Default avatar is None.
        There's also currently no way to remove the webhooks unless you're an admin, so don't use this too often.
        """
        split = message.content.strip().split(" ")
        name = "DefaultName" if len(split)<=1 else split[1]
        avatar = None if len(split)<=2 else split[2]
        if len(split)>3:
            await channel.send("That wasn't a valid input",delete_after=30)
            return
        try:
            #print(avatar)
            hook = await channel.create_webhook(name=name, avatar=str.encode(avatar) if avatar else None, reason = "Because I said so.")
        except:
            await channel.send("There was a problem creating the webhook :( (avatar_url input is currently broken btw)")
            raise
        await channel.send("The url for the new webhook is: "+hook.url)
        return

    #Splits a very long string into smaller strings with lengths less than (not equal to, I think) MAX_LEN
    #will try to split the text along line breaks and then along spaces when possible
    #returns a list of these shorter strings
    def split_long_text(self, input, MAX_LEN=1990):
        output = []
        shorts = input.split("\n") #split into newlines

        #break the text into progressively smaller chunks as necessary
        for i in range(len(shorts)-1,-1,-1):
            shorts[i]=shorts[i]+("\n" if i<len(shorts)-1 else "") #add the newline back
            short = shorts[i]

            if len(short)>=MAX_LEN: #regular short is too long - break into space-separated "shorters"
                shorts.pop(i)
                shorters = short.split(" ")
                for j in range(len(shorters)-1,-1,-1):
                    shorters[j] = shorters[j]+(" " if j<len(shorters)-1 else "") #add the space back
                    shorter = shorters[j]

                    if len(shorter)>=MAX_LEN: #shorter is too long - break into individual characters
                        for k in range(len(shorter)-1,-1,-1):
                            shorts.insert(i,shorter[k])
                    else:
                        shorts.insert(i,shorter)


        #now recombine smaller chunks where possible
        current = ""
        for short in shorts:
            if len(current)+len(short)+2 >= MAX_LEN:
                output.append(current)
                current = short
            else:
                current += short
        output.append(current) #don't forgor the last bit

        return output
            


    @dev_only
    async def cmd_longtest(self, message, channel):
        """
        Usage:
            {command_prefix}longtest [number]

        Used to test long-text breakup function. Generates a very long string of random characters and line breaks. 
        [number] is the number of characters to use. Defaults to 3000. DO NOT set higher than 20,000, this causes the bot to freeze.
        """
        number = 3000
        input = message.content.strip().split(" ")
        if len(input)>1:
            number = int(input[1])
        characters = "abcdefghijklmnopqrstuvwxyz:;./"
        output = ""
        for i in range(number):

            if random.random()<0.000037:
                output+=""#"\n"
            else:
                output += random.choice(characters)
        shorts = self.split_long_text(output)
        for short in shorts:
            print(short)
            print(len(short))
            print("\n\n\n")
            await channel.send(short)
        return

    #sets the daily wordle word to a new random word. Uses a seed based on the day to ensure the word doesn't change after a reboot.
    def set_secret_word(self):
        seed_number = (datetime.now()-datetime(day=1, month=1, year=1)).days #ensures the word doesn't change when bot restarts
        with open(os.path.abspath("misc_data/wordles.txt"),"r") as f:
            random.seed(seed_number)
            word = random.choice(f.readlines())
        self.secret_word = word.strip()
        #print("SECRET WORD:  "+word)

    #checks an input guess against the daily secret word
    #returns a string of emojis representing the guess's closeness to the word
    def check_wordle_word(self, guess):
        word = self.secret_word

        output_blocks = ["#" for i in range(len(guess))] #filler spaces

        #pass 1 - check for green squares
        for i in range(min(len(word),len(guess))):
            letter = guess[i]
            if word[i]==letter:
                output_blocks[i]="G"
                word = word[:i]+"#"+word[i+1:] #prevent double-counting

        #pass 2 - check for other squares
        for i in range(len(guess)):
            letter = guess[i]
            if i>=len(self.secret_word):
                output_blocks[i]="N"
            elif output_blocks[i]=="G":
                continue
            elif not letter in word:
                output_blocks[i]="B"
            elif letter in word:
                output_blocks[i]="Y"
                word = word.replace(letter, "#", 1)

        #convert to emojis
        output = "".join(output_blocks)
        output = output.replace("N",":woman_gesturing_no:").replace("G",":green_square:").replace("B",":black_large_square:").replace("Y",":yellow_square:")
        if guess==self.secret_word:
            output+="   :100:"
        return output


    async def cmd_wordle(self, message, channel):
        """
        Usage:
            {command_prefix}wordle [guess]

        Submits a wordle guess for your current discord server. The secret word changes every day, and can have 5 or more letters.
        Once the wordle has been solved for one of your servers, you must wait until the next day to play again.
        The feedback is as follows:
        Black square - The letter is not found anywhere in the secret word
        Yellow square - The letter is present but in the wrong place
        Green square - The letter is present and in the correct place
        100 emoji - You have correctly guessed the word! Good job!
        Woman gesturing "no" - The secret word does not have that many characters.
        """
        input = list(filter(lambda x:x, message.content.strip().split(" ")))
        if len(input)<=1:
            await channel.send("You need to guess a word.",delete_after=15)
            return
        elif len(input)>2:
            await channel.send("Your guess can only be one word long.", delete_after=30)
            return
        guess = input[1].lower()
        if not guess.isalpha():
            await channel.send("The secret word will only contain letters from the modern English alphabet.", delete_after=30)
            return
        if self.getScoreboard(channel.guild.id, "wordle_finished")>0:
            await channel.send("Sorry, someone already guessed the secret word in this server today. Try again tomorrow. :heart:", delete_after=30)
            return
        elif self.getScoreboard(message.author.id, "wordle_finished")>0:
            await channel.send("Sorry, but the secret word was already guessed in another server you're in. Try again tomorrow. :wink:", delete_after=30)
            return

        feedback = self.check_wordle_word(guess)
        attempts = self.changeScoreboard(channel.guild.id, "wordle_attempts")

        await channel.send("("+str(attempts)+") "+guess+"\: "+feedback)
        if feedback.endswith(":100:"):
            await channel.send(message.author.mention+" GUESSED THE WORD! It took this server "+str(attempts)+" tries to guess the word! Good job! :heart:")
            self.changeScoreboard(message.author.id, "wordle_wins")
            self.changeScoreboard(channel.guild.id, "wordle_finished")
            for member in channel.guild.members:
                self.changeScoreboard(member.id, "wordle_finished")

        return


    @dev_only
    async def cmd_cleanwordlelist(self, message):
        """
        Usage:
            {command_prefix}cleanwordlelist
        Helper command.
        Cleans the wordle list and prunes any unusable words. These include words with non-alphabet characters (like hyphens) and words shorter than 5 characters.
        Only needs to be used once.
        IMPORTANT: If you restart the bot on the same day you use this command, it might change the daily secret word.
        """
        with open(os.path.abspath("misc_data/wordles.txt"),"r") as f:
            lines = f.readlines()
        print("Start: "+str(len(lines)))
        for i in range(len(lines)-1,-1,-1):
            word = lines[i].strip()
            if len(word)<5 or not word.isalpha():
                lines.pop(i)
            lines[i] = lines[i].lower()
        print("End: "+str(len(lines)))
        with open(os.path.abspath("misc_data/wordles.txt"),"w") as f:
            f.writelines(lines)
        print("DONE")
        return

    @owner_only
    async def cmd_lightoff(self, message):
        """
        Usage:
            {command_prefix}lightoff
        Turns off the red running indicator light for the raspi.
        """
        await self.run_cli("/home/pi/Extras/LightOff.sh")
        await message.add_reaction("✅")
        return

    @owner_only
    async def cmd_lighton(self, message):
        """
        Usage:
            {command_prefix}lighton
        Turns on the red running indicator light for the raspi.
        """
        await self.run_cli("/home/pi/Extras/LightOn.sh")
        await message.add_reaction("💡")
        return

    async def cmd_emotelist(self, message, channel):
        """
        Usage:
            {command_prefix}emotelist

        Gives you a list of all the custom emotes that are useable in the current server.
        Emotes are listed alphabetically.
        Emotes with asterisks (*) are only useable in the current server.
        """

        with open(os.path.abspath("emotes/default.txt"),'r') as f:
            lines = f.readlines()
        lines_guild=[] #in case the file is not found
        try:
            with open(os.path.abspath("emotes/{}.txt".format(channel.guild.id)),'r') as f:
                lines_guild = f.readlines() #guild-specific emotes list
        except:
            pass
        key_list = []
        for line in lines:
            if not line.strip() or line.startswith("##"):
                continue
            key_list.append(((line.split(";")[0]).split(",")[0]).replace("{emote}","").lower())
        for line in lines_guild:
            if not line.strip() or line.startswith("##"):
                continue
            key_list.append(((line.split(";")[0]).split(",")[0]).replace("{emote}","").lower()+"*")
        key_list.sort()
        await channel.send(" , ".join(key_list), delete_after=120)
        return

    async def cmd_addemote(self, message, channel):
        """
        Usage:
            {command_prefix}addemote [trigger] [url]
            {command_prefix}addemote [trigger1] [trigger2] [trigger3] ...
            {command_prefix}addemote [trigger] (message has attachment(s))
            {command_prefix}addemote [trigger] (message replies to a message with attachment(s))
            {command_prefix}addemote everywhere [trigger] [url]


        Adds a new emote to the server's emote list given a list of triggers and images. 
        If multiple images are provided, it will add all of them to the list (one will be chosen at random when the trigger word is sent)
        IMPORTANT - the bot does not actually save the image (it just saves the url), so do not delete the original image afterwards or the emote will not work properly.
        Including the keyword "everywhere" at the start of the command will make the emote useable in all bnuuy servers.
        """
        input = list(filter(lambda x: x, message.content.split(" ")))
        input.pop(0) #(this is just the command text)
        if len(input)==0:
            await channel.send("I'm not sure what you want me to do with this...\nAn empty command is useless", delete_after=20)
            return

        #print(input)

        emote_extensions = [".png",".jpg",".jpeg",".mov",".mp4",".gif",".webp"] #defines what a valid emote file is
        triggers = []
        emotes = []
        everywhere = False
        if input[0] == "everywhere":
            everywhere = True
            input.pop(0)

        #sort inputs into emotes and triggers
        for i in input:
            if any(i.endswith(ex) for ex in emote_extensions):
                emotes.append(i)
            else:
                triggers.append(i)

        #grab emotes from attachments
        for a in message.attachments:
            if any(a.url.endswith(ex) for ex in emote_extensions):
                emotes.append(a.url)

        #grab all possible emotes from the reference (if there is one)
        if message.reference and message.reference.resolved:
            ref = await channel.fetch_message(message.reference.message_id)
            for i in list(filter(lambda x: x, ref.content.split(" "))):
                if any(i.endswith(ex) for ex in emote_extensions):
                    emotes.append(i)
            for a in ref.attachments:
                if any(a.url.endswith(ex) for ex in emote_extensions):
                    emotes.append(a.url)

        emotes = list(set(emotes))
        triggers = list(set(triggers))

        if len(emotes)==0: #No emotes provided
            await channel.send("Sorry, but I couldn't see any valid emotes from what you've provided me.", delete_after=20)
            return
        if len(triggers)==0: #No triggers provided
            await channel.send("Sorry, but I couldn't see any valid triggers from what you've provided me.", delete_after=20)
            return

        for t in triggers: #Make sure all triggers are valid
            if ";" in t or "," in t or "{" in t or "}" in t or "##" in t or "\\" in t or ":" in t[1:-2]:
                await channel.send("Sorry, but one of your provided trigger words contains a reserved character. Commas, semicolons, and colons are not allowed (yet).",delete_after=20)
                return
            if len(t) <3: #<3
                await channel.send("Sorry, but one of your provided trigger words is too short. Please make them at least 5 characters long.",delete_after=20)
                return

        for t in emotes: #Make sure all emotes are valid
            if ";" in t or "," in t or "{" in t or "}" in t or "##" in t or "\\" in t:
                await channel.send("Sorry, but one of your provided emotes contains a reserved character. Commas and semicolons are not allowed (yet).",delete_after=20)
                return

        for i in range(len(emotes)): #Do the embed fix for all emotes
            emotes[i] = emotes[i].replace("media.discordapp.net","cdn.discordapp.com")

        for i in range(len(triggers)): #Add the leading and trailing colons if not already present
            t = triggers[i]
            if not t.startswith(":"):
                t = ":"+t
            if not t.endswith(":"):
                t = t+":"
            triggers[i]=t

        line = "{emote},".join(triggers)+"{emote};"+",".join(emotes)+"\n"

        if everywhere:
            with open(os.path.abspath("emotes/default.txt".format(channel.guild.id)),'a') as f:
                f.write(line)
        else:
            with open(os.path.abspath("emotes/{}.txt".format(channel.guild.id)),'a') as f:
                f.write(line)

        await message.add_reaction("✅")
        return
    
    @dev_only
    async def cmd_convertunits(self, message, channel):
        """
        Usage:
            {command_prefix}convertunits [input1] to [input2]
            {command_prefix}convertunits "[input1]" "[input2]"

        Aliases:
            {command_prefix}convert
            {command_prefix}units

        Converts the units of measurement given by [input1] into [input2]. 
        This command is just a wrapper for the GNUUnits program, which can do a lot of very cool stuff.
        Use the second format (with quotes around the inputs) if [input1] contain the word " to ". This way the parser does not get confused.
        """
        #parse inputs - I may be using strip() a little excessively
        try:
            input = message.content.strip()
            input = input.split(" ",1)[1] #remove command string

            if input.startswith("\""): #Format 2
                input_split = input.split("\"")
                input_split = list(filter(lambda x:x.strip(), input_split))
                input1 = input_split[0].strip()
                input2 = input_split[1].strip()

            elif " to " in input: #Format 1
                input_split = input.split(" to ",1)
                input1 = input_split[0].strip()
                input2 = input_split[1].strip()
            else:
                await channel.send("Sorry, that input format is invalid. Use &help convertunits to see the proper input format.",delete_after=60)
                return
        except:
            await channel.send("Sorry, I could not parse that input. Use &help convertunits to see the proper input format.",delete_after=60)
            return

        #plug inputs into GNU Units and send results
        command = "units \""+input1+"\" \""+input2+"\""
        output = await self.run_cli(command)

        if output[1].strip(): #error occurred
            await channel.send("There was a problem running those units. See the GNU Units documentation for help.",delete_after=60)

        else: #no errors - send converted units and reciprocal in case someone needs it
            if output[0].strip().startswith("*"):
                output_split = output[0].split("\n")
                result = output_split[0].strip()[1:]
                reciprocal = output_split[1].strip()[1:]
                output_string = "Result : "+result+"\n(Reciprocal : "+reciprocal+")"
            else:
                output_string = output[0]
            await channel.send(output_string)
        return

    @dev_only
    async def cmd_qrencode(self, message, channel):
        """
        Usage:
            {command_prefix}qrencode [text]
            {command_prefix}qrencode advanced [text]

        Converts the input text into a qr code and sends it back to the user.

        "advanced" input:
        This is basically a wrapper for the qrencode library: https://github.com/fukuchi/libqrencode
        You may use any special flags described in the qrencode documentation (except for -o and -r and any text-output commands)
        Note: You may also need to include the quotation marks around your text while using "advanced" mode.
        """
        #parse inputs
        unallowed_strings = ["-o ","--output=","-r ","--read-from=","-V","--version","--verbose","-h","--help"]
        input = message.content.strip()
        try:
            input = input.split(" ",1)[1].strip()
        except:
            await channel.send("You need to give me text to encode",delete_after=30)
            return
        if any(unstr in input for unstr in unallowed_strings):
            await channel.send("Sorry, but you used a forbidden flag in your input. Please only use flags that affect the qrcode output.",delete_after=60)
            return

        #filename uses the unique message id to prevent overwrite issues
        hash_code = str(message.id)
        filename = os.path.abspath("image_cache/qrcode"+hash_code+".png")

        #generate command and run generation through cli
        if input.startswith("advanced "):
            command = "qrencode --output="+filename+" "+input.replace("advanced ","",1) #advanced mode - drops the quotes around the input text
        else:
            command = "qrencode --output="+filename+" '"+input+"'"
        print(command)
        output = await self.run_cli(command)
        print(output)
        if output[1].strip():
            await channel.send("There was a problem during QR code generation. Perhaps your input was invalid.")
            return
        
        #send image file
        with open(filename,"rb") as f:
            await channel.send(file=discord.File(f, "qrcode"+hash_code+".png"))
        return

    def write_birthday(self, user_id, day, month):
        """
        Adds a birthday to the birthdays.json file.
        Any existing birthdays for the same user will be overwritten.
        """
        try:
            with open(os.path.abspath("misc_data/birthdays.json"),"r") as f:
                entries = json.load(f)
        except FileNotFoundError:
            entries = {}
        
        entries.update({str(user_id) : [month,day]})

        with open(os.path.abspath("misc_data/birthdays.json"),"w") as f:
            json.dump(entries, f)

    def clear_birthday(self, user_id):
        """
        Attempts to remove a birthday from birthdays.json.
        Returns FileNotFoundError if birthdays.json does not exist.
        Returns KeyError if user_id does not already have a birthday saved.
        """
        with open(os.path.abspath("misc_data/birthdays.json"),"r") as f:
            entries = json.load(f)
        entries.pop(str(user_id))
        with open(os.path.abspath("misc_data/birthdays.json"),"w") as f:
            json.dump(entries, f)
        

    @dev_only
    async def cmd_addbirthday(self, message, channel):
        """
        Usage:
            {command_prefix}addbirthday [user_id] [date]

        Adds a user to the birthday list. 
        [date] follows the format MM/DD (Leading zeros may be omitted)
        Once added to the birthday list, the user can be removed with the {command_prefix}removebirthday command.
        If a birthday is already saved for the user, it will be overwritten.
        """
        #Read inputs
        inputs = message.content.strip().lower().split(" ")[1:]
        inputs = list(filter(lambda x:x, inputs))
        if not len(inputs)==2:
            await channel.send("That input has too many parts.",delete_after=30)
            return
        inputs[0],inputs[1] = inputs[0].strip(), inputs[1].strip()
        if not self.is_valid_id(inputs[0]):
            await channel.send("That was not a valid user id")
            return
        user_id = int(inputs[0])
        try:
            date_split = inputs[1].split("/")
            month = int(date_split[0])
            day = int(date_split[1])
        except IndexError:
            await channel.send("I couldn't parse that date",delete_after=30)
            return
        
        #Make entry
        self.write_birthday(user_id, day, month)
        await message.add_reaction("✅")
        return

    @dev_only
    async def cmd_removebirthday(self, message, channel):
        """
        Usage:
            {command_prefix}removebirthday [user_id]
        Removes a userfrom the birthday list. 
        """
        inputs = message.content.strip().lower().split(" ")[1:]
        inputs = list(filter(lambda x:x, inputs))
        try:
            if not self.is_valid_id(inputs[0]):
                raise ValueError
            user_id = int(inputs[0])
        except:
            await channel.send("I had trouble parsing that. Maybe it wasn't a valid ID",delete_after=30)
            return
        try:
            self.clear_birthday(user_id)
        except FileNotFoundError:
            await channel.send("I couldn't find the birthdays.json file",delete_after=30)
            return
        except KeyError:
            await channel.send("Nobody with that ID has a birthday stored",delete_after=30)
            return
        await message.add_reaction("✅")
        return

    @dev_only
    async def cmd_sendbirthdays(self, message):
        """
        Usage:
            {command_prefix}sendbirthdays
        For debug purposes only. Runs the daily birthday check and sends the birthday messages.
        """
        await self.send_birthdays()
        await message.add_reaction("✅")
        return

    def get_todays_birthdays(self):
        """Searches the birthdays.json and returns a list of all user_ids who have a birthday today"""
        matches = []
        try:
            with open(os.path.abspath("misc_data/birthdays.json"),"r") as f:
                entries = json.load(f)
        except FileNotFoundError:
            return []
        
        #get today's month and day as ints
        today = datetime.today()
        month = int(today.strftime("%m"))
        day = int(today.strftime("%d"))

        #search through entries
        for key, val in entries.items():
            if [month,day]==val: #json imports tuples as lists for some reason
                matches.append(key)

        return matches

    def get_all_birthdays(self):
        """Returns a list of all user_ids with saved birthdays"""
        try:
            with open(os.path.abspath("misc_data/birthdays.json"),"r") as f:
                entries = json.load(f)
        except FileNotFoundError:
            return []
        return entries.keys()

    async def send_birthdays(self):
        """
        Sends out the birthday messages.
        If a user is in the birthday.json list, and someone else in the list has a birthday today, the user is notified.
        """
        cake_emojis = "🧁🍥🥮🍰🎂🎁🥳🎉"
        bday_messages = ["Happy bday!!", "Woah! It's your birthday! That's so cool!", 
            "It's your birthday!", "Happy bday!", "Yoo! Happy birthday!", 
            "Congrats on the birthday!", "Wishing you a cool and fun birthday!"]

        bdays = self.get_todays_birthdays() #Everyone with a birthday today
        everyone = self.get_all_birthdays() #Everyone in the list

        if len(bdays)==0 or len(everyone)==0:
            return #Sad :(

        for id in bdays:
            user = self.get_user(id)
            user = user if user else await self.fetch_user(id)
            if not user:
                print("Could not find the birthday user "+str(id))
                continue #If the user does not exist, they will not be included in the birthday fun

            for id2 in everyone: #Notify everyone
                if id2==id or id2==self.user.id:
                    continue #Don't notify the person whos bday it is
                user2 = self.get_user(id2)
                user2 = user2 if user2 else await self.fetch_user(id2)
                if not user:
                    print("Failed to message "+str(id2)+" about the birthday of "+user.name)
                    continue #User2 could not be found
                
                try:
                    await user2.send("Psst! Today is "+user.mention+" 's birthday!! "+random.choice(cake_emojis))
                except:
                    continue #User2 could not be contacted :(
            
            #Finally wish the person a happy bday
            if not user.id == self.user.id:
                bday_message = random.choice(cake_emojis)+random.choice(cake_emojis)+" "+random.choice(bday_messages)+" "+random.choice(cake_emojis)+random.choice(cake_emojis)
                try:
                    await user.send(bday_message)
                except:
                    continue
        return


    async def cmd_rdj(self, message, channel):
        """
        Usage:
            {command_prefix}rdj [text]
            {command_prefix}rdj [font size] [text]
            {command_prefix}rdj flip [text]
            {command_prefix}rdj flip [font size] [text]


        Make a Robert Downey Jr meme out of your input text.
        Start the message with "flip" to mirror the image.
        Start the message with a number (after "flip" if you include it) to set the font size (default is 64).
        You can do multiple lines by simply using newlines in your message (SHIFT+return on desktop, return on mobile).

        --RDJ code by Katie.
        """

        #Read input
        try:
            text = message.content.strip().split(" ",1)[1]
        except IndexError:
            await channel.send("You need to give me text",delete_after=30)
            return
        
        #Parse any extra input stuff
        flip = text.startswith("flip ")
        if flip:
            text = text[5:]
        font_size = 64
        try:
            input_split = text.split(" ",1)
            if input_split[0].isnumeric():
                text = input_split[1]
                font_size = int(input_split[0])
        except IndexError:
            pass #Nothing to see here

        try:
            file_output = rdj(text=text, size=font_size, flip=flip) #in hindsight this didn't need to be its own file
        except:
            await channel.send("Woah, something went wrong whil making the image. Maybe try different inputs.", delete_after=30)
            return
        try:
            await channel.send(file=file_output)
        except:
            await channel.send("Hmm. I couldn't send you that image for some reason.\nIt's probably my fault.", delete_after=30)
            return
        return


    def generate_babynames(self, count, boys, girls):
        """
        Generates a List of unique-sounding baby names.
        'boys' and 'girls' are booleans that restrict the algorithm to boy or girl names. Setting both to true will have no effect. Setting both to false will also have no effect.
        """
        if not boys and not girls: #hacky fix
            boys, girls = True, True

        #Read babynames database
        names = []
        with open(os.path.abspath("misc_data/babynames.csv"),"r") as csv_file:
            csv_reader = csv.reader(csv_file, delimiter=",")
            for row in csv_reader:
                if boys and row[1]=="boy":
                    names.append(row[0])
                if girls and row[1]=="girl":
                    names.append(row[0])

        #Build the unique names
        results = []
        while len(results)<count:
            try:
                name1 = random.choice(names)
                name2 = random.choice(names).lower()
                combo_name = self.portmanteau(name1, name2)
                if not combo_name or combo_name==name1 or combo_name==name2:
                    continue
                results.append(combo_name)
            except (IndexError, AttributeError):
                print("There was a problem in the name generator while portmanteauing {} and {}".format(name1,name2))
        return results

    def generate_password(self, length, noSpecial, lower, allowSimilar):
        """Gerates a secure random password for the &generate command"""
        alphanumeric = "abcdefghjkmnpqrstuvwxyz23456789"
        alphanumeric_similar = "il1o0"
        special = "@#!?-+=',$%^&*()[]{}/\._"
        usable = ""+alphanumeric
        if allowSimilar:
            usable += alphanumeric_similar
        if not noSpecial:
            usable += special
        
        result = ""
        while len(result)<length:
            newchar = random.choice(usable)
            if not lower and random.random()<0.5:
                newchar = newchar.upper()
            result+=newchar
        
        return result


    async def cmd_generate(self, message, channel):
        """
        Multipurpose String Generator.

        Usage 1:
            {command_prefix}generate [boy/girl]
            {command_prefix}generatename [number (#)] [boy/girl]

        Generates a unique-sounding baby name.
        Specify a number to generate a list of multiple names. 
        Including the keywords "boy" or "girl" will restrict the algorithm to use gender-specific names as a basis.

        Usage 2:
            {command_prefix}generatepassword
            {command_prefix}generatepassword [length (#)] [noSpecial] [lower] [allowSimilar]

        Generates a unique password. Default length is 10.
        Including the keyword "noSpecial" will restrict the password to only alphanumeric characters.
        Including the keyword "lower" will prevent the password from using uppercase characters.
        By default, passwords will never contain character that look similar to other characters (e.g. I and l are not used). Use keyword "allowSimilar" to disable this.
        The message will auto-delete after one day.
        """
        #Read inputs
        try:
            tokens = message.content.lower().strip().split(" ")
            tokens = list(filter(lambda x:x, tokens)) #filter whitespace
        except IndexError:
            await channel.send("I had trouble parsing that",delete_after=30)
            return



        if tokens[0][1:]=="generatepassword" or "password" in tokens: #generate passwords
            number = 10
            for token in tokens:
                if token.isnumeric():
                    number = int(token)
                    break
            noSpecial = "nospecial" in tokens
            lower = "lower" in tokens
            allowSimilar = "allowsimilar" in tokens
            if number>100:
                await channel.send("That's too long.", delete_after=30)
                return
            result = self.generate_password(number, noSpecial, lower, allowSimilar)
            await channel.send("Here is your password: "+result, delete_after=3600*24)


        else:   #gerneate baby names
            number = 1
            for token in tokens:
                if token.isnumeric():
                    number = int(token)
                    break
            boys = "boys" in tokens or "boy" in tokens
            girls = "girls" in tokens or "girl" in tokens
            if number>50:
                await channel.send("That's too many names.", delete_after=30)
                return
            results = self.generate_babynames(number, boys, girls)
            await channel.send("Here are your names: "+", ".join(results))
        return


    async def cmd_8ball(self, channel):
        """
        Usage:
            {command_prefix}8ball

        Uses cutting-edge technology to shake the magic 8 ball, revealing it's wisdom.

        `(Disclaimer: 8 ball responses are random, and should not be treated as actual wisdom)`
        """

        ball_responses = [" It is certain","It is decidedly so","Without a doubt",\
            "Yes definitely","You may rely on it","As I see it, yes","Most likely","Outlook good",\
                "Yes","Signs point to yes","Reply hazy, try again","Ask again later","Better not tell you now",\
                    "Cannot predict now","Concentrate and ask again","Don't count on it","My reply is no","My sources say no",\
                        "Outlook not so good","Very doubtful"]
        
        shakes = random.randint(1,5)
        for i in range(0,shakes):
            shake_message = "shake "*random.randint(1,5)
            await channel.send(shake_message, delete_after=30)
            await asyncio.sleep(random.randint(1,3))
        await asyncio.sleep(0.5)
        await channel.send("< "+random.choice(ball_responses)+" >")
        return


        



        




