# A shapefile that remembers its arcs (`.crv`), version 1

A shapefile cannot hold an arc. This is an ordinary shapefile, read by every program, with one more file
beside it that lets a program which knows it give the arcs back. The format is open: anyone may read or write it.

## The shapefile itself

An arc is written as short lines:

* the two ends of the arc stay where they are;
* the points between them are at equal angles, on a radius `ρ` a little larger than the arc's radius `r`,
  chosen so that the area between the short lines and the chord is the area between the arc and the chord:
  for `n` lines over a sweep `θ`, with `α = θ / n`,
  `(n − 2)·sin α·ρ² + 2·r·sin α·ρ − r²·θ = 0` (for `n = 2`: `ρ = r·θ / (2·sin α)`);
* `n` is the smallest number for which the lines are at most 0.1 mm longer than the arc and their length
  written to the centimetre equals the arc's (never coarser than 5° a line);
* the points are worked out from the end of the arc that is smaller in (x, y), so two features sharing an arc
  get the same points and meet without a gap or an overlap.

So a polygon's area is exact in any program, and its perimeter to the centimetre. (Both cannot be exact: of
all lines between two points that close the same area, the arc is the shortest.)

Optionally the true measures are also attributes: `AREA_TRUE` and `PERIM_TRUE` for polygons, `LEN_TRUE` for
lines, in the units of the layer's CRS. The text is UTF-8 (`.cpg`).

## The `.crv` file

`<name>.crv`, next to `<name>.shp`: UTF-8 JSON.

```json
{
 "format": "biruni-shapefile-arcs",
 "version": 1,
 "written": "2026-11-06T12:00:00+00:00",
 "length_tolerance": 0.0001,
 "fields": {"parcel_num": "parcel_number_long"},
 "arcs": {"<fingerprint>": "CurvePolygon (CompoundCurve ((0 0, 100 0), CircularString (100 0, 120 50, 100 100), …))"}
}
```

* `arcs`: for every feature that had arcs, its true shape as WKT, under the feature's **fingerprint**: the
  SHA-1 (hex) of the WKB of the feature's shape as stored in the `.shp` (as the reader's library gives it).
* `fields`: the names the DBF shortened to ten characters, with the full names.

## Reading

For each feature of the shapefile, compute the fingerprint of its shape. If `arcs` has it, the feature's shape
is the WKT stored there; if not, the feature is as it is in the shapefile.

A feature another program changed has another fingerprint: the arcs kept for it are no longer claimed, and it
is taken as that program left it. Features deleted, added or reordered by another program need no care: the
match is by shape, never by record number.
