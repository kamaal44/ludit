from common.log import logger as log
from common import util
from enum import Enum
import time
import gi
gi.require_version('Gst', '1.0')
from gi.repository import GObject, Gst
from client import cecaudio

GObject.threads_init()
Gst.init(None)


class Channel(Enum):
    LEFT = 0
    RIGHT = 1
    STEREO = 2


class Band(Enum):
    LOW = 0,
    HIGH = 1


LOG_FIRST_AUDIO_COUNT = 5
LOCAL_CEC_DEFAULT_VOLUME = 0.05


class Pipeline(util.Base):
    signals = 'status'

    pipeline = None
    appsrc_element = None
    last_queue = None
    default_buffer_size = 100000        # can be overruled from configuration file
    log_first_audio = LOG_FIRST_AUDIO_COUNT

    source_gain = 1.0
    codec = 'pcm'
    remote_streaming_volumes = [0.0, 0.0]           # will incorporate any balance offsets
    local_cec_volume = LOCAL_CEC_DEFAULT_VOLUME
    cec_muted = False
    cec = None

    balance = 0.0
    highlowbalance = 0.0
    xoverfreq = 1000.0
    xoverpoles = 4
    eq_bands = 10
    eq_band_gain = [0.0] * eq_bands
    user_volume = 0.3
    channel_list = []
    alsa_hw_device = {'0': '', '1': ''}     # The default empty will be using default alsa device
    channel = False                         # class Channel

    stereo_enhance_depth = 0.0
    stereo_enhance_enabled = False
    noise_gate_level_db = None
    noise_gate_duration_secs = None

    def __init__(self, cec_controlled=True):
        if cec_controlled:
            self.cec = cecaudio.CECAudio(self.cec_callback)
        pass

    def terminate(self):
        if self.cec:
            self.cec.terminate()
        self.stop_pipeline()

    def has_pipeline(self):
        return self.pipeline is not None

    def get_buffer_values(self):
        try:
            return self.current_buffer_size, int(self.last_queue.get_property('current-level-bytes'))
        except:
            return self.current_buffer_size, 0

    def realtime_enabled(self):
        return self.noise_gate_level_db is not None

    def set_play_time(self, play_time_ns):
        try:
            self.pipeline.set_base_time(
                self.pipeline.get_pipeline_clock().get_time()
                + play_time_ns
                - time.time() * util.NS_IN_SEC)
        except Exception as e:
            log.error(f'got fatal exception {e}')
            log.error(f'{self.pipeline.get_pipeline_clock().get_time()} + {play_time_ns} - {time.time() * util.NS_IN_SEC}')
            raise e

    def delayed_start(self):
        self.pipeline.set_start_time(Gst.CLOCK_TIME_NONE)
        self.pipeline.set_state(Gst.State.PLAYING)

    def bus_message(self, bus, message):
        if message.type == Gst.MessageType.EOS:
            log.debug('EOS')
        elif message.type == Gst.MessageType.ERROR:
            err, deb = message.parse_error()
            log.critical("pipeline error: %s '%s'" % (err, deb))
            self.emit('status', 'pipeline_error')
        elif message.type == Gst.MessageType.STATE_CHANGED:
            old_state, new_state, pending_state = message.parse_state_changed()
            if message.src == self.pipeline:
                log.debug('* pipeline state changed to %s' % Gst.Element.state_get_name(new_state))

    def cutter_message(self, bus, message):
        try:
            if message.has_name("cutter"):
                above = message.get_structure().get_value('above')
                if above:
                    self.emit('status', 'rt_play')
                else:
                    self.emit('status', 'rt_stop')

        except Exception as e:
            log.critical('[%s] parsing cutter message gave "%s"' % (self.name, str(e)))

    def get_pipeline_volume(self, channel, realtime):
        if realtime:
            return self.local_cec_volume
        else:
            return self.remote_streaming_volumes[int(channel)]

    def get_channel_mask(self, band: Band, channel: int):
        """
        If playing stereo on a single soundcard then the 4 output channels
        needs to know where they should be located 'on the soundcard'. This
        is done by setting a gstreamer 'channel-mask' on each of the 4 channels.
        """
        if self.channel == Channel.STEREO and self.alsa_hw_device[0] == self.alsa_hw_device[1]:
            bitmasks = (0x01, 0x02, 0x10, 0x20)
            mask = bitmasks[channel * 2 + band.value]
            return 'audioconvert ! audio/x-raw,channels=1,channel-mask=(bitmask)%02x !' % mask
        return ''

    def construct_pipeline(self, realtime=False):
        if self.pipeline:
            self.pipeline.set_state(Gst.State.NULL)

        if self.codec == 'sbc':
            decoding = 'sbcparse ! sbcdec !'
            self.current_buffer_size = self.default_buffer_size
            self.source_gain = 3.0
        elif self.codec == 'aac':
            # decoding = 'aacparse ! avdec_aac !'
            decoding = 'decodebin !'
            self.current_buffer_size = self.default_buffer_size
            self.source_gain = 0.5
        elif self.codec == 'aac_adts':
            # audio generated with gstreamer "faac ! aacparse ! avmux_adts"
            decoding = 'decodebin !'
            self.current_buffer_size = self.default_buffer_size
            self.source_gain = 0.5
        elif self.codec == 'pcm':
            self.current_buffer_size = 200000
            decoding = 'decodebin !'
            self.source_gain = 1.0
        else:
            log.critical("unknown codec '%s'" % self.codec)

        try:
            lo, hi = self.calculate_highlowbalance(self.highlowbalance)
            self.set_volume(None)

            stereo_enhance_element = ''
            if self.stereo_enhance_enabled:
                stereo_enhance_element = 'audioconvert ! stereo stereo=%f ! ' % self.stereo_enhance_depth

            if realtime:
                pipeline = (
                    'alsasrc device=hw:0 ! '
                    'cutter name=cutter leaky=false run-length=%i threshold-dB=%f ! '
                    'audioconvert ! audio/x-raw,format=F32LE,channels=2 ! queue ! deinterleave name=d ' %
                    (self.noise_gate_duration_secs * util.NS_IN_SEC, self.noise_gate_level_db))
                buffer_time = 100
            else:
                pipeline = (
                    'appsrc name=audiosource emit-signals=true max-bytes=%i ! %s %s '
                    'audioconvert ! audio/x-raw,format=F32LE,channels=2 ! queue ! deinterleave name=d ' %
                    (self.current_buffer_size, decoding, stereo_enhance_element))
                buffer_time = 200000

            for channel in self.channel_list:

                if self.channel == Channel.STEREO and self.alsa_hw_device['0'] != self.alsa_hw_device['1']:
                    alsa_device = self.alsa_hw_device[channel]
                else:
                    # left, right and single soundcard stereo will only use the first single alsa device.
                    alsa_device = self.alsa_hw_device['0']

                log.debug('pipeline channel %s playing in alsa device "%s"' %
                          (channel, alsa_device if alsa_device else 'default'))

                lo_mask = self.get_channel_mask(Band.LOW, int(channel))
                hi_mask = self.get_channel_mask(Band.HIGH, int(channel))

                try:
                    eq_band_gains = ''.join(
                        ['band%i=%f ' % (band, self.eq_band_gain[band]) for band in range(self.eq_bands)])
                    eq_setup = 'equalizer-10bands name=equalizer%s %s ' % (channel, eq_band_gains)
                except Exception as e:
                    log.error('equalizer setup failed with %s' % str(e))
                    eq_setup = ''

                pipeline += (
                    'd.src_%s ! tee name=t%s '

                    'interleave name=i%s ! capssetter caps = audio/x-raw,channels=2,channel-mask=0x3 ! '
                    'audioconvert ! audioresample ! queue name=lastqueue%s max-size-time=20000000000 ! '
                    'volume name=vol%s volume=%f ! alsasink sync=true %s buffer-time=%d '

                    't%s.src_0 ! queue ! audiocheblimit poles=%i name=lowpass%s mode=low-pass cutoff=%f ! '
                    '%s ! volume name=lowvol%s volume=%f ! %s i%s.sink_0 '

                    't%s.src_1 ! queue ! audiocheblimit poles=%i name=highpass%s mode=high-pass cutoff=%f ! '
                    'volume name=highvol%s volume=%f ! %s i%s.sink_1 ' %
                    (channel, channel,
                     channel,
                     channel,
                     channel, self.get_pipeline_volume(int(channel), realtime), alsa_device, buffer_time,
                     channel, self.xoverpoles, channel, self.xoverfreq, eq_setup, channel, lo, lo_mask, channel,
                     channel, self.xoverpoles, channel, self.xoverfreq, channel, hi, hi_mask, channel))

            # print(pipeline)

            if realtime:
                log.info('launching pipeline (local realtime alsa)')
            else:
                log.info('launching pipeline (server audio stream)')

            self.pipeline = Gst.parse_launch(pipeline)
            if realtime:
                self.appsrc_element = None
            else:
                self.appsrc_element = self.pipeline.get_by_name('audiosource')

            self.last_queue = self.pipeline.get_by_name('lastqueue' + self.channel_list[0])

            bus = self.pipeline.get_bus()
            bus.add_signal_watch()
            bus.enable_sync_message_emission()
            bus.connect('message', self.bus_message)

            self.pipeline.set_state(Gst.State.PAUSED)
            if realtime:
                bus.connect('message::element', self.cutter_message)

        except Exception as e:
            log.critical("couldn't construct pipeline, %s" % str(e))

    def construct_and_start_local_pipeline(self):
        self.construct_pipeline(realtime=True)
        self.pipeline.set_state(Gst.State.PLAYING)

    def stop_pipeline(self):
        if self.pipeline:
            # log.warning('writing pipeline dot file')
            # Gst.debug_bin_to_dot_file(self.pipeline, Gst.DebugGraphDetails.ALL, 'ludit_client')
            self.pipeline.set_state(Gst.State.NULL)
            self.pipeline = None

    def set_volume(self, volume, local_cec=False):
        if local_cec:
            if self.pipeline:
                log.debug('local cec volume %.3f' % volume)
                for channel in self.channel_list:
                    volume_element = self.pipeline.get_by_name('vol%s' % channel)
                    volume_element.set_property('volume', volume)
        else:
            if volume is not None:
                self.user_volume = volume

            for channel in self.channel_list:
                channel_int = int(channel)
                balance = 1.0
                if channel_int == 0 and self.balance > 0.0:
                    balance = 1.0 - self.balance
                elif channel_int == 1 and self.balance < 0.0:
                    balance = 1.0 + self.balance

                channel_vol = max(0.0005, self.user_volume * self.source_gain * balance)
                self.remote_streaming_volumes[channel_int] = channel_vol
                if self.pipeline:
                    log.debug('channel %s volume %.3f' % (channel, channel_vol))
                    volume_element = self.pipeline.get_by_name('vol%s' % channel)
                    volume_element.set_property('volume', channel_vol)

    def set_balance(self, balance):
        self.balance = balance
        log.debug('setting balance %.2f' % self.balance)
        self.set_volume(None)

    def calculate_highlowbalance(self, highlowbalance):
        self.highlowbalance = highlowbalance
        lowvol = 1.0
        highvol = 1.0
        if self.highlowbalance > 0.0:
            lowvol -= self.highlowbalance
        elif self.highlowbalance < 0.0:
            highvol += self.highlowbalance
        return lowvol, highvol

    def process_message(self, message):
        command = message['command']

        if command == 'setcodec':
            self.codec = message['codec']
            log.info("setting codec to '%s'" % self.codec)
            self.construct_pipeline()

        elif command == 'setvolume':
            volume = int(message['volume']) / 127.0
            log.debug('setting volume %.3f' % volume)
            self.set_volume(volume)

        elif command == 'configure' or command == 'setparam':
            param = message['param']
            if param == 'general':
                self.configure_general(param['general'])
            else:
                self.set_pipeline_parameter(param)

        else:
            log.critical("got unknown server command '%s'" % command)

    def set_pipeline_parameter(self, param: dict):
        """
        Parameters will initially be set in two passes, first from reading
        the local client configuration and later from parameters sent by
        the server. Both these passes will predate the construction of the
        first pipeline.
        Later calls (e.g. volume) will see the value stored and also applied
        on any running pipeline immediately.
        """
        try:
            # the channel identifier from the server configuration, right, left or stereo
            channel_name = param['channel']
            self.channel = Channel[channel_name.upper()]
            if self.channel:
                log.info('device is configuring as "%s"' % self.channel)
                self.channel_list = []
                if self.channel == Channel.LEFT or self.channel == Channel.STEREO:
                    self.channel_list.append('0')
                if self.channel == Channel.RIGHT or self.channel == Channel.STEREO:
                    self.channel_list.append('1')
        except:
            pass

        alsa = param.get('alsa')
        if alsa:
            alsa_device = alsa['devices'][0]
            self.alsa_hw_device['0'] = 'device=%s' % alsa_device
            log.debug('first alsa device is %s' % alsa_device)

            try:
                alsa_device = alsa['devices'][1]
                self.alsa_hw_device['1'] = 'device=%s' % alsa_device
                log.debug('second alsa device is %s' % alsa_device)
            except:
                log.info('no second alsa device found, using alsa device "%s" for all outputs' % self.alsa_hw_device['0'])

        levels = param.get('levels')
        if levels:
            volume = levels.get('volume')
            if volume is not None:
                volume = float(volume) / 100.0
                log.debug('setting volume %.3f' % volume)
                self.set_volume(volume)

            balance = levels.get('balance')
            if balance is not None:
                self.set_balance(float(balance) / 100.0)

            equalizer = levels.get('equalizer')
            if equalizer is not None:
                """
                Center frequencies 29 59 119 237 474 947 1889 3770 7523 15011
                """
                for band in range(self.eq_bands):
                    att = equalizer.get('%i' % band)
                    if att:
                        self.eq_band_gain[band] = float(att)
                        log.debug('setting equalizer band %i to %f' % (band, self.eq_band_gain[band]))
                        if self.pipeline:
                            for channel in self.channel_list:
                                eq = self.pipeline.get_by_name('equalizer' + channel)
                                eq.set_property('band%i' % band, self.eq_band_gain[band])

        stereo_enhance = param.get('stereoenhance')
        if stereo_enhance:
            stereo_enhance_depth = stereo_enhance.get('depth')
            if stereo_enhance_depth:
                log.debug('setting stereoenhance depth %s' % stereo_enhance_depth)
                self.stereo_enhance_depth = float(stereo_enhance_depth)

            stereo_enhance_enabled = stereo_enhance.get('enabled')
            if stereo_enhance_enabled:
                log.debug('setting stereoenhance enable %s' % stereo_enhance_enabled)
                self.stereo_enhance_enabled = stereo_enhance_enabled == 'true'

        xover = param.get('xover')
        if xover:
            highlowbalance = xover.get('highlowbalance')
            if highlowbalance is not None:
                highlowbalance = float(highlowbalance)
                lo, hi = self.calculate_highlowbalance(highlowbalance)
                log.debug('setting high/low balance %.2f (low %.5f high %.5f)' %
                          (highlowbalance, lo, hi))
                if self.pipeline:
                    for channel in self.channel_list:
                        self.pipeline.get_by_name('highvol' + channel).set_property('volume', hi)
                        self.pipeline.get_by_name('lowvol' + channel).set_property('volume', lo)

            xoverfreq = xover.get('freq')
            if xoverfreq is not None:
                log.debug('setting xover frequency %s' % xoverfreq)
                self.xoverfreq = float(xoverfreq)
                if self.pipeline:
                    for channel in self.channel_list:
                        xlow = self.pipeline.get_by_name('lowpass' + channel)
                        xlow.set_property('cutoff', self.xoverfreq)
                        xhigh = self.pipeline.get_by_name('highpass' + channel)
                        xhigh.set_property('cutoff', self.xoverfreq)

            xoverpoles = xover.get('poles')
            if xoverpoles:
                log.debug('setting xover poles %s' % xoverpoles)
                self.xoverpoles = int(xoverpoles)
                if self.pipeline:
                    for channel in self.channel_list:
                        xlow = self.pipeline.get_by_name('lowpass' + channel)
                        xlow.set_property('poles', self.xoverpoles)
                        xhigh = self.pipeline.get_by_name('highpass' + channel)
                        xhigh.set_property('poles', self.xoverpoles)

        buffersize = param.get('buffersize')
        if buffersize is not None:
            self.default_buffer_size = int(buffersize)
            log.debug('setting buffersize %i' % self.default_buffer_size)

        realtime = param.get('realtime')
        if realtime is not None and realtime.get('enabled') == 'true':
            self.noise_gate_level_db = float(realtime.get('level_db'))
            self.noise_gate_duration_secs = int(float(realtime.get('duration_sec')))
            log.info('realtime mode enabled, threshold %.1f dB, duration %i secs' %
                     (self.noise_gate_level_db, self.noise_gate_duration_secs))

    def new_audio(self, audio):
        # if the server issued a stop due to a starvation restart then this is a good
        # time to construct a new pipeline
        if not self.pipeline or not self.appsrc_element:
            self.construct_pipeline()

        if self.log_first_audio:
            self.log_first_audio -= 1
            log.debug('received %i bytes audio (%i)' % (len(audio), self.log_first_audio))

        if audio:
            buf = Gst.Buffer.new_allocate(None, len(audio), None)
            buf.fill(0, audio)
            self.appsrc_element.emit('push-buffer', buf)

    def cec_callback(self, action):
        global high_volume, low_volume, cec_audio_control, muted
        if not self.realtime_enabled():
            return
        if action == 'vol_up' and low_volume < 10.0:
            self.cec_muted = False
            self.local_cec_volume *= 1.2
        elif action == 'vol_down' and low_volume > 0.0003:
            self.cec_muted = False
            cec_audio_control = True
            self.local_cec_volume *= 1.0 / 1.2
        elif action == 'mute':
            self.cec_muted = not self.cec_muted

        self.set_volume(self.local_cec_volume if not self.cec_muted else 0.0001, True)
