"""Private IPC regression; no Arrow/database/native decoding in this module."""
import json
import socket
import struct
import threading
import unittest
from quantfoundry import _transport as ipc

class TransportTests(unittest.TestCase):
    def test_frame_roundtrip_and_exact_time_string(self):
        a,b=socket.socketpair()
        parent,child=ipc.Channel(a),ipc.Channel(b)
        frame=ipc.Frame('run',1,'check','ok',{'time_ns':'1767225600000000123'})
        parent.send(frame)
        self.assertEqual(child.receive(),frame)
        parent.close(); child.close()

    def test_unbounded_lengths_rejected_before_allocating(self):
        for head,payload in ((2**32-1,0),(1,2**64-1),(0,0)):
            a,b=socket.socketpair(); channel=ipc.Channel(a,arrow_budget=4096)
            b.sendall(struct.pack('!4sIQ',b'QFD4',head,payload))
            with self.assertRaises(ipc.TransportError):
                channel.receive()
            self.assertTrue(channel.closed); b.close()

    def test_duplicate_fields_unknown_enum_paths_and_run_binding(self):
        for body in (
            b'{"protocol":"qf.ipc.v1","protocol":"qf.ipc.v1"}',
            json.dumps({'protocol':'qf.ipc.v1','run_id':'run','request_id':1,'op':'execute_sql',
                        'status':'request','body':{},'payload_bytes':0}).encode(),
            json.dumps({'protocol':'qf.ipc.v1','run_id':'run','request_id':1,'op':'check',
                        'status':'request','body':{},'payload_bytes':0,'path':'/data'}).encode()):
            a,b=socket.socketpair(); channel=ipc.Channel(a)
            b.sendall(struct.pack('!4sIQ',b'QFD4',len(body),0)+body)
            with self.assertRaises(ipc.TransportError):
                channel.receive()
            b.close()

    def test_cancel_interrupts_wait_and_disconnect_is_cancelled(self):
        a,b=socket.socketpair(); cancelled=threading.Event()
        channel=ipc.Channel(a,cancelled=cancelled.is_set)
        timer=threading.Timer(.05,cancelled.set); timer.start()
        with self.assertRaises(ipc.TransportError) as caught:
            channel.receive()
        self.assertEqual(caught.exception.code,'CANCELLED')
        timer.join(); b.close()
        a,b=socket.socketpair(); b.close(); channel=ipc.Channel(a)
        with self.assertRaises(ipc.TransportError) as caught:
            channel.receive()
        self.assertEqual(caught.exception.code,'CANCELLED')

    def test_response_identity_mismatch_closes_channel(self):
        a,b=socket.socketpair(); client=ipc.Client(ipc.Channel(a),'run')
        server=ipc.Channel(b)
        def reply():
            server.receive()
            server.send(ipc.Frame('other-run',1,'check','ok',{}))
        thread=threading.Thread(target=reply); thread.start()
        with self.assertRaises(ipc.TransportError):
            client.call('check',{})
        thread.join(); server.close()

    def test_error_and_wrong_response_phase_close_endpoint_immediately(self):
        for status,body,code in (
            ('error',dict(code='DATA_CHANGED',operation='data_gateway',message='changed',scope={}), 'DATA_CHANGED'),
            ('request',{},'INVALID_CONTRACT'),
            ('error',dict(code=['invalid'],operation='data_gateway',message='changed',scope={}), 'INVALID_CONTRACT')):
            a,b=socket.socketpair(); client=ipc.Client(ipc.Channel(a),'run'); server=ipc.Channel(b)
            def reply():
                server.receive(); server.send(ipc.Frame('run',1,'check',status,body))
            thread=threading.Thread(target=reply); thread.start()
            with self.assertRaises(ipc.TransportError) as caught:
                client.call('check',{})
            self.assertEqual(caught.exception.code,code)
            self.assertTrue(client.channel.closed)
            thread.join(); server.close()

if __name__=='__main__':
    unittest.main()
