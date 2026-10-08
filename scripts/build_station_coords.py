"""Build ``data/station_coords.csv``: truck stop coordinates from OpenStreetMap.

This runs once, offline, before the app is used. The output is committed to the
repository and is meant for ``load_stations``: a station with a row gets those
coordinates, one without keeps its city center. The app (and whoever evaluates
it) never needs this script, the network or OpenStreetMap. Run it again only to
refresh the data.

Why. The OPIS price file has no coordinates, only a city and a highway address
("I-44, EXIT 283 & US-69"). The loader places each station at its CITY CENTER
(offline places index), and the 10-mile route corridor absorbs that error. This
script moves a station to where it really is when OpenStreetMap says so with
confidence, and leaves it at the city center otherwise.

How.

1. Download, ONE STATE AT A TIME and only the states of the price file, from the
   Overpass API (``overpass-api.de``; fallbacks ``overpass.kumi.systems`` and
   ``maps.mail.ru``):

   * every fuel station (``nwr[amenity=fuel]``, with its center and tags);
   * every motorway exit (``node[highway=motorway_junction]``, whose ``ref`` is
     the exit number) and the motorway / trunk / primary ways that contain
     them, so each exit knows the highway it is on ("I 44").

   Two requests per state (fuel; exits), sequential, with a descriptive User-Agent,
   a pause between states, the next endpoint on 429 / 5xx / timeouts and back-off
   when all of them failed. An instance whose OSM copy is more than 14 days old
   is not used (a stale mirror once answered with partial data). Raw answers
   (both requests merged) are cached in ``.cache/osm/<STATE>.json``
   (git-ignored): a re-run does not download again.

2. Match every OPIS station of the state, inside 25 miles of its current city
   center (the same one ``load_stations`` computes), with these rules, in order:

   * ``osm_fuel``: an OSM fuel station whose name / brand / operator matches the
     OPIS name (brands normalised: "PILOT TRAVEL CENTER #1243" = "Pilot",
     "TravelCenters of America" = "TA", "KWIK STAR" = "Kwik Trip"...), and

     - when the address has an exit that was found in OSM: within 1.5 miles of
       that exit. A site whose OSM store number is the OPIS one wins; a site that
       carries only other store numbers is another store and is not taken (high;
       medium if several sites match there or one was set aside);
     - otherwise: the OSM ``ref`` (or name / branch) carries the OPIS store number
       ("#1243") and only one site has it within 25 miles (high). A name alone is
       not enough away from an exit: the price file often lists several stores of
       a chain in one town, and they would all land on the one OSM knows.

   * ``osm_exit``: the exit node whose number is the address' exit number, on a
     way of the address' highway, within 25 miles. Several nodes (both directions
     of the same interchange): the closest to the city center, or their midpoint
     when they are more than 1.5 miles apart (frontage-road ramps). If the nodes
     with that number are more than 5 miles apart (two different exits), nothing
     is used. The number before a renumbering (OSM ``old_ref``) counts too, so
     that an old exit number is not taken for the new exit with the same number.
     In MA and RI (every exit renumbered in 2021, ``old_ref`` mostly missing) an
     exit matched on its current number must also be within 10 miles of the city.
     A state route of the address is read as the US route with that number
     ("SR-395" = US 395) only when no state route with that number has an exit in
     the area (medium). The address alone is not trusted far from town: such a
     row is medium beyond 10 miles, dropped beyond 20 miles, and dropped beyond
     5 miles when the chain's only store in town is in OSM and none is at the exit
     (the price file sometimes gives the nearest exit of a store that is in town).

   * one OSM fuel station is one store: when OPIS stations of DIFFERENT exits (or
     without an exit) get the same one, the one whose store number OSM carries
     keeps it; the others are matched again without it (usually their own exit
     node). OPIS stations of the same exit may share it (one truck stop listed
     twice).

   * otherwise the station keeps its city center (no row in the CSV).

   Stations whose city name is ambiguous (homonyms far apart; ``load_stations``
   leaves them without coordinates) are placed only by an exit match: within
   25 miles of one of the homonyms, and no node with that number near another
   one (medium).

3. Write ``data/station_coords.csv`` (sorted by ``opis_id``) and
   ``data/station_coords_report.md``.

Licence: the output is derived from OpenStreetMap data, (c) OpenStreetMap
contributors, available under the Open Database License (ODbL 1.0).

Usage (from the repository root)::

    python scripts/build_station_coords.py               # download what is missing, match, write
    python scripts/build_station_coords.py --offline     # use the cache only
    python scripts/build_station_coords.py --refresh     # download every state again
    python scripts/build_station_coords.py --states TX,OK --output /tmp/coords.csv

The matching functions below are pure (no Django, no network) and unit-tested in
``fuelroute/tests/test_build_station_coords.py``.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import ssl
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PRICE_FILE = ROOT / "data" / "fuel-prices-for-be-assessment.csv"
OUTPUT = ROOT / "data" / "station_coords.csv"
REPORT = ROOT / "data" / "station_coords_report.md"
CACHE_DIR = ROOT / ".cache" / "osm"

# Public Overpass instances, tried in this order. The third one (VK Maps) is there because
# the first two were overloaded / down on the day of the first build (HTTP 504 / 500).
ENDPOINTS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
)
USER_AGENT = (
    "spotter-fuel-route/1.0 (one-time offline build of truck stop coordinates; "
    "scripts/build_station_coords.py; sequential, cached, two requests per US state)"
)
# Server-side [timeout:]. Busy servers turn away (HTTP 504) queries that declare a large
# budget; the biggest state answers in under 2 minutes.
QUERY_TIMEOUT_SECONDS = 300
HTTP_TIMEOUT_SECONDS = QUERY_TIMEOUT_SECONDS + 120
RETRY_DELAYS_SECONDS = (60, 120, 240, 300)  # after every endpoint failed
ENDPOINT_SWITCH_PAUSE_SECONDS = 5
# An instance whose database is older than this is not used: on 2026-10-08 one mirror
# answered with data from May and an exits answer for Texas that stopped at Houston.
MAX_DATA_AGE_DAYS = 14
DEFAULT_PAUSE_SECONDS = 20.0

# Matching rules (miles).
AREA_MILES = 25.0  # a station is looked for this far from its city center
FUEL_NEAR_EXIT_MILES = 1.5  # an OSM fuel station this close to the address' exit
FUEL_UNIQUE_MILES = 5.0  # no exit: the brand must be unique this close to the city center
EXIT_SPREAD_MILES = 5.0  # matching exit nodes further apart than this: two exits, skip
EXIT_TIGHT_MILES = 1.5  # matching exit nodes all within this: one interchange (high)
SAME_SITE_MILES = 0.35  # OSM fuel features closer than this are one site (car + truck canopies)
# States that replaced sequential exit numbers by mile-based ones in 2021: the price file
# uses both, and OSM rarely keeps the old number (old_ref). There an exit number is
# trusted only near the city: the other numbering's exit N is typically 15-25 miles away
# ("I-95, EXIT 60", Salisbury MA: its old exit 60; the new exit 60 is 24 miles south).
RENUMBERED_STATES = frozenset({"MA", "RI"})
RENUMBERED_MAX_MILES = 10.0
# An exit node with no fuel station of the right name next to it rests on the address
# alone. The price file sometimes gives the NEAREST interstate exit of a store that is in
# town ("CASEYS #3798", Drumright OK: "I-44, EXIT 211", 22 miles away). Such a row is
# medium beyond EXIT_ONLY_HIGH_MILES, not written beyond EXIT_ONLY_MAX_MILES, and not
# written beyond FUEL_UNIQUE_MILES when OSM has a station of that name in town
# (within IN_TOWN_MILES of the city center).
EXIT_ONLY_HIGH_MILES = 10.0
EXIT_ONLY_MAX_MILES = 20.0
IN_TOWN_MILES = 3.0

CSV_COLUMNS = (
    "opis_id", "lat", "lon", "source", "confidence", "osm_type_id",
    "matched_label", "miles_from_city_center", "reason",
)


# --------------------------------------------------------------------------------------
# Geometry
# --------------------------------------------------------------------------------------

EARTH_RADIUS_MILES = 3958.8


def miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle (haversine) distance in miles."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_MILES * math.asin(math.sqrt(min(1.0, h)))


class GridIndex:
    """Points bucketed in 0.2-degree cells: "what is within R miles of here"."""

    CELL = 0.2

    def __init__(self, items):
        self._cells: dict[tuple[int, int], list] = defaultdict(list)
        for item in items:
            self._cells[self._cell(item.lat, item.lon)].append(item)

    def _cell(self, lat: float, lon: float) -> tuple[int, int]:
        return math.floor(lat / self.CELL), math.floor(lon / self.CELL)

    def near(self, lat: float, lon: float, radius: float) -> list[tuple[float, object]]:
        """[(miles, item)] within ``radius`` miles, nearest first."""
        dlat = radius / 69.0
        dlon = radius / max(1.0, 69.17 * math.cos(math.radians(lat)))
        (r0, c0), (r1, c1) = self._cell(lat - dlat, lon - dlon), self._cell(lat + dlat, lon + dlon)
        found = []
        for row in range(r0, r1 + 1):
            for col in range(c0, c1 + 1):
                for item in self._cells.get((row, col), ()):
                    d = miles(lat, lon, item.lat, item.lon)
                    if d <= radius:
                        found.append((d, item))
        found.sort(key=lambda pair: pair[0])
        return found

    def near_any(self, points, radius: float) -> list[tuple[float, object]]:
        """Like ``near`` for the union of several centers; distance = to the nearest one."""
        best: dict[int, tuple[float, object]] = {}
        for lat, lon in points:
            for d, item in self.near(lat, lon, radius):
                key = id(item)
                if key not in best or d < best[key][0]:
                    best[key] = (d, item)
        return sorted(best.values(), key=lambda pair: pair[0])


# --------------------------------------------------------------------------------------
# Address parsing: "I-44, EXIT 283 & US-69" -> highways and exits
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Highway:
    kind: str  # I, US, SR (state route), HWY (US or state, unknown), FM, CR
    number: int
    suffix: str = ""  # "E" of I-35E, or a direction letter ("I-81N")

    @property
    def label(self) -> str:
        return f"{self.kind}-{self.number}{self.suffix}"


@dataclass(frozen=True)
class ExitRef:
    highways: tuple[Highway, ...]  # the highway(s) the exit is on, as written before "EXIT"
    numbers: tuple[tuple[int, str], ...]  # (283, "A"); several for "EXIT 88/89"

    @property
    def label(self) -> str:
        roads = "/".join(h.label for h in self.highways) or "?"
        return f"{roads} exit " + "/".join(f"{n}{letter}" for n, letter in self.numbers)


@dataclass(frozen=True)
class ParsedAddress:
    highways: tuple[Highway, ...]
    exits: tuple[ExitRef, ...]


_US_PREFIX = r"US[\s-]*(?:HWY|HIGHWAY|ROUTE|RTE|RT)?"
_HIGHWAY_RE = re.compile(
    r"(?<![A-Z])(?P<prefix>INTERSTATE|IH|"
    + _US_PREFIX
    + r"|STATE\s+(?:ROUTE|ROAD|HWY|HIGHWAY)|HWYY|HWY|HIGHWAY|ROUTE|RTE|RT|SR|SH|ST|FM|RM|CR|[A-Z]{1,2})"
    r"\s*-?\s*(?P<number>\d{1,4})(?P<suffix>[A-Z](?![A-Z]))?(?![0-9])"
)
_EXIT_RE = re.compile(
    # The letter of "EXIT 144-B" / "EXIT 15 B", but not the "I" of "EXIT 39 I-77".
    # "EX 360" and "EXT 138C" are abbreviations of EXIT.
    r"(?<![A-Z])(?:EXIT|EXT|EX)(?![A-Z])[\s,.\-#]*(?P<number>\d{1,3})"
    r"(?:\s*-?\s*(?P<letter>[A-Z])(?![A-Z])(?!\s*-?\s*\d))?(?![0-9])"
    r"(?P<more>(?:\s*/\s*\d{1,3}(?:[A-Z](?![A-Z]))?(?![0-9]))*)"
)
_MORE_EXITS_RE = re.compile(r"(\d{1,3})([A-Z]?)")
# Prefixes that mean "state route" in one state only (Michigan M-14, Kansas K-10).
_STATE_LETTER_ROUTES = {"MI": "M", "KS": "K"}


def _highway_kind(prefix: str, state: str) -> str | None:
    prefix = " ".join(prefix.split())
    if prefix in ("I", "IH", "INTERSTATE"):
        return "I"
    if prefix.startswith("US"):
        return "US"
    if prefix in ("SR", "SH", "ST", "RT", "RTE", "ROUTE") or prefix.startswith("STATE "):
        return "SR"
    if prefix in ("HWY", "HWYY", "HIGHWAY"):
        return "HWY"
    if prefix in ("FM", "RM"):
        return "FM"
    if prefix == "CR":
        return "CR"
    if prefix == state or prefix == _STATE_LETTER_ROUTES.get(state):
        return "SR"  # "TX-21", "WI-29", "M-57" in Michigan
    return None


def parse_address(address: str, state: str) -> ParsedAddress:
    """Highways and exits of an OPIS address.

    "I-44, EXIT 283 & US-69"        -> highways I-44, US-69; exit 283 on I-44
    "I-29 & I-80, EXIT 1B"          -> exit 1B on I-29 or I-80 (concurrent)
    "I-85, EXIT 39 I-77, EXIT 13"   -> exit 39 on I-85 and exit 13 on I-77
    "I-75, EXIT 144-B"              -> exit 144B on I-75

    An exit belongs to the highways written between the previous exit (or the start)
    and the word EXIT; when there are none there, to every highway of the address.
    """
    text = " ".join(address.upper().split())
    state = state.strip().upper()
    found: list[tuple[int, Highway]] = []
    for match in _HIGHWAY_RE.finditer(text):
        kind = _highway_kind(match.group("prefix"), state)
        if kind is None:
            continue
        found.append((match.start(), Highway(kind, int(match.group("number")), match.group("suffix") or "")))
    highways = tuple(dict.fromkeys(h for _, h in found))

    exits: list[ExitRef] = []
    previous_end = 0
    for match in _EXIT_RE.finditer(text):
        numbers = [(int(match.group("number")), match.group("letter") or "")]
        numbers += [(int(n), letter) for n, letter in _MORE_EXITS_RE.findall(match.group("more") or "")]
        before = tuple(dict.fromkeys(h for pos, h in found if previous_end <= pos < match.start()))
        exits.append(ExitRef(before or highways, tuple(dict.fromkeys(numbers))))
        previous_end = match.end()
    return ParsedAddress(highways, tuple(exits))


# --------------------------------------------------------------------------------------
# OSM tags: way refs ("I 44;US 69") and exit refs ("283A", "144A-B")
# --------------------------------------------------------------------------------------

_OSM_REF_RE = re.compile(r"^(?P<prefix>[A-Z]{1,5}|STATE ROUTE|STATE HIGHWAY)[\s-]*(?P<number>\d{1,4})(?P<suffix>[A-Z]?)(?:\s+(?P<tail>.*))?$")
# A ref with one of these words after the number is another road (US 69 Business).
_OTHER_ROAD_WORDS = re.compile(r"\b(BUS|BUSINESS|ALT|ALTERNATE|TRUCK|SPUR|BYP|BYPASS|LOOP|CONN|CONNECTOR|SCENIC)\b")


def parse_osm_highway_refs(ref: str | None, state: str) -> frozenset[Highway]:
    """Highways of an OSM way ``ref`` tag: "I 44;US 69" -> {I-44, US-69}; "OK 66" in OK -> {SR-66}.

    "DE 1 Toll" is DE 1. "US 69 Business" is skipped (a different road).
    """
    if not ref:
        return frozenset()
    state = state.strip().upper()
    result = set()
    for part in re.split(r"[;,]", ref.upper()):
        match = _OSM_REF_RE.match(" ".join(part.split()))
        if not match:
            continue
        if match.group("tail") and _OTHER_ROAD_WORDS.search(match.group("tail")):
            continue
        prefix = match.group("prefix")
        if prefix == "I":
            kind = "I"
        elif prefix == "US":
            kind = "US"
        elif prefix in ("FM", "RM"):
            kind = "FM"
        elif prefix in ("SR", "SH", "STATE ROUTE", "STATE HIGHWAY") or prefix == state or prefix == _STATE_LETTER_ROUTES.get(state):
            # Before "CO" = county road: in Colorado "CO 470" is the state highway.
            kind = "SR"
        elif prefix in ("CR", "CO"):
            kind = "CR"
        else:
            continue
        result.add(Highway(kind, int(match.group("number")), match.group("suffix")))
    return frozenset(result)


def parse_osm_exit_refs(ref: str | None) -> frozenset[tuple[int, str]]:
    """Exit numbers of a ``motorway_junction`` ref: "283A" -> {(283, "A")}; "144A-B" -> 144A and 144B."""
    if not ref:
        return frozenset()
    result = set()
    for part in re.split(r"[;,/]", ref.upper()):
        match = re.match(r"^\s*(\d{1,3})\s*-?\s*([A-Z](?:\s*-?\s*[A-Z])*)?\s*$", part)
        if not match:
            continue
        number = int(match.group(1))
        letters = re.sub(r"[^A-Z]", "", match.group(2) or "")
        if letters:
            result.update((number, letter) for letter in letters)
        else:
            result.add((number, ""))
    return frozenset(result)


def highways_compatible(address: Highway, osm: Highway, relaxed: bool = False) -> bool:
    """Same road? Kinds agree (HWY = US or state route), same number, and suffixes agree
    or one side has none ("I-81N" in an address is northbound I 81; I-35E is not I-35W).

    ``relaxed``: a state route and a US route with the same number also agree. The price
    file sometimes writes a US route as a state route ("SR-395, EXIT 78" in Reno is
    US 395); ``find_exit`` uses this only when no state route with that number is near.
    """
    if address.number != osm.number:
        return False
    if address.kind == "HWY":
        if osm.kind not in ("US", "SR"):
            return False
    elif address.kind != osm.kind and not (relaxed and {address.kind, osm.kind} == {"US", "SR"}):
        return False
    return not address.suffix or not osm.suffix or address.suffix == osm.suffix


def exit_match_level(numbers, osm_refs) -> int:
    """2: same number and letter; 1: same number, letters differ or missing; 0: no match."""
    level = 0
    for number, letter in numbers:
        if (number, letter) in osm_refs:
            return 2
        if any(n == number for n, _ in osm_refs):
            level = 1
    return level


# --------------------------------------------------------------------------------------
# Names and brands
# --------------------------------------------------------------------------------------

# Applied in order to the normalized text (lower case, ASCII, no apostrophes).
_BRAND_ALIASES = (
    (r"\btravel ?centers? of america\b", "ta"),
    (r"\bpetro ?card\b", "petrocard"),  # Petro-Card 24, a cardlock, is not Petro
    (r"\bpetro stopping cent(?:er|re)s?\b", "petro"),
    (r"\bpilot flying j\b", "pilot flyingj"),
    (r"\bflying j\b", "flyingj"),
    (r"\bkwik (?:trip|star)\b", "kwiktrip"),
    (r"\bquik ?trip\b", "quiktrip"),
    (r"^qt\b", "quiktrip"),
    (r"\b(?:7|seven) ?eleven\b", "7eleven"),
    (r"\bone ?9\b", "one9"),
    (r"\bcircle k\b", "circlek"),
    (r"\bkum (?:and )?go\b", "kumandgo"),
    (r"\bphillips 66\b", "phillips66"),
    (r"\bbuc ?ees\b", "bucees"),
    (r"\bsapp bros?\b", "sappbros"),
    (r"\broad ranger\b", "roadranger"),
    (r"\broyal farms\b", "royalfarms"),
    (r"\btown pump\b", "townpump"),
    (r"\bholiday station ?stores?\b", "holiday"),
    (r"\bmurphy (?:usa|express)\b", "murphyusa"),
    (r"\btoot n totum\b", "tootntotum"),
    (r"\b(?:pac|pacific) pride\b", "pacificpride"),  # a cardlock network, not "Pride" stations
)
_BRAND_ALIASES = tuple((re.compile(pattern), repl) for pattern, repl in _BRAND_ALIASES)
# Short or generic-looking tokens that are, alone, a brand.
KNOWN_BRANDS = frozenset({
    "ta", "bp", "76", "qt", "one9", "7eleven", "flyingj", "pilot", "loves", "petro", "quiktrip",
    "kwiktrip", "circlek", "bucees", "gulf", "hess", "arco", "mobil", "exxon", "shell", "citgo",
    "cenex", "conoco", "valero", "sunoco", "amoco", "chevron", "texaco", "marathon", "speedway",
    "sinclair", "phillips66", "wawa", "sheetz", "caseys", "racetrac", "raceway", "maverik",
    "holiday", "stripes", "murphyusa", "roadranger", "sappbros", "royalfarms", "townpump",
    "kumandgo", "ambest", "fastrac", "hucks", "tootntotum", "quarles", "allsups", "cefco",
})
# Words that say what the place is, not which one it is.
GENERIC_WORDS = frozenset("""
    the of and at a an n
    travel travels center centers centre centres plaza plazas stop stops stopping truck trucks
    truckstop truckstops fuel fuels fueling gas gasoline station stations food foods mart market
    markets store stores shop shoppe inc llc co corp company express auto service services oil oils
    petroleum convenience country general quick mini hr hour hours cafe restaurant diesel dealer
    stationstore stationstores card cardlock fleet energy exit network
