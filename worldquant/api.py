"""Single source of truth for every WorldQuant BRAIN endpoint and payload shape.

Nothing in the business layer builds a URL or a request body. If BRAIN changes
its contract, this is the only file that needs editing.

Verification status. The contract below was first cross-checked on 2026-09-05
against three independent public implementations (BRAIN publishes no stable REST
spec and this project had no pre-existing client to reuse), then **confirmed
end-to-end against the live API on 2026-09-06** with a real login, submission,
poll and result fetch.

VERIFIED (live)
    POST /authentication                  HTTP Basic Auth, 201 == success,
                                          body contains a "user" key on success.
    POST /simulations                     body = {"type": "REGULAR",
                                                  "regular": <expression>,
                                                  "settings": {...}}
                                          201 + "Location" header == simulation URL
    GET  /simulations/{simulation_id}     in progress -> {"progress": float, ...}
                                          finished    -> {"alpha": <alpha_id>}
                                          failed      -> {"status": "FAIL"|"ERROR",
                                                          "message": ...}
    GET  /alphas/{alpha_id}               "is" block holds sharpe / fitness /
                                          turnover / returns / drawdown / margin /
                                          longCount / shortCount / pnl / bookSize /
                                          startDate / checks[]
    GET  /alphas/{alpha_id}/recordsets/yearly-stats
                                          JSON recordset, NOT CSV:
                                          {"schema": {"properties": [{"name", "type"}...]},
                                           "records": [[<positional values>], ...]}
                                          Columns: year, pnl, bookSize, longCount,
                                          shortCount, turnover, sharpe, returns,
                                          drawdown, margin, fitness, stage("IS"/"OS").
                                          Percent columns are decimal fractions.
    Retry-After header                    server-suggested poll delay
    GET  /alphas/{id}/recordsets          lists available recordsets
    400 body shape                        {"detail": ...} or
                                          {"settings": {"<field>": ["<reason>"]}}

Read-only catalog / account endpoints, also confirmed live:
    GET  /users/self/alphas               paged {"count", "next", "results": [...]};
                                          each alpha carries "grade" and an "is"
                                          block. Params: limit (<=100), offset.
    GET  /data-sets                       dataset catalog: id, name, category,
                                          coverage, valueScore, alphaCount,
                                          userCount, fieldCount. Params: region,
                                          delay, universe, instrumentType, limit,
                                          offset.
    GET  /data-fields                     searchable field catalog. Params add
                                          dataset.id, search, category, type,
                                          order (e.g. -alphaCount), limit, offset.
    GET  /data-fields/{field_id}          single field, including its "dataset".
    GET  /operators                       a bare JSON ARRAY (not a paged object)
                                          of ~66 operators, each with name,
                                          definition, description, category,
                                          level, scope.

Operator signature gotcha: ``hump`` is declared ``hump(x, hump = 0.01)`` and its
second argument MUST be passed by name. ``hump(expr, 0.1)`` is rejected with
``400 Invalid number of inputs : 2, should be exactly 1 input(s)`` because the
positional slot count is checked before parameters are bound.

Operator *availability* gotcha: ``GET /operators`` returns the 66 operators this
account may actually use, and ``ts_max`` / ``ts_min`` are **not** in that list. An
expression using one does not score badly — the simulation fails outright with
``Attempted to use inaccessible or unknown operator "ts_max"``, which cost three
round-24 candidates. Check ``/operators`` before putting a name in a candidate
pool. For a rolling maximum the usable substitutes are ``ts_arg_max(x, d)`` (days
since the max) or ``ts_rank(x, d)`` (where today sits in the window's
distribution); ``max(x, y, ...)`` is element-wise across inputs, not over time.

Catalog endpoint quirks, learned the hard way:

* ``/data-fields`` caps ``limit`` at **50**. Asking for more returns
  ``400 ["Invalid query: pagination limit too high."]`` — a bare JSON *array* as
  the error body, so error handling must tolerate both shapes.
* Filtering by dataset is **``dataset.id=option9``**. The obvious ``dataset=``
  is *silently ignored*: HTTP 200 with the full 4367-field catalog and no error,
  so a wrong parameter name looks like "this dataset has everything". Verify the
  returned rows' ``dataset.id`` rather than trusting the count.
* ``/data-fields`` returns ``400 ["Invalid query"]`` unless **both** ``universe``
  and ``instrumentType`` are supplied alongside ``region`` and ``delay``.
* ``/data-sets`` returns each dataset **once per universe** — 84 rows for the 14
  USA/delay-1 datasets. Dedupe by ``id`` before treating the list as a catalog.
* **Catalog availability is not simulation availability.** ``/data-sets`` and
  ``/data-fields`` both answer for ``delay=0`` (option8's 64 fields, pv13's 37),
  but ``POST /simulations`` rejects it with
  ``400 {'settings': {'delay': ['Delay 0 is not available.']}}`` — it is an
  account entitlement, not a data property. Four round-20 candidates were lost to
  this. Probe with one cheap simulation before building a delay sweep.
* ``search=`` matches loosely (descriptions included) and is NOT a prefix
  filter: searching a long field-name prefix returns almost nothing, while
  searching one of its words returns thousands. To enumerate a field family,
  search several semantic terms and filter client-side, or probe
  ``/data-fields/{id}`` directly and read 404 as "does not exist".
* ``category`` takes the lowercase catalog id: ``category=option`` returns the
  138 option fields, while ``category=FUNDAMENTAL`` returned ``count: 0`` for
  USA/TOP3000/delay 1 despite fundamental datasets plainly existing.

Two live-API findings worth remembering:

* The yearly-stats response was assumed to be CSV. It is a self-describing JSON
  recordset with positional rows, so :func:`parse_yearly_stats` maps columns by
  schema name rather than index.
* ``settings.pasteurization`` / ``settings.nanHandling`` must be the *strings*
  "ON"/"OFF". YAML 1.1 parses a bare ``ON``/``OFF`` as a boolean, which serializes
  to JSON ``true``/``false`` and earns
  ``400 {'settings': {'pasteurization': ['"True" is not a valid choice.']}}``.
  :func:`normalize_settings` coerces those booleans back. ``visualization`` is the
  one setting that genuinely is a boolean.

Settings field names differ from the ones used in the original task brief: the
real API is camelCase (`unitHandling`, `nanHandling`, `instrumentType`) and also
requires `language` ("FASTEXPR") and `visualization`. `normalize_settings` accepts
the snake_case spellings too so config files stay readable.
"""

