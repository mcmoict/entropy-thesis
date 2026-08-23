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

from ..simulation.data_loader import DEFAULT_COORDINATE_UNIT, coordinate_scale_to_meter, load_dataset
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
from ..simulation.phase4 import allocate_phase4_workers
from ..simulation.warehouse import WarehouseGraph


METHODS: tuple[str, ...] = ("observed", "equal", "random", "volume", "entropy")
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


def build_animation_payload(
    *,
    warehouse: WarehouseGraph,
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

    workers_payload: list[dict[str, Any]] = []
    for worker_id in sorted(workers):
        worker = workers[worker_id]
        segments = _timeline_for_worker(
            worker,
            node_raw=node_raw,
            simulation_end_seconds=simulation_end_seconds,
            default_start_node=default_start_node,
        )
        svg_segments: list[dict[str, Any]] = []
        for segment in segments:
            sx0, sy0 = svg_transform.raw_to_svg(segment["x0"], segment["y0"])
            sx1, sy1 = svg_transform.raw_to_svg(segment["x1"], segment["y1"])
            item = dict(segment)
            item.update({"sx0": sx0, "sy0": sy0, "sx1": sx1, "sy1": sy1})
            svg_segments.append(item)

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

    return {
        "meta": {
            "selected_date": selected_date.isoformat(),
            "method": method,
            "method_label": METHOD_LABELS[method],
            "picking_lists": len(selected_lists),
            "operators": len(workers_payload),
            "simulation_end_seconds": round(float(simulation_end_seconds), 3),
            "default_start_node": default_start_node,
            "worker_counts": list(worker_counts) if worker_counts is not None else None,
            "entropy_lambda": entropy_lambda,
        },
        "workers": workers_payload,
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

    result: dict[str, Any] = {}
    for method_index, method in enumerate(methods, start=1):
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
    all_data: dict[str, Any],
    entropy_lambda: float,
) -> str:
    data_json = json.dumps(all_data, ensure_ascii=False, separators=(",", ":"))
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
  #overlay .marker circle {{ stroke: #fff; stroke-width: 2.2; }}
  #overlay .marker text {{ font-size: 18px; font-weight: 700; text-anchor: middle; dominant-baseline: middle; fill: #111827; pointer-events: none; }}
  .controls {{ display: grid; grid-template-columns: auto 76px 150px minmax(120px,1fr) 74px; gap: 9px; align-items: center; padding: 8px 0 0; }}
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
    </main>

    <aside class="sidebar">
      <div class="kv">
        <div class="key">날짜</div><div class="value"><select id="dateSel" aria-label="날짜 선택"></select></div>
        <div class="key">방법</div><div class="value"><select id="methodSel" aria-label="배치 방법">{method_options}</select></div>
        <div class="key">피킹리스트 수</div><div class="value" id="metaLists"></div>
        <div class="key">작업자 수</div><div class="value" id="metaWorkers"></div>
        <div class="key">총 재생시간</div><div class="value" id="metaDuration"></div>
      </div>
      <div class="allocation" id="allocationInfo"></div>
      <div class="status" id="statusBox"></div>
      <div class="notes">
        - 원형 마커는 작업자 현재 위치입니다.<br />
        - 실제 SVG support marker로 좌표를 자동 보정합니다.<br />
        - 이동 중에는 graph edge를 따라 선형 보간합니다.<br />
        - 날짜가 끝나면 같은 방법으로 다음 날짜가 자동 재생됩니다.<br />
        - Entropy는 λ*={entropy_lambda:g}를 사용합니다.
      </div>
      <div class="workers" id="workerList"></div>
    </aside>
  </div>
</div>
<script id="animationData" type="application/json">{data_json}</script>
<script>
(() => {{
  const rootData = JSON.parse(document.getElementById('animationData').textContent);
  const dates = rootData.date_order;
  const scenarios = rootData.dates;
  const overlay = document.getElementById('overlay');
  const dateSel = document.getElementById('dateSel');
  const methodSel = document.getElementById('methodSel');
  const workerSel = document.getElementById('workerSel');
  const speedSel = document.getElementById('speedSel');
  const playBtn = document.getElementById('playBtn');
  const slider = document.getElementById('timeSlider');
  const timeLabel = document.getElementById('timeLabel');
  const workerList = document.getElementById('workerList');
  const statusBox = document.getElementById('statusBox');
  const allocationInfo = document.getElementById('allocationInfo');

  dates.forEach(d => {{
    const opt = document.createElement('option');
    opt.value = d; opt.textContent = d; dateSel.appendChild(opt);
  }});

  let currentDate = dates[0];
  let currentMethod = 'observed';
  let scenario = null;
  let currentTime = 0;
  let playing = false;
  let raf = null;
  let lastTs = null;
  let selectedWorker = 'ALL';
  let markerMap = new Map();
  let workerIndices = new Map();

  function formatSeconds(value) {{
    const total = Math.max(0, Math.floor(Number(value) || 0));
    const h = String(Math.floor(total / 3600)).padStart(2, '0');
    const m = String(Math.floor((total % 3600) / 60)).padStart(2, '0');
    const s = String(total % 60).padStart(2, '0');
    return `${{h}}:${{m}}:${{s}}`;
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
    playing = true;
    playBtn.textContent = '⏸ 일시정지';
    lastTs = null;
    raf = requestAnimationFrame(tick);
  }}

  function rebuildWorkers() {{
    overlay.innerHTML = '';
    workerSel.innerHTML = '<option value="ALL">전체 작업자</option>';
    workerList.innerHTML = '';
    markerMap = new Map();
    workerIndices = new Map();

    scenario.workers.forEach((worker, index) => {{
      workerIndices.set(worker.worker_id, 0);
      const color = colorForIndex(index);
      const g = svgNode('g'); g.setAttribute('class', 'marker');
      const c = svgNode('circle'); c.setAttribute('r', '12'); c.setAttribute('fill', color);
      const t = svgNode('text'); t.textContent = shortWorkerLabel(worker.worker_id, index);
      g.appendChild(c); g.appendChild(t); overlay.appendChild(g);
      markerMap.set(worker.worker_id, {{ g, c, t }});

      const option = document.createElement('option');
      option.value = worker.worker_id; option.textContent = worker.worker_id; workerSel.appendChild(option);

      const row = document.createElement('div'); row.className = 'worker-row';
      row.innerHTML = `<div class="dot" style="background:${{color}}"></div>` +
        `<div><div class="worker-name" title="${{worker.worker_id}}">${{worker.worker_id}}</div>` +
        `<div class="worker-sub">move=${{worker.movement_events}}, pick=${{worker.pick_events}}</div></div>` +
        `<div class="distance">${{worker.total_distance_m.toFixed(1)}} m</div>`;
      workerList.appendChild(row);
    }});
    selectedWorker = 'ALL';
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

  function render() {{
    if (!scenario) return;
    slider.value = String(currentTime);
    timeLabel.textContent = formatSeconds(currentTime);
    const states = [];

    scenario.workers.forEach(worker => {{
      const marker = markerMap.get(worker.worker_id);
      const visible = selectedWorker === 'ALL' || selectedWorker === worker.worker_id;
      marker.g.style.display = visible ? '' : 'none';
      if (!visible) return;
      const seg = findSegment(worker, currentTime);
      const p = position(seg, currentTime);
      marker.c.setAttribute('cx', p.x.toFixed(3)); marker.c.setAttribute('cy', p.y.toFixed(3));
      marker.t.setAttribute('x', p.x.toFixed(3)); marker.t.setAttribute('y', p.y.toFixed(3));
      marker.g.setAttribute('opacity', seg.kind === 'idle' ? '0.68' : '1');
      states.push(`${{worker.worker_id}}(${{seg.kind}})`);
    }});

    statusBox.innerHTML = `<strong>현재 시간</strong> : ${{formatSeconds(currentTime)}}<br>` +
      `<strong>표시 작업자</strong> : ${{selectedWorker === 'ALL' ? '전체' : selectedWorker}}<br>` +
      `<strong>활성 마커 수</strong> : ${{states.length}}<br>` +
      `<strong>상태</strong> : ${{states.slice(0, 7).join(', ')}}${{states.length > 7 ? ' ...' : ''}}`;
  }}

  function loadScenario(dateValue, methodValue, autoPlay) {{
    stop();
    currentDate = dateValue;
    currentMethod = methodValue;
    scenario = scenarios[currentDate][currentMethod];
    dateSel.value = currentDate;
    methodSel.value = currentMethod;
    currentTime = 0;
    slider.max = String(scenario.meta.simulation_end_seconds);
    slider.value = '0';
    document.getElementById('metaLists').textContent = scenario.meta.picking_lists;
    document.getElementById('metaWorkers').textContent = scenario.meta.operators;
    document.getElementById('metaDuration').textContent = formatSeconds(scenario.meta.simulation_end_seconds);
    const counts = scenario.meta.worker_counts;
    let allocationText = '';
    if (Array.isArray(counts)) allocationText += `구역 인원: [${{counts.join(', ')}}]`;
    if (scenario.meta.entropy_lambda !== null) allocationText += `${{allocationText ? ' · ' : ''}}λ=${{scenario.meta.entropy_lambda}}`;
    allocationInfo.textContent = allocationText;
    rebuildWorkers();
    render();
    if (autoPlay) start();
  }}

  function advanceDate() {{
    const idx = dates.indexOf(currentDate);
    if (idx < 0 || idx >= dates.length - 1) {{ stop(); return; }}
    loadScenario(dates[idx + 1], currentMethod, true);
  }}

  function tick(ts) {{
    if (!playing) return;
    if (lastTs === null) lastTs = ts;
    const dt = (ts - lastTs) / 1000;
    lastTs = ts;
    currentTime += dt * Number(speedSel.value || 1);
    if (currentTime >= scenario.meta.simulation_end_seconds) {{
      currentTime = scenario.meta.simulation_end_seconds;
      render();
      advanceDate();
      return;
    }}
    render();
    raf = requestAnimationFrame(tick);
  }}

  playBtn.addEventListener('click', () => playing ? stop() : start());
  speedSel.addEventListener('change', () => {{ lastTs = null; }});
  workerSel.addEventListener('change', e => {{ selectedWorker = e.target.value; render(); }});
  slider.addEventListener('input', e => {{ currentTime = Number(e.target.value || 0); render(); }});
  dateSel.addEventListener('change', e => loadScenario(e.target.value, currentMethod, true));
  methodSel.addEventListener('change', e => loadScenario(currentDate, e.target.value, true));

  loadScenario(currentDate, currentMethod, false);
}})();
</script>
</body>
</html>'''


def _remove_legacy_date_directories(output_html: Path) -> None:
    parent = output_html.parent
    if not parent.exists():
        return
    for path in parent.iterdir():
        if path.is_dir() and path.name.endswith("_dates"):
            print(f"[CLEAN] Removing legacy date directory: {path}")
            shutil.rmtree(path)


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
    data_dir = Path(data_dir)
    output_html = Path(output_html)
    output_html.parent.mkdir(parents=True, exist_ok=True)
    _remove_legacy_date_directories(output_html)

    print("[MODE ] Single HTML + embedded date/method JSON (no per-date HTML)")
    print(f"[LOAD ] Loading dataset: {data_dir}")
    bundle = load_dataset(data_dir)
    print("[GRAPH] Building deterministic warehouse graph")
    warehouse = WarehouseGraph.build(
        bundle.storage_locations,
        bundle.support_points,
        deterministic_order=True,
    )
    transform = parse_svg_axes_transform(
        layout_svg,
        support_points=bundle.support_points,
    )
    selected_lambda = _read_entropy_lambda(data_dir, entropy_lambda)

    dates = available_phase2_dates(warehouse, bundle.picking_lists)
    if not dates:
        raise ValueError("애니메이션으로 생성할 fully-valid 날짜가 없습니다.")

    progress = GenerationProgress(len(dates), METHODS)
    date_payloads: dict[str, Any] = {}
    for date_index, target_date in enumerate(dates, start=1):
        selected_date, selected_lists = select_phase2_lists(
            warehouse,
            bundle.picking_lists,
            target_date=target_date,
            max_lists=max_lists,
        )
        date_payloads[selected_date.isoformat()] = _simulate_date_methods(
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
        )

    all_data = {
        "meta": {
            "format": "single-html-date-method-json-v2",
            "entropy_lambda": selected_lambda,
            "seed": seed,
            "coordinate_calibration_points": transform.calibration_points,
            "coordinate_max_residual_px": transform.max_residual_px,
        },
        "date_order": [value.isoformat() for value in dates],
        "dates": date_payloads,
    }

    print(f"[PACK ] Serializing {len(dates)} dates × {len(METHODS)} methods into one HTML")
    html = render_single_html(
        svg_transform=transform,
        all_data=all_data,
        entropy_lambda=selected_lambda,
    )
    output_html.write_text(html, encoding="utf-8")
    size_mb = output_html.stat().st_size / (1024 * 1024)
    elapsed = time.monotonic() - progress.started
    print(f"[WRITE] {output_html} | size={size_mb:.1f} MB")
    print(f"[DONE ] {len(dates)} dates × {len(METHODS)} methods | elapsed={_format_duration(elapsed)}")
    print("[DONE ] Exactly one HTML was generated; all scenario data is embedded JSON.")
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
    )
    all_data = {
        "meta": {"format": "single-html-date-method-json-v2", "entropy_lambda": selected_lambda, "seed": seed},
        "date_order": [selected_date.isoformat()],
        "dates": {selected_date.isoformat(): methods_data},
    }
    output_html.write_text(
        render_single_html(svg_transform=transform, all_data=all_data, entropy_lambda=selected_lambda),
        encoding="utf-8",
    )
    print(f"[DONE ] Single-date HTML saved to: {output_html}")
    return output_html


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Generate a single interactive warehouse picking animation HTML with "
            "Observed/Equal/Random/Volume/Entropy method switching."
        )
    )
    parser.add_argument("--data-dir", default="data/raw")
    parser.add_argument("--layout-svg", default="data/raw_original/Layout_Z1.0.svg")
    parser.add_argument("--output-html", default="results/figures/observed_picking_animation.html")
    parser.add_argument("--date", default=None, help="YYYY-MM-DD; omit with --all-dates")
    parser.add_argument("--all-dates", action="store_true")
    parser.add_argument("--max-lists", type=int, default=None, help="quick-test limit per date")
    parser.add_argument("--entropy-lambda", type=float, default=None, help="override Phase 4 λ*")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--walking-speed-mps", type=float, default=1.2)
    parser.add_argument("--pick-seconds-per-unit", type=float, default=3.0)
    parser.add_argument("--edge-capacity", type=int, default=1)
    parser.add_argument("--pick-node-capacity", type=int, default=1)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    if args.all_dates and args.date is not None:
        raise ValueError("--all-dates와 --date는 동시에 사용할 수 없습니다.")
    if not args.all_dates and args.date is None:
        raise ValueError("--all-dates 또는 --date YYYY-MM-DD 중 하나를 지정해 주세요.")

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
        generate_all_dates_single_html(**common)
    else:
        generate_single_date_html(target_date=date.fromisoformat(args.date), **common)


if __name__ == "__main__":
    main()
