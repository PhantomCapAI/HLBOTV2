"""Deterministic, persistent wallet codenames.

A wallet's codename is derived purely from its address, so the same address
always maps to the same name — it survives restarts and leaderboard-rank changes
and is consistent across every alert. The bare address is no longer the headline
identifier; the codename is.

Format: <Adjective><Noun>-<last 4 of address>, e.g. "SilentOrca-1ee7".
The 4-hex suffix ties the name to the address and makes collisions between two
different wallets sharing the same adjective+noun effectively impossible.
"""
from __future__ import annotations

import hashlib

# Kept PG, distinct, and roughly equal-length for readability. Lengths need not
# be powers of two — we index by a hash byte mod the list length.
_ADJECTIVES = [
    "Silent", "Iron", "Golden", "Crimson", "Shadow", "Solar", "Frozen", "Velvet",
    "Rogue", "Lucky", "Granite", "Obsidian", "Cobalt", "Amber", "Quiet", "Savage",
    "Phantom", "Electric", "Midnight", "Royal", "Feral", "Stoic", "Hollow", "Brave",
    "Cosmic", "Rapid", "Ancient", "Restless", "Hungry", "Noble", "Wild", "Steady",
]
_NOUNS = [
    "Orca", "Falcon", "Bison", "Viper", "Marlin", "Wolf", "Heron", "Lynx",
    "Kraken", "Stag", "Otter", "Raven", "Mantis", "Jaguar", "Badger", "Tuna",
    "Cobra", "Panther", "Walrus", "Osprey", "Bull", "Bear", "Shark", "Hawk",
    "Ferret", "Condor", "Moose", "Gecko", "Swan", "Drake", "Manta", "Rhino",
]


def codename_for(address: str) -> str:
    """Stable codename for a wallet address (same address -> same name forever)."""
    a = (address or "").strip().lower()
    if not a:
        return "Unknown-0000"
    h = hashlib.sha256(a.encode()).digest()
    adjective = _ADJECTIVES[h[0] % len(_ADJECTIVES)]
    noun = _NOUNS[h[1] % len(_NOUNS)]
    suffix = a[-4:] if len(a) >= 4 else f"{h[2]:02x}{h[3]:02x}"[:4]
    return f"{adjective}{noun}-{suffix}"
