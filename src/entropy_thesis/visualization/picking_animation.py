from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date
import json
from pathlib import Path
import re
from typing import Any
import xml.etree.ElementTree as ET

from ..simulation.data_loader import DEFAULT_COORDINATE_UNIT, coordinate_scale_to_meter, load_dataset
from ..simulation.phase2 import available_phase2_dates, run_phase2_simulation, select_phase2_lists
from ..simulation.warehouse import WarehouseGraph


@dataclass(frozen=True)
class SvgAxesTransform:
    svg_markup: str
    view_box: str
    axes_left: float
    axes_right: float
    axes_top: float
    axes_bottom: float
    raw_x_min: float
    raw_x_max: float
    raw_y_min: float
    raw_y_max: float

    def raw_to_svg(self, raw_x: float, raw_y: float) -> tuple[float, float]:
        x_span = self.raw_x_max - self.raw_x_min
        y_span = self.raw_y_max - self.raw_y_min
        if x_span <= 0 or y_span <= 0:
            raise ValueError("잘못된 raw 좌표 범위입니다.")

        sx = self.axes_left + ((raw_x - self.raw_x_min) / x_span) * (
            self.axes_right - self.axes_left
        )
        sy = self.axes_bottom - ((raw_y - self.raw_y_min) / y_span) * (
            self.axes_bottom - self.axes_top
        )
        return float(sx), float(sy)


def _strip_xml_declaration(svg_text: str) -> str:
    return re.sub(r"^<\?xml[^>]*>\s*", "", svg_text, count=1)


def parse_svg_axes_transform(
    svg_path: str | Path,
    *,
    raw_x_min: float,
    raw_x_max: float,
    raw_y_min: float,
    raw_y_max: float,
) -> SvgAxesTransform:
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

    axes_path = None
    for element in root.iter():
        if element.attrib.get("id") == "path2":
            axes_path = element
            break

    if axes_path is None:
        raise ValueError(
            "SVG에서 axes drawing area(path2)를 찾지 못했습니다. "
            "Layout_Z1.0.svg의 구조를 확인해 주세요."
        )

    path_d = axes_path.attrib.get("d")
    if not path_d:
        raise ValueError("SVG의 path2에서 d 좌표 정보를 찾지 못했습니다.")

    nums = [
        float(value)
        for value in re.findall(r"[-+]?(?:\d*\.\d+|\d+)", path_d)
    ]
    if len(nums) < 8:
        raise ValueError(f"SVG axes path 좌표 해석에 실패했습니다: {path_d}")

    xs = [nums[0], nums[2], nums[4], nums[6]]
    ys = [nums[1], nums[3], nums[5], nums[7]]
    axes_left = min(xs)
    axes_right = max(xs)
    axes_top = min(ys)
    axes_bottom = max(ys)

    print(
        "[SVG] Layout axes detected | "
        f"x={axes_left:.1f}..{axes_right:.1f}, "
        f"y={axes_top:.1f}..{axes_bottom:.1f}"
    )
    print(
        "[SVG] Warehouse raw coordinates | "
        f"x={raw_x_min:.1f}..{raw_x_max:.1f}, "
        f"y={raw_y_min:.1f}..{raw_y_max:.1f}"
    )

    return SvgAxesTransform(
        svg_markup=svg_text,
        view_box=view_box_match.group(1),
        axes_left=axes_left,
        axes_right=axes_right,
        axes_top=axes_top,
        axes_bottom=axes_bottom,
        raw_x_min=raw_x_min,
        raw_x_max=raw_x_max,
        raw_y_min=raw_y_min,
        raw_y_max=raw_y_max,
    )


