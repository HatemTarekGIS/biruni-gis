"""WhatsApp chat locations to points: the locations a field team sent in a chat,
with who sent them, when, and what they said about them."""
import os
import re
import zipfile
from datetime import datetime
from urllib.parse import unquote

from qgis.core import (Qgis, QgsCoordinateReferenceSystem, QgsFeature, QgsFeatureSink, QgsField, QgsFields,
                       QgsGeometry, QgsPointXY, QgsProcessingException, QgsProcessingParameterDateTime,
                       QgsProcessingParameterEnum, QgsProcessingParameterFeatureSink, QgsProcessingParameterFile,
                       QgsProcessingParameterString)
from qgis.PyQt.QtCore import QDate, QDateTime, QMetaType, QTime

from ..i18n import tr
from ..smart_table import ARABIC_DIGITS, normalise_text, parse_angle
from .base import BiruniAlgorithm

# direction marks and invisible spaces WhatsApp puts around dates, names and links
MARKS = re.compile("[‎‏‪-‮⁦-⁩﻿]")
SPACES = re.compile("[    ]")
_DATE = r"(\d{1,4})[/.\-](\d{1,2})[/.\-](\d{1,4})"
_TIME = r"(\d{1,2})[:.](\d{2})(?:[:.](\d{2}))?\s*([AaPp]\.?\s?[Mm]\.?|ص|م)?"
IOS_RE = re.compile(rf"^\s*\[{_DATE},?\s*{_TIME}\]\s*(.*)$")
ANDROID_RE = re.compile(rf"^\s*{_DATE},?\s*{_TIME}\s*[-–]\s(.*)$")
SENDER_RE = re.compile(r"^([^:]{1,80}?):\s(.*)$", re.S)
URL_RE = re.compile(r"(?:https?://|geo:)\S+", re.I)
SHORT_RE = re.compile(r"https?://(?:maps\.app\.goo\.gl|goo\.gl/maps|g\.co/kgs)/\S*", re.I)
LIVE_RE = re.compile(r"live location|الموقع المباشر|موقعي المباشر|موقع مباشر", re.I)
MEDIA_RE = re.compile(r"<attached:|\(file attached\)|<Media omitted>|image omitted|video omitted|"
                      r"تم استبعاد الوسائط|<الوسائط محذوفة>|الصورة محذوفة|ملف مرفق|\.(?:jpe?g|png|webp|mp4|opus|pdf)\b",
                      re.I)
LINK_RE = re.compile(r"(?:[?&](?:q|query|ll|sll|daddr|destination|center)=(?:loc:)?|@|/place/|/search/)"
                     r"(-?\d+(?:\.\d+)?)\s*,\s*\+?(-?\d+(?:\.\d+)?)")
OSM_RE = re.compile(r"mlat=(-?\d+(?:\.\d+)?)&mlon=(-?\d+(?:\.\d+)?)")
GEO_RE = re.compile(r"geo:(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)")
# decimal degrees typed in a message: at least three decimals, so prices and times are not taken
PAIR_TEXT_RE = re.compile(r"(?<![\d.])(-?\d{1,3}\.\d{3,})\s*[,;\s]\s*(-?\d{1,3}\.\d{3,})(?![\d.])")
DMS_TEXT_RE = re.compile(r"(\d{1,3}\s*°[^°\n]{0,18}?[NSns])[\s,]*(\d{1,3}\s*°[^°\n]{0,18}?[EWew])")
LABEL_RE = re.compile(r"^\s*(?:location|الموقع|موقع)\s*:\s*", re.I)

AUTO, DAY_FIRST, MONTH_FIRST = 0, 1, 2


def clean(line):
    return SPACES.sub(" ", MARKS.sub("", line))


def _header_key(line):
    """Digits and commas as plain ASCII, one character for one, so positions
    found in the key are positions in the line."""
    return line.translate(ARABIC_DIGITS).replace("،", ",").replace("٫", ".")


