from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Literal

import pandas as pd


CoordinateUnit = Literal["centimeter", "meter"]


@dataclass(frozen=True)
class StorageLocation:
    """Storage_Location.csv 한 행.

    x_m/y_m는 작업자 이동거리 계산에 사용할 평면 좌표(m)이고,
    level은 랙의 수직 레벨(1~4)이다. 이 데이터셋의 z는 실제 보행 높이가
    아니라 랙 레벨이므로 보행거리에는 직접 더하지 않는다.
    """

    location_id: str
    x_m: float
    y_m: float
    level: int
    raw_x: float
    raw_y: float
    raw_z: float


@dataclass(frozen=True)
class SupportPoint:
    """Support_Points_Navigation.csv 한 행."""

    point_id: str
    label: str
    corridor: str
    corridor_index: int
    x_m: float
    y_m: float
    level: int
    raw_x: float
    raw_y: float
    raw_z: float


@dataclass(frozen=True)
class Product:
    reference: str
    abc_code: str | None
    sector: str | None


@dataclass(frozen=True)
class CustomerOrderLine:
    customer_code: str
    order_number: str
    collect_sequence: int | None
    reference: str
    size_us: float | None
    quantity_units: float
    creation_date: pd.Timestamp
    wave_number: str
    operator: str


@dataclass(frozen=True)
class PickTask:
    """Picking_Wave.csv의 실제 행 순서를 보존한 피킹 작업."""

    wave_number: str
    operator: str
    sequence: int
    wave_sequence: int
    reference: str
    size_us: float | None
    quantity_units: float
    location_id: str
    abc_code: str | None = None
    sector: str | None = None


@dataclass(frozen=True)
class PickingList:
    """한 wave 안에서 한 operator에게 실제 배정된 피킹 리스트.

    실제 Picking_Wave.csv에는 같은 waveNumber에 여러 operator가 존재하는
    경우가 있으므로 waveNumber만으로 묶지 않고 (waveNumber, operator) 단위로
    분리한다.
    """

    wave_number: str
    operator: str
    picks: tuple[PickTask, ...]
    order_lines: tuple[CustomerOrderLine, ...] = ()

    @property
    def order_numbers(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(line.order_number for line in self.order_lines))

    @property
    def created_at(self) -> pd.Timestamp | None:
        if not self.order_lines:
            return None
        return min(line.creation_date for line in self.order_lines)


@dataclass(frozen=True)
class DatasetBundle:
    storage_locations: tuple[StorageLocation, ...]
    support_points: tuple[SupportPoint, ...]
    products: tuple[Product, ...]
    customer_orders: tuple[CustomerOrderLine, ...]
    picking_lists: tuple[PickingList, ...]


def _read_csv(path: str | Path, sep: str) -> pd.DataFrame:
    return pd.read_csv(
        Path(path),
        sep=sep,
        encoding="utf-8-sig",
        low_memory=False,
    )


def _normalize_text(value: object) -> str:
    if pd.isna(value):
        return ""
    return str(value).strip()


def _normalize_id(value: object) -> str:
    if pd.isna(value):
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    text = str(value).strip()
    if text.endswith(".0"):
        try:
            number = float(text)
            if number.is_integer():
                return str(int(number))
        except ValueError:
            pass
    return text


def _to_optional_float(value: object) -> float | None:
    if pd.isna(value):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coordinate_scale(coordinate_unit: CoordinateUnit) -> float:
    if coordinate_unit == "centimeter":
        return 0.01
    if coordinate_unit == "meter":
        return 1.0
    raise ValueError(f"지원하지 않는 coordinate_unit: {coordinate_unit}")


def _parse_coordinate(value: object) -> tuple[float, float, float]:
    if isinstance(value, (tuple, list)) and len(value) >= 3:
        return float(value[0]), float(value[1]), float(value[2])

    text = _normalize_text(value)
    if not text:
        raise ValueError("좌표 값이 비어 있습니다.")

    try:
        parsed = ast.literal_eval(text)
        if isinstance(parsed, (tuple, list)) and len(parsed) >= 3:
            return float(parsed[0]), float(parsed[1]), float(parsed[2])
    except (SyntaxError, ValueError):
        pass

    parts = [part.strip() for part in text.strip("()[]").split(",")]
    if len(parts) < 3:
        raise ValueError(f"좌표 형식을 해석할 수 없습니다: {value!r}")
    return float(parts[0]), float(parts[1]), float(parts[2])


