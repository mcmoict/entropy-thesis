from __future__ import annotations

"""Native Qt desktop viewer for the warehouse picking animation.

This module intentionally reuses the monthly JSON produced by
``picking_animation_actual.py`` instead of rerunning the simulation.

Recommended placement:
    src/entropy_thesis/visualization/picking_animation_desktop.py

Typical execution:
    python -m entropy_thesis.visualization.picking_animation_desktop

Dependencies:
    pip install PySide6

The viewer uses:
- QSvgRenderer for the static warehouse layout.
- QPainter for zones, picking targets and workers.
- QTimer(Qt.PreciseTimer) + QElapsedTimer for time-based interpolation.
- The exact DES ``conflict_events`` already serialized in the monthly JSON.
"""

import argparse
import csv
from dataclasses import dataclass
from datetime import datetime, timedelta
import html as html_lib
import json
import math
from pathlib import Path
import re
import sys
from typing import Any, Iterable
import xml.etree.ElementTree as ET

try:
    from PySide6.QtCore import (
        QElapsedTimer,
        QPointF,
        QRectF,
        Qt,
        QTimer,
        Signal,
    )
    from PySide6.QtGui import (
        QBrush,
        QColor,
        QFont,
        QPainter,
        QPen,
        QPixmap,
    )
    from PySide6.QtSvg import QSvgRenderer
    from PySide6.QtWidgets import (
        QApplication,
        QCheckBox,
        QComboBox,
        QFormLayout,
        QFrame,
        QHBoxLayout,
        QLabel,
        QListWidget,
        QListWidgetItem,
        QMainWindow,
        QMessageBox,
        QPushButton,
        QScrollArea,
        QSizePolicy,
        QSlider,
        QSplitter,
        QTextBrowser,
        QVBoxLayout,
        QWidget,
    )
except ImportError as exc:  # pragma: no cover - runtime convenience
    raise SystemExit(
        "PySide6가 설치되어 있지 않습니다.\n"
        "먼저 아래 명령을 실행해 주세요.\n\n"
        "    python -m pip install PySide6\n\n"
        f"원본 오류: {exc}"
    ) from exc

try:
    # Keep the same project data parser used by picking_animation_actual.py.
    from ..simulation.data_loader import load_support_points
except ImportError:  # pragma: no cover - direct-file fallback
    load_support_points = None

try:  # optional, considerably faster for very large monthly JSON files
    import orjson  # type: ignore
except ImportError:  # pragma: no cover
    orjson = None


METHODS: tuple[str, ...] = ("observed", "equal", "random", "volume", "entropy")
METHOD_LABELS: dict[str, str] = {
    "observed": "Observed",
    "equal": "Equal",
    "random": "Random",
    "volume": "Volume",
    "entropy": "Entropy",
}

SPEEDS: tuple[float, ...] = (0.5, 1, 2, 3, 5, 10, 20, 50)
SLIDER_STEPS = 100_000
UI_REFRESH_MS = 100
FRAME_INTERVAL_MS = 16  # ~60 FPS


# ---------------------------------------------------------------------------
# JSON / HTML manifest loading
# ---------------------------------------------------------------------------


def _json_load_path(path: Path) -> Any:
    raw = path.read_bytes()
    if orjson is not None:
        return orjson.loads(raw)
    return json.loads(raw.decode("utf-8"))


def _availability_from_month_date_payload(date_payload: dict[str, Any]) -> dict[str, Any]:
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
    comparison_present = all(method in available_methods for method in METHODS[1:])
    return {
        "comparison_eligible": comparison_present,
        "reason": "eligible" if comparison_present else "availability_metadata_missing",
        "active_zones": None,
        "observed_workers": observed_workers,
        "picking_lists": picking_lists,
        "min_lists_per_date": None,
        "available_methods": available_methods or ["observed"],
    }


