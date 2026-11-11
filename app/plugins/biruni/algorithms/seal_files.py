"""Verification seals: seal delivered files so the client can check, from a QR code or a link, that each file is
byte-for-byte the one the office sent; and check a received file against its seal, offline (seal/core.py)."""
import datetime as dt
import os

from qgis.core import (Qgis, QgsProcessingException, QgsProcessingMultiStepFeedback, QgsProcessingOutputNumber,
                       QgsProcessingOutputString, QgsProcessingParameterBoolean, QgsProcessingParameterFile,
                       QgsProcessingParameterMultipleLayers, QgsProcessingParameterString)

from ..i18n import tr
from ..seal import core
from .base import BiruniAlgorithm


class SealFiles(BiruniAlgorithm):
    ID = "sealfiles"
    NAME = "Seal files for delivery"
    GROUP = "Engineering office"
    GROUP_ID = "office"
    HELP = ("Seals the files you deliver (zipped Shapefiles, PDF, DXF, GeoPackage or any other file) so the client "
            "can check that each one is exactly the file your office sent. Next to each file come a seal "
            "(.seal.json), its QR code (.seal.png) and, if asked, a printable certificate (.seal.html). Scanning "
            "the code or opening its link shows who sealed the file and when; the client then picks the "
            "received file and the browser checks it on the spot, without uploading it. One changed byte is "
            "enough to show 'This file was changed'. The seal is signed with a key made for you on this "
            "computer and kept encrypted for your Windows user; publish your key fingerprint so clients can "
            "be sure the seal is yours. Send the seal files with the file, or print the QR code on the "
            "delivery letter.")

    INPUT, OFFICE, CERTIFICATE = "INPUT", "OFFICE", "CERTIFICATE"

    def initAlgorithm(self, config=None):  # noqa: N802
        self.addParameter(QgsProcessingParameterMultipleLayers(
            self.INPUT, tr("Files to seal"), Qgis.ProcessingSourceType.File))
        try:
            office = core.office()
        except Exception:  # no settings (e.g. listing the tools)
            office = ""
        self.addParameter(QgsProcessingParameterString(
            self.OFFICE, tr("Office name (kept for next time)"), defaultValue=office, optional=True))
        self.addParameter(QgsProcessingParameterBoolean(
            self.CERTIFICATE, tr("Also write a printable certificate (.seal.html)"), True))
        self.addOutput(QgsProcessingOutputNumber("COUNT", tr("Sealed files")))
        self.addOutput(QgsProcessingOutputString("FINGERPRINT", tr("Key fingerprint")))

    def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
        files = [p for p in self.parameterAsFileList(parameters, self.INPUT, context) if p]
        office = (self.parameterAsString(parameters, self.OFFICE, context) or "").strip()
        certificate = self.parameterAsBoolean(parameters, self.CERTIFICATE, context)
        if office:
            core.set_office(office)
        else:
            feedback.pushWarning(tr("No office name: the seals will not say who sealed the files."))
        skipped = [p for p in files if core.is_seal_output(p)]
        for p in skipped:
            feedback.pushInfo(tr("{name} is a seal file - not sealed again.").format(name=os.path.basename(p)))
        files = [p for p in files if p not in skipped]
        missing = [p for p in files if not os.path.isfile(p)]
        if missing:
            raise QgsProcessingException(tr("File not found: {path}").format(path=missing[0]))
        if not files:
            raise QgsProcessingException(tr("Choose at least one file to seal."))

        steps = QgsProcessingMultiStepFeedback(len(files), feedback)
        seals, links = [], []
        for i, path in enumerate(files):
            if feedback.isCanceled():
                return {}
            steps.setCurrentStep(i)
            try:
                seal = core.seal_file(path, office, certificate, steps)
            except InterruptedError:
                return {}
            seals.append(path + core.SEAL_EXT)
            links.append(seal["url"])
            feedback.pushInfo(tr("Sealed {name} (SHA-256 {sha}).").format(name=os.path.basename(path),
                                                                            sha=seal["sha256"][:16] + "…"))
            feedback.pushInfo(seal["url"])
        fp = core.fingerprint(core.key_pair()[1])
        feedback.pushInfo(tr("Key fingerprint: {fp} - publish it (on your website or letterhead) so clients can "
                             "be sure the seals are yours.").format(fp=fp))
        return {"COUNT": len(seals), "SEALS": seals, "LINKS": links, "FINGERPRINT": fp}


class CheckSeal(BiruniAlgorithm):
    ID = "checkseal"
    NAME = "Check a sealed file"
    GROUP = "Engineering office"
    GROUP_ID = "office"
    HELP = ("Checks a file you received against its Biruni seal, without internet: the seal (.seal.json) next "
            "to the file, another seal file, or the link from its QR code. Tells whether the file is unchanged "
            "since it was sealed, was changed (or is another file), or whether the seal itself was altered "
            "(signature invalid). Shows who sealed it, when, and the key fingerprint: the office name is "
            "written by whoever sealed the file, so compare the fingerprint with the one the office publishes.")

    INPUT, SEAL, LINK = "INPUT", "SEAL", "LINK"

    def initAlgorithm(self, config=None):  # noqa: N802
        self.addParameter(QgsProcessingParameterFile(self.INPUT, tr("Received file")))
        self.addParameter(QgsProcessingParameterFile(
            self.SEAL, tr("Seal file (empty: the .seal.json next to the file)"), optional=True,
            fileFilter=tr("Biruni seal") + " (*.seal.json)"))
        self.addParameter(QgsProcessingParameterString(
            self.LINK, tr("Or the seal link (from the QR code)"), optional=True))
        for key, label in (("RESULT", tr("Result")), ("OFFICE", tr("Office")), ("SEALED", tr("Sealed on")),
                           ("FINGERPRINT", tr("Key fingerprint"))):
            self.addOutput(QgsProcessingOutputString(key, label))

    def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
        path = self.parameterAsFile(parameters, self.INPUT, context)
        if not path or not os.path.isfile(path):
            raise QgsProcessingException(tr("File not found: {path}").format(path=path))
        source = (self.parameterAsString(parameters, self.LINK, context) or "").strip() or \
            self.parameterAsFile(parameters, self.SEAL, context) or None
        try:
            status, d = core.check_file(path, source, feedback)
        except InterruptedError:
            return {}
        out = {"RESULT": status, "UNCHANGED": status == core.UNCHANGED, "OFFICE": d.get("office", ""),
               "SEALED": d.get("sealed_utc", ""), "FINGERPRINT": d.get("fingerprint", "")}
        if status == core.BAD_SEAL:
            feedback.reportError(tr("No readable Biruni seal for this file."), False)
            return out
        when = dt.datetime.fromtimestamp(d["t"]).strftime("%Y-%m-%d %H:%M")
        office = d["office"] or tr("(no name given)")
        feedback.pushInfo(tr("Sealed by: {office}").format(office=office))
        feedback.pushInfo(tr("Sealed on: {date}").format(date=when))
        feedback.pushInfo(tr("Key fingerprint: {fp}").format(fp=d["fingerprint"]))
        if status == core.UNCHANGED:
            feedback.pushInfo(tr("Unchanged: the file is byte-for-byte the one sealed on {date} by {office}.").format(
                date=when, office=office))
        elif status == core.CHANGED:
            feedback.reportError(tr("Changed: this is not the file that was sealed (it was changed after "
                                    "sealing, or it is another file)."), False)
        else:
            feedback.reportError(tr("Signature invalid: the seal was altered or is not genuine. Do not rely on "
                                    "this file."), False)
        return out
