"""Private endpoint selection and bounded, receive-only RTC signaling."""
import json
import unittest
from unittest.mock import Mock, patch

from media_importer.preview_rtc import endpoint, signaling
from media_importer.preview_access import media_request
from media_importer.model import ConfigError

OFFER = ('v=0\r\no=- 1 2 IN IP4 127.0.0.1\r\ns=-\r\nt=0 0\r\n'
         'm=video 9 UDP/TLS/RTP/SAVPF 96\r\na=recvonly\r\na=rtpmap:96 H264/90000\r\n')


class SignalingTests(unittest.TestCase):
    def test_offer_and_candidates_are_only_available_for_rtc_sessions(self):
        packet=json.dumps({'type':'webrtc/offer','value':OFFER})
        with self.assertRaises(ValueError):media_request(packet)
        self.assertEqual(OFFER,json.loads(media_request(packet,webrtc=True))['value'])
        for value in ('','candidate:1 1 UDP 123 100.64.0.10 5555 typ host',
                      'candidate:2 1 tcp 123 2001:db8::1 5555 typ host tcptype active',
                      'candidate:3 1 udp 123 abc-123.local 5555 typ host'):
            signaling('webrtc/candidate',value)

    def test_viewer_cannot_publish_or_add_other_media_or_ice_server_settings(self):
        for value in (OFFER.replace('recvonly','sendonly'),OFFER.replace('recvonly','sendrecv'),
                      OFFER+'m=audio 9 UDP/TLS/RTP/SAVPF 0\r\na=recvonly\r\n',
                      OFFER.replace('m=video','m=application'),OFFER.replace('a=recvonly\r\n',''),
                      {'sdp':OFFER,'ice_servers':[{'urls':'stun:untrusted.example'}]},'x'*24001):
            with self.assertRaises(ValueError):signaling('webrtc/offer',value)
        for kind in ('webrtc','config','camera','unknown'):
            with self.assertRaises(ValueError):media_request(json.dumps({'type':kind,'value':OFFER}),webrtc=True)

    def test_candidates_cannot_contain_urls_or_extra_lines(self):
        for value in ('http://example.org','candidate:1 1 UDP 123 example.org 5555 typ host',
                      'candidate:1 1 UDP 123 127.0.0.1 70000 typ host',
                      'candidate:1 1 UDP 123 127.0.0.1 5555 typ host\r\na=sendrecv'):
            with self.assertRaises(ValueError):signaling('webrtc/candidate',value)


class EndpointTests(unittest.TestCase):
    def probe(self,config,info=None,failed_bind=False):
        sock=Mock();sock.__enter__=Mock(return_value=sock);sock.__exit__=Mock(return_value=False)
        if failed_bind:sock.bind.side_effect=OSError('unavailable')
        with patch('media_importer.preview_rtc.environment',return_value=config), \
                patch('media_importer.preview_rtc.shutil.which',return_value='/fixture/tailscale'), \
                patch('media_importer.preview_rtc.subprocess.run',return_value=Mock(stdout=json.dumps(info))) as run, \
                patch('media_importer.preview_rtc.socket.socket',return_value=sock):
            result=endpoint(config.get('PREVIEW_PUBLIC_URL', ''))
            return result,run,sock

    def test_local_preview_never_binds_other_interfaces(self):
        result,run,sock=self.probe({})
        self.assertEqual('127.0.0.1:18555',result['listen']);self.assertEqual([],result['ice_servers'])
        run.assert_not_called();self.assertEqual(2,sock.bind.call_count)

    def test_remote_preview_only_binds_own_tailnet_ip(self):
        info={'BackendState':'Running','Self':{'TailscaleIPs':['fd7a:115c:a1e0::1','100.64.0.1']}}
        result,run,sock=self.probe({'PREVIEW_PUBLIC_URL':'https://preview.example.ts.net','PREVIEW_RTC_PORT':'18556'},info)
        self.assertEqual('100.64.0.1:18556',result['listen'])
        self.assertEqual(['100.64.0.1'],result['filters']['candidates'])
        self.assertEqual(['status','--json'],run.call_args.args[0][-2:])
        self.assertTrue(run.call_args.kwargs['env']['TERM'])

    def test_missing_or_offline_tailnet_and_busy_port_use_hls(self):
        config={'PREVIEW_PUBLIC_URL':'https://preview.example.ts.net'}
        for info in ({},{'BackendState':'Stopped'},{'BackendState':'Running','Self':{'TailscaleIPs':['192.168.1.1']}}):
            self.assertIsNone(self.probe(config,info)[0])
        self.assertIsNone(self.probe({},failed_bind=True)[0])

    def test_invalid_port_is_configuration_error(self):
        for value in ('80','65536','not-a-port'):
            with self.assertRaises(ConfigError):self.probe({'PREVIEW_RTC_PORT':value})