from __future__ import annotations

import json
import re
from typing import Any, Final, Sequence

from .exceptions import ConfigError

DEFAULT_BASE_URL: Final[str] = "https://api.worldquantbrain.com"

AUTHENTICATION_PATH: Final[str] = "/authentication"
SIMULATIONS_PATH: Final[str] = "/simulations"
ALPHAS_PATH: Final[str] = "/alphas"
#: Paged list of the account's own alphas. Response shape:
#: ``{"count", "next", "previous", "results": [...]}``; params ``limit`` (<=100)
#: and ``offset``. Each result carries the same blocks as ``GET /alphas/{id}``
#: (``is``, ``settings``, ``grade``) plus the ``regular`` expression string.
SELF_ALPHAS_PATH: Final[str] = "/users/self/alphas"
#: ``GET /alphas/{id}/check`` resolves the submission checks. Asynchronous: it
#: answers 200 with an empty text/html body plus Retry-After while computing.
ALPHA_CHECK_SUFFIX: Final[str] = "/check"
DATA_FIELDS_PATH: Final[str] = "/data-fields"

# Verified against the live API; overridable via config or WQBRAIN_YEARLY_PATH.
DEFAULT_YEARLY_STATS_PATH: Final[str] = "/alphas/{alpha_id}/recordsets/yearly-stats"

LOCATION_HEADER: Final[str] = "Location"
RETRY_AFTER_HEADER: Final[str] = "Retry-After"

DEFAULT_USER_AGENT: Final[str] = (
    "worldquant-backtest-tool/1.0 (+https://github.com/local; python-requests)"
)

# snake_case (friendly for config files) -> camelCase (what BRAIN expects)
_SETTINGS_ALIASES: Final[dict[str, str]] = {
    "instrument_type": "instrumentType",
    "unit_handling": "unitHandling",
    "nan_handling": "nanHandling",
}

DEFAULT_SETTINGS: Final[dict[str, Any]] = {
    "instrumentType": "EQUITY",
    "region": "USA",
    "universe": "TOP3000",
    "delay": 1,
    "decay": 0,
    "neutralization": "INDUSTRY",
    "truncation": 0.08,
    "pasteurization": "ON",
    "unitHandling": "VERIFY",
    "nanHandling": "OFF",
    "language": "FASTEXPR",
    "visualization": False,
    #: Mandatory for this project's searches: hold out the last year as a test
    #: period, splitting the in-sample window into train/test so a result cannot
    #: be silently overfit to the whole period. Live alphas carrying this setting
    #: came back with populated "train" and "test" blocks.
    "testPeriod": "P1Y",
}