def _manifest_from_html(html_path: Path) -> dict[str, Any] | None:
    if not html_path.exists():
        return None
    text = html_path.read_text(encoding="utf-8")
    match = re.search(
        r'<script\s+id=["\']animationManifest["\']\s+type=["\']application/json["\']>(.*?)</script>',
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return None
    return json.loads(html_lib.unescape(match.group(1)))


class MonthlyDataStore:
    """Lazily load one month at a time; never keep every large JSON in memory."""

    def __init__(self, *, html_path: Path, json_dir: Path | None = None) -> None:
        self.html_path = html_path
        self.json_dir = json_dir or html_path.with_name(f"{html_path.stem}_data")
        if not self.json_dir.exists():
            raise FileNotFoundError(f"월별 JSON 디렉터리가 없습니다: {self.json_dir}")

        manifest = _manifest_from_html(html_path)
        if manifest is None:
            manifest = self._scan_manifest_from_json()

        self.manifest = manifest
        self.date_order = [str(value) for value in manifest.get("date_order", [])]
        self.month_by_date = {
            str(k): str(v) for k, v in (manifest.get("month_by_date") or {}).items()
        }
        self.month_files = {
            str(k): str(v) for k, v in (manifest.get("month_files") or {}).items()
        }
        self.availability_by_date = {
            str(k): v for k, v in (manifest.get("availability_by_date") or {}).items()
        }
        if not self.date_order:
            raise ValueError("재생 가능한 날짜가 없습니다.")

        self._loaded_month_key: str | None = None
        self._loaded_month_data: dict[str, Any] | None = None

    def _scan_manifest_from_json(self) -> dict[str, Any]:
        month_files: dict[str, str] = {}
        month_by_date: dict[str, str] = {}
        availability_by_date: dict[str, Any] = {}
        date_order: list[str] = []

        paths = sorted(self.json_dir.glob("*.json"))
        if not paths:
            raise FileNotFoundError(f"월별 JSON 파일이 없습니다: {self.json_dir}")

        print("[SCAN ] HTML manifest가 없어 월별 JSON에서 인덱스를 구성합니다.")
        for path in paths:
            payload = _json_load_path(path)
            meta = payload.get("meta") if isinstance(payload, dict) else {}
            month_key = str((meta or {}).get("month") or path.stem)
            dates = payload.get("dates") if isinstance(payload, dict) else None
            if not isinstance(dates, dict):
                continue
            month_files[month_key] = path.name
            for date_text, date_payload in dates.items():
                if not isinstance(date_payload, dict):
                    continue
                date_text = str(date_text)
                date_order.append(date_text)
                month_by_date[date_text] = month_key
                availability_by_date[date_text] = _availability_from_month_date_payload(
                    date_payload
                )
            del payload

        date_order = sorted(set(date_order))
        return {
            "meta": {"format": "desktop-scanned-manifest"},
            "date_order": date_order,
            "month_by_date": month_by_date,
            "month_files": month_files,
            "availability_by_date": availability_by_date,
        }

    def availability(self, date_text: str) -> dict[str, Any]:
        info = self.availability_by_date.get(date_text)
        if isinstance(info, dict):
            return info
        return {
            "comparison_eligible": True,
            "reason": "eligible",
            "available_methods": list(METHODS),
            "observed_workers": 0,
        }

    def method_available(self, date_text: str, method: str) -> bool:
        methods = self.availability(date_text).get("available_methods") or ["observed"]
        return method in methods

    def scenarios_for_date(self, date_text: str) -> dict[str, Any]:
        month_key = self.month_by_date.get(date_text)
        if month_key is None:
            month_key = date_text[:7]
        if self._loaded_month_key != month_key or self._loaded_month_data is None:
            file_name = self.month_files.get(month_key, f"{month_key}.json")
            path = self.json_dir / file_name
            print(f"[LOAD ] {path}")
            payload = _json_load_path(path)
            if not isinstance(payload, dict) or not isinstance(payload.get("dates"), dict):
                raise ValueError(f"월별 JSON 형식이 올바르지 않습니다: {path}")
            self._loaded_month_key = month_key
            self._loaded_month_data = payload

        dates = self._loaded_month_data["dates"]
        value = dates.get(date_text)
        if not isinstance(value, dict):
            raise KeyError(f"날짜 데이터를 찾지 못했습니다: {date_text}")
        return value


# ---------------------------------------------------------------------------
# Macro-zone calibration copied from the same geometry logic as the web viewer
# ---------------------------------------------------------------------------


def _strip_xml_declaration(svg_text: str) -> str:
    return re.sub(r"^<\?xml[^>]*>\s*", "", svg_text, count=1)


def _extract_svg_support_markers(root: ET.Element) -> list[tuple[float, float]]:
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
    *, support_points: Iterable[Any], svg_markers: Iterable[tuple[float, float]]
) -> tuple[dict[str, Any], ...]:
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
    if any(code not in by_code for code in required):
        return ()

    lc = [by_code[f"LC-{i:02d}"] for i in range(8, 18)]
    rc = [by_code[f"RC-{i:02d}"] for i in range(8, 18)]
    row_y = [0.5 * (lc[i][1] + rc[i][1]) for i in range(10)]
    near_mid_y = 0.5 * (row_y[4] + row_y[5])
    outer_08_y = row_y[0] - (row_y[1] - row_y[0]) / 2.0
    outer_17_y = row_y[-1] + (row_y[-1] - row_y[-2]) / 2.0

    split_x = by_code["CC-08"][0]
    all_x = sorted(float(x) for x, _ in markers)
    diffs = [b - a for a, b in zip(all_x, all_x[1:]) if b - a > 1e-6]
    x_pad = min(diffs) / 2.0 if diffs else 0.0
    left_x = all_x[0] - x_pad
    right_x = all_x[-1] + x_pad

    def rect(zone_id: str, label: str, x0: float, x1: float, y0: float, y1: float) -> dict[str, Any]:
        left, right = sorted((float(x0), float(x1)))
        top, bottom = sorted((float(y0), float(y1)))
        return {
            "zone_id": zone_id,
            "label": label,
            "x": left,
            "y": top,
            "width": right - left,
            "height": bottom - top,
        }

    return (
        rect("Z01", "Z01 · Left / Near", left_x, split_x, outer_08_y, near_mid_y),
        rect("Z02", "Z02 · Left / Far", left_x, split_x, near_mid_y, outer_17_y),
        rect("Z03", "Z03 · Right / Near", split_x, right_x, outer_08_y, near_mid_y),
        rect("Z04", "Z04 · Right / Far", split_x, right_x, near_mid_y, outer_17_y),
    )


def build_macro_zones(svg_path: Path, support_csv: Path) -> tuple[dict[str, Any], ...]:
    if not svg_path.exists() or not support_csv.exists():
        print("[ZONE ] SVG/support CSV가 없어 Z01~Z04 표시를 생략합니다.")
        return ()

    root = ET.fromstring(svg_path.read_text(encoding="utf-8"))
    markers = _extract_svg_support_markers(root)

    if load_support_points is not None:
        supports = load_support_points(support_csv)
    else:
        # Direct-file execution fallback.  Zone calibration only needs the CSV
        # row order and point codes, so the project's dataclass parser is not
        # required here.
        with support_csv.open("r", encoding="utf-8-sig", newline="") as handle:
            supports = list(csv.DictReader(handle))
        print("[ZONE ] direct CSV fallback enabled")

    zones = _build_macro_zone_rectangles(support_points=supports, svg_markers=markers)
    if zones:
        print("[ZONE ] Z01~Z04 calibrated from SVG support markers")
    return zones


# ---------------------------------------------------------------------------
# Scenario runtime and exact DES conflict cursor
# ---------------------------------------------------------------------------


@dataclass
class WorkerFrame:
    worker: dict[str, Any]
    x: float
    y: float
    kind: str
    visible: bool
    colliding: bool
    opacity: float


@dataclass
class ConflictSnapshot:
    active: list[dict[str, Any]]
    collision_workers: set[str]
    cumulative_count: int
    cumulative_picker_count: int


