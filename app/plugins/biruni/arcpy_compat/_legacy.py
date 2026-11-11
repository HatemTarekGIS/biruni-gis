"""ArcGIS 10.0-style cursors (arcpy.SearchCursor / UpdateCursor / InsertCursor with Row objects) and simple
FieldMappings / FieldMap / ValueTable holders."""
from . import da
from . import data as D
from ._base import tool_warning


class Row:
    def __init__(self, names, values, shape_name):
        object.__setattr__(self, "_names", list(names))
        object.__setattr__(self, "_values", list(values))
        object.__setattr__(self, "_shape", shape_name)

    def _i(self, name):
        low = str(name).lower()
        for i, n in enumerate(self._names):
            if n.lower() == low:
                return i
        if self._shape and low == self._shape.lower():
            return self._names.index("SHAPE@")
        raise RuntimeError(f"Row: Field {name} does not exist")

    def getValue(self, field_name):  # noqa: N802
        return self._values[self._i(field_name)]

    def setValue(self, field_name, value):  # noqa: N802
        self._values[self._i(field_name)] = value

    def isNull(self, field_name):  # noqa: N802
        return self.getValue(field_name) is None

    def setNull(self, field_name):  # noqa: N802
        self.setValue(field_name, None)

    def __getattr__(self, name):
        try:
            return self.getValue(name)
        except RuntimeError:
            raise AttributeError(name) from None

    def __setattr__(self, name, value):
        self.setValue(name, value)


def _names_for(dataset, fields):
    info = D.Source(dataset).info()
    attrs = [fd.GetName() for fd in info["fields"]]
    if fields:
        wanted = [f.strip() for f in str(fields).split(";") if f.strip()] if isinstance(fields, str) else list(fields)
        attrs = [a for a in attrs if a.lower() in {w.lower() for w in wanted}]
    names = ["OID@"] + (["SHAPE@"] if info["shape"] else []) + attrs
    return names, info


def _sql(sort_fields):
    if not sort_fields:
        return (None, None)
    parts = []
    for item in str(sort_fields).split(";"):
        bits = item.strip().split()
        if bits:
            parts.append(f"{bits[0]} {'DESC' if len(bits) > 1 and bits[1].upper().startswith('D') else 'ASC'}")
    return (None, "ORDER BY " + ", ".join(parts))


def SearchCursor(dataset, where_clause=None, spatial_reference=None, fields=None, sort_fields=None):  # noqa: N802
    names, info = _names_for(dataset, fields)
    cur = da.SearchCursor(dataset, names, where_clause, spatial_reference, sql_clause=_sql(sort_fields))
    labels = [info["oid"]] + names[1:]

    class _It:
        def __iter__(self):
            return self

        def __next__(self):
            return Row(labels, next(cur), info["shape"])

        def next(self):
            try:
                return self.__next__()
            except StopIteration:
                return None

        def reset(self):
            cur.reset()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    return _It()


def UpdateCursor(dataset, where_clause=None, spatial_reference=None, fields=None, sort_fields=None):  # noqa: N802
    names, info = _names_for(dataset, fields)
    cur = da.UpdateCursor(dataset, names, where_clause, spatial_reference, sql_clause=_sql(sort_fields))
    labels = [info["oid"]] + names[1:]

    class _It:
        def __iter__(self):
            return self

        def __next__(self):
            return Row(labels, next(cur), info["shape"])

        def next(self):
            try:
                return self.__next__()
            except StopIteration:
                return None

        def updateRow(self, row):  # noqa: N802
            cur.updateRow(row._values)

        def deleteRow(self, row=None):  # noqa: N802
            cur.deleteRow()

        def reset(self):
            cur.reset()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            cur._close()
            return False

        def __del__(self):
            try:
                cur._close()
            except Exception:
                pass

    return _It()


