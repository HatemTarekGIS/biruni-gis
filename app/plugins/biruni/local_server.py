"""The HTTP server class every local server of Biruni uses (field photos, live share, browser GPS, team room).

Python's HTTPServer turns SO_REUSEADDR on. On Windows that lets a second server bind a port another one is
still listening on, and connections then reach either of them at random - two Biruni windows sharing at once,
or another program, would silently take each other's phones. Here the port is held exclusively: a busy port
is an error the caller can show (or a reason to try the next one).
"""
import socket
import sys
from http.server import ThreadingHTTPServer


class ExclusiveServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def server_bind(self):
        if sys.platform == "win32" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()