""".split())
_STORE_NUMBER_RE = re.compile(r"#\s*(\d+)")


def normalize_text(value: str | None) -> str:
    """Lower case ASCII words: "Love's Travel Stop #512" -> "loves travel stop 512"."""
    text = unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode().lower()
    text = re.sub(r"['`]", "", text).replace("&", " and ")
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def name_core(value: str | None) -> tuple[str, ...]:
    """The words that identify a station: brands merged, store numbers and generic words removed.

    "PILOT TRAVEL CENTER #1243" -> ("pilot",); "TravelCenters of America" -> ("ta",);
    "WOODSHED OF BIG CABIN" -> ("woodshed", "big", "cabin").
    """
    text = _STORE_NUMBER_RE.sub(" ", value or "")
    text = normalize_text(text)
    for pattern, repl in _BRAND_ALIASES:
        text = pattern.sub(repl, text)
    return tuple(
        token for token in text.split()
        if token not in GENERIC_WORDS and not (token.isdigit() and len(token) >= 3)
    )


def store_number(value: str | None) -> str | None:
    """"PILOT #1243" -> "1243" (leading zeros removed)."""
    match = _STORE_NUMBER_RE.search(value or "")
    if match is None:
        return None
    return match.group(1).lstrip("0") or "0"


def _contains(longer: tuple[str, ...], shorter: tuple[str, ...]) -> bool:
    n = len(shorter)
    return any(longer[i:i + n] == shorter for i in range(len(longer) - n + 1))


def names_match(opis: tuple[str, ...], osm: tuple[str, ...], city_words: frozenset[str] = frozenset()) -> bool:
    """Do two name cores designate the same business?

    The words of the city name are ignored first ("MOULTON COWBOYS" in Moulton is
    "Cowboys"; "Big Cabin Shell" and "Woodshed of Big Cabin" have nothing in common).
    Then: equal, or one STARTS the other ("woodshed" / "woodshed big cabin"), or the
    OSM name is a run of words of the OPIS name that contains a known brand ("shell" in
    "bear shell"). "Pride" is not "Pac Pride": the end of a name is not enough without
    a brand.

    The shorter one must be distinctive. An OPIS name that is a brand alone ("76",
    "PILOT", "SHELL") matches an OSM name that is the brand alone or with other brands
    ("Pilot Flying J"), not "Route 76", "Pilot Knob Mart" or "Shell Lake Cenex" (OSM's
    ``brand`` tag usually carries the brand alone). A single word that is not a brand
    must have 6+ letters ("best", "swift", "power" say little), and several words need a
    brand or a word of 4+ letters, unless both names are equal ("big d").
    """
    opis = tuple(w for w in opis if w not in city_words)
    osm = tuple(w for w in osm if w not in city_words)
    if not opis or not osm:
        return False
    if opis == osm:
        return any(w in KNOWN_BRANDS for w in opis) or len("".join(opis)) >= 4
    if len(opis) == 1 and opis[0] in KNOWN_BRANDS and len(osm) > 1:
        # A brand alone: the OSM name is that brand with other brands only ("Pilot Flying J").
        return opis[0] in osm and all(w in KNOWN_BRANDS for w in osm)
    opis_is_shorter = len(opis) <= len(osm)
    shorter, longer = (opis, osm) if opis_is_shorter else (osm, opis)
    if longer[: len(shorter)] != shorter and not (
        # The OSM name inside the OPIS one, with a brand: "Shell" in "BEAR SHELL"
        not opis_is_shorter and _contains(longer, shorter) and any(w in KNOWN_BRANDS for w in shorter)
    ):
        return False
    if len(shorter) == 1 and shorter[0] not in KNOWN_BRANDS:
        return len(shorter[0]) >= 6
    return any(w in KNOWN_BRANDS or len(w) >= 4 for w in shorter)


# --------------------------------------------------------------------------------------
# OSM data of one state
# --------------------------------------------------------------------------------------


@dataclass
class FuelPoi:
    osm_id: str  # "node/123", "way/456"
    lat: float
    lon: float
    tags: dict
    cores: tuple[tuple[str, ...], ...] = ()
    store_numbers: frozenset[str] = frozenset()

    @property
    def hgv(self) -> bool:
        return self.tags.get("hgv") in ("yes", "designated") or self.tags.get("fuel:HGV_diesel") == "yes"

    @property
    def label(self) -> str:
        name = self.tags.get("name") or self.tags.get("brand") or self.tags.get("operator") or "(unnamed)"
        extra = [
            f"{key} {self.tags[key]}" for key in ("brand", "operator", "ref")
            if self.tags.get(key) and self.tags[key] != name
        ]
        return f"{name} ({', '.join(extra)})" if extra else name


@dataclass
class ExitNode:
    osm_id: str
    lat: float
    lon: float
    exit_refs: frozenset[tuple[int, str]]
    highways: frozenset[Highway]
    tags: dict = field(default_factory=dict)
    # ``old_ref``: the number before a renumbering (Massachusetts went from sequential
    # to mile-based exit numbers in 2021; the price file may use either).
    old_exit_refs: frozenset[tuple[int, str]] = frozenset()

    @property
    def label(self) -> str:
        roads = "/".join(sorted(h.label for h in self.highways)) or "?"
        where = self.tags.get("exit_to") or self.tags.get("destination") or self.tags.get("name") or ""
        text = f"{roads} exit {self.tags.get('ref', '?')}"
        if self.tags.get("old_ref"):
            text += f" (old exit {self.tags['old_ref']})"
        return f"{text} ({where})" if where else text


class StateOSM:
    """Fuel stations and numbered exits of one state, with spatial indexes."""

    def __init__(self, fuel: list[FuelPoi], exits: list[ExitNode]):
        self.fuel = fuel
        self.exits = exits
        self.fuel_index = GridIndex(fuel)
        self.exit_index = GridIndex(exits)


def make_fuel_poi(osm_id: str, lat: float, lon: float, tags: dict) -> FuelPoi:
    cores = tuple(core for core in (name_core(tags.get(key)) for key in ("name", "brand", "operator", "official_name")) if core)
    numbers = {store_number(tags.get("name")), store_number(tags.get("branch"))}
    ref = (tags.get("ref") or "").strip()
    if ref.isdigit():
        numbers.add(store_number("#" + ref))
    return FuelPoi(osm_id, lat, lon, tags, cores, frozenset(n for n in numbers if n))


def parse_overpass(payload: dict, state: str) -> StateOSM:
    """Turn one state's Overpass answer into fuel stations and numbered exits."""
    fuel: list[FuelPoi] = []
    junctions: dict[int, dict] = {}
    ways: list[dict] = []
    for element in payload.get("elements", ()):
        tags = element.get("tags") or {}
        kind = element["type"]
        if tags.get("amenity") == "fuel":
            point = element if kind == "node" else element.get("center")
            if point and "lat" in point:
                fuel.append(make_fuel_poi(f"{kind}/{element['id']}", point["lat"], point["lon"], tags))
        elif kind == "node" and tags.get("highway") == "motorway_junction":
            junctions[element["id"]] = element
        elif kind == "way" and tags.get("highway") in ("motorway", "trunk", "primary"):
            ways.append(element)

    highways_of: dict[int, set[Highway]] = defaultdict(set)
    for way in ways:
        refs = parse_osm_highway_refs(way["tags"].get("ref"), state)
        if not refs:
            continue
        for node_id in way.get("nodes", ()):
            if node_id in junctions:
                highways_of[node_id] |= refs

    exits = []
    for node_id, node in junctions.items():
        tags = node.get("tags") or {}
        exit_refs = parse_osm_exit_refs(tags.get("ref"))
        old_exit_refs = parse_osm_exit_refs(tags.get("old_ref"))
        if (exit_refs or old_exit_refs) and highways_of.get(node_id):
            exits.append(ExitNode(
                f"node/{node_id}", node["lat"], node["lon"], exit_refs, frozenset(highways_of[node_id]), tags,
                old_exit_refs,
            ))
    return StateOSM(fuel, exits)