def _node_raw_coordinates(warehouse: WarehouseGraph) -> dict[str, tuple[float, float]]:
    scale = coordinate_scale_to_meter(DEFAULT_COORDINATE_UNIT)
    result: dict[str, tuple[float, float]] = {}
    for node_id, attrs in warehouse.graph.nodes(data=True):
        result[node_id] = (float(attrs["x_m"]) / scale, float(attrs["y_m"]) / scale)
    return result


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

    segments: list[dict[str, Any]] = []
    current_time = 0.0
    current_x, current_y = node_raw[default_start_node]

    if not events:
        return [
            {
                "kind": "idle",
                "t0": 0.0,
                "t1": float(simulation_end_seconds),
                "x0": current_x,
                "y0": current_y,
                "x1": current_x,
                "y1": current_y,
                "wave_number": None,
            }
        ]

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
        current_time = event["t1"]
        current_x = event["x1"]
        current_y = event["y1"]

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

    return segments


def build_animation_payload(
    *,
    warehouse: WarehouseGraph,
    workers: dict[str, Any],
    selected_lists: list[Any],
    selected_date: date,
    simulation_end_seconds: float,
    svg_transform: SvgAxesTransform,
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
        svg_segments = []
        for seg in segments:
            sx0, sy0 = svg_transform.raw_to_svg(seg["x0"], seg["y0"])
            sx1, sy1 = svg_transform.raw_to_svg(seg["x1"], seg["y1"])
            svg_seg = dict(seg)
            svg_seg.update({"sx0": sx0, "sy0": sy0, "sx1": sx1, "sy1": sy1})
            svg_segments.append(svg_seg)

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
            "picking_lists": len(selected_lists),
            "operators": len(workers_payload),
            "simulation_end_seconds": round(float(simulation_end_seconds), 3),
            "default_start_node": default_start_node,
        },
        "workers": workers_payload,
    }


