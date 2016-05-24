# -*- coding: utf-8 -*-
"""
Classes and functions for receiver function calculation.
"""

import json
import logging
from operator import itemgetter
import warnings

import numpy as np
from obspy import read, Stream, Trace
from obspy.core import AttribDict
from obspy.geodetics import gps2dist_azimuth, kilometer2degrees
from obspy.taup import TauPyModel
from rf.deconvolve import deconvolve
from rf.simple_model import load_model
from rf.util import IterMultipleComponents


def __get_event_origin(h):
    return lambda event: (event.preferred_origin() or event.origins[0])[h]


def __SAC2UTC(stats, head):
    from obspy.io.sac.util import get_sac_reftime
    return get_sac_reftime(stats.sac) + stats[head]


def __UTC2SAC(stats, head):
    from obspy.io.sac.util import get_sac_reftime
    return stats[head] - get_sac_reftime(stats.sac)


STATION_GETTER = (('station_latitude', itemgetter('latitude')),
                  ('station_longitude', itemgetter('longitude')),
                  ('station_elevation', itemgetter('elevation')))
EVENT_GETTER = (  # ('event_id', lambda event: _get_event_id(event)),
    ('event_latitude', __get_event_origin('latitude')),
    ('event_longitude', __get_event_origin('longitude')),
    ('event_depth', lambda event: event.preferred_origin()['depth'] / 1000.),
    ('event_magnitude', lambda event: event.preferred_magnitude()['mag']),
    ('event_time', __get_event_origin('time')))
HEADERS = zip(*STATION_GETTER)[0] + zip(*EVENT_GETTER)[0] + (
    'onset', 'distance', 'back_azimuth', 'inclination', 'slowness',
    'pp_latitude', 'pp_longitude', 'pp_depth')
# The following headers can only be stored for H5:
# moveout
FORMATHEADERS = {'sac': ('stla', 'stlo', 'stel', 'evla', 'evlo',
                         'evdp', 'mag',
                         'o', 'a', 'gcarc', 'baz', 'user0', 'user1',
                         'user2', 'user3', 'user4'),
                 # field 'COMMENT' is violated for different information
                 'sh': ('COMMENT', 'COMMENT', 'COMMENT',
                        'LAT', 'LON', 'DEPTH',
                        'MAGNITUDE', 'ORIGIN', 'P-ONSET', 'DISTANCE',
                        'AZIMUTH', 'INCI', 'SLOWNESS',
                        'COMMENT', 'COMMENT', 'COMMENT')}
_HEADER_CONVERSIONS = {'sac': {'onset': (__SAC2UTC, __UTC2SAC),
                               'event_time': (__SAC2UTC, __UTC2SAC)}}


_TF = '.datetime:%Y-%m-%dT%H:%M:%S'
H5INDEX = ('{network}.{station}.{location}/{event_time%s}/' % _TF +
           '{channel}_{starttime%s}_{endtime%s}' % (_TF, _TF))
H5INDEX_STACK = '{network}.{station}.{location}/{channel}'


def set_index(index='rf'):
    import obspyh5
    if index == 'rf':
        index = H5INDEX
    elif index == 'rf_stack':
        index = H5INDEX_STACK
    obspyh5.set_index(index)


def read_rf(*args, **kwargs):
    """
    Read waveform files into RFStream object.

    See :func:`read() <obspy.core.stream.read>` in ObsPy.
    """
    return RFStream(read(*args, **kwargs))