# --------------------------------------------------------------------------------------
# Matching
# --------------------------------------------------------------------------------------


@dataclass
class Station:
    opis_id: int
    name: str
    address: str
    city: str
    state: str
    # (lat, lon) of the city center ``load_stations`` uses; several when the city name is
    # ambiguous (homonyms, the station has no coordinates today); none when not found.
    centers: tuple[tuple[float, float], ...]


@dataclass
class Match:
    opis_id: int
    lat: float
    lon: float
    source: str  # osm_fuel | osm_exit
    confidence: str  # high | medium
    osm_type_id: str
    matched_label: str
    miles_from_city_center: float
    reason: str
    # Not written to the CSV; used to check that two OPIS stations do not get one OSM
    # fuel station: the exit numbers of the address' exit the match was made at (empty
    # without an exit), and whether OSM carries the OPIS store number at that site.
    exit_numbers: frozenset[int] = frozenset()
    store_confirmed: bool = False


@dataclass
class ExitFound:
    node: ExitNode  # the matching node closest to the city center
    nodes: list[ExitNode]  # every node with the number (both directions of the interchange)
    exact: bool  # same number AND letter, on the current ref
    spread: float  # miles between the chosen node and the furthest matching one
    exit_ref: ExitRef
    via_old_ref: bool = False  # the address uses the exit's number before a renumbering
    relaxed: bool = False  # found only by reading a state route as the US route (SR-395 = US 395)
    best_nodes: list[ExitNode] = field(default_factory=list)  # the nodes of the best match level

    @property
    def point(self) -> tuple[float, float, str, str]:
        """(lat, lon, osm ids, how) of the exit: the node closest to the city center, or,
        when the nodes of the exit are spread over more than EXIT_TIGHT_MILES (Texas
        frontage roads, one ramp per direction), their midpoint."""
        nodes = self.best_nodes or [self.node]
        far = max(miles(a.lat, a.lon, z.lat, z.lon) for a in nodes for z in nodes)
        if len(nodes) < 2 or far <= EXIT_TIGHT_MILES:
            return self.node.lat, self.node.lon, self.node.osm_id, ""
        lat = sum(n.lat for n in nodes) / len(nodes)
        lon = sum(n.lon for n in nodes) / len(nodes)
        ids = "+".join(sorted(n.osm_id for n in nodes))
        return lat, lon, ids, f"midpoint of {len(nodes)} exit nodes {far:.1f} mi apart"


