# Phase 1 - 실제 업로드 CSV 스키마 대응 버전

대상 파일:

- `data/raw/Storage_Location.csv`
- `data/raw/Support_Points_Navigation.csv`
- `data/raw/Customer_Order.csv`
- `data/raw/Picking_Wave.csv`
- `data/raw/Product.csv`

추가/교체 파일:

```text
src/entropy_thesis/simulation/
├─ __init__.py
├─ data_loader.py
├─ warehouse.py
├─ worker.py
└─ phase1.py

tests/
├─ test_phase1_data_loader.py
└─ test_phase1_warehouse.py
```

기존 `simulation/model.py`, `main.py`, `allocation/` 등은 건드리지 않는다.

## 실행

프로젝트 루트에서:

```bash
python -m entropy_thesis.simulation.phase1 --data-dir data/raw
```

`src` layout 때문에 import가 안 되면 editable install 후 실행:

```bash
pip install -e .
python -m entropy_thesis.simulation.phase1 --data-dir data/raw
```

networkx 오류 발생하면 패키지 설치:
(thesis-env) PS D:\workspace\entropy-thesis> pip install networkx 실행

```bash
pip install networkx
```

## 필요한 패키지

- pandas
- networkx
- simpy

## 중요한 모델링 가정

1. CAD 좌표값은 기본적으로 cm로 보고 0.01을 곱해 m로 변환한다.
2. `z=1~4`는 랙 레벨이며 작업자 평면 보행거리에는 직접 사용하지 않는다.
3. 각 Storage Location은 가장 가까운 Support Point의 y 좌표에 있는 피킹 통로로 투영한다.
4. `Picking_Wave.csv`의 행 순서를 실제 피킹 순서로 보존한다.
5. 같은 wave에 여러 operator가 실제로 존재하므로 `(waveNumber, operator)` 단위로 PickingList를 만든다.
6. Storage_Location.csv에 좌표가 없는 `RC-01` 등의 위치는 좌표를 임의 생성하지 않는다. audit에서 unresolved로 기록한다.
