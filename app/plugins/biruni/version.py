APP_NAME = "Biruni GIS"
VERSION = "0.2.0"
# GitHub "owner/name" of the public repository; empty disables update checks
# and problem-report links until the repository is published.
REPO = "HatemTarekGIS/biruni-gis"
# Public project page.
HOMEPAGE = f"https://github.com/{REPO}" if REPO else ""
# Updates of Biruni's own code (update/client.py, scripts/publish_update.py): where the signed list is, and the
# public half of the key that signs it. A list signed with any other key is refused.
UPDATE_URL = "https://biruni-gis.com/updates/"
UPDATE_KEY = "ece4cf1d6f94010b495c5f537698205a7e83f56397508223d1e9047e5bf0c461"
