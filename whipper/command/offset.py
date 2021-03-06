# -*- Mode: Python -*-
# vi:si:et:sw=4:sts=4:ts=4

# Copyright (C) 2009 Thomas Vander Stichele

# This file is part of whipper.
#
# whipper is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# whipper is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with whipper.  If not, see <http://www.gnu.org/licenses/>.

import argparse
import os
import sys
import tempfile

import gobject
gobject.threads_init()

from whipper.command.basecommand import BaseCommand
from whipper.common import accurip, common, config, drive, program
from whipper.common import task as ctask
from whipper.program import cdrdao, cdparanoia, utils
from whipper.common import checksum

from whipper.extern.task import task

import logging
logger = logging.getLogger(__name__)

# see http://www.accuraterip.com/driveoffsets.htm
# and misc/offsets.py
OFFSETS = "+6, +48, +102, +667, +12, +30, +618, +594, +738, -472, " + \
          "+98, +116, +96, +733, +120, +691, +685, +97, +600, " + \
          "+690, +1292, +99, +676, +686, +1182, -24, +704, +572, " + \
          "+688, +91, +696, +103, -491, +689, +145, +708, +697, " + \
          "+564, +86, +679, +355, -496, -1164, +1160, +694, 0, " + \
          "-436, +79, +94, +684, +681, +106, +692, +943, +1194, " + \
          "+92, +117, +680, +682, +1268, +678, -582, +1473, +1279, " + \
          "-54, +1508, +740, +1272, +534, +976, +687, +675, +1303, " + \
          "+674, +1263, +108, +974, +122, +111, -489, +772, +732, " + \
          "-495, -494, +975, +935, +87, +668, +1776, +1364, +1336, " + \
          "+1127"


class Find(BaseCommand):
    summary = "find drive read offset"
    description = """Find drive's read offset by ripping tracks from a
CD in the AccurateRip database."""
    formatter_class = argparse.ArgumentDefaultsHelpFormatter
    device_option = True

    def add_arguments(self):
        self.parser.add_argument(
            '-o', '--offsets',
            action="store", dest="offsets", default=OFFSETS,
            help="list of offsets, comma-separated, colon-separated for ranges"
        )

    def handle_arguments(self):
        self._offsets = []
        blocks = self.options.offsets.split(',')
        for b in blocks:
            if ':' in b:
                a, b = b.split(':')
                self._offsets.extend(range(int(a), int(b) + 1))
            else:
                self._offsets.append(int(b))

        logger.debug('Trying with offsets %r', self._offsets)

    def do(self):
        runner = ctask.SyncRunner()

        device = self.options.device

        # if necessary, load and unmount
        sys.stdout.write('Checking device %s\n' % device)

        utils.load_device(device)
        utils.unmount_device(device)

        # first get the Table Of Contents of the CD
        t = cdrdao.ReadTOCTask(device)
        table = t.table

        logger.debug("CDDB disc id: %r", table.getCDDBDiscId())
        url = table.getAccurateRipURL()
        logger.debug("AccurateRip URL: %s", url)

        # FIXME: download url as a task too
        responses = []
        import urllib2
        try:
            handle = urllib2.urlopen(url)
            data = handle.read()
            responses = accurip.getAccurateRipResponses(data)
        except urllib2.HTTPError, e:
            if e.code == 404:
                sys.stdout.write(
                    'Album not found in AccurateRip database.\n')
                return 1
            else:
                raise

        if responses:
            logger.debug('%d AccurateRip responses found.' % len(responses))

            if responses[0].cddbDiscId != table.getCDDBDiscId():
                logger.warning("AccurateRip response discid different: %s",
                    responses[0].cddbDiscId)

        # now rip the first track at various offsets, calculating AccurateRip
        # CRC, and matching it against the retrieved ones

        def match(archecksum, track, responses):
            for i, r in enumerate(responses):
                if archecksum == r.checksums[track - 1]:
                    return archecksum, i

            return None, None

        for offset in self._offsets:
            sys.stdout.write('Trying read offset %d ...\n' % offset)
            try:
                archecksum = self._arcs(runner, table, 1, offset)
            except task.TaskException, e:

                # let MissingDependency fall through
                if isinstance(e.exception,
                    common.MissingDependencyException):
                    raise e

                if isinstance(e.exception, cdparanoia.FileSizeError):
                    sys.stdout.write(
                        'WARNING: cannot rip with offset %d...\n' % offset)
                    continue

                logger.warning("Unknown task exception for offset %d: %r" % (
                    offset, e))
                sys.stdout.write(
                    'WARNING: cannot rip with offset %d...\n' % offset)
                continue

            logger.debug('AR checksum calculated: %s' % archecksum)

            c, i = match(archecksum, 1, responses)
            if c:
                count = 1
                logger.debug('MATCHED against response %d' % i)
                sys.stdout.write(
                    'Offset of device is likely %d, confirming ...\n' %
                        offset)

                # now try and rip all other tracks as well, except for the
                # last one (to avoid readers that can't do overread
                for track in range(2, (len(table.tracks) + 1) - 1):
                    try:
                        archecksum = self._arcs(runner, table, track, offset)
                    except task.TaskException, e:
                        if isinstance(e.exception, cdparanoia.FileSizeError):
                            sys.stdout.write(
                                'WARNING: cannot rip with offset %d...\n' %
                                offset)
                            continue

                    c, i = match(archecksum, track, responses)
                    if c:
                        logger.debug('MATCHED track %d against response %d' % (
                            track, i))
                        count += 1

                if count == len(table.tracks) - 1:
                    self._foundOffset(device, offset)
                    return 0
                else:
                    sys.stdout.write(
                        'Only %d of %d tracks matched, continuing ...\n' % (
                        count, len(table.tracks)))

        sys.stdout.write('No matching offset found.\n')
        sys.stdout.write('Consider trying again with a different disc.\n')

    # TODO MW: Update this further for ARv2 code
    def _arcs(self, runner, table, track, offset):
        # rips the track with the given offset, return the arcs checksum
        logger.debug('Ripping track %r with offset %d ...', track, offset)

        fd, path = tempfile.mkstemp(
            suffix=u'.track%02d.offset%d.whipper.wav' % (
                track, offset))
        os.close(fd)

        t = cdparanoia.ReadTrackTask(path, table,
            table.getTrackStart(track), table.getTrackEnd(track),
            overread=False, offset=offset, device=self.options.device)
        t.description = 'Ripping track %d with read offset %d' % (
            track, offset)
        runner.run(t)


        # TODO MW: Update this to also use the v2 checksum(s)
        t = checksum.FastAccurateRipChecksumTask(path, trackNumber=track,
            trackCount=len(table.tracks), wave=True, v2=False)
        runner.run(t)

        os.unlink(path)
        return "%08x" % t.checksum

    def _foundOffset(self, device, offset):
        sys.stdout.write('\nRead offset of device is: %d.\n' %
            offset)

        info = drive.getDeviceInfo(device)
        if not info:
            sys.stdout.write('Offset not saved: could not get device info (requires pycdio).\n')
            return

        sys.stdout.write('Adding read offset to configuration file.\n')

        config.Config().setReadOffset(info[0], info[1], info[2],
            offset)


class Offset(BaseCommand):
    summary = "handle drive offsets"
    description = """
Drive offset detection utility.
"""
    subcommands = {
        'find': Find,
    }
