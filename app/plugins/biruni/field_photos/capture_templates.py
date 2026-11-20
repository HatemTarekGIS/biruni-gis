"""Built-in capture templates for survey offices: building, villa, vacant land and shop."""
from ..i18n import tr
from .capture import clean_schema


def _job_questions():
    return [
        {"name": "job_no", "label": tr("Job number"), "type": "text", "req": True, "title": True},
        {"name": "client", "label": tr("Client / owner"), "type": "text", "req": True},
        {"name": "deed_no", "label": tr("Deed number"), "type": "text"},
        {"name": "district", "label": tr("District"), "type": "text"},
    ]


def building_template():
    """Multi-floor building survey template."""
    return clean_schema({
        "biruni_capture": 1,
        "id": "building",
        "rev": 1,
        "title": tr("Building survey"),
        "job": _job_questions(),
        "nodes": [
            {
                "id": "facades",
                "label": tr("Facades"),
                "req": True,
                "nodes": [
                    {"id": "front", "label": tr("Front facade"), "photos": [1, 3], "req": True},
                    {"id": "right", "label": tr("Right facade"), "photos": [1, 3], "req": True},
                    {"id": "left", "label": tr("Left facade"), "photos": [1, 3], "req": True},
                    {"id": "back", "label": tr("Back facade"), "photos": [1, 3], "req": True},
                ],
            },
            {"id": "entrance", "label": tr("Entrance"), "photos": [1, 4], "req": True},
            {"id": "stairs", "label": tr("Stairs"), "photos": [1, 4], "req": True},
            {"id": "roof", "label": tr("Roof"), "photos": [1, 6], "req": False},
            {
                "id": "floors",
                "label": tr("Floor"),
                "req": True,
                "repeat": {"min": 1, "max": 60},
                "q": [
                    {"name": "floor_name", "label": tr("Floor number / name"), "type": "text", "title": True},
                ],
                "nodes": [
                    {
                        "id": "apts",
                        "label": tr("Apartment"),
                        "req": True,
                        "repeat": {"min": 0, "max": 40},
                        "q": [
                            {"name": "apt_no", "label": tr("Apartment number"), "type": "text",
                             "req": True, "title": True},
                            {"name": "use", "label": tr("Use"), "type": "choice", "req": True,
                             "choices": [tr("Residential"), tr("Commercial"), tr("Office")]},
                            {"name": "condition", "label": tr("Condition"), "type": "choice", "req": True,
                             "choices": [tr("Good"), tr("Fair"), tr("Poor")]},
                            {"name": "rooms_count", "label": tr("Rooms count"), "type": "integer",
                             "min": 1, "max": 30},
                        ],
                        "nodes": [
                            {
                                "id": "rooms",
                                "label": tr("Room"),
                                "req": True,
                                "repeat": {"min": 1, "max": 30},
                                "photos": [2, 10],
                                "q": [
                                    {"name": "room_type", "label": tr("Room type"), "type": "choice",
                                     "req": True, "title": True,
                                     "choices": [tr("Bedroom"), tr("Living room"), tr("Kitchen"),
                                                 tr("Bathroom"), tr("Reception"), tr("Balcony"), tr("Other")]},
                                ],
                            },
                        ],
                    },
                ],
            },
            {
                "id": "services",
                "label": tr("Services"),
                "req": False,
                "nodes": [
                    {"id": "elec_meter", "label": tr("Electricity meter"), "photos": [1, 3], "req": False},
                    {"id": "water_meter", "label": tr("Water meter"), "photos": [1, 3], "req": False},
                ],
            },
        ],
    })


def villa_template():
    """Residential villa survey template."""
    return clean_schema({
        "biruni_capture": 1,
        "id": "villa",
        "rev": 1,
        "title": tr("Villa survey"),
        "job": _job_questions(),
        "nodes": [
            {
                "id": "exterior",
                "label": tr("Exterior & yard"),
                "req": True,
                "nodes": [
                    {"id": "street_view", "label": tr("Street view & gate"), "photos": [1, 4], "req": True},
                    {"id": "yard", "label": tr("Courtyard / garden"), "photos": [1, 4], "req": True},
                    {"id": "parking", "label": tr("Parking / garage"), "photos": [1, 3], "req": False},
                    {"id": "annex", "label": tr("External annex"), "photos": [1, 4], "req": False},
                ],
            },
            {
                "id": "v_floors",
                "label": tr("Floor"),
                "req": True,
                "repeat": {"min": 1, "max": 6},
                "q": [
                    {"name": "level_name", "label": tr("Floor"), "type": "choice", "req": True, "title": True,
                     "choices": [tr("Basement"), tr("Ground floor"), tr("First floor"),
                                 tr("Upper annex"), tr("Roof")]},
                ],
                "nodes": [
                    {
                        "id": "v_rooms",
                        "label": tr("Room"),
                        "req": True,
                        "repeat": {"min": 1, "max": 25},
                        "photos": [2, 8],
                        "q": [
                            {"name": "room_type", "label": tr("Room type"), "type": "choice",
                             "req": True, "title": True,
                             "choices": [tr("Reception"), tr("Living room"), tr("Bedroom"),
                                         tr("Kitchen"), tr("Bathroom"), tr("Balcony"), tr("Other")]},
                            {"name": "condition", "label": tr("Condition"), "type": "choice",
                             "choices": [tr("Good"), tr("Fair"), tr("Poor")]},
                        ],
                    },
                ],
            },
            {
                "id": "v_services",
                "label": tr("Services"),
                "req": False,
                "nodes": [
                    {"id": "v_elec", "label": tr("Electricity meter"), "photos": [1, 2], "req": False},
                    {"id": "v_water", "label": tr("Water meter"), "photos": [1, 2], "req": False},
                ],
            },
        ],
    })