def find_exit(parsed: ParsedAddress, centers, osm: StateOSM, area: float = AREA_MILES) -> tuple[ExitFound | None, str]:
    """The OSM exit the address names, within ``area`` miles of a center, or (None, why).

    When nothing matches, a state route of the address is tried as the US route with the
    same number (and the reverse), but only if no exit of that state route is in the area:
    "SR-395, EXIT 78" in Reno is US 395 exit 78."""
    if not parsed.exits:
        return None, "no exit in address"
    if not any(e.highways for e in parsed.exits):
        return None, "exit without a highway in address"
    nearby = osm.exit_index.near_any(centers, area)

    def collect(relaxed: bool) -> list[tuple[int, float, ExitNode, ExitRef, bool]]:
        # (level, miles from the center, node, address exit, matched on the node's old_ref only)
        found = []
        for exit_ref in parsed.exits:
            for distance, node in nearby:
                if not any(highways_compatible(h, nh, relaxed) for h in exit_ref.highways for nh in node.highways):
                    continue
                level = exit_match_level(exit_ref.numbers, node.exit_refs)
                old_level = exit_match_level(exit_ref.numbers, node.old_exit_refs)
                if level or old_level:
                    found.append((max(level, old_level), distance, node, exit_ref, old_level > level))
        return found

    matches, relaxed = collect(False), False
    if not matches:
        address_roads = {h for e in parsed.exits for h in e.highways if h.kind in ("SR", "US")}
        road_is_near = any(
            highways_compatible(h, nh) for _, node in nearby for nh in node.highways for h in address_roads
        )
        if address_roads and not road_is_near:
            matches, relaxed = collect(True), True
    if not matches:
        return None, f"{parsed.exits[0].label} not found in OSM within {area:g} mi"
    best_level = max(m[0] for m in matches)
    best = sorted((m for m in matches if m[0] == best_level), key=lambda m: m[1])
    chosen, exit_ref, via_old_ref = best[0][2], best[0][3], best[0][4]
    # Every node with that number (any letter, current or old ref) must be the same
    # interchange: the number twice in the area is two exits, or a renumbering
    # (Massachusetts' old exit 60 is not its new exit 60, 24 miles away). Skip.
    nodes = list({id(m[2]): m[2] for m in matches}.values())
    spread = max(miles(chosen.lat, chosen.lon, n.lat, n.lon) for n in nodes)
    if spread > EXIT_SPREAD_MILES:
        return None, f"{exit_ref.label} ambiguous: matching exits {spread:.1f} mi apart"
    best_nodes = list({id(m[2]): m[2] for m in best if m[3] == exit_ref}.values())
    return ExitFound(chosen, nodes, best_level == 2 and not via_old_ref, spread, exit_ref, via_old_ref,
                     relaxed, best_nodes), ""