class SimulationStatus:
    """Internal lifecycle states, mirrored in the SQLite `simulations` table."""

    PENDING: Final[str] = "PENDING"
    SUBMITTED: Final[str] = "SUBMITTED"
    RUNNING: Final[str] = "RUNNING"
    COMPLETED: Final[str] = "COMPLETED"
    FAILED: Final[str] = "FAILED"
    TIMEOUT: Final[str] = "TIMEOUT"
    AUTH_ERROR: Final[str] = "AUTH_ERROR"
    REQUEST_ERROR: Final[str] = "REQUEST_ERROR"
    SKIPPED: Final[str] = "SKIPPED"

    TERMINAL: Final[frozenset[str]] = frozenset(
        {COMPLETED, FAILED, TIMEOUT, AUTH_ERROR, REQUEST_ERROR, SKIPPED}
    )

    #: States where an in-flight remote simulation is worth picking up again.
    #:
    #: TIMEOUT and REQUEST_ERROR are here on purpose: we stopped waiting, but the
    #: server-side simulation is very likely still running. Re-polling is cheap,
    #: whereas re-submitting burns the account's simulation quota and creates a
    #: duplicate alpha on the platform. PENDING is included because the row was
    #: created before the POST returned, so it must be reused rather than
    #: orphaned.
    #:
    #: Deliberately excluded: FAILED (the server rejected the expression and
    #: would reject it again), AUTH_ERROR (the fix is credentials, not polling),
    #: COMPLETED and SKIPPED (nothing left to do).
    RESUMABLE: Final[frozenset[str]] = frozenset(
        {PENDING, SUBMITTED, RUNNING, TIMEOUT, REQUEST_ERROR}
    )

    #: Remote statuses BRAIN reports that mean "this will never finish".
    REMOTE_FAILURE: Final[frozenset[str]] = frozenset({"FAIL", "FAILED", "ERROR"})


class AlphaGrade:
    """BRAIN's overall verdict on a completed alpha, from ``GET /alphas/{id}``.

    Vocabulary observed on the live API: INFERIOR, AVERAGE, GOOD. The scale may
    be wider than one account happens to have produced, so unknown values are
    stored and compared verbatim rather than rejected.

    What decides the grade
    ----------------------
    **Fitness**, not the checks. Across 60+ locally simulated alphas plus 9 from
    a live account, sorting by fitness separates the grades with **zero**
    monotonicity violations, while sorting by sharpe produces 51. Observed
    brackets (bracketed, not pinned — no sample landed between them)::

        fitness <= 0.83          INFERIOR    (57 local samples)
        1.01 .. 1.50             AVERAGE     (8 account alphas, plus 1.04-1.50 local)
        1.52 .. 1.77             GOOD        (5 local, 1 account)
        2.07 .. 2.30             EXCELLENT   (3 local)
        >= 2.69                  SPECTACULAR (1 local: sharpe 2.28, returns 17.5%,
                                              drawdown 7.3%, every check passing)

    Boundaries are bracketed, not pinned — no sample landed inside them:
    INFERIOR/AVERAGE in (0.83, 1.01], AVERAGE/GOOD in (1.50, 1.52],
    GOOD/EXCELLENT in (1.77, 2.07], EXCELLENT/SPECTACULAR in (2.30, 2.69].

    ``EXCELLENT`` and then ``SPECTACULAR`` were each discovered only when a live
    run returned them; the account's own history had never produced either.
    Treat the vocabulary as open-ended and add tiers as they appear.

    ``is.checks`` is a **separate** concern: it gates whether an alpha can be
    submitted to WorldQuant, not how it is graded. Decisive counter-evidence for
    the opposite reading — an alpha graded AVERAGE while failing LOW_SHARPE
    (0.66 vs limit 1.25), LOW_TURNOVER (0.0097 vs 0.01) and
    LOW_SUB_UNIVERSE_SHARPE, and two graded GOOD with failing checks as well.
    An earlier inference that "grade == all IS checks pass" came from a
    confounded sample: that account's AVERAGE alphas were well constructed and
    happened to pass every check too. (The EXCELLENT alpha does pass every
    check, but that is a consequence of its fitness, not the cause of its grade.)

    The ``train`` / ``test`` blocks carry metrics but no checks, and do not
    affect the grade either.

    Fitness formula (reverse-engineered, then verified against seven live alphas
    spanning all four grades)::

        fitness = sharpe * sqrt(|returns| / max(turnover, 0.125))

    The ``max(turnover, 0.125)`` floor is what makes low-turnover alphas score
    well: below 12.5% turnover, driving it lower buys nothing, so returns and
    sharpe are the only remaining levers. Conversely a very low turnover (below
    the 0.01 LOW_TURNOVER check limit) is fine for grading but blocks submission.

    A high grade does NOT imply a usable alpha: ``neutralization=NONE`` with an
    always-positive ``rank()`` yields a long-only book (``shortCount == 0``) whose
    returns come from directional concentration. Two such alphas graded GOOD with
    53% drawdown. Check ``AlphaResult.is_one_sided_book`` before trusting a grade.

    Check thresholds observed live, for reference::

        LOW_SHARPE               sharpe   >= 1.25
        LOW_FITNESS              fitness  >= 1.0
        LOW_TURNOVER             turnover >= 0.01
        HIGH_TURNOVER            turnover <= 0.7
        LOW_SUB_UNIVERSE_SHARPE  sub-universe sharpe >= ~0.43 * sharpe
        CONCENTRATED_WEIGHT      no single position too large
        SELF_CORRELATION         PENDING until checked
        MATCHES_COMPETITION      competition eligibility
    """

    INFERIOR: Final[str] = "INFERIOR"
    AVERAGE: Final[str] = "AVERAGE"
    GOOD: Final[str] = "GOOD"
    EXCELLENT: Final[str] = "EXCELLENT"
    SPECTACULAR: Final[str] = "SPECTACULAR"

    #: Observed values, worst first. Used for ordering and for deciding whether
    #: a result meets or beats a target grade.
    OBSERVED_ORDER: Final[tuple[str, ...]] = (
        INFERIOR, AVERAGE, GOOD, EXCELLENT, SPECTACULAR,
    )

    @classmethod
    def rank(cls, grade: str | None) -> int:
        """Sort weight for a grade; unknown or missing grades rank lowest."""
        if not grade:
            return -1
        try:
            return cls.OBSERVED_ORDER.index(grade.strip().upper())
        except ValueError:
            return -1