def read_chat(path):
    """The text of an exported chat (.txt, or the .zip WhatsApp makes)."""
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            names = [n for n in z.namelist() if n.lower().endswith(".txt")]
            if not names:
                raise ValueError("no chat text in the ZIP file")
            names.sort(key=lambda n: (os.path.basename(n) != "_chat.txt", "whatsapp" not in n.lower(), len(n)))
            data = z.read(names[0])
    else:
        with open(path, "rb") as f:
            data = f.read()
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16")
    for enc in ("utf-8-sig", "cp1256"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


def parse_messages(text):
    """[{line, raw_date, time, sender, text}] - continuation lines belong to the
    message above them; system messages have sender ''."""
    messages = []
    for no, raw in enumerate(text.splitlines(), 1):
        line = clean(raw)
        key = _header_key(line)
        m = IOS_RE.match(key) or ANDROID_RE.match(key)
        if m is None:
            if messages and line.strip():
                messages[-1]["text"] += "\n" + line.strip()
            continue
        body = line[m.start(8):]
        sm = SENDER_RE.match(body)
        sender, msg = (sm.group(1).strip(), sm.group(2)) if sm else ("", body)
        ampm = (m.group(7) or "").replace(".", "").replace(" ", "").lower()
        messages.append({"line": no, "date": (int(m.group(1)), int(m.group(2)), int(m.group(3))),
                         "time": (int(m.group(4)), int(m.group(5)), int(m.group(6) or 0), ampm),
                         "sender": sender, "text": msg.strip()})
    return messages


def date_order(messages):
    """'ymd', 'dmy' or 'mdy' from the dates of the chat; None when every date
    could be either (then the caller decides)."""
    day_first = month_first = False
    for m in messages:
        a, b, _c = m["date"]
        if a > 31:
            return "ymd"
        if a > 12:
            day_first = True
        elif b > 12:
            month_first = True
    if day_first and not month_first:
        return "dmy"
    if month_first and not day_first:
        return "mdy"
    return None


def to_datetime(m, order):
    a, b, c = m["date"]
    if order == "ymd":
        y, mo, d = a, b, c
    elif order == "mdy":
        mo, d, y = a, b, c
    else:
        d, mo, y = a, b, c
    if y < 100:
        y += 2000
    h, mi, s, ampm = m["time"]
    if ampm in ("pm", "م") and h < 12:
        h += 12
    elif ampm in ("am", "ص") and h == 12:
        h = 0
    try:
        return datetime(y, mo, d, h, mi, s)
    except ValueError:
        return None


def _valid(lat, lon):
    return -90 <= lat <= 90 and -180 <= lon <= 180 and not (lat == 0 and lon == 0)


def find_location(text):
    """(lat, lon, link, line) of the first position in a message, or
    ('short', link) for a short link that needs the internet, ('live',) for a
    live location, or None."""
    for line in text.splitlines():
        for url in URL_RE.findall(line):
            url = url.rstrip(").,،>]")
            u = unquote(unquote(url))
            for rx in (OSM_RE, GEO_RE, LINK_RE):
                mm = rx.search(u)
                if mm:
                    lat, lon = float(mm.group(1)), float(mm.group(2))
                    if _valid(lat, lon):
                        return lat, lon, url, line.strip()
            if SHORT_RE.match(url):
                return "short", url, line.strip()
        s = normalise_text(line)
        mm = PAIR_TEXT_RE.search(s)
        if mm:
            lat, lon = float(mm.group(1)), float(mm.group(2))
            if _valid(lat, lon):
                return lat, lon, "", line.strip()
        mm = DMS_TEXT_RE.search(s)
        if mm:
            lat, _h = parse_angle(mm.group(1))
            lon, _h = parse_angle(mm.group(2))
            if lat is not None and lon is not None and _valid(lat, lon):
                return lat, lon, "", line.strip()
    if LIVE_RE.search(text):
        return ("live",)
    return None


def _is_plain(m):
    return bool(m["sender"]) and m["text"] and not MEDIA_RE.search(m["text"]) and find_location(m["text"]) is None \
        and not URL_RE.search(m["text"])


def context_for(messages, i):
    """What the sender said about the location: the rest of the location message,
    else the nearest plain text message of the same sender within two messages."""
    m = messages[i]
    rest = []
    for ln in m["text"].splitlines():
        s = normalise_text(URL_RE.sub(" ", ln))
        s = DMS_TEXT_RE.sub(" ", PAIR_TEXT_RE.sub(" ", s))
        s = LABEL_RE.sub("", s).strip(" ,.:;-–()")
        if re.search(r"[^\W\d_]", s):
            rest.append(s)
    if rest:
        return " ".join(rest)
    for off in (1, -1, 2, -2):
        j = i + off
        if 0 <= j < len(messages) and messages[j]["sender"] == m["sender"] and _is_plain(messages[j]):
            return messages[j]["text"]
    return ""


def extract(text, order=AUTO, sender_filter="", date_from=None, date_to=None):
    """Locations of a chat: (points, info). points: [{lat, lon, sender, sent_at,
    message, context, link, line}]; info: counts, short links, live locations,
    date order used."""
    messages = parse_messages(text)
    found = date_order(messages)
    if order == DAY_FIRST:
        used = "dmy" if found != "ymd" else "ymd"
    elif order == MONTH_FIRST:
        used = "mdy" if found != "ymd" else "ymd"
    else:
        used = found or "dmy"
    points, short, live = [], [], []
    needle = (sender_filter or "").strip().lower()
    for i, m in enumerate(messages):
        if needle and needle not in m["sender"].lower():
            continue
        when = to_datetime(m, used)
        if when is not None and ((date_from and when.date() < date_from) or (date_to and when.date() > date_to)):
            continue
        loc = find_location(m["text"])
        if loc is None:
            continue
        if loc[0] == "short":
            short.append((m["line"], m["sender"], loc[1]))
            continue
        if loc[0] == "live":
            live.append((m["line"], m["sender"]))
            continue
        lat, lon, link, line = loc
        points.append({"lat": lat, "lon": lon, "sender": m["sender"], "sent_at": when,
                       "message": LABEL_RE.sub("", line) or line, "context": context_for(messages, i),
                       "link": link, "line": m["line"]})
    info = {"messages": len(messages), "short": short, "live": live, "order": used,
            "order_guessed": found is None and order == AUTO}
    return points, info


class WhatsAppLocations(BiruniAlgorithm):
    """Locations shared in a WhatsApp chat as a point layer."""

    ID = "whatsapplocations"
    NAME = "WhatsApp chat locations to points"
    GROUP = "Engineering office"
    GROUP_ID = "office"
    HELP = ("Turns the locations a field team sent in a WhatsApp chat into points: who sent each one, when, the "
            "location message and what the sender wrote next to it (for example 'pole number 15'). Export the "
            "chat from WhatsApp (chat menu - More - Export chat, with or without media) and choose the .txt or "
            ".zip file. Android and iPhone exports in English or Arabic are understood, with map links (Google "
            "Maps, Apple Maps, OpenStreetMap) and coordinates typed in the messages. Short links "
            "(maps.app.goo.gl) do not contain the position and live locations are not saved in the export: "
            "both are listed in the log so they can be asked for again.")

    INPUT, DATE_ORDER, SENDER, DATE_FROM, DATE_TO, OUTPUT = (
        "INPUT", "DATE_ORDER", "SENDER", "DATE_FROM", "DATE_TO", "OUTPUT")
    ORDERS = ["Automatic", "Day first (31/12/2024)", "Month first (12/31/2024)"]

    def initAlgorithm(self, config=None):  # noqa: N802
        self.addParameter(QgsProcessingParameterFile(
            self.INPUT, tr("Exported WhatsApp chat (.txt or .zip)"),
            fileFilter=tr("WhatsApp chat") + " (*.txt *.zip *.TXT *.ZIP)"))
        self.addParameter(QgsProcessingParameterEnum(
            self.DATE_ORDER, tr("Dates in the chat"), [tr(o) for o in self.ORDERS], defaultValue=0))
        self.addParameter(QgsProcessingParameterString(
            self.SENDER, tr("Only from senders whose name contains"), optional=True))
        self.addParameter(QgsProcessingParameterDateTime(
            self.DATE_FROM, tr("From date"), Qgis.ProcessingDateTimeParameterDataType.Date, optional=True))
        self.addParameter(QgsProcessingParameterDateTime(
            self.DATE_TO, tr("To date"), Qgis.ProcessingDateTimeParameterDataType.Date, optional=True))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUTPUT, tr("Chat locations"), Qgis.ProcessingSourceType.VectorPoint))

    def _date(self, parameters, name, context):
        if parameters.get(name) in (None, ""):
            return None
        d = self.parameterAsDateTime(parameters, name, context)
        if not d.isValid():
            return None
        q = d.date()
        return datetime(q.year(), q.month(), q.day()).date()

    def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
        path = self.parameterAsFile(parameters, self.INPUT, context)
        if not path or not os.path.exists(path):
            raise QgsProcessingException(tr("Choose the exported chat file."))
        try:
            text = read_chat(path)
        except (OSError, ValueError, zipfile.BadZipFile) as e:
            raise QgsProcessingException(tr("The chat could not be read: {e}").format(e=e))
        points, info = extract(text, self.parameterAsEnum(parameters, self.DATE_ORDER, context),
                               self.parameterAsString(parameters, self.SENDER, context),
                               self._date(parameters, self.DATE_FROM, context),
                               self._date(parameters, self.DATE_TO, context))
        if not info["messages"]:
            raise QgsProcessingException(tr("This file does not look like an exported WhatsApp chat."))

        fields = QgsFields()
        for name, kind in (("sender", QMetaType.Type.QString), ("sent_at", QMetaType.Type.QDateTime),
                           ("message", QMetaType.Type.QString), ("context", QMetaType.Type.QString),
                           ("link", QMetaType.Type.QString), ("chat_line", QMetaType.Type.Int)):
            fields.append(QgsField(name, kind))
        sink, dest = self.parameterAsSink(parameters, self.OUTPUT, context, fields, Qgis.WkbType.Point,
                                          QgsCoordinateReferenceSystem("EPSG:4326"))
        for p in points:
            f = QgsFeature(fields)
            f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(p["lon"], p["lat"])))
            when = p["sent_at"]
            qdt = QDateTime(QDate(when.year, when.month, when.day), QTime(when.hour, when.minute, when.second)) \
                if when else None
            f.setAttributes([p["sender"], qdt, p["message"], p["context"], p["link"], p["line"]])
            sink.addFeature(f, QgsFeatureSink.Flag.FastInsert)

        feedback.pushInfo(tr("{m} messages read, {p} locations found.").format(m=info["messages"], p=len(points)))
        if info["order_guessed"]:
            feedback.pushWarning(tr("The dates could be day/month or month/day; read as day first. Choose the "
                                    "order of the dates if they look wrong."))
        if info["short"]:
            feedback.pushWarning(tr("{n} short map links (maps.app.goo.gl) have no position inside them - open "
                                    "them or ask for the location again:").format(n=len(info["short"])))
            for line, sender, link in info["short"][:20]:
                feedback.pushWarning(f"  {tr('line')} {line} - {sender}: {link}")
        if info["live"]:
            feedback.pushWarning(tr("{n} live locations are not saved in the exported chat:").format(
                n=len(info["live"])))
            for line, sender in info["live"][:20]:
                feedback.pushWarning(f"  {tr('line')} {line} - {sender}")
        feedback.setProgress(100)
        return {self.OUTPUT: dest, "POINT_COUNT": len(points), "SHORT_LINK_COUNT": len(info["short"]),
                "LIVE_COUNT": len(info["live"]), "MESSAGE_COUNT": info["messages"]}