def _sites(candidates: list[tuple[float, FuelPoi]]) -> list[list[tuple[float, FuelPoi]]]:
    """Group fuel features closer than SAME_SITE_MILES (one truck stop mapped as car and
    truck canopies, or as a node and an area). Groups ordered by their nearest member."""
    groups: list[list[tuple[float, FuelPoi]]] = []
    for item in sorted(candidates, key=lambda pair: pair[0]):
        joined = [g for g in groups if any(miles(item[1].lat, item[1].lon, o.lat, o.lon) <= SAME_SITE_MILES for _, o in g)]
        if not joined:
            groups.append([item])
            continue
        merged = [item] + [member for g in joined for member in g]
        groups = [g for g in groups if all(g is not j for j in joined)] + [merged]
    for g in groups:
        g.sort(key=lambda pair: pair[0])
    groups.sort(key=lambda g: g[0][0])
    return groups


def _representative(group: list[tuple[float, FuelPoi]], store: str | None) -> tuple[float, FuelPoi]:
    """The feature of a site to report: the one with the store number, then a truck
    (HGV) one, then the nearest."""
    return min(group, key=lambda pair: (not (store and store in pair[1].store_numbers), not pair[1].hgv, pair[0]))


def _brand_candidates(station_core, city_words, items) -> list[tuple[float, FuelPoi]]:
    return [(d, poi) for d, poi in items if any(names_match(station_core, core, city_words) for core in poi.cores)]


def numbers_agree(ours: str, theirs: str) -> bool:
    """Same store number, or the same with a chain prefix: Circle K "#4707622" is ref 7622."""
    if ours == theirs:
        return True
    longer, shorter = (ours, theirs) if len(ours) >= len(theirs) else (theirs, ours)
    return len(shorter) >= 2 and longer.endswith(shorter)


def _site_numbers(site: list[tuple[float, FuelPoi]]) -> frozenset[str]:
    return frozenset(number for _, poi in site for number in poi.store_numbers)


def _other_store(site: list[tuple[float, FuelPoi]], store: str | None) -> bool:
    """OSM gives the site store number(s) and none is ours: another store of the chain."""
    numbers = _site_numbers(site)
    return bool(store and numbers) and not any(numbers_agree(store, n) for n in numbers)


