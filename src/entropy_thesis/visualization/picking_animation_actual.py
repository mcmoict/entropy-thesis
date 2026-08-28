from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date
import json
import math
from pathlib import Path
import re
import shutil
import time
from typing import Any, Callable, Iterable
import xml.etree.ElementTree as ET

import numpy as np

from ..simulation.data_loader import (
    DEFAULT_COORDINATE_UNIT,
    coordinate_scale_to_meter,
    load_dataset,
    load_support_points,
)
from ..simulation.phase2 import (
    available_phase2_dates,
    calculate_demand_entropy,
    select_phase2_lists,
)
from ..simulation.phase3 import (
    allocate_phase3_workers,
    build_aisle_zones,
    classify_picking_lists_by_zone,
    macro_zone_demand_profiles,
    run_phase3_method,
    run_phase3_observed_baseline,
    zone_workload,
)
from ..simulation.phase4 import (
    DEFAULT_MIN_LISTS_PER_DATE,
    allocate_phase4_workers,
)
from ..simulation.warehouse import WarehouseGraph


METHODS: tuple[str, ...] = ("observed", "equal", "random", "volume", "entropy")
COMPARISON_METHODS: tuple[str, ...] = ("equal", "random", "volume", "entropy")
METHOD_LABELS: dict[str, str] = {
    "observed": "Observed",
    "equal": "Equal",
    "random": "Random",
    "volume": "Volume",
    "entropy": "Entropy",
}
PHASE3_METHOD_NAMES: dict[str, str] = {
    "equal": "equal",
    "random": "random",
    "volume": "volume_proportional",
}
DEFAULT_ENTROPY_LAMBDA = 0.25


@dataclass(frozen=True)
class SvgAxesTransform:
    """Axis-aligned affine transform from warehouse raw CAD coordinates to SVG."""

    svg_markup: str
    view_box: str
    x_scale: float
    x_offset: float
    y_scale: float
    y_offset: float
    calibration_points: int
    max_residual_px: float
    zone_rectangles: tuple[dict[str, Any], ...]

    def raw_to_svg(self, raw_x: float, raw_y: float) -> tuple[float, float]:
        return (
            float(self.x_scale * raw_x + self.x_offset),
            float(self.y_scale * raw_y + self.y_offset),
        )


def _strip_xml_declaration(svg_text: str) -> str:
    return re.sub(r"^<\?xml[^>]*>\s*", "", svg_text, count=1)


def _fit_linear(raw_values: Iterable[float], svg_values: Iterable[float]) -> tuple[float, float, float]:
    raw = np.asarray(tuple(raw_values), dtype=float)
    svg = np.asarray(tuple(svg_values), dtype=float)
    if raw.size < 2 or svg.size != raw.size:
        raise ValueError("SVG 좌표 보정에 사용할 점이 부족합니다.")

    design = np.column_stack([raw, np.ones(raw.size, dtype=float)])
    coef, *_ = np.linalg.lstsq(design, svg, rcond=None)
    scale, offset = float(coef[0]), float(coef[1])
    predicted = scale * raw + offset
    residual = float(np.max(np.abs(predicted - svg)))
    return scale, offset, residual


def _extract_svg_support_markers(root: ET.Element) -> list[tuple[float, float]]:
    """Extract the 44 gray support-point marker coordinates from Layout_Z1.0.svg.

    The supplied SVG is a Matplotlib export. Support points are rendered as gray
    <use> markers with explicit x/y values. Using those plotted marker positions
    allows the animation coordinates to be calibrated against the drawing itself,
    rather than stretching data min/max values to the axes rectangle.
    """

    markers: list[tuple[float, float]] = []
    for element in root.iter():
        if not element.tag.endswith("use"):
            continue
        x = element.attrib.get("x")
        y = element.attrib.get("y")
        if x is None or y is None:
            continue
        style = element.attrib.get("style", "").lower().replace(" ", "")
        if "#d3d3d3" not in style:
            continue
        markers.append((float(x), float(y)))
    return markers


def _support_point_code(point: Any) -> str | None:
    """Best-effort extraction of a support-point code such as LC-08 / CC-08."""

    candidates: list[Any] = []
    for name in (
        "point_id", "support_point_id", "support_id", "location_id",
        "code", "name", "id", "point", "location",
    ):
        try:
            value = getattr(point, name)
        except Exception:
            continue
        if value is not None:
            candidates.append(value)

    # Dataclass / namedtuple implementations can vary between dataset revisions.
    # Also inspect public attribute values and finally repr/str as a safe fallback.
    try:
        values = vars(point)
    except TypeError:
        values = {}
    if isinstance(values, dict):
        candidates.extend(values.values())
    candidates.extend((str(point), repr(point)))

    for value in candidates:
        match = re.search(r"\b(?:LC|RC|CC)-\d{2}\b", str(value), flags=re.IGNORECASE)
        if match:
            return match.group(0).upper()
    return None


def _build_macro_zone_rectangles(
    *,
    support_points: Iterable[Any],
    svg_markers: Iterable[tuple[float, float]],
) -> tuple[dict[str, Any], ...]:
    """Build Z01~Z04 rectangles from the model's LC/RC 08~17 anchors.

    Macro-zone definition used by Phase 3/4:
      Z01 = LC-08~LC-12 (Left / Near)
      Z02 = LC-13~LC-17 (Left / Far)
      Z03 = RC-08~RC-12 (Right / Near)
      Z04 = RC-13~RC-17 (Right / Far)

    The left/right split follows CC-08. The near/far boundary is the midpoint
    between the 12 and 13 anchor rows. Outer Y boundaries are extrapolated by
    half an anchor interval so the rectangles cover the full 08~17 zone cells.
    """

    supports = tuple(support_points)
    markers = tuple(svg_markers)
    if len(supports) != len(markers):
        return ()

    by_code: dict[str, tuple[float, float]] = {}
    for point, marker in zip(supports, markers):
        code = _support_point_code(point)
        if code:
            by_code[code] = (float(marker[0]), float(marker[1]))

    required = [
        *(f"LC-{i:02d}" for i in range(8, 18)),
        *(f"RC-{i:02d}" for i in range(8, 18)),
        "CC-08",
    ]
    missing = [code for code in required if code not in by_code]
    if missing:
        print(
            "[ZONE ] Macro-zone rectangles unavailable; support ids not resolved: "
            + ", ".join(missing[:8])
            + (" ..." if len(missing) > 8 else "")
        )
        return ()

    lc = [by_code[f"LC-{i:02d}"] for i in range(8, 18)]
    rc = [by_code[f"RC-{i:02d}"] for i in range(8, 18)]

    # The SVG can have an inverted Y axis, so derive boundaries numerically from
    # marker positions instead of assuming that larger raw Y means lower screen Y.
    row_y = [0.5 * (lc[i][1] + rc[i][1]) for i in range(10)]
    near_mid_y = 0.5 * (row_y[4] + row_y[5])  # between 12 and 13
    first_step = row_y[1] - row_y[0]
    last_step = row_y[-1] - row_y[-2]
    outer_08_y = row_y[0] - first_step / 2.0
    outer_17_y = row_y[-1] + last_step / 2.0

    # CC-08 is the model's left/right discriminator. Use the support-marker cloud
    # for the warehouse-visible horizontal extent, with a small half-spacing pad.
    split_x = by_code["CC-08"][0]
    all_x = sorted(float(x) for x, _ in markers)
    x_min = all_x[0]
    x_max = all_x[-1]
    if len(all_x) >= 2:
        diffs = [b - a for a, b in zip(all_x, all_x[1:]) if b - a > 1e-6]
        x_pad = (min(diffs) / 2.0) if diffs else 0.0
    else:
        x_pad = 0.0
    left_x = x_min - x_pad
    right_x = x_max + x_pad

    def rect(
        zone_id: str,
        label: str,
        x0: float,
        x1: float,
        y0: float,
        y1: float,
    ) -> dict[str, Any]:
        left, right = sorted((float(x0), float(x1)))
        top, bottom = sorted((float(y0), float(y1)))
        return {
            "zone_id": zone_id,
            "label": label,
            "x": round(left, 3),
            "y": round(top, 3),
            "width": round(right - left, 3),
            "height": round(bottom - top, 3),
        }

    # Near/Far is determined by support anchor numbers, not by screen direction.
    near_y0, near_y1 = outer_08_y, near_mid_y
    far_y0, far_y1 = near_mid_y, outer_17_y
    zones = (
        rect("Z01", "Z01 · Left / Near", left_x, split_x, near_y0, near_y1),
        rect("Z02", "Z02 · Left / Far", left_x, split_x, far_y0, far_y1),
        rect("Z03", "Z03 · Right / Near", split_x, right_x, near_y0, near_y1),
        rect("Z04", "Z04 · Right / Far", split_x, right_x, far_y0, far_y1),
    )
    print(
        "[ZONE ] Macro-zone rectangles calibrated | "
        + " | ".join(
            f"{item['zone_id']}=({item['x']:.1f},{item['y']:.1f},"
            f"{item['width']:.1f}x{item['height']:.1f})"
            for item in zones
        )
    )
    return zones


def parse_svg_axes_transform(
    svg_path: str | Path,
    *,
    support_points: Iterable[Any],
) -> SvgAxesTransform:
    """Calibrate raw warehouse coordinates from support markers embedded in SVG.

    This deliberately does *not* use min/max warehouse coordinates. The previous
    min/max mapping incorrectly placed LC workers on the left plot border. Here all
    support points in Support_Points_Navigation.csv are paired with the actual gray
    support markers drawn in Layout_Z1.0.svg, and two least-squares affine fits are
    estimated:

        svg_x = a_x * raw_x + b_x
        svg_y = a_y * raw_y + b_y

    The current dataset/SVG pair yields essentially zero residual and therefore
    reproduces the actual plotted support-point positions.
    """

    svg_path = Path(svg_path)
    raw_svg_text = svg_path.read_text(encoding="utf-8")
    svg_text = _strip_xml_declaration(raw_svg_text)

    view_box_match = re.search(r'viewBox\s*=\s*"([^"]+)"', raw_svg_text)
    if not view_box_match:
        raise ValueError(f"SVG에서 viewBox를 찾지 못했습니다: {svg_path}")

    try:
        root = ET.fromstring(raw_svg_text)
    except ET.ParseError as exc:
        raise ValueError(f"SVG XML 파싱에 실패했습니다: {svg_path}") from exc

    supports = tuple(support_points)
    markers = _extract_svg_support_markers(root)
    if len(markers) != len(supports):
        raise ValueError(
            "SVG support marker 수와 Support_Points_Navigation.csv 행 수가 다릅니다. "
            f"svg_markers={len(markers)}, support_points={len(supports)}. "
            "Layout_Z1.0.svg가 현재 데이터셋과 동일한 버전인지 확인해 주세요."
        )

    raw_x = [float(point.raw_x) for point in supports]
    raw_y = [float(point.raw_y) for point in supports]
    svg_x = [point[0] for point in markers]
    svg_y = [point[1] for point in markers]

    x_scale, x_offset, x_residual = _fit_linear(raw_x, svg_x)
    y_scale, y_offset, y_residual = _fit_linear(raw_y, svg_y)
    max_residual = max(x_residual, y_residual)
    zone_rectangles = _build_macro_zone_rectangles(
        support_points=supports,
        svg_markers=markers,
    )

    # If point ordering no longer matches the supplied SVG, the regression error
    # immediately exposes it instead of silently producing a misleading animation.
    if max_residual > 0.75:
        raise ValueError(
            "SVG support-point 자동 보정 오차가 너무 큽니다. "
            f"max_residual={max_residual:.3f}px. "
            "SVG와 Support_Points_Navigation.csv의 버전/순서를 확인해 주세요."
        )

    print(
        "[SVG  ] Auto-calibrated from support markers | "
        f"points={len(supports)}, "
        f"x={x_scale:.9f}*raw+{x_offset:.6f}, "
        f"y={y_scale:.9f}*raw+{y_offset:.6f}, "
        f"max_residual={max_residual:.6f}px"
    )

    return SvgAxesTransform(
        svg_markup=svg_text,
        view_box=view_box_match.group(1),
        x_scale=x_scale,
        x_offset=x_offset,
        y_scale=y_scale,
        y_offset=y_offset,
        calibration_points=len(supports),
        max_residual_px=max_residual,
        zone_rectangles=zone_rectangles,
    )


def _node_raw_coordinates(warehouse: WarehouseGraph) -> dict[str, tuple[float, float]]:
    """Use original CAD coordinates where available, otherwise recover from meters."""

    scale = coordinate_scale_to_meter(DEFAULT_COORDINATE_UNIT)
    raw_by_node: dict[str, tuple[float, float]] = {}

    for point in warehouse.support_nodes:
        node_id = warehouse.support_nodes[point]
        attrs = warehouse.graph.nodes[node_id]
        # Support nodes are built from SupportPoint and retain only meter values in
        # the graph, so recover raw coordinates from the globally consistent ratio.
        raw_by_node[node_id] = (float(attrs["x_m"]) / scale, float(attrs["y_m"]) / scale)

    for node_id, attrs in warehouse.graph.nodes(data=True):
        if node_id in raw_by_node:
            continue
        raw_by_node[node_id] = (float(attrs["x_m"]) / scale, float(attrs["y_m"]) / scale)
    return raw_by_node