# --------------------------------------------------------------------------- #
# URLs
# --------------------------------------------------------------------------- #
def authentication_url(base_url: str) -> str:
    return base_url.rstrip("/") + AUTHENTICATION_PATH


def simulations_url(base_url: str) -> str:
    return base_url.rstrip("/") + SIMULATIONS_PATH


def simulation_url(base_url: str, simulation_id: str) -> str:
    return f"{base_url.rstrip('/')}{SIMULATIONS_PATH}/{simulation_id}"


def alpha_url(base_url: str, alpha_id: str) -> str:
    return f"{base_url.rstrip('/')}{ALPHAS_PATH}/{alpha_id}"


def alpha_check_url(base_url: str, alpha_id: str) -> str:
    return f"{alpha_url(base_url, alpha_id)}{ALPHA_CHECK_SUFFIX}"


def self_alphas_url(base_url: str) -> str:
    return base_url.rstrip("/") + SELF_ALPHAS_PATH


def yearly_stats_url(base_url: str, alpha_id: str, path_template: str) -> str:
    return base_url.rstrip("/") + path_template.format(alpha_id=alpha_id)


def simulation_id_from_location(location: str) -> str:
    """Extract the simulation id from the ``Location`` header.

    BRAIN returns an absolute URL such as
    ``https://api.worldquantbrain.com/simulations/abc123``. The id is the last
    path segment; falling back to the whole header keeps resume working even if
    the shape changes.
    """
    cleaned = location.strip().rstrip("/")
    if not cleaned:
        return ""
    tail = cleaned.rsplit("/", 1)[-1]
    return tail or cleaned


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
def normalize_settings(settings: dict[str, Any] | None) -> dict[str, Any]:
    """Merge user settings over :data:`DEFAULT_SETTINGS` and fix key spelling.

    Accepts both ``unit_handling`` and ``unitHandling``. Unknown keys are kept
    so future BRAIN settings pass through without a code change.
    """
    merged: dict[str, Any] = dict(DEFAULT_SETTINGS)
    for raw_key, value in (settings or {}).items():
        if value is None:
            continue
        key = _SETTINGS_ALIASES.get(str(raw_key), str(raw_key))
        merged[key] = value

    #: Settings BRAIN expects as the literal strings "ON" / "OFF".
    on_off_keys = ("pasteurization", "nanHandling")
    #: Settings BRAIN expects as an upper-cased enum string.
    enum_keys = ("region", "universe", "neutralization", "unitHandling",
                 "instrumentType", "language")
    # YAML 1.1 boolean words. Unquoted they arrive as real booleans; quoted they
    # arrive as strings. Either way BRAIN only accepts "ON" / "OFF".
    yaml_true_words = frozenset({"on", "yes", "true", "y", "1"})
    yaml_false_words = frozenset({"off", "no", "false", "n", "0"})

    for key in on_off_keys:
        value = merged.get(key)
        if isinstance(value, bool):
            # YAML 1.1 parses a bare ON/OFF/yes/no as a boolean, so
            # `pasteurization: ON` arrives here as True. BRAIN rejects JSON
            # true/false with 400 '"True" is not a valid choice' — it wants the
            # string. Coerce it back rather than making the user quote it.
            merged[key] = "ON" if value else "OFF"
        elif isinstance(value, str):
            text = value.strip()
            lowered = text.lower()
            if lowered in yaml_true_words:
                merged[key] = "ON"
            elif lowered in yaml_false_words:
                merged[key] = "OFF"
            else:
                merged[key] = text.upper()

    for key in enum_keys:
        value = merged.get(key)
        if isinstance(value, bool):
            raise ConfigError(
                f"simulation setting {key!r} must be a string, got the boolean "
                f"{value!r}. YAML parses bare ON/OFF/yes/no as booleans, so quote "
                f'the value (e.g. {key}: "VERIFY").'
            )
        if isinstance(value, str):
            merged[key] = value.strip().upper()

    for int_key in ("delay", "decay"):
        value = merged.get(int_key)
        if isinstance(value, str):
            text = value.strip()
            if text.lstrip("-").isdigit():
                merged[int_key] = int(text)
        elif isinstance(value, float) and value.is_integer():
            merged[int_key] = int(value)

    truncation = merged.get("truncation")
    if isinstance(truncation, str):
        try:
            merged["truncation"] = float(truncation)
        except ValueError:
            pass

    visualization = merged.get("visualization")
    if isinstance(visualization, str):
        merged["visualization"] = visualization.strip().lower() in {"1", "true", "yes", "on"}

    return merged


