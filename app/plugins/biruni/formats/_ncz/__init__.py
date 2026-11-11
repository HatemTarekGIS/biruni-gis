# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-2.0-or-later
"""Netcad NCZ decoder - NCZ Engine v2 from NCZ2Geo 0.13.1
(https://github.com/YusufEminoglu/NCZ2Geo, Copyright (C) 2026 Yusuf Eminoğlu,
GPL-2.0-or-later), bundled unchanged except:

* binary.decode_oem_text decodes Windows-1254 so Turkish letters are kept;
* the on-disk index cache (cache.py) and the exporters are not bundled.

Pure Python, reads bytes only: no file writes, no network."""
from .parser import PARSER_BACKEND_V2, NczCatalog, parse_bytes, parse_file  # noqa: F401
