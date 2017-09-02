'''
Classes for reading and writing MIDI files
'''
from warnings import warn
from struct import unpack, pack
from util import read_varlen, write_varlen
from Constants import DEFAULT_MIDI_HEADER_SIZE
from Containers import Track, Pattern
from Events import MetaEvent, SysexEvent, EventRegistry, UnknownMetaEvent, Event


class FileReader(object):

    # TODO: move these to constants
    CHUNK_SIZE = 4  # size of midi file or track header in bytes
    HEADER_SIZE = 10  # size of midi header contents in bytes

    def read(self, buffer):
        '''
        Read a midi file from a buffer and return a Pattern object
        '''
        pattern = self.parse_file_header(buffer)
        for track in pattern:
            track += self.parse_track(buffer)
        return pattern


    def parse_file_header(self, buffer):
        '''
        Parse header information from a buffer and return a Pattern based on that information.
        '''
        header = buffer.read(self.CHUNK_SIZE)
        if header != b'MThd':
            raise TypeError("Bad header in MIDI file")
        # a long followed by three shorts
        data = unpack(">LHHH", buffer.read(self.HEADER_SIZE))
        header_size = data[0]
        fmt = data[1]
        num_tracks = data[2]
        resolution = self.parse_resolution(data[3])
        # assume any remaining bytes in header are padding
        if header_size > DEFAULT_MIDI_HEADER_SIZE:
            buffer.read(header_size - DEFAULT_MIDI_HEADER_SIZE)
        tracks = [Track() for _ in range(num_tracks)]
        return Pattern(tracks=tracks, resolution=resolution, fmt=fmt)


    def parse_resolution(self, resolution):
        '''
        Parse resolution information in MIDI header, either ticks per quarter note
        or SMPTE information
        '''
        if resolution & 0x80000:
            smpte = (resolution >> 7) | 0x7f
            ticks_per_frame = resolution | 0x7f
            return (smpte, ticks_per_frame)
        return (resolution,)


    def parse_track(self, buffer):
        '''Parse a MIDI track into a tuple of events'''
        self.running_status = None
        track_size = self.parse_track_header(buffer)
        track_data = iter(buffer.read(track_size))
        events = []
        while track_data:
            try:
                event = self.parse_event(track_data)
                events.append(event)
            except StopIteration:
                break
        return tuple(events)


    def parse_track_header(self, buffer):
        '''Parse track information from header
        Return track size in bytes'''
        header = buffer.read(self.CHUNK_SIZE)
        if header != b'MTrk':
            raise TypeError("Bad track header in midi file: " + str(header))
        track_size = unpack('>L', buffer.read(4))[0]
        return track_size

    def parse_event(self, track_iter):
        '''Parses an event from a byte iterator.
        Returns a MidiEvent, SysexEvent, or MetaEvent, or subclass thereof'''
        tick = read_varlen(track_iter)
        header_byte = next(track_iter)
        if SysexEvent.is_event(header_byte):
            return self.parse_sysex_event(tick, track_iter)
        elif MetaEvent.is_event(header_byte):
            return self.parse_meta_event(tick, track_iter)
        return self.parse_midi_event(tick, header_byte, track_iter)
        

    def parse_sysex_event(tick, track_iter):
        '''
        Return a SysexEvent object given a tick and track_iter byte iterator
        '''
        payload = []
        byte = next(track_iter)
        # 0xF7 signals end of Sysex data stream
        while byte != 0xF7:
            payload.append(byte)
            byte = next(track_iter)
        return SysexEvent(tick=tick, data=payload)


    def parse_meta_event(self, tick, track_iter):
        '''
        Parse and return a MetaEvent subclass from a byte iterator
        '''
        metacommand = next(track_iter)
        if metacommand not in EventRegistry.MetaEvents:
            warn('Unknown Meta MIDI Event: ' + str(metacommand), Warning)
            cls = UnknownMetaEvent
        else:
            cls = EventRegistry.MetaEvents[metacommand]
        length = read_varlen(track_iter)
        data = [next(track_iter) for x in range(length)]
        return cls(tick=tick, data=data, metacommand=metacommand)
        


    def parse_midi_event(self, tick, header_byte, track_iter):
        '''
        Parse and return a standard MIDI event
        '''
        key = header_byte & 0xF0
        # if this key isn't an event, it's data for an event of
        # the same time we just parsed
        if key not in EventRegistry.Events:
            assert self.running_status, 'Bad byte value'
            data = []
            key = self.running_status & 0xF0
            cls = EventRegistry.Events[key]
            channel = self.running_status & 0xF
            data.append(header_byte)
            data += [next(track_iter) for x in range(cls.length - 1)]
            return cls(tick=tick, channel=channel, data=data)
        else:
            self.running_status = header_byte
            cls = EventRegistry.Events[key]
            channel = self.running_status & 0xF
            data = [next(track_iter) for x in range(cls.length)]
            return cls(tick=tick, channel=channel, data=data)
        raise Warning("Uknown midi event: " + str(header_byte))

class FileWriter(object):
    def write(self, midifile, pattern):
        self.write_file_header(midifile, pattern)
        for track in pattern:
            self.write_track(midifile, track)

    def write_file_header(self, midifile, pattern):
        # First four bytes are MIDI header
        # TODO: fix resolution shit
        packdata = pack(">LHHH", 6,    
                        pattern.format, 
                        len(pattern),
                        pattern.resolution[0])
        midifile.write(b'MThd%s' % packdata)
            
    def write_track(self, midifile, track):
        buf = b''
        self.running_status = None
        for event in track:
            buf += self.encode_event(event)
        buf = self.encode_track_header(len(buf)) + buf
        midifile.write(buf)

    def encode_track_header(self, trklen):
        return b'MTrk%s' % pack(">L", trklen)

    def encode_event(self, event):
        ret = write_varlen(event.tick)
        # is the event a MetaEvent?
        if isinstance(event, MetaEvent):
            ret += bytes([event.status]) + bytes([event.metacommand])
            ret += write_varlen(len(event.data))
            ret += b''.join(map(lambda x: bytes([x]), event.data))
        # is this event a Sysex Event?
        elif isinstance(event, SysexEvent):
            ret += bytes([0xF0])
            ret += b''.join(map(lambda x: bytes([x]), event.data))
            ret += bytes([0xF7])
        # not a Meta MIDI event or a Sysex event, must be a general message
        elif isinstance(event, Event):
            if not self.running_status or \
                self.running_status.status != event.status or \
                self.running_status.channel != event.channel:
                    self.running_status = event
                    ret += bytes([event.status | event.channel])
            ret += b''.join(map(lambda x: bytes([x]), event.data))
        else:
            raise ValueError("Unknown MIDI Event: " + str(event))
        return ret

def write_midifile(filename, pattern):
    with open(filename, 'wb') as f:
        writer = FileWriter()
        return writer.write(f, pattern)

def read_midifile(filename):
    with open(filename, 'rb') as f:
        reader = FileReader()
        return reader.read(f)