class ScenarioRuntime:
    def __init__(self, scenario: dict[str, Any]) -> None:
        self.scenario = scenario
        self.workers = list(scenario.get("workers") or [])
        self.worker_indices: dict[str, int] = {
            str(worker.get("worker_id")): 0 for worker in self.workers
        }
        self.last_worker_time = -math.inf

        self.conflicts = sorted(
            list(scenario.get("conflict_events") or []),
            key=lambda item: (float(item.get("t0") or 0), float(item.get("t1") or 0)),
        )
        self.has_exact_conflicts = isinstance(scenario.get("conflict_events"), list)
        self.conflict_cursor = 0
        self.active_conflicts: list[dict[str, Any]] = []
        self.last_conflict_time = -math.inf
        self.cumulative_picker_prefix = [0]
        for event in self.conflicts:
            ids = event.get("worker_ids")
            count = len(ids) if isinstance(ids, list) else 0
            self.cumulative_picker_prefix.append(self.cumulative_picker_prefix[-1] + count)

        targets = scenario.get("pick_targets")
        self.pick_targets = list(targets) if isinstance(targets, list) and targets else self._derive_pick_targets()

    @property
    def end_seconds(self) -> float:
        meta = self.scenario.get("meta") or {}
        return max(0.0, float(meta.get("simulation_end_seconds") or 0.0))

    def reset_cursors(self) -> None:
        for key in self.worker_indices:
            self.worker_indices[key] = 0
        self.last_worker_time = -math.inf
        self.conflict_cursor = 0
        self.active_conflicts = []
        self.last_conflict_time = -math.inf

    def _derive_pick_targets(self) -> list[dict[str, Any]]:
        by_point: dict[tuple[float, float], dict[str, Any]] = {}
        for worker in self.workers:
            worker_id = str(worker.get("worker_id") or "")
            for seg in worker.get("segments") or []:
                if seg.get("kind") != "pick":
                    continue
                sx = float(seg.get("sx1") or 0)
                sy = float(seg.get("sy1") or 0)
                key = (round(sx, 3), round(sy, 3))
                item = by_point.setdefault(
                    key,
                    {
                        "node_id": f"SVG({sx:.1f}, {sy:.1f})",
                        "sx": sx,
                        "sy": sy,
                        "pick_events": 0,
                        "quantity_units": 0.0,
                        "worker_ids": [],
                        "location_ids": [],
                        "references": [],
                    },
                )
                item["pick_events"] += 1
                if worker_id not in item["worker_ids"]:
                    item["worker_ids"].append(worker_id)
        return list(by_point.values())

    def _find_segment(self, worker: dict[str, Any], t: float) -> dict[str, Any] | None:
        segs = worker.get("segments") or []
        if not segs:
            return None
        worker_id = str(worker.get("worker_id") or "")
        idx = self.worker_indices.get(worker_id, 0)
        idx = min(max(idx, 0), len(segs) - 1)
        while idx > 0 and t < float(segs[idx].get("t0") or 0):
            idx -= 1
        while idx < len(segs) - 1 and t > float(segs[idx].get("t1") or 0):
            idx += 1
        self.worker_indices[worker_id] = idx
        return segs[idx]

    def conflict_snapshot(self, t: float, started: bool) -> ConflictSnapshot:
        if not started or not self.has_exact_conflicts:
            return ConflictSnapshot([], set(), 0, 0)

        if t + 1e-9 < self.last_conflict_time:
            self.conflict_cursor = 0
            self.active_conflicts = []

        while (
            self.conflict_cursor < len(self.conflicts)
            and float(self.conflicts[self.conflict_cursor].get("t0") or 0) <= t
        ):
            self.active_conflicts.append(self.conflicts[self.conflict_cursor])
            self.conflict_cursor += 1

        if self.active_conflicts:
            self.active_conflicts = [
                event
                for event in self.active_conflicts
                if float(event.get("t1") or 0) >= t
            ]
        self.last_conflict_time = t

        workers: set[str] = set()
        for event in self.active_conflicts:
            for worker_id in event.get("worker_ids") or []:
                workers.add(str(worker_id))
        cumulative_picker = self.cumulative_picker_prefix[self.conflict_cursor]
        return ConflictSnapshot(
            active=list(self.active_conflicts),
            collision_workers=workers,
            cumulative_count=self.conflict_cursor,
            cumulative_picker_count=cumulative_picker,
        )

    def frame(
        self,
        t: float,
        *,
        selected_worker: str,
        started: bool,
    ) -> tuple[list[WorkerFrame], ConflictSnapshot]:
        if t + 1e-9 < self.last_worker_time:
            for key in self.worker_indices:
                self.worker_indices[key] = 0
        self.last_worker_time = t

        conflict = self.conflict_snapshot(t, started)
        states: list[WorkerFrame] = []
        for worker in self.workers:
            worker_id = str(worker.get("worker_id") or "")
            seg = self._find_segment(worker, t)
            if seg is None:
                continue
            kind = str(seg.get("kind") or "idle")
            x = float(seg.get("sx1") or 0.0)
            y = float(seg.get("sy1") or 0.0)
            t0 = float(seg.get("t0") or 0.0)
            t1 = float(seg.get("t1") or 0.0)
            if kind == "move" and t1 > t0:
                r = max(0.0, min(1.0, (t - t0) / (t1 - t0)))
                x0 = float(seg.get("sx0") or 0.0)
                y0 = float(seg.get("sy0") or 0.0)
                x1 = float(seg.get("sx1") or 0.0)
                y1 = float(seg.get("sy1") or 0.0)
                x = x0 + (x1 - x0) * r
                y = y0 + (y1 - y0) * r
            states.append(
                WorkerFrame(
                    worker=worker,
                    x=x,
                    y=y,
                    kind=kind,
                    visible=(selected_worker == "ALL" or selected_worker == worker_id),
                    colliding=(worker_id in conflict.collision_workers),
                    opacity=(0.68 if started and kind == "idle" else 1.0),
                )
            )
        return states, conflict


# ---------------------------------------------------------------------------
# Native renderer
# ---------------------------------------------------------------------------


ZONE_COLORS = {
    "Z01": QColor(37, 99, 235),
    "Z02": QColor(124, 58, 237),
    "Z03": QColor(5, 150, 105),
    "Z04": QColor(219, 39, 119),
}


