"""Tests for ArcGIS Server / Portal / ArcGIS Online services (biruni.arcgis_services), off screen, against a
local stand-in server (tests/arcgis_mock_server.py) - nothing is ever written to a real server:

    REST client      folders, services, layers, paged queries, sign-in (user/password tokens, API key, OAuth with
                     PKCE through the browser redirect), expired tokens renewed, clear errors
    connections      saved; the password is encrypted (DPAPI), never plain text
    browser          folders -> services -> layers in the window, Add to Map
    provider         the engine's feature service provider reads and edits (add, attributes, geometry, delete);
                     a read-only service and a service without Create/Delete say so; a signed-in service
    offline / sync   take a layer offline to a GeoPackage, edit it, sync back: adds, changes, deletes, a conflict
                     (another user's edit, editor tracking), server changes brought in, "mine" and "theirs",
                     a read-only service refusing edits; the summary text

Optional: a read-only look at Esri's public sample server (skipped without internet).

Run:  engine\\bin\\python-qgis.bat tests\\test_arcgis_services.py
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))

from qgis.core import QgsApplication  # noqa: E402

SETTINGS_DIR = tempfile.mkdtemp()
os.environ["QGIS_CUSTOM_CONFIG_PATH"] = SETTINGS_DIR
app = QgsApplication([], True)
app.initQgis()
sys.stdout.reconfigure(line_buffering=True)
from qgis.core import (QgsFeature, QgsGeometry, QgsPointXY, QgsProject, QgsSettings,  # noqa: E402
                       QgsVectorLayer)

from biruni.arcgis_services import connections, layers, offline  # noqa: E402
from biruni.arcgis_services.rest import (AUTH_APIKEY, AUTH_OAUTH, AUTH_TOKEN, Client, Connection,  # noqa: E402
                                         RestError, services_root, split_service_url)

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


# ---- the stand-in server --------------------------------------------------------------------------------------------
tmp = tempfile.mkdtemp(prefix="biruni_ags_")
port_file = os.path.join(tmp, "port")
server = subprocess.Popen([sys.executable, os.path.join(HERE, "arcgis_mock_server.py"), port_file])
for _ in range(200):
    if os.path.exists(port_file) and open(port_file).read().strip():
        break
    time.sleep(0.05)
PORT = int(open(port_file).read())
HOST = f"http://127.0.0.1:{PORT}"
ROOT = f"{HOST}/arcgis/rest/services"


def admin(path, **params):
    return json.loads(urllib.request.urlopen(f"{HOST}/__admin/{path}?" + urllib.parse.urlencode(params)).read())


def server_rows(layer_url):
    d = json.loads(urllib.request.urlopen(layer_url + "/query?where=1%3D1&outFields=*&returnGeometry=true&f=json"
                                          "&resultRecordCount=1000&resultOffset=0").read())
    return {f["attributes"]["OBJECTID"]: f for f in d["features"]}


def all_rows(layer_url):
    """Every feature, paging past the stand-in's tiny maxRecordCount."""
    ids = json.loads(urllib.request.urlopen(layer_url + "/query?where=1%3D1&returnIdsOnly=true&f=json").read())
    out = {}
    for o in ids["objectIds"]:
        d = json.loads(urllib.request.urlopen(layer_url + f"/query?objectIds={o}&outFields=*&f=json").read())
        out[o] = d["features"][0]
    return out


