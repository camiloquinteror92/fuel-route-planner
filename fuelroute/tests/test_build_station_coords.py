"""The one-time OpenStreetMap matching of ``scripts/build_station_coords.py``: address
parsing, OSM tag parsing, name matching and the rules that place a station.

Synthetic data only: nothing here downloads anything (the script's download code is
never called; ``load_state`` is only tested offline against an empty cache). The script
uses urllib, not ``fuelroute.services.http``, so every test here also gets ``no_network``.
The last test checks the committed ``data/station_coords.csv``."""

import csv
import importlib.util
import re
import socket
import sys
import urllib.request
from collections import defaultdict
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[2] / "scripts" / "build_station_coords.py"
_SPEC = importlib.util.spec_from_file_location("build_station_coords", _PATH)
b = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = b  # dataclasses look their module up while the class is created
_SPEC.loader.exec_module(b)

I44, US69 = b.Highway("I", 44), b.Highway("US", 69)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Any connection attempt fails the test, whatever library makes it."""

    def refuse(*args, **kwargs):
        raise AssertionError("the station coordinates builder tried to reach the network")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setattr(urllib.request, "urlopen", refuse)


# --- address parsing -------------------------------------------------------------------


def test_parse_interstate_exit_and_other_highways():
    parsed = b.parse_address("I-44, EXIT 283 & US-69", "OK")
    assert parsed.highways == (I44, US69)
    assert parsed.exits == (b.ExitRef((I44,), ((283, ""),)),)


@pytest.mark.parametrize(
    "address, numbers",
    [
        ("I-75, EXIT 144-B", ((144, "B"),)),
        ("I-94, EXIT 15 B", ((15, "B"),)),
        ("I-10, EXIT 140B", ((140, "B"),)),
        ("I-12, EXIT 10 AT MILE 10", ((10, ""),)),  # "AT" is not an exit letter
        ("I-81, EXIT 36 NORTH", ((36, ""),)),
        ("I-70, EXIT 88/89", ((88, ""), (89, ""))),
        ("I-35,  EXITN 5", ()),  # garbage: no exit
        ("I-40 EXIT12", ((12, ""),)),
        ("I-80 EX 360", ((360, ""),)),  # abbreviations of EXIT
        ("I-35 EXT 138C", ((138, "C"),)),
        ("I-10 EX. 37", ((37, ""),)),
        ("I-10 EXPRESS LANES", ()),  # a word that starts with EX is not an exit
    ],
)
def test_parse_exit_number_formats(address, numbers):
    exits = b.parse_address(address, "TX").exits
    assert (exits[0].numbers if exits else ()) == numbers


def test_an_exit_belongs_to_the_highways_written_before_it():
    parsed = b.parse_address("I-85, EXIT 39 I-77, EXIT 13", "SC")
    assert [(e.highways, e.numbers) for e in parsed.exits] == [
        ((b.Highway("I", 85),), ((39, ""),)),
        ((b.Highway("I", 77),), ((13, ""),)),
    ]
    concurrent = b.parse_address("I-29 & I-80, EXIT 1B", "IA").exits[0]
    assert concurrent.highways == (b.Highway("I", 29), b.Highway("I", 80))
    glued = b.parse_address("I-380, EXIT 68US-20, EXIT 68", "IA").exits
    assert [e.highways for e in glued] == [(b.Highway("I", 380),), (b.Highway("US", 20),)]
    directions = b.parse_address("I-81N, EXIT 2W & I-81S, EXIT 3", "VA").exits
    assert [(e.highways[0].label, e.numbers) for e in directions] == [("I-81N", ((2, "W"),)), ("I-81S", ((3, ""),))]


@pytest.mark.parametrize(
    "address, state, labels",
    [
        ("US HWY 287 & SR-114", "TX", ["US-287", "SR-114"]),
        ("IH-35 & FM-1960", "TX", ["I-35", "FM-1960"]),
        ("HWY 30 E", "IA", ["HWY-30"]),
        ("WI-29 EXIT 132", "WI", ["SR-29"]),
        ("M-57", "MI", ["SR-57"]),
        ("TX-21", "OK", []),  # another state's route prefix: not a highway of this state
        ("NJTP, MM 5 SB", "NJ", []),  # mile marker, turnpike: no highway we can match
        ("SR- 60 & FL TURNPIKE, EXIT 193", "FL", ["SR-60"]),
        ("US-BUS 53", "WI", []),
    ],
)
def test_parse_highway_kinds(address, state, labels):
    assert [h.label for h in b.parse_address(address, state).highways] == labels


def test_an_exit_without_any_highway_has_no_road():
    parsed = b.parse_address("U-55, EXIT 220", "MO")
    assert parsed.exits[0].highways == ()


# --- OSM tags --------------------------------------------------------------------------


def test_osm_way_refs():
    assert b.parse_osm_highway_refs("I 44;US 69", "OK") == {I44, US69}
    assert b.parse_osm_highway_refs("OK 66", "OK") == {b.Highway("SR", 66)}
    assert b.parse_osm_highway_refs("DE 1 Toll", "DE") == {b.Highway("SR", 1)}
    assert b.parse_osm_highway_refs("US 69 Business", "OK") == set()
    assert b.parse_osm_highway_refs("I 35E", "TX") == {b.Highway("I", 35, "E")}
    assert b.parse_osm_highway_refs("M 14", "MI") == {b.Highway("SR", 14)}
    assert b.parse_osm_highway_refs("TX 21", "OK") == set()
    assert b.parse_osm_highway_refs(None, "OK") == set()
    # "CO" is Colorado's state highway prefix, and a county road elsewhere
    assert b.parse_osm_highway_refs("CO 470", "CO") == {b.Highway("SR", 470)}
    assert b.parse_osm_highway_refs("CO 12", "TX") == {b.Highway("CR", 12)}
    assert b.parse_osm_highway_refs("CR 12", "CO") == {b.Highway("CR", 12)}


def test_osm_exit_refs():
    assert b.parse_osm_exit_refs("283A") == {(283, "A")}
    assert b.parse_osm_exit_refs("144A-B") == {(144, "A"), (144, "B")}
    assert b.parse_osm_exit_refs("12;13") == {(12, ""), (13, "")}
    assert b.parse_osm_exit_refs("15 B") == {(15, "B")}
    assert b.parse_osm_exit_refs("") == set()
    assert b.parse_osm_exit_refs("East") == set()


def test_highway_compatibility():
    assert b.highways_compatible(b.Highway("I", 81, "N"), b.Highway("I", 81))  # direction letter
    assert b.highways_compatible(b.Highway("I", 35, "E"), b.Highway("I", 35, "E"))
    assert not b.highways_compatible(b.Highway("I", 35, "E"), b.Highway("I", 35, "W"))
    assert b.highways_compatible(b.Highway("HWY", 30), b.Highway("US", 30))
    assert b.highways_compatible(b.Highway("HWY", 30), b.Highway("SR", 30))
    assert not b.highways_compatible(b.Highway("HWY", 30), b.Highway("I", 30))
    assert not b.highways_compatible(I44, b.Highway("US", 44))
    # relaxed: a state route and a US route with the same number ("SR-395" for US 395)
    assert not b.highways_compatible(b.Highway("SR", 395), b.Highway("US", 395))
    assert b.highways_compatible(b.Highway("SR", 395), b.Highway("US", 395), relaxed=True)
    assert b.highways_compatible(b.Highway("US", 46), b.Highway("SR", 46), relaxed=True)
    assert not b.highways_compatible(b.Highway("I", 44), b.Highway("US", 44), relaxed=True)


def test_exit_match_level():
    assert b.exit_match_level(((283, ""),), {(283, "")}) == 2
    assert b.exit_match_level(((15, "B"),), {(15, "A")}) == 1
    assert b.exit_match_level(((15, ""),), {(15, "A"), (15, "B")}) == 1
    assert b.exit_match_level(((15, "B"),), {(16, "B")}) == 0


# --- names -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "opis, osm, city, expected",
    [
        ("PILOT TRAVEL CENTER #1243", "Pilot Travel Center", "Gila Bend", True),
        ("PILOT #1243", "Pilot", "Gila Bend", True),
        ("TA SEYMOUR TRAVEL CENTER", "TravelCenters of America", "Seymour", True),
        ("TA EXPRESS", "TA", "Seymour", True),
        ("LOVES TRAVEL STOP #512", "Love’s Travel Stop", "Tulsa", True),
        ("KWIK STAR #1000", "Kwik Trip", "Waterloo", True),
        ("KWIK FILL #12", "Kwik Trip", "Erie", False),
        ("PETRO STOPPING CENTER #348", "Petro Stopping Centers", "Shorter", True),
        ("PETRO-CARD 24", "Petro", "Fresno", False),
        ("FLYING J TRAVEL PLAZA", "Pilot", "Lodi", False),
        ("FLYING J TRAVEL PLAZA", "Pilot Flying J", "Lodi", True),
        ("7-ELEVEN #41263", "7-Eleven", "Laurel", True),
        ("CASEYS GENERAL STORE #12", "Casey's", "Ames", True),
        ("WOODSHED OF BIG CABIN", "Woodshed", "Big Cabin", True),
        ("WOODSHED OF BIG CABIN", "Big Cabin Shell", "Big Cabin", False),  # only the city in common
        ("QUICK FUEL #12", "Quick Fuel", "Dallas", False),  # nothing distinctive
        ("MOULTON COWBOYS", "Cowboys", "Moulton", True),  # the city's name is ignored
        ("PAC PRIDE", "Pride Gas Station", "Glendale", False),  # Pacific Pride is a cardlock
        ("ONE9 XPRESS FUEL #1264", "Xpress Fuel", "Eloy", False),  # the end of a name, no brand
        ("BEAR SHELL", "Shell", "Newark", True),  # the OSM brand inside the OPIS name
        ("TA BALDWIN TRAVEL CENTER", "TA", "Jacksonville", True),
        # A brand alone matches the brand alone (or with other brands), not a longer name
        ("SHELL", "Bear Shell", "Newark", False),  # OSM's brand tag carries "Shell" alone
        ("76", "Route 76 Fuel", "Tulsa", False),
        ("PILOT #12", "Pilot Knob Mart", "Tulsa", False),
        ("SHELL", "Shell Lake Cenex", "Hayward", False),
        ("QUARLES #4423", "Quarles Fuel Network", "Glen Burnie", True),
        # A single word that is not a brand must be long to count
        ("BEST", "Best Western", "Tulsa", False),
        ("SWIFT", "Swift Transportation", "Tulsa", False),
        ("SWIFT", "Swift", "Tulsa", True),  # equal names
        ("WOODSHED", "Woodshed Bar", "Tulsa", True),
        ("BIG D", "Big D", "Austin", True),  # equal short names
        ("BP", "BP", "Toledo", True),
        ("SHELL", "Exxon", "Toledo", False),
    ],
)
def test_names_match(opis, osm, city, expected):
    city_words = frozenset(b.normalize_text(city).split())
    assert b.names_match(b.name_core(opis), b.name_core(osm), city_words) is expected


def test_name_core_and_store_number():
    assert b.name_core("PILOT TRAVEL CENTER #1243") == ("pilot",)
    assert b.name_core("TravelCenters of America") == ("ta",)
    assert b.name_core("WOODSHED OF BIG CABIN") == ("woodshed", "big", "cabin")
    assert b.store_number("PILOT #01243") == "1243"
    assert b.store_number("SHELL") is None


# --- matching rules --------------------------------------------------------------------

CENTER = (36.54, -95.22)


def north(point, miles_):
    """A point ``miles_`` north (or south, negative) of ``point``."""
    return (point[0] + miles_ / 69.05, point[1])


def east(point, miles_):
    import math

    return (point[0], point[1] + miles_ / (69.17 * math.cos(math.radians(point[0]))))


class OSMBuilder:
    """Builds an Overpass-like answer, parsed by the real ``parse_overpass``."""

    def __init__(self):
        self.elements = []
        self._next = 1

    def _id(self):
        self._next += 1
        return self._next

    def fuel(self, point, as_way=False, **tags):
        element_id = self._id()
        tags = {"amenity": "fuel", **tags}
        if as_way:
            self.elements.append({"type": "way", "id": element_id, "center": {"lat": point[0], "lon": point[1]}, "tags": tags})
            return f"way/{element_id}"
        self.elements.append({"type": "node", "id": element_id, "lat": point[0], "lon": point[1], "tags": tags})
        return f"node/{element_id}"

    def exit(self, point, ref, road="I 44", highway="motorway", old_ref=None):
        node_id = self._id()
        tags = {"highway": "motorway_junction", "ref": ref}
        if old_ref:
            tags["old_ref"] = old_ref
        self.elements.append({"type": "node", "id": node_id, "lat": point[0], "lon": point[1], "tags": tags})
        self.elements.append({"type": "way", "id": self._id(), "nodes": [node_id - 100, node_id],
                              "tags": {"highway": highway, "ref": road}})
        return f"node/{node_id}"

    def build(self, state="OK"):
        return b.parse_overpass({"elements": self.elements}, state)


def station(name="PILOT TRAVEL CENTER #1243", address="I-44, EXIT 283 & US-69", city="Big Cabin",
            centers=(CENTER,), opis_id=7):
    return b.Station(opis_id, name, address, city, "OK", tuple(centers))


def test_parse_overpass_keeps_numbered_exits_on_highways_only():
    osm = OSMBuilder()
    osm.fuel(CENTER, as_way=True, name="Pilot")
    osm.exit(north(CENTER, 1), "283")
    osm.exit(north(CENTER, 2), "", road="I 44")  # unnumbered exit
    osm.exit(north(CENTER, 3), "12", road="")  # on a way without ref
    state = osm.build()
    assert len(state.fuel) == 1 and state.fuel[0].osm_id.startswith("way/")
    assert [(e.exit_refs, e.highways) for e in state.exits] == [({(283, "")}, {I44})]


def test_fuel_station_with_the_brand_next_to_the_exit_wins():
    osm = OSMBuilder()
    exit_point = north(CENTER, 3)
    osm.exit(exit_point, "283")
    osm.exit(east(exit_point, 0.3), "283")  # the other direction of the same interchange
    pilot = osm.fuel(east(exit_point, 0.5), as_way=True, name="Pilot Travel Center", brand="Pilot")
    osm.fuel(east(exit_point, 0.4), name="Shell", brand="Shell")
    osm.fuel(north(CENTER, -10), name="Pilot")  # another Pilot, far from the exit
    match, why = b.match_station(station(), osm.build())
    assert why == ""
    assert (match.source, match.confidence, match.osm_type_id) == ("osm_fuel", "high", pilot)
    assert match.miles_from_city_center == pytest.approx(3.0, abs=0.1)
    assert "I-44 exit 283" in match.reason


def test_two_canopies_of_one_truck_stop_are_one_site():
    osm = OSMBuilder()
    exit_point = north(CENTER, 3)
    osm.exit(exit_point, "283")
    osm.fuel(east(exit_point, 0.3), name="Love's Travel Stop")
    truck = osm.fuel(east(exit_point, 0.45), name="Love's Travel Stop", hgv="yes")
    match, _ = b.match_station(station(name="LOVES TRAVEL STOP #512"), osm.build())
    assert (match.source, match.confidence, match.osm_type_id) == ("osm_fuel", "high", truck)


def test_several_matching_sites_near_the_exit_is_medium():
    osm = OSMBuilder()
    exit_point = north(CENTER, 3)
    osm.exit(exit_point, "283")
    near = osm.fuel(east(exit_point, 0.2), name="Shell")
    osm.fuel(east(exit_point, -1.2), name="Shell")
    match, _ = b.match_station(station(name="SHELL"), osm.build())
    assert (match.source, match.confidence, match.osm_type_id) == ("osm_fuel", "medium", near)


def test_exit_node_when_no_station_with_the_name_is_near_it():
    osm = OSMBuilder()
    exit_point = north(CENTER, 3)
    far_side = osm.exit(exit_point, "283")
    near_side = osm.exit(north(exit_point, -0.4), "283")  # closer to the city center
    osm.fuel(east(exit_point, 0.3), name="Shell")
    osm.fuel(east(exit_point, 3.0), name="Woodshed")  # right name, but 3 mi from the exit
    match, _ = b.match_station(station(name="WOODSHED OF BIG CABIN"), osm.build())
    assert (match.source, match.confidence, match.osm_type_id) == ("osm_exit", "high", near_side)
    assert far_side != near_side
    assert match.lat == pytest.approx(north(exit_point, -0.4)[0])


def test_exit_on_another_highway_does_not_count():
    osm = OSMBuilder()
    osm.exit(north(CENTER, 3), "283", road="US 69")
    match, why = b.match_station(station(name="WOODSHED OF BIG CABIN"), osm.build())
    assert match is None
    assert "not found" in why


def test_exit_letter_match_is_preferred():
    osm = OSMBuilder()
    osm.exit(north(CENTER, 1), "15A")
    exact = osm.exit(north(CENTER, 1.6), "15B")
    match, _ = b.match_station(station(name="NOWHERE", address="I-44, EXIT 15B"), osm.build())
    assert (match.source, match.osm_type_id, match.confidence) == ("osm_exit", exact, "high")
    # Without its letter the address matches both: the closest, medium (letter differs)
    osm2 = OSMBuilder()
    closest = osm2.exit(north(CENTER, 1), "15A")
    osm2.exit(north(CENTER, 1.6), "15B")
    match, _ = b.match_station(station(name="NOWHERE", address="I-44, EXIT 15C"), osm2.build())
    assert (match.osm_type_id, match.confidence) == (closest, "medium")


def test_same_exit_number_far_apart_is_ambiguous_and_skipped():
    osm = OSMBuilder()
    osm.exit(north(CENTER, 6), "283")
    osm.exit(north(CENTER, -9), "283")  # 15 mi from the other one
    osm.fuel(north(CENTER, 6.2), name="Pilot")
    osm.fuel(north(CENTER, -9.2), name="Pilot")
    match, why = b.match_station(station(), osm.build())
    assert match is None
    assert "ambiguous" in why
    # The exit is unusable, so only the store number can place it: OSM's Pilot "ref 1243"
    numbered = osm.fuel(north(CENTER, 1), name="Pilot", ref="1243")
    match, _ = b.match_station(station(), osm.build())
    assert (match.source, match.confidence, match.osm_type_id) == ("osm_fuel", "high", numbered)
    assert "ambiguous" in match.reason


def test_old_exit_number_after_a_renumbering():
    # Massachusetts renumbered its exits: Salisbury was exit 60, it is exit 90 now.
    osm = OSMBuilder()
    salisbury = osm.exit(north(CENTER, 1), "90", road="I 95", old_ref="60")
    match, _ = b.match_station(station(name="NOWHERE", address="I-95, EXIT 60"), osm.build())
    assert (match.source, match.confidence, match.osm_type_id) == ("osm_exit", "medium", salisbury)
    assert "old exit number" in match.reason
    assert "(old exit 60)" in match.matched_label
    # The new exit 60 is 20 miles away: the old and the new number disagree, skip.
    osm.exit(north(CENTER, -19), "60", road="I 95", old_ref="44")
    match, why = b.match_station(station(name="NOWHERE", address="I-95, EXIT 60"), osm.build())
    assert match is None and "ambiguous" in why


def test_in_a_renumbered_state_a_far_exit_number_is_not_trusted():
    osm = OSMBuilder()
    osm.exit(north(CENTER, -20), "60", road="I 95")  # the NEW exit 60, 20 mi from town
    ma = b.Station(1, "MOBIL", "I-95, EXIT 60", "Salisbury", "MA", (CENTER,))
    match, why = b.match_station(ma, osm.build("MA"))
    assert match is None and "renumbered" in why
    near = OSMBuilder()
    node = near.exit(north(CENTER, 2), "60", road="I 95")
    match, _ = b.match_station(ma, near.build("MA"))
    assert (match.source, match.confidence, match.osm_type_id) == ("osm_exit", "medium", node)


def test_exit_beyond_the_search_area_is_ignored():
    osm = OSMBuilder()
    osm.exit(north(CENTER, 30), "283")
    osm.fuel(north(CENTER, 2), name="Pilot")
    match, why = b.match_station(station(), osm.build())
    assert match is None
    assert why == "I-44 exit 283 not found in OSM within 25 mi; no OSM site with the store number and the name"


def test_without_exit_a_name_alone_is_not_enough():
    # The price file lists several Casey's in one town: a name match alone would put them
    # all on the one store OSM knows. Without an exit, only the store number places one.
    osm = OSMBuilder()
    osm.fuel(north(CENTER, 1), name="Casey's")
    match, why = b.match_station(station(name="CASEYS #12", address="US-69"), osm.build())
    assert match is None
    assert why == "no exit in address; no OSM site with the store number and the name"
    numbered = osm.fuel(north(CENTER, 7), name="Casey's", ref="12")
    match, _ = b.match_station(station(name="CASEYS #12", address="US-69"), osm.build())
    assert (match.source, match.confidence, match.osm_type_id) == ("osm_fuel", "high", numbered)
    assert match.miles_from_city_center == pytest.approx(7.0, abs=0.05)
    assert match.store_confirmed
    # The same store number at two sites: cannot tell
    osm.fuel(north(CENTER, -9), name="Casey's", ref="12")
    match, why = b.match_station(station(name="CASEYS #12", address="US-69"), osm.build())
    assert match is None and "store number is on 2 OSM sites" in why


def test_without_exit_the_store_number_in_osm_ref_is_high():
    osm = OSMBuilder()
    osm.fuel(north(CENTER, 1), name="Love's Travel Stop")
    numbered = osm.fuel(north(CENTER, 12), name="Love's Travel Stop", ref="512")
    match, _ = b.match_station(station(name="LOVES TRAVEL STOP #512", address="US-69"), osm.build())
    assert (match.source, match.confidence, match.osm_type_id) == ("osm_fuel", "high", numbered)


def test_without_exit_a_unique_match_with_another_store_number_is_rejected():
    osm = OSMBuilder()
    osm.fuel(north(CENTER, 1), name="Love's Travel Stop", ref="200")
    match, why = b.match_station(station(name="LOVES TRAVEL STOP #512", address="US-69"), osm.build())
    assert match is None
    assert "no OSM site with the store number" in why


def test_no_match_keeps_the_city_center():
    osm = OSMBuilder()
    osm.fuel(north(CENTER, 1), name="Shell")
    match, why = b.match_station(station(name="ACI TRUCK STOP", address="US-46"), osm.build())
    assert match is None
    assert why == "no exit in address; no store number to confirm a name match"


def test_a_state_route_exit_is_read_on_the_us_route_when_no_state_route_is_near():
    # "SR-395, EXIT 78" in Reno: OSM tags the road US 395
    osm = OSMBuilder()
    node = osm.exit(north(CENTER, 3), "78", road="US 395")
    nowhere = station(name="NOWHERE", address="SR-395, EXIT 78")
    match, _ = b.match_station(nowhere, osm.build())
    assert (match.source, match.confidence, match.osm_type_id) == ("osm_exit", "medium", node)
    assert "state route read as the US route" in match.reason
    # A state route 395 with other exits nearby: the address' exit is just not in OSM
    osm.exit(north(CENTER, -4), "70", road="OK 395")
    match, why = b.match_station(nowhere, osm.build())
    assert match is None and "not found in OSM" in why


def test_at_the_exit_the_site_with_the_store_number_wins():
    # Speedway #3503 at I-95 exit 13: the nearest Speedway is store 3495 in OSM
    osm = OSMBuilder()
    exit_point = north(CENTER, 3)
    osm.exit(exit_point, "13", road="I 95")
    osm.fuel(east(exit_point, 0.5), name="Speedway", ref="3495")
    ours = osm.fuel(east(exit_point, -1.2), name="Speedway", ref="3503")
    match, _ = b.match_station(station(name="SPEEDWAY #3503", address="I-95, EXIT 13"), osm.build())
    assert (match.source, match.confidence, match.osm_type_id) == ("osm_fuel", "high", ours)
    assert match.store_confirmed and "store number" in match.reason


def test_at_the_exit_a_site_of_another_store_is_not_taken():
    osm = OSMBuilder()
    exit_point = north(CENTER, 3)
    node = osm.exit(exit_point, "308", road="I 94")
    osm.fuel(east(exit_point, 0.5), name="Speedway", ref="4152")
    speedway = station(name="SPEEDWAY #4100", address="I-94, EXIT 308")
    match, _ = b.match_station(speedway, osm.build())
    assert (match.source, match.confidence, match.osm_type_id) == ("osm_exit", "medium", node)
    assert "another store" in match.reason
    # An unnumbered Speedway there too: it may be ours, not for sure
    other = osm.fuel(east(exit_point, -0.8), name="Speedway")
    match, _ = b.match_station(speedway, osm.build())
    assert (match.source, match.confidence, match.osm_type_id) == ("osm_fuel", "medium", other)


def test_store_numbers_with_a_chain_prefix_agree():
    assert b.numbers_agree("4707622", "7622")  # Circle K #4707622 is ref 7622 in OSM
    assert b.numbers_agree("512", "512")
    assert not b.numbers_agree("3503", "3495")
    assert not b.numbers_agree("4707622", "2")  # one digit says nothing


def test_exit_nodes_spread_out_place_the_station_at_their_midpoint():
    # Georgetown, TX: one exit-266 ramp per direction, 2.2 mi apart (frontage roads)
    osm = OSMBuilder()
    south = osm.exit(north(CENTER, 1), "266", road="I 35")
    north_side = osm.exit(north(CENTER, 3.2), "266", road="I 35")
    match, _ = b.match_station(station(name="NOWHERE", address="I-35, EXIT 266"), osm.build())
    assert (match.source, match.confidence) == ("osm_exit", "medium")
    assert (match.lat, match.lon) == (pytest.approx(north(CENTER, 2.1)[0]), pytest.approx(CENTER[1]))
    assert match.osm_type_id == "+".join(sorted([south, north_side]))
    assert "midpoint of 2 exit nodes" in match.reason


def test_an_exit_far_from_the_city_needs_more_than_the_address():
    caseys = station(name="CASEYS #3832", address="I-35, EXIT 40", city="Janesville")
    # 23 miles away and no Casey's next to it: not used
    osm = OSMBuilder()
    osm.exit(north(CENTER, 23), "40", road="I 35")
    match, why = b.match_station(caseys, osm.build())
    assert match is None and "mi from the city and no fuel station with the name" in why
    # 8 miles away, and the town's only Casey's is in town: the address gave its nearest exit
    osm = OSMBuilder()
    node = osm.exit(north(CENTER, 8), "40", road="I 35")
    osm.fuel(north(CENTER, 0.3), name="Casey's")
    match, why = b.match_station(caseys, osm.build())
    assert match is None and "the name is in town" in why
    # A chain with several stores in town says nothing about this one: the exit is used
    osm.fuel(north(CENTER, -1.5), name="Casey's")
    match, _ = b.match_station(caseys, osm.build())
    assert (match.source, match.confidence, match.osm_type_id) == ("osm_exit", "high", node)
    # 12 miles away, nothing in town: used, medium
    osm = OSMBuilder()
    osm.exit(north(CENTER, 12), "40", road="I 35")
    match, _ = b.match_station(caseys, osm.build())
    assert (match.source, match.confidence) == ("osm_exit", "medium")


def test_one_osm_station_is_not_given_to_opis_stations_of_different_exits():
    # Kwik Star #932 (exit 143) and a Kwik Star of exit 142: one Kwik Star between both exits
    osm = OSMBuilder()
    e142 = osm.exit(north(CENTER, 2), "142", road="I 80")
    e143 = osm.exit(north(CENTER, 3), "143", road="I 80")
    osm.fuel(east(north(CENTER, 2.5), 0.3), name="Kwik Star")
    at_143 = station(name="KWIK STAR #932", address="I-80, EXIT 143", opis_id=1)
    at_142 = station(name="KWIK STAR", address="I-80, EXIT 142", opis_id=2)
    matches, unmatched = b.match_state([at_143, at_142], osm.build())
    found = {m.opis_id: (m.source, m.osm_type_id, m.confidence) for m in matches}
    assert unmatched == {}
    # Neither can show it is theirs: both go to their own exit
    assert found == {1: ("osm_exit", e143, "medium"), 2: ("osm_exit", e142, "medium")}
    assert "held by another OPIS station" in next(m.reason for m in matches if m.opis_id == 1)
    # OSM carries store 932: that one keeps it, the other goes to its exit
    osm = OSMBuilder()
    e142 = osm.exit(north(CENTER, 2), "142", road="I 80")
    osm.exit(north(CENTER, 3), "143", road="I 80")
    kwik = osm.fuel(east(north(CENTER, 2.5), 0.3), name="Kwik Star", ref="932")
    matches, _ = b.match_state([at_143, at_142], osm.build())
    found = {m.opis_id: (m.source, m.osm_type_id, m.confidence) for m in matches}
    assert found == {1: ("osm_fuel", kwik, "high"), 2: ("osm_exit", e142, "medium")}


def test_opis_stations_of_one_exit_may_share_its_truck_stop():
    # One truck stop listed twice in the price file (two store numbers, the same exit)
    osm = OSMBuilder()
    osm.exit(north(CENTER, 2), "284", road="I 80;US 6")
    pilot = osm.fuel(east(north(CENTER, 2), 0.3), name="Pilot")
    twice = [
        station(name="PILOT #43", address="I-80, EXIT 284", opis_id=3),
        station(name="PILOT #268", address="I-80/US-6, EXIT 284", opis_id=4),
    ]
    matches, _ = b.match_state(twice, osm.build())
    assert {(m.opis_id, m.osm_type_id) for m in matches} == {(3, pilot), (4, pilot)}


def test_ambiguous_city_is_placed_only_by_an_exit_near_one_of_its_homonyms():
    homonyms = (CENTER, north(CENTER, 120))
    osm = OSMBuilder()
    exit_node = osm.exit(north(homonyms[1], 2), "283")
    match, _ = b.match_station(station(name="NOWHERE", centers=homonyms), osm.build())
    assert (match.source, match.confidence, match.osm_type_id) == ("osm_exit", "medium", exit_node)
    assert match.miles_from_city_center == pytest.approx(2.0, abs=0.05)
    # The same exit number near both homonyms: cannot tell
    osm.exit(north(homonyms[0], 2), "283")
    match, why = b.match_station(station(name="NOWHERE", centers=homonyms), osm.build())
    assert match is None and "ambiguous" in why
    # No exit: a brand that is unique near one homonym is not enough
    osm3 = OSMBuilder()
    osm3.fuel(north(homonyms[1], 1), name="Pilot")
    match, why = b.match_station(station(address="US-69", centers=homonyms), osm3.build())
    assert match is None and why.startswith("city ambiguous")


def test_station_without_any_city_is_left_alone():
    match, why = b.match_station(station(centers=()), OSMBuilder().build())
    assert match is None and why == "city not found"


# --- output and download guard -----------------------------------------------------------


def test_csv_is_sorted_with_the_documented_columns(tmp_path):
    rows = [
        b.Match(20, 32.9, -112.7, "osm_fuel", "high", "way/1", "Pilot", 1.234, "why"),
        b.Match(7, 36.5, -95.2, "osm_exit", "medium", "node/2", "I-44 exit 283", 0.5, "why"),
    ]
    path = tmp_path / "coords.csv"
    b.write_csv(rows, path)
    with open(path, encoding="utf-8", newline="") as handle:
        read = list(csv.DictReader(handle))
    assert list(read[0]) == list(b.CSV_COLUMNS)
    assert [r["opis_id"] for r in read] == ["7", "20"]
    assert read[1]["miles_from_city_center"] == "1.23"
    assert read[1]["lat"] == "32.900000"


def test_offline_without_cache_downloads_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(b, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(b, "download_state", lambda *a, **k: pytest.fail("network used"))
    assert b.load_state("TX", offline=True, refresh=False) == (None, False)


def test_a_stale_overpass_instance_is_refused():
    from datetime import datetime, timezone

    now = datetime(2026, 10, 8, tzinfo=timezone.utc)
    b.check_fresh({"osm3s": {"timestamp_osm_base": "2026-10-07T23:00:00Z"}}, now)
    with pytest.raises(b.OverpassError, match="stale"):
        b.check_fresh({"osm3s": {"timestamp_osm_base": "2026-05-31T10:00:00Z"}}, now)
    with pytest.raises(b.OverpassError, match="timestamp"):
        b.check_fresh({}, now)


def test_overpass_queries_ask_for_one_state():
    fuel, exits = b.overpass_queries("TX")
    assert '"ISO3166-2"="US-TX"' in fuel and '"ISO3166-2"="US-TX"' in exits
    assert 'nwr["amenity"="fuel"]' in fuel
    assert "motorway_junction" in exits and "way(bn.exits)" in exits


# --- the committed file ------------------------------------------------------------------


def _exit_numbers(reason: str) -> set[int]:
    """Exit numbers of a "name match 0.25 mi from I-75 exit 358/359A (node/1)" reason."""
    match = re.search(r" exit ((?:\d+[A-Z]?)(?:/\d+[A-Z]?)*) \(", reason)
    return {int(re.match(r"\d+", part).group()) for part in match.group(1).split("/")} if match else set()


def test_the_committed_file_gives_an_osm_fuel_station_to_one_exit_at_most():
    """Two OPIS stations share an OSM fuel station only when both were matched at the
    same exit (one truck stop listed twice); otherwise at most one of them is right.
    The reasons name rules, not the OPIS station names."""
    with open(b.OUTPUT, encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        assert tuple(reader.fieldnames) == b.CSV_COLUMNS
        rows = list(reader)
    assert rows
    assert not [r["opis_id"] for r in rows if "named like" in r["reason"] or "only name match" in r["reason"]]
    claims = defaultdict(list)
    for row in rows:
        if row["source"] == "osm_fuel":
            claims[row["osm_type_id"]].append(row)
    for feature, group in claims.items():
        if len(group) < 2:
            continue
        numbers = [_exit_numbers(row["reason"]) for row in group]
        assert all(numbers), (feature, [row["opis_id"] for row in group])
        joined, pending = set(numbers[0]), numbers[1:]
        while any(n & joined for n in pending):
            joined |= next(n for n in pending if n & joined)
            pending = [n for n in pending if not n <= joined]
        assert not pending, (feature, [row["opis_id"] for row in group])