def canonical_settings_json(settings: dict[str, Any] | None) -> str:
    """Deterministic JSON for hashing: sorted keys, normalized spelling."""
    return json.dumps(normalize_settings(settings), sort_keys=True, separators=(",", ":"))


def build_simulation_payload(expression: str, settings: dict[str, Any] | None) -> dict[str, Any]:
    """Build the POST /simulations body.

    Verified shape (see module docstring):
        {"type": "REGULAR", "regular": <expression>, "settings": {...}}
    """
    return {
        "type": "REGULAR",
        "regular": expression,
        "settings": normalize_settings(settings),
    }


# --------------------------------------------------------------------------- #
# Response parsing (defensive: never assume a field exists)
# --------------------------------------------------------------------------- #
def _as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _as_int(value: Any) -> int | None:
    number = _as_float(value)
    if number is None:
        return None
    try:
        return int(number)
    except (OverflowError, ValueError):
        return None


def _dig(payload: Any, *path: str) -> Any:
    """Safely walk a nested dict, returning ``None`` when any level is missing."""
    current = payload
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current


def parse_simulation_progress(payload: Any) -> dict[str, Any]:
    """Turn a GET /simulations/{id} body into an internal status dict.

    Returns keys: ``status`` (internal SimulationStatus), ``alpha_id``,
    ``progress`` (0..1 or None), ``message``.
    """
    if not isinstance(payload, dict):
        return {
            "status": SimulationStatus.REQUEST_ERROR,
            "alpha_id": None,
            "progress": None,
            "message": f"unexpected simulation payload type: {type(payload).__name__}",
        }

    alpha_id = payload.get("alpha")
    if isinstance(alpha_id, str) and alpha_id:
        return {
            "status": SimulationStatus.COMPLETED,
            "alpha_id": alpha_id,
            "progress": _as_float(payload.get("progress")),
            "message": None,
        }

    remote_status = payload.get("status")
    message = payload.get("message") or payload.get("detail")
    if isinstance(remote_status, str) and remote_status.upper() in SimulationStatus.REMOTE_FAILURE:
        return {
            "status": SimulationStatus.FAILED,
            "alpha_id": None,
            "progress": None,
            "message": str(message) if message is not None else f"remote status={remote_status}",
        }

    progress = _as_float(payload.get("progress"))
    if progress is None and message is not None:
        # No progress and an explanatory message: BRAIN rejected the run.
        return {
            "status": SimulationStatus.FAILED,
            "alpha_id": None,
            "progress": None,
            "message": str(message),
        }

    return {
        "status": SimulationStatus.RUNNING,
        "alpha_id": None,
        "progress": progress,
        "message": str(message) if message is not None else None,
    }


# Mapping from BRAIN "is" block keys -> internal AlphaResult attributes.
IS_METRIC_FIELDS: Final[dict[str, str]] = {
    "sharpe": "sharpe",
    "fitness": "fitness",
    "turnover": "turnover",
    "returns": "returns",
    "drawdown": "drawdown",
    "margin": "margin",
    "pnl": "pnl",
    "bookSize": "book_size",
    "longCount": "long_count",
    "shortCount": "short_count",
}


def parse_stage_block(block: Any) -> dict[str, Any] | None:
    """Extract metrics from one of BRAIN's period blocks (``train``/``test``/``os``).

    Returns ``None`` when the block is absent, which is the normal case for a
    simulation run without ``testPeriod``. Shares :data:`IS_METRIC_FIELDS` with
    the ``is`` block because BRAIN uses the same field names in each.
    """
    if not isinstance(block, dict):
        return None

    parsed: dict[str, Any] = {"start_date": block.get("startDate")}
    for remote_key, attr in IS_METRIC_FIELDS.items():
        value = block.get(remote_key)
        parsed[attr] = (
            _as_int(value) if attr in {"long_count", "short_count"} else _as_float(value)
        )
    return parsed


#: The eight checks BRAIN evaluates before an alpha may be submitted. The user's
#: acceptance rule is that all eight must read PASS; a high grade alone is not
#: enough, because grade follows fitness and ignores submittability.
SUBMISSION_CHECKS: Final[tuple[str, ...]] = (
    "LOW_SHARPE",
    "LOW_FITNESS",
    "LOW_TURNOVER",
    "HIGH_TURNOVER",
    "CONCENTRATED_WEIGHT",
    "LOW_SUB_UNIVERSE_SHARPE",
    "SELF_CORRELATION",
    "MATCHES_COMPETITION",
)