def _timeline_for_worker(
    worker: Any,
    *,
    node_raw: dict[str, tuple[float, float]],
    simulation_end_seconds: float,
    default_start_node: str,
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []

    for event in worker.movement_events:
        x0, y0 = node_raw[event.from_node]
        x1, y1 = node_raw[event.to_node]
        events.append(
            {
                "kind": "move",
                "t0": float(event.started_at),
                "t1": float(event.finished_at),
                "x0": x0,
                "y0": y0,
                "x1": x1,
                "y1": y1,
                "wave_number": event.wave_number,
                "from_node": event.from_node,
                "to_node": event.to_node,
            }
        )

    for event in worker.pick_events:
        x, y = node_raw[event.node_id]
        events.append(
            {
                "kind": "pick",
                "t0": float(event.started_at),
                "t1": float(event.finished_at),
                "x0": x,
                "y0": y,
                "x1": x,
                "y1": y,
                "wave_number": event.wave_number,
                "location_id": event.location_id,
                "reference": event.reference,
                "quantity_units": float(event.quantity_units),
                "node_id": event.node_id,
            }
        )

    events.sort(key=lambda item: (item["t0"], 0 if item["kind"] == "move" else 1, item["t1"]))

    start_x, start_y = node_raw[default_start_node]
    current_time = 0.0
    current_x = start_x
    current_y = start_y
    segments: list[dict[str, Any]] = []

    for event in events:
        if event["t0"] > current_time:
            segments.append(
                {
                    "kind": "idle",
                    "t0": current_time,
                    "t1": event["t0"],
                    "x0": current_x,
                    "y0": current_y,
                    "x1": current_x,
                    "y1": current_y,
                    "wave_number": None,
                }
            )
        segments.append(event)
        current_time = max(current_time, float(event["t1"]))
        current_x = float(event["x1"])
        current_y = float(event["y1"])

    if current_time < simulation_end_seconds:
        segments.append(
            {
                "kind": "idle",
                "t0": current_time,
                "t1": float(simulation_end_seconds),
                "x0": current_x,
                "y0": current_y,
                "x1": current_x,
                "y1": current_y,
                "wave_number": None,
            }
        )

    if not segments:
        segments.append(
            {
                "kind": "idle",
                "t0": 0.0,
                "t1": float(simulation_end_seconds),
                "x0": start_x,
                "y0": start_y,
                "x1": start_x,
                "y1": start_y,
                "wave_number": None,
            }
        )
    return segments



# ---------------------------------------------------------------------------
# Actual DES congestion-conflict extraction
# ---------------------------------------------------------------------------

_CONFLICT_COLLECTION_ATTRS: tuple[str, ...] = (
    "congestion_events",
    "conflict_events",
    "resource_wait_events",
    "contention_events",
    "wait_events",
)
_WAIT_SECONDS_ATTRS: tuple[str, ...] = (
    "congestion_wait_seconds",
    "resource_wait_seconds",
    "wait_seconds",
    "waiting_seconds",
    "queue_wait_seconds",
    "queued_seconds",
)
_WAIT_START_ATTRS: tuple[str, ...] = (
    "wait_started_at",
    "waiting_started_at",
    "queued_at",
    "requested_at",
    "request_time",
)
_WAIT_END_ATTRS: tuple[str, ...] = (
    "wait_finished_at",
    "waiting_finished_at",
    "entered_at",
    "acquired_at",
    "resource_acquired_at",
    "granted_at",
)
_WORKER_ID_ATTRS: tuple[str, ...] = (
    "worker_id",
    "operator_id",
    "operator",
    "worker",
)
_BLOCKING_WORKER_ATTRS: tuple[str, ...] = (
    "blocking_worker_id",
    "blocker_worker_id",
    "owner_worker_id",
    "other_worker_id",
)


def _public_attrs(obj: Any) -> dict[str, Any]:
    if obj is None:
        return {}
    try:
        values = vars(obj)
    except TypeError:
        values = {}
    if isinstance(values, dict) and values:
        return {str(k): v for k, v in values.items() if not str(k).startswith("_")}

    result: dict[str, Any] = {}
    for name in dir(obj):
        if name.startswith("_"):
            continue
        try:
            value = getattr(obj, name)
        except Exception:
            continue
        if callable(value):
            continue
        result[name] = value
    return result


def _first_attr(obj: Any, names: Iterable[str]) -> Any:
    for name in names:
        try:
            value = getattr(obj, name)
        except Exception:
            continue
        if value is not None:
            return value
    return None


def _finite_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _numeric_wait_attr(obj: Any) -> tuple[str | None, float | None]:
    for name in _WAIT_SECONDS_ATTRS:
        value = _finite_float(_first_attr(obj, (name,)))
        if value is not None:
            return name, value

    for name, raw in _public_attrs(obj).items():
        lower = name.lower()
        if "wait" not in lower or "second" not in lower:
            continue
        value = _finite_float(raw)
        if value is not None:
            return name, value
    return None, None


def _event_worker_ids(event: Any, default_worker_id: str | None) -> list[str]:
    worker_ids: list[str] = []
    if default_worker_id:
        worker_ids.append(str(default_worker_id))

    primary = _first_attr(event, _WORKER_ID_ATTRS)
    if primary is not None and not isinstance(primary, (list, tuple, set, dict)):
        value = str(primary)
        if value and value not in worker_ids:
            worker_ids.append(value)

    many = _first_attr(event, ("worker_ids", "operators", "participants"))
    if isinstance(many, (list, tuple, set)):
        for raw in many:
            value = str(raw)
            if value and value not in worker_ids:
                worker_ids.append(value)

    blocker = _first_attr(event, _BLOCKING_WORKER_ATTRS)
    if blocker is not None:
        value = str(blocker)
        if value and value not in worker_ids:
            worker_ids.append(value)
    return worker_ids


def _event_resource_info(
    event: Any,
    *,
    event_kind: str | None,
) -> tuple[str, str | None, str | None]:
    resource_type = str(
        _first_attr(event, ("resource_kind", "resource_type", "contention_type", "kind", "type"))
        or ("edge" if event_kind == "move" else "pick_node" if event_kind == "pick" else "resource")
    )

    from_node = _first_attr(event, ("from_node", "source_node"))
    to_node = _first_attr(event, ("to_node", "target_node"))
    node_id = _first_attr(event, ("node_id", "resource_node", "location_node"))

    if event_kind == "move" or (from_node is not None and to_node is not None):
        resource_id = _first_attr(event, ("resource_id", "edge_id"))
        if resource_id is None and from_node is not None and to_node is not None:
            resource_id = f"{from_node}->{to_node}"
        return (
            resource_type,
            str(resource_id) if resource_id is not None else None,
            str(from_node) if from_node is not None else None,
        )

    resource_id = _first_attr(event, ("resource_id", "pick_node_id", "location_id"))
    if resource_id is None:
        resource_id = node_id
    return (
        resource_type,
        str(resource_id) if resource_id is not None else None,
        str(node_id) if node_id is not None else None,
    )


def _serialize_conflict_event(
    event: Any,
    *,
    default_worker_id: str | None,
    event_kind: str | None,
    node_raw: dict[str, tuple[float, float]],
    svg_transform: SvgAxesTransform,
) -> dict[str, Any] | None:
    wait_attr, wait_seconds = _numeric_wait_attr(event)
    t0 = _finite_float(_first_attr(event, _WAIT_START_ATTRS))
    t1 = _finite_float(_first_attr(event, _WAIT_END_ATTRS))

    if wait_seconds is None and t0 is not None and t1 is not None:
        wait_seconds = max(0.0, t1 - t0)

    event_started = _finite_float(_first_attr(event, ("started_at", "start_time", "started")))
    if wait_seconds is not None and wait_seconds > 1e-9:
        if t1 is None and event_started is not None:
            t1 = event_started
        if t0 is None and t1 is not None:
            t0 = t1 - wait_seconds

    if wait_seconds is None or wait_seconds <= 1e-9 or t0 is None or t1 is None:
        return None

    worker_ids = _event_worker_ids(event, default_worker_id)
    if not worker_ids:
        return None

    resource_type, resource_id, location_node = _event_resource_info(
        event,
        event_kind=event_kind,
    )

    sx = sy = None
    if location_node is not None and location_node in node_raw:
        raw_x, raw_y = node_raw[location_node]
        sx, sy = svg_transform.raw_to_svg(raw_x, raw_y)

    return {
        "t0": round(float(t0), 3),
        "t1": round(float(t1), 3),
        "wait_seconds": round(float(wait_seconds), 3),
        "worker_ids": worker_ids,
        "resource_type": resource_type,
        "resource_id": resource_id,
        "location_node": location_node,
        "sx": round(float(sx), 3) if sx is not None else None,
        "sy": round(float(sy), 3) if sy is not None else None,
        "wait_attr": wait_attr,
    }


def _extract_actual_conflict_events(
    *,
    simulation: Any,
    warehouse: WarehouseGraph,
    svg_transform: SvgAxesTransform,
) -> list[dict[str, Any]]:
    """Extract DES contention waits and reconcile them to congestion_conflicts.

    No distance-based approximation is permitted here. If the simulator's
    event-level records cannot be reconciled to summary.congestion_conflicts,
    generation fails with diagnostics instead of producing a misleading figure.
    """

    summary = getattr(simulation, "summary", None)
    expected_raw = _finite_float(_first_attr(summary, ("congestion_conflicts",)))
    expected = int(round(expected_raw or 0.0))
    node_raw = _node_raw_coordinates(warehouse)

    if expected <= 0:
        return []

    candidates: list[tuple[str, list[dict[str, Any]]]] = []

    for attr_name in _CONFLICT_COLLECTION_ATTRS:
        collection = _first_attr(simulation, (attr_name,))
        if not isinstance(collection, (list, tuple)):
            continue
        serialized: list[dict[str, Any]] = []
        for event in collection:
            item = _serialize_conflict_event(
                event,
                default_worker_id=None,
                event_kind=None,
                node_raw=node_raw,
                svg_transform=svg_transform,
            )
            if item is not None:
                serialized.append(item)
        if serialized:
            candidates.append((f"simulation.{attr_name}", serialized))

    workers = getattr(simulation, "workers", {})
    if isinstance(workers, dict):
        for attr_name in _CONFLICT_COLLECTION_ATTRS:
            serialized = []
            for worker_id, worker in workers.items():
                collection = _first_attr(worker, (attr_name,))
                if not isinstance(collection, (list, tuple)):
                    continue
                for event in collection:
                    item = _serialize_conflict_event(
                        event,
                        default_worker_id=str(worker_id),
                        event_kind=None,
                        node_raw=node_raw,
                        svg_transform=svg_transform,
                    )
                    if item is not None:
                        serialized.append(item)
            if serialized:
                candidates.append((f"worker.{attr_name}", serialized))

        serialized = []
        for worker_id, worker in workers.items():
            for event in getattr(worker, "movement_events", ()):
                item = _serialize_conflict_event(
                    event,
                    default_worker_id=str(worker_id),
                    event_kind="move",
                    node_raw=node_raw,
                    svg_transform=svg_transform,
                )
                if item is not None:
                    serialized.append(item)
            for event in getattr(worker, "pick_events", ()):
                item = _serialize_conflict_event(
                    event,
                    default_worker_id=str(worker_id),
                    event_kind="pick",
                    node_raw=node_raw,
                    svg_transform=svg_transform,
                )
                if item is not None:
                    serialized.append(item)
        if serialized:
            candidates.append(("worker movement/pick wait fields", serialized))

    for source_name, events in candidates:
        if len(events) == expected:
            events.sort(key=lambda item: (item["t0"], item["t1"], item["worker_ids"]))
            print(
                "[CONFLICT] Exact DES events extracted | "
                f"source={source_name} | conflicts={len(events)}"
            )
            return events

    candidate_counts = ", ".join(
        f"{name}={len(events)}" for name, events in candidates
    ) or "none"

    samples: list[str] = []
    if isinstance(workers, dict):
        for worker in workers.values():
            movement = getattr(worker, "movement_events", ())
            pick = getattr(worker, "pick_events", ())
            if movement:
                samples.append(
                    "movement_event attrs=" + ",".join(sorted(_public_attrs(movement[0]).keys()))
                )
                break
            if pick:
                samples.append(
                    "pick_event attrs=" + ",".join(sorted(_public_attrs(pick[0]).keys()))
                )
                break

    raise RuntimeError(
        "논문의 congestion_conflicts와 1:1로 대응되는 DES 이벤트를 추출하지 못했습니다. "
        f"summary.congestion_conflicts={expected}, candidates=[{candidate_counts}]. "
        "거리 기반 근사로 대체하지 않습니다. "
        + ("; ".join(samples) if samples else "")
    )


def _build_pick_targets(
    *,
    workers: dict[str, Any],
    node_raw: dict[str, tuple[float, float]],
    svg_transform: SvgAxesTransform,
) -> list[dict[str, Any]]:
    """Aggregate the day's actual pick-event destinations into SVG target points.

    The marker is keyed by simulation node so repeated picks at the same physical
    point are represented once, with visit count / quantity retained for tooltip
    and label display in the browser.
    """

    targets: dict[str, dict[str, Any]] = {}
    for worker_id in sorted(workers):
        worker = workers[worker_id]
        for event in getattr(worker, "pick_events", ()):
            node_id = str(getattr(event, "node_id", "") or "")
            if not node_id or node_id not in node_raw:
                continue

            raw_x, raw_y = node_raw[node_id]
            sx, sy = svg_transform.raw_to_svg(raw_x, raw_y)
            location_id = getattr(event, "location_id", None)
            reference = getattr(event, "reference", None)
            quantity_units = _finite_float(getattr(event, "quantity_units", None)) or 0.0
            started_at = _finite_float(getattr(event, "started_at", None))
            finished_at = _finite_float(getattr(event, "finished_at", None))

            item = targets.setdefault(
                node_id,
                {
                    "node_id": node_id,
                    "sx": round(float(sx), 3),
                    "sy": round(float(sy), 3),
                    "pick_events": 0,
                    "quantity_units": 0.0,
                    "worker_ids": set(),
                    "location_ids": set(),
                    "references": set(),
                    "first_pick_at": None,
                    "last_pick_at": None,
                },
            )
            item["pick_events"] += 1
            item["quantity_units"] += float(quantity_units)
            item["worker_ids"].add(str(worker_id))
            if location_id is not None:
                item["location_ids"].add(str(location_id))
            if reference is not None:
                item["references"].add(str(reference))
            if started_at is not None:
                current = item["first_pick_at"]
                item["first_pick_at"] = started_at if current is None else min(current, started_at)
            if finished_at is not None:
                current = item["last_pick_at"]
                item["last_pick_at"] = finished_at if current is None else max(current, finished_at)

    result: list[dict[str, Any]] = []
    for item in targets.values():
        result.append(
            {
                "node_id": item["node_id"],
                "sx": item["sx"],
                "sy": item["sy"],
                "pick_events": int(item["pick_events"]),
                "quantity_units": round(float(item["quantity_units"]), 3),
                "worker_ids": sorted(item["worker_ids"]),
                "location_ids": sorted(item["location_ids"]),
                "references": sorted(item["references"]),
                "first_pick_at": round(float(item["first_pick_at"]), 3) if item["first_pick_at"] is not None else None,
                "last_pick_at": round(float(item["last_pick_at"]), 3) if item["last_pick_at"] is not None else None,
            }
        )

    result.sort(key=lambda item: (item["sy"], item["sx"], item["node_id"]))
    return result


def build_animation_payload(
    *,
    warehouse: WarehouseGraph,
    simulation: Any,
    workers: dict[str, Any],
    selected_lists: list[Any],
    selected_date: date,
    simulation_end_seconds: float,
    svg_transform: SvgAxesTransform,
    method: str,
    worker_counts: tuple[int, ...] | None = None,
    entropy_lambda: float | None = None,
) -> dict[str, Any]:
    node_raw = _node_raw_coordinates(warehouse)
    default_start_node = warehouse.default_start_node()
    conflict_events = _extract_actual_conflict_events(
        simulation=simulation,
        warehouse=warehouse,
        svg_transform=svg_transform,
    )
    summary = getattr(simulation, "summary", None)
    congestion_conflicts = int(round(float(getattr(summary, "congestion_conflicts", 0) or 0)))
    congestion_wait_seconds = float(getattr(summary, "congestion_wait_seconds", 0.0) or 0.0)
    pick_targets = _build_pick_targets(
        workers=workers,
        node_raw=node_raw,
        svg_transform=svg_transform,
    )

    workers_payload: list[dict[str, Any]] = []
    for worker_id in sorted(workers):
        worker = workers[worker_id]
        segments = _timeline_for_worker(
            worker,
            node_raw=node_raw,
            simulation_end_seconds=simulation_end_seconds,
            default_start_node=default_start_node,
        )
        # Serialize only fields used by the browser animation. This removes
        # duplicated raw CAD/node/wave metadata from every edge event and keeps
        # the monthly JSON substantially smaller than the previous 1 GB HTML.
        svg_segments: list[dict[str, Any]] = []
        for segment in segments:
            sx0, sy0 = svg_transform.raw_to_svg(segment["x0"], segment["y0"])
            sx1, sy1 = svg_transform.raw_to_svg(segment["x1"], segment["y1"])
            svg_segments.append(
                {
                    "kind": segment["kind"],
                    "t0": round(float(segment["t0"]), 3),
                    "t1": round(float(segment["t1"]), 3),
                    "sx0": round(float(sx0), 3),
                    "sy0": round(float(sy0), 3),
                    "sx1": round(float(sx1), 3),
                    "sy1": round(float(sy1), 3),
                }
            )

        workers_payload.append(
            {
                "worker_id": worker_id,
                "total_distance_m": round(float(worker.total_distance_m), 3),
                "total_picked_units": round(float(worker.total_picked_units), 3),
                "movement_events": len(worker.movement_events),
                "pick_events": len(worker.pick_events),
                "segments": svg_segments,
            }
        )

    origin_timestamp = None
    created_times = [
        item.created_at for item in selected_lists
        if getattr(item, "created_at", None) is not None
    ]
    if created_times:
        origin_timestamp = min(created_times).isoformat()

    return {
        "meta": {
            "selected_date": selected_date.isoformat(),
            "origin_timestamp": origin_timestamp,
            "method": method,
            "method_label": METHOD_LABELS[method],
            "picking_lists": len(selected_lists),
            "operators": len(workers_payload),
            "simulation_end_seconds": round(float(simulation_end_seconds), 3),
            "default_start_node": default_start_node,
            "worker_counts": list(worker_counts) if worker_counts is not None else None,
            "entropy_lambda": entropy_lambda,
            "congestion_conflicts": congestion_conflicts,
            "congestion_wait_seconds": round(congestion_wait_seconds, 3),
            "conflict_event_count": len(conflict_events),
            "conflict_event_source": "DES resource contention",
            "picking_target_points": len(pick_targets),
            "picking_target_events": sum(item["pick_events"] for item in pick_targets),
        },
        "workers": workers_payload,
        "pick_targets": pick_targets,
        "conflict_events": conflict_events,
    }


def _read_entropy_lambda(data_dir: Path, explicit_lambda: float | None) -> float:
    if explicit_lambda is not None:
        value = float(explicit_lambda)
        if not math.isfinite(value) or value < 0:
            raise ValueError("--entropy-lambda는 0 이상의 유한한 값이어야 합니다.")
        return value

    # data/raw -> project root/results/phase4/phase4_recommendation.json
    project_root = data_dir.resolve().parent.parent
    recommendation = project_root / "results" / "phase4" / "phase4_recommendation.json"
    if recommendation.exists():
        try:
            payload = json.loads(recommendation.read_text(encoding="utf-8"))
            value = float(payload["entropy_weight"])
            if math.isfinite(value) and value >= 0:
                print(f"[LAMBDA] Using Phase 4 recommendation λ*={value:g} from {recommendation}")
                return value
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            pass

    print(f"[LAMBDA] Phase 4 recommendation not found; using default λ={DEFAULT_ENTROPY_LAMBDA:g}")
    return DEFAULT_ENTROPY_LAMBDA


class GenerationProgress:
    def __init__(self, total_dates: int, methods: tuple[str, ...]) -> None:
        self.total_dates = total_dates
        self.methods = methods
        self.total_scenarios = total_dates * len(methods)
        self.started = time.monotonic()
        self.completed_scenarios = 0
        self._last_print = 0.0

    def callback(
        self,
        *,
        date_index: int,
        date_value: date,
        method_index: int,
        method: str,
        total_lists: int,
    ) -> Callable[[int, int, Any], None]:
        def report(completed: int, total: int, _event: Any) -> None:
            now = time.monotonic()
            if completed not in {1, total} and completed % max(1, total // 10) != 0:
                return
            if now - self._last_print < 0.10 and completed != total:
                return
            self._last_print = now
            scenario_fraction = 0.0 if total <= 0 else completed / total
            scenario_position = self.completed_scenarios + scenario_fraction
            overall = scenario_position / self.total_scenarios
            elapsed = now - self.started
            eta = 0.0 if overall <= 0 else elapsed * (1.0 - overall) / overall
            print(
                f"[RUN..] {overall*100:6.2f}% | "
                f"date={date_index:>3}/{self.total_dates} {date_value.isoformat()} | "
                f"method={METHOD_LABELS[method]:<8} {method_index}/{len(self.methods)} | "
                f"lists={completed:>4}/{total:<4} | "
                f"elapsed={_format_duration(elapsed)} | ETA={_format_duration(eta)}"
            )
        return report

    def finish_scenario(self) -> None:
        self.completed_scenarios += 1


def _format_duration(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def _comparison_eligibility(
    *,
    selected_lists: list[Any],
    total_workers: int,
    workloads: tuple[float, ...],
    min_lists_per_date: int,
) -> tuple[bool, str, int]:
    """Phase 4와 동일한 핵심 날짜 적합성 조건을 애니메이션 내부에서만 검사한다."""

    active_zones = sum(value > 0 for value in workloads)
    required_workers = active_zones

    if len(selected_lists) < min_lists_per_date:
        return False, "too_few_lists", active_zones
    if total_workers <= 0:
        return False, "no_workers", active_zones
    if active_zones <= 0:
        return False, "no_active_zones", active_zones
    if total_workers < required_workers:
        return False, "insufficient_workers_for_active_zones", active_zones
    return True, "eligible", active_zones


def _simulate_date_methods(
    *,
    warehouse: WarehouseGraph,
    selected_date: date,
    selected_lists: list[Any],
    svg_transform: SvgAxesTransform,
    methods: tuple[str, ...],
    entropy_lambda: float,
    seed: int,
    walking_speed_mps: float,
    pick_seconds_per_unit: float,
    edge_capacity: int,
    pick_node_capacity: int,
    progress: GenerationProgress,
    date_index: int,
    min_lists_per_date: int,
) -> dict[str, Any]:
    zones = build_aisle_zones(warehouse, number_of_zones=4)
    assignments = classify_picking_lists_by_zone(warehouse, selected_lists, zones)
    workloads = zone_workload(zones, assignments, basis="tasks")
    demand_entropy, _ = calculate_demand_entropy(warehouse, selected_lists)
    total_workers = len({item.operator for item in selected_lists})
    if total_workers <= 0:
        raise ValueError(f"{selected_date.isoformat()}의 작업자 수가 0입니다.")

    profiles = macro_zone_demand_profiles(
        warehouse,
        selected_lists,
        zones,
        basis="tasks",
    )
    concentrations = tuple(profile.microzone_concentration for profile in profiles)

    comparison_eligible, comparison_reason, active_zones = _comparison_eligibility(
        selected_lists=selected_lists,
        total_workers=total_workers,
        workloads=workloads,
        min_lists_per_date=min_lists_per_date,
    )

    result: dict[str, Any] = {
        "__availability__": {
            "comparison_eligible": comparison_eligible,
            "reason": comparison_reason,
            "active_zones": active_zones,
            "observed_workers": total_workers,
            "picking_lists": len(selected_lists),
            "min_lists_per_date": min_lists_per_date,
            "available_methods": (
                list(methods)
                if comparison_eligible
                else [method for method in methods if method == "observed"]
            ),
        }
    }

    if not comparison_eligible:
        print(
            f"[INFO ] date={date_index:>3}/{progress.total_dates} "
            f"{selected_date.isoformat()} | comparison unavailable | "
            f"lists={len(selected_lists)}, workers={total_workers}, "
            f"active_zones={active_zones}, min_lists={min_lists_per_date}, "
            f"reason={comparison_reason}"
        )

    for method_index, method in enumerate(methods, start=1):
        # Observed는 Picking_Wave의 실제 작업자 배치를 그대로 재생한다.
        # 비교 방법은 Phase 4 적합 조건을 만족하는 날짜에서만 생성한다.
        if method != "observed" and not comparison_eligible:
            print(
                f"[SKIP ] date={date_index:>3}/{progress.total_dates} "
                f"{selected_date.isoformat()} | "
                f"method={METHOD_LABELS[method]:<8} | "
                f"lists={len(selected_lists)} | workers={total_workers} | "
                f"active_zones={active_zones} | reason={comparison_reason}"
            )
            progress.finish_scenario()
            continue

        callback = progress.callback(
            date_index=date_index,
            date_value=selected_date,
            method_index=method_index,
            method=method,
            total_lists=len(selected_lists),
        )
        print(
            f"[START] date={date_index:>3}/{progress.total_dates} {selected_date.isoformat()} | "
            f"method={METHOD_LABELS[method]} | lists={len(selected_lists)} | workers={total_workers}"
        )

        if method == "observed":
            simulation = run_phase3_observed_baseline(
                warehouse,
                selected_lists,
                zones,
                assignments,
                selected_date=selected_date,
                demand_entropy=demand_entropy,
                walking_speed_mps=walking_speed_mps,
                pick_seconds_per_unit=pick_seconds_per_unit,
                edge_capacity=edge_capacity,
                pick_node_capacity=pick_node_capacity,
                sample_seconds=5.0,
                return_to_io=True,
                volume_basis="tasks",
                progress_callback=callback,
            )
            counts = None
            lambda_value = None
        elif method in {"equal", "random", "volume"}:
            canonical = PHASE3_METHOD_NAMES[method]
            counts = allocate_phase3_workers(
                canonical,
                total_workers=total_workers,
                workloads=workloads,
                seed=seed,
                minimum_per_active_zone=1,
            )
            simulation = run_phase3_method(
                warehouse,
                selected_lists,
                zones,
                assignments,
                method=canonical,
                worker_counts=counts,
                selected_date=selected_date,
                demand_entropy=demand_entropy,
                seed=seed,
                walking_speed_mps=walking_speed_mps,
                pick_seconds_per_unit=pick_seconds_per_unit,
                edge_capacity=edge_capacity,
                pick_node_capacity=pick_node_capacity,
                sample_seconds=5.0,
                return_to_io=True,
                volume_basis="tasks",
                progress_callback=callback,
            )
            lambda_value = None
        else:
            counts = allocate_phase4_workers(
                total_workers=total_workers,
                workloads=workloads,
                entropy_weight=entropy_lambda,
                microzone_concentrations=concentrations,
                minimum_per_active_zone=1,
            )
            simulation = run_phase3_method(
                warehouse,
                selected_lists,
                zones,
                assignments,
                method=f"entropy_lambda_{entropy_lambda:g}",
                worker_counts=counts,
                selected_date=selected_date,
                demand_entropy=demand_entropy,
                seed=seed,
                walking_speed_mps=walking_speed_mps,
                pick_seconds_per_unit=pick_seconds_per_unit,
                edge_capacity=edge_capacity,
                pick_node_capacity=pick_node_capacity,
                sample_seconds=5.0,
                return_to_io=True,
                volume_basis="tasks",
                progress_callback=callback,
            )
            lambda_value = entropy_lambda

        sim_end = float(simulation.summary.simulation_elapsed_seconds)
        result[method] = build_animation_payload(
            warehouse=warehouse,
            simulation=simulation,
            workers=simulation.workers,
            selected_lists=selected_lists,
            selected_date=selected_date,
            simulation_end_seconds=sim_end,
            svg_transform=svg_transform,
            method=method,
            worker_counts=counts,
            entropy_lambda=lambda_value,
        )
        progress.finish_scenario()

    return result

def render_single_html(
    *,
    svg_transform: SvgAxesTransform,
    manifest: dict[str, Any],
    entropy_lambda: float,
    data_dir_name: str,
) -> str:
    """Render one lightweight HTML shell that lazily loads monthly JSON files."""

    manifest_json = json.dumps(manifest, ensure_ascii=False, separators=(",", ":"))
    data_dir_json = json.dumps(data_dir_name, ensure_ascii=False)
    zone_rectangles_json = json.dumps(
        svg_transform.zone_rectangles, ensure_ascii=False, separators=(",", ":")
    )
    method_options = "\n".join(
        f'<option value="{method}">{METHOD_LABELS[method]}</option>' for method in METHODS
    )

    return f'''<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Warehouse Picking Animation</title>
<style>
  * {{ box-sizing: border-box; }}
  html, body {{ margin: 0; min-height: 100%; font-family: Arial, "Noto Sans KR", sans-serif; background: #f3f6fa; color: #172033; }}
  .app {{ width: 100%; padding: 18px 20px 26px; }}
  .layout {{ display: grid; grid-template-columns: minmax(0, 1fr) 330px; gap: 16px; align-items: start; max-width: 1680px; margin: 0 auto; }}
  .visual-panel {{ min-width: 0; background: #fff; border-radius: 14px; box-shadow: 0 5px 22px rgba(15,23,42,.06); padding: 10px; }}
  #svg-stack {{ position: relative; width: 100%; aspect-ratio: 3 / 2; overflow: hidden; background: #fff; border-radius: 10px; }}
  #svg-stack > svg {{ position: absolute; inset: 0; width: 100%; height: 100%; }}
  #svg-stack > svg:first-of-type {{ z-index: 1; }}
  #overlay {{ z-index: 2; }}
  #workerCanvas {{ position: absolute; inset: 0; width: 100%; height: 100%; z-index: 3; pointer-events: none; background: transparent !important; }}
  #overlay .zone-rect {{ pointer-events: none; }}
  #overlay .zone-rect rect {{ fill-opacity: .055; stroke-width: 2.1; stroke-dasharray: 8 5; vector-effect: non-scaling-stroke; }}
  #overlay .zone-rect text {{ font-size: 15px; font-weight: 800; paint-order: stroke; stroke: rgba(255,255,255,.92); stroke-width: 4px; stroke-linejoin: round; pointer-events: none; }}
  #overlay .zone-z01 rect {{ fill: #2563eb; stroke: #2563eb; }}
  #overlay .zone-z01 text {{ fill: #1d4ed8; }}
  #overlay .zone-z02 rect {{ fill: #7c3aed; stroke: #7c3aed; }}
  #overlay .zone-z02 text {{ fill: #6d28d9; }}
  #overlay .zone-z03 rect {{ fill: #059669; stroke: #059669; }}
  #overlay .zone-z03 text {{ fill: #047857; }}
  #overlay .zone-z04 rect {{ fill: #db2777; stroke: #db2777; }}
  #overlay .zone-z04 text {{ fill: #be185d; }}
  #overlay .pick-target circle {{ fill: #f59e0b; fill-opacity: .34; stroke: #b45309; stroke-width: 1.5; }}
  #overlay .pick-target text {{ font-size: 8px; font-weight: 700; text-anchor: middle; dominant-baseline: middle; fill: #78350f; pointer-events: none; }}
  #overlay .pick-target {{ pointer-events: auto; }}
  .controls {{ display: grid; grid-template-columns: auto 76px 150px minmax(120px,1fr) 74px; gap: 9px; align-items: center; padding: 8px 0 0; }}
  .play-options {{ display: flex; justify-content: flex-start; align-items: center; gap: 12px; padding: 7px 2px 0; font-size: 13px; color: #455065; }}
  .play-option-label {{ display: inline-flex; align-items: center; gap: 7px; cursor: pointer; user-select: none; }}
  .play-option-label input[type="checkbox"] {{ width: 16px; height: 16px; margin: 0; cursor: pointer; }}
  button, select {{ min-height: 38px; border: 1px solid #d6dce5; border-radius: 9px; background: #fff; color: #172033; padding: 7px 10px; font-size: 14px; }}
  button {{ cursor: pointer; white-space: nowrap; }}
  input[type="range"] {{ width: 100%; }}
  #timeLabel {{ font-variant-numeric: tabular-nums; font-size: 13px; text-align: right; }}
  .sidebar {{ background: #fff; border-radius: 14px; box-shadow: 0 5px 22px rgba(15,23,42,.07); padding: 14px 15px 16px; min-width: 0; }}
  .kv {{ display: grid; grid-template-columns: 108px minmax(0,1fr); gap: 10px 8px; align-items: center; font-size: 14px; }}
  .key {{ color: #667085; }}
  .value {{ min-width: 0; font-variant-numeric: tabular-nums; }}
  .sidebar select {{ width: 100%; min-width: 0; }}
  .status {{ margin-top: 13px; background: #f7f9fc; border-radius: 11px; padding: 12px; font-size: 13px; line-height: 1.45; }}
  .status.error {{ background: #fff3f3; color: #9b1c1c; }}
  .status.loading {{ background: #f0f6ff; color: #244b76; }}
  .notes {{ margin-top: 14px; padding-bottom: 13px; border-bottom: 1px solid #e5e7eb; color: #455065; font-size: 12.5px; line-height: 1.5; }}
  .workers {{ margin-top: 12px; max-height: 430px; overflow: auto; }}
  .worker-row {{ display: grid; grid-template-columns: 14px minmax(0,1fr) auto; gap: 8px; align-items: center; padding: 6px 0; font-size: 12.5px; }}
  .dot {{ width: 11px; height: 11px; border-radius: 50%; }}
  .worker-name {{ white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
  .worker-sub {{ color: #697386; font-size: 11px; }}
  .distance {{ white-space: nowrap; font-variant-numeric: tabular-nums; }}
  .allocation {{ margin-top: 8px; color: #697386; font-size: 11px; line-height: 1.4; }}
  @media (max-width: 980px) {{
    .layout {{ grid-template-columns: 1fr; }}
    .sidebar {{ order: -1; }}
    .workers {{ max-height: 230px; }}
  }}
  @media (max-width: 650px) {{
    .app {{ padding: 8px; }}
    .controls {{ grid-template-columns: 1fr 1fr; }}
    .controls input[type="range"] {{ grid-column: 1 / -1; }}
    .play-options {{ padding-top: 9px; }}
    #timeLabel {{ text-align: left; }}
  }}
</style>
</head>
<body>
<div class="app">
  <div class="layout">
    <main class="visual-panel">
      <div id="svg-stack">
        {svg_transform.svg_markup}
        <svg id="overlay" viewBox="{svg_transform.view_box}" preserveAspectRatio="xMidYMid meet"></svg>
        <canvas id="workerCanvas" aria-label="작업자 위치 애니메이션"></canvas>
      </div>
      <div class="controls">
        <button id="playBtn" type="button">▶ 재생</button>
        <select id="speedSel" aria-label="재생 배속">
          <option value="0.5">0.5x</option>
          <option value="1" selected>1x</option>
          <option value="2">2x</option>
          <option value="3">3x</option>
          <option value="5">5x</option>
          <option value="10">10x</option>
          <option value="20">20x</option>
          <option value="50">50x</option>
        </select>
        <select id="workerSel" aria-label="작업자 선택"><option value="ALL">전체 작업자</option></select>
        <input id="timeSlider" type="range" min="0" max="1" step="0.1" value="0" aria-label="재생 시간" />
        <div id="timeLabel">00:00:00</div>
      </div>
      <div class="play-options">
        <label class="play-option-label" for="autoNextDateChk">
          <input id="autoNextDateChk" type="checkbox" />
          <span>다음 날짜 자동실행</span>
        </label>
        <label class="play-option-label" for="pickTargetsChk">
          <input id="pickTargetsChk" type="checkbox" checked />
          <span>피킹 대상 표시</span>
        </label>
        <label class="play-option-label" for="zonesChk">
          <input id="zonesChk" type="checkbox" checked />
          <span>Z01~Z04 구역 표시</span>
        </label>
      </div>
    </main>

    <aside class="sidebar">
      <div class="kv">
        <div class="key">날짜 <span id="dateCountBadge"></span></div><div class="value"><select id="dateSel" aria-label="날짜 선택"></select></div>
        <div class="key">방법</div><div class="value"><select id="methodSel" aria-label="배치 방법">{method_options}</select></div>
        <div class="key">피킹리스트 수</div><div class="value" id="metaLists">-</div>
        <div class="key">작업자 수</div><div class="value" id="metaWorkers">-</div>
        <div class="key">피킹 대상</div><div class="value" id="metaPickTargets">-</div>
        <div class="key">총 재생시간</div><div class="value" id="metaDuration">-</div>
      </div>
      <div class="allocation" id="allocationInfo"></div>
      <div class="status loading" id="statusBox">월별 데이터를 불러오는 중입니다.</div>
      <div class="notes">
        - 큰 원형 마커는 Canvas로 렌더링되는 작업자 현재 위치입니다.<br />
        - 주황색 반투명 포인트는 당일 Picking_Wave의 피킹 대상 위치입니다. 숫자는 동일 포인트의 피킹 이벤트 수입니다.<br />
        - 점선 사각형은 인력배치 Macro-zone입니다: Z01=Left/Near, Z02=Left/Far, Z03=Right/Near, Z04=Right/Far.<br />
        - 실제 SVG support marker로 좌표를 자동 보정합니다.<br />
        - 논문의 Conflicts와 동일한 DES resource contention 대기 구간에만 해당 피커가 빨간색으로 표시됩니다.<br />
        - '다음 날짜 자동실행' 체크 시 날짜가 끝나면 같은 방법의 다음 사용 가능 날짜를 자동 재생합니다.<br />
        - 체크를 해제하면 현재 날짜의 재생 종료 시 그 자리에서 정지합니다.<br />
        - Entropy는 λ*={entropy_lambda:g}를 사용합니다.
      </div>
      <div class="workers" id="workerList"></div>
    </aside>
  </div>
</div>
<script id="animationManifest" type="application/json">{manifest_json}</script>
<script>
(() => {{
  const manifest = JSON.parse(document.getElementById('animationManifest').textContent);
  const dataDir = {data_dir_json};
  const macroZones = {zone_rectangles_json};
  const dates = manifest.date_order;
  const monthByDate = manifest.month_by_date;
  const monthFiles = manifest.month_files;
  const availabilityIndex = manifest.availability_by_date;

  const overlay = document.getElementById('overlay');
  const workerCanvas = document.getElementById('workerCanvas');
  const workerCtx = workerCanvas.getContext('2d', {{alpha: true}});
  const svgStack = document.getElementById('svg-stack');
  const dateSel = document.getElementById('dateSel');
  const methodSel = document.getElementById('methodSel');
  const workerSel = document.getElementById('workerSel');
  const speedSel = document.getElementById('speedSel');
  const playBtn = document.getElementById('playBtn');
  const autoNextDateChk = document.getElementById('autoNextDateChk');
  const pickTargetsChk = document.getElementById('pickTargetsChk');
  const zonesChk = document.getElementById('zonesChk');
  const slider = document.getElementById('timeSlider');
  const timeLabel = document.getElementById('timeLabel');
  const workerList = document.getElementById('workerList');
  const statusBox = document.getElementById('statusBox');
  const allocationInfo = document.getElementById('allocationInfo');
  const dateCountBadge = document.getElementById('dateCountBadge');

  // 충돌 표시는 월별 JSON의 실제 DES resource-contention event만 사용한다.
  // 거리/픽셀 기반 근접 판정은 사용하지 않는다.

  dateCountBadge.textContent = `(${{dates.length}})`;

  function observedWorkerCountFor(dateValue) {{
    const info = availabilityIndex[dateValue];
    if (info && Number.isFinite(Number(info.observed_workers))) {{
      return Number(info.observed_workers);
    }}
    return 0;
  }}

  dates.forEach(d => {{
    const opt = document.createElement('option');
    const pickerCount = observedWorkerCountFor(d);
    opt.value = d;
    opt.textContent = `${{d}} (${{pickerCount}})`;
    opt.title = `날짜=${{d}}, 피커=${{pickerCount}}명`;
    dateSel.appendChild(opt);
  }});

  let currentDate = dates[0];
  let currentMethod = 'observed';
  let scenario = null;
  let currentTime = 0;
  let playing = false;
  let hasStarted = false;
  let raf = null;
  let lastTs = null;
  let selectedWorker = 'ALL';
  let markerMap = new Map();
  let workerIndices = new Map();
  let zoneLayer = null;
  let pickTargetLayer = null;
  let currentPickTargets = [];
  let loadedMonthKey = null;
  let loadedMonthData = null;
  let loadToken = 0;

  // Performance: 작업자 위치는 브라우저 프레임마다 갱신하지만,
  // 상태 패널/슬라이더 같은 HTML UI는 10 FPS로 제한한다.
  // 고배속에서도 프레임 수를 늘리지 않고 시뮬레이션 시간만 빠르게 진행한다.
  const UI_REFRESH_MS = 100;
  let lastUiRenderMs = -Infinity;

  // Performance: conflict_events를 매 프레임 filter()하지 않고 시간 순 cursor로 소비한다.
  // 슬라이더로 과거 시각으로 이동할 때만 cursor를 재구성한다.
  let preparedConflictEvents = [];
  let conflictCursor = 0;
  let activeConflictPool = [];
  let cumulativePickerPrefix = [0];
  let lastConflictTime = -Infinity;
  let hasExactConflictEvents = false;

  // Canvas worker layer: SVG의 preserveAspectRatio="xMidYMid meet"와 같은
  // 좌표 변환을 사용하여 Operator만 Canvas에서 고속 렌더링한다.
  let canvasCssWidth = 1;
  let canvasCssHeight = 1;
  let canvasScale = 1;
  let canvasOffsetX = 0;
  let canvasOffsetY = 0;
  let canvasDpr = 1;
  let lastCanvasFrameStates = [];

  function updateCanvasMetrics() {{
    // Canvas 자체는 항상 투명 레이어로 유지한다.
    workerCanvas.style.backgroundColor = 'transparent';
    const rect = workerCanvas.getBoundingClientRect();
    canvasCssWidth = Math.max(1, rect.width);
    canvasCssHeight = Math.max(1, rect.height);
    canvasDpr = Math.max(1, Math.min(3, Number(window.devicePixelRatio) || 1));

    const targetWidth = Math.max(1, Math.round(canvasCssWidth * canvasDpr));
    const targetHeight = Math.max(1, Math.round(canvasCssHeight * canvasDpr));
    if (workerCanvas.width !== targetWidth || workerCanvas.height !== targetHeight) {{
      workerCanvas.width = targetWidth;
      workerCanvas.height = targetHeight;
    }}
    workerCtx.setTransform(canvasDpr, 0, 0, canvasDpr, 0, 0);

    const vb = overlay.viewBox.baseVal;
    const vbWidth = Math.max(1e-9, Number(vb.width) || 1);
    const vbHeight = Math.max(1e-9, Number(vb.height) || 1);
    canvasScale = Math.min(canvasCssWidth / vbWidth, canvasCssHeight / vbHeight);
    canvasOffsetX = (canvasCssWidth - vbWidth * canvasScale) / 2 - Number(vb.x || 0) * canvasScale;
    canvasOffsetY = (canvasCssHeight - vbHeight * canvasScale) / 2 - Number(vb.y || 0) * canvasScale;
  }}

  function clearWorkerCanvas() {{
    if (!workerCtx) return;
    // 일부 Chromium/Edge GPU 환경에서 desynchronized/alpha Canvas가 검게 합성되는
    // 현상을 피하기 위해 실제 backing-store pixel 전체를 identity transform으로 지운다.
    workerCtx.save();
    workerCtx.setTransform(1, 0, 0, 1, 0, 0);
    workerCtx.clearRect(0, 0, workerCanvas.width, workerCanvas.height);
    workerCtx.restore();
  }}

  function drawWorkersCanvas(frameStates = lastCanvasFrameStates) {{
    if (!workerCtx) return;
    lastCanvasFrameStates = frameStates || [];
    clearWorkerCanvas();
    if (!lastCanvasFrameStates.length) return;

    const radius = Math.max(4, 12 * canvasScale);
    const baseStroke = Math.max(1, 2.2 * canvasScale);
    const collisionStroke = Math.max(1.5, 3.4 * canvasScale);
    const fontPx = Math.max(8, 18 * canvasScale);
    workerCtx.textAlign = 'center';
    workerCtx.textBaseline = 'middle';
    workerCtx.font = `700 ${{fontPx.toFixed(2)}}px Arial, "Noto Sans KR", sans-serif`;

    for (const state of lastCanvasFrameStates) {{
      if (!state.visible) continue;
      const marker = markerMap.get(state.worker.worker_id);
      if (!marker) continue;
      const x = canvasOffsetX + state.x * canvasScale;
      const y = canvasOffsetY + state.y * canvasScale;
      if (!Number.isFinite(x) || !Number.isFinite(y)) continue;

      workerCtx.globalAlpha = state.opacity;
      workerCtx.beginPath();
      workerCtx.arc(x, y, radius, 0, Math.PI * 2);
      workerCtx.fillStyle = state.colliding ? '#ef4444' : marker.baseColor;
      workerCtx.fill();
      workerCtx.lineWidth = state.colliding ? collisionStroke : baseStroke;
      workerCtx.strokeStyle = state.colliding ? '#991b1b' : '#ffffff';
      workerCtx.stroke();

      workerCtx.fillStyle = state.colliding ? '#ffffff' : '#111827';
      workerCtx.fillText(marker.label, x, y + 0.3 * canvasScale);
    }}
    workerCtx.globalAlpha = 1;
  }}

  function resizeWorkerCanvas() {{
    updateCanvasMetrics();
    drawWorkersCanvas();
  }}

  function formatSeconds(value) {{
    const total = Math.max(0, Math.floor(Number(value) || 0));
    const h = String(Math.floor(total / 3600)).padStart(2, '0');
    const m = String(Math.floor((total % 3600) / 60)).padStart(2, '0');
    const s = String(total % 60).padStart(2, '0');
    return `${{h}}:${{m}}:${{s}}`;
  }}

  function formatActualDateTime(elapsedSeconds) {{
    const originText = scenario && scenario.meta ? scenario.meta.origin_timestamp : null;
    if (!originText) return '-';

    // ISO timestamp의 날짜/시간 부분을 직접 계산하여 브라우저 timezone 변환에
    // 의해 시각이 바뀌지 않도록 한다.
    const match = String(originText).match(
      /^(\\d{{4}})-(\\d{{2}})-(\\d{{2}})T(\\d{{2}}):(\\d{{2}}):(\\d{{2}}(?:\\.\\d+)?)/
    );
    if (!match) return String(originText);

    const year = Number(match[1]);
    const month = Number(match[2]);
    const day = Number(match[3]);
    const hour = Number(match[4]);
    const minute = Number(match[5]);
    const second = Number(match[6]);

    // UTC 계산 함수를 단순 calendar arithmetic 용도로만 사용한다.
    // 입력 timestamp의 timezone을 변환하려는 목적이 아니다.
    const originMs = Date.UTC(year, month - 1, day, hour, minute, Math.floor(second));
    const actual = new Date(originMs + Math.max(0, Number(elapsedSeconds) || 0) * 1000);

    const yyyy = actual.getUTCFullYear();
    const mm = String(actual.getUTCMonth() + 1).padStart(2, '0');
    const dd = String(actual.getUTCDate()).padStart(2, '0');
    const hh = String(actual.getUTCHours()).padStart(2, '0');
    const mi = String(actual.getUTCMinutes()).padStart(2, '0');
    const ss = String(actual.getUTCSeconds()).padStart(2, '0');
    return `${{yyyy}}-${{mm}}-${{dd}} ${{hh}}:${{mi}}:${{ss}}`;
  }}

  function colorForIndex(i) {{ return `hsl(${{(i * 47) % 360}} 70% 52%)`; }}
  function svgNode(tag) {{ return document.createElementNS('http://www.w3.org/2000/svg', tag); }}
  function shortWorkerLabel(id, index) {{
    const observed = id.match(/Operator_(\\d+)/i);
    if (observed) return observed[1];
    return String(index + 1).padStart(2, '0');
  }}

  function stop() {{
    playing = false;
    playBtn.textContent = '▶ 재생';
    lastTs = null;
    if (raf !== null) cancelAnimationFrame(raf);
    raf = null;
  }}

  function start() {{
    if (!scenario) return;
    if (currentTime >= scenario.meta.simulation_end_seconds) currentTime = 0;
    if (playing) return;
    hasStarted = true;
    playing = true;
    playBtn.textContent = '⏸ 일시정지';
    lastTs = null;
    render(true);
    raf = requestAnimationFrame(tick);
  }}

  function setStatus(message, kind = '') {{
    statusBox.className = `status${{kind ? ' ' + kind : ''}}`;
    statusBox.innerHTML = message;
  }}

  function dataLoadError(error) {{
    stop();
    const localHint = location.protocol === 'file:'
      ? '<br><br><strong>중요:</strong> 월별 JSON은 브라우저 보안정책 때문에 파일을 더블클릭(file://)해서는 읽지 못할 수 있습니다.' +
        '<br>HTML이 있는 폴더에서 <code>python -m http.server 8000</code> 실행 후 ' +
        '<code>http://localhost:8000/</code> 으로 접속해 주세요.'
      : '';
    setStatus(`<strong>데이터 로드 실패</strong><br>${{String(error.message || error)}}${{localHint}}`, 'error');
  }}

  async function loadMonth(monthKey) {{
    if (loadedMonthKey === monthKey && loadedMonthData) return loadedMonthData;
    const fileName = monthFiles[monthKey];
    if (!fileName) throw new Error(`월별 데이터 파일 정보가 없습니다: ${{monthKey}}`);
    setStatus(`<strong>${{monthKey}}</strong> 월 데이터를 불러오는 중...`, 'loading');
    const response = await fetch(`${{dataDir}}/${{fileName}}`, {{cache: 'force-cache'}});
    if (!response.ok) throw new Error(`HTTP ${{response.status}} · ${{dataDir}}/${{fileName}}`);
    const data = await response.json();
    loadedMonthKey = monthKey;
    loadedMonthData = data;
    return data;
  }}

  async function scenariosForDate(dateValue) {{
    const monthKey = monthByDate[dateValue];
    const monthData = await loadMonth(monthKey);
    const dateScenarios = monthData.dates[dateValue];
    if (!dateScenarios) throw new Error(`날짜 데이터를 찾지 못했습니다: ${{dateValue}}`);
    return dateScenarios;
  }}

  function derivePickTargets() {{
    // v7 JSON은 풍부한 pick_targets 메타데이터를 사용한다.
    if (Array.isArray(scenario.pick_targets) && scenario.pick_targets.length) {{
      return scenario.pick_targets;
    }}

    // 구버전(v6 이하) 월별 JSON도 HTML-only 재생성만으로 표시할 수 있도록
    // worker.segments의 pick 좌표를 물리 포인트별로 합친다.
    const byPoint = new Map();
    (scenario.workers || []).forEach(worker => {{
      (worker.segments || []).forEach(seg => {{
        if (seg.kind !== 'pick') return;
        const sx = Number(seg.sx1);
        const sy = Number(seg.sy1);
        if (!Number.isFinite(sx) || !Number.isFinite(sy)) return;
        const key = `${{sx.toFixed(3)}}|${{sy.toFixed(3)}}`;
        let item = byPoint.get(key);
        if (!item) {{
          item = {{
            node_id: `SVG(${{sx.toFixed(1)}}, ${{sy.toFixed(1)}})`,
            sx, sy, pick_events: 0, quantity_units: 0,
            worker_ids: [], location_ids: [], references: []
          }};
          byPoint.set(key, item);
        }}
        item.pick_events += 1;
        if (!item.worker_ids.includes(worker.worker_id)) item.worker_ids.push(worker.worker_id);
      }});
    }});
    return Array.from(byPoint.values());
  }}

  function rebuildZoneLayer() {{
    zoneLayer = svgNode('g');
    zoneLayer.setAttribute('id', 'macro-zone-layer');
    overlay.appendChild(zoneLayer);

    (macroZones || []).forEach(zone => {{
      const g = svgNode('g');
      g.setAttribute('class', `zone-rect zone-${{String(zone.zone_id || '').toLowerCase()}}`);

      const rect = svgNode('rect');
      rect.setAttribute('x', Number(zone.x).toFixed(3));
      rect.setAttribute('y', Number(zone.y).toFixed(3));
      rect.setAttribute('width', Number(zone.width).toFixed(3));
      rect.setAttribute('height', Number(zone.height).toFixed(3));
      rect.setAttribute('rx', '3');
      rect.setAttribute('ry', '3');
      g.appendChild(rect);

      const text = svgNode('text');
      text.setAttribute('x', (Number(zone.x) + 8).toFixed(3));
      text.setAttribute('y', (Number(zone.y) + 19).toFixed(3));
      text.textContent = String(zone.zone_id || '');
      g.appendChild(text);

      const title = svgNode('title');
      title.textContent = String(zone.label || zone.zone_id || 'Macro zone');
      g.appendChild(title);
      zoneLayer.appendChild(g);
    }});
    zoneLayer.style.display = zonesChk.checked ? '' : 'none';
  }}

  function prepareConflictIndex() {{
    hasExactConflictEvents = Array.isArray(scenario && scenario.conflict_events);
    preparedConflictEvents = hasExactConflictEvents
      ? scenario.conflict_events.slice().sort((a, b) => Number(a.t0) - Number(b.t0))
      : [];
    conflictCursor = 0;
    activeConflictPool = [];
    lastConflictTime = -Infinity;
    cumulativePickerPrefix = new Array(preparedConflictEvents.length + 1);
    cumulativePickerPrefix[0] = 0;
    for (let i = 0; i < preparedConflictEvents.length; i++) {{
      const ids = Array.isArray(preparedConflictEvents[i].worker_ids)
        ? preparedConflictEvents[i].worker_ids.length
        : 0;
      cumulativePickerPrefix[i + 1] = cumulativePickerPrefix[i] + ids;
    }}
  }}

  function conflictSnapshot(t) {{
    if (!hasStarted || !hasExactConflictEvents) {{
      return {{ active: [], collisionWorkers: new Set(), cumulativeCount: 0, cumulativePickerCount: 0 }};
    }}

    // 슬라이더/날짜 이동 등으로 시간이 뒤로 간 경우에만 cursor를 초기화한다.
    if (t + 1e-9 < lastConflictTime) {{
      conflictCursor = 0;
      activeConflictPool = [];
    }}

    while (conflictCursor < preparedConflictEvents.length &&
           Number(preparedConflictEvents[conflictCursor].t0) <= t) {{
      activeConflictPool.push(preparedConflictEvents[conflictCursor]);
      conflictCursor += 1;
    }}

    // 이미 종료된 이벤트만 제거하므로 일반 재생에서는 active event 수만큼만 검사한다.
    if (activeConflictPool.length) {{
      activeConflictPool = activeConflictPool.filter(event => Number(event.t1) >= t);
    }}
    lastConflictTime = t;

    const collisionWorkers = new Set();
    activeConflictPool.forEach(event => {{
      (event.worker_ids || []).forEach(workerId => collisionWorkers.add(String(workerId)));
    }});
    return {{
      active: activeConflictPool,
      collisionWorkers,
      cumulativeCount: conflictCursor,
      cumulativePickerCount: cumulativePickerPrefix[conflictCursor] || 0
    }};
  }}

  function rebuildWorkers() {{
    overlay.innerHTML = '';
    workerSel.innerHTML = '<option value="ALL">전체 작업자</option>';
    workerList.innerHTML = '';
    markerMap = new Map();
    workerIndices = new Map();
    lastCanvasFrameStates = [];

    // 정적 요소만 SVG에 유지한다: Macro-zone -> 피킹 대상.
    rebuildZoneLayer();

    pickTargetLayer = svgNode('g');
    pickTargetLayer.setAttribute('id', 'pick-target-layer');
    overlay.appendChild(pickTargetLayer);
    currentPickTargets = derivePickTargets();
    currentPickTargets.forEach(target => {{
      const g = svgNode('g');
      g.setAttribute('class', 'pick-target');
      const events = Math.max(1, Number(target.pick_events) || 1);
      const radius = Math.min(12, 6 + Math.log2(events + 1) * 1.6);
      const c = svgNode('circle');
      c.setAttribute('cx', Number(target.sx).toFixed(3));
      c.setAttribute('cy', Number(target.sy).toFixed(3));
      c.setAttribute('r', radius.toFixed(2));
      g.appendChild(c);

      if (events > 1) {{
        const t = svgNode('text');
        t.setAttribute('x', Number(target.sx).toFixed(3));
        t.setAttribute('y', Number(target.sy).toFixed(3));
        t.textContent = String(events);
        g.appendChild(t);
      }}

      const title = svgNode('title');
      const locations = Array.isArray(target.location_ids) && target.location_ids.length
        ? target.location_ids.join(', ')
        : target.node_id;
      const workers = Array.isArray(target.worker_ids) ? target.worker_ids.join(', ') : '';
      const qty = Number(target.quantity_units);
      const qtyText = Number.isFinite(qty) && qty > 0 ? ` | 수량=${{qty.toFixed(1)}}` : '';
      title.textContent = `피킹 대상: ${{locations}} | 이벤트=${{events}}${{qtyText}}${{workers ? ' | 작업자=' + workers : ''}}`;
      g.appendChild(title);
      pickTargetLayer.appendChild(g);
    }});
    pickTargetLayer.style.display = pickTargetsChk.checked ? '' : 'none';

    // Operator는 SVG DOM을 만들지 않고 Canvas 렌더링용 메타데이터만 준비한다.
    scenario.workers.forEach((worker, index) => {{
      workerIndices.set(worker.worker_id, 0);
      const color = colorForIndex(index);
      const label = shortWorkerLabel(worker.worker_id, index);

      const option = document.createElement('option');
      option.value = worker.worker_id;
      option.textContent = worker.worker_id;
      workerSel.appendChild(option);

      const row = document.createElement('div');
      row.className = 'worker-row';
      row.innerHTML = `<div class="dot" style="background:${{color}}"></div>` +
        `<div><div class="worker-name" title="${{worker.worker_id}}">${{worker.worker_id}}</div>` +
        `<div class="worker-sub">move=${{worker.movement_events}}, pick=${{worker.pick_events}}</div></div>` +
        `<div class="distance">${{worker.total_distance_m.toFixed(1)}} m</div>`;
      workerList.appendChild(row);
      const dot = row.querySelector('.dot');
      markerMap.set(worker.worker_id, {{
        dot,
        baseColor: color,
        label,
        lastCollision: null
      }});
    }});
    selectedWorker = 'ALL';
    resizeWorkerCanvas();
  }}

  function findSegment(worker, t) {{
    const segs = worker.segments;
    let idx = workerIndices.get(worker.worker_id) || 0;
    while (idx > 0 && t < segs[idx].t0) idx--;
    while (idx < segs.length - 1 && t > segs[idx].t1) idx++;
    workerIndices.set(worker.worker_id, idx);
    return segs[idx];
  }}

  function position(seg, t) {{
    if (seg.kind !== 'move' || seg.t1 <= seg.t0) return {{x: seg.sx1, y: seg.sy1}};
    const r = Math.max(0, Math.min(1, (t - seg.t0) / (seg.t1 - seg.t0)));
    return {{x: seg.sx0 + (seg.sx1 - seg.sx0) * r, y: seg.sy0 + (seg.sy1 - seg.sy0) * r}};
  }}

  function render(forceUi = false) {{
    if (!scenario) return;

    const conflictState = conflictSnapshot(currentTime);
    const activeConflictEvents = conflictState.active;
    const collisionWorkers = conflictState.collisionWorkers;
    const nowMs = performance.now();
    const updateUi = forceUi || (nowMs - lastUiRenderMs >= UI_REFRESH_MS);
    const states = updateUi ? [] : null;
    const frameStates = [];

    // Operator 위치 계산은 기존 segment cursor를 그대로 사용하되,
    // 화면 출력은 SVG DOM 변경 대신 Canvas 한 장을 매 프레임 다시 그린다.
    scenario.workers.forEach(worker => {{
      const seg = findSegment(worker, currentTime);
      let x = Number(seg.sx1);
      let y = Number(seg.sy1);
      if (seg.kind === 'move' && seg.t1 > seg.t0) {{
        const r = Math.max(0, Math.min(1, (currentTime - seg.t0) / (seg.t1 - seg.t0)));
        x = Number(seg.sx0) + (Number(seg.sx1) - Number(seg.sx0)) * r;
        y = Number(seg.sy0) + (Number(seg.sy1) - Number(seg.sy0)) * r;
      }}

      const visible = selectedWorker === 'ALL' || selectedWorker === worker.worker_id;
      const colliding = collisionWorkers.has(worker.worker_id);
      const opacity = hasStarted && seg.kind === 'idle' ? 0.68 : 1;
      const marker = markerMap.get(worker.worker_id);

      if (marker && marker.lastCollision !== colliding) {{
        if (marker.dot) marker.dot.style.background = colliding ? '#ef4444' : marker.baseColor;
        marker.lastCollision = colliding;
      }}

      frameStates.push({{ worker, seg, x, y, visible, colliding, opacity }});
      if (updateUi && visible) {{
        states.push(`${{worker.worker_id}}(${{seg.kind}}${{colliding ? ', 충돌' : ''}})`);
      }}
    }});

    drawWorkersCanvas(frameStates);

    if (!updateUi) return;
    lastUiRenderMs = nowMs;
    slider.value = String(currentTime);
    timeLabel.textContent = formatSeconds(currentTime);

    const conflictPreview = activeConflictEvents.length
      ? activeConflictEvents.slice(0, 5).map(event => {{
          const workers = (event.worker_ids || []).join('↔') || '?';
          const resource = event.resource_id ? ` · ${{event.resource_type}}:${{event.resource_id}}` : '';
          return `${{workers}}${{resource}}`;
        }}).join(', ') + (activeConflictEvents.length > 5 ? ' ...' : '')
      : '없음';
    const totalConflicts = Number(scenario.meta.congestion_conflicts || 0);
    const totalWait = Number(scenario.meta.congestion_wait_seconds || 0);
    const stateText = hasStarted
      ? `${{states.slice(0, 7).join(', ')}}${{states.length > 7 ? ' ...' : ''}}`
      : '재생 대기';

    setStatus(`<strong>현재 시간</strong> : ${{formatSeconds(currentTime)}}<br>` +
      `<strong>실제 시간</strong> : ${{formatActualDateTime(currentTime)}}<br>` +
      `<strong>표시 작업자</strong> : ${{selectedWorker === 'ALL' ? '전체' : selectedWorker}}<br>` +
      `<strong>활성 마커 수</strong> : ${{states.length}}<br>` +
      `<strong>피킹 대상 포인트</strong> : ${{currentPickTargets.length}}개<br>` +
      `<strong>DES Conflicts</strong> : ${{totalConflicts}}회 · <strong>총 대기</strong> : ${{totalWait.toFixed(2)}}초<br>` +
      `<strong>현재 충돌 이벤트</strong> : ${{activeConflictEvents.length}}개 · <strong>충돌 피커</strong> : ${{collisionWorkers.size}}명<br>` +
      `<strong>누적 충돌 이벤트</strong> : ${{conflictState.cumulativeCount}}개 · <strong>누적 충돌 피커</strong> : ${{conflictState.cumulativePickerCount}}명<br>` +
      `<strong>현재 충돌</strong> : ${{conflictPreview}}<br>` +
      `<strong>이벤트 소스</strong> : ${{hasExactConflictEvents ? '실제 DES resource contention' : '구버전 JSON · 실제 이벤트 없음'}}<br>` +
      `<strong>렌더링</strong> : Operator Canvas(transparent) · 정적 도면 SVG<br>` +
      `<strong>상태</strong> : ${{stateText}}`);
  }}

  function availabilityFor(dateValue) {{
    return availabilityIndex[dateValue] || {{
      comparison_eligible: true,
      reason: 'eligible',
      available_methods: ['observed', 'equal', 'random', 'volume', 'entropy']
    }};
  }}

  function reasonLabel(reason) {{
    const labels = {{
      too_few_lists: '피킹리스트 수가 비교 실험 기준보다 적음',
      no_workers: '작업자가 없음',
      no_active_zones: '활성 구역이 없음',
      insufficient_workers_for_active_zones: '작업자 수가 활성 구역 수보다 적음',
      eligible: '사용 가능'
    }};
    return labels[reason] || reason;
  }}

  function methodAvailable(dateValue, methodValue) {{
    const info = availabilityFor(dateValue);
    return (info.available_methods || ['observed']).includes(methodValue);
  }}

  function updateMethodOptions(dateValue) {{
    const info = availabilityFor(dateValue);
    const available = new Set(info.available_methods || ['observed']);
    Array.from(methodSel.options).forEach(option => {{ option.disabled = !available.has(option.value); }});
  }}

  async function loadScenario(dateValue, methodValue, autoPlay) {{
    const token = ++loadToken;
    stop();
    currentDate = dateValue;
    updateMethodOptions(currentDate);
    currentMethod = methodAvailable(currentDate, methodValue) ? methodValue : 'observed';
    dateSel.value = currentDate;
    methodSel.value = currentMethod;

    try {{
      const dateScenarios = await scenariosForDate(currentDate);
      if (token !== loadToken) return;
      scenario = dateScenarios[currentMethod] || dateScenarios.observed;
      if (!scenario) throw new Error(`시나리오가 없습니다: ${{currentDate}} / ${{currentMethod}}`);

      currentTime = 0;
      hasStarted = false;
      slider.max = String(scenario.meta.simulation_end_seconds);
      slider.value = '0';
      document.getElementById('metaLists').textContent = scenario.meta.picking_lists;
      document.getElementById('metaWorkers').textContent = scenario.meta.operators;
      document.getElementById('metaDuration').textContent = formatSeconds(scenario.meta.simulation_end_seconds);

      const counts = scenario.meta.worker_counts;
      const availability = availabilityFor(currentDate);
      let allocationText = '';
      if (Array.isArray(counts)) allocationText += `구역 인원: [${{counts.join(', ')}}]`;
      if (scenario.meta.entropy_lambda !== null) allocationText += `${{allocationText ? ' · ' : ''}}λ=${{scenario.meta.entropy_lambda}}`;
      if (!availability.comparison_eligible) {{
        allocationText += `${{allocationText ? ' · ' : ''}}비교방법 사용 불가 · ${{reasonLabel(availability.reason)}}` +
          ` · workers=${{availability.observed_workers}} · active zones=${{availability.active_zones}}`;
      }}
      allocationInfo.textContent = allocationText;

      prepareConflictIndex();
      rebuildWorkers();
      document.getElementById('metaPickTargets').textContent = `${{currentPickTargets.length}}개 포인트`;
      lastUiRenderMs = -Infinity;
      render(true);
      if (autoPlay) start();
    }} catch (error) {{
      if (token === loadToken) dataLoadError(error);
    }}
  }}

  async function advanceDate() {{
    const idx = dates.indexOf(currentDate);
    if (idx < 0 || idx >= dates.length - 1) {{ stop(); return; }}
    if (currentMethod === 'observed') {{
      await loadScenario(dates[idx + 1], 'observed', true);
      return;
    }}
    for (let next = idx + 1; next < dates.length; next++) {{
      if (methodAvailable(dates[next], currentMethod)) {{
        await loadScenario(dates[next], currentMethod, true);
        return;
      }}
    }}
    stop();
  }}

  function tick(ts) {{
    if (!playing) return;
    if (lastTs === null) lastTs = ts;
    const dt = (ts - lastTs) / 1000;
    lastTs = ts;
    currentTime += dt * Number(speedSel.value || 1);
    if (currentTime >= scenario.meta.simulation_end_seconds) {{
      currentTime = scenario.meta.simulation_end_seconds;
      render(true);
      stop();

      // 옵션이 체크되어 있을 때만 같은 방법의 다음 사용 가능 날짜를 자동 재생한다.
      if (autoNextDateChk.checked) {{
        void advanceDate();
      }}
      return;
    }}
    render();
    raf = requestAnimationFrame(tick);
  }}

  playBtn.addEventListener('click', () => playing ? stop() : start());
  speedSel.addEventListener('change', () => {{ lastTs = null; }});
  workerSel.addEventListener('change', e => {{ selectedWorker = e.target.value; render(true); }});
  pickTargetsChk.addEventListener('change', () => {{
    if (pickTargetLayer) pickTargetLayer.style.display = pickTargetsChk.checked ? '' : 'none';
  }});
  zonesChk.addEventListener('change', () => {{
    if (zoneLayer) zoneLayer.style.display = zonesChk.checked ? '' : 'none';
  }});
  slider.addEventListener('input', e => {{
    hasStarted = true;
    currentTime = Number(e.target.value || 0);
    render(false);
  }});
  slider.addEventListener('change', e => {{
    hasStarted = true;
    currentTime = Number(e.target.value || 0);
    render(true);
  }});
  dateSel.addEventListener('change', e => {{ void loadScenario(e.target.value, currentMethod, false); }});
  methodSel.addEventListener('change', e => {{ void loadScenario(currentDate, e.target.value, false); }});

  // 창 크기/반응형 레이아웃 변경 시 Canvas backing store와 SVG 좌표 매핑을 동기화한다.
  if ('ResizeObserver' in window) {{
    const canvasResizeObserver = new ResizeObserver(() => resizeWorkerCanvas());
    canvasResizeObserver.observe(svgStack);
  }} else {{
    window.addEventListener('resize', resizeWorkerCanvas, {{passive: true}});
  }}
  resizeWorkerCanvas();

  if (!dates.length) {{ setStatus('재생 가능한 날짜가 없습니다.', 'error'); }}
  else {{ void loadScenario(currentDate, currentMethod, false); }}
}})();
</script>
</body>
</html>'''

def _month_key(value: date) -> str:
    return value.strftime("%Y-%m")


def _data_directory_for(output_html: Path) -> Path:
    return output_html.with_name(f"{output_html.stem}_data")


def _prepare_data_directory(output_html: Path) -> Path:
    data_dir = _data_directory_for(output_html)
    if data_dir.exists():
        print(f"[CLEAN] Removing previous monthly data directory: {data_dir}")
        shutil.rmtree(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


def _write_month_json(
    *,
    data_dir: Path,
    month_key: str,
    dates_payload: dict[str, Any],
    entropy_lambda: float,
    seed: int,
) -> tuple[str, float]:
    file_name = f"{month_key}.json"
    path = data_dir / file_name
    payload = {
        "meta": {
            "format": "monthly-date-method-json-v7-pick-targets",
            "month": month_key,
            "entropy_lambda": entropy_lambda,
            "seed": seed,
        },
        "dates": dates_payload,
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    size_mb = path.stat().st_size / (1024 * 1024)
    print(f"[JSON ] {month_key} -> {path.name} | dates={len(dates_payload)} | size={size_mb:.1f} MB")
    return file_name, size_mb


def _print_browser_open_instructions(output_html: Path) -> None:
    print("[OPEN ] Monthly JSON is loaded with browser fetch().")
    print("[OPEN ] If file:// is blocked, serve the output directory locally:")
    print(f"[OPEN ]   cd {output_html.parent}")
    print("[OPEN ]   python -m http.server 8000")
    print(f"[OPEN ]   http://localhost:8000/{output_html.name}")


def _remove_legacy_date_directories(output_html: Path) -> None:
    parent = output_html.parent
    if not parent.exists():
        return
    for path in parent.iterdir():
        if path.is_dir() and path.name.endswith("_dates"):
            print(f"[CLEAN] Removing legacy date directory: {path}")
            shutil.rmtree(path)



def _availability_from_month_date_payload(date_payload: dict[str, Any]) -> dict[str, Any]:
    """Return the date-level availability metadata stored in a monthly JSON file.

    v4 monthly JSON files already contain ``__availability__``.  The fallback is
    intentionally conservative so that HTML-only rebuilding can also tolerate an
    older monthly file that has an Observed scenario but no explicit availability
    object.
    """

    availability = date_payload.get("__availability__")
    if isinstance(availability, dict):
        return availability

    observed = date_payload.get("observed")
    observed_workers = 0
    picking_lists = 0
    if isinstance(observed, dict):
        meta = observed.get("meta")
        if isinstance(meta, dict):
            observed_workers = int(meta.get("operators") or 0)
            picking_lists = int(meta.get("picking_lists") or 0)

    available_methods = [
        method for method in METHODS if isinstance(date_payload.get(method), dict)
    ]
    comparison_methods_present = all(
        method in available_methods for method in COMPARISON_METHODS
    )
    return {
        "comparison_eligible": comparison_methods_present,
        "reason": "eligible" if comparison_methods_present else "availability_metadata_missing",
        "active_zones": None,
        "observed_workers": observed_workers,
        "picking_lists": picking_lists,
        "min_lists_per_date": None,
        "available_methods": available_methods or ["observed"],
    }


def regenerate_html_from_existing_json(
    *,
    data_dir: str | Path,
    layout_svg: str | Path,
    output_html: str | Path,
    entropy_lambda: float | None,
) -> Path:
    """Rebuild only the lightweight HTML shell from existing monthly JSON files.

    No simulation is executed and no monthly JSON file is rewritten or deleted.
    The manifest required by the browser is reconstructed by scanning the existing
    ``<output-stem>_data/*.json`` files.
    """

    data_dir = Path(data_dir)
    output_html = Path(output_html)
    output_html.parent.mkdir(parents=True, exist_ok=True)
    monthly_data_dir = _data_directory_for(output_html)

    if not monthly_data_dir.exists():
        raise FileNotFoundError(
            "--html-only에 사용할 월별 JSON 디렉터리가 없습니다: "
            f"{monthly_data_dir}"
        )

    json_paths = sorted(monthly_data_dir.glob("*.json"))
    if not json_paths:
        raise FileNotFoundError(
            "--html-only에 사용할 월별 JSON 파일이 없습니다: "
            f"{monthly_data_dir}"
        )

    print("[MODE ] HTML-only rebuild from existing monthly JSON | canvas-workers-v9.1-transparent-fix")
    print(f"[DATA ] Existing JSON directory: {monthly_data_dir}")
    print(f"[SCAN ] Monthly JSON files: {len(json_paths)}")

    month_files: dict[str, str] = {}
    month_by_date: dict[str, str] = {}
    availability_by_date: dict[str, Any] = {}
    date_values: set[str] = set()
    discovered_lambda: float | None = None
    discovered_seed = 42

    for json_path in json_paths:
        try:
            payload = json.loads(json_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"월별 JSON 파싱 실패: {json_path}") from exc

        if not isinstance(payload, dict):
            raise ValueError(f"월별 JSON 루트가 object가 아닙니다: {json_path}")

        meta = payload.get("meta")
        if not isinstance(meta, dict):
            meta = {}
        month_key = str(meta.get("month") or json_path.stem)
        dates_payload = payload.get("dates")
        if not isinstance(dates_payload, dict):
            raise ValueError(f"월별 JSON의 dates object가 없습니다: {json_path}")

        month_files[month_key] = json_path.name

        if discovered_lambda is None:
            try:
                candidate = float(meta.get("entropy_lambda"))
            except (TypeError, ValueError):
                candidate = float("nan")
            if math.isfinite(candidate) and candidate >= 0:
                discovered_lambda = candidate
        try:
            discovered_seed = int(meta.get("seed", discovered_seed))
        except (TypeError, ValueError):
            pass

        for date_text, date_payload in dates_payload.items():
            if not isinstance(date_payload, dict):
                continue
            date_text = str(date_text)
            # Validate ISO date while preserving the original YYYY-MM-DD string.
            date.fromisoformat(date_text)
            date_values.add(date_text)
            month_by_date[date_text] = month_key
            availability_by_date[date_text] = _availability_from_month_date_payload(
                date_payload
            )

        print(
            f"[SCAN ] {json_path.name:<12} | month={month_key} | "
            f"dates={len(dates_payload)} | size={json_path.stat().st_size / (1024*1024):.1f} MB"
        )

    if not date_values:
        raise ValueError("월별 JSON에서 날짜를 하나도 찾지 못했습니다.")

    if entropy_lambda is not None:
        selected_lambda = float(entropy_lambda)
        if not math.isfinite(selected_lambda) or selected_lambda < 0:
            raise ValueError("--entropy-lambda는 0 이상의 유한한 값이어야 합니다.")
    elif discovered_lambda is not None:
        selected_lambda = discovered_lambda
    else:
        selected_lambda = _read_entropy_lambda(data_dir, None)

    support_path = data_dir / "Support_Points_Navigation.csv"
    if not support_path.exists():
        raise FileNotFoundError(
            "HTML의 SVG 좌표 보정에 필요한 파일이 없습니다: "
            f"{support_path}"
        )
    support_points = load_support_points(support_path)
    transform = parse_svg_axes_transform(
        layout_svg,
        support_points=support_points,
    )

    date_order = sorted(date_values)
    manifest = {
        "meta": {
            "format": "html-manifest-monthly-json-v5-html-only",
            "entropy_lambda": selected_lambda,
            "seed": discovered_seed,
            "coordinate_calibration_points": transform.calibration_points,
            "coordinate_max_residual_px": transform.max_residual_px,
            "months": len(month_files),
        },
        "date_order": date_order,
        "month_by_date": month_by_date,
        "month_files": month_files,
        "availability_by_date": availability_by_date,
    }

    html = render_single_html(
        svg_transform=transform,
        manifest=manifest,
        entropy_lambda=selected_lambda,
        data_dir_name=monthly_data_dir.name,
    )
    output_html.write_text(html, encoding="utf-8")
    html_mb = output_html.stat().st_size / (1024 * 1024)

    print(
        f"[WRITE] HTML only: {output_html} | size={html_mb:.2f} MB | "
        f"dates={len(date_order)} | months={len(month_files)}"
    )
    print("[KEEP ] Existing monthly JSON files were not modified.")
    _print_browser_open_instructions(output_html)
    return output_html

def generate_all_dates_single_html(
    *,
    data_dir: str | Path,
    layout_svg: str | Path,
    output_html: str | Path,
    max_lists: int | None,
    entropy_lambda: float | None,
    seed: int = 42,
    walking_speed_mps: float = 1.2,
    pick_seconds_per_unit: float = 3.0,
    edge_capacity: int = 1,
    pick_node_capacity: int = 1,
) -> Path:
    """Generate one lightweight HTML plus one JSON file per calendar month."""

    data_dir = Path(data_dir)
    output_html = Path(output_html)
    output_html.parent.mkdir(parents=True, exist_ok=True)
    _remove_legacy_date_directories(output_html)
    monthly_data_dir = _prepare_data_directory(output_html)

    print("[MODE ] Lightweight HTML + lazy monthly JSON | monthly-json v9.1 canvas-workers transparent-fix + actual-conflict-events + pick-targets")
    print(f"[LOAD ] Loading dataset: {data_dir}")
    bundle = load_dataset(data_dir)
    print("[GRAPH] Building deterministic warehouse graph")
    warehouse = WarehouseGraph.build(
        bundle.storage_locations,
        bundle.support_points,
        deterministic_order=True,
    )
    transform = parse_svg_axes_transform(layout_svg, support_points=bundle.support_points)
    selected_lambda = _read_entropy_lambda(data_dir, entropy_lambda)

    dates = available_phase2_dates(warehouse, bundle.picking_lists)
    if not dates:
        raise ValueError("애니메이션으로 생성할 fully-valid 날짜가 없습니다.")

    comparison_min_lists = (
        DEFAULT_MIN_LISTS_PER_DATE
        if max_lists is None
        else min(DEFAULT_MIN_LISTS_PER_DATE, max_lists)
    )
    print(
        f"[RULE ] Comparison eligibility | min_lists={comparison_min_lists}, "
        "minimum_per_active_zone=1"
    )

    progress = GenerationProgress(len(dates), METHODS)
    month_files: dict[str, str] = {}
    month_by_date: dict[str, str] = {}
    availability_by_date: dict[str, Any] = {}
    current_month: str | None = None
    current_month_payload: dict[str, Any] = {}
    total_json_mb = 0.0

    def flush_month() -> None:
        nonlocal current_month_payload, total_json_mb
        if current_month is None or not current_month_payload:
            return
        file_name, size_mb = _write_month_json(
            data_dir=monthly_data_dir,
            month_key=current_month,
            dates_payload=current_month_payload,
            entropy_lambda=selected_lambda,
            seed=seed,
        )
        month_files[current_month] = file_name
        total_json_mb += size_mb
        current_month_payload = {}

    for date_index, target_date in enumerate(dates, start=1):
        selected_date, selected_lists = select_phase2_lists(
            warehouse,
            bundle.picking_lists,
            target_date=target_date,
            max_lists=max_lists,
        )
        month_key = _month_key(selected_date)
        if current_month is not None and month_key != current_month:
            flush_month()
        current_month = month_key

        methods_data = _simulate_date_methods(
            warehouse=warehouse,
            selected_date=selected_date,
            selected_lists=selected_lists,
            svg_transform=transform,
            methods=METHODS,
            entropy_lambda=selected_lambda,
            seed=seed,
            walking_speed_mps=walking_speed_mps,
            pick_seconds_per_unit=pick_seconds_per_unit,
            edge_capacity=edge_capacity,
            pick_node_capacity=pick_node_capacity,
            progress=progress,
            date_index=date_index,
            min_lists_per_date=comparison_min_lists,
        )
        date_text = selected_date.isoformat()
        current_month_payload[date_text] = methods_data
        month_by_date[date_text] = month_key
        availability_by_date[date_text] = methods_data["__availability__"]

    flush_month()

    manifest = {
        "meta": {
            "format": "html-manifest-monthly-json-v7-pick-targets",
            "entropy_lambda": selected_lambda,
            "seed": seed,
            "coordinate_calibration_points": transform.calibration_points,
            "coordinate_max_residual_px": transform.max_residual_px,
            "months": len(month_files),
        },
        "date_order": [value.isoformat() for value in dates],
        "month_by_date": month_by_date,
        "month_files": month_files,
        "availability_by_date": availability_by_date,
    }

    print(f"[PACK ] Writing lightweight HTML manifest | months={len(month_files)}")
    html = render_single_html(
        svg_transform=transform,
        manifest=manifest,
        entropy_lambda=selected_lambda,
        data_dir_name=monthly_data_dir.name,
    )
    output_html.write_text(html, encoding="utf-8")
    html_mb = output_html.stat().st_size / (1024 * 1024)
    elapsed = time.monotonic() - progress.started
    print(f"[WRITE] HTML  : {output_html} | size={html_mb:.2f} MB")
    print(f"[WRITE] JSON  : {monthly_data_dir} | months={len(month_files)} | total={total_json_mb:.1f} MB")
    print(f"[DONE ] dates={len(dates)} | elapsed={_format_duration(elapsed)}")
    print("[DONE ] Simulation data is no longer embedded in the HTML.")
    _print_browser_open_instructions(output_html)
    return output_html


def generate_single_date_html(
    *,
    data_dir: str | Path,
    layout_svg: str | Path,
    output_html: str | Path,
    target_date: date,
    max_lists: int | None,
    entropy_lambda: float | None,
    seed: int = 42,
    walking_speed_mps: float = 1.2,
    pick_seconds_per_unit: float = 3.0,
    edge_capacity: int = 1,
    pick_node_capacity: int = 1,
) -> Path:
    data_dir = Path(data_dir)
    output_html = Path(output_html)
    output_html.parent.mkdir(parents=True, exist_ok=True)
    _remove_legacy_date_directories(output_html)
    monthly_data_dir = _prepare_data_directory(output_html)

    print("[MODE ] Lightweight HTML + external monthly JSON | single-date")
    bundle = load_dataset(data_dir)
    warehouse = WarehouseGraph.build(
        bundle.storage_locations,
        bundle.support_points,
        deterministic_order=True,
    )
    transform = parse_svg_axes_transform(layout_svg, support_points=bundle.support_points)
    selected_lambda = _read_entropy_lambda(data_dir, entropy_lambda)
    selected_date, selected_lists = select_phase2_lists(
        warehouse,
        bundle.picking_lists,
        target_date=target_date,
        max_lists=max_lists,
    )
    comparison_min_lists = (
        DEFAULT_MIN_LISTS_PER_DATE
        if max_lists is None
        else min(DEFAULT_MIN_LISTS_PER_DATE, max_lists)
    )
    progress = GenerationProgress(1, METHODS)
    methods_data = _simulate_date_methods(
        warehouse=warehouse,
        selected_date=selected_date,
        selected_lists=selected_lists,
        svg_transform=transform,
        methods=METHODS,
        entropy_lambda=selected_lambda,
        seed=seed,
        walking_speed_mps=walking_speed_mps,
        pick_seconds_per_unit=pick_seconds_per_unit,
        edge_capacity=edge_capacity,
        pick_node_capacity=pick_node_capacity,
        progress=progress,
        date_index=1,
        min_lists_per_date=comparison_min_lists,
    )

    date_text = selected_date.isoformat()
    month_key = _month_key(selected_date)
    file_name, _ = _write_month_json(
        data_dir=monthly_data_dir,
        month_key=month_key,
        dates_payload={date_text: methods_data},
        entropy_lambda=selected_lambda,
        seed=seed,
    )
    manifest = {
        "meta": {
            "format": "html-manifest-monthly-json-v7-pick-targets",
            "entropy_lambda": selected_lambda,
            "seed": seed,
            "months": 1,
        },
        "date_order": [date_text],
        "month_by_date": {date_text: month_key},
        "month_files": {month_key: file_name},
        "availability_by_date": {date_text: methods_data["__availability__"]},
    }
    output_html.write_text(
        render_single_html(
            svg_transform=transform,
            manifest=manifest,
            entropy_lambda=selected_lambda,
            data_dir_name=monthly_data_dir.name,
        ),
        encoding="utf-8",
    )
    print(f"[DONE ] Single-date HTML saved to: {output_html}")
    _print_browser_open_instructions(output_html)
    return output_html


def _serve_output(output_html: Path, port: int) -> None:
    """Serve the generated HTML/JSON folder so browser fetch() works locally."""

    import functools
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
    import webbrowser

    if port <= 0 or port > 65535:
        raise ValueError("--port는 1~65535 범위여야 합니다.")

    directory = output_html.parent.resolve()
    handler = functools.partial(SimpleHTTPRequestHandler, directory=str(directory))
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    url = f"http://127.0.0.1:{port}/{output_html.name}"
    print(f"[SERVE] directory={directory}")
    print(f"[SERVE] {url}")
    print("[SERVE] Ctrl+C to stop.")
    webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[SERVE] stopped")
    finally:
        server.server_close()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Generate one lightweight warehouse animation HTML plus monthly JSON data "
            "for Observed/Equal/Random/Volume/Entropy switching."
        )
    )
    parser.add_argument("--data-dir", default="data/raw")
    parser.add_argument("--layout-svg", default="data/raw_original/Layout_Z1.0.svg")
    parser.add_argument("--output-html", default="results/figures/picking_animation_actual.html")
    parser.add_argument("--date", default=None, help="YYYY-MM-DD; omit with --all-dates")
    parser.add_argument("--all-dates", action="store_true")
    parser.add_argument("--max-lists", type=int, default=None, help="quick-test limit per date")
    parser.add_argument("--entropy-lambda", type=float, default=None, help="override Phase 4 λ*")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--walking-speed-mps", type=float, default=1.2)
    parser.add_argument("--pick-seconds-per-unit", type=float, default=3.0)
    parser.add_argument("--edge-capacity", type=int, default=1)
    parser.add_argument("--pick-node-capacity", type=int, default=1)
    parser.add_argument(
        "--html-only",
        action="store_true",
        help="rebuild only HTML from existing monthly JSON; do not rerun simulation or rewrite JSON",
    )
    parser.add_argument("--serve", action="store_true", help="serve generated HTML/JSON over localhost")
    parser.add_argument("--port", type=int, default=8000, help="localhost port used with --serve")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()

    if args.html_only:
        if args.all_dates or args.date is not None:
            raise ValueError("--html-only는 --all-dates/--date와 함께 사용하지 않습니다.")
        output = regenerate_html_from_existing_json(
            data_dir=args.data_dir,
            layout_svg=args.layout_svg,
            output_html=args.output_html,
            entropy_lambda=args.entropy_lambda,
        )
        if args.serve:
            _serve_output(output, args.port)
        return

    if args.all_dates and args.date is not None:
        raise ValueError("--all-dates와 --date는 동시에 사용할 수 없습니다.")
    if not args.all_dates and args.date is None:
        raise ValueError(
            "--all-dates, --date YYYY-MM-DD 또는 --html-only 중 하나를 지정해 주세요."
        )

    common = dict(
        data_dir=args.data_dir,
        layout_svg=args.layout_svg,
        output_html=args.output_html,
        max_lists=args.max_lists,
        entropy_lambda=args.entropy_lambda,
        seed=args.seed,
        walking_speed_mps=args.walking_speed_mps,
        pick_seconds_per_unit=args.pick_seconds_per_unit,
        edge_capacity=args.edge_capacity,
        pick_node_capacity=args.pick_node_capacity,
    )
    if args.all_dates:
        output = generate_all_dates_single_html(**common)
    else:
        output = generate_single_date_html(target_date=date.fromisoformat(args.date), **common)

    if args.serve:
        _serve_output(output, args.port)


if __name__ == "__main__":
    main()
