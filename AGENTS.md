# Project Instructions

이 저장소는 석사 논문 **엔트로피 기반 물류센터 피킹 작업자 배치 최적화** 연구용 Python 프로젝트입니다.

## Current Thesis Model

- Python: **3.13+**
- Simulation: **Discrete-Event Simulation (SimPy)**
- Common depot / I/O: **CC-08 (`SUP:CC-08`)**
- Source coordinate unit: **inch**, meter conversion `× 0.0254`
- Demand space: **20 micro-zones** (`LC-08~17`, `RC-08~17`)
- Workforce allocation space: **4 macro-zones** (`Z01~Z04`)
- Phase 3 controls: Observed Baseline / Random / Equal / Volume Proportional
- Phase 4 proposal: integer Entropy-based allocation
- Final calibrated weight: **λ*=0.25**
- Validation: chronological Calibration 92 days / Frozen Holdout 40 days

## Entropy Allocation Objective

```text
d_z = V_z / ΣV_z
p_z = n_z / N
D(n) = 0.5 × Σ |p_z - d_z|
C_z = 1 - H_z
R(n) = Σ C_z × C(n_z, 2)
J(n; λ) = D(n) + λR(n)
```

`λ=0`은 Volume Proportional 정수 배치의 control입니다.

## Development Rules

- 실험 재현성을 위해 random seed를 명시적으로 관리합니다.
- Phase 5/6에서는 λ를 재선택하거나 Holdout을 재분할하지 않습니다.
- 원본 데이터에 없는 좌표를 임의 생성하지 않습니다.
- `congestion_conflicts`는 사람 간 물리 충돌이 아니라 DES resource contention event로 해석합니다.
- 생성 결과는 `results/`에 저장하고 핵심 계산 코드는 `src/entropy_thesis/`에 둡니다.
- 변경 후 관련 `pytest`를 실행합니다.
- 과거 CC-01 / centimeter 모델이나 폐기된 연속 entropy-weight 방식의 결과를 현재 결과로 사용하지 않습니다.

## Documentation

- 전체 안내: `README.md`
- Phase 상세: `docs/phases/README_PHASE1.md` ~ `README_PHASE6.md`
- 시각화: `src/entropy_thesis/visualization/README.md`
