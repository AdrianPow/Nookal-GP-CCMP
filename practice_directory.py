"""
practice_directory.py — remember the practices that refer to us.

The referrals come from a bounded set of local practices, and each one
carries identifiers that survive a bad scan far better than the practice's
name does: phone and fax numbers, the practice's own web address, the
doctors' provider numbers, the street address. This module keeps a small
JSON table of those identifiers against the practice name a human has
confirmed, so that the next referral from the same practice gets the right
name even when its letterhead is unreadable — deterministically, offline,
and with an audit trail (the table is a readable file, and every fill says
what it matched on).

The table teaches itself. Nothing is entered by hand: every time a reviewer
confirms a referral on the review screen, the practice name they approved
(or corrected) is stored against the identifiers found on that referral.
The first referral from a practice gets whatever extraction manages; every
later one benefits.

Nothing here talks to the network. That is deliberate — the alternative,
looking practices up on the web, would send referral details to a third
party and make extraction irreproducible.

Matching is not blind trust: a directory fill is always confidence 'check',
so the reviewer sees it and what it matched on.
"""

from __future__ import annotations

import datetime as _dt
import difflib
import json
import os
import re
from dataclasses import dataclass, field as _field
from typing import Optional

from nookal_client import provider_number_valid
from referral_extract import (RE_DOMAIN, RE_GP_ADDRESS_BLOCK,
                              RE_PROVIDER_CANDIDATE, is_our_own_clinic)

# Phone and fax numbers are read from the letterhead region only. Further
# down the page the numbers belong to the patient, and a patient's home
# landline learned as a practice key would match their *next* referral even
# if a different practice sends it.
LETTERHEAD_LINES = 18

RE_PHONE_LABELLED = re.compile(
    r"\b(?:P|Ph|Phone|T|Tel|F|Fax)\b[:.]?\s*"
    r"(\(?0\d\)?[\d\s\-]{6,12}\d)", re.I)

# How close a mangled key has to be to a stored one. Domains are long
# strings, so OCR corruption still leaves them well above what any other
# practice's domain reaches ("castehilimedicaicente" scores 0.93 against
# the real stem, while a different medical centre's stem stays under 0.7 —
# "medicalcentre" alone is not enough shared material).
DOMAIN_SIMILARITY = 0.85
ADDRESS_SIMILARITY = 0.80

# How strong each kind of match is, for choosing between records. Provider
# numbers are check-digit validated so a mis-read cannot produce one;
# phones are exact after normalisation; domains and addresses are fuzzy.
KEY_STRENGTH = {"provider_numbers": 3, "phones": 2, "domains": 2,
                "addresses": 1}


# --------------------------------------------------------------------------
# Keys off a referral
# --------------------------------------------------------------------------

def normalise_phone(raw: str) -> Optional[str]:
    """Digits only, mobiles excluded.

    Mobiles are excluded because on the referral forms a bare 04 number is
    almost always the patient's, and a patient's number stored as a
    practice key would mis-match their next referral."""
    digits = re.sub(r"\D", "", raw)
    if digits.startswith("61"):
        digits = "0" + digits[2:]
    if len(digits) != 10 or not digits.startswith("0"):
        return None
    if digits.startswith("04"):
        return None
    return digits


def normalise_address(raw: str) -> str:
    return re.sub(r"[^a-z0-9]", "", raw.lower())


def extract_practice_keys(text: str) -> dict[str, list[str]]:
    """Everything on a referral that identifies the sending practice.

    Provider numbers come from the whole document — on the scanned bundles
    the referral form is pages in. The rest is confined to where practice
    detail actually lives: phones to the letterhead, the address to the
    form's own 'GP details' block.
    """
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    head = lines[:LETTERHEAD_LINES]

    phones = [p for line in head
              for m in RE_PHONE_LABELLED.finditer(line)
              if (p := normalise_phone(m.group(1)))]

    domains = [m.group(1).lower() for line in head
               for m in RE_DOMAIN.finditer(line)
               if not is_our_own_clinic(m.group(1))]

    providers = [re.sub(r"\s+", "", n)
                 for n in RE_PROVIDER_CANDIDATE.findall(text)
                 if provider_number_valid(n)]

    addresses = []
    block = RE_GP_ADDRESS_BLOCK.search(text)
    if block:
        addr = normalise_address(block.group(1))
        if len(addr) >= 8:
            addresses.append(addr)

    def unique(values: list[str]) -> list[str]:
        return list(dict.fromkeys(values))

    return {"phones": unique(phones), "domains": unique(domains),
            "provider_numbers": unique(providers),
            "addresses": unique(addresses)}