#: Columns of the ``selfCorrelated`` recordset, in the order observed live.
_SELF_CORRELATED_COLUMNS: Final[tuple[str, ...]] = (
    "alpha_id", "name", "instrument_type", "region", "universe",
    "correlation", "sharpe", "turnover", "drawdown", "fitness", "margin",
)


def parse_check_payload(payload: Any) -> dict[str, Any]:
    """Parse ``GET /alphas/{alpha_id}/check``.

    Everything is nested under an ``is`` key holding ``checks`` plus a
    ``selfCorrelated`` recordset. This endpoint is the only way to resolve
    ``SELF_CORRELATION``: the plain ``GET /alphas/{id}`` payload reports it as
    ``PENDING`` forever, so an alpha can look fully graded while still being
    unchecked for correlation against the account's existing alphas.

    The endpoint is asynchronous — it answers ``200`` with an empty ``text/html``
    body and a ``Retry-After`` header while computing, so callers must poll until
    JSON arrives.

    Returns ``checks`` (normalized by name), ``self_correlation`` (the worst
    correlation found), ``self_correlated_with`` (which alphas, and how closely),
    ``passed_count``, ``total``, ``all_passed`` and ``failures``.
    """
    empty: dict[str, Any] = {
        "checks": {},
        "self_correlation": None,
        "self_correlated_with": [],
        "passed_count": 0,
        "total": 0,
        "all_passed": False,
        "failures": [],
    }
    if not isinstance(payload, dict):
        return empty

    is_block = payload.get("is")
    if not isinstance(is_block, dict):
        # Some responses put the checks at the top level; tolerate both.
        is_block = payload

    checks: dict[str, Any] = {}
    raw_checks = is_block.get("checks")
    if isinstance(raw_checks, list):
        for check in raw_checks:
            if isinstance(check, dict) and isinstance(check.get("name"), str):
                checks[check["name"]] = {
                    "result": check.get("result"),
                    "limit": check.get("limit"),
                    "value": check.get("value"),
                    "competitions": check.get("competitions"),
                }

    correlated = is_block.get("selfCorrelated")
    self_correlation: float | None = None
    correlated_with: list[dict[str, Any]] = []
    if isinstance(correlated, dict):
        self_correlation = _as_float(correlated.get("max"))
        schema = correlated.get("schema")
        names = [
            str(prop.get("name"))
            for prop in (schema or {}).get("properties", [])
            if isinstance(prop, dict) and prop.get("name")
        ] or list(_SELF_CORRELATED_COLUMNS)
        for record in correlated.get("records") or []:
            if not isinstance(record, (list, tuple)):
                continue
            row = {
                name: (record[index] if index < len(record) else None)
                for index, name in enumerate(names)
            }
            correlated_with.append(row)
            # The recordset's own correlation column is authoritative when the
            # top-level max is missing.
            value = _as_float(row.get("correlation"))
            if value is not None and (self_correlation is None or value > self_correlation):
                self_correlation = value

    failures = failed_check_reasons(checks)
    return {
        "checks": checks,
        "self_correlation": self_correlation,
        "self_correlated_with": correlated_with,
        "passed_count": passed_check_count(checks),
        "total": len(checks),
        "all_passed": bool(checks) and not failures,
        "failures": failures,
    }


def passed_check_count(checks: dict[str, Any] | None) -> int:
    """How many of the resolved submission checks read PASS."""
    return sum(
        1
        for check in (checks or {}).values()
        if isinstance(check, dict) and str(check.get("result") or "").upper() == "PASS"
    )


def failed_check_reasons(checks: dict[str, Any] | None) -> list[str]:
    """Human-readable reasons for every check that is not PASS.

    ``PENDING`` counts as not-passed: an unresolved check is not evidence that
    the alpha is submittable, which is exactly why the check endpoint has to be
    called rather than reading the alpha payload.
    """
    failures: list[str] = []
    for name, check in (checks or {}).items():
        if not isinstance(check, dict):
            continue
        result = str(check.get("result") or "").upper()
        if result == "PASS":
            continue
        value = check.get("value")
        limit = check.get("limit")
        if value is not None and limit is not None:
            failures.append(f"{name}={value} (limit {limit}): {result or 'NO RESULT'}")
        elif value is not None:
            failures.append(f"{name}={value}: {result or 'NO RESULT'}")
        else:
            failures.append(f"{name}: {result or 'NO RESULT'}")
    return failures


def all_checks_passed(
    checks: dict[str, Any] | None,
    *,
    expected: Sequence[str] = SUBMISSION_CHECKS,
) -> bool:
    """True when every expected submission check is present and PASS.

    Matching on names rather than on a count matters: a response that dropped
    ``SELF_CORRELATION`` while carrying some unknown extra check would otherwise
    still read as eight PASSes, and an alpha whose correlation was never resolved
    is precisely what this gate exists to refuse. Checks BRAIN adds beyond the
    expected set are allowed but must also PASS.
    """
    if not isinstance(checks, dict) or not checks:
        return False
    for name in expected:
        check = checks.get(name)
        if not isinstance(check, dict):
            return False
        if str(check.get("result") or "").upper() != "PASS":
            return False
    return all(
        str(check.get("result") or "").upper() == "PASS"
        for check in checks.values()
        if isinstance(check, dict)
    )


