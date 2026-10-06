"""Place-name normalization shared by station geocoding and user input geocoding."""

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


def normalize_place(name: str) -> str:
    """Lowercase, strip accents and punctuation, expand common abbreviations.

    >>> normalize_place("St. Louis")
    'saint louis'
    >>> normalize_place("O'Fallon")
    'ofallon'
    """
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    text = text.lower().replace("'", "").replace(".", " ")
    text = re.sub(r"[^a-z0-9]+", " ", text).strip()
    tokens = text.split()
    if len(tokens) > 1 and tokens[0] in _LEADING_DIRECTION:
        tokens[0] = _LEADING_DIRECTION[tokens[0]]
    return " ".join(_TOKEN_MAP.get(token, token) for token in tokens)


def compact(normalized: str) -> str:
    """Second-chance key without spaces: "mc lean" == "mclean", "la place" == "laplace"."""
    return normalized.replace(" ", "")


def normalize_state(value: str) -> str | None:
    """Return the 2-letter USPS code for "TX", "tx" or "Texas"; None if not a US state."""
    value = value.strip().rstrip(".")
    if value.upper() in US_STATES:
        return value.upper()
    return _STATE_BY_NAME.get(value.lower())
