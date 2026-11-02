"""Policy tests use real broker operations with an in-process test transport.

Production still requires the Unix socket. This fixture does not establish
installed/socket acceptance in a sandbox that refuses bind(2).
"""
from contextlib import contextmanager
import time
import uuid
from unittest import mock
from agent_pooper_scooper import runner
from agent_pooper_scooper.authority import ProducerClient


@contextmanager
def inprocess_broker():
    current = {}
    class Server:
        def __init__(self, owner, path):
            self.socket_path = path
            current[path] = owner
        def __enter__(self):
            return self
        def __exit__(self, *args):
            current.pop(self.socket_path)
    def request(client, method, params, request_id=None):
        return current[client.socket_path].producer_call(client.credentials, method, params,
                request_id=request_id or uuid.uuid4().hex, deadline=time.monotonic() + 2)
    with mock.patch.object(runner, 'AuthorityServer', Server), mock.patch.object(ProducerClient, 'request', request):
        yield
