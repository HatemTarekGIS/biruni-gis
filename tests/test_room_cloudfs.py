r"""Tests for field_photos/drive.named_folders & team/cloudfs.py (no QGIS).

Run:  engine\bin\python-qgis.bat tests\test_room_cloudfs.py
"""
import os
import shutil
import sys
import tempfile
import unittest.mock as mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))
sys.path.insert(0, os.path.join(HERE, "..", "app", "python"))

from biruni.field_photos import bridge, drive  # noqa: E402
from biruni.team import cloudfs  # noqa: E402

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        failures.append(msg)


work = tempfile.mkdtemp()
try:
    # ---- named_folders / ensure_named_folder / wrappers --------------------------------------------------------
    r1 = os.path.join(work, "r1")
    r2 = os.path.join(work, "r2")
    r3 = os.path.join(work, "r3")
    direct = os.path.join(r1, "biruni-room-aaaa")
    under = os.path.join(r2, "biruni-room-aaaa")
    deep = os.path.join(r3, "Projects", "biruni-room-aaaa")
    for p in (direct, under, deep):
        os.makedirs(p)

    check(drive.named_folders([direct], "biruni-room-aaaa") == [os.path.normcase(os.path.abspath(direct))],
          "named_folders finds folder passed as root itself")
    check(drive.named_folders([r2], "biruni-room-aaaa") == [os.path.normcase(os.path.abspath(under))],
          "named_folders finds folder at root\\name")
    check(drive.named_folders([r3], "biruni-room-aaaa") == [os.path.normcase(os.path.abspath(deep))],
          "named_folders finds folder at root\\*\\name")

    eroot = os.path.join(work, "eroot")
    os.makedirs(eroot)
    ep, created = drive.ensure_named_folder([eroot], "biruni-room-bbbb")
    check(created and ep == os.path.normcase(os.path.abspath(os.path.join(eroot, "biruni-room-bbbb")))
          and os.path.isdir(ep), "ensure_named_folder creates folder when missing")
    ep2, created2 = drive.ensure_named_folder([eroot], "biruni-room-bbbb")
    check((not created2) and ep2 == ep, "ensure_named_folder reuses existing folder")

    k = bridge.new_key()
    ok_path, ok_made = drive.ensure_office_folder([eroot], k)
    check(ok_made and drive.office_folders([eroot], k) == [ok_path],
          "office_folders and ensure_office_folder wrappers unchanged")

    # ---- cloudfs.kind ------------------------------------------------------------------------------------------
    check(cloudfs.kind(work, _info=("G:\\", "Google Drive", "FAT32")) == "gdrive-stream",
          "kind detects Google Drive stream volume")
    with mock.patch.object(cloudfs, "_is_cloud_files", return_value=False):
        check(cloudfs.kind(work, _info=("C:\\", "OS", "NTFS")) == "plain",
              "kind returns plain for OS volume")
        with mock.patch.object(cloudfs, "_is_cloud_files", return_value=True):
            check(cloudfs.kind(work, _info=("C:\\", "OS", "NTFS")) == "cloud-files",
                  "kind returns cloud-files when cloud attributes present")

    # ---- cloudfs.placeholder -----------------------------------------------------------------------------------
    sample = os.path.join(work, "sample.bpf")
    with open(sample, "wb") as f:
        f.write(b"abc")
    with mock.patch.object(cloudfs, "kind", return_value="plain"):
        check(cloudfs.placeholder(sample, _attrs=0x400000) is True,
              "placeholder True for 0x400000 (RECALL_ON_DATA_ACCESS)")
        check(cloudfs.placeholder(sample, _attrs=0x40000) is True,
              "placeholder True for 0x40000 (RECALL_ON_OPEN)")
        check(cloudfs.placeholder(sample, _attrs=0x1000) is True,
              "placeholder True for 0x1000 (OFFLINE)")
        check(cloudfs.placeholder(sample, _attrs=0x80) is False,
              "placeholder False on plain volume")
    with mock.patch.object(cloudfs, "kind", return_value="gdrive-stream"):
        check(cloudfs.placeholder(sample) is None,
              "placeholder None on gdrive-stream")

    # ---- cloudfs.find_room (0, 1, 2 hits) ----------------------------------------------------------------------
    tag = "0123456789abcdef"
    empty_root = os.path.join(work, "empty_root")
    hit1_root = os.path.join(work, "hit1_root")
    hit2_root = os.path.join(work, "hit2_root")
    os.makedirs(empty_root)
    os.makedirs(os.path.join(hit1_root, "Office", "biruni-room-" + tag))
    os.makedirs(os.path.join(hit2_root, "biruni-room-" + tag))

    check(cloudfs.find_room(tag, [empty_root]) == [], "find_room with 0 hits")
    h1 = cloudfs.find_room(tag, [hit1_root])
    check(h1 == [os.path.normcase(os.path.abspath(os.path.join(hit1_root, "Office")))],
          f"find_room with 1 hit ({h1})")
    h2 = cloudfs.find_room(tag, [hit1_root, hit2_root])
    check(len(h2) == 2, f"find_room with 2 hits ({h2})")
finally:
    shutil.rmtree(work, ignore_errors=True)

print(f"\n{len(failures)} failure(s)")
sys.exit(1 if failures else 0)