def render_animation_html(
    *,
    svg_transform: SvgAxesTransform,
    payload: dict[str, Any],
    title: str,
) -> str:
    data_json = json.dumps(payload, ensure_ascii=False)

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{title}</title>
  <style>
    body {{ font-family: Arial, sans-serif; margin: 0; background: #f6f8fb; color: #1f2937; }}
    .page {{ max-width: 1600px; margin: 0 auto; padding: 20px; }}
    .title {{ margin-bottom: 14px; }}
    .title h1 {{ margin: 0 0 6px 0; font-size: 24px; }}
    .title p {{ margin: 0; color: #4b5563; }}
    .layout {{ display: grid; grid-template-columns: minmax(0, 1fr) 320px; gap: 16px; align-items: start; }}
    .panel, .sidebar {{ background: white; border-radius: 14px; box-shadow: 0 4px 16px rgba(0,0,0,0.06); }}
    .panel {{ padding: 12px; }}
    .sidebar {{ padding: 16px; }}
    #svg-stack {{ position: relative; width: 100%; aspect-ratio: 3 / 2; overflow: hidden; border-radius: 10px; background: white; }}
    #svg-stack svg {{ position: absolute; inset: 0; width: 100%; height: 100%; }}
    #overlay .marker circle {{ stroke: #fff; stroke-width: 2; }}
    #overlay .marker text {{ font-size: 20px; font-weight: 700; text-anchor: middle; dominant-baseline: middle; fill: #111827; pointer-events: none; }}
    .controls {{ display: grid; grid-template-columns: auto auto auto 1fr auto; gap: 12px; align-items: center; margin-top: 14px; }}
    button, select {{ border: 1px solid #d1d5db; background: white; border-radius: 10px; padding: 8px 12px; font-size: 14px; }}
    input[type='range'] {{ width: 100%; }}
    .kv {{ display: grid; grid-template-columns: 110px 1fr; row-gap: 10px; column-gap: 8px; font-size: 14px; }}
    .kv .label {{ color: #6b7280; }}
    .legend {{ margin-top: 14px; font-size: 13px; color: #4b5563; line-height: 1.5; }}
    #worker-list {{ margin-top: 14px; max-height: 420px; overflow: auto; border-top: 1px solid #e5e7eb; padding-top: 12px; }}
    .worker-row {{ display: grid; grid-template-columns: 18px 1fr auto; align-items: center; gap: 8px; font-size: 13px; padding: 5px 0; }}
    .dot {{ width: 12px; height: 12px; border-radius: 999px; }}
    .small {{ color: #6b7280; font-size: 12px; }}
    .status-box {{ margin-top: 14px; padding: 12px; background: #f9fafb; border-radius: 10px; font-size: 13px; line-height: 1.5; }}
  </style>
</head>
<body>
  <div class="page">
    <div class="title">
      <h1>{title}</h1>
      <p>Layout_Z1.0.svg 배경 위에 Picking_Wave 기반 observed path를 시간순으로 재생합니다.</p>
    </div>
    <div class="layout">
      <div class="panel">
        <div id="svg-stack">
          {svg_transform.svg_markup}
          <svg id="overlay" viewBox="{svg_transform.view_box}" preserveAspectRatio="xMidYMid meet"></svg>
        </div>
        <div class="controls">
          <button id="playBtn">▶ 재생</button>
          <select id="speedSel">
            <option value="0.5">0.5x</option>
            <option value="1" selected>1x</option>
            <option value="5">5x</option>
            <option value="10">10x</option>
            <option value="20">20x</option>
            <option value="50">50x</option>
          </select>
          <select id="workerSel">
            <option value="ALL">전체 작업자</option>
          </select>
          <input id="timeSlider" type="range" min="0" max="1" step="0.1" value="0" />
          <div id="timeLabel">00:00:00</div>
        </div>
      </div>
      <aside class="sidebar">
        <div class="kv">
          <div class="label">날짜</div><div id="meta-date"></div>
          <div class="label">피킹리스트 수</div><div id="meta-lists"></div>
          <div class="label">작업자 수</div><div id="meta-workers"></div>
          <div class="label">총 재생시간</div><div id="meta-duration"></div>
        </div>
        <div class="status-box" id="statusBox"></div>
        <div class="legend">
          - 원형 마커는 작업자 현재 위치입니다.<br/>
          - 이동 중에는 선형 보간으로 통로를 따라 움직입니다.<br/>
          - Picking 구간에서는 해당 위치에 정지합니다.<br/>
          - 특정 작업자만 선택하여 경로를 집중적으로 볼 수 있습니다.
        </div>
        <div id="worker-list"></div>
      </aside>
    </div>
  </div>

  <script id="animation-data" type="application/json">{data_json}</script>
  <script>
    const data = JSON.parse(document.getElementById('animation-data').textContent);
    const duration = data.meta.simulation_end_seconds;
    const overlay = document.getElementById('overlay');
    const playBtn = document.getElementById('playBtn');
    const speedSel = document.getElementById('speedSel');
    const workerSel = document.getElementById('workerSel');
    const timeSlider = document.getElementById('timeSlider');
    const timeLabel = document.getElementById('timeLabel');
    const statusBox = document.getElementById('statusBox');
    let currentTime = 0;
    let playing = false;
    let lastTimestamp = null;
    let animationHandle = null;
    let selectedWorker = 'ALL';

    timeSlider.max = String(duration);
    document.getElementById('meta-date').textContent = data.meta.selected_date;
    document.getElementById('meta-lists').textContent = String(data.meta.picking_lists);
    document.getElementById('meta-workers').textContent = String(data.meta.operators);
    document.getElementById('meta-duration').textContent = formatSeconds(duration);

    function colorForIndex(index) {{
      const hue = (index * 47) % 360;
      return `hsl(${{hue}} 72% 55%)`;
    }}

    const markers = new Map();
    const workerState = new Map();

    function createSvg(tag) {{
      return document.createElementNS('http://www.w3.org/2000/svg', tag);
    }}

    data.workers.forEach((worker, index) => {{
      const group = createSvg('g');
      group.setAttribute('class', 'marker');
      group.dataset.workerId = worker.worker_id;
      const circle = createSvg('circle');
      circle.setAttribute('r', '13');
      circle.setAttribute('fill', colorForIndex(index));
      const text = createSvg('text');
      text.textContent = worker.worker_id.replace('Operator_', '');
      group.appendChild(circle);
      group.appendChild(text);
      overlay.appendChild(group);
      markers.set(worker.worker_id, {{ group, circle, text, color: colorForIndex(index) }});
      workerState.set(worker.worker_id, 0);

      const opt = document.createElement('option');
      opt.value = worker.worker_id;
      opt.textContent = worker.worker_id;
      workerSel.appendChild(opt);
    }});

    const workerList = document.getElementById('worker-list');
    data.workers.forEach((worker, index) => {{
      const row = document.createElement('div');
      row.className = 'worker-row';
      row.innerHTML = `<div class="dot" style="background:${{colorForIndex(index)}}"></div><div><div>${{worker.worker_id}}</div><div class="small">move=${{worker.movement_events}}, pick=${{worker.pick_events}}</div></div><div>${{worker.total_distance_m.toFixed(1)}} m</div>`;
      workerList.appendChild(row);
    }});

    function formatSeconds(totalSeconds) {{
      const t = Math.max(0, Math.floor(totalSeconds));
      const h = String(Math.floor(t / 3600)).padStart(2, '0');
      const m = String(Math.floor((t % 3600) / 60)).padStart(2, '0');
      const s = String(t % 60).padStart(2, '0');
      return `${{h}}:${{m}}:${{s}}`;
    }}

    function segmentAtTime(worker, timeSec) {{
      let idx = workerState.get(worker.worker_id) || 0;
      const segs = worker.segments;
      while (idx > 0 && timeSec < segs[idx].t0) idx -= 1;
      while (idx < segs.length - 1 && timeSec > segs[idx].t1) idx += 1;
      workerState.set(worker.worker_id, idx);
      return segs[idx];
    }}

    function positionFromSegment(seg, timeSec) {{
      if (seg.t1 <= seg.t0 || seg.kind !== 'move') {{
        return {{ x: seg.sx1, y: seg.sy1 }};
      }}
      const ratio = Math.max(0, Math.min(1, (timeSec - seg.t0) / (seg.t1 - seg.t0)));
      return {{
        x: seg.sx0 + (seg.sx1 - seg.sx0) * ratio,
        y: seg.sy0 + (seg.sy1 - seg.sy0) * ratio,
      }};
    }}

    function updateStatus(activeItems) {{
      const lines = [];
      lines.push(`<strong>현재 시간</strong> : ${{formatSeconds(currentTime)}}`);
      lines.push(`<strong>표시 작업자</strong> : ${{selectedWorker === 'ALL' ? '전체' : selectedWorker}}`);
      lines.push(`<strong>활성 마커 수</strong> : ${{activeItems.length}}`);
      if (activeItems.length > 0) {{
        const preview = activeItems.slice(0, 6).map(item => `${{item.workerId}}(${{item.kind}})`).join(', ');
        lines.push(`<strong>상태</strong> : ${{preview}}${{activeItems.length > 6 ? ' ...' : ''}}`);
      }}
      statusBox.innerHTML = lines.join('<br/>');
    }}

    function render() {{
      timeSlider.value = String(currentTime);
      timeLabel.textContent = formatSeconds(currentTime);
      const activeItems = [];

      data.workers.forEach((worker) => {{
        const marker = markers.get(worker.worker_id);
        const visible = selectedWorker === 'ALL' || selectedWorker === worker.worker_id;
        marker.group.style.display = visible ? '' : 'none';
        if (!visible) return;

        const seg = segmentAtTime(worker, currentTime);
        const pos = positionFromSegment(seg, currentTime);
        marker.circle.setAttribute('cx', pos.x.toFixed(2));
        marker.circle.setAttribute('cy', pos.y.toFixed(2));
        marker.text.setAttribute('x', pos.x.toFixed(2));
        marker.text.setAttribute('y', pos.y.toFixed(2));
        marker.group.setAttribute('opacity', seg.kind === 'idle' ? '0.65' : '1.0');
        activeItems.push({{ workerId: worker.worker_id, kind: seg.kind }});
      }});

      updateStatus(activeItems);
    }}

    function tick(timestamp) {{
      if (!playing) return;
      if (lastTimestamp == null) lastTimestamp = timestamp;
      const deltaMs = timestamp - lastTimestamp;
      lastTimestamp = timestamp;
      const speed = parseFloat(speedSel.value || '1');
      currentTime += (deltaMs / 1000.0) * speed;
      if (currentTime >= duration) {{
        currentTime = duration;
        playing = false;
        playBtn.textContent = '▶ 재생';
      }}
      render();
      if (playing) animationHandle = requestAnimationFrame(tick);
    }}

    playBtn.addEventListener('click', () => {{
      playing = !playing;
      playBtn.textContent = playing ? '⏸ 일시정지' : '▶ 재생';
      lastTimestamp = null;
      if (playing) animationHandle = requestAnimationFrame(tick);
      else if (animationHandle) cancelAnimationFrame(animationHandle);
    }});

    timeSlider.addEventListener('input', (e) => {{
      currentTime = parseFloat(e.target.value || '0');
      render();
    }});

    workerSel.addEventListener('change', (e) => {{
      selectedWorker = e.target.value;
      render();
    }});

    render();
  </script>
</body>
</html>
"""


def generate_observed_picking_animation(
    *,
    data_dir: str | Path,
    layout_svg: str | Path,
    output_html: str | Path,
    target_date: date | None,
    max_lists: int | None,
    walking_speed_mps: float = 1.2,
    pick_seconds_per_unit: float = 3.0,
    edge_capacity: int = 1,
    pick_node_capacity: int = 1,
    return_to_io: bool = True,
) -> Path:
    data_dir = Path(data_dir)
    output_html = Path(output_html)

    bundle = load_dataset(data_dir)
    warehouse = WarehouseGraph.build(
        bundle.storage_locations,
        bundle.support_points,
        deterministic_order=True,
    )
    selected_date, selected_lists = select_phase2_lists(
        warehouse,
        bundle.picking_lists,
        target_date=target_date,
        max_lists=max_lists,
    )

    workers, _traffic, _executions, _entropy_samples, _cell_metrics, _origin, sim_end = run_phase2_simulation(
        warehouse,
        selected_lists,
        walking_speed_mps=walking_speed_mps,
        pick_seconds_per_unit=pick_seconds_per_unit,
        edge_capacity=edge_capacity,
        pick_node_capacity=pick_node_capacity,
        return_to_io=return_to_io,
    )

    raw_x_values = [loc.raw_x for loc in bundle.storage_locations] + [p.raw_x for p in bundle.support_points]
    raw_y_values = [loc.raw_y for loc in bundle.storage_locations] + [p.raw_y for p in bundle.support_points]
    svg_transform = parse_svg_axes_transform(
        layout_svg,
        raw_x_min=min(raw_x_values),
        raw_x_max=max(raw_x_values),
        raw_y_min=min(raw_y_values),
        raw_y_max=max(raw_y_values),
    )

    payload = build_animation_payload(
        warehouse=warehouse,
        workers=workers,
        selected_lists=selected_lists,
        selected_date=selected_date,
        simulation_end_seconds=sim_end,
        svg_transform=svg_transform,
    )
    title = f"Observed Picking Animation | {selected_date.isoformat()}"
    html = render_animation_html(svg_transform=svg_transform, payload=payload, title=title)

    output_html.parent.mkdir(parents=True, exist_ok=True)
    output_html.write_text(html, encoding="utf-8")
    return output_html



def render_date_selector_html(
    *,
    date_files: list[tuple[str, str]],
    title: str = "Observed Picking Animation | All Dates",
) -> str:
    if not date_files:
        raise ValueError("날짜별 animation 파일이 없습니다.")

    options = "\n".join(
        f'<option value="{relative_path}">{date_text}</option>'
        for date_text, relative_path in date_files
    )
    first_path = date_files[0][1]
    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{title}</title>
  <style>
    html, body {{ margin: 0; min-height: 100%; font-family: Arial, sans-serif; background: #f6f8fb; color: #1f2937; }}
    .toolbar {{ position: sticky; top: 0; z-index: 10; display: flex; flex-wrap: wrap; gap: 12px; align-items: center; padding: 12px 18px; background: #ffffff; border-bottom: 1px solid #e5e7eb; box-shadow: 0 2px 8px rgba(0,0,0,.05); }}
    .toolbar strong {{ margin-right: 8px; }}
    select {{ min-width: 180px; padding: 9px 12px; border: 1px solid #d1d5db; border-radius: 9px; background: white; font-size: 14px; }}
    .meta {{ color: #6b7280; font-size: 13px; }}
    iframe {{ display: block; width: 100%; height: 92vh; border: 0; background: white; }}
  </style>
</head>
<body>
  <div class="toolbar">
    <strong>날짜 선택</strong>
    <select id="dateSelect" aria-label="애니메이션 날짜 선택">
      {options}
    </select>
    <span class="meta">총 {len(date_files)}개 날짜 · 선택 즉시 해당 날짜 애니메이션으로 전환됩니다.</span>
  </div>
  <iframe id="animationFrame" src="{first_path}" title="Picking animation"></iframe>
  <script>
    const dateSelect = document.getElementById('dateSelect');
    const animationFrame = document.getElementById('animationFrame');
    dateSelect.addEventListener('change', () => {{
      animationFrame.src = dateSelect.value;
    }});
  </script>
</body>
</html>
"""


def generate_all_dates_animation_site(
    *,
    data_dir: str | Path,
    layout_svg: str | Path,
    output_html: str | Path,
    max_lists: int | None,
    walking_speed_mps: float = 1.2,
    pick_seconds_per_unit: float = 3.0,
    edge_capacity: int = 1,
    pick_node_capacity: int = 1,
    return_to_io: bool = True,
) -> Path:
    data_dir = Path(data_dir)
    output_html = Path(output_html)

    bundle = load_dataset(data_dir)
    warehouse = WarehouseGraph.build(
        bundle.storage_locations,
        bundle.support_points,
        deterministic_order=True,
    )
    dates = available_phase2_dates(warehouse, bundle.picking_lists)
    if not dates:
        raise ValueError("애니메이션으로 생성할 fully-valid 날짜가 없습니다.")

    raw_x_values = [loc.raw_x for loc in bundle.storage_locations] + [p.raw_x for p in bundle.support_points]
    raw_y_values = [loc.raw_y for loc in bundle.storage_locations] + [p.raw_y for p in bundle.support_points]
    svg_transform = parse_svg_axes_transform(
        layout_svg,
        raw_x_min=min(raw_x_values),
        raw_x_max=max(raw_x_values),
        raw_y_min=min(raw_y_values),
        raw_y_max=max(raw_y_values),
    )

    output_html.parent.mkdir(parents=True, exist_ok=True)
    date_dir = output_html.parent / f"{output_html.stem}_dates"
    date_dir.mkdir(parents=True, exist_ok=True)

    date_files: list[tuple[str, str]] = []
    total_dates = len(dates)
    print(f"[START] Generating observed animations for {total_dates} dates")

    for index, target_date in enumerate(dates, start=1):
        print(f"[RUN  ] {index:>3}/{total_dates} | {target_date.isoformat()}")
        selected_date, selected_lists = select_phase2_lists(
            warehouse,
            bundle.picking_lists,
            target_date=target_date,
            max_lists=max_lists,
        )

        workers, _traffic, _executions, _entropy_samples, _cell_metrics, _origin, sim_end = run_phase2_simulation(
            warehouse,
            selected_lists,
            walking_speed_mps=walking_speed_mps,
            pick_seconds_per_unit=pick_seconds_per_unit,
            edge_capacity=edge_capacity,
            pick_node_capacity=pick_node_capacity,
            return_to_io=return_to_io,
        )

        payload = build_animation_payload(
            warehouse=warehouse,
            workers=workers,
            selected_lists=selected_lists,
            selected_date=selected_date,
            simulation_end_seconds=sim_end,
            svg_transform=svg_transform,
        )
        title = f"Observed Picking Animation | {selected_date.isoformat()}"
        html = render_animation_html(
            svg_transform=svg_transform,
            payload=payload,
            title=title,
        )

        filename = f"observed_picking_animation_{selected_date.isoformat()}.html"
        date_output = date_dir / filename
        date_output.write_text(html, encoding="utf-8")
        relative_path = date_output.relative_to(output_html.parent).as_posix()
        date_files.append((selected_date.isoformat(), relative_path))

    selector_html = render_date_selector_html(date_files=date_files)
    output_html.write_text(selector_html, encoding="utf-8")
    print(f"[DONE] Date selector HTML saved to: {output_html}")
    print(f"[DONE] Date animation files saved under: {date_dir}")
    return output_html

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate observed picking animation HTML on top of Layout_Z1.0.svg"
    )
    parser.add_argument("--data-dir", default="data/raw", help="directory containing CSV data files")
    parser.add_argument(
        "--layout-svg",
        default="data/raw_original/Layout_Z1.0.svg",
        help="warehouse layout SVG used as the animation background",
    )
    parser.add_argument(
        "--output-html",
        default="results/figures/observed_picking_animation.html",
        help="output standalone HTML path (or all-dates selector page with --all-dates)",
    )
    parser.add_argument("--date", default=None, help="target date in YYYY-MM-DD format")
    parser.add_argument(
        "--all-dates",
        action="store_true",
        help="generate every available date and a select-box index page",
    )
    parser.add_argument("--max-lists", type=int, default=None, help="optional cap for quick testing")
    parser.add_argument("--walking-speed-mps", type=float, default=1.2)
    parser.add_argument("--pick-seconds-per-unit", type=float, default=3.0)
    parser.add_argument("--edge-capacity", type=int, default=1)
    parser.add_argument("--pick-node-capacity", type=int, default=1)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    if args.all_dates and args.date is not None:
        raise ValueError("--all-dates와 --date는 동시에 사용할 수 없습니다.")

    if args.all_dates:
        output = generate_all_dates_animation_site(
            data_dir=args.data_dir,
            layout_svg=args.layout_svg,
            output_html=args.output_html,
            max_lists=args.max_lists,
            walking_speed_mps=args.walking_speed_mps,
            pick_seconds_per_unit=args.pick_seconds_per_unit,
            edge_capacity=args.edge_capacity,
            pick_node_capacity=args.pick_node_capacity,
        )
        print(f"[DONE] Open this file in your browser: {output}")
        return

    target_date = None if args.date is None else date.fromisoformat(args.date)
    output = generate_observed_picking_animation(
        data_dir=args.data_dir,
        layout_svg=args.layout_svg,
        output_html=args.output_html,
        target_date=target_date,
        max_lists=args.max_lists,
        walking_speed_mps=args.walking_speed_mps,
        pick_seconds_per_unit=args.pick_seconds_per_unit,
        edge_capacity=args.edge_capacity,
        pick_node_capacity=args.pick_node_capacity,
    )
    print(f"[DONE] Observed animation HTML saved to: {output}")


if __name__ == "__main__":
    main()