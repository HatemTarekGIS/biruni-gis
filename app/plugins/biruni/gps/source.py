"""NMEA sources: a phone on the same Wi-Fi (it sends to this PC over UDP/TCP,
or this PC connects to it over TCP) or a Bluetooth / USB serial port (phones
with a Bluetooth GPS app, external GNSS receivers)."""
from qgis.PyQt.QtCore import QObject, QTimer, pyqtSignal
from qgis.PyQt.QtNetwork import QAbstractSocket, QHostAddress, QNetworkInterface, QTcpServer, QTcpSocket, QUdpSocket

from .nmea import NmeaParser

DEFAULT_PORT = 10110  # IANA port for NMEA 0183 over IP

MODE_LISTEN = "listen"    # phone sends to this PC
MODE_CONNECT = "connect"  # this PC connects to the phone
MODE_SERIAL = "serial"    # Bluetooth / USB COM port
MODE_BROWSER = "browser"  # the phone's web browser (QR code, no app)


def local_addresses():
    """IPv4 addresses of this PC on the local network (best first)."""
    out = []
    for a in QNetworkInterface.allAddresses():
        if a.protocol() != QAbstractSocket.NetworkLayerProtocol.IPv4Protocol or a.isLoopback():
            continue
        ip = a.toString()
        if ip.startswith("169.254."):
            continue
        rank = 0 if ip.startswith("192.168.") else 1 if ip.startswith("10.") else 2
        out.append((rank, ip))
    return [ip for _, ip in sorted(out)]


def serial_ports():
    try:
        from qgis.PyQt.QtSerialPort import QSerialPortInfo
    except ImportError:
        return []
    return [(p.portName(), p.description()) for p in QSerialPortInfo.availablePorts()
            if not p.portName().startswith("NULL")]


class NmeaSource(QObject):
    fixReceived = pyqtSignal(object)
    status = pyqtSignal(str, bool)       # message, connected
    rawReceived = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.parser = NmeaParser()
        self.mode = None
        self._udp = self._server = self._client = self._serial = None
        self._clients = []
        self._target = None
        self._retry = QTimer(self)
        self._retry.setInterval(3000)
        self._retry.timeout.connect(self._reconnect)
        self.bytes = 0
        self.browser = None
        self._poll = QTimer(self)
        self._poll.setInterval(250)
        self._poll.timeout.connect(self._read_browser)

    # ---- start / stop --------------------------------------------------
    def listen(self, port=DEFAULT_PORT):
        self.stop()
        self.mode = MODE_LISTEN
        self._udp = QUdpSocket(self)
        ok_udp = self._udp.bind(QHostAddress(QHostAddress.SpecialAddress.AnyIPv4), port,
                                QUdpSocket.BindFlag.ShareAddress | QUdpSocket.BindFlag.ReuseAddressHint)
        self._udp.readyRead.connect(self._read_udp)
        self._server = QTcpServer(self)
        ok_tcp = self._server.listen(QHostAddress(QHostAddress.SpecialAddress.AnyIPv4), port)
        self._server.newConnection.connect(self._accept)
        if not (ok_udp or ok_tcp):
            self.status.emit(f"port {port} is busy", False)
            self.stop()
            return False
        self.status.emit("listening", True)
        return True

    def connect_to(self, host, port=DEFAULT_PORT):
        self.stop()
        self.mode = MODE_CONNECT
        self._target = (host, port)
        self._client = QTcpSocket(self)
        self._client.readyRead.connect(lambda s=self._client: self._read_stream(s))
        self._client.connected.connect(lambda: self.status.emit("connected", True))
        self._client.disconnected.connect(lambda: (self.status.emit("disconnected", False), self._retry.start()))
        self._client.errorOccurred.connect(lambda *_: (self.status.emit("unreachable", False), self._retry.start()))
        self._client.connectToHost(host, port)
        return True

    def open_serial(self, port_name, baud=9600):
        self.stop()
        try:
            from qgis.PyQt.QtSerialPort import QSerialPort
        except ImportError:
            self.status.emit("serial ports are not available", False)
            return False
        self.mode = MODE_SERIAL
        self._serial = QSerialPort(port_name, self)
        self._serial.setBaudRate(baud)
        if not self._serial.open(QSerialPort.OpenModeFlag.ReadOnly):
            self.status.emit(self._serial.errorString(), False)
            self.stop()
            return False
        self._serial.readyRead.connect(lambda s=self._serial: self._read_stream(s))
        self.status.emit("connected", True)
        return True

    def serve_browser(self, cert_folder, page_texts, lang="en", port=None):
        """Serve the HTTPS location page; returns the URLs (best first) or None."""
        from .browser import BROWSER_PORT, BrowserServer

        self.stop()
        try:
            self.browser = BrowserServer(cert_folder, page_texts, lang, port or BROWSER_PORT, local_addresses())
        except OSError as e:
            self.status.emit(f"port {port or BROWSER_PORT} is busy ({e})", False)
            return None
        except ImportError as e:
            self.status.emit(str(e), False)
            return None
        self.mode = MODE_BROWSER
        self._poll.start()
        self.status.emit("listening", True)
        return [self.browser.url(ip) for ip in local_addresses()] or [self.browser.url("127.0.0.1")]

    def _read_browser(self):
        if self.browser is None:
            return
        got = False
        while not self.browser.positions.empty():
            self.fixReceived.emit(self.browser.positions.get_nowait())
            got = True
        if got:
            self.rawReceived.emit()

    def stop(self):
        self._retry.stop()
        self._poll.stop()
        if self.browser is not None:
            try:
                self.browser.stop()
            except OSError:
                pass
            self.browser = None
        for obj in [self._udp, self._server, self._client, self._serial] + self._clients:
            if obj is None:
                continue
            try:
                obj.close()
                obj.deleteLater()
            except RuntimeError:
                pass
        self._udp = self._server = self._client = self._serial = None
        self._clients = []
        was = self.mode
        self.mode = None
        self.parser = NmeaParser()
        if was:
            self.status.emit("stopped", False)

    def is_active(self):
        return self.mode is not None

    # ---- reading -------------------------------------------------------
    def _accept(self):
        while self._server and self._server.hasPendingConnections():
            s = self._server.nextPendingConnection()
            s.readyRead.connect(lambda s=s: self._read_stream(s))
            s.disconnected.connect(lambda s=s: self._clients.remove(s) if s in self._clients else None)
            self._clients.append(s)
            self.status.emit("phone connected", True)

    def _reconnect(self):
        if self.mode == MODE_CONNECT and self._client is not None and \
                self._client.state() == QAbstractSocket.SocketState.UnconnectedState:
            self._client.connectToHost(*self._target)

    def _read_udp(self):
        while self._udp is not None and self._udp.hasPendingDatagrams():
            dg = self._udp.receiveDatagram()
            self._handle(bytes(dg.data()))

    def _read_stream(self, dev):
        try:
            self._handle(bytes(dev.readAll()))
        except RuntimeError:
            pass

    def _handle(self, data):
        if not data:
            return
        self.bytes += len(data)
        self.rawReceived.emit()
        for fix in self.parser.feed(data.decode("ascii", errors="ignore")):
            self.fixReceived.emit(fix)
