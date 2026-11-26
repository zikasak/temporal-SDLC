"""A loopback HTTP server that stands in for a provider or exporter in a test."""
import threading
from contextlib import contextmanager
from http.server import ThreadingHTTPServer


@contextmanager
def loopback_server(handler):
    """An HTTP server on a free loopback port, answering with `handler` on its own thread
    until the block ends."""
    server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
    # serve_forever notices shutdown() only between polls: at its default 0.5 seconds,
    # stopping the server would add up to half a second to every test that starts one.
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01},
                              daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
