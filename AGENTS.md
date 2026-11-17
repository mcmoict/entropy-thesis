# Project Instructions

이 저장소는 석사 논문 **엔트로피 기반 물류센터 피킹 작업자 배치 최적화** 연구용 Python 프로젝트입니다.

## Current Thesis Model

- Python: **3.13+**
- Simulation: **Discrete-Event Simulation (SimPy)**
- Common depot / I/O: **CC-08 (`SUP:CC-08`)**
- Source coordinate unit: **inch**, meter conversion `× 0.0254`
- Demand space: **20 micro-zones** (`LC-08~17`, `RC-08~17`)
- Workforce allocation space: **4 macro-zones** (`Z01~Z04`)
- Model Revision: **2026-08-22-cc08-inch-micro20-macro4-integer-objective-v1-pareto-knee-v1**

### Phase 3-6: Optimization & Validation

- Phase 3 controls: Observed Baseline / Random / Equal / Volume Proportional
- Phase 4 proposal: integer Entropy-based allocation with λ Calibration
- Final calibrated weight: **λ*=0.25** (Pareto-knee selected on Calibration 92 days)
- Phase 4 Calibration: 92 days (λ* selection)
- Phase 5 Frozen Holdout: 40 days (out-of-sample validation, no λ re-selection)
- Phase 6 Analysis: Generalization / mechanism / robustness

### Phase 8+: AI-Adaptive Extensions

- Phase 8 (XGBoost): XGBoost-based Adaptive λ_t selection on Frozen Holdout 40 days
- Phase 8-RF (Random Forest): Random Forest comparison with identical chronological split
- Phase 8-Boosting: LightGBM / CatBoost / Linear Regression / XGBoost benchmark
- All AI models: Tabular KPI prediction, Fixed-EWA Flow guardrail, chronological date split

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

### Data & Reproducibility

- 실험 재현성을 위해 random seed를 명시적으로 관리합니다.
- 원본 데이터에 없는 좌표를 임의 생성하지 않습니다.
- 과거 CC-01 / centimeter 모델이나 폐기된 연속 entropy-weight 방식의 결과를 현재 결과로 사용하지 않습니다.

### Phase 4-6: Optimization & Validation Integrity

- Phase 4 이전에 Calibration / Holdout을 분리합니다.
- Phase 5에서는 λ를 재선택하거나 Holdout을 재분할하지 않습니다.
- Phase 6에서도 λ 또는 Holdout을 재조정하지 않습니다.
- Frozen Holdout은 λ 선택에 사용하지 않으며, Phase 4에서 선택한 λ*를 고정으로 유지합니다.

### Simulation & Analysis

- `congestion_conflicts`는 사람 간 물리 충돌이 아니라 DES resource contention event로 해석합니다.
- 생성 결과는 `results/`에 저장하고 핵심 계산 코드는 `src/entropy_thesis/`에 둡니다.
- 변경 후 관련 `pytest`를 실행합니다.

### Phase 8+: AI-Adaptive Model Rules

- Phase 8의 AI 학습은 Phase 4 Calibration 날짜만 사용하며, 동일 날짜의 λ 후보가 Train/Validation에 동시에 들어가지 않도록 날짜 단위 chronological split을 사용합니다.
- Phase 8 Frozen Holdout에서 Adaptive λ 선택은 AI 예측 KPI만 사용하며, 선택이 끝난 뒤 선택된 정수배치만 실제 DES로 평가합니다.
- 예측 정확도보다 **최종 정수 배치 의사결정의 DES 성능 효과**를 중시합니다.
- 모든 AI 모델 (XGBoost, LightGBM, CatBoost, Random Forest)은 동일한 chronological split, 동일한 Tabular feature, 동일한 KPI 예측 target을 사용합니다.

## Documentation

- 전체 안내: `README.md`
- Phase 1-6 상세: `docs/phases/README_PHASE1.md` ~ `README_PHASE6.md`
- Phase 8 (XGBoost): `docs/phases/README_PHASE8.md`
- Phase 8-RF (Random Forest): `docs/phases/README_PHASE8_RF.md`
- Phase 8-Boosting (Benchmark): `docs/phases/README_PHASE8_BOOSTING.md`
- 시각화: `src/entropy_thesis/visualization/README.md`
