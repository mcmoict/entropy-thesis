# Phase 8-Boosting - 부스팅 모델 벤치마크 (XGBoost / LightGBM / CatBoost)

## 1. 목적

Phase 8의 XGBoost 기반 Adaptive EWA가 여러 부스팅 모델 중에서 얼마나 효과적인지 검증하기 위해 **동일한 chronological date split, 동일한 Tabular feature, 동일한 Frozen Holdout 평가 조건**에서 세 부스팅 모델을 비교합니다.

비교 대상:

```text
1. XGBoost (Phase 8 기본 모델)
2. LightGBM (고속 그래디언트 부스팅)
3. CatBoost (카테고리형 feature 최적화)
```

기준 모델 (비교용):

```text
1. Linear Regression (선형 기준)
2. Random Forest (트리 기반 비부스팅 기준)
```

---

## 2. 연구 질문

> **XGBoost가 충분히 좋은 선택인가? 아니면 LightGBM이나 CatBoost가 더 나은 KPI 예측을 제공하는가?**

더 넓게는:

> **KPI 예측 정확도의 차이가 최종 작업자 배치 의사결정에 얼마나 영향을 미치는가?**

---

## 3. 동일 조건 설정

### Data Split (Phase 8과 동일)

```text
Calibration : 92일
├─ Internal Train     : 73일 (앞 80%)
└─ Internal Validation: 19일 (뒤 20%)

Frozen Holdout : 40일
```

모든 모델은 이 동일한 chronological split을 사용하므로 모델 간 비교가 직접적입니다.

### Feature 동일

모든 모델 (Linear Regression 제외)은 Phase 8과 동일한 Tabular feature를 사용합니다.

```text
- 당일 Picking List 수
- Pick Task / Pick Unit 수
- 작업자 수
- Z01~Z04 workload share
- Z01~Z04 micro-zone concentration
- λ가 생성한 후보 정수 작업자 수 n_z
- Demand-Worker L1 mismatch D(n)
- Congestion-risk proxy R(n)
- Volume 대비 worker displacement
- 작업자당 task / unit
- macro-zone workload entropy
```

λ 숫자 자체는 feature에 포함하지 않습니다.

### Target 동일

```text
1. mean_flow_time_seconds
2. congestion_conflicts
3. congestion_wait_seconds
4. congestion_delay_ratio
```

각 모델마다 4개의 독립 회귀 모델을 학습합니다.

---

## 4. 모델 설정

### Linear Regression (선형 기준)

```python
from sklearn.linear_model import LinearRegression
# 기본 설정
```

간단한 선형 관계만 포착하므로 baseline으로 사용합니다.

### Random Forest (트리 비부스팅 기준)

```python
n_estimators = 400
min_samples_leaf = 2
max_features = 0.8
random_state = 42
n_jobs = -1
```

[Phase 8-RF](README_PHASE8_RF.md)에서 사용한 동일 설정입니다.

### XGBoost

```python
max_depth = 6
learning_rate = 0.1
n_estimators = 100
subsample = 0.8
colsample_bytree = 0.8
random_state = 42
n_jobs = -1
```

Phase 8의 기본 XGBoost 설정입니다.

### LightGBM

```python
num_leaves = 31
learning_rate = 0.1
n_estimators = 100
subsample = 0.8
colsample_bytree = 0.8
random_state = 42
n_jobs = -1
```

LightGBM의 표준 설정입니다.

### CatBoost

```python
iterations = 100
learning_rate = 0.1
depth = 6
subsample = 0.8
colsample_bylevel = 0.8
random_state = 42
thread_count = -1
verbose = 0
```

CatBoost의 기본 설정입니다.

---

## 5. 평가 지표

### 5.1 KPI 예측 성능 (내부 Validation 데이터)

| 지표 | 의미 |
|---|---|
| MAE | 평균 절대 오차 |
| RMSE | 제곱근 평균 제곱 오차 |
| R² | 결정계수 |

각 KPI별, 각 모델별로 계산합니다.

### 5.2 Adaptive λ 선택 일관성

Fixed EWA의 Adaptive λ=0.25와 각 모델의 Adaptive λ 분포를 비교합니다.

```text
Adaptive λ 선택이 얼마나 일관된가?
└─ Fixed EWA가 가장 많이 선택되는 모델이 가장 안정적
```

### 5.3 최종 정수 배치 수렴성

Adaptive λ로 계산한 정수 작업자 배치가 Fixed EWA와 얼마나 같은가?

```text
배치가 같음 = 최종 의사결정이 같음 = 최종 KPI도 같음
배치가 다름 = 새로운 DES 실행 필요
```

### 5.4 Frozen Holdout 최종 KPI

새로운 정수 배치를 선택한 경우에만 추가 DES를 실행하고, 최종 KPI를 Fixed EWA와 비교합니다.

---

## 6. 실행

### 6.1 내부검증 성능만 비교 (빠름)

```powershell
python -m entropy_thesis.simulation.phase8_boosting --data-dir data/raw --train-only
```

결과:

```text
results/phase8_boosting/boosting_model_benchmark.csv
```

이 단계에서는:

1. 각 모델의 KPI 예측 성능 (MAE/RMSE/R²)
2. Internal Validation의 Adaptive λ 선택 분포
3. 모델 간 비교표

---

### 6.2 Holdout 후보 선택까지만 (중간)