try:
    # ---- REST client ------------------------------------------------------------------------------------------------
    check(services_root("gis.example.com/arcgis") == "https://gis.example.com/arcgis/rest/services" and
          services_root(ROOT + "/Parcels/FeatureServer/0") == ROOT, "server address normalised")
    check(split_service_url(ROOT + "/Utilities/Water/FeatureServer/1") == (ROOT, "Utilities/Water", "FeatureServer", 1),
          "service URL split into folder/service/type/layer")
    anon = Client(Connection("Local", HOST + "/arcgis"))
    cat = anon.catalog()
    check(cat["folders"] == ["Secure", "Utilities"] and {s["name"] for s in cat["services"]} == {"Parcels", "Basemap"},
          "catalog: folders and services")
    check({s["name"] for s in anon.catalog("Utilities")["services"]} == {"Utilities/Water", "Utilities/Readonly"},
          "folder listing")
    lyrs, _info = anon.layers(ROOT + "/Utilities/Water/FeatureServer")
    check([x["name"] for x in lyrs] == ["Valves", "Mains"], "layers of a feature service")
    parcels_url = ROOT + "/Parcels/FeatureServer/0"
    feats = anon.query_all(parcels_url)
    check(len(feats) == 5 and sorted(f["attributes"]["OBJECTID"] for f in feats) == [1, 2, 3, 4, 5],
          "paged query (resultOffset) gets all 5 features past maxRecordCount 2")
    check(len(anon.query_all(ROOT + "/Utilities/Water/FeatureServer/0")) == 3,
          "query by object id batches when the layer has no pagination")
    secure_url = ROOT + "/Secure/Assets/FeatureServer/0"
    try:
        anon.info(secure_url)
        check(False, "a secured service refuses anonymous requests")
    except RestError as e:
        check(e.code == 499 and "sign-in" in str(e), f"a secured service asks for a sign-in ({e})")
    try:
        Client(Connection("Bad", HOST + "/arcgis", auth=AUTH_TOKEN, username="editor", password="nope")).info(
            secure_url)
        check(False, "wrong password refused")
    except RestError as e:
        check("wrong" in str(e), "wrong password: clear message")
    tok_conn = Connection("Company", HOST + "/arcgis", auth=AUTH_TOKEN, username="editor", password="s3cret!")
    signed = Client(tok_conn)
    check(signed.info(secure_url).get("name") == "Assets" and tok_conn.token, "generateToken sign-in reads a "
                                                                              "secured layer")
    first = tok_conn.token
    tok_conn.token = "expired-token"          # an expired token is renewed once and the request repeated
    check(signed.info(secure_url).get("name") == "Assets" and tok_conn.token not in ("expired-token", first),
          "expired token renewed automatically")
    key_conn = Connection("Key", HOST + "/arcgis", auth=AUTH_APIKEY, api_key=tok_conn.token)
    check(Client(key_conn).info(secure_url).get("name") == "Assets", "API key sign-in")
    portal = Connection("Portal", HOST + "/portal", auth=AUTH_OAUTH, client_id="biruni-test-app")
    pc = Client(portal)
    pc.oauth_login(lambda url: urllib.request.urlopen(url).read(), wait=20)   # the "browser" follows the redirect
    check(portal.token and portal.username == "oauth-user" and portal.refresh_token.startswith("r-"),
          "OAuth 2 sign-in (authorization code + PKCE) through the local redirect")
    oauth_server = Connection("Portal server", HOST + "/arcgis", auth=AUTH_OAUTH, client_id="biruni-test-app",
                              token=portal.token, token_expires=portal.token_expires)
    check(Client(oauth_server).info(secure_url).get("name") == "Assets", "OAuth token opens secured services")
    old = portal.token
    portal.token_expires = 0
    check(pc.ensure_token() != old, "OAuth token refreshed with the refresh token")

    # ---- saved connections: secrets encrypted -----------------------------------------------------------------------
    connections.save(tok_conn)
    raw = QgsSettings().value("biruni/arcgis-services/connections/Company/password", "")
    check(raw.startswith("dpapi:") and "s3cret" not in raw and not connections.stored_as_plain_text("Company"),
          "password stored encrypted (DPAPI), never as plain text")
    loaded = connections.load("Company")
    check(loaded.password == "s3cret!" and loaded.username == "editor" and loaded.auth == AUTH_TOKEN,
          "saved connection loads with its password")
    check(connections.find_for_url(secure_url).name == "Company", "connection found from a service address")
    connections.save(Connection("Local", HOST + "/arcgis"))
    check(set(connections.names()) == {"Company", "Local"}, "connections listed")

    # ---- the browser window ------------------------------------------------------------------------------------------
    from biruni.arcgis_services.ui import ROLE_URL, ServicesBrowser, offline_source, take_offline_to_map

    project = QgsProject()
    notes = []
    br = ServicesBrowser(project=project, notify=lambda text, level: notes.append((level, text)))
    br.conns.setCurrentIndex(br.conns.findData("Local"))
    root_item = br.tree.topLevelItem(0)
    br.expand(root_item)
    names = [root_item.child(i).text(0) for i in range(root_item.childCount())]
    check(names == ["Secure", "Utilities", "Parcels", "Basemap"], f"browser: folders and services ({names})")
    util = next(it for it in br.items("folder") if it.text(0) == "Utilities")
    br.expand(util)
    water = next(it for it in br.items("service") if it.text(0) == "Water")
    br.expand(water)
    valves_item = next(it for it in br.items("layer") if it.text(0) == "Valves")
    check([it.text(0) for it in br.items("layer")] == ["Valves", "Mains"], "browser: layers of a service")
    br.tree.setCurrentItem(valves_item)
    check("add, change, delete" in br.info.text(), f"browser: edit rights shown ({br.info.text()})")
    valves = br.add_selected(valves_item)
    check(valves is not None and valves.isValid() and valves.featureCount() == 3 and
          valves.providerType() == "arcgisfeatureserver", "Add to Map: feature layer through the engine's provider")
    basemap = next(it for it in br.items("service") if it.text(0) == "Basemap")
    br.expand(basemap)
    check(any(it.data(0, ROLE_URL).endswith("/Basemap/MapServer") for it in br.items("image")),
          "map services offered as a map image")

    # ---- editing through the provider ---------------------------------------------------------------------------------
    r = layers.rights(valves)
    check(all(r.values()), "provider: add / change / geometry / delete allowed")
    valves.startEditing()
    check(notes and notes[-1][0] == 0 and "add, change, delete" in notes[-1][1], "starting to edit says what is "
                                                                                    "allowed")
    nf = QgsFeature(valves.fields())
    nf.setAttribute("valve_id", "V9")
    nf.setAttribute("diameter", 250)
    nf.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(3900900, 2900900)))
    valves.addFeature(nf)
    v1 = next(f for f in valves.getFeatures() if f["valve_id"] == "V1")
    v2 = next(f for f in valves.getFeatures() if f["valve_id"] == "V2")
    valves.changeAttributeValue(v1.id(), valves.fields().indexOf("diameter"), 999)
    valves.changeGeometry(v1.id(), QgsGeometry.fromPointXY(QgsPointXY(3900011, 2900011)))
    valves.deleteFeature(v2.id())
    check(valves.commitChanges(), f"provider edits committed ({valves.commitErrors()})")
    rows = all_rows(ROOT + "/Utilities/Water/FeatureServer/0")
    by_id = {f["attributes"]["valve_id"]: f for f in rows.values()}
    check(set(by_id) == {"V1", "V3", "V9"}, "server: feature added and deleted")
    check(by_id["V1"]["attributes"]["diameter"] == 999 and by_id["V1"]["geometry"]["x"] == 3900011,
          "server: attribute and geometry changed")
    mains = layers.make_layer(Client(Connection("Local", HOST + "/arcgis")), ROOT + "/Utilities/Water/FeatureServer/1")
    r = layers.rights(mains)
    check(not r["add"] and r["update"] and not r["delete"], "a service without Create / Delete: only changes")
    check("does not allow you to add / delete" in layers.rights_text(mains), "and says so")
    hyd = layers.make_layer(anon, ROOT + "/Utilities/Readonly/FeatureServer/0")
    layers.guard(hyd)
    check(not any(layers.rights(hyd).values()) and not hyd.startEditing() and
          "read-only" in layers.rights_text(hyd), "a read-only service cannot be edited, with a clear message")
    assets = layers.make_layer(Client(tok_conn), secure_url)
    check(assets.isValid() and assets.featureCount() == 2 and "token" not in assets.source().lower() and
          "bearer" not in assets.source().lower(), "signed-in service through the provider (no token in the source)")
    assets.startEditing()
    a1 = next(assets.getFeatures())
    assets.changeAttributeValue(a1.id(), assets.fields().indexOf("tag"), "A1-checked")
    check(assets.commitChanges() and any(f["attributes"]["tag"] == "A1-checked"
                                         for f in Client(tok_conn).query_all(secure_url)),
          "signed-in service edited through the provider")
    project.addMapLayer(assets)
    layers._SIGNED.clear()                  # as after opening a saved project: nobody signed in yet
    n_signed = layers.refresh_tokens(project)
    check(n_signed >= 1 and assets.isValid() and assets.featureCount() == 2,
          f"after opening a project the signed-in layer is signed in again from its saved connection ({n_signed})")
    layers._SIGNED.clear()
    got = []
    check(layers.refresh_tokens(project, background=True, done=got.append) is None,
          "in the background the call returns at once (opening a project does not wait for the server)")
    for _ in range(300):
        QgsApplication.processEvents()
        if got:
            break
        time.sleep(0.02)
    check(got and got[0] >= 1 and assets.isValid() and assets.featureCount() == 2,
          f"and the layer is signed in when the token comes ({got})")

    # ---- take offline, edit, sync back ----------------------------------------------------------------------------------
    gpkg = os.path.join(tmp, "field.gpkg")
    client = Client(Connection("Local", HOST + "/arcgis"))
    off = take_offline_to_map(client, parcels_url, gpkg, project, "Parcels")
    check(off is not None and off.isValid() and off.featureCount() == 5, "taken offline: 5 parcels in a GeoPackage")
    check(offline_source(off) == (gpkg, "Parcels"), "the offline copy knows its service")
    p0 = offline.pending(gpkg, "Parcels")
    check(p0.count == 0, "nothing pending right after download (dates, numbers and shapes round-trip)")
    meta = offline.offline_layers(gpkg)["Parcels"]
    check(meta["oid_field"] == "OBJECTID" and meta["wkid"] == 3857 and
          meta["edit_fields"].get("editDateField") == "last_edited_date", "service facts kept with the copy")

    def local(parcel):
        return next(f for f in off.getFeatures() if f["parcel_no"] == parcel)

    off.startEditing()
    for k in (1, 2):
        f = QgsFeature(off.fields())
        f.setAttribute("parcel_no", f"N-{k}")
        f.setAttribute("use", "park")
        f.setGeometry(QgsGeometry.fromWkt(f"MULTIPOLYGON(((3901000 290100{k},3901000 2901100,3901100 2901100,"
                                          f"3901100 290100{k},3901000 290100{k})))"))
        off.addFeature(f)
    off.changeAttributeValue(local("P-1").id(), off.fields().indexOf("use"), "school")
    off.changeGeometry(local("P-2").id(), QgsGeometry.fromWkt(
        "MULTIPOLYGON(((3900400 2900000,3900400 2900150,3900550 2900150,3900550 2900000,3900400 2900000)))"))
    off.deleteFeature(local("P-3").id())
    check(off.commitChanges(), "offline edits saved in the GeoPackage")
    p1 = offline.pending(gpkg, "Parcels")
    check((len(p1.adds), len(p1.updates), len(p1.deletes)) == (2, 2, 1), "pending: 2 added, 2 changed, 1 deleted")

    # meanwhile, on the server: someone else changes P-1 (also changed here) and P-4 (not changed here)
    admin("edit", layer="/arcgis/rest/services/Parcels/FeatureServer/0", oid=1, attributes=json.dumps({"use": "mall"}))
    admin("edit", layer="/arcgis/rest/services/Parcels/FeatureServer/0", oid=4, attributes=json.dumps({"use": "farm"}))
    rep = offline.sync(client, gpkg, "Parcels")
    print("   ", rep.text())
    check((rep.added, rep.changed, rep.deleted, len(rep.conflicts), rep.pulled) == (2, 1, 1, 1, 1),
          "sync: 2 added, 1 changed, 1 deleted, 1 conflict, 1 brought in")
    check(rep.text() == "2 added, 1 changed, 1 deleted, 1 updated from the server, 1 conflict", "summary text")
    check(rep.conflicts[0][1] == 1 and "someone_else" in rep.conflicts[0][2], "the conflict names who changed it")
    srv = all_rows(parcels_url)
    by_no = {f["attributes"]["parcel_no"]: f for f in srv.values()}
    check(set(by_no) == {"P-1", "P-2", "P-4", "P-5", "N-1", "N-2"}, "server: adds and delete applied")
    check(by_no["P-1"]["attributes"]["use"] == "mall", "server: the conflicting edit was not sent")
    check(by_no["P-2"]["geometry"]["rings"][0][0][0] == 3900400, "server: geometry change applied")
    off.reload()
    check(local("N-1")["OBJECTID"] == by_no["N-1"]["attributes"]["OBJECTID"],
          "new features got their server object id")
    check(local("P-4")["use"] == "farm", "the server's change to P-4 was brought in")
    check(local("N-2")["last_edited_user"] == "anonymous", "editor tracking values come back from the server")
    p2 = offline.pending(gpkg, "Parcels")
    check((len(p2.adds), len(p2.updates), len(p2.deletes)) == (0, 1, 0), "only the conflict is still pending")
    rep = offline.sync(client, gpkg, "Parcels", policy=offline.MINE)
    check(rep.changed == 1 and not rep.conflicts and all_rows(parcels_url)[1]["attributes"]["use"] == "school",
          "'mine': my version overwrites the server")
    check(offline.pending(gpkg, "Parcels").count == 0, "nothing pending after sync")
    off.reload()
    off.startEditing()
    off.changeAttributeValue(local("P-5").id(), off.fields().indexOf("use"), "hotel")
    off.commitChanges()
    admin("edit", layer="/arcgis/rest/services/Parcels/FeatureServer/0", oid=5, attributes=json.dumps({"use": "clinic"}))
    rep = offline.sync(client, gpkg, "Parcels", policy=offline.THEIRS)
    off.reload()
    check(rep.changed == 0 and not rep.conflicts and local("P-5")["use"] == "clinic" and
          offline.pending(gpkg, "Parcels").count == 0, "'theirs': the server's version replaces my edit")

    # a layer without editor tracking: conflicts found by comparing with what was downloaded
    gpkg2 = os.path.join(tmp, "valves.gpkg")
    t2 = offline.take_offline(client, ROOT + "/Utilities/Water/FeatureServer/0", gpkg2)
    vl = QgsVectorLayer(f"{gpkg2}|layername={t2}", "valves", "ogr")
    vl.startEditing()
    v3 = next(f for f in vl.getFeatures() if f["valve_id"] == "V3")
    vl.changeAttributeValue(v3.id(), vl.fields().indexOf("diameter"), 1)
    vl.commitChanges()
    admin("edit", layer="/arcgis/rest/services/Utilities/Water/FeatureServer/0", oid=3,
          attributes=json.dumps({"diameter": 2}))
    rep = offline.sync(client, gpkg2, t2)
    check(len(rep.conflicts) == 1 and rep.changed == 0, "conflict found without editor tracking")
    del vl

    # a read-only service keeps the edits and says why
    gpkg3 = os.path.join(tmp, "hydrants.gpkg")
    t3 = offline.take_offline(client, ROOT + "/Utilities/Readonly/FeatureServer/0", gpkg3)
    hl = QgsVectorLayer(f"{gpkg3}|layername={t3}", "h", "ogr")
    hl.startEditing()
    hf = QgsFeature(hl.fields())
    hf.setAttribute("hyd_id", "H2")
    hf.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(3900500, 2900500)))
    hl.addFeature(hf)
    hl.commitChanges()
    rep = offline.sync(client, gpkg3, t3)
    check(rep.added == 0 and rep.refused and "not allowed by the service" in rep.text() and
          offline.pending(gpkg3, t3).count == 1, "read-only service: the edit stays offline, refused with a reason")
    del hl

    # ---- the sync window ----------------------------------------------------------------------------------------------
    from biruni.arcgis_services.ui import SyncDialog
    off.startEditing()
    off.changeAttributeValue(local("P-4").id(), off.fields().indexOf("use"), "orchard")
    off.commitChanges()
    dlg = SyncDialog(None, gpkg, "Parcels", conn=Connection("Local", HOST + "/arcgis"))
    check("1 changed" in dlg.counts.text(), f"sync window shows what is pending ({dlg.counts.text()})")
    out = dlg.run()
    check(out is not None and out.changed == 1 and "1 changed" in dlg.result.text(), "sync window syncs")

    # ---- the engine's provider on the mock: read-only look at a public sample server (optional) -----------------------
    try:
        pub = Client(Connection("Esri samples", "https://sampleserver6.arcgisonline.com/arcgis"), timeout=15)
        cat = pub.catalog()
        ok = bool(cat["services"]) or bool(cat["folders"])
        lyr_info = pub.info("https://sampleserver6.arcgisonline.com/arcgis/rest/services/SampleWorldCities/"
                            "MapServer/0")
        check(ok and lyr_info.get("name"), "public sample server: catalog and layer read (read-only)")
    except RestError as e:
        print("SKIP public sample server not reachable:", e)
finally:
    server.kill()

print()
print("ALL PASS" if not failures else f"{len(failures)} FAILED")
sys.exit(1 if failures else 0)
