# -*- Mode: Python; test-case-name: morituri.test.test_common_musicbrainzngs -*-
# vi:si:et:sw=4:sts=4:ts=4

# Morituri - for those about to RIP

# Copyright (C) 2009, 2010, 2011 Thomas Vander Stichele

# This file is part of morituri.
#
# morituri is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# morituri is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with morituri.  If not, see <http://www.gnu.org/licenses/>.

"""
Handles communication with the musicbrainz server using NGS.
"""

import urllib2

from morituri.common import log


VA_ID = "89ad4ac3-39f7-470e-963a-56509c546377" # Various Artists

class MusicBrainzException(Exception):
    def __init__(self, exc):
        self.args = (exc, )
        self.exception = exc

class NotFoundException(MusicBrainzException):
    def __str__(self):
        return "Disc not found in MusicBrainz"


class TrackMetadata(object):
    artist = None
    title = None
    duration = None # in ms
    mbid = None
    sortName = None
    mbidArtist = None


class DiscMetadata(object):
    """
    @param release:      earliest release date, in YYYY-MM-DD
    @type  release:      unicode
    @param title:        title of the disc (with disambiguation)
    @param releaseTitle: title of the release (without disambiguation)
    """
    artist = None
    sortName = None
    title = None
    various = False
    tracks = None
    release = None

    releaseTitle = None

    mbid = None
    mbidArtist = None

    def __init__(self):
        self.tracks = []


def _getMetadata(release, discid):
    """
    @type  release: L{musicbrainz2.model.Release}

    @rtype: L{DiscMetadata} or None
    """
    log.debug('program', 'getMetadata for release id %r',
        release['id'])
    if not release['id']:
        log.warning('program', 'No id for release %r', release)
        return None

    assert release['id'], 'Release does not have an id'

    metadata = DiscMetadata()

    if len(release['artist-credit']) > 1:
        log.warning('musicbrainzngs', 'artist-credit more than 1: %r',
            release['artist-credit'])

    artist = release['artist-credit'][0]['artist']

    # FIXME: is there a better way to check for VA
    metadata.various = False
    if artist['id'] == VA_ID:
        metadata.various = True
    isSingleArtist = not metadata.various

    # getUniqueName gets disambiguating names like Muse (UK rock band)
    metadata.artist = artist['name']
    metadata.sortName = artist['sort-name']
    # FIXME: is format str ?
    if not release.has_key('date'):
        log.warning('musicbrainzngs', 'Release %r does not have date', release)
    else:
        metadata.release = release['date']

    metadata.mbid = release['id']
    metadata.mbidArtist = artist['id']
    metadata.url = 'http://musicbrainz.org/release/' + release['id']

    tainted = False
    duration = 0

    # only show discs from medium-list->disc-list with matching discid
    for medium in release['medium-list']:
        for disc in medium['disc-list']:
            if disc['id'] == discid:
                title = release['title']
                metadata.releaseTitle = title
                if release.has_key('disambiguation'):
                    title += " (%s)" % release['disambiguation']
                count = len(release['medium-list'])
                if count > 1:
                    title += ' (Disc %d of %d)' % (
                        int(medium['position']), count)
                if medium.has_key('title'):
                    title += ": %s" % medium['title']
                metadata.title = title
                for t in medium['track-list']:
                    track = TrackMetadata()

                    if isSingleArtist or not t['recording'].has_key('artist-credit'):
                        track.artist = metadata.artist
                        track.sortName = metadata.sortName
                        track.mbidArtist = metadata.mbidArtist
                    else:
                        # various artists discs can have tracks with no artist
                        if len(t['recording']['artist-credit']) > 1:
                            log.warning('musicbrainzngs', 'artist-credit more than 1: %r',
                                t['recording']['artist-credit'])
                        artist = t['recording']['artist-credit'][0]['artist']
                        track.artist = artist and artist['name'] or metadata.artist.name
                        track.sortName = artist and artist['sort-name'] or metadata.artist.sortName
                        track.mbidArtist = artist and artist['id'] or metadata.artist.mbid

                    track.title = t['recording']['title']
                    track.mbid = t['recording']['id']

                    # FIXME: unit of duration ?
                    track.duration = int(t['recording'].get('length', 0))
                    if not track.duration:
                        log.warning('getMetadata',
                            'track %r (%r) does not have duration' % (
                                track.title, track.mbid))
                        tainted = True
                    else:
                        duration += track.duration

                    metadata.tracks.append(track)

                if not tainted:
                    metadata.duration = duration
                else:
                    metadata.duration = 0

    return metadata


# see http://bugs.musicbrainz.org/browser/python-musicbrainz2/trunk/examples/ripper.py
def musicbrainz(discid):
    """
    Based on a MusicBrainz disc id, get a list of DiscMetadata objects
    for the given disc id.

    Example disc id: Mj48G109whzEmAbPBoGvd4KyCS4-

    @type  discid: str

    @rtype: list of L{DiscMetadata}
    """
    log.debug('musicbrainz', 'looking up results for discid %r', discid)
    from morituri.extern.musicbrainzngs import musicbrainz

    results = []

    try:
        result = musicbrainz.get_releases_by_discid(discid,
            includes=["artists", "recordings", "release-groups"])
    except musicbrainz.ResponseError, e:
        if isinstance(e.cause, urllib2.HTTPError):
            if e.cause.code == 404:
                raise NotFoundException(e)

        raise MusicBrainzException(e)

    # No disc matching this DiscID has been found.
    if len(result) == 0:
        return None

    log.debug('musicbrainz', 'found %d releases for discid %r',
        len(result['disc']['release-list']),
        discid)

    # Display the returned results to the user.
    ret = []

    for release in result['disc']['release-list']:
        log.debug('program', 'result %r: artist %r, title %r' % (
            release, release['artist-credit-phrase'], release['title']))

        # to get titles of recordings, we need to query the release with
        # artist-credits

        res = musicbrainz.get_release_by_id(release['id'],
            includes=["artists", "artist-credits", "recordings", "discids"])
        release = res['release']

        md = _getMetadata(release, discid)
        if md:
            log.debug('program', 'duration %r', md.duration)
            ret.append(md)

    return ret