# --------------------------------------------------------------------------
# The directory itself
# --------------------------------------------------------------------------

@dataclass
class Match:
    name: str
    matched_on: str            # human-readable, for the review-screen note
    confirmed: int             # how many reviews have confirmed this record


@dataclass
class _Record:
    name: str
    keys: dict[str, list[str]] = _field(default_factory=dict)
    confirmed: int = 0
    updated: str = ""


def _fuzzy(kind: str, key: str, stored: str) -> bool:
    if kind in ("domains", "addresses"):
        threshold = (DOMAIN_SIMILARITY if kind == "domains"
                     else ADDRESS_SIMILARITY)
        return (difflib.SequenceMatcher(None, key, stored).ratio()
                >= threshold)
    return key == stored


_DESCRIBE = {"provider_numbers": "a doctor's provider number",
             "phones": "its phone number",
             "domains": "its web address",
             "addresses": "its street address"}


class PracticeDirectory:
    """The table on disk. Load-on-demand, atomic save, and a corrupt or
    missing file means an empty directory rather than a dead server."""

    def __init__(self, path: str = "practices.json"):
        self.path = path
        self._records: Optional[list[_Record]] = None

    # ------------------------------------------------------------ storage

    @property
    def records(self) -> list[_Record]:
        if self._records is None:
            self._records = self._load()
        return self._records

    def _load(self) -> list[_Record]:
        try:
            with open(self.path, encoding="utf-8") as f:
                return [_Record(**r) for r in json.load(f)]
        except FileNotFoundError:
            return []
        except (OSError, TypeError, ValueError) as exc:
            print(f"  practice directory {self.path} unreadable ({exc}) — "
                  "starting empty; the old file is left in place")
            return []

    def _save(self) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump([vars(r) for r in self.records], f, indent=2)
        os.replace(tmp, self.path)

    # ------------------------------------------------------------- lookup

    def lookup(self, keys: dict[str, list[str]]) -> Optional[Match]:
        """The practice these keys point at, or None.

        Records are scored by the strength of what matched, so if a phone
        key has gone stale (a practice moved, say) but provider numbers
        point elsewhere, the provider numbers win. Ties go to the record
        confirmed most recently — the newest human decision."""
        best: Optional[tuple[int, str, _Record, str]] = None
        for record in self.records:
            score = 0
            kinds: list[str] = []
            for kind, values in keys.items():
                stored = record.keys.get(kind, [])
                if any(_fuzzy(kind, v, s) for v in values for s in stored):
                    score += KEY_STRENGTH.get(kind, 1)
                    kinds.append(kind)
            if not score:
                continue
            candidate = (score, record.updated, record,
                         " and ".join(_DESCRIBE[k] for k in kinds))
            if best is None or candidate[:2] > best[:2]:
                best = candidate
        if best is None:
            return None
        _, _, record, matched_on = best
        return Match(record.name, matched_on, record.confirmed)

    # ------------------------------------------------------------ learning

    def learn(self, name: str, keys: dict[str, list[str]]) -> None:
        """Record a human-confirmed practice name against a referral's keys.

        Called when the reviewer creates the referral in Nookal — the one
        moment the fields on screen are known to have been looked at. The
        name stored is whatever they approved, corrections included, so a
        practice fixed once stays fixed.
        """
        name = " ".join((name or "").split())
        if not name or not any(keys.values()):
            return
        record = self._by_name(name)
        if record is None:
            record = _Record(name=name)
            self.records.append(record)
        for kind, values in keys.items():
            stored = record.keys.setdefault(kind, [])
            stored.extend(v for v in values if v not in stored)
        record.name = name          # keep the casing most recently approved
        record.confirmed += 1
        record.updated = _dt.datetime.now().isoformat(timespec="seconds")
        self._save()

    def _by_name(self, name: str) -> Optional[_Record]:
        wanted = re.sub(r"[^a-z0-9]", "", name.lower())
        for record in self.records:
            if re.sub(r"[^a-z0-9]", "", record.name.lower()) == wanted:
                return record
        return None