def match_station(station: Station, osm: StateOSM, exclude=()) -> tuple[Match | None, str]:
    """Apply the rules (see the module docstring) to one station: (match, "") or (None, why).

    ``exclude``: (lat, lon) of OSM fuel sites the station may not take, because an OPIS
    station of another exit holds them (see ``match_state``)."""
    if not station.centers:
        return None, "city not found"
    parsed = parse_address(station.address, station.state)
    core = name_core(station.name)
    city_words = frozenset(normalize_text(station.city).split())
    store = store_number(station.name)
    ambiguous_city = len(station.centers) > 1
    ambiguous_note = "; city name ambiguous, exit unique in its area" if ambiguous_city else ""

    def from_center(lat: float, lon: float) -> float:
        return min(miles(lat, lon, c_lat, c_lon) for c_lat, c_lon in station.centers)

    def allowed(poi: FuelPoi) -> bool:
        return not any(miles(poi.lat, poi.lon, lat, lon) <= SAME_SITE_MILES for lat, lon in exclude)

    found, exit_note = find_exit(parsed, station.centers, osm)
    renumbered = station.state in RENUMBERED_STATES and found is not None and not found.via_old_ref
    if renumbered and from_center(found.node.lat, found.node.lon) > RENUMBERED_MAX_MILES:
        found, exit_note = None, (
            f"{found.exit_ref.label} found {from_center(found.node.lat, found.node.lon):.0f} mi away, "
            f"but {station.state} renumbered its exits"
        )
    if found:
        exit_label = found.exit_ref.label
        exit_numbers = frozenset(number for number, _ in found.exit_ref.numbers)
        near: dict[int, tuple[float, FuelPoi]] = {}
        for node in found.nodes:
            for d, poi in osm.fuel_index.near(node.lat, node.lon, FUEL_NEAR_EXIT_MILES):
                if id(poi) not in near or d < near[id(poi)][0]:
                    near[id(poi)] = (d, poi)
        named = _brand_candidates(core, city_words, near.values())
        sites = _sites([(d, poi) for d, poi in named if allowed(poi)])
        # A site that carries our store number wins; one that carries only other numbers
        # is another store of the chain (Speedway #3503 is not "Speedway, ref 3495").
        with_store = [site for site in sites if store and store in _site_numbers(site)]
        other_store = [site for site in sites if _other_store(site, store)]
        usable = with_store or [site for site in sites if all(site is not o for o in other_store)]
        claimed = len(sites) < len(_sites(named))  # a site another OPIS station holds
        if usable:
            d, poi = _representative(usable[0], store)
            confident = len(usable) == 1 and not ambiguous_city and (bool(with_store) or not (claimed or other_store))
            why = (
                f"name match {d:.2f} mi from {exit_label} ({found.node.osm_id})"
                + ("; OSM has the store number" if with_store else "")
                + ("" if len(usable) == 1 else f"; nearest of {len(usable)} matching sites")
                + ("; a site of another store set aside" if other_store and not with_store else "")
                + ("; a site held by another OPIS station set aside" if claimed and not with_store else "")
                + ambiguous_note
            )
            return Match(station.opis_id, poi.lat, poi.lon, "osm_fuel", "high" if confident else "medium",
                         poi.osm_id, poi.label, from_center(poi.lat, poi.lon), why,
                         exit_numbers, bool(with_store)), ""

        lat, lon, osm_ids, how = found.point
        moved = from_center(lat, lon)
        # The chain's only store in town (not another store number), none at the exit: the
        # address probably gives the nearest exit of that store. A chain with several
        # stores in town (QuikTrip in St. Louis) says nothing about this one.
        town_sites = _sites(_brand_candidates(core, city_words, osm.fuel_index.near_any(station.centers, IN_TOWN_MILES)))
        in_town = (
            not named and moved > FUEL_UNIQUE_MILES
            and len(town_sites) == 1 and not _other_store(town_sites[0], store)
        )
        if moved > EXIT_ONLY_MAX_MILES:
            exit_note = f"{exit_label} {moved:.0f} mi from the city and no fuel station with the name next to it"
        elif in_town:
            exit_note = f"{exit_label} {moved:.0f} mi from the city, the name is in town and not at the exit"
        else:
            confident = (
                found.exact and found.spread <= EXIT_TIGHT_MILES and not ambiguous_city and not renumbered
                and not found.relaxed and not named and moved <= EXIT_ONLY_HIGH_MILES
            )
            why = (
                f"{exit_label}: {len(found.nodes)} node(s) within {found.spread:.1f} mi"
                + (f", {how}" if how else "")
                + (", matched on the old exit number (old_ref)" if found.via_old_ref
                   else "" if found.exact else ", exit letter differs")
                + (", state route read as the US route with that number" if found.relaxed else "")
                + (f"; {station.state} renumbered its exits" if renumbered else "")
                + (f"; no fuel station with the name within {FUEL_NEAR_EXIT_MILES:g} mi" if not named
                   else "; the fuel station with the name there is another store" if other_store
                   else "; the fuel station with the name there is held by another OPIS station")
                + (f"; {moved:.0f} mi from the city" if moved > EXIT_ONLY_HIGH_MILES else "")
                + ambiguous_note
            )
            return Match(station.opis_id, lat, lon, "osm_exit", "high" if confident else "medium",
                         osm_ids, found.node.label, moved, why, exit_numbers), ""

    if ambiguous_city:
        return None, f"city ambiguous and {exit_note}"
    if not store:
        # A name alone, away from an exit, is not enough: the price file lists several
        # stores of a chain in one town, and they would all land on the one OSM knows.
        return None, f"{exit_note}; no store number to confirm a name match"
    lat, lon = station.centers[0]
    same_store = [
        (d, poi) for d, poi in _brand_candidates(core, city_words, osm.fuel_index.near(lat, lon, AREA_MILES))
        if store in poi.store_numbers and allowed(poi)
    ]
    sites = _sites(same_store)
    if len(sites) == 1:
        d, poi = _representative(sites[0], store)
        return Match(station.opis_id, poi.lat, poi.lon, "osm_fuel", "high", poi.osm_id, poi.label, d,
                     f"store number and name match, {d:.1f} mi from city center ({exit_note})",
                     store_confirmed=True), ""
    if sites:
        return None, f"{exit_note}; the store number is on {len(sites)} OSM sites"
    return None, f"{exit_note}; no OSM site with the store number and the name"


def _exit_groups(claims: list[Match]) -> list[list[Match]]:
    """Claimants of one OSM fuel station grouped by exit: two OPIS stations matched at the
    same exit (number) can be one truck stop listed twice; without an exit, alone."""
    groups: list[list[Match]] = []
    for match in sorted(claims, key=lambda m: m.opis_id):
        joined = [g for g in groups if match.exit_numbers and any(match.exit_numbers & m.exit_numbers for m in g)]
        merged = [match] + [m for g in joined for m in g]
        groups = [g for g in groups if all(g is not j for j in joined)] + [merged]
    return groups


def shared_feature_losers(matches: list[Match]) -> dict[int, tuple[float, float]]:
    """OPIS stations that must give up their OSM fuel station: {opis_id: its position}.

    One OSM fuel station is one store. When OPIS stations of different exits (or without
    an exit) claim it, at most one of them is right: the group whose store number OSM
    carries keeps it; without exactly one such group, every group gives it up."""
    claims: dict[str, list[Match]] = defaultdict(list)
    for match in matches:
        if match.source == "osm_fuel":
            claims[match.osm_type_id].append(match)
    losers: dict[int, tuple[float, float]] = {}
    for feature in sorted(claims):
        groups = _exit_groups(claims[feature])
        if len(groups) < 2:
            continue
        confirmed = [g for g in groups if any(m.store_confirmed for m in g)]
        keep = confirmed[0] if len(confirmed) == 1 else None
        for group in groups:
            if group is not keep:
                losers.update((m.opis_id, (m.lat, m.lon)) for m in group)
    return losers


def match_state(stations: list[Station], osm: StateOSM, rounds: int = 5) -> tuple[list[Match], dict[int, str]]:
    """Match every station of one state: (matches, {opis_id: why} for the others).

    The stations that lost a shared OSM fuel station (``shared_feature_losers``) are then
    matched again without it (usually landing on their own exit node); whatever is still
    shared after ``rounds`` passes keeps its city center."""
    by_id = {s.opis_id: s for s in stations}
    results = {s.opis_id: match_station(s, osm) for s in stations}
    excluded: dict[int, list[tuple[float, float]]] = defaultdict(list)
    for _ in range(rounds):
        losers = shared_feature_losers([m for m, _ in results.values() if m])
        if not losers:
            break
        for opis_id, point in sorted(losers.items()):
            excluded[opis_id].append(point)
            results[opis_id] = match_station(by_id[opis_id], osm, tuple(excluded[opis_id]))
    for opis_id in shared_feature_losers([m for m, _ in results.values() if m]):
        results[opis_id] = (None, "its OSM fuel station is claimed by another OPIS station")
    matches = [m for m, _ in results.values() if m]
    return matches, {opis_id: why for opis_id, (m, why) in results.items() if m is None}


# --------------------------------------------------------------------------------------
# Overpass download (polite, cached)
# --------------------------------------------------------------------------------------


def overpass_queries(state: str) -> tuple[str, str]:
    """The two queries of a state: its fuel stations; its exits and the highway ways they
    are on. Two medium requests get through busy servers where one big one gets 504."""
    head = f"""[out:json][timeout:{QUERY_TIMEOUT_SECONDS}];
area["ISO3166-2"="US-{state}"]["admin_level"="4"]->.state;
"""
    fuel = head + """nwr["amenity"="fuel"](area.state);
out center tags;
"""
    exits = head + """node["highway"="motorway_junction"](area.state)->.exits;
.exits out body;
way(bn.exits)["highway"~"^(motorway|trunk|primary)$"];
out body;
"""
    return fuel, exits


class OverpassError(Exception):
    pass


def _ssl_context() -> ssl.SSLContext:
    try:
        import certifi  # in requirements.txt (urllib3's CA bundle)

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:  # pragma: no cover
        return ssl.create_default_context()


