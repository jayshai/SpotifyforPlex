from client import SpotifyClient
from routing import function_path, route_path
from utils import localized_format, authenticated, ViewMode, Track, TrackMetadata, check_restart

from cachecontrol import CacheControl
from spotify_web.friendly import SpotifyArtist, SpotifyAlbum, SpotifyTrack
from threading import RLock, Event, Semaphore

import locale
import requests
import urllib
import urllib2
import time
from random import randint

class SpotifyPlugin(object):
    def __init__(self):
        self.client = None
        self.server = None
        self.play_lock      = Semaphore(1)
        self.start_lock     = Semaphore(1)
        self.start_marker   = Event()
        self.last_track_uri = None
        self.last_track_object = None

        Dict.Reset()
        Dict['play_count']             = 0
        Dict['last_restart']           = 0
        Dict['schedule_restart_each']  = 5*60    # restart each  X minutes
        Dict['play_restart_each']      = 2       # restart each  X plays
        Dict['check_restart_each']     = 5       # check if I should restart each X seconds

        Dict['radio_salt']             = False   # Saves last radio salt so multiple queries return the same radio track list

        self.start()

        self.session = requests.session()
        self.session_cached = CacheControl(self.session)

        Thread.CreateTimer(Dict['check_restart_each'], self.check_automatic_restart, globalize=True)

    @property
    def username(self):
        return Prefs["username"]

    @property
    def password(self):
        return Prefs["password"]

    def check_automatic_restart(self):

        can_restart = False

        try:

            diff = time.time() - Dict['last_restart']
            scheduled_restart  = diff >= Dict['schedule_restart_each']
            play_count_restart = Dict['play_count'] >= Dict['play_restart_each']
            must_restart = play_count_restart or scheduled_restart

            if must_restart:
                can_restart = self.play_lock.acquire(blocking=False)
                if can_restart:
                    Log.Debug('Automatic restart started')
                    self.start()
                    Log.Debug('Automatic restart finished')

        finally:

            if can_restart:
                self.play_lock.release()

            Thread.CreateTimer(Dict['check_restart_each'], self.check_automatic_restart, globalize=True)

    @check_restart
    def preferences_updated(self):
        """ Called when the user updates the plugin preferences"""
        self.start() # Trigger a client restart

    def start(self):
        """ Start the Spotify client and HTTP server """
        if not self.username or not self.password:
            Log("Username or password not set: not logging in")
            return False

        can_start = self.start_lock.acquire(blocking=False)
        try:
            # If there is a start in process, just wait until it finishes, but don't raise another one
            if not can_start:
                Log.Debug("Start already in progress, waiting it finishes to return")
                self.start_lock.acquire()
            else:
                Log.Debug("Start triggered, entering private section")
                self.start_marker.clear()

                if self.client:
                    self.client.restart(self.username, self.password)
                else:
                    self.client = SpotifyClient(self.username, self.password)

                self.last_track_uri = None
                self.last_track_object = None
                Dict['play_count']   = 0
                Dict['last_restart'] = time.time()
                self.start_marker.set()
                Log.Debug("Start finished, leaving private section")
        finally:
            self.start_lock.release()

        return self.client and self.client.is_logged_in()

    @check_restart
    def play(self, uri):
        Log('play(%s)' % repr(uri))

        uri = urllib.quote(uri.encode("utf8")).replace("%3A", ":").decode("utf8")

        track_url = None
        if not self.client.is_track_uri_valid(uri):
            Log("Play track callback invoked with invalid URI (%s). This is very bad :-(" % uri)
            track_url = "http://www.xamuel.com/blank-mp3-files/2sec.mp3"
        else:
            self.play_lock.acquire(blocking=True)
            try:
                track_url = self.get_track_url(uri)

                # If first request failed, trigger re-connection to spotify
                retry_num = 0
                while not track_url and retry_num < 2:
                    Log.Info('get_track_url (%s) failed, re-connecting to spotify...' % uri)
                    time.sleep(retry_num*0.5) # Wait some time based on number of failures
                    if self.start():
                        track_url = self.get_track_url(uri)
                    retry_num = retry_num + 1

                if track_url == False or track_url is None:
                    # Send an empty and short mp3 so player do not fail and we can go on listening next song
                    Log.Error("Play track (%s) couldn't be obtained. This is very bad :-(" % uri)
                    track_url = 'http://www.xamuel.com/blank-mp3-files/2sec.mp3'
                elif retry_num == 0: # If I didn't restart, add 1 to playcount
                    Dict['play_count'] = Dict['play_count'] + 1
            finally:
                self.play_lock.release()

        return Redirect(track_url)

    def get_track_url(self, track_uri):
        if not self.client.is_track_uri_valid(track_uri):
            return None

        track_url = None

        track = self.client.get(track_uri)
        if track:
            track_url = track.getFileURL(urlOnly=True, retries=1)

        return track_url

    #
    # TRACK DETAIL
    #
    @check_restart
    def metadata(self, uri):
        Log('metadata(%s)' % repr(uri))

        uri = urllib.quote(uri.encode("utf8")).replace("%3A", ":").decode("utf8")

        oc = ObjectContainer()
        track_object = None

        if not self.client.is_track_uri_valid(uri):
            Log("Metadata callback invoked with invalid URI (%s)" % uri)
            track_object = self.create_track_object_empty(uri)
        else:
            if self.last_track_uri == uri:
                track_object = self.last_track_object
            else:
                track_metadata = self.get_track_metadata(uri)

                if track_metadata:
                    track_object = self.create_track_object_from_metatada(track_metadata)
                    self.last_track_uri = uri
                    self.last_track_object = track_object
                else:
                    track_object = self.create_track_object_empty(uri)

        oc.add(track_object)
        return oc

    def get_track_metadata(self, track_uri):
        if not self.client.is_track_uri_valid(track_uri):
            return None

        track = self.client.get(track_uri)
        if not track:
            return None

        #track_uri       = track.getURI().decode("utf-8")
        title           = track.getName().decode("utf-8")
        image_url       = self.select_image(track.getAlbumCovers())
        track_duration  = int(track.getDuration())
        track_number    = int(track.getNumber())
        track_album     = track.getAlbum(nameOnly=True).decode("utf-8")
        track_artists   = track.getArtists(nameOnly=True).decode("utf-8")
        metadata        = TrackMetadata(title, image_url, track_uri, track_duration, track_number, track_album, track_artists)

        return metadata

    @staticmethod
    def select_image(images):
        if images == None:
            return None

        if images.get(640):
            return images[640]
        elif images.get(320):
            return images[320]
        elif images.get(300):
            return images[300]
        elif images.get(160):
            return images[160]
        elif images.get(60):
            return images[60]

        Log.Info('Unable to select image, available sizes: %s' % images.keys())
        return None

    def get_uri_image(self, uri):
        images = None
        obj = self.client.get(uri)
        if isinstance(obj, SpotifyArtist):
            images = obj.getPortraits()
        elif isinstance(obj, SpotifyAlbum):
            images = obj.getCovers()
        elif isinstance(obj, SpotifyTrack):
            images = obj.getAlbum().getCovers()
        elif isinstance(obj, SpotifyPlaylist):
            images = obj.getImages()

        return self.select_image(images)

    @authenticated
    @check_restart
    def image(self, uri):
        if not uri:
            # TODO media specific placeholders
            return Redirect(R('placeholder-artist.png'))

        Log.Debug('Getting image for: %s' % uri)

        uri = urllib.quote(uri.encode("utf8")).replace("%3A", ":").decode("utf8")

        if uri.startswith('spotify:'):
            # Fetch object for spotify URI and select image
            image_url = self.get_uri_image(uri)

            if not image_url:
                # TODO media specific placeholders
                return Redirect(R('placeholder-artist.png'))
        else:
            # pre-selected image provided
            Log.Debug('Using pre-selected image URL: "%s"' % uri)
            image_url = uri

        return self.session_cached.get(image_url).content

    #
    # SECOND_LEVEL_MENU
    #

    @authenticated
    @check_restart
    def explore(self):
        Log("explore")

        """ Explore shared music
        """
        return ObjectContainer(
            objects=[
                DirectoryObject(
                    key=route_path('explore/featured_playlists'),
                    title=L("MENU_FEATURED_PLAYLISTS"),
                    thumb=R("icon-explore-featuredplaylists.png")
                ),
                DirectoryObject(
                    key=route_path('explore/top_playlists'),
                    title=L("MENU_TOP_PLAYLISTS"),
                    thumb=R("icon-explore-topplaylists.png")
                ),
                DirectoryObject(
                    key=route_path('explore/new_releases'),
                    title=L("MENU_NEW_RELEASES"),
                    thumb=R("icon-explore-newreleases.png")
                ),
                DirectoryObject(
                    key=route_path('explore/genres'),
                    title=L("MENU_GENRES"),
                    thumb=R("icon-explore-genres.png")
                )
            ],
        )

    @authenticated
    @check_restart
    def discover(self):
        Log("discover")

        oc = ObjectContainer(
            title2=L("MENU_DISCOVER"),
            view_group=ViewMode.Stories
        )

        stories = self.client.discover()
        for story in stories:
            self.add_story_to_directory(story, oc)
        return oc

    @authenticated
    @check_restart
    def radio(self):
        Log("radio")

        """ Show radio options """
        return ObjectContainer(
            objects=[
                DirectoryObject(
                    key=route_path('radio/stations'),
                    title=L("MENU_RADIO_STATIONS"),
                    thumb=R("icon-radio-stations.png")
                ),
                DirectoryObject(
                    key=route_path('radio/genres'),
                    title=L("MENU_RADIO_GENRES"),
                    thumb=R("icon-radio-genres.png")
                )
            ],
        )

    @authenticated
    @check_restart
    def your_music(self):
        Log("your_music")

        """ Explore your music
        """
        return ObjectContainer(
            objects=[
                DirectoryObject(
                    key=route_path('your_music/playlists'),
                    title=L("MENU_PLAYLISTS"),
                    thumb=R("icon-playlists.png")
                ),
                DirectoryObject(
                    key=route_path('your_music/albums'),
                    title=L("MENU_ALBUMS"),
                    thumb=R("icon-albums.png")
                ),
                DirectoryObject(
                    key=route_path('your_music/artists'),
                    title=L("MENU_ARTISTS"),
                    thumb=R("icon-artists.png")
                ),
            ],
        )

    #
    # EXPLORE
    #

    @authenticated
    @check_restart
    def featured_playlists(self):
        Log("featured playlists")

        oc = ObjectContainer(
            title2=L("MENU_FEATURED_PLAYLISTS"),
            content=ContainerContent.Playlists,
            view_group=ViewMode.Playlists
        )

        playlists = self.client.get_featured_playlists()

        for playlist in playlists:
            self.add_playlist_to_directory(playlist, oc)

        return oc

    @authenticated
    @check_restart
    def top_playlists(self):
        Log("top playlists")

        oc = ObjectContainer(
            title2=L("MENU_TOP_PLAYLISTS"),
            content=ContainerContent.Playlists,
            view_group=ViewMode.Playlists
        )

        playlists = self.client.get_top_playlists()

        for playlist in playlists:
            self.add_playlist_to_directory(playlist, oc)

        return oc

    @authenticated
    @check_restart
    def new_releases(self):
        Log("new releases")

        oc = ObjectContainer(
            title2=L("MENU_NEW_RELEASES"),
            content=ContainerContent.Albums,
            view_group=ViewMode.Albums
        )

        albums = self.client.get_new_releases()

        for album in albums:
            self.add_album_to_directory(album, oc)

        return oc

    @authenticated
    @check_restart
    def genres(self):
        Log("genres")

        oc = ObjectContainer(
            title2=L("MENU_GENRES"),
            content=ContainerContent.Playlists,
            view_group=ViewMode.Playlists
        )

        genres = self.client.get_genres()

        for genre in genres:
            self.add_genre_to_directory(genre, oc)

        return oc

    @authenticated
    @check_restart
    def genre_playlists(self, genre_name):
        Log("genre playlists")

        oc = ObjectContainer(
            title2=genre_name,
            content=ContainerContent.Playlists,
            view_group=ViewMode.Playlists
        )

        playlists = self.client.get_playlists_by_genre(genre_name)

        for playlist in playlists:
            self.add_playlist_to_directory(playlist, oc)

        return oc

    #
    # RADIO
    #

    @authenticated
    @check_restart
    def radio_stations(self):
        Log('radio stations')

        Dict['radio_salt'] = False
        oc = ObjectContainer(title2=L("MENU_RADIO_STATIONS"))
        stations = self.client.get_radio_stations()
        for station in stations:
            oc.add(PopupDirectoryObject(
                        key=route_path('radio/stations/' + station.getURI()),
                        title=station.getTitle(),
                        thumb=function_path('image.png', uri=self.select_image(station.getImages()))
                        ))
        return oc

    @authenticated
    @check_restart
    def radio_genres(self):
        Log('radio genres')

        Dict['radio_salt'] = False
        oc = ObjectContainer(title2=L("MENU_RADIO_GENRES"))
        genres = self.client.get_radio_genres()
        for genre in genres:
            oc.add(PopupDirectoryObject(
                        key=route_path('radio/genres/' + genre.getURI()),
                        title=genre.getTitle(),
                        thumb=function_path('image.png', uri=self.select_image(genre.getImages()))
                        ))
        return oc

    @authenticated
    @check_restart
    def radio_track_num(self, uri):
        Log('radio track num')

        uri = urllib.quote(uri.encode("utf8")).replace("%3A", ":").decode("utf8")

        return ObjectContainer(
            title2=L("MENU_RADIO_TRACK_NUM"),
            objects=[
                DirectoryObject(
                    key=route_path('radio/play/' + uri + '/10'),
                    title=localized_format("MENU_TRACK_NUM", "10"),
                    thumb=R("icon-radio-item.png")
                ),
                DirectoryObject(
                    key=route_path('radio/play/' + uri + '/20'),
                    title=localized_format("MENU_TRACK_NUM", "20"),
                    thumb=R("icon-radio-item.png")
                ),
                DirectoryObject(
                    key=route_path('radio/play/' + uri + '/50'),
                    title=localized_format("MENU_TRACK_NUM", "50"),
                    thumb=R("icon-radio-item.png")
                ),
                DirectoryObject(
                    key=route_path('radio/play/' + uri + '/80'),
                    title=localized_format("MENU_TRACK_NUM", "80"),
                    thumb=R("icon-radio-item.png")
                ),
                DirectoryObject(
                    key=route_path('radio/play/' + uri + '/100'),
                    title=localized_format("MENU_TRACK_NUM", "100"),
                    thumb=R("icon-radio-item.png")
                )
            ],
        )

    @authenticated
    @check_restart
    def radio_tracks(self, uri, num_tracks):
        Log('radio tracks')

        uri = urllib.quote(uri.encode("utf8")).replace("%3A", ":").decode("utf8")

        oc     = None
        radio  = self.client.get_radio(uri)

        if not Dict['radio_salt']:
            Dict['radio_salt'] = radio.generateSalt()

        salt = Dict['radio_salt']
        tracks = radio.getTracks(salt=salt, num_tracks=int(num_tracks))

        oc = ObjectContainer(
            title2     = radio.getTitle().decode("utf-8"),
            content    = ContainerContent.Tracks,
            view_group = ViewMode.Tracks
        )

        for track in tracks:
            self.add_track_to_directory(track, oc)

        return oc

    #
    # YOUR_MUSIC
    #

    @authenticated
    @check_restart
    def playlists(self):
        Log("playlists")

        oc = ObjectContainer(
            title2=L("MENU_PLAYLISTS"),
            content=ContainerContent.Playlists,
            view_group=ViewMode.Playlists
        )

        playlists = self.client.get_playlists()

        for playlist in playlists:
            self.add_playlist_to_directory(playlist, oc)

        return oc

    @authenticated
    @check_restart
    def albums(self):
        Log("albums")

        oc = ObjectContainer(
            title2=L("MENU_ALBUMS"),
            content=ContainerContent.Albums,
            view_group=ViewMode.Albums
        )

        albums = self.client.get_my_albums()

        for album in albums:
            self.add_album_to_directory(album, oc)

        return oc

    @authenticated
    @check_restart
    def artists(self):
        Log("artists")

        oc = ObjectContainer(
            title2=L("MENU_ARTISTS"),
            content=ContainerContent.Artists,
            view_group=ViewMode.Artists
        )

        artists = self.client.get_my_artists()

        for artist in artists:
            self.add_artist_to_directory(artist, oc)

        return oc

    #
    # ARTIST DETAIL
    #

    @authenticated
    @check_restart
    def artist(self, uri):
        Log("artist")

        uri = urllib.quote(uri.encode("utf8")).replace("%3A", ":").decode("utf8")

        artist = self.client.get(uri)
        return ObjectContainer(
            title2=artist.getName().decode("utf-8"),

            objects=[
                DirectoryObject(
                    key  = route_path('artist/%s/top_tracks' % uri),
                    title=L("MENU_TOP_TRACKS"),
                    thumb=R("icon-artist-toptracks.png")
                ),
                DirectoryObject(
                    key  = route_path('artist/%s/albums' % uri),
                    title =L("MENU_ALBUMS"),
                    thumb =R("icon-albums.png")
                ),
                DirectoryObject(
                    key  = route_path('artist/%s/related' % uri),
                    title =L("MENU_RELATED"),
                    thumb =R("icon-artist-related.png")
                ),
                DirectoryObject(
                    key=route_path('radio/stations/' + uri),
                    title =L("MENU_RADIO"),
                    thumb =R("icon-radio-custom.png")
                )
            ],
        )

    @authenticated
    @check_restart
    def artist_albums(self, uri):
        Log("artist_albums")

        uri = urllib.quote(uri.encode("utf8")).replace("%3A", ":").decode("utf8")

        artist = self.client.get(uri)

        oc = ObjectContainer(
            title2=artist.getName().decode("utf-8"),
            content=ContainerContent.Albums
        )

        for album in artist.getAlbums():
            self.add_album_to_directory(album, oc)

        return oc

    @authenticated
    @check_restart
    def artist_top_tracks(self, uri):
        Log("artist_top_tracks")

        uri = urllib.quote(uri.encode("utf8")).replace("%3A", ":").decode("utf8")

        oc          = None
        artist      = self.client.get(uri)
        top_tracks  = artist.getTracks()

        if top_tracks:
            oc = ObjectContainer(
                title2=artist.getName().decode("utf-8"),
                content=ContainerContent.Tracks,
                view_group=ViewMode.Tracks
            )
            for track in artist.getTracks():
                self.add_track_to_directory(track, oc)
        else:
            oc = MessageContainer(
                header=L("MSG_TITLE_NO_RESULTS"),
                message=localized_format("MSG_FMT_NO_RESULTS", artist.getName().decode("utf-8"))
            )
        return oc

    @authenticated
    @check_restart
    def artist_related(self, uri):
        Log("artist_related")

        uri = urllib.quote(uri.encode("utf8")).replace("%3A", ":").decode("utf8")

        artist = self.client.get(uri)

        oc = ObjectContainer(
            title2=localized_format("MSG_RELATED_TO", artist.getName().decode("utf-8")),
            content=ContainerContent.Artists
        )

        for artist in artist.getRelatedArtists():
            self.add_artist_to_directory(artist, oc)

        return oc

    #
    # ALBUM DETAIL
    #

    @authenticated
    @check_restart
    def album(self, uri):
        Log("album")

        uri = urllib.quote(uri.encode("utf8")).replace("%3A", ":").decode("utf8")

        album = self.client.get(uri)

        oc = ObjectContainer(
            title2=album.getName().decode("utf-8"),
            content=ContainerContent.Artists
        )

        oc.add(DirectoryObject(
                    key  = route_path('album/%s/tracks' % uri),
                    title=L("MENU_ALBUM_TRACKS"),
                    thumb=R("icon-album-tracks.png")))

        artists = album.getArtists()
        for artist in artists:
            self.add_artist_to_directory(artist, oc)

        oc.add(DirectoryObject(
                    key=route_path('radio/stations/' + uri),
                    title =L("MENU_RADIO"),
                    thumb =R("icon-radio-custom.png")))

        return oc

    @authenticated
    @check_restart
    def album_tracks(self, uri):
        Log("album_tracks")

        uri = urllib.quote(uri.encode("utf8")).replace("%3A", ":").decode("utf8")

        album = self.client.get(uri)

        oc = ObjectContainer(
            title2=album.getName().decode("utf-8"),
            content=ContainerContent.Tracks,
            view_group=ViewMode.Tracks
        )

        for track in album.getTracks():
            self.add_track_to_directory(track, oc)

        return oc

    #
    # PLAYLIST DETAIL
    #

    @authenticated
    @check_restart
    def playlist(self, uri):
        Log("playlist")

        uri = urllib.quote(uri.encode("utf8")).replace("%3A", ":").decode("utf8")
        
        pl = self.client.get(uri)
        if pl is None:
            # Unable to find playlist
            return MessageContainer(
                header=L("MSG_TITLE_UNKNOWN_PLAYLIST"),
                message='URI: %s' % uri
            )

        Log("Get playlist: %s", pl.getName().decode("utf-8"))
        Log.Debug('playlist truncated: %s', pl.obj.contents.truncated)

        oc = ObjectContainer(
            title2=pl.getName().decode("utf-8"),
            content=ContainerContent.Tracks,
            view_group=ViewMode.Tracks,
            mixed_parents=True
        )

        for x, track in enumerate(pl.getTracks()):
            self.add_track_to_directory(track, oc, index=x)

        return oc

    #
    # MAIN MENU
    #
    def main_menu(self):
        Log("main_menu")

        return ObjectContainer(
            objects=[
                InputDirectoryObject(
                    key=route_path('search'),
                    prompt=L("PROMPT_SEARCH"),
                    title=L("MENU_SEARCH"),
                    thumb=R("icon-search.png")
                ),
                DirectoryObject(
                    key=route_path('explore'),
                    title=L("MENU_EXPLORE"),
                    thumb=R("icon-explore.png")
                ),
                DirectoryObject(
                    key=route_path('discover'),
                    title=L("MENU_DISCOVER"),
                    thumb=R("icon-discover.png")
                ),
                DirectoryObject(
                    key=route_path('radio'),
                    title=L("MENU_RADIO"),
                    thumb=R("icon-radio.png")
                ),
                DirectoryObject(
                    key=route_path('your_music'),
                    title=L("MENU_YOUR_MUSIC"),
                    thumb=R("icon-yourmusic.png")
                ),
                PrefsObject(
                    title=L("MENU_PREFS"),
                    thumb=R("icon-preferences.png")
                )
            ],
        )

    #
    # Create objects
    #
    def create_track_object_from_track(self, track, index=None):
        if not track:
            return None

        # Get metadata info
        track_uri       = track.getURI()
        title           = track.getName().decode("utf-8")
        image_url       = self.select_image(track.getAlbumCovers())
        track_duration  = int(track.getDuration()) - 500
        track_number    = int(track.getNumber())
        track_album     = track.getAlbum(nameOnly=True).decode("utf-8")
        track_artists   = track.getArtists(nameOnly=True).decode("utf-8")
        metadata = TrackMetadata(title, image_url, track_uri, track_duration, track_number, track_album, track_artists)

        return self.create_track_object_from_metatada(metadata, index=index)

    def create_track_object_from_metatada(self, metadata, index=None):
        if not metadata:
            return None
        return self.create_track_object(metadata.uri, metadata.duration, metadata.title, metadata.album, metadata.artists, metadata.number, metadata.image_url, index)

    def create_track_object_empty(self, uri):
        if not uri:
            return None
        return self.create_track_object(uri, -1, "", "", "", 0, None)

    def create_track_object(self, uri, duration, title, album, artists, track_number, image_url, index=None):
        rating_key = uri
        if index is not None:
            rating_key = '%s::%s' % (uri, index)

        art_num = str(randint(1,40)).rjust(2, "0")

        track_obj = TrackObject(
            items=[
                MediaObject(
                    parts=[PartObject(key=route_path('play/%s' % uri))],
                    duration=duration,
                    container=Container.MP3, audio_codec=AudioCodec.MP3, audio_channels = 2
                )
            ],

            key = route_path('metadata', uri),
            rating_key = rating_key,

            title  = title,
            album  = album,
            artist = artists,

            index    = index if index != None else track_number,
            duration = duration,

            source_title='Spotify',
            art   = R('art-' + art_num + '.png'),
            thumb = function_path('image.png', uri=image_url)
        )

        Log.Debug('New track object for metadata: --|%s|%s|%s|%s|%s|%s|--' % (image_url, uri, str(duration), str(track_number), album, artists))

        return track_obj

    def create_album_object(self, album, custom_summary=None, custom_image_url=None):
        """ Factory method for album objects """
        title = album.getName().decode("utf-8")
        if Prefs["displayAlbumYear"] and album.getYear() != 0:
            title = "%s (%s)" % (title, album.getYear())
        artist_name = album.getArtists(nameOnly=True).decode("utf-8")
        summary     = '' if custom_summary == None else custom_summary.decode('utf-8')
        image_url   = self.select_image(album.getCovers()) if custom_image_url == None else custom_image_url

        return DirectoryObject(
            key=route_path('album', album.getURI()),

            title=title + " - " + artist_name,
            tagline=artist_name,
            summary=summary,

            art=function_path('image.png', uri=image_url),
            thumb=function_path('image.png', uri=image_url),
        )

        #return AlbumObject(
        #    key=route_path('album', album.getURI().decode("utf-8")),
        #    rating_key=album.getURI().decode("utf-8"),
        #
        #    title=title,
        #    artist=artist_name,
        #    summary=summary,
        #
        #    track_count=album.getNumTracks(),
        #    source_title='Spotify',
        #
        #    art=function_path('image.png', uri=image_url),
        #    thumb=function_path('image.png', uri=image_url),
        #)

    def create_playlist_object(self, playlist):
        uri         = playlist.getURI()
        image_url   = self.select_image(playlist.getImages())
        artist      = playlist.getUsername().decode('utf8')
        title       = playlist.getName().decode("utf-8")
        summary     = ''
        if playlist.getDescription() != None and len(playlist.getDescription()) > 0:
            summary = playlist.getDescription().decode("utf-8")

        return DirectoryObject(
            key=route_path('playlist', uri),

            title=title + " - " + artist,
            tagline=artist,
            summary=summary,

            art=function_path('image.png', uri=image_url) if image_url != None else R("placeholder-playlist.png"),
            thumb=function_path('image.png', uri=image_url) if image_url != None else R("placeholder-playlist.png")
        )

        #return AlbumObject(
        #    key=route_path('playlist', uri),
        #    rating_key=uri,
        #
        #    title=title,
        #    artist=artist,
        #    summary=summary,
        #
        #    source_title='Spotify',
        #
        #    art=function_path('image.png', uri=image_url) if image_url != None else R("placeholder-playlist.png"),
        #    thumb=function_path('image.png', uri=image_url) if image_url != None else R("placeholder-playlist.png")
        #)

    def create_genre_object(self, genre):
        uri         = genre.getTemplateName()
        title       = genre.getName().decode("utf-8")
        image_url   = genre.getIconUrl()

        return DirectoryObject(
            key=route_path('genre', uri),

            title=title,

            art=function_path('image.png', uri=image_url) if image_url != None else R("placeholder-playlist.png"),
            thumb=function_path('image.png', uri=image_url) if image_url != None else R("placeholder-playlist.png")
        )

    def create_artist_object(self, artist, custom_summary=None, custom_image_url=None):
        image_url   = self.select_image(artist.getPortraits()) if custom_image_url == None else custom_image_url
        artist_name = artist.getName().decode("utf-8")
        summary     = '' if custom_summary == None else custom_summary.decode('utf-8')

        return DirectoryObject(
                    key=route_path('artist', artist.getURI()),

                    title=artist_name,
                    summary=summary,

                    art=function_path('image.png', uri=image_url),
                    thumb=function_path('image.png', uri=image_url)
                )

        #return ArtistObject(
        #        key=route_path('artist', artist.getURI().decode("utf-8")),
        #        rating_key=artist.getURI().decode("utf-8"),
        #
        #        title=artist_name,
        #        summary=summary,
        #        source_title='Spotify',
        #
        #        art=function_path('image.png', uri=image_url),
        #        thumb=function_path('image.png', uri=image_url)
        #    )

    #
    # Insert objects into container
    #

    def add_section_header(self, title, oc):
        oc.add(
            DirectoryObject(
                key='',
                title=title
            )
        )

    def add_track_to_directory(self, track, oc, index = None):
        if not self.client.is_track_playable(track):
            Log("Ignoring unplayable track: %s" % track.getName())
            return

        track_uri = track.getURI().decode("utf-8")
        if not self.client.is_track_uri_valid(track_uri):
            Log("Ignoring unplayable track: %s, invalid uri: %s" % (track.getName(), track_uri))
            return

        oc.add(self.create_track_object_from_track(track, index=index))

    def add_album_to_directory(self, album, oc, custom_summary=None, custom_image_url=None):
        if not self.client.is_album_playable(album):
            Log("Ignoring unplayable album: %s" % album.getName())
            return
        oc.add(self.create_album_object(album, custom_summary=custom_summary, custom_image_url=custom_image_url))

    def add_artist_to_directory(self, artist, oc, custom_summary=None, custom_image_url=None):
        oc.add(self.create_artist_object(artist, custom_summary=custom_summary, custom_image_url=custom_image_url))

    def add_playlist_to_directory(self, playlist, oc):
        oc.add(self.create_playlist_object(playlist))

    def add_genre_to_directory(self, genre, oc):
        oc.add(self.create_genre_object(genre))

    def add_story_to_directory(self, story, oc):
        content_type = story.getContentType()
        image_url    = self.select_image(story.getImages())
        item         = story.getObject()
        if content_type == 'artist':
            self.add_artist_to_directory(item, oc, custom_summary=story.getDescription(), custom_image_url=image_url)
        elif content_type == 'album':
            self.add_album_to_directory(item,  oc, custom_summary=story.getDescription(), custom_image_url=image_url)
        elif content_type == 'track':
            self.add_album_to_directory(item.getAlbum(), oc, custom_summary=story.getDescription() + " - " + item.getName(), custom_image_url=image_url)

        # Do not include playlists (just like official spotify client does)
        #elif content_type == 'playlist':
        #    self.add_playlist_to_directory(item, oc)