class WarehouseCanvas(QWidget):
    workerSelected = Signal(str)

    def __init__(self, svg_path: Path, zones: tuple[dict[str, Any], ...]) -> None:
        super().__init__()
        self.setMinimumSize(720, 480)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setAutoFillBackground(False)
        self.setMouseTracking(True)

        self.svg_path = svg_path
        self.renderer = QSvgRenderer(str(svg_path))
        if not self.renderer.isValid():
            raise ValueError(f"SVG를 렌더링할 수 없습니다: {svg_path}")
        self.view_box = self.renderer.viewBoxF()
        self.zones = zones

        self.runtime: ScenarioRuntime | None = None
        self.worker_frames: list[WorkerFrame] = []
        self.conflict_snapshot = ConflictSnapshot([], set(), 0, 0)
        self.show_targets = True
        self.show_zones = True
        self.selected_worker = "ALL"
        self._worker_colors: dict[str, QColor] = {}
        self._last_content_rect = QRectF()
        self._static_cache: QPixmap | None = None
        self._static_cache_dpr = 0.0

    def invalidate_static_cache(self) -> None:
        self._static_cache = None
        self._static_cache_dpr = 0.0
        self.update()

    def set_runtime(self, runtime: ScenarioRuntime | None) -> None:
        self.runtime = runtime
        self.invalidate_static_cache()
        self.worker_frames = []
        self.conflict_snapshot = ConflictSnapshot([], set(), 0, 0)
        self._worker_colors = {}
        if runtime is not None:
            for index, worker in enumerate(runtime.workers):
                worker_id = str(worker.get("worker_id") or "")
                self._worker_colors[worker_id] = QColor.fromHsv((index * 47) % 360, 178, 210)
        self.update()

    def set_frame(self, frames: list[WorkerFrame], conflict: ConflictSnapshot) -> None:
        self.worker_frames = frames
        self.conflict_snapshot = conflict
        self.update()

    def _content_rect(self) -> QRectF:
        w = max(1.0, float(self.width()))
        h = max(1.0, float(self.height()))
        vbw = max(1e-9, self.view_box.width())
        vbh = max(1e-9, self.view_box.height())
        scale = min(w / vbw, h / vbh)
        rw = vbw * scale
        rh = vbh * scale
        return QRectF((w - rw) / 2.0, (h - rh) / 2.0, rw, rh)

    def _map_point(self, x: float, y: float) -> QPointF:
        rect = self._last_content_rect
        sx = rect.width() / max(1e-9, self.view_box.width())
        sy = rect.height() / max(1e-9, self.view_box.height())
        return QPointF(
            rect.left() + (x - self.view_box.left()) * sx,
            rect.top() + (y - self.view_box.top()) * sy,
        )

    def _map_rect(self, item: dict[str, Any]) -> QRectF:
        p0 = self._map_point(float(item["x"]), float(item["y"]))
        p1 = self._map_point(
            float(item["x"]) + float(item["width"]),
            float(item["y"]) + float(item["height"]),
        )
        return QRectF(p0, p1).normalized()

    def _rebuild_static_cache(self) -> None:
        self._last_content_rect = self._content_rect()
        dpr = max(1.0, float(self.devicePixelRatioF()))
        pixel_w = max(1, int(round(self.width() * dpr)))
        pixel_h = max(1, int(round(self.height() * dpr)))
        cache = QPixmap(pixel_w, pixel_h)
        cache.setDevicePixelRatio(dpr)
        cache.fill(QColor("white"))

        painter = QPainter(cache)
        painter.setRenderHints(
            QPainter.RenderHint.Antialiasing
            | QPainter.RenderHint.TextAntialiasing
            | QPainter.RenderHint.SmoothPixmapTransform
        )
        self.renderer.render(painter, self._last_content_rect)
        if self.show_zones:
            self._draw_zones(painter)
        if self.runtime is not None and self.show_targets:
            self._draw_pick_targets(painter, self.runtime.pick_targets)
        painter.end()

        self._static_cache = cache
        self._static_cache_dpr = dpr

    def resizeEvent(self, event: Any) -> None:
        self.invalidate_static_cache()
        super().resizeEvent(event)

    def paintEvent(self, _event: Any) -> None:
        self._last_content_rect = self._content_rect()
        current_dpr = max(1.0, float(self.devicePixelRatioF()))
        if self._static_cache is None or abs(self._static_cache_dpr - current_dpr) > 1e-6:
            self._rebuild_static_cache()

        painter = QPainter(self)
        painter.setRenderHints(
            QPainter.RenderHint.Antialiasing
            | QPainter.RenderHint.TextAntialiasing
            | QPainter.RenderHint.SmoothPixmapTransform
        )
        if self._static_cache is not None:
            painter.drawPixmap(QPointF(0.0, 0.0), self._static_cache)
        else:
            painter.fillRect(self.rect(), QColor("white"))
        self._draw_workers(painter)

    def _draw_zones(self, painter: QPainter) -> None:
        for zone in self.zones:
            zone_id = str(zone.get("zone_id") or "")
            color = ZONE_COLORS.get(zone_id, QColor(80, 80, 80))
            box = self._map_rect(zone)

            fill = QColor(color)
            fill.setAlpha(14)
            painter.setBrush(QBrush(fill))
            pen = QPen(color)
            pen.setWidthF(2.0)
            pen.setStyle(Qt.PenStyle.DashLine)
            painter.setPen(pen)
            painter.drawRoundedRect(box, 3, 3)

            font = QFont(self.font())
            font.setBold(True)
            font.setPointSizeF(10.0)
            painter.setFont(font)
            painter.setPen(color)
            painter.drawText(box.adjusted(7, 5, -4, -4), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop, zone_id)

    def _draw_pick_targets(self, painter: QPainter, targets: list[dict[str, Any]]) -> None:
        for target in targets:
            x = float(target.get("sx") or 0.0)
            y = float(target.get("sy") or 0.0)
            events = max(1, int(target.get("pick_events") or 1))
            radius = min(12.0, 6.0 + math.log2(events + 1) * 1.6)
            p = self._map_point(x, y)

            fill = QColor(245, 158, 11, 88)
            painter.setBrush(QBrush(fill))
            pen = QPen(QColor(180, 83, 9))
            pen.setWidthF(1.4)
            painter.setPen(pen)
            painter.drawEllipse(p, radius, radius)

            if events > 1:
                font = QFont(self.font())
                font.setBold(True)
                font.setPointSizeF(6.5)
                painter.setFont(font)
                painter.setPen(QColor(120, 53, 15))
                painter.drawText(
                    QRectF(p.x() - radius, p.y() - radius, radius * 2, radius * 2),
                    Qt.AlignmentFlag.AlignCenter,
                    str(events),
                )

    @staticmethod
    def _short_worker_label(worker_id: str, index: int) -> str:
        match = re.search(r"Operator_(\d+)", worker_id, flags=re.IGNORECASE)
        if match:
            return match.group(1)
        return str(index + 1).zfill(2)

    def _draw_workers(self, painter: QPainter) -> None:
        radius = 10.5
        label_font = QFont(self.font())
        label_font.setBold(True)
        label_font.setPointSizeF(8.0)
        painter.setFont(label_font)

        visible_index = 0
        for index, state in enumerate(self.worker_frames):
            if not state.visible:
                continue
            worker_id = str(state.worker.get("worker_id") or "")
            point = self._map_point(state.x, state.y)
            base = self._worker_colors.get(worker_id, QColor.fromHsv((index * 47) % 360, 178, 210))
            fill = QColor(239, 68, 68) if state.colliding else QColor(base)
            fill.setAlphaF(state.opacity)

            painter.setBrush(QBrush(fill))
            pen = QPen(QColor(153, 27, 27) if state.colliding else QColor("white"))
            pen.setWidthF(3.1 if state.colliding else 2.0)
            painter.setPen(pen)
            painter.drawEllipse(point, radius, radius)

            painter.setPen(QColor("white") if state.colliding else QColor(17, 24, 39))
            painter.drawText(
                QRectF(point.x() - radius, point.y() - radius, radius * 2, radius * 2),
                Qt.AlignmentFlag.AlignCenter,
                self._short_worker_label(worker_id, index),
            )
            visible_index += 1

    def mousePressEvent(self, event: Any) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return super().mousePressEvent(event)
        click = event.position()
        best: tuple[float, str] | None = None
        for state in self.worker_frames:
            if not state.visible:
                continue
            worker_id = str(state.worker.get("worker_id") or "")
            p = self._map_point(state.x, state.y)
            dist = math.hypot(click.x() - p.x(), click.y() - p.y())
            if dist <= 15 and (best is None or dist < best[0]):
                best = (dist, worker_id)
        if best is not None:
            self.workerSelected.emit(best[1])


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------