class RFStream(Stream):

    """
    Class providing the necessary functions for receiver function calculation.

    To initialize a RFStream from a Stream object use

    >>> rfstream = RFStream(stream)

    To initialize a RFStream from a file use

    >>> rfstream = read_rf('test.SAC')

    Format specific headers are loaded into the stats object of all traces.
    """

    def __init__(self, traces=None, warn=True):
        self.traces = []
        if isinstance(traces, Trace):
            traces = [traces]
        if traces:
            for tr in traces:
                if not isinstance(tr, RFTrace):
                    tr = RFTrace(trace=tr, warn=warn)
                self.traces.append(tr)

    def write(self, filename, format, **kwargs):
        """
        Save stream to file including format specific headers.

        See :meth:`Stream.write() <obspy.core.stream.Stream.write>` in ObsPy.
        """
        for tr in self:
            tr._write_format_specific_header(format)
            if format.upper() == 'Q':
                tr.stats.station = tr.id
        super(RFStream, self).write(filename, format, **kwargs)
        if format.upper() == 'Q':
            for tr in self:
                tr.stats.station = tr.stats.station.split('.')[1]

    def rotate(self, *args, **kwargs):
        """
        Rotate three component streams.

        See :meth:`Stream.rotate() <obspy.core.stream.Stream.rotate>`.
        """
        for stream3c in IterMultipleComponents(self, key='onset',
                                               number_components=(2, 3)):
            super(RFTrace, self).rotate(*args, **kwargs)

    def deconvolve(self, *args, **kwargs):
        """
        Deconvolve source component of stream.

        All args and kwargs are passed to the function
        :func:`~rf.deconvolve.deconvolve`.
        """
        rsp = deconvolve(self, *args, **kwargs)
        self.traces = rsp

    def rf(self, method='P', filter=None, window=None, downsample=None,
           rotate='ZNE->LQT', deconvolve='time', source_components='LZ',
           **kwargs):
        r"""
        Calculate receiver functions in-place.

        :param method: 'P' for P receiver functions, 'S' for S receiver
            functions
        :param dictionary filter: filter stream with its
            :meth:`~obspy.core.stream.Stream.filter` method and given kwargs
        :type window: tuple of length 2
        :param window: trim stream relative to P- or S-onset
             with :meth:`~obspy.core.stream.Stream.trim` (seconds)
        :param float downsample: downsample stream with its
            :meth:`~obspy.core.stream.Stream.decimate` method to the given
            frequency
        :param rotate: 'ZNE->LQT' or 'NE->RT', rotate stream with its
            :meth:`~obspy.core.stream.Stream.rotate`
            method with the angles given by the back_azimuth and inclination
            attributes of the traces stats objects. You can set these to your
            needs or let them be computed by :func:`~rf.rfstream.rfstats`.
        :param deconvolve: 'time' or 'freq' for time or frequency domain
            deconvolution by the streams
            :meth:`~rf.rfstream.RFStream.deconvolve`
            method. See :func:`~rf.deconvolve.deconvolve`,
            :func:`~rf.deconvolve.deconvt` and :func:`~rf.deconvolve.deconvf`
            for further documentation.
        :param \*\*kwargs: all other kwargs not mentioned here are
            passed to deconvolve

        After performing the deconvolution the Q/R and T components are
        multiplied by -1 to get a positive phase for a Moho-like positive
        velocity contrast. Furthermore for method='S' all components are
        mirrored at t=0 for a better comparison with P receiver functions.
        See source code of this function for the default
        deconvolution windows.
        """
        def iter3c(stream):
            return IterMultipleComponents(self, key='onset',
                                          number_components=(2, 3))
        if method not in 'PS':
            raise NotImplementedError
        if filter:
            self.filter(**filter)
        if window:
            for tr in self:
                tr.trim(tr.stats.onset + window[0], tr.stats.onset + window[1])
        if downsample:
            for tr in self:
                if downsample <= tr.stats.sampling_rate:
                    tr.decimate(int(tr.stats.sampling_rate) // downsample)
        if rotate:
            for stream3c in iter3c(self):
                stream3c.rotate(rotate)
        if deconvolve:
            for stream3c in iter3c(self):
                stream3c.deconvolve(method=deconvolve, set_tw=method,
                                    source_components=source_components,
                                    **kwargs)
        # Mirrow Q/R and T component at 0s for S-receiver method for a better
        # comparison with P-receiver method (converted Sp wave arrives before
        # S wave, but converted Ps wave arrives after P wave)
        if method == 'S':
            for tr in self:
                tr.data = tr.data[::-1]
                tr.stats.onset = tr.stats.starttime + (tr.stats.endtime -
                                                       tr.stats.onset)
        # Multiply -1 on Q/R and T component, because Q/R component is pointing
        # towards the event after the rotation. For a positive phase at
        # a Moho-like velocity contrast, the Q/R component has to
        # point away from the event.
        for tr in self:
            if tr.stats.channel[-1] not in source_components:
                tr.data = -tr.data

    def moveout(self, phase='Ps', ref=6.4, model='iasp91'):
        """
        In-place moveout correction to a reference slowness.

        Needs stats attributes slowness and onset.

        :param phase: 'Ps', 'Sp', 'Ppss' or other multiples
        :param ref: reference ray parameter in s/deg
        :param model: Path to model file
            (see :class:`~rf.simple_model.SimpleModel`, default: iasp91)
        """
        model = load_model(model)
        model.moveout(self, phase=phase, ref=ref)
        for tr in self:
            tr.stats.moveout = {'phase': phase, 'model': model,
                                'slowness_before_moveout': tr.stats.slowness}
            tr.stats.slowness = ref

    def _moveout_xy(self, *args, **kwargs):
        for tr in self:
            tr._moveout_xy(*args, **kwargs)

    def ppoint(self, pp_depth, pp_phase='S', model='iasp91'):
        """
        Calculate coordinates of piercing point by 1D ray tracing.

        The iasp91 model is used. Piercing point coordinates are stored in the
        stats attributes plat and plon. Needs stats attributes
        station_latitude, station_longitude, slowness and back_azimuth.

        :param pp_depth: depth of interface in km
        :param pp_phase: 'P' for piercing points of P wave, 'S' for piercing
            points of S wave. Multiples are possible, too.
        :param model: Path to model file
            (see :class:`~rf.simple_model.SimpleModel`, default: iasp91)
        :return: NumPy array with coordinates of piercing points

        .. note::

            `phase='S'` is usually wanted for P receiver functions and 'P'
            for S receiver functions.
        """
        model = load_model(model)
        for tr in self:
            model.ppoint(tr.stats, pp_depth, phase=pp_phase)
        return np.array([(tr.stats.pp_latitude, tr.stats.pp_longitude)
                         for tr in self])

    def _ppoint_xy(self, *args, **kwargs):
        for tr in self:
            tr._ppoint_xy(*args, **kwargs)

    def stack(self):
        """
        Stack traces with the same id into new Stream.

        Traces with same id need to have the same number of datapoints.
        """
        ids = set(tr.id for tr in self)
        traces = []
        for id in ids:
            net, sta, loc, cha = id.split('.')
            data = np.mean([tr.data for tr in self if tr.id == id], axis=0)
            header = {'network': net, 'station': sta, 'location': loc,
                      'channel': cha, 'sampling_rate': tr.stats.sampling_rate}
            onset = tr.stats.onset - tr.stats.starttime
            tr2 = RFTrace(data=data, header=header)
            tr2.stats['onset'] = tr2.stats['starttime'] + onset
            traces.append(tr2)
        return self.__class__(traces)

    def plot_rf(self, *args, **kwargs):
        """
        Create receiver function plot.

        See :func:`~rf.imaging.plot_rf` for help on arguments.
        """
        from rf.imaging import plot_rf
        return plot_rf(self, *args, **kwargs)


class RFTrace(Trace):

    """
    Class providing the Trace object for receiver function calculation.
    """

    def __init__(self, data=np.array([]), header={}, trace=None, warn=True):
        if trace is not None:
            data = trace.data
            header = trace.stats
        super(RFTrace, self).__init__(data=data, header=header)
        st = self.stats
        if ('_format'in st and st._format.upper() == 'Q' and
                st.station.count('.') > 0):
            st.network, st.station, st.location = st.station.split('.')[:3]
        self._read_format_specific_header(warn=warn)

    def __str__(self, id_length=None):
        out = (u' | {event_magnitude:.1f}M dist:{distance:.1f} '
               u'baz:{back_azimuth:.1f}')
        try:
            out = out.format(**self.stats)
        except KeyError:
            out = ''
        return super(RFTrace, self).__str__(id_length=id_length) + out

    def _read_format_specific_header(self, format=None, warn=True):
        st = self.stats
        if format is None:
            if '_format' not in st:
                return
            format = st._format
        format = format.lower()
        if format == 'q':
            format = 'sh'
        if format == 'h5':
            return
        try:
            header_map = zip(HEADERS, FORMATHEADERS[format])
        except KeyError:
            if warn:
                warnings.warn('Reading rf header of a file with this format '
                              'is not supported.')
            return
        read_comment = False
        for head, head_format in header_map:
            if format == 'sh' and read_comment:
                continue
            try:
                value = st[format][head_format]
            except KeyError:
                continue
            else:
                if format == 'sac' and '-12345' in str(value):
                    pass
                elif format == 'sh' and head_format == 'COMMENT':
                    st.update(json.loads(value))
                    continue
                else:
                    st[head] = value
            try:
                convert = _HEADER_CONVERSIONS[format][head][0]
                st[head] = convert(st, head)
            except KeyError:
                pass

    def _write_format_specific_header(self, format):
        st = self.stats
        format = format.lower()
        if format == 'q':
            format = 'sh'
        elif format == 'h5':
            return
        elif format == 'sac' and 'sac' not in self.stats:
            from obspy.io.sac.util import obspy_to_sac_header
            self.stats.sac = obspy_to_sac_header(self.stats)
        try:
            header_map = zip(HEADERS, FORMATHEADERS[format])
        except KeyError:
            if format != 'h5':
                msg = ("rf in-/output of file format '%s' is not supported" %
                       format)
                warnings.warn(msg)
            return
        if format not in st:
            st[format] = AttribDict({})
        if format == 'sh':
            comment = {}
        for head, head_format in header_map:
            if format == 'sh' and head_format == 'COMMENT':
                try:
                    comment[head] = st[head]
                except KeyError:
                    pass
                continue
            try:
                val = st[head]
            except KeyError:
                continue
            try:
                convert = _HEADER_CONVERSIONS[format][head][1]
                val = convert(st, head)
            except KeyError:
                pass
            st[format][head_format] = val
        if format == 'sh' and len(comment) > 0:
            st[format]['COMMENT'] = json.dumps(comment, separators=(',', ':'))

    def write(self, filename, format, **kwargs):
        """
        Save current trace into a file  including format specific headers.

        See :meth:`Trace.write() <obspy.core.trace.Trace.write>` in ObsPy.
        """
        RFStream([self]).write(filename, format, **kwargs)

    def _moveout_xy(self, phase='Ps'):
        """
        Depreciated! Moveout correction to a slowness of 6.4s/deg.

        The iasp91 model is used. The correction is independent from the type
        of receiver function. Needs stats attributes slowness and onset.
        """
        from rf import _xy
        itype = {'Ps': 1, 'Ppps': 2, 'Ppss': 3, 'Psss': 3}[phase]
        st = self.stats
        dt = st.onset - st.starttime
        data = _xy.psmout([self.data], st.slowness, -dt,
                          st.endtime - st.starttime-dt, st.delta, itype)
        self.data = data[0, :]

    def _ppoint_xy(self, depth, method='P'):
        """
        Depreciated! Calculate coordinates of piercing point by 1D ray tracing.

        The iasp91 model is used. Piercing point coordinates are stored in the
        stats attributes `plat` and `plon`. Needs stats attributes
        station_latitude, station_longitude, slowness and back_azimuth.

        :param depth: depth of piercing points in km
        :param method: 'P' or 'S' for P or S waves
        """
        from rf import _xy
        if method not in 'PS':
            raise NotImplementedError()
        st = self.stats
        args = (depth, st.station_latitude, st.station_longitude,
                st.slowness, st.back_azimuth)
        pier_func = _xy.pspier if method == 'P' else _xy.sppier
        _, lat, lon = pier_func(*args)
        st.plat = lat
        st.plon = lon


def obj2stats(event=None, station=None):
    """
    Map event and station object to stats with attributes.

    :param event: ObsPy :class:`~obspy.core.event.Event` object
    :param station: station object with attributes latitude, longitude and
        elevation
    :return: ``stats`` object with station and event attributes
    """
    stats = AttribDict({})
    if event is not None:
        for key, getter in EVENT_GETTER:
            stats[key] = getter(event)
    if station is not None:
        for key, getter in STATION_GETTER:
            stats[key] = getter(station)
    return stats


def rfstats(stats=None, event=None, station=None, stream=None,
            phase='P', dist_range='default', tt_model='iasp91',
            pp_depth=None, pp_phase=None, model='iasp91'):
    """
    Calculate ray specific values like slowness for given event and station.

    :param stats: stats object with event and/or station attributes. Can be
        None if both event and station are given.
    :param event: ObsPy :class:`~obspy.core.event.Event` object
    :param station: station object with attributes latitude, longitude and
        elevation
    :param stream: If a stream is given, stats has to be None. In this case
        rfstats will be called for every stats object in the stream.
    :param phase: string with phase. Usually this will be 'P' or
        'S' for P and S receiver functions, respectively.
    :type dist_range: tuple of length 2
    :param dist_range: if epicentral of event is not in this intervall, None
        is returned by this function,\n
        if phase == 'P' defaults to (30, 90),\n
        if phase == 'S' defaults to (50, 85)
    :param tt_model: model for travel time calculation.
        (see the :mod:`obspy.taup` module, default: iasp91)
    :param pp_depth: Depth for piercing point calculation
        (in km, default: None -> No calculation)
    :param pp_phase: Phase for pp calculation (default: 'S' for P-receiver
        function and 'P' for S-receiver function)
    :param model': Path to model file for pp calculation
        (see :class:`~rf.simple_model.SimpleModel`, default: iasp91)
    :return: ``stats`` object with event and station attributes, distance,
        back_azimuth, inclination, onset and slowness or None if epicentral
        distance is not in the given intervall
    """
    if stream is not None:
        assert stats is None
        kwargs = {'event': event, 'station': station, 'stream': None,
                  'phase': phase, 'dist_range': dist_range,
                  'tt_model': tt_model, 'pp_depth': pp_depth,
                  'pp_phase': pp_phase, 'model': model}
        for tr in stream:
            rfstats(stats=tr.stats, **kwargs)
        return
    phase = phase.upper()
    if dist_range == 'default' and phase in 'PS':
        dist_range = (30, 90) if phase == 'P' else (50, 85)
    if stats is None:
        stats = AttribDict({})
    if event is not None and station is not None:
        stats.update(obj2stats(event=event, station=station))
    dist, baz, _ = gps2dist_azimuth(stats.station_latitude,
                                    stats.station_longitude,
                                    stats.event_latitude,
                                    stats.event_longitude)
    dist = kilometer2degrees(dist / 1000)
    if dist_range and not dist_range[0] <= dist <= dist_range[1]:
        return
    tt_model = TauPyModel(model=tt_model)
    arrivals = tt_model.get_travel_times(stats.event_depth, dist, (phase,))
    if len(arrivals) == 0:
        raise Exception('TauPy does not return phase %s at distance %s' %
                        (phase, dist))
    if len(arrivals) > 1:
        from warnings import warn
        msg = ('TauPy returns more than one arrival for phase %s at '
               'distance -> take first arrival')
        warn(msg % (phase, dist))
    arrival = arrivals[0]
    onset = stats.event_time + arrival.time
    inc = arrival.incident_angle
    slowness = arrival.ray_param_sec_degree
    stats.update({'distance': dist, 'back_azimuth': baz, 'inclination': inc,
                  'onset': onset, 'slowness': slowness})
    if pp_depth is not None:
        model = load_model(model)
        if pp_phase is None:
            pp_phase = 'S' if phase.upper().endswith('P') else 'P'
        model.ppoint(stats, pp_depth, phase=pp_phase)
    return stats
