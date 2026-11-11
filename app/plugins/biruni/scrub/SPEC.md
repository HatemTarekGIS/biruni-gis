# scrub — clean what leaves the office

This is the first piece of «غرفة التعقيم» (report idea I10, 2026-11-09). It handles outgoing files only. The incoming part, rebuilding a file inside a jail, is not built.

## API

| Call | What it does |
|---|---|
| `scrub_file(path, out_path=None, options) -> Receipt` | Cleans the file in place, or into a copy at `out_path`. |
| `after_export(paths, feedback=None)` | The hook exporters call after they write a file. It runs only when the setting `biruni/scrub/outgoing` is on (the default). |
| `open_dialog(plugin)` | "Clean a File for Sending…". It writes `<name>_clean.<ext>`, shows the receipt, and can save `<name>.scrub.txt`. |

`options` takes three keys:
- `names`: extra user names to remove.
- `drop_fields`: `{table, file stem or "*": [fields]}`.
- `drop_tables`: GeoPackage tables to drop.

## What is removed from each format

### GeoPackage
- `gpkg_metadata` and `gpkg_metadata_reference` rows.
- The embedded `qgis_projects` table.
- In `layer_styles`, the owner. Local paths become file names.
- Paths in `gpkg_contents` descriptions.
- The tables named in `drop_tables` and the fields named in `drop_fields`.
- Deleted pages, removed by VACUUM.

### Shapefile
- These sidecars are deleted: `.shp.xml`, `.xml`, `.qmd` and `.aux.xml`.
- In `.qml` and `.sld` sidecars, local paths become file names.
- Hidden fields are dropped.

### DXF
- These HEADER variables are blanked: `$LASTSAVEDBY`, `$PROJECTNAME`, `$HYPERLINKBASE`, `$STYLESHEET` and `$AUTHOR`.
- `$FINGERPRINTGUID` and `$VERSIONGUID` get new ids.
- Local paths in text and XDATA become file names.

### KML and KMZ
- The author is removed.
- Local hrefs and paths become file names.
- Hidden `SimpleData` and `SimpleField` entries are dropped.
- In a KMZ, the zip dates are reset to a neutral date.

### XLSX
- The creator, lastModifiedBy, Company, Manager and lastPrinted properties are blanked.
- `docProps/custom.xml` is removed.
- `x15ac:absPath`, the folder the workbook was saved in, is removed.

### CSV and GeoJSON
- Hidden fields are dropped.

### PDF
- These Info values are blanked: Author, Creator, Producer, Subject and Keywords. Title stays.
- XMP values are blanked: dc:creator, CreatorTool, Producer and the document ids.
- Values are blanked in place, so every byte offset and the xref stay valid.

## Never changed

Data is left as it is: features, geometry and visible attribute values. If attribute values look like local paths, the receipt warns about them but they are not altered.

## Hidden fields

A field hidden in the attribute form (widget type "Hidden") is left out by Export Layers when the setting is on. It uses `SaveVectorOptions.attributes`.

## Limits

- **PDF:** compressed or encrypted metadata streams are not touched; the receipt warns about them. Page content is not scanned.
- **DWG:** not handled.
- **File names:** the file names that stay may themselves say something.
- **User names:** they are found from `USERNAME`, `USERPROFILE` and `getpass`, plus `options["names"]`. Other people's names inside the data are data.
