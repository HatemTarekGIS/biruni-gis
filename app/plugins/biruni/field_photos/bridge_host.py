"""The hidden receiver of the Biruni bridge, in its own process (see bridge.py).

It loads the receiver page from this PC's photo server (https://127.0.0.1, its own
certificate) in an invisible Chromium page and keeps it running; if the page's renderer
dies it loads it again. Biruni ends this process when the photos stop or Biruni ends.

Run by Biruni only:  python.exe -I bridge_host.py   (address in BIRUNI_BRIDGE_URL)
"""
import os
import sys

from PyQt6.QtCore import QCoreApplication, Qt, QTimer, QUrl

QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
from PyQt6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile  # noqa: E402
from PyQt6.QtGui import QGuiApplication  # noqa: E402

URL = QUrl(os.environ.get("BIRUNI_BRIDGE_URL", ""))
if URL.host() != "127.0.0.1":
    sys.exit(2)  # only ever this PC's own photo server

app = QGuiApplication(sys.argv[:1])
profile = QWebEngineProfile()  # nothing kept on disk


class Page(QWebEnginePage):
    def javaScriptConsoleMessage(self, level, message, line, source):  # noqa: N802
        print(f"js: {message} ({line})", file=sys.stderr, flush=True)


page = Page(profile)


def accept(err):
    # the photo server's own certificate - and only on this PC
    if err.url().host() == "127.0.0.1" and err.url().port() == URL.port():
        err.acceptCertificate()
    else:
        err.rejectCertificate()


page.certificateError.connect(accept)
page.renderProcessTerminated.connect(lambda *_a: QTimer.singleShot(2000, lambda: page.load(URL)))
page.loadFinished.connect(lambda ok: ok or QTimer.singleShot(5000, lambda: page.load(URL)))
page.load(URL)
sys.exit(app.exec())
