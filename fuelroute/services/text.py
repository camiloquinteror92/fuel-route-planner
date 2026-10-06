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