def load_storage_locations(
    path: str | Path,
    *,
    coordinate_unit: CoordinateUnit = "centimeter",
) -> list[StorageLocation]:
    """실제 Storage_Location.csv 스키마를 읽는다.

    expected columns:
      originalLocation, position, x, y, z

    이 데이터셋의 평면 좌표는 CAD 기반 숫자이며 66, 403, 1471과 같은 값이다.
    기본값은 cm -> m(0.01 배율)로 변환한다. 원본이 이미 m라고 판단하는 경우
    coordinate_unit="meter"로 실행하면 된다.
    """

    df = _read_csv(path, sep=",")
    required = {"originalLocation", "x", "y", "z"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Storage_Location.csv 필수 컬럼 누락: {sorted(missing)}")

    scale = _coordinate_scale(coordinate_unit)
    result: list[StorageLocation] = []
    for row in df.itertuples(index=False):
        location_id = _normalize_text(getattr(row, "originalLocation"))
        if not location_id:
            continue

        raw_x = float(getattr(row, "x"))
        raw_y = float(getattr(row, "y"))
        raw_z = float(getattr(row, "z"))
        result.append(
            StorageLocation(
                location_id=location_id,
                x_m=raw_x * scale,
                y_m=raw_y * scale,
                level=int(round(raw_z)),
                raw_x=raw_x,
                raw_y=raw_y,
                raw_z=raw_z,
            )
        )
    return result


def load_support_points(
    path: str | Path,
    *,
    coordinate_unit: CoordinateUnit = "centimeter",
) -> list[SupportPoint]:
    """실제 Support_Points_Navigation.csv 스키마를 읽는다.

    expected columns (semicolon separated):
      points_specified;labels
    """

    df = _read_csv(path, sep=";")
    required = {"points_specified", "labels"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(
            f"Support_Points_Navigation.csv 필수 컬럼 누락: {sorted(missing)}"
        )

    scale = _coordinate_scale(coordinate_unit)
    result: list[SupportPoint] = []
    for row in df.itertuples(index=False):
        label = _normalize_text(getattr(row, "labels"))
        if not label:
            continue
        raw_x, raw_y, raw_z = _parse_coordinate(getattr(row, "points_specified"))

        parts = label.split("-", maxsplit=1)
        corridor = parts[0].upper()
        try:
            corridor_index = int(parts[1])
        except (IndexError, ValueError):
            corridor_index = 0

        result.append(
            SupportPoint(
                point_id=f"SUP:{label}",
                label=label,
                corridor=corridor,
                corridor_index=corridor_index,
                x_m=raw_x * scale,
                y_m=raw_y * scale,
                level=int(round(raw_z)),
                raw_x=raw_x,
                raw_y=raw_y,
                raw_z=raw_z,
            )
        )
    return result


def load_products(path: str | Path) -> list[Product]:
    """실제 Product.csv 스키마: Reference;ABCCOD;Sector"""

    df = _read_csv(path, sep=";")
    required = {"Reference", "ABCCOD", "Sector"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Product.csv 필수 컬럼 누락: {sorted(missing)}")

    products: list[Product] = []
    for row in df.itertuples(index=False):
        reference = _normalize_text(getattr(row, "Reference"))
        if not reference:
            continue
        abc = _normalize_text(getattr(row, "ABCCOD")) or None
        sector = _normalize_text(getattr(row, "Sector")) or None
        products.append(Product(reference=reference, abc_code=abc, sector=sector))
    return products


def load_customer_orders(path: str | Path) -> list[CustomerOrderLine]:
    """실제 Customer_Order.csv 스키마를 주문 라인 단위로 읽는다."""

    df = _read_csv(path, sep=";")
    required = {
        "codCustomer",
        "orderNumber",
        "orderToCollect",
        "Reference",
        "Size (US)",
        "quantity (units)",
        "creationDate",
        "waveNumber",
        "operator",
    }
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Customer_Order.csv 필수 컬럼 누락: {sorted(missing)}")

    result: list[CustomerOrderLine] = []
    for row in df.itertuples(index=False, name=None):
        values = dict(zip(df.columns, row))
        created = pd.to_datetime(
            values["creationDate"],
            format="%d/%m/%Y %H:%M",
            errors="coerce",
        )
        if pd.isna(created):
            created = pd.to_datetime(values["creationDate"], dayfirst=True, errors="raise")

        collect_sequence = None
        raw_collect = values["orderToCollect"]
        if not pd.isna(raw_collect):
            collect_sequence = int(raw_collect)

        result.append(
            CustomerOrderLine(
                customer_code=_normalize_text(values["codCustomer"]),
                order_number=_normalize_id(values["orderNumber"]),
                collect_sequence=collect_sequence,
                reference=_normalize_text(values["Reference"]),
                size_us=_to_optional_float(values["Size (US)"]),
                quantity_units=float(values["quantity (units)"]),
                creation_date=created,
                wave_number=_normalize_id(values["waveNumber"]),
                operator=_normalize_text(values["operator"]),
            )
        )
    return result


def load_picking_lists(
    picking_wave_path: str | Path,
    customer_order_path: str | Path | None = None,
    product_path: str | Path | None = None,
) -> list[PickingList]:
    """Picking_Wave.csv를 (waveNumber, operator)별 실제 피킹 리스트로 만든다.

    중요:
    - CSV의 원래 행 순서를 피킹 순서로 보존한다.
    - 동일 waveNumber에 여러 operator가 있는 88개 wave가 실제 데이터에 존재하므로
      waveNumber만으로 단일 작업자에게 귀속시키지 않는다.
    - Product.csv를 넘기면 ABC/Sector 정보를 PickTask에 붙인다.
    - Customer_Order.csv를 넘기면 동일 wave의 주문 라인을 연결한다.
    """

    df = _read_csv(picking_wave_path, sep=";").reset_index(drop=False)
    required = {
        "waveNumber",
        "reference",
        "Size (US)",
        "quantityToPick (units)",
        "locations",
        "operator",
    }
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Picking_Wave.csv 필수 컬럼 누락: {sorted(missing)}")

    products_by_ref: dict[str, Product] = {}
    if product_path is not None:
        products_by_ref = {p.reference: p for p in load_products(product_path)}

    orders_by_wave: dict[str, list[CustomerOrderLine]] = {}
    if customer_order_path is not None:
        for line in load_customer_orders(customer_order_path):
            orders_by_wave.setdefault(line.wave_number, []).append(line)

    # wave 내부의 원본 행 순서(0,1,2...)를 별도 보존한다.
    df["_wave_sequence"] = df.groupby("waveNumber", sort=False).cumcount()

    result: list[PickingList] = []
    grouped = df.groupby(["waveNumber", "operator"], sort=False, dropna=False)
    for (raw_wave, raw_operator), group in grouped:
        wave_number = _normalize_id(raw_wave)
        operator = _normalize_text(raw_operator)
        if not wave_number or not operator:
            continue

        picks: list[PickTask] = []
        for local_sequence, (_, row) in enumerate(group.iterrows()):
            reference = _normalize_text(row["reference"])
            product = products_by_ref.get(reference)
            picks.append(
                PickTask(
                    wave_number=wave_number,
                    operator=operator,
                    sequence=local_sequence,
                    wave_sequence=int(row["_wave_sequence"]),
                    reference=reference,
                    size_us=_to_optional_float(row["Size (US)"]),
                    quantity_units=float(row["quantityToPick (units)"]),
                    location_id=_normalize_text(row["locations"]),
                    abc_code=product.abc_code if product else None,
                    sector=product.sector if product else None,
                )
            )

        result.append(
            PickingList(
                wave_number=wave_number,
                operator=operator,
                picks=tuple(picks),
                order_lines=tuple(orders_by_wave.get(wave_number, ())),
            )
        )

    return result


def load_dataset(
    data_dir: str | Path,
    *,
    coordinate_unit: CoordinateUnit = "centimeter",
    progress_callback: Callable[[str, int, int, Path], None] | None = None,
) -> DatasetBundle:
    """사용자가 업로드한 5개 CSV를 한 번에 읽는 편의 함수.

    ``progress_callback``은 CLI 진행상태 표시용 선택적 hook이다. 콜백은
    ``(state, completed, total, path)``를 받으며 state는 ``start`` 또는
    ``done``이다. 기존 호출자는 콜백을 넘기지 않으면 종전과 동일하다.
    """

    data_dir = Path(data_dir)
    total_files = 5
    completed = 0

    def notify(state: str, path: Path) -> None:
        if progress_callback is not None:
            progress_callback(state, completed, total_files, path)

    product_path = data_dir / "Product.csv"
    notify("start", product_path)
    products = load_products(product_path)
    completed += 1
    notify("done", product_path)

    customer_order_path = data_dir / "Customer_Order.csv"
    notify("start", customer_order_path)
    customer_orders = load_customer_orders(customer_order_path)
    completed += 1
    notify("done", customer_order_path)

    picking_wave_path = data_dir / "Picking_Wave.csv"
    notify("start", picking_wave_path)
    picking_lists = load_picking_lists(
        picking_wave_path,
        customer_order_path,
        product_path,
    )
    completed += 1
    notify("done", picking_wave_path)

    storage_path = data_dir / "Storage_Location.csv"
    notify("start", storage_path)
    storage_locations = tuple(
        load_storage_locations(storage_path, coordinate_unit=coordinate_unit)
    )
    completed += 1
    notify("done", storage_path)

    support_path = data_dir / "Support_Points_Navigation.csv"
    notify("start", support_path)
    support_points = tuple(
        load_support_points(support_path, coordinate_unit=coordinate_unit)
    )
    completed += 1
    notify("done", support_path)

    return DatasetBundle(
        storage_locations=storage_locations,
        support_points=support_points,
        products=tuple(products),
        customer_orders=tuple(customer_orders),
        picking_lists=tuple(picking_lists),
    )
