# Handoff: Biruni GIS — serverless teamwork (2026-11-11)

You are continuing work another assistant started. Read this whole file before touching code.

## The project
- Biruni GIS: a free, offline-first desktop GIS for Arab engineering offices, surveyors and municipalities, built as a plugin on the QGIS 4.2 engine. Code: `D:\Biruni-GIS\app\plugins\biruni\`. Python 3.12, PyQt6, QtWebEngine.
- Owner: Hatem. Reply to him in Egyptian Arabic.

## Hard rules (do not break)
1. Never open a GUI window on Hatem's screen. All UI tests run offscreen (`QT_QPA_PLATFORM=offscreen`; QtWebEngine tests also `QTWEBENGINE_CHROMIUM_FLAGS=--disable-gpu --no-sandbox`, in their own script, not inside Biruni).
2. Run Python with `D:\Biruni-GIS\engine\bin\python-qgis.bat <script>` (no `-I`).
3. No commit or push unless Hatem asks. The `gh-pages` push is done by Hatem himself: give him the exact commands.
4. Nothing that costs money for an organisation: no paid licences, keys, trials, non-commercial-only parts. Code bundled into published web pages must be MIT/BSD/ISC/Apache/public domain.
5. No server the user must rent or run, no ngrok/tunnels, no paid TURN. Public relays (Nostr, ntfy) only carry end-to-end encrypted bytes.
6. No AI assistant / LLM features inside Biruni.
7. Never write "QGIS" in UI text, except About, the start-page footer and legal notices. The app's Arabic UI is Modern Standard Arabic. "Biruni" stays untranslated except in Arabic (بيروني).
8. Honest security: never claim what client-side code cannot enforce; write the limits in each package's SPEC.md.
9. Don't edit MyPart / PWA Package-9 (`D:\PWA Roads Asbuilt SouthPackage-9\`) from this work.
10. Data writes to any live database need Hatem's explicit approval naming the change.

## How to test
- One test: `engine\bin\python-qgis.bat tests\test_X.py` (prints PASS/FAIL, ends with ALL PASSED).
- Full suite (~151 tests, slow): `powershell -File scripts\run_tests.ps1 -Out <file>`. `test_thesis_starter` downloads data and can time out on a slow network; run it alone if so.
- Ribbon limits: each workspace ≤ 62% of the full ribbon (`test_workbench`), no page needs an arrow at 1366 px (`test_ribbon_fold`). When adding buttons, hide them in workspaces in `workbench.py` if needed.
- Translations: put new English `tr()` strings in a JSON `{lang:{english:translation}}` for ar, es, fr, pt_BR, tr, fa, merge with `scripts\merge_strings.py <json>`, check with `scripts\extract_strings.py app/plugins/biruni app/plugins/biruni/i18n/ar.json`. About 38 "missing" entries are known false alarms (ids such as admin, area, layer, keep, last, sum, ask, authority, acct_accounts…).
- Bash heredocs eat backslashes: write Python files with an editor tool, not `cat <<EOF`.
- New commands go in `commands.py` (`_c(id, label, icon, run=..., tip=...)`), ribbon in `plugin.py`, workspace hiding in `workbench.py`.

## What already exists (do not rebuild)
Team room (`team/`: host on a member's PC, takeover, merge, time machine, branches, locks, provenance; rooms across networks over WebRTC via Nostr/ntfy), phone bridge (`field_photos/bridge.py`), carrier pigeon (`pigeon/`), light sync (`lightsync/`), peer-review lottery (`peer_review/`), team computing (`team_compute/`), live dashboards + live paper (`dashboard/`), map service as a folder (`mapservice/`), signed portal (`portal/`), team vault (`team_vault/`), shared copies/fingerprints/proofs/PSI.

Done on 2026-11-09/10 (first pieces of the approved ideas):
- Security fixes: Chromium sandbox for the bridge (`scripts/patch_engine.py` copies 6 DLLs), vault `drop` waits 30 days (`team_vault`), PC identity pin `&i=` in bridge links (signed answers), «New paper address…».
- G11 door check: `secure/door.py`, signed `build\bridge-page\release.json` + `release.sig`.
- I10 outgoing scrub: `scrub/` (+ hooks in export_layers, kmz; command `scrub_file`).
- I05 folder carrier: `team/folder.py` (room changes through a shared folder; the host still merges when it opens).
- I06 merge rules + number ranges: `team/rules.py`, `team/rules_ui.py`.

## The research and what Hatem approved
Report (Arabic): `C:\Users\HatemTarek\Desktop\DXF_to_SHP_App_HatemTarek\reports\ابتكارات بيروني بلا استضافة.md`; notes: `...\research_notes\ابتكارات بيروني بلا استضافة\`. Read the section of an idea before building it, including the red-team fixes and the limits.

Approved, still to do (suggested order):
1. I06 next piece: shared-node geometry merge (moving a common corner reaches all neighbours; disagreement shrinks to one node). Also carry rules/grants inside pigeon and folder packs.
2. I05 next pieces: joining a room through the folder; signed member chain; relay carrier; tiny edit capsules.
3. I14 «الخزنة التي ترفض الموت»: daily proofs that holders still keep their shards, monthly automatic restore drills with a health certificate, a sister office holding encrypted copies, rotating USB.
4. I04 «الورقة التي ترد»: PDF comments and tick-box sheets come back as proposals pinned to the printed version (no AI; handwriting stays an image).
5. I10 next piece: rebuild incoming files in an isolated process.
6. I01 «الملف ذو الوجهين», I02 «السيرفر الشبح», I07 «الطاولة الزجاجية», I03 «شنطة الميدان», I13 «الصندوق الأسود», I11 «بوصلة الإقامة», I09 «أطلس المفاتيح».
7. G21 «الساعي» only after a 2-day test on a real iPhone.
Cancelled: I08 دفتر الجروب, I12 الخريطة في الحبر, G12 مجلس الإصدار. Postponed: G22 المرسى.

## Not verified yet
Nothing above was tested on real phones on mobile data, or on a real OneDrive/NAS. Say so; don't claim it.

## Working style Hatem wants
Small steps, one at a time; run the related tests; report briefly in Egyptian Arabic what changed, what passed, and what is still untested.