def _format_seconds(value: float) -> str:
    total = max(0, int(value))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _actual_datetime(origin_text: str | None, elapsed_seconds: float) -> str:
    if not origin_text:
        return "-"
    match = re.match(
        r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2}(?:\.\d+)?)",
        str(origin_text),
    )
    if not match:
        return str(origin_text)
    try:
        origin = datetime(
            int(match.group(1)), int(match.group(2)), int(match.group(3)),
            int(match.group(4)), int(match.group(5)), int(float(match.group(6))),
        )
        value = origin + timedelta(seconds=max(0.0, elapsed_seconds))
        return value.strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return str(origin_text)


def _reason_label(reason: str) -> str:
    return {
        "too_few_lists": "피킹리스트 수가 비교 실험 기준보다 적음",
        "no_workers": "작업자가 없음",
        "no_active_zones": "활성 구역이 없음",
        "insufficient_workers_for_active_zones": "작업자 수가 활성 구역 수보다 적음",
        "availability_metadata_missing": "비교방법 availability 메타데이터 없음",
        "eligible": "사용 가능",
    }.get(reason, reason)


class PickingAnimationWindow(QMainWindow):
    def __init__(
        self,
        *,
        store: MonthlyDataStore,
        svg_path: Path,
        zones: tuple[dict[str, Any], ...],
        start_date: str | None = None,
        start_method: str = "observed",
    ) -> None:
        super().__init__()
        self.store = store
        self.svg_path = svg_path
        self.zones = zones

        self.runtime: ScenarioRuntime | None = None
        self.scenario: dict[str, Any] | None = None
        self.current_date = start_date if start_date in store.date_order else store.date_order[0]
        self.current_method = start_method if start_method in METHODS else "observed"
        self.current_time = 0.0
        self.selected_worker = "ALL"
        self.playing = False
        self.has_started = False
        self._ui_elapsed_since_refresh = 0.0

        self.frame_clock = QElapsedTimer()
        self.timer = QTimer(self)
        self.timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.timer.setInterval(FRAME_INTERVAL_MS)
        self.timer.timeout.connect(self._tick)

        self.setWindowTitle("Warehouse Picking Animation · Desktop")
        self.resize(1520, 900)
        self.setMinimumSize(1120, 720)
        self._build_ui()
        self._populate_dates()
        self._populate_methods()
        self._load_scenario(self.current_date, self.current_method, auto_play=False)

    def _build_ui(self) -> None:
        root = QWidget(self)
        self.setCentralWidget(root)
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(12, 12, 12, 12)
        root_layout.setSpacing(8)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        root_layout.addWidget(splitter, 1)

        # Left visual side
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(8)

        frame = QFrame()
        frame.setFrameShape(QFrame.Shape.StyledPanel)
        frame_layout = QVBoxLayout(frame)
        frame_layout.setContentsMargins(4, 4, 4, 4)
        self.canvas = WarehouseCanvas(self.svg_path, self.zones)
        self.canvas.workerSelected.connect(self._select_worker_from_canvas)
        frame_layout.addWidget(self.canvas)
        left_layout.addWidget(frame, 1)

        controls = QHBoxLayout()
        self.play_btn = QPushButton("▶ 재생")
        self.play_btn.setMinimumWidth(92)
        self.play_btn.clicked.connect(self._toggle_play)
        controls.addWidget(self.play_btn)

        self.speed_combo = QComboBox()
        for speed in SPEEDS:
            self.speed_combo.addItem(f"{speed:g}x", speed)
        self.speed_combo.setCurrentIndex(SPEEDS.index(1))
        self.speed_combo.currentIndexChanged.connect(self._reset_frame_clock)
        self.speed_combo.setMinimumWidth(78)
        controls.addWidget(self.speed_combo)

        self.worker_combo = QComboBox()
        self.worker_combo.setMinimumWidth(155)
        self.worker_combo.currentIndexChanged.connect(self._worker_changed)
        controls.addWidget(self.worker_combo)

        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, SLIDER_STEPS)
        self.slider.valueChanged.connect(self._slider_changed)
        self.slider.sliderPressed.connect(self._reset_frame_clock)
        controls.addWidget(self.slider, 1)

        self.time_label = QLabel("00:00:00")
        self.time_label.setMinimumWidth(70)
        self.time_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        controls.addWidget(self.time_label)
        left_layout.addLayout(controls)

        options = QHBoxLayout()
        self.auto_next_chk = QCheckBox("다음 날짜 자동실행")
        self.targets_chk = QCheckBox("피킹 대상 표시")
        self.targets_chk.setChecked(True)
        self.targets_chk.toggled.connect(self._targets_toggled)
        self.zones_chk = QCheckBox("Z01~Z04 구역 표시")
        self.zones_chk.setChecked(True)
        self.zones_chk.toggled.connect(self._zones_toggled)
        options.addWidget(self.auto_next_chk)
        options.addWidget(self.targets_chk)
        options.addWidget(self.zones_chk)
        options.addStretch(1)
        left_layout.addLayout(options)

        splitter.addWidget(left)

        # Right sidebar
        right_scroll = QScrollArea()
        right_scroll.setWidgetResizable(True)
        right_scroll.setMinimumWidth(340)
        right_scroll.setMaximumWidth(430)
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(10, 10, 10, 10)

        form = QFormLayout()
        self.date_combo = QComboBox()
        self.date_combo.currentIndexChanged.connect(self._date_changed)
        self.method_combo = QComboBox()
        self.method_combo.currentIndexChanged.connect(self._method_changed)
        self.meta_lists = QLabel("-")
        self.meta_workers = QLabel("-")
        self.meta_targets = QLabel("-")
        self.meta_duration = QLabel("-")
        self.allocation_label = QLabel("")
        self.allocation_label.setWordWrap(True)

        form.addRow("날짜", self.date_combo)
        form.addRow("방법", self.method_combo)
        form.addRow("피킹리스트 수", self.meta_lists)
        form.addRow("작업자 수", self.meta_workers)
        form.addRow("피킹 대상", self.meta_targets)
        form.addRow("총 재생시간", self.meta_duration)
        right_layout.addLayout(form)
        right_layout.addWidget(self.allocation_label)

        self.status = QTextBrowser()
        self.status.setOpenExternalLinks(False)
        self.status.setMinimumHeight(235)
        self.status.setStyleSheet(
            "QTextBrowser { background:#f7f9fc; border:1px solid #e5e7eb; "
            "border-radius:8px; padding:6px; }"
        )
        right_layout.addWidget(self.status)

        note = QLabel(
            "• 작업자 위치는 Qt QPainter로 약 60 FPS 렌더링합니다.\n"
            "• 빨간색은 월별 JSON의 실제 DES resource-contention 구간입니다.\n"
            "• 주황색은 당일 Picking_Wave 피킹 대상 포인트입니다.\n"
            "• 점선 사각형은 Z01~Z04 인력배치 Macro-zone입니다."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color:#5c667a; font-size:11px;")
        right_layout.addWidget(note)

        self.worker_list = QListWidget()
        self.worker_list.setMinimumHeight(220)
        self.worker_list.itemClicked.connect(self._worker_list_clicked)
        right_layout.addWidget(self.worker_list, 1)

        right_scroll.setWidget(right)
        splitter.addWidget(right_scroll)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 0)
        splitter.setSizes([1150, 360])

    def _populate_dates(self) -> None:
        self.date_combo.blockSignals(True)
        self.date_combo.clear()
        for date_text in self.store.date_order:
            workers = int(self.store.availability(date_text).get("observed_workers") or 0)
            self.date_combo.addItem(f"{date_text} ({workers})", date_text)
        idx = self.date_combo.findData(self.current_date)
        self.date_combo.setCurrentIndex(max(0, idx))
        self.date_combo.blockSignals(False)

    def _populate_methods(self) -> None:
        self.method_combo.blockSignals(True)
        self.method_combo.clear()
        for method in METHODS:
            self.method_combo.addItem(METHOD_LABELS[method], method)
        idx = self.method_combo.findData(self.current_method)
        self.method_combo.setCurrentIndex(max(0, idx))
        self.method_combo.blockSignals(False)
        self._update_method_enabled(self.current_date)

    def _update_method_enabled(self, date_text: str) -> None:
        model = self.method_combo.model()
        for i in range(self.method_combo.count()):
            method = str(self.method_combo.itemData(i))
            enabled = self.store.method_available(date_text, method)
            item = model.item(i)
            if item is not None:
                item.setEnabled(enabled)

    def _set_busy(self, busy: bool) -> None:
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor) if busy else QApplication.restoreOverrideCursor()

    def _load_scenario(self, date_text: str, method: str, *, auto_play: bool) -> None:
        self._stop()
        self._set_busy(True)
        try:
            self.current_date = date_text
            self._update_method_enabled(date_text)
            if not self.store.method_available(date_text, method):
                method = "observed"
            self.current_method = method

            scenarios = self.store.scenarios_for_date(date_text)
            scenario = scenarios.get(method) or scenarios.get("observed")
            if not isinstance(scenario, dict):
                raise ValueError(f"시나리오가 없습니다: {date_text} / {method}")

            self.scenario = scenario
            self.runtime = ScenarioRuntime(scenario)
            self.canvas.set_runtime(self.runtime)
            self.current_time = 0.0
            self.has_started = False
            self.selected_worker = "ALL"

            self.date_combo.blockSignals(True)
            self.date_combo.setCurrentIndex(self.date_combo.findData(date_text))
            self.date_combo.blockSignals(False)
            self.method_combo.blockSignals(True)
            self.method_combo.setCurrentIndex(self.method_combo.findData(method))
            self.method_combo.blockSignals(False)

            self.slider.blockSignals(True)
            self.slider.setValue(0)
            self.slider.blockSignals(False)
            self._populate_workers()
            self._update_meta()
            self._render_current(force_ui=True)
            if auto_play:
                self._start()
        except Exception as exc:
            QMessageBox.critical(self, "데이터 로드 실패", str(exc))
            raise
        finally:
            self._set_busy(False)

    def _populate_workers(self) -> None:
        self.worker_combo.blockSignals(True)
        self.worker_combo.clear()
        self.worker_combo.addItem("전체 작업자", "ALL")
        self.worker_list.clear()

        if self.runtime is not None:
            for index, worker in enumerate(self.runtime.workers):
                worker_id = str(worker.get("worker_id") or "")
                self.worker_combo.addItem(worker_id, worker_id)
                distance = float(worker.get("total_distance_m") or 0.0)
                moves = int(worker.get("movement_events") or 0)
                picks = int(worker.get("pick_events") or 0)
                item = QListWidgetItem(f"●  {worker_id}    {distance:.1f} m\n    move={moves}, pick={picks}")
                color = QColor.fromHsv((index * 47) % 360, 178, 210)
                item.setForeground(color)
                item.setData(Qt.ItemDataRole.UserRole, worker_id)
                self.worker_list.addItem(item)
        self.worker_combo.setCurrentIndex(0)
        self.worker_combo.blockSignals(False)

    def _update_meta(self) -> None:
        if self.scenario is None or self.runtime is None:
            return
        meta = self.scenario.get("meta") or {}
        self.meta_lists.setText(str(meta.get("picking_lists", "-")))
        self.meta_workers.setText(str(meta.get("operators", "-")))
        self.meta_targets.setText(f"{len(self.runtime.pick_targets)}개 포인트")
        self.meta_duration.setText(_format_seconds(self.runtime.end_seconds))

        counts = meta.get("worker_counts")
        parts: list[str] = []
        if isinstance(counts, list):
            parts.append(f"구역 인원: [{', '.join(str(v) for v in counts)}]")
        if meta.get("entropy_lambda") is not None:
            parts.append(f"λ={meta.get('entropy_lambda')}")

        availability = self.store.availability(self.current_date)
        if not availability.get("comparison_eligible", True):
            parts.append(
                "비교방법 사용 불가 · "
                + _reason_label(str(availability.get("reason") or ""))
            )
        self.allocation_label.setText(" · ".join(parts))

    def _toggle_play(self) -> None:
        self._stop() if self.playing else self._start()

    def _start(self) -> None:
        if self.runtime is None:
            return
        if self.current_time >= self.runtime.end_seconds:
            self.current_time = 0.0
            self.runtime.reset_cursors()
        if self.playing:
            return
        self.has_started = True
        self.playing = True
        self.play_btn.setText("⏸ 일시정지")
        self.frame_clock.restart()
        self._ui_elapsed_since_refresh = 0.0
        self.timer.start()
        self._render_current(force_ui=True)

    def _stop(self) -> None:
        self.playing = False
        self.timer.stop()
        if hasattr(self, "play_btn"):
            self.play_btn.setText("▶ 재생")

    def _reset_frame_clock(self) -> None:
        if self.playing:
            self.frame_clock.restart()

    def _tick(self) -> None:
        if not self.playing or self.runtime is None:
            return
        elapsed_ms = self.frame_clock.restart()
        dt = max(0.0, elapsed_ms / 1000.0)
        speed = float(self.speed_combo.currentData() or 1.0)
        self.current_time += dt * speed
        self._ui_elapsed_since_refresh += dt * 1000.0

        if self.current_time >= self.runtime.end_seconds:
            self.current_time = self.runtime.end_seconds
            self._render_current(force_ui=True)
            self._stop()
            if self.auto_next_chk.isChecked():
                self._advance_date()
            return

        force_ui = self._ui_elapsed_since_refresh >= UI_REFRESH_MS
        if force_ui:
            self._ui_elapsed_since_refresh = 0.0
        self._render_current(force_ui=force_ui)

    def _render_current(self, *, force_ui: bool) -> None:
        if self.runtime is None or self.scenario is None:
            return
        frames, conflict = self.runtime.frame(
            self.current_time,
            selected_worker=self.selected_worker,
            started=self.has_started,
        )
        self.canvas.selected_worker = self.selected_worker
        self.canvas.set_frame(frames, conflict)

        if not force_ui:
            return
        self.slider.blockSignals(True)
        if self.runtime.end_seconds > 0:
            ratio = max(0.0, min(1.0, self.current_time / self.runtime.end_seconds))
            self.slider.setValue(int(round(ratio * SLIDER_STEPS)))
        else:
            self.slider.setValue(0)
        self.slider.blockSignals(False)
        self.time_label.setText(_format_seconds(self.current_time))
        self._update_status(frames, conflict)

    def _update_status(self, frames: list[WorkerFrame], conflict: ConflictSnapshot) -> None:
        if self.scenario is None or self.runtime is None:
            return
        meta = self.scenario.get("meta") or {}
        visible = [state for state in frames if state.visible]
        state_text = "재생 대기"
        if self.has_started:
            snippets = []
            for state in visible[:7]:
                worker_id = str(state.worker.get("worker_id") or "")
                suffix = ", 충돌" if state.colliding else ""
                snippets.append(f"{worker_id}({state.kind}{suffix})")
            state_text = ", ".join(snippets) + (" ..." if len(visible) > 7 else "")

        previews: list[str] = []
        for event in conflict.active[:5]:
            worker_ids = "↔".join(str(v) for v in event.get("worker_ids") or []) or "?"
            resource_id = event.get("resource_id")
            resource_type = event.get("resource_type")
            resource = f" · {resource_type}:{resource_id}" if resource_id else ""
            previews.append(worker_ids + resource)
        conflict_preview = ", ".join(previews) or "없음"
        if len(conflict.active) > 5:
            conflict_preview += " ..."

        total_conflicts = int(meta.get("congestion_conflicts") or 0)
        total_wait = float(meta.get("congestion_wait_seconds") or 0.0)
        actual_time = _actual_datetime(meta.get("origin_timestamp"), self.current_time)
        exact_source = "실제 DES resource contention" if self.runtime.has_exact_conflicts else "실제 이벤트 없음"

        self.status.setHtml(
            f"<b>현재 시간</b> : {_format_seconds(self.current_time)}<br>"
            f"<b>실제 시간</b> : {html_lib.escape(actual_time)}<br>"
            f"<b>표시 작업자</b> : {html_lib.escape('전체' if self.selected_worker == 'ALL' else self.selected_worker)}<br>"
            f"<b>활성 마커 수</b> : {len(visible)}<br>"
            f"<b>피킹 대상 포인트</b> : {len(self.runtime.pick_targets)}개<br>"
            f"<b>DES Conflicts</b> : {total_conflicts}회 · <b>총 대기</b> : {total_wait:.2f}초<br>"
            f"<b>현재 충돌 이벤트</b> : {len(conflict.active)}개 · <b>충돌 피커</b> : {len(conflict.collision_workers)}명<br>"
            f"<b>누적 충돌 이벤트</b> : {conflict.cumulative_count}개 · "
            f"<b>누적 충돌 피커</b> : {conflict.cumulative_picker_count}명<br>"
            f"<b>현재 충돌</b> : {html_lib.escape(conflict_preview)}<br>"
            f"<b>이벤트 소스</b> : {exact_source}<br>"
            f"<b>렌더링</b> : Qt QPainter · QSvgRenderer · ~60 FPS<br>"
            f"<b>상태</b> : {html_lib.escape(state_text)}"
        )

    def _slider_changed(self, value: int) -> None:
        if self.runtime is None:
            return
        self.has_started = True
        ratio = value / SLIDER_STEPS
        self.current_time = max(0.0, min(self.runtime.end_seconds, self.runtime.end_seconds * ratio))
        self._reset_frame_clock()
        self._render_current(force_ui=True)

    def _worker_changed(self, _index: int) -> None:
        value = self.worker_combo.currentData()
        if value is None:
            return
        self.selected_worker = str(value)
        self._render_current(force_ui=True)

    def _select_worker_from_canvas(self, worker_id: str) -> None:
        idx = self.worker_combo.findData(worker_id)
        if idx >= 0:
            self.worker_combo.setCurrentIndex(idx)

    def _worker_list_clicked(self, item: QListWidgetItem) -> None:
        worker_id = item.data(Qt.ItemDataRole.UserRole)
        if worker_id:
            self._select_worker_from_canvas(str(worker_id))

    def _targets_toggled(self, checked: bool) -> None:
        self.canvas.show_targets = checked
        self.canvas.invalidate_static_cache()

    def _zones_toggled(self, checked: bool) -> None:
        self.canvas.show_zones = checked
        self.canvas.invalidate_static_cache()

    def _date_changed(self, _index: int) -> None:
        date_text = self.date_combo.currentData()
        if date_text is None or not hasattr(self, "scenario"):
            return
        self._load_scenario(str(date_text), self.current_method, auto_play=False)

    def _method_changed(self, _index: int) -> None:
        method = self.method_combo.currentData()
        if method is None or not hasattr(self, "scenario"):
            return
        self._load_scenario(self.current_date, str(method), auto_play=False)

    def _advance_date(self) -> None:
        try:
            idx = self.store.date_order.index(self.current_date)
        except ValueError:
            return
        method = self.current_method
        if method == "observed":
            if idx + 1 < len(self.store.date_order):
                self._load_scenario(self.store.date_order[idx + 1], method, auto_play=True)
            return
        for next_idx in range(idx + 1, len(self.store.date_order)):
            next_date = self.store.date_order[next_idx]
            if self.store.method_available(next_date, method):
                self._load_scenario(next_date, method, auto_play=True)
                return

    def closeEvent(self, event: Any) -> None:
        self._stop()
        super().closeEvent(event)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Native PySide6 desktop viewer for picking_animation_actual monthly JSON. "
            "No browser or localhost web server is required."
        )
    )
    parser.add_argument(
        "--html",
        default="results/figures/picking_animation_actual.html",
        help="existing HTML is used only for its lightweight manifest (not rendered)",
    )
    parser.add_argument(
        "--json-dir",
        default=None,
        help="monthly JSON directory; default=<html-stem>_data",
    )
    parser.add_argument(
        "--layout-svg",
        default="data/raw_original/Layout_Z1.0.svg",
    )
    parser.add_argument(
        "--support-points",
        default="data/raw/Support_Points_Navigation.csv",
        help="used only to calibrate Z01~Z04 rectangles",
    )
    parser.add_argument("--date", default=None, help="initial date YYYY-MM-DD")
    parser.add_argument(
        "--method",
        choices=METHODS,
        default="observed",
        help="initial allocation method",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    html_path = Path(args.html)
    json_dir = Path(args.json_dir) if args.json_dir else None
    svg_path = Path(args.layout_svg)
    support_csv = Path(args.support_points)

    if not svg_path.exists():
        raise FileNotFoundError(f"Layout SVG가 없습니다: {svg_path}")

    store = MonthlyDataStore(html_path=html_path, json_dir=json_dir)
    zones = build_macro_zones(svg_path, support_csv)

    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("Warehouse Picking Animation")
    app.setStyle("Fusion")

    window = PickingAnimationWindow(
        store=store,
        svg_path=svg_path,
        zones=zones,
        start_date=args.date,
        start_method=args.method,
    )
    window.show()
    raise SystemExit(app.exec())


if __name__ == "__main__":
    main()
