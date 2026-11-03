"""Named, explicitly authored timeline positions and exact reference resolution."""

import re
from fractions import Fraction

from .project import Project, WorkbenchError, read_csv

ID_PATTERN = r"[A-Za-z_][A-Za-z0-9_]*"
REFERENCE = re.compile(rf"marker:({ID_PATTERN})(?:([+-])([0-9]+)(ms|f))?")


def marker_seconds(row: dict, project: Project) -> Fraction:
    """Read one unambiguous marker without quantizing an output-frame index."""
    if row.get("at_frame") not in (None, "") and row.get("time_ms") not in (None, ""):
        raise WorkbenchError("markers.csv: cannot mix time_ms and at_frame")
    try:
        if row.get("at_frame") not in (None, ""):
            value = int(row["at_frame"])
            result = Fraction(value, 1) / project.fps
        else:
            value = int(row.get("time_ms", ""))
            result = Fraction(value, 1000)
        if value < 0:
            raise ValueError("negative timestamp")
        return result
    except ValueError as exc:
        raise WorkbenchError(f"Invalid marker {row}: {exc}") from exc


def named_markers(project: Project, rows=None) -> dict[str, Fraction]:
    if rows is None:
        rows = read_csv(project.path("markers.csv"), ["kind", "label", "origin"])
    result = {}
    seen = set()
    for number, row in enumerate(rows, 2):
        identifier = row.get("id", "")
        if not identifier:
            continue
        if not re.fullmatch(ID_PATTERN, identifier):
            raise WorkbenchError(
                f"markers.csv row {number}: invalid marker id {identifier!r}"
            )
        if identifier in seen:
            raise WorkbenchError(
                f"markers.csv row {number}: duplicate marker id {identifier!r}"
            )
        seen.add(identifier)
        position = marker_seconds(row, project)
        if row["origin"] == "manual":
            result[identifier] = position
    return result


def resolve_reference(
    value: str, *, fps: Fraction, markers: dict[str, Fraction]
) -> int:
    match = REFERENCE.fullmatch(value)
    if not match:
        raise WorkbenchError(
            f"Malformed marker reference {value!r}; expected marker:id with optional +/- integer ms or f offset"
        )
    identifier, sign, amount, unit = match.groups()
    if identifier not in markers:
        raise WorkbenchError(f"Unknown manual marker id {identifier!r}")
    position = markers[identifier]
    if amount is not None:
        offset = (
            Fraction(int(amount), 1000)
            if unit == "ms"
            else Fraction(int(amount), 1) / fps
        )
        position += offset if sign == "+" else -offset
    if position < 0:
        raise WorkbenchError(f"Marker reference {value!r} resolves to a negative time")
    # Add offsets before quantization, including for frame-authored markers.
    return round(position * fps)
