"""Place-name normalization shared by station geocoding and user-input geocoding.

The price file writes city names the way a clerk typed them ("St. Louis",
"Mc Lean", "S Coffeyville", "Fort Worth   "), and users type them their own way.
Both sides go through ``normalize_place`` before the offline lookup, and the
places dataset is stored already normalized with the same function
(``scripts/build_places_dataset.py``), so a key built here always matches a key
built there.
"""

import re
import unicodedata

# Token-level expansions. Applied to whole words only.
_TOKEN_MAP = {
    "st": "saint",
    "ste": "sainte",
    "ft": "fort",
    "mt": "mount",
    "pt": "point",
}
# A single compass letter at the start ("S Coffeyville", "N Little Rock").
_LEADING_DIRECTION = {"n": "north", "s": "south", "e": "east", "w": "west"}

US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois",
    "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana",
    "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon",
    "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia",
    "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
}
_STATE_BY_NAME = {name.lower(): code for code, name in US_STATES.items()}
_STATE_BY_NAME["washington dc"] = _STATE_BY_NAME["district of columbia"]

# Regions that are NOT US states, recognised in "City, XX" so that "Toronto, ON" is
# answered "outside the USA" at once instead of being sent to a geocoder restricted
# to the USA (which then matches a street called "Toronto Court"). A code that is
# also a US state code is never read as foreign: the US state wins.
US_TERRITORIES = {
    "PR": "Puerto Rico", "VI": "U.S. Virgin Islands", "GU": "Guam", "AS": "American Samoa",
    "MP": "Northern Mariana Islands", "UM": "U.S. Minor Outlying Islands",
}
CANADA_PROVINCES = {
    "AB": "Alberta", "BC": "British Columbia", "MB": "Manitoba", "NB": "New Brunswick",
    "NL": "Newfoundland and Labrador", "NS": "Nova Scotia", "NT": "Northwest Territories", "NU": "Nunavut",
    "ON": "Ontario", "PE": "Prince Edward Island", "QC": "Quebec", "SK": "Saskatchewan", "YT": "Yukon",
}
# ISO 3166-2:MX codes (three letters) and the usual abbreviations. "NL" (Nuevo Leon,
# also Newfoundland) and "BC" (Baja California, also British Columbia) are outside
# the USA either way.
MEXICO_STATES = {
    "AGU": "Aguascalientes", "BCN": "Baja California", "BCS": "Baja California Sur", "CAM": "Campeche",
    "CHP": "Chiapas", "CHH": "Chihuahua", "CMX": "Ciudad de Mexico", "COA": "Coahuila", "COL": "Colima",
    "DUR": "Durango", "GUA": "Guanajuato", "GRO": "Guerrero", "HID": "Hidalgo", "JAL": "Jalisco",
    "MEX": "Estado de Mexico", "MIC": "Michoacan", "MOR": "Morelos", "NAY": "Nayarit", "NLE": "Nuevo Leon",
    "OAX": "Oaxaca", "PUE": "Puebla", "QUE": "Queretaro", "ROO": "Quintana Roo", "SLP": "San Luis Potosi",
    "SIN": "Sinaloa", "SON": "Sonora", "TAB": "Tabasco", "TAM": "Tamaulipas", "TLA": "Tlaxcala",
    "VER": "Veracruz", "YUC": "Yucatan", "ZAC": "Zacatecas",
}
_MEXICO_ALIASES = {"CDMX": "CMX", "DF": "CMX", "QROO": "ROO", "NL": "NLE"}
_COUNTRIES = {"canada": "Canada", "mexico": "Mexico"}

TERRITORY, CANADA, MEXICO = "us_territory", "canada", "mexico"


def foreign_region(value: str, codes_only: bool = False) -> tuple[str, str] | None:
    """(kind, name) when ``value`` is a US territory, a Canadian province or territory,
    a Mexican state, or Canada / Mexico themselves; None otherwise (US states too).

    "ON" -> ("canada", "Ontario"); "Nuevo León" -> ("mexico", "Nuevo Leon");
    "PR" -> ("us_territory", "Puerto Rico"). ``codes_only``: match codes, not names.
    """
    code = " ".join(value.replace(".", "").split()).upper()
    if code in US_STATES:
        return None
    if code in US_TERRITORIES:
        return TERRITORY, US_TERRITORIES[code]
    if code in CANADA_PROVINCES:
        return CANADA, CANADA_PROVINCES[code]
    if code in MEXICO_STATES or code in _MEXICO_ALIASES:
        return MEXICO, MEXICO_STATES[_MEXICO_ALIASES.get(code, code)]
    if codes_only:
        return None
    name = normalize_place(value)
    if name in _COUNTRIES:
        return (CANADA if name == "canada" else MEXICO), _COUNTRIES[name]
    for kind, table in ((TERRITORY, US_TERRITORIES), (CANADA, CANADA_PROVINCES), (MEXICO, MEXICO_STATES)):
        for full in table.values():
            if normalize_place(full) == name:
                return kind, full
    return None


def normalize_place(name: str) -> str:
    """Lowercase, strip accents and punctuation, expand common abbreviations.

    A leading "The" is dropped, so "The Bronx" (the official name) and "Bronx"
    (what the price file and most people write) are the same key.

    >>> normalize_place("St. Louis")
    'saint louis'
    >>> normalize_place("O'Fallon")
    'ofallon'
    >>> normalize_place("The Bronx")
    'bronx'
    """
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    text = text.lower().replace("'", "").replace(".", " ")
    text = re.sub(r"[^a-z0-9]+", " ", text).strip()
    tokens = text.split()
    if len(tokens) > 1 and tokens[0] == "the":
        tokens = tokens[1:]
    if len(tokens) > 1 and tokens[0] in _LEADING_DIRECTION:
        tokens[0] = _LEADING_DIRECTION[tokens[0]]
    return " ".join(_TOKEN_MAP.get(token, token) for token in tokens)


def compact(normalized: str) -> str:
    """Second-chance key without spaces: "mc lean" == "mclean", "la place" == "laplace"."""
    return normalized.replace(" ", "")


def normalize_state(value: str) -> str | None:
    """Return the 2-letter USPS code for "TX", "tx", "Texas" or "D.C."; None if not a US state."""
    value = " ".join(value.replace(".", "").split())
    if value.upper() in US_STATES:
        return value.upper()
    return _STATE_BY_NAME.get(value.lower())