def parse_alpha_payload(payload: Any) -> dict[str, Any]:
    """Extract metrics from GET /alphas/{alpha_id}.

    Missing keys become ``None`` instead of raising: BRAIN omits fields for
    failed/partial runs, and one missing metric must not discard the whole
    alpha. ``checks`` is normalized to ``{name: {value, result, limit}}``.

    Turnover is kept exactly as BRAIN returns it — a decimal fraction where
    0.423 means 42.3%. Formatting to percent happens only at display time.
    """
    result: dict[str, Any] = {field: None for field in IS_METRIC_FIELDS.values()}
    result.update(
        {
            "alpha_id": None,
            "date_created": None,
            "status": None,
            "grade": None,
            "stage": None,
            "start_date": None,
            "train": None,
            "test": None,
            "checks": {},
            "settings": None,
        }
    )

    if not isinstance(payload, dict):
        return result

    result["alpha_id"] = payload.get("id") if isinstance(payload.get("id"), str) else None
    result["date_created"] = payload.get("dateCreated")
    result["status"] = payload.get("status")
    # BRAIN's overall verdict on the alpha, e.g. INFERIOR / AVERAGE / GOOD.
    grade = payload.get("grade")
    result["grade"] = grade.strip().upper() if isinstance(grade, str) else None
    stage = payload.get("stage")
    result["stage"] = stage.strip().upper() if isinstance(stage, str) else None
    settings = payload.get("settings")
    result["settings"] = settings if isinstance(settings, dict) else None
    # Present only when the simulation was run with a testPeriod; the held-out
    # year is the whole point of that setting, so it must not be discarded.
    result["train"] = parse_stage_block(payload.get("train"))
    result["test"] = parse_stage_block(payload.get("test"))

    is_block = payload.get("is")
    if isinstance(is_block, dict):
        for remote_key, attr in IS_METRIC_FIELDS.items():
            value = is_block.get(remote_key)
            result[attr] = _as_int(value) if attr in {"long_count", "short_count"} else _as_float(value)
        result["start_date"] = is_block.get("startDate")

        checks = is_block.get("checks")
        if isinstance(checks, list):
            normalized: dict[str, Any] = {}
            for check in checks:
                if isinstance(check, dict) and isinstance(check.get("name"), str):
                    normalized[check["name"]] = {
                        "value": check.get("value"),
                        "result": check.get("result"),
                        "limit": check.get("limit"),
                    }
            result["checks"] = normalized

    return result


#: Candidate locations of the expression string on an alpha list/detail row, in
#: priority order. List rows carry ``regular``; tolerate the other spellings.
_EXPRESSION_KEYS: Final[tuple[str, ...]] = ("regular", "expression", "code", "formula")


def extract_alpha_expression(payload: Any) -> str:
    """Pull the FASTEXPR body out of an alpha object, tolerating schema drift."""
    if not isinstance(payload, dict):
        return ""
    for key in _EXPRESSION_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    # Some envelopes nest the expression under a ``regular`` block.
    regular = payload.get("regular")
    if isinstance(regular, dict):
        for key in _EXPRESSION_KEYS:
            value = regular.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        if isinstance(regular.get("code"), str):
            return regular["code"].strip()
    return ""


def parse_self_alphas_page(payload: Any) -> dict[str, Any]:
    """Parse one ``GET /users/self/alphas`` page.

    Returns ``{"count", "next", "results"}`` where each result is a normalized
    dict with ``alpha_id``, ``expression``, ``settings``, ``grade``, the IS
    metric block and the check/date fields :func:`parse_alpha_payload` recovers.
    Defensive throughout: an item without an expression is skipped rather than
    imported as an empty row.
    """
    empty = {"count": 0, "next": None, "results": []}
    if not isinstance(payload, dict):
        return empty

    raw_results = payload.get("results")
    if not isinstance(raw_results, list):
        return empty

    results: list[dict[str, Any]] = []
    for item in raw_results:
        if not isinstance(item, dict):
            continue
        parsed = parse_alpha_payload(item)
        expression = extract_alpha_expression(item)
        if not expression:
            continue
        results.append(
            {
                "alpha_id": parsed.get("alpha_id") or item.get("id"),
                "expression": expression,
                "settings": parsed.get("settings") if isinstance(parsed.get("settings"), dict) else {},
                "grade": parsed.get("grade"),
                "stage": parsed.get("stage"),
                "status": parsed.get("status") or item.get("status"),
                "date_created": parsed.get("date_created"),
                "metrics": {
                    key: parsed.get(key)
                    for key in (
                        "sharpe", "fitness", "turnover", "returns", "drawdown",
                        "margin", "pnl", "book_size", "long_count", "short_count",
                    )
                },
                "train": parsed.get("train"),
                "test": parsed.get("test"),
                "checks": parsed.get("checks") or {},
                "raw": item,
            }
        )

    return {
        "count": _as_int(payload.get("count")) or len(results),
        "next": payload.get("next") if isinstance(payload.get("next"), str) else None,
        "results": results,
    }


