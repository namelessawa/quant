"""Alpha input loading.

Supported inputs:
    * ``.txt`` — one expression per line; blank lines and full-line ``#`` or
      ``//`` comments ignored. Inline comments are deliberately NOT stripped:
      guessing where a comment starts could silently corrupt an expression.
    * ``.csv`` — ``name,expression`` plus optional per-row settings columns and
      optional ablation provenance columns (``source`` / ``ablation_group_id`` /
      ``changed_parameters`` / ``parent_experiment_id``)
    * ``.json`` — a list of expressions, or a list of ``{name, expression}``
      maps (the same settings/ablation keys are supported)

Rows are de-duplicated on ``expression + settings`` at load time, so a repeated
line in the input file cannot cause a second submission.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Iterable

from .api import DEFAULT_SETTINGS, normalize_settings
from .exceptions import ConfigError
from .hashing import auto_alpha_id, dedup_key
from .logging_utils import get_logger
from .models import AlphaSpec

_EXPRESSION_COLUMNS = ("expression", "code", "formula", "expr", "regular", "alpha")
_NAME_COLUMNS = ("name", "id", "alpha_id", "label")
#: Optional ablation provenance columns (CSV) / keys (JSON). Absent for normal
#: alpha inputs; see ``AlphaSpec`` for their semantics.
_SOURCE_COLUMN = "source"
_ABLATION_GROUP_COLUMNS = ("ablation_group_id", "ablation_group")
_PARENT_COLUMNS = ("parent_experiment_id", "parent_id")
_CHANGED_COLUMNS = ("changed_parameters", "changed_params", "ablation_changes")
#: CSV columns that override the global backtest settings for one row.
_SETTING_COLUMNS = frozenset(DEFAULT_SETTINGS) | {
    "instrument_type", "unit_handling", "nan_handling",
}


def _pick(row: dict[str, Any], candidates: Iterable[str]) -> str | None:
    for key in candidates:
        value = row.get(key)
        if value is None:
            lowered = {str(k).strip().lower(): v for k, v in row.items()}
            value = lowered.get(key.lower())
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _row_settings(row: dict[str, Any]) -> dict[str, Any]:
    """Extract per-row settings overrides, coercing numeric/boolean text."""
    overrides: dict[str, Any] = {}
    lowered = {str(key).strip().lower(): value for key, value in row.items()}
    for column in _SETTING_COLUMNS:
        raw = lowered.get(column.lower())
        if raw is None or not str(raw).strip():
            continue
        text = str(raw).strip()
        if column in {"delay", "decay"}:
            overrides[column] = int(float(text))
        elif column in {"truncation"}:
            overrides[column] = float(text)
        elif column in {"visualization"}:
            overrides[column] = text.lower() in {"1", "true", "yes", "on"}
        else:
            overrides[column] = text
    return overrides


def _raw_pick(row: dict[str, Any], candidates: Iterable[str]) -> Any:
    """Like :func:`_pick` but keeps non-string values (JSON dicts) intact."""
    for key in candidates:
        value = row.get(key)
        if value is None:
            lowered = {str(k).strip().lower(): v for k, v in row.items()}
            value = lowered.get(key.lower())
        if value is not None and (not isinstance(value, str) or value.strip()):
            return value
    return None


def _coerce_scalar(text: str) -> Any:
    """Parse one ``changed_parameters`` value (numbers/bools stay typed)."""
    token = text.strip()
    if token == "":
        return ""
    lowered = token.lower()
    if lowered in {"true", "yes", "on"}:
        return True
    if lowered in {"false", "no", "off"}:
        return False
    try:
        number = float(token)
    except ValueError:
        return token
    return int(number) if number.is_integer() else number


def _parse_changed_parameters(raw: Any) -> dict[str, Any] | None:
    """Accept a JSON object or ``key=value;key=value`` text; None when empty."""
    if raw is None:
        return None
    if isinstance(raw, dict):
        return dict(raw)
    text = str(raw).strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict):
        return dict(parsed)
    changes: dict[str, Any] = {}
    for piece in text.replace("\n", ";").split(";"):
        if not piece.strip() or "=" not in piece:
            continue
        key, _, value = piece.partition("=")
        key = key.strip()
        if key:
            changes[key] = _coerce_scalar(value)
    return changes or None


def _ablation_metadata(row: dict[str, Any]) -> dict[str, Any]:
    """Extract optional ablation provenance fields from a CSV/JSON row.

    Returns keyword arguments for ``AlphaSpec`` (omitted when not supplied).
    """
    meta: dict[str, Any] = {}
    source = _pick(row, [_SOURCE_COLUMN])
    if source:
        meta["source"] = source
    group = _pick(row, _ABLATION_GROUP_COLUMNS)
    if group:
        meta["ablation_group_id"] = group
    changed_raw = _raw_pick(row, _CHANGED_COLUMNS)
    changes = _parse_changed_parameters(changed_raw)
    if changes:
        meta["changed_parameters"] = changes
    parent_raw = _raw_pick(row, _PARENT_COLUMNS)
    if parent_raw:
        try:
            meta["parent_experiment_id"] = int(float(parent_raw))
        except ValueError:
            raise ConfigError(
                f"parent_experiment_id must be an integer, got {parent_raw!r}"
            ) from None
    return meta


def _from_csv(path: Path) -> list[AlphaSpec]:
    # utf-8-sig strips the BOM Excel writes on Windows.
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise ConfigError(f"{path} is empty or has no header row")

        header = [str(name).strip().lower() for name in reader.fieldnames]
        expression_column = next((c for c in _EXPRESSION_COLUMNS if c in header), None)
        if expression_column is None:
            if len(header) == 1:
                expression_column = header[0]
            else:
                raise ConfigError(
                    f"{path} has no expression column; expected one of "
                    f"{list(_EXPRESSION_COLUMNS)}, found {header}"
                )

        specs: list[AlphaSpec] = []
        for line_number, row in enumerate(reader, start=2):
            if row is None:
                continue
            cleaned = {str(k).strip().lower(): v for k, v in row.items() if k is not None}
            expression = cleaned.get(expression_column)
            if expression is None or not str(expression).strip():
                continue
            expression = str(expression).strip()
            name = _pick(cleaned, _NAME_COLUMNS)
            specs.append(
                AlphaSpec(
                    expression=expression,
                    name=name,
                    settings=_row_settings(cleaned),
                    **_ablation_metadata(cleaned),
                )
            )
        if not specs:
            raise ConfigError(f"{path} contained no usable alpha rows")
        return specs


def _from_text(path: Path) -> list[AlphaSpec]:
    specs: list[AlphaSpec] = []
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith("//"):
            continue
        specs.append(AlphaSpec(expression=line))
    if not specs:
        raise ConfigError(f"{path} contained no expressions")
    return specs


def _from_json(path: Path) -> list[AlphaSpec]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path} is not valid JSON: {exc}") from exc

    if isinstance(payload, dict):
        payload = payload.get("alphas") or payload.get("results") or []
    if not isinstance(payload, list):
        raise ConfigError(f"{path} must contain a JSON list of alphas")

    specs: list[AlphaSpec] = []
    for item in payload:
        if isinstance(item, str):
            if item.strip():
                specs.append(AlphaSpec(expression=item.strip()))
        elif isinstance(item, dict):
            expression = _pick(item, _EXPRESSION_COLUMNS)
            if not expression:
                continue
            settings = item.get("settings")
            specs.append(
                AlphaSpec(
                    expression=expression,
                    name=_pick(item, _NAME_COLUMNS),
                    settings=dict(settings) if isinstance(settings, dict) else {},
                    **_ablation_metadata(item),
                )
            )
        else:
            raise ConfigError(
                f"{path} contains an unsupported entry type: {type(item).__name__}"
            )
    if not specs:
        raise ConfigError(f"{path} contained no usable alphas")
    return specs


def load_alphas(
    path: str | Path,
    *,
    default_settings: dict[str, Any] | None = None,
    logger: Any = None,
) -> list[AlphaSpec]:
    """Read alpha specs from a txt/csv/json file.

    Names are generated when absent, and duplicates (same expression under the
    same effective settings) are dropped with a warning.
    """
    log = logger or get_logger("loader")
    file_path = Path(path)
    if not file_path.exists():
        raise ConfigError(f"alpha input file not found: {file_path}")

    suffix = file_path.suffix.lower()
    if suffix == ".csv":
        specs = _from_csv(file_path)
    elif suffix in {".txt", ".list", ""}:
        specs = _from_text(file_path)
    elif suffix == ".json":
        specs = _from_json(file_path)
    else:
        raise ConfigError(
            f"unsupported alpha input format {suffix or '(none)'} for {file_path}; "
            "use .txt, .csv or .json"
        )

    merged_settings = normalize_settings(default_settings)
    unique: list[AlphaSpec] = []
    seen: set[str] = set()
    duplicates = 0

    for spec in specs:
        effective = dict(merged_settings)
        effective.update(normalize_settings(spec.settings))
        key = dedup_key(spec.expression, effective)
        if key in seen:
            duplicates += 1
            continue
        seen.add(key)
        if not spec.name:
            spec.name = auto_alpha_id(spec.expression, effective)
        spec.settings = effective
        unique.append(spec)

    if duplicates:
        log.warning(
            "skipped %d duplicate expression(s) already present in %s", duplicates, file_path
        )
    log.info("loaded %d alpha(s) from %s", len(unique), file_path)
    return unique


def specs_from_expressions(
    expressions: Iterable[str],
    *,
    settings: dict[str, Any] | None = None,
) -> list[AlphaSpec]:
    """Build specs from raw expression strings (used by ``--expression``)."""
    merged = normalize_settings(settings)
    specs: list[AlphaSpec] = []
    for expression in expressions:
        text = expression.strip()
        if not text:
            continue
        specs.append(
            AlphaSpec(
                expression=text,
                name=auto_alpha_id(text, merged),
                settings=dict(merged),
            )
        )
    return specs