def vacant_land_template():
    """Vacant land / plot survey template."""
    return clean_schema({
        "biruni_capture": 1,
        "id": "vacant_land",
        "rev": 1,
        "title": tr("Vacant land survey"),
        "job": _job_questions() + [
            {"name": "plot_no", "label": tr("Plot number"), "type": "text", "req": True},
            {"name": "plan_no", "label": tr("Plan number"), "type": "text"},
        ],
        "nodes": [
            {
                "id": "corners",
                "label": tr("Corner"),
                "req": True,
                "repeat": {"min": 3, "max": 30},
                "photos": [1, 3],
                "q": [
                    {"name": "corner_id", "label": tr("Corner number"), "type": "text",
                     "req": True, "title": True},
                    {"name": "marker_type", "label": tr("Boundary marker"), "type": "choice", "req": True,
                     "choices": [tr("Iron peg"), tr("Concrete monument"), tr("Wall corner"),
                                 tr("Paint mark"), tr("Unmarked")]},
                ],
            },
            {
                "id": "boundaries",
                "label": tr("Boundaries"),
                "req": True,
                "nodes": [
                    {"id": "north_bound", "label": tr("North boundary"), "photos": [1, 3], "req": True},
                    {"id": "east_bound", "label": tr("East boundary"), "photos": [1, 3], "req": True},
                    {"id": "south_bound", "label": tr("South boundary"), "photos": [1, 3], "req": True},
                    {"id": "west_bound", "label": tr("West boundary"), "photos": [1, 3], "req": True},
                ],
            },
            {
                "id": "access_road",
                "label": tr("Access road"),
                "req": True,
                "photos": [1, 4],
                "q": [
                    {"name": "road_type", "label": tr("Road surface"), "type": "choice", "req": True,
                     "choices": [tr("Paved"), tr("Unpaved"), tr("No road")]},
                    {"name": "road_width", "label": tr("Road width (m)"), "type": "number", "min": 0, "max": 200},
                ],
            },
            {
                "id": "encroachments",
                "label": tr("Encroachments"),
                "req": False,
                "photos": [0, 10],
                "q": [
                    {"name": "has_encroachment", "label": tr("Encroachment present?"), "type": "yesno"},
                    {"name": "encroachment_desc", "label": tr("Encroachment details"), "type": "text"},
                ],
            },
        ],
    })


def shop_template():
    """Commercial shop / unit survey template."""
    return clean_schema({
        "biruni_capture": 1,
        "id": "shop",
        "rev": 1,
        "title": tr("Shop survey"),
        "job": _job_questions(),
        "nodes": [
            {
                "id": "signboard",
                "label": tr("Facade & signboard"),
                "req": True,
                "photos": [1, 4],
                "q": [
                    {"name": "shop_name", "label": tr("Commercial name"), "type": "text",
                     "req": True, "title": True},
                    {"name": "activity", "label": tr("Activity"), "type": "text", "req": True},
                    {"name": "frontage_m", "label": tr("Frontage width (m)"), "type": "number",
                     "min": 0.5, "max": 200},
                ],
            },
            {
                "id": "hall",
                "label": tr("Main hall"),
                "req": True,
                "photos": [2, 8],
                "q": [
                    {"name": "area_m2", "label": tr("Approximate area (m²)"), "type": "number",
                     "min": 1, "max": 50000},
                    {"name": "condition", "label": tr("Condition"), "type": "choice", "req": True,
                     "choices": [tr("Good"), tr("Fair"), tr("Poor")]},
                ],
            },
            {"id": "mezzanine", "label": tr("Mezzanine"), "req": False, "photos": [1, 4]},
            {"id": "storage", "label": tr("Storage / back room"), "req": False, "photos": [1, 4]},
            {
                "id": "safety",
                "label": tr("Safety & services"),
                "req": True,
                "photos": [1, 4],
                "q": [
                    {"name": "fire_ext", "label": tr("Fire extinguisher present?"), "type": "yesno", "req": True},
                    {"name": "license_shown", "label": tr("License displayed?"), "type": "yesno"},
                ],
            },
        ],
    })


def builtin_templates():
    """Return all four built-in templates in order."""
    return [building_template(), villa_template(), vacant_land_template(), shop_template()]


def template_by_id(template_id):
    for t in builtin_templates():
        if t["id"] == template_id:
            return t
    return None
