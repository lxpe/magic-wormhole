from __future__ import print_function, absolute_import, unicode_literals
import os, sys
from attr import attrs, attrib
from zope.interface import implementer
from twisted.python import failure
from twisted.internet import defer
from ._interfaces import IWormhole
from .util import bytes_to_hexstr
from .timing import DebugTiming
from .journal import ImmediateJournal
from ._boss import Boss
from ._key import derive_key
from .errors import NoKeyError, WormholeClosed
from .util import to_bytes

# We can provide different APIs to different apps:
# * Deferreds
#   w.when_code().addCallback(print_code)
#   w.send(data)
#   w.when_received().addCallback(got_data)
#   w.close().addCallback(closed)

# * delegate callbacks (better for journaled environments)
#   w = wormhole(delegate=app)
#   w.send(data)
#   app.wormhole_got_code(code)
#   app.wormhole_got_verifier(verifier)
#   app.wormhole_got_version(versions)
#   app.wormhole_receive(data)
#   w.close()
#   app.wormhole_closed()
#
# * potential delegate options
#   wormhole(delegate=app, delegate_prefix="wormhole_",
#            delegate_args=(args, kwargs))

class _WelcomeHandler:
    def __init__(self, url, stderr=sys.stderr):
        self.relay_url = url
        self.stderr = stderr

    def handle_welcome(self, welcome):
        if "motd" in welcome:
            motd_lines = welcome["motd"].splitlines()
            motd_formatted = "\n ".join(motd_lines)
            print("Server (at %s) says:\n %s" %
                  (self.relay_url, motd_formatted), file=self.stderr)

@attrs
@implementer(IWormhole)
class _DelegatedWormhole(object):
    _delegate = attrib()

    def __attrs_post_init__(self):
        self._key = None

    def _set_boss(self, boss):
        self._boss = boss

    # from above

    def allocate_code(self, code_length=2):
        self._boss.allocate_code(code_length)
    def input_code(self):
        return self._boss.input_code()
    def set_code(self, code):
        self._boss.set_code(code)

    ## def serialize(self):
    ##     s = {"serialized_wormhole_version": 1,
    ##          "boss": self._boss.serialize(),
    ##          }
    ##     return s

    def send(self, plaintext):
        self._boss.send(plaintext)

    def derive_key(self, purpose, length):
        """Derive a new key from the established wormhole channel for some
        other purpose. This is a deterministic randomized function of the
        session key and the 'purpose' string (unicode/py3-string). This
        cannot be called until when_verifier() has fired, nor after close()
        was called.
        """
        if not isinstance(purpose, type("")): raise TypeError(type(purpose))
        if not self._key: raise NoKeyError()
        return derive_key(self._key, to_bytes(purpose), length)

    def close(self):
        self._boss.close()

    def debug_set_trace(self, client_name, which="B N M S O K SK R RC L C T",
                        file=sys.stderr):
        self._boss._set_trace(client_name, which, file)

    # from below
    def got_code(self, code):
        self._delegate.wormhole_code(code)
    def got_key(self, key):
        self._delegate.wormhole_key(key)
        self._key = key # for derive_key()
    def got_verifier(self, verifier):
        self._delegate.wormhole_verified(verifier)
    def got_version(self, versions):
        self._delegate.wormhole_version(versions)
    def received(self, plaintext):
        self._delegate.wormhole_received(plaintext)
    def closed(self, result):
        self._delegate.wormhole_closed(result)

