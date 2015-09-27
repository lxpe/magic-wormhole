import json
from twisted.trial import unittest
from twisted.internet import defer
from twisted.internet.threads import deferToThread
from ..blocking.transcribe import Wormhole as BlockingWormhole, UsageError
from .common import ServerBase

class Blocking(ServerBase, unittest.TestCase):
    # we need Twisted to run the server, but we run the sender and receiver
    # with deferToThread()

    def test_basic(self):
        appid = "appid"
        w1 = BlockingWormhole(appid, self.relayurl)
        w2 = BlockingWormhole(appid, self.relayurl)
        d = deferToThread(w1.get_code)
        def _got_code(code):
            w2.set_code(code)
            d1 = deferToThread(w1.get_data, "data1")
            d2 = deferToThread(w2.get_data, "data2")
            return defer.DeferredList([d1,d2], fireOnOneErrback=False)
        d.addCallback(_got_code)
        def _done(dl):
            ((success1, dataX), (success2, dataY)) = dl
            r1,r2 = dl
            self.assertTrue(success1, dataX)
            self.assertTrue(success2, dataY)
            self.assertEqual(dataX, "data2")
            self.assertEqual(dataY, "data1")
        d.addCallback(_done)
        return d

    def test_fixed_code(self):
        appid = "appid"
        w1 = BlockingWormhole(appid, self.relayurl)
        w2 = BlockingWormhole(appid, self.relayurl)
        w1.set_code("123-purple-elephant")
        w2.set_code("123-purple-elephant")
        d1 = deferToThread(w1.get_data, "data1")
        d2 = deferToThread(w2.get_data, "data2")
        d = defer.DeferredList([d1,d2], fireOnOneErrback=False)
        def _done(dl):
            ((success1, dataX), (success2, dataY)) = dl
            r1,r2 = dl
            self.assertTrue(success1, dataX)
            self.assertTrue(success2, dataY)
            self.assertEqual(dataX, "data2")
            self.assertEqual(dataY, "data1")
        d.addCallback(_done)
        return d

    def test_errors(self):
        appid = "appid"
        w1 = BlockingWormhole(appid, self.relayurl)
        self.assertRaises(UsageError, w1.get_verifier)
        self.assertRaises(UsageError, w1.get_data, "data")
        w1.set_code("123-purple-elephant")
        self.assertRaises(UsageError, w1.set_code, "123-nope")
        self.assertRaises(UsageError, w1.get_code)
        w2 = BlockingWormhole(appid, self.relayurl)
        d = deferToThread(w2.get_code)
        def _done(code):
            self.assertRaises(UsageError, w2.get_code)
        d.addCallback(_done)
        return d

    def test_serialize(self):
        appid = "appid"
        w1 = BlockingWormhole(appid, self.relayurl)
        self.assertRaises(UsageError, w1.serialize) # too early
        w2 = BlockingWormhole(appid, self.relayurl)
        d = deferToThread(w1.get_code)
        def _got_code(code):
            self.assertRaises(UsageError, w2.serialize) # too early
            w2.set_code(code)
            w2.serialize() # ok
            s = w1.serialize()
            self.assertEqual(type(s), type(""))
            unpacked = json.loads(s) # this is supposed to be JSON
            self.assertEqual(type(unpacked), dict)
            new_w1 = BlockingWormhole.from_serialized(s)
            d1 = deferToThread(new_w1.get_data, "data1")
            d2 = deferToThread(w2.get_data, "data2")
            return defer.DeferredList([d1,d2], fireOnOneErrback=False)
        d.addCallback(_got_code)
        def _done(dl):
            ((success1, dataX), (success2, dataY)) = dl
            r1,r2 = dl
            self.assertTrue(success1, dataX)
            self.assertTrue(success2, dataY)
            self.assertEqual(dataX, "data2")
            self.assertEqual(dataY, "data1")
            self.assertRaises(UsageError, w2.serialize) # too late
        d.addCallback(_done)
        return d
    test_serialize.skip = "not yet implemented for the blocking flavor"