def InsertCursor(dataset, spatial_reference=None):  # noqa: N802
    names, info = _names_for(dataset, None)
    names = names[1:]
    cur = da.InsertCursor(dataset, names)
    labels = list(names)

    class _Ins:
        def newRow(self):  # noqa: N802
            return Row(labels, [None] * len(labels), info["shape"])

        def insertRow(self, row):  # noqa: N802
            return cur.insertRow(row._values)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            cur._close()
            return False

        def __del__(self):
            try:
                cur._close()
            except Exception:
                pass

    return _Ins()


# ---------------------------------------------------------------- field mappings (kept, then ignored by tools)

class FieldMap:
    def __init__(self):
        self._inputs = []
        self.mergeRule = "First"
        self.joinDelimiter = ""
        self.outputField = None

    def addInputField(self, table_dataset, field_name, start_position=-1, end_position=-1):  # noqa: N802
        self._inputs.append((str(table_dataset), str(field_name)))
        if self.outputField is None:
            for f in D.ListFields(table_dataset):
                if f.name.lower() == str(field_name).lower():
                    self.outputField = f
                    break

    def getInputFieldName(self, index):  # noqa: N802
        return self._inputs[index][1]

    def getInputTableName(self, index):  # noqa: N802
        return self._inputs[index][0]

    def removeInputField(self, index):  # noqa: N802
        del self._inputs[index]

    def removeAll(self):  # noqa: N802
        self._inputs = []

    def findInputFieldIndex(self, table_dataset, field_name):  # noqa: N802
        for i, (t, f) in enumerate(self._inputs):
            if f.lower() == str(field_name).lower():
                return i
        return -1

    @property
    def inputFieldCount(self):  # noqa: N802
        return len(self._inputs)


class FieldMappings:
    def __init__(self):
        self._maps = []
        tool_warning("FieldMappings are accepted but tools copy all fields as they are")

    def addTable(self, table_dataset):  # noqa: N802
        for f in D.ListFields(table_dataset):
            if f.type in ("OID", "Geometry"):
                continue
            fm = FieldMap()
            fm.addInputField(table_dataset, f.name)
            self._maps.append(fm)

    def addFieldMap(self, field_map):  # noqa: N802
        self._maps.append(field_map)

    def getFieldMap(self, index):  # noqa: N802
        return self._maps[index]

    def replaceFieldMap(self, index, value):  # noqa: N802
        self._maps[index] = value

    def removeFieldMap(self, index):  # noqa: N802
        del self._maps[index]

    def removeAll(self):  # noqa: N802
        self._maps = []

    def findFieldMapIndex(self, field_map_name):  # noqa: N802
        for i, fm in enumerate(self._maps):
            if fm.outputField is not None and fm.outputField.name.lower() == str(field_map_name).lower():
                return i
        return -1

    @property
    def fieldCount(self):  # noqa: N802
        return len(self._maps)

    @property
    def fields(self):
        return [fm.outputField for fm in self._maps if fm.outputField is not None]

    def exportToString(self):  # noqa: N802
        return ";".join(fm.outputField.name for fm in self._maps if fm.outputField is not None)


class ValueTable:
    def __init__(self, columns=1):
        self.columnCount = int(columns)
        self._rows = []

    def addRow(self, value):  # noqa: N802
        if isinstance(value, (list, tuple)):
            self._rows.append([str(v) for v in value])
        else:
            self._rows.append(str(value).split())

    def getRow(self, index):  # noqa: N802
        return " ".join(self._rows[index])

    def getValue(self, row, column):  # noqa: N802
        return self._rows[row][column]

    def setValue(self, row, column, value):  # noqa: N802
        self._rows[row][column] = str(value)

    def removeRow(self, index):  # noqa: N802
        del self._rows[index]

    @property
    def rowCount(self):  # noqa: N802
        return len(self._rows)

    def exportToString(self):  # noqa: N802
        return ";".join(" ".join(r) for r in self._rows)

    def __iter__(self):
        return iter(list(self._rows))

    def __str__(self):
        return self.exportToString()
