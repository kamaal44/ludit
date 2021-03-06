from common import util
from common.log import logger as log
from common import datapacket
from server import group
import time


class PlaySequencer(util.Base):
    """ The playsequencer host the groups as defined in the configuration and makes it
        possible to e.g. send messages to all groups (and devices) in one go.
        It also maintain a list of currently online groups.
    """
    signals = ('allgroupsdisconnected')
    m_state = group.State.STOPPED
    m_buffer_count = 0
    groups = {}
    connected_groups = []

    def __init__(self, jsn):
        self.jsn = jsn
        for group_jsn in jsn['groups']:
            group_name = group_jsn['general']['name']
            log.debug('playsequencer, adding group "%s"' % group_name)

            streaming = jsn['streaming']
            self.audio_timeout = float(streaming['audiotimeout'])
            group_jsn['streaming'] = streaming

            _group = group.Group(group_jsn)
            _group.connect('groupconnected', self.slot_group_connected)
            _group.connect('groupdisconnected', self.slot_group_disconnected)
            _group.connect('status', self.slot_group_status)
            self.groups.update({group_name: _group})
            if not group_jsn['general']['enabled']:
                log.info(' - group %s is currently disabled' % group_name)

        self.starvation_pause = False
        self.client_buffering_ready = False
        log.info('playsequencer ready with the groups %s' % ', '.join([g for g in self.groups.keys()]))

    def terminate(self):
        for _group in self.groups.values():
            _group.terminate()

    def slot_group_connected(self, group):
        log.debug('group %s connected' % group.name())
        self.connected_groups.append(group)

    def slot_group_disconnected(self, group):
        try:
            self.connected_groups.remove(group)
            log.info('group %s disconnected' % group.name())
        except:
            log.warning(f'internal error while group {group.name()} disconnecting')
        if not self.connected_groups:
            log.info('all groups disconnected, no connected devices left')
            self.emit('allgroupsdisconnected')

    def slot_group_status(self, state):
        if state in ('buffered', 'starved'):
            if self.m_state == group.State.BUFFERING and state == 'buffered':
                log.info('------ playing -------')
                self.state_changed(group.State.PLAYING)
            elif self.m_state == group.State.PLAYING and state == 'starved':
                log.info('------ pause ---------')
                self.state_changed(group.State.STOPPED)

    def current_configuration(self):
        config = {}
        groups = []
        for _group in self.groups.values():
            groups.append(_group.get_configuration())

        config['groups'] = groups
        config['streaming'] = self.jsn['streaming']
        config['version'] = self.jsn['version']
        return config

    def send_to_groups(self, packet):
        for _group in self.playing_groups():
            _group.send(packet)

    def set_codec(self, codec):
        self.send_to_groups({'command': 'setcodec', 'codec': codec})

    def set_volume(self, volume):
        self.send_to_groups({'command': 'setvolume', 'volume': volume})

    def broadcast(self, command, value):
        self.send_to_groups({'command': command, 'value': value})

    def get_group(self, groupname):
        return self.groups[groupname]

    def set_state(self, state):
        if state == 'stop':
            self.state_changed(group.State.STOPPED)
        else:
            log.warning('illegal state %s' % state)

    def state_changed(self, new_state):
        global AudioQueue
        if self.m_state != new_state:
            log.debug('state changing from %s to %s' % (self.m_state, new_state))
            self.m_state = new_state
            now = time.time()
            play_groups = self.playing_groups()

            for _group in play_groups:
                if new_state == group.State.BUFFERING:
                    _group.send({'runtime': {'command': 'buffering'}})
                elif new_state == group.State.STOPPED:
                    _group.stop_playing()
                elif new_state == group.State.PLAYING:
                    _group.start_playing(now)
                else:
                    log.critical('internal error #0082')
        else:
            log.debug('ignoring a state change request from %s to %s' % (self.m_state, new_state))

    def playing_groups(self):
        return [group for group in self.groups.values() if group.ready_to_play()]

    def new_audio(self, audio):
        new_state = self.m_state

        if self.m_state == group.State.STOPPED:
            self.m_buffer_count = 0
            new_state = group.State.BUFFERING
            self.client_buffering_ready = False

        if new_state != self.m_state:
            self.state_changed(new_state)

        self.send_to_groups(datapacket.pack_audio(audio))