```powershell
python -m entropy_thesis.simulation.phase8_boosting --data-dir data/raw --selection-only
```

결과 추가:

```text
results/phase8_boosting/boosting_only_benchmark.csv
results/phase8_boosting/boosting_<model>_holdout_selection.csv
```

이 단계에서는:

1. Frozen Holdout에서 각 모델의 Adaptive λ 선택
2. λ 선택으로 인한 정수 배치 차이 분석
3. 각 모델이 추가 DES를 필요로 하는지 판단

---

### 6.3 최종 Holdout DES까지 (전체)

```powershell
python -m entropy_thesis.simulation.phase8_boosting --data-dir data/raw
```

최종 결과 추가:

```text
results/phase8_boosting/boosting_holdout_comparison.csv
```

이 단계에서:

1. 필요한 경우에만 새로운 정수 배치의 실제 DES 실행
2. Fixed EWA와의 최종 KPI 비교
3. 모델별 최종 의사결정 효과 평가

---

## 7. 주요 출력 파일

```text
results/phase8_boosting/

# 모델 성능 비교
├─ boosting_model_benchmark.csv           # 내부검증 KPI 예측 성능 (MAE/RMSE/R²)
├─ boosting_only_benchmark.csv            # 세 부스팅 모델만 추출 비교
├─ boosting_model_feature_importance/     # 각 모델의 feature importance
│  ├─ xgboost_importance.csv
│  ├─ lightgbm_importance.csv
│  └─ catboost_importance.csv
│
# Holdout 의사결정 분석
├─ boosting_xgboost_holdout_selection.csv       # XGBoost Adaptive λ
├─ boosting_lightgbm_holdout_selection.csv      # LightGBM Adaptive λ
├─ boosting_catboost_holdout_selection.csv      # CatBoost Adaptive λ
│
# 최종 Holdout 성능 비교
├─ boosting_holdout_comparison.csv        # Fixed EWA vs 각 모델 최종 KPI
├─ boosting_holdout_new_allocations.json  # 새로운 배치 정보 (필요 시)
│
# 모델 저장
├─ models/
│  ├─ xgboost/
│  │  ├─ mean_flow_time_seconds.json
│  │  ├─ congestion_conflicts.json
│  │  ├─ congestion_wait_seconds.json
│  │  └─ congestion_delay_ratio.json
│  ├─ lightgbm/
│  │  └─ (동일 구조)
│  └─ catboost/
│     └─ (동일 구조)
│
# 메타데이터
└─ boosting_metadata.json                 # 모델 설정 및 split 정보
```

---

## 8. 보고 방법

### 8.1 교수님께 설명할 때 중점

#### (1) KPI 예측 정확도 비교

> "세 부스팅 모델 중 XGBoost가 가장 균형잡혀 있고, CatBoost는 특정 KPI에서 더 나은 경우도 있습니다. 하지만..."

#### (2) **최종 의사결정 효과가 더 중요**

> "예측 정확도의 차이보다 중요한 것은 최종 작업자 배치 선택입니다. 모든 모델이 Fixed EWA와 같은 배치를 선택했다면, 최종 성능은 동일합니다."

#### (3) 수렴성 분석

```text
모든 모델이 Fixed EWA와 같은 배치 선택
└─ Holdout KPI 변화율 = 0.000% (동일)
└─ 예측 정확도 차이는 의사결정에 영향 없음
```

또는:

```text
XGBoost만 고정EWA와 다른 배치 선택
└─ Holdout에서 XGBoost가 의사결정 효과 발생
└─ LightGBM/CatBoost는 추가 개선 없음
```

#### (4) 최종 결론

> "따라서 XGBoost를 주 모델로 선택한 것은 합리적입니다. 세 부스팅 모델의 예측 성능이 비슷하다면 더 단순하고 빠른 모델을 선택할 수 있지만, 현재 결과에서는 XGBoost가 최종 Holdout 의사결정에서도 가장 좋은 선택을 보여줍니다."

---

## 9. 모델별 특성 요약

| 항목 | XGBoost | LightGBM | CatBoost |
|---|---|---|---|
| 속도 | 중간 | 가장 빠름 | 중간 |
| 메모리 | 중간 | 가장 적음 | 가장 많음 |
| Tabular 성능 | 우수 | 우수 | 우수 |
| Feature importance | 명확함 | 명확함 | 명확함 |
| 카테고리 처리 | 직접 지원 아님 | 약간 지원 | 최적화됨 |
| Hyperparameter 튜닝 | 간단함 | 간단함 | 복잡함 |

현재 Tabular feature는 대부분 수치형이므로 세 모델의 성능이 비슷합니다.

---

## 10. 최종 권장 명령어

빠른 검토:

```powershell
python -m entropy_thesis.simulation.phase8_boosting --data-dir data/raw --train-only
```

전체 비교:

```powershell
python -m entropy_thesis.simulation.phase8_boosting --data-dir data/raw
```

---

## 11. 다음 단계

Phase 8-Boosting 결과가 모두 동일한 최종 Holdout KPI를 보인다면, **XGBoost가 필요한 복잡도의 적절한 선택**임을 확인할 수 있습니다.

새로운 배치가 생기거나 KPI 개선이 발생한다면, 그에 해당하는 모델의 특성을 분석하여 향후 AI 모델 선택에 반영합니다.