#: Yearly-stats recordset columns we keep, mapped to internal snake_case names.
_YEARLY_METRIC_COLUMNS: Final[dict[str, str]] = {
    "sharpe": "sharpe",
    "fitness": "fitness",
    "turnover": "turnover",
    "returns": "returns",
    "drawdown": "drawdown",
    "margin": "margin",
    "pnl": "pnl",
    "bookSize": "book_size",
    "longCount": "long_count",
    "shortCount": "short_count",
}

#: Values of the recordset's ``stage`` column that mean "in sample".
#:
#: Without a ``testPeriod`` the rows read ``IS`` / ``OS``. With one they read
#: ``TRAIN`` / ``TEST`` — filtering on ``IS`` alone silently dropped *every* row
#: once ``testPeriod=P1Y`` became mandatory, so a whole batch lost its yearly
#: stats while the endpoint kept returning a valid, fully populated recordset.
_YEARLY_IN_SAMPLE_STAGES: Final[frozenset[str]] = frozenset({"IS", "TRAIN"})


def parse_yearly_stats(payload: Any) -> dict[str, dict[str, float | None]]:
    """Parse ``GET /alphas/{id}/recordsets/yearly-stats`` into ``{year: {metric: value}}``.

    Verified shape (confirmed against the live API)::

        {"schema": {"name": "yearly-stats",
                    "properties": [{"name": "year", "type": "year"},
                                   {"name": "pnl", "type": "amount"}, ...]},
         "records": [["2019", -706865.0, 20000000, 1551, 1556, 0.585,
                      -1.59, -0.0701, 0.0765, -0.00024, -0.55, "IS"], ...]}

    Records are positional arrays, so columns are resolved through the schema's
    property names rather than hard-coded indices: if BRAIN reorders or adds a
    column the mapping still lines up instead of silently shifting every value.

    Only in-sample rows are kept when a ``stage`` column is present, matching the
    aggregate ``is`` metrics the filters compare against. Which label counts as
    in-sample depends on ``testPeriod``: ``IS`` without one, ``TRAIN`` with one
    (the held-out year reads ``TEST``). Percent-typed columns arrive as decimal
    fractions (0.585 == 58.5%), the same convention as the aggregate block, so no
    unit conversion is needed.

    Returns ``{}`` for anything unparseable so yearly enrichment can never break
    a batch run.
    """
    if not isinstance(payload, dict):
        return {}

    schema = payload.get("schema")
    records = payload.get("records")
    if not isinstance(schema, dict) or not isinstance(records, list):
        return {}

    properties = schema.get("properties")
    if not isinstance(properties, list):
        return {}

    column_index: dict[str, int] = {}
    for position, prop in enumerate(properties):
        if isinstance(prop, dict) and isinstance(prop.get("name"), str):
            column_index[prop["name"]] = position

    if "year" not in column_index:
        return {}

    year_at = column_index["year"]
    stage_at = column_index.get("stage")
    metric_positions = {
        internal: column_index[remote]
        for remote, internal in _YEARLY_METRIC_COLUMNS.items()
        if remote in column_index
    }

    yearly: dict[str, dict[str, float | None]] = {}
    for record in records:
        if not isinstance(record, (list, tuple)) or len(record) <= year_at:
            continue

        if stage_at is not None and len(record) > stage_at:
            stage = record[stage_at]
            if isinstance(stage, str) and stage.strip().upper() not in _YEARLY_IN_SAMPLE_STAGES:
                continue

        raw_year = record[year_at]
        if raw_year is None:
            continue
        text = str(raw_year).strip()
        match = re.search(r"(\d{4})", text)
        year = match.group(1) if match else text
        if not year:
            continue

        metrics: dict[str, float | None] = {}
        for internal, position in metric_positions.items():
            value = record[position] if len(record) > position else None
            metrics[internal] = (
                _as_int(value)
                if internal in {"long_count", "short_count"}
                else _as_float(value)
            )
        yearly[year] = metrics

    return yearly


def is_captcha_payload(payload: Any) -> bool:
    """True when BRAIN demands an interactive challenge we must not bypass."""
    if not isinstance(payload, dict):
        return False
    if "inquiry" in payload:  # persona / biometric hand-off
        return True
    detail = payload.get("detail")
    if isinstance(detail, str) and re.search(r"captcha|persona|biometric", detail, re.I):
        return True
    return False


def is_credentials_payload(payload: Any) -> bool:
    """True when the body indicates bad/expired credentials."""
    if not isinstance(payload, dict):
        return False
    for key in ("detail", "message", "error"):
        value = payload.get(key)
        if isinstance(value, str) and re.search(r"credential|unauthorized|invalid.*password", value, re.I):
            return True
    return False
