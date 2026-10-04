"""Official F1 25 track identities from the UDP specification appendix."""

from __future__ import annotations


# IDs are deliberately sparse. Removed circuits and reverse layouts must not be
# aliased to another ID because the numeric ID is the comparison/PB identity.
TRACK_NAMES = {
    0: "Melbourne",
    2: "Shanghai",
    3: "Sakhir (Bahrain)",
    4: "Catalunya",
    5: "Monaco",
    6: "Montreal",
    7: "Silverstone",
    9: "Hungaroring",
    10: "Spa",
    11: "Monza",
    12: "Singapore",
    13: "Suzuka",
    14: "Abu Dhabi",
    15: "Texas",
    16: "Brazil",
    17: "Austria",
    19: "Mexico",
    20: "Baku (Azerbaijan)",
    26: "Zandvoort",
    27: "Imola",
    29: "Jeddah",
    30: "Miami",
    31: "Las Vegas",
    32: "Losail",
    39: "Silverstone (Reverse)",
    40: "Austria (Reverse)",
    41: "Zandvoort (Reverse)",
}


def is_known_track(track_id: int | None) -> bool:
    return track_id in TRACK_NAMES


def track_name(track_id: int | None, override: str | None = None) -> str:
    if override and not is_known_track(track_id):
        return override
    if track_id is None:
        return "Unknown track"
    return TRACK_NAMES.get(track_id, f"Unknown track (ID {track_id})")
