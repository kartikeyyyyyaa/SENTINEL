"""The watchlist the edge worker matches every plate and face against.

This is the "live crime detection" half of the submission's Step 3 requirement:
a searchable list of stolen/blacklisted vehicles and wanted/missing persons,
matched continuously against what the edge is already looking at, with the match
itself becoming the seed of a cross-camera trail (see the module docstring on
``rules.py`` for how a match becomes an ``Alert`` and how repeated matches across
cameras become a track for police to follow).

**Why matching happens at the edge, not centrally.** The whole architecture's
bandwidth argument (see ``services/registry/app/federation/base.py``) is that
only small metadata leaves a camera site, never continuous video. Shipping every
plate read to a central service to check against a watchlist and shipping the
result back would add a network round trip to the one path where seconds matter
— a stolen vehicle passing an ANPR camera. So each worker process holds its own
copy of the active watchlist in memory (``WatchlistIndex``) and matches
locally, in the same process and the same millisecond as the plate is read. The
registry stays the system of record — an operator adds or resolves an entry
there — and workers pull the current list on start and on a refresh interval;
see ``resolve_watchlist`` and ``WorkerConfig.watchlist``.

**Vehicles match on plate text; people match on face embedding — never on
appearance category.** See the "deliberate omission" note at the bottom of
``services/common/events.py`` and the module docstring of ``stages/face.py``:
matching against an individually enrolled photo of a specific wanted or missing
person is not the same act as guessing someone's demographics from a wide-angle
CCTV view, and this module only ever does the former.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from .stages.face import Embedding, cosine_similarity
from .stages.plate import normalise as normalise_plate

log = logging.getLogger(__name__)

#: Mirrors ``app.watchlist_entry.entry_type``'s CHECK constraint.
VEHICLE_ENTRY_TYPES = frozenset({"stolen_vehicle", "blacklisted_vehicle"})
PERSON_ENTRY_TYPES = frozenset({"wanted_person", "missing_person", "suspect"})
ENTRY_TYPES = VEHICLE_ENTRY_TYPES | PERSON_ENTRY_TYPES

RISK_LEVELS = ("low", "medium", "high", "critical")


@dataclass(frozen=True, slots=True)
class WatchlistEntry:
    """One row of ``app.watchlist_entry``, as the worker needs it.

    Deliberately not the full registry record: no photo, no free-text notes, no
    officer contact details. The worker's job is match-or-not, and the
    console/registry is where an operator who gets a hit looks up the rest —
    the same "small metadata at the edge, detail behind an authenticated read"
    split as everywhere else in this architecture.
    """

    entry_id: str
    entry_type: str
    risk_level: str = "medium"
    case_reference: str | None = None
    label: str = ""  # Free display text only — e.g. "Stolen: 2019 white Swift".
    plate_number: str | None = None  # Normalised: see stages.plate.normalise.
    embedding: Embedding | None = None

    def __post_init__(self) -> None:
        if self.entry_type not in ENTRY_TYPES:
            raise ValueError(f"unknown watchlist entry_type {self.entry_type!r}")
        if self.risk_level not in RISK_LEVELS:
            raise ValueError(f"unknown risk_level {self.risk_level!r}")
        if self.entry_type in VEHICLE_ENTRY_TYPES and not self.plate_number:
            raise ValueError(f"{self.entry_type} entry {self.entry_id} needs a plate_number")
        if self.entry_type in PERSON_ENTRY_TYPES and not self.embedding:
            # An entry with no embedding yet (photo submitted, enrolment
            # pending) is legitimate in the registry but cannot be matched
            # here, so it is filtered out in from_rows rather than rejected —
            # see that function.
            raise ValueError(f"{self.entry_type} entry {self.entry_id} needs an embedding")


class WatchlistIndex:
    """In-memory, thread-safe lookup: a plate or an embedding to an entry.

    Rebuilt wholesale on refresh rather than diffed, because the watchlist is
    small (thousands, not millions, of entries) and a full rebuild is simpler and
    cannot leave a stale half-applied state — the two properties that matter more
    than the marginal cost of re-hashing a few thousand plates every
    ``watchlist_refresh_seconds``.
    """

    def __init__(self, entries: Sequence[WatchlistEntry] = (), face_threshold: float = 0.90) -> None:
        self._lock = threading.Lock()
        self._face_threshold = face_threshold
        self._by_plate: dict[str, WatchlistEntry] = {}
        self._faces: list[WatchlistEntry] = []
        self.replace(entries)

    def replace(self, entries: Sequence[WatchlistEntry]) -> None:
        by_plate: dict[str, WatchlistEntry] = {}
        faces: list[WatchlistEntry] = []
        for entry in entries:
            if entry.plate_number:
                by_plate[entry.plate_number] = entry
            if entry.embedding:
                faces.append(entry)
        with self._lock:
            self._by_plate = by_plate
            self._faces = faces

    @property
    def size(self) -> int:
        with self._lock:
            return len(self._by_plate) + len(self._faces)

    def match_plate(self, plate_text: str) -> WatchlistEntry | None:
        """Exact match on the normalised plate. See ``stages.plate.normalise`` —
        never on a fuzzy or OCR-confusion-corrected form, for the same evidential
        reason that function gives: a corrected character is a fabricated one,
        and a fabricated match is worse than a missed one."""
        key = normalise_plate(plate_text)
        if not key:
            return None
        with self._lock:
            return self._by_plate.get(key)

    def match_face(self, embedding: Embedding) -> tuple[WatchlistEntry, float] | None:
        """Best match at or above the configured threshold, or ``None``.

        Linear scan. A watchlist of a few thousand people compared against one
        embedding is a few thousand dot products — microseconds — and the
        alternative (an ANN index) is complexity this scale does not earn yet;
        ``docs/`` can note it as the first thing to swap in if the person
        watchlist grows past the point this stops being true.
        """
        with self._lock:
            faces = list(self._faces)
        best: WatchlistEntry | None = None
        best_score = self._face_threshold
        for entry in faces:
            score = cosine_similarity(embedding, entry.embedding or ())
            if score >= best_score:
                best, best_score = entry, score
        return (best, best_score) if best is not None else None


def entry_from_mapping(data: Mapping[str, Any]) -> WatchlistEntry | None:
    """Build one entry from a registry row or a JSON fixture.

    Returns ``None`` (rather than raising) for a person entry with no embedding
    yet: that is a normal, expected state for a freshly-submitted missing-person
    report awaiting photo enrolment, and one bad row must not take down the
    refresh for every other entry — the same "skip and log" posture
    ``config.CameraConfig.from_mapping`` takes on an unrecognised field.
    """
    try:
        entry_type = str(data["entry_type"])
        embedding = data.get("embedding")
        return WatchlistEntry(
            entry_id=str(data.get("entry_id") if "entry_id" in data else data["id"]),
            entry_type=entry_type,
            risk_level=str(data.get("risk_level") or "medium"),
            case_reference=(str(data["case_reference"]) if data.get("case_reference") else None),
            label=str(data.get("label") or ""),
            plate_number=(
                normalise_plate(str(data["plate_number"])) if data.get("plate_number") else None
            ),
            embedding=tuple(float(v) for v in embedding) if embedding else None,
        )
    except (KeyError, ValueError) as exc:
        log.warning("skipping unusable watchlist row %r: %s", data.get("entry_id") or data.get("id"), exc)
        return None


def load_watchlist_from_json(path: str) -> tuple[WatchlistEntry, ...]:
    """A local fixture: ``{"entries": [...]}`` or a bare list. Dry runs, demos,
    and any site running disconnected from the registry."""
    with open(path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    rows = payload.get("entries", payload) if isinstance(payload, dict) else payload
    return tuple(e for e in (entry_from_mapping(r) for r in rows) if e is not None)


def load_watchlist_from_registry(
    base_url: str, token: str = "", timeout: float = 10.0
) -> tuple[WatchlistEntry, ...]:
    """Fetch the active watchlist. Stdlib ``urllib``, matching
    ``config.load_cameras_from_registry`` — see that function's docstring for why
    this worker never adds ``httpx`` for a read this infrequent."""
    if not base_url:
        return ()
    url = base_url.rstrip("/") + "/api/analytics/watchlist"
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"registry rejected the watchlist request ({exc.code}): {url}") from exc
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise RuntimeError(f"could not reach registry at {url}: {exc}") from exc

    rows = payload.get("entries", payload) if isinstance(payload, dict) else payload
    entries = tuple(e for e in (entry_from_mapping(r) for r in rows) if e is not None)
    log.info("registry returned %d active watchlist entr(y/ies)", len(entries))
    return entries


@dataclass
class WatchlistRefresher:
    """Owns the periodic pull, on its own thread, so a slow or unreachable
    registry never blocks a camera thread mid-frame.

    Failure is deliberately quiet-but-logged: a refresh that cannot reach the
    registry leaves the previous, still-good index in place and tries again next
    interval. A worker that went blind to its watchlist because of one dropped
    request would be a worse outcome than matching against a few-minutes-stale
    list.
    """

    index: WatchlistIndex
    base_url: str
    token: str = ""
    interval_seconds: float = 60.0
    _stop: threading.Event = field(default_factory=threading.Event, repr=False)
    _thread: threading.Thread | None = field(default=None, repr=False)

    def start(self) -> None:
        if not self.base_url or self.interval_seconds <= 0:
            return
        self._thread = threading.Thread(target=self._run, name="watchlist-refresh", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            try:
                entries = load_watchlist_from_registry(self.base_url, self.token)
                self.index.replace(entries)
                log.info("watchlist refreshed: %d entries", len(entries))
            except Exception as exc:  # noqa: BLE001 - see docstring
                log.warning("watchlist refresh failed, keeping previous list: %s", exc)


def resolve_watchlist(
    json_path: str = "", registry_url: str = "", registry_token: str = "", face_threshold: float = 0.90
) -> WatchlistIndex:
    """Environment/config wiring, mirroring ``config.resolve_config``'s ordering:
    an explicit local fixture wins (useful for a demo scenario you want
    reproducible), otherwise the registry, otherwise an empty list — a worker
    with no watchlist configured simply never matches anything, which is a valid
    and common configuration (Model 2-only sites), not an error."""
    if json_path:
        entries = load_watchlist_from_json(json_path)
    elif registry_url:
        try:
            entries = load_watchlist_from_registry(registry_url, registry_token)
        except RuntimeError as exc:
            log.warning("initial watchlist load failed, starting empty: %s", exc)
            entries = ()
    else:
        entries = ()
    return WatchlistIndex(entries, face_threshold=face_threshold)


def _now() -> float:  # small seam for tests that want to control timing
    return time.monotonic()