def _post(endpoint: str, query: str) -> dict:
    body = urllib.parse.urlencode({"data": query}).encode()
    request = urllib.request.Request(
        endpoint, data=body,
        headers={"User-Agent": USER_AGENT, "Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS, context=_ssl_context()) as response:
        payload = json.loads(response.read().decode("utf-8"))
    remark = payload.get("remark") or ""
    if "error" in remark.lower() or "timed out" in remark.lower():
        raise OverpassError(f"server remark: {remark.strip()[:200]}")
    if "elements" not in payload:
        raise OverpassError("answer without elements")
    check_fresh(payload)
    return payload


def check_fresh(payload: dict, now: datetime | None = None) -> None:
    """Refuse an answer from an instance whose OSM copy is stale (or does not say its age)."""
    stamp = (payload.get("osm3s") or {}).get("timestamp_osm_base", "")
    try:
        built = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        raise OverpassError(f"answer without a valid timestamp_osm_base: {stamp!r}") from None
    age = (now or datetime.now(timezone.utc)) - built
    if age.days > MAX_DATA_AGE_DAYS:
        raise OverpassError(f"stale instance: OSM data from {stamp[:10]}")


def download_state(state: str, log=print) -> dict:
    """Both queries of the state, one after the other, merged into one Overpass-like answer."""
    parts = []
    for label, query in zip(("fuel", "exits"), overpass_queries(state)):
        if parts:
            time.sleep(ENDPOINT_SWITCH_PAUSE_SECONDS)
        parts.append(_download(f"{state} {label}", query, log))
    return {"osm3s": parts[0].get("osm3s", {}), "elements": [e for part in parts for e in part["elements"]]}


def _download(state: str, query: str, log) -> dict:
    """POST one query. On 429 / 5xx / timeouts try the next endpoint (after a short pause);
    when every endpoint failed, back off (RETRY_DELAYS_SECONDS) and start over."""
    errors = []
    for round_number in range(len(RETRY_DELAYS_SECONDS) + 1):
        for endpoint in ENDPOINTS:
            started = time.monotonic()
            try:
                payload = _post(endpoint, query)
                log(f"  {state}: {len(payload['elements']):,} elements in {time.monotonic() - started:.0f}s from {endpoint}")
                return payload
            except urllib.error.HTTPError as exc:
                if exc.code not in (429, 500, 502, 503, 504):
                    raise
                errors.append(f"HTTP {exc.code}")
            except (urllib.error.URLError, TimeoutError, OverpassError, json.JSONDecodeError, ConnectionError) as exc:
                errors.append(f"{type(exc).__name__}: {exc}"[:200])
            log(f"  {state}: {errors[-1]} from {endpoint} after {time.monotonic() - started:.0f}s")
            time.sleep(ENDPOINT_SWITCH_PAUSE_SECONDS)
        if round_number < len(RETRY_DELAYS_SECONDS):
            delay = RETRY_DELAYS_SECONDS[round_number]
            log(f"  {state}: every endpoint failed; retrying in {delay}s")
            time.sleep(delay)
    raise OverpassError(f"{state}: giving up after {len(errors)} attempts: {errors[-3:]}")


def load_state(state: str, *, offline: bool, refresh: bool, log=print) -> tuple[dict | None, bool]:
    """(payload, downloaded) from the cache or Overpass; (None, False) when offline and not cached."""
    path = CACHE_DIR / f"{state}.json"
    if path.exists() and not refresh:
        return json.loads(path.read_text(encoding="utf-8")), False
    if offline:
        return None, False
    payload = download_state(state, log)
    if not any((e.get("tags") or {}).get("amenity") == "fuel" for e in payload["elements"]):
        raise OverpassError(f"{state}: no fuel station in the answer (wrong area?)")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(path)
    return payload, True


# --------------------------------------------------------------------------------------
# Stations of the price file, with the city centers load_stations uses
# --------------------------------------------------------------------------------------


def read_stations(price_file: Path) -> list[Station]:
    """US stations of the price file with their city center(s), exactly as ``load_stations``
    computes them (same parser, same places index, same homonym resolution)."""
    sys.path.insert(0, str(ROOT))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    import django

    django.setup()
    from fuelroute.services.places import get_place_index
    from fuelroute.services.station_loader import LoadReport, geocode_rows, parse_price_file
    from fuelroute.services.text import US_STATES

    report = LoadReport()
    rows = [row for row in parse_price_file(price_file, report).values() if row.state in US_STATES]
    places = get_place_index()
    located = geocode_rows(rows, places, report)
    stations = []
    for row in rows:
        place = located.get(row.opis_id)
        if place:
            centers = ((place.latitude, place.longitude),)
        else:
            centers = tuple((p.latitude, p.longitude) for p in places.candidates(row.city, row.state))
        stations.append(Station(row.opis_id, row.name, row.address, row.city, row.state, centers))
    return stations


# --------------------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------------------


def write_csv(matches: list[Match], path: Path) -> None:
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(CSV_COLUMNS)
        for m in sorted(matches, key=lambda m: m.opis_id):
            writer.writerow([
                m.opis_id, f"{m.lat:.6f}", f"{m.lon:.6f}", m.source, m.confidence, m.osm_type_id,
                _clean_label(m.matched_label), f"{m.miles_from_city_center:.2f}", m.reason,
            ])


def _clean_label(label: str) -> str:
    """One line, no zero-width characters (OSM names sometimes carry U+200B)."""
    return " ".join(re.sub("[\u200b-\u200f\ufeff]", "", label).split())


def _percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    k = (len(ordered) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)


def write_report(path: Path, *, stations, matches, unmatched_reasons, states, failed, osm_dates, by_id) -> None:
    total = len(stations)
    with_center = sum(1 for s in stations if len(s.centers) == 1)
    ambiguous = sum(1 for s in stations if len(s.centers) > 1)
    moved = [m.miles_from_city_center for m in matches if by_id[m.opis_id].centers and len(by_id[m.opis_id].centers) == 1]
    by_source = Counter((m.source, m.confidence) for m in matches)
    placed_ambiguous = sum(1 for m in matches if len(by_id[m.opis_id].centers) > 1)
    shared_fuel = sum(1 for count in Counter(m.osm_type_id for m in matches if m.source == "osm_fuel").values() if count > 1)
    lines = [
        "# Station coordinates from OpenStreetMap",
        "",
        "Built by `scripts/build_station_coords.py` (offline, once) into `data/station_coords.csv`:",
        "a station with a row gets those coordinates, one without keeps its city center.",
        "",
        f"- Built: {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC. OSM data as of (states): "
        + (", ".join(f"{day} ({count})" for day, count in sorted(osm_dates.items())) if osm_dates else "n/a")
        + " (the date of the Overpass instance that answered)",
        f"- US stations in the price file: **{total:,}** in {len(states)} states "
        f"({with_center:,} with a city center, {ambiguous} with an ambiguous city name)",
        f"- States that could not be downloaded: {', '.join(failed) if failed else 'none'}",
        "",
        "## Result",
        "",
        "| source | confidence | stations |",
        "|---|---|---:|",
    ]
    for (source, confidence), count in sorted(by_source.items()):
        lines.append(f"| {source} | {confidence} | {count:,} |")
    lines += [
        f"| **placed** | | **{len(matches):,}** ({100 * len(matches) / max(total, 1):.1f}%) |",
        f"| kept at city center (no row) | | {with_center - (len(matches) - placed_ambiguous):,} |",
        f"| ambiguous city, still without coordinates | | {ambiguous - placed_ambiguous} |",
        "",
        f"Ambiguous-city stations placed by a unique exit: {placed_ambiguous} of {ambiguous}.",
        "",
        f"OSM fuel stations matched by more than one OPIS ID: {shared_fuel}, each time OPIS IDs of the "
        "same exit (one truck stop listed twice in the price file).",
        "",
        "## How far stations moved from their city center",
        "",
        f"Stations that had a city center: {len(moved):,}. "
        f"Median **{_percentile(moved, 0.5):.1f} mi**, p75 {_percentile(moved, 0.75):.1f}, "
        f"p90 {_percentile(moved, 0.9):.1f}, p99 {_percentile(moved, 0.99):.1f}, max {max(moved, default=0):.1f}.",
        "",
        "| miles moved | stations |",
        "|---|---:|",
    ]
    buckets = ((0, 1), (1, 2), (2, 5), (5, 10), (10, 15), (15, 20), (20, 30))
    for lo, hi in buckets:
        lines.append(f"| {lo}-{hi} | {sum(1 for v in moved if lo <= v < hi):,} |")
    lines += ["", "## The 20 largest moves", "", "| opis_id | station | city | source | conf. | miles | matched |", "|---|---|---|---|---|---:|---|"]
    for m in sorted(matches, key=lambda m: -m.miles_from_city_center)[:20]:
        s = by_id[m.opis_id]
        label = m.matched_label.replace("|", "/")
        lines.append(f"| {m.opis_id} | {s.name} | {s.city}, {s.state} | {m.source} | {m.confidence} | {m.miles_from_city_center:.1f} | {label} |")
    lines += ["", "## Why the others kept their city center", "", "| reason | stations |", "|---|---:|"]
    for reason, count in Counter(_reason_family(r) for r in unmatched_reasons.values()).most_common():
        lines.append(f"| {reason} | {count:,} |")
    lines += [
        "",
        "## Rules",
        "",
        f"- Search area: {AREA_MILES:g} mi around the city center `load_stations` computes.",
        "- `osm_fuel`: OSM `amenity=fuel` whose name / brand / operator matches the OPIS name (brands",
        f"  normalised, store numbers removed), within {FUEL_NEAR_EXIT_MILES:g} mi of the address' exit when that exit",
        "  is found. A site with the OPIS store number in OSM wins; a site with only other store numbers",
        "  is another store and is not taken (high; medium if several sites match or one was set aside).",
        "  Without a usable exit, only the same store number in OSM places a station (high): a name",
        "  alone is not used, because the price file lists several stores of a chain in one town.",
        "- `osm_exit`: the `motorway_junction` with the address' exit number on a way with the address'",
        "  highway (closest to the city center, or the midpoint of the nodes when they are more than",
        f"  {EXIT_TIGHT_MILES:g} mi apart; skipped if the nodes with that number, current `ref` or `old_ref` from",
        f"  before a renumbering, are > {EXIT_SPREAD_MILES:g} mi apart). High when number and letter match the current",
        f"  ref, all its nodes are within {EXIT_TIGHT_MILES:g} mi and it is within {EXIT_ONLY_HIGH_MILES:g} mi of the city;",
        f"  medium otherwise. Not used beyond {EXIT_ONLY_MAX_MILES:g} mi, nor beyond {FUEL_UNIQUE_MILES:g} mi when the chain's",
        f"  only store within {IN_TOWN_MILES:g} mi of the city center is in OSM and none is at the exit.",
        "  A state route is read as the US route with the same number (\"SR-395\" = US 395) only when no",
        "  state route with that number has an exit in the area (medium).",
        f"- {' and '.join(sorted(RENUMBERED_STATES))} renumbered every exit in 2021 and OSM rarely keeps the old",
        f"  number: there an exit matched on its current number must be within {RENUMBERED_MAX_MILES:g} mi of the city.",
        "- One OSM fuel station is one store: OPIS stations of different exits never share one. The one",
        "  whose store number OSM carries keeps it; the others are matched again without it.",
        "- Fuel features closer than 0.35 mi count as one site (car and truck canopies).",
        "",
        "## Re-run",
        "",
        "```",
        "python scripts/build_station_coords.py            # downloads missing states into .cache/osm/",
        "python scripts/build_station_coords.py --offline  # re-match from the cache only",
        "python scripts/build_station_coords.py --refresh  # download every state again",
        "```",
        "",
        "Two Overpass requests per state (fuel; exits), sequential, with a pause between states and",
        "back-off on 429/5xx. Then `python manage.py load_stations` to load the result.",
        "",
        "## Columns of `data/station_coords.csv`",
        "",
        "`opis_id` (the price file's station), `lat` / `lon` (the position), `source` (`osm_fuel`:",
        "its OSM fuel station; `osm_exit`: the exit of its address), `confidence` (`high` / `medium`),",
        "`osm_type_id` (the OSM feature, or the exit nodes joined by `+` for a midpoint),",
        "`matched_label` (that feature's OSM name or exit), `miles_from_city_center` and `reason` (the",
        "rule that placed it). `load_stations` reads `opis_id`, `lat`, `lon` and `source`.",
        "",
        "## Licence",
        "",
        "Derived from OpenStreetMap data, © OpenStreetMap contributors, available under the",
        "Open Database License (ODbL 1.0, https://opendatacommons.org/licenses/odbl/).",
        "`data/station_coords.csv` is made available under the same licence.",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def _reason_family(reason: str) -> str:
    """Group reasons for the report: numbers and labels removed."""
    text = re.sub(r"\b[A-Z]{1,3}-\d+[A-Z]?(/[A-Z]{1,3}-\d+[A-Z]?)*\b", "<road>", reason)
    text = re.sub(r"exit [\d/A-Z]+", "exit <n>", text)
    text = re.sub(r"\d+(\.\d+)?", "<n>", text)
    return text


# --------------------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------------------


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--states", help="Comma-separated state codes (default: every US state of the price file).")
    parser.add_argument("--offline", action="store_true", help="Never download; use .cache/osm/ only.")
    parser.add_argument("--refresh", action="store_true", help="Download every state again.")
    parser.add_argument("--download-only", action="store_true", help="Fill the cache; match nothing, write nothing.")
    parser.add_argument("--pause", type=float, default=DEFAULT_PAUSE_SECONDS, help="Seconds between two downloads.")
    parser.add_argument("--prices", default=str(PRICE_FILE))
    parser.add_argument("--output", default=str(OUTPUT))
    parser.add_argument("--report", default=str(REPORT))
    args = parser.parse_args(argv)

    stations = read_stations(Path(args.prices))
    states = sorted({s.state for s in stations})
    if args.states:
        wanted = {s.strip().upper() for s in args.states.split(",") if s.strip()}
        states = [s for s in states if s in wanted]
        stations = [s for s in stations if s.state in wanted]
    print(f"{len(stations):,} US stations in {len(states)} states")

    by_state: dict[str, list[Station]] = defaultdict(list)
    for station in stations:
        by_state[station.state].append(station)

    matches: list[Match] = []
    unmatched: dict[int, str] = {}
    failed: list[str] = []
    osm_dates: Counter = Counter()
    downloaded_before = False
    for state in states:
        try:
            will_download = not args.offline and (args.refresh or not (CACHE_DIR / f"{state}.json").exists())
            if will_download and downloaded_before:
                time.sleep(args.pause)  # be polite to the public Overpass servers
            payload, downloaded = load_state(state, offline=args.offline, refresh=args.refresh)
            downloaded_before = downloaded_before or downloaded
        except Exception as exc:  # noqa: BLE001 - one state failing must not stop the others
            print(f"  {state}: FAILED ({exc})")
            payload = None
        if payload is None:
            failed.append(state)
            for station in by_state[state]:
                unmatched[station.opis_id] = "state not downloaded"
            continue
        if args.download_only:
            continue
        stamp = (payload.get("osm3s") or {}).get("timestamp_osm_base", "")
        if stamp:
            osm_dates[stamp[:10]] += 1
        osm = parse_overpass(payload, state)
        state_matches, state_unmatched = match_state(by_state[state], osm)
        matches.extend(state_matches)
        unmatched.update(state_unmatched)
        print(f"  {state}: {len(osm.fuel):,} fuel, {len(osm.exits):,} numbered exits; "
              f"placed {len(state_matches)}/{len(by_state[state])}")

    if args.download_only:
        print(f"download only; failed states: {failed or 'none'}")
        return 1 if failed else 0
    write_csv(matches, Path(args.output))
    by_id = {s.opis_id: s for s in stations}
    write_report(Path(args.report), stations=stations, matches=matches, unmatched_reasons=unmatched,
                 states=states, failed=failed, osm_dates=osm_dates, by_id=by_id)
    counts = Counter(m.source for m in matches)
    print(f"placed {len(matches):,}/{len(stations):,}: {dict(counts)}; failed states: {failed or 'none'}")
    print(f"wrote {args.output} and {args.report}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