@implementer(IWormhole)
class _DeferredWormhole(object):
    def __init__(self):
        self._code = None
        self._code_observers = []
        self._key = None
        self._key_observers = []
        self._verifier = None
        self._verifier_observers = []
        self._versions = None
        self._version_observers = []
        self._received_data = []
        self._received_observers = []
        self._observer_result = None
        self._closed_result = None
        self._closed_observers = []

    def _set_boss(self, boss):
        self._boss = boss

    # from above
    def when_code(self):
        # TODO: consider throwing error unless one of allocate/set/input_code
        # was called first. It's legit to grab the Deferred before triggering
        # the process that will cause it to fire, but forbidding that
        # ordering would make it easier to cause programming errors that
        # forget to trigger it entirely.
        if self._observer_result is not None:
            return defer.fail(self._observer_result)
        if self._code is not None:
            return defer.succeed(self._code)
        d = defer.Deferred()
        self._code_observers.append(d)
        return d

    def when_key(self):
        if self._observer_result is not None:
            return defer.fail(self._observer_result)
        if self._key is not None:
            return defer.succeed(self._key)
        d = defer.Deferred()
        self._key_observers.append(d)
        return d

    def when_verified(self):
        if self._observer_result is not None:
            return defer.fail(self._observer_result)
        if self._verifier is not None:
            return defer.succeed(self._verifier)
        d = defer.Deferred()
        self._verifier_observers.append(d)
        return d

    def when_version(self):
        if self._observer_result is not None:
            return defer.fail(self._observer_result)
        if self._versions is not None:
            return defer.succeed(self._versions)
        d = defer.Deferred()
        self._version_observers.append(d)
        return d

    def when_received(self):
        if self._observer_result is not None:
            return defer.fail(self._observer_result)
        if self._received_data:
            return defer.succeed(self._received_data.pop(0))
        d = defer.Deferred()
        self._received_observers.append(d)
        return d

    def allocate_code(self, code_length=2):
        self._boss.allocate_code(code_length)
    def input_code(self):
        return self._boss.input_code()
    def set_code(self, code):
        self._boss.set_code(code)

    # no .serialize in Deferred-mode
    def send(self, plaintext):
        self._boss.send(plaintext)

    def derive_key(self, purpose, length):
        """Derive a new key from the established wormhole channel for some
        other purpose. This is a deterministic randomized function of the
        session key and the 'purpose' string (unicode/py3-string). This
        cannot be called until when_verified() has fired, nor after close()
        was called.
        """
        if not isinstance(purpose, type("")): raise TypeError(type(purpose))
        if not self._key: raise NoKeyError()
        return derive_key(self._key, to_bytes(purpose), length)

    def close(self):
        # fails with WormholeError unless we established a connection
        # (state=="happy"). Fails with WrongPasswordError (a subclass of
        # WormholeError) if state=="scary".
        if self._closed_result:
            return defer.succeed(self._closed_result) # maybe Failure
        d = defer.Deferred()
        self._closed_observers.append(d)
        self._boss.close() # only need to close if it wasn't already
        return d

    def debug_set_trace(self, client_name, which="B N M S O K SK R RC L C T",
                        file=sys.stderr):
        self._boss._set_trace(client_name, which, file)

    # from below
    def got_code(self, code):
        self._code = code
        for d in self._code_observers:
            d.callback(code)
        self._code_observers[:] = []
    def got_key(self, key):
        self._key = key # for derive_key()
        for d in self._key_observers:
            d.callback(key)
        self._key_observers[:] = []
    def got_verifier(self, verifier):
        self._verifier = verifier
        for d in self._verifier_observers:
            d.callback(verifier)
        self._verifier_observers[:] = []
    def got_version(self, versions):
        self._versions = versions
        for d in self._version_observers:
            d.callback(versions)
        self._version_observers[:] = []

    def received(self, plaintext):
        if self._received_observers:
            self._received_observers.pop(0).callback(plaintext)
            return
        self._received_data.append(plaintext)

    def closed(self, result):
        #print("closed", result, type(result))
        if isinstance(result, Exception):
            self._observer_result = self._closed_result = failure.Failure(result)
        else:
            # pending w.key()/w.verify()/w.version()/w.read() get an error
            self._observer_result = WormholeClosed(result)
            # but w.close() only gets error if we're unhappy
            self._closed_result = result
        for d in self._code_observers:
            d.errback(self._observer_result)
        for d in self._key_observers:
            d.errback(self._observer_result)
        for d in self._verifier_observers:
            d.errback(self._observer_result)
        for d in self._version_observers:
            d.errback(self._observer_result)
        for d in self._received_observers:
            d.errback(self._observer_result)
        for d in self._closed_observers:
            d.callback(self._closed_result)


def create(appid, relay_url, reactor, # use keyword args for everything else
           versions={},
           delegate=None, journal=None, tor_manager=None,
           timing=None, welcome_handler=None,
           stderr=sys.stderr):
    timing = timing or DebugTiming()
    side = bytes_to_hexstr(os.urandom(5))
    journal = journal or ImmediateJournal()
    if not welcome_handler:
        welcome_handler = _WelcomeHandler(relay_url).handle_welcome
    if delegate:
        w = _DelegatedWormhole(delegate)
    else:
        w = _DeferredWormhole()
    wormhole_versions = {} # will be used to indicate Wormhole capabilities
    wormhole_versions["app_versions"] = versions # app-specific capabilities
    b = Boss(w, side, relay_url, appid, wormhole_versions,
             welcome_handler, reactor, journal,
             tor_manager, timing)
    w._set_boss(b)
    b.start()
    return w

## def from_serialized(serialized, reactor, delegate,
##                     journal=None, tor_manager=None,
##                     timing=None, stderr=sys.stderr):
##     assert serialized["serialized_wormhole_version"] == 1
##     timing = timing or DebugTiming()
##     w = _DelegatedWormhole(delegate)
##     # now unpack state machines, including the SPAKE2 in Key
##     b = Boss.from_serialized(w, serialized["boss"], reactor, journal, timing)
##     w._set_boss(b)
##     b.start() # ??
##     raise NotImplemented
##     # should the new Wormhole call got_code? only if it wasn't called before.
