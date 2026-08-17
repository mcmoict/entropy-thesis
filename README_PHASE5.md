# Phase 5 - 다중 날짜 실험 및 검증

Phase 5는 Phase 3의 기존 작업자 배치 방법과 Phase 4에서 선택한 Entropy-based Allocation을 **여러 실제 운영일에서 반복 비교**하여 결과의 일반화 가능성을 검증한다.

핵심 원칙은 **Phase 4에서 선택한 λ(lambda)를 validation date마다 다시 최적화하지 않는 것**이다. calibration date에서 한 번 선택한 λ를 고정하고, 각 날짜의 실제 workload 분포에 맞추어 zone별 정수 작업자 수만 다시 계산한다. 이렇게 해야 특정 날짜에 과적합된 λ를 다른 날짜에서 공정하게 검증할 수 있다.

## 1. 비교 방법

각 validation date마다 동일 조건으로 다음 5개 방법을 비교한다.

```text
1. Baseline             : Picking_Wave.csv의 실제 operator 배정
2. Random               : 무작위 zone 작업자 배치
3. Equal                : 활성 zone 균등 배치
4. Volume Proportional  : zone workload 비례 배치
5. Entropy-based        : Phase 4에서 선택한 고정 λ 적용
```

작업자 수를 `--workers`로 직접 지정하지 않으면 각 날짜의 실제 observed operator 수를 사용한다.

## 2. λ 고정 규칙

기본 실행에서는 다음 파일을 읽는다.

```text
results/phase4/phase4_recommendation.json
```

즉 Phase 4에서 이미 선택한 λ를 그대로 사용한다. 이 파일이 없으면 Phase 4 calibration을 다시 수행한다.

직접 λ를 지정할 수도 있다.

```powershell
python -m entropy_thesis.simulation.phase5 --data-dir data/raw --entropy-weight 1
```

Phase 4를 강제로 다시 수행하려면:

```powershell
python -m entropy_thesis.simulation.phase5 --data-dir data/raw --recalibrate --calibration-date 2023-01-05
```

## 3. Validation date 선택

기본값은 calibration date를 제외하고, fully-valid list가 충분한 운영일 중 전체 기간에 걸쳐 **시간축 기준으로 균등하게 12일**을 선택한다.

```powershell
python -m entropy_thesis.simulation.phase5 --data-dir data/raw
```

검증 일수 변경:

```powershell
python -m entropy_thesis.simulation.phase5 --data-dir data/raw --validation-days 20
```

특정 날짜 직접 지정:

```powershell
python -m entropy_thesis.simulation.phase5 --data-dir data/raw --dates 2023-01-06,2023-02-16,2023-03-22,2023-05-16
```

모든 가능한 날짜 실행:

```powershell
python -m entropy_thesis.simulation.phase5 --data-dir data/raw --all-dates
```

기본적으로 fully-valid picking list가 20개 미만인 날짜는 검증 표본에서 제외한다. 기준을 바꾸려면 `--min-lists`를 사용한다.

## 4. 통계적 검증

운영일을 paired observation으로 두고 Entropy-based 방법과 각 비교 방법 사이에 다음을 계산한다.

```text
- 날짜별 KPI
- 방법별 평균 / 표준편차 / 중앙값 / 최소 / 최대
- Entropy의 wins / ties / losses
- 평균 개선율(%)
- paired Wilcoxon signed-rank test
- two-sided p-value
- p < 0.05 여부
```

Wilcoxon 검정은 날짜별 차이가 정규분포라고 강하게 가정하지 않는 비모수 paired test이다.

## 5. 주요 KPI

```text
mean_flow_time_seconds
makespan_seconds
congestion_wait_seconds
congestion_conflicts
total_distance_m
mean_release_delay_seconds
mean_spatial_entropy_normalized
```

기본 primary KPI는 Phase 4와 동일한 `mean_flow_time_seconds`이다.

## 6. 출력 파일

```text
results/phase5/
  phase5_daily_summary.csv
  phase5_date_profiles.csv
  phase5_allocations.csv
  phase5_method_summary.csv
  phase5_paired_comparison.csv
  phase5_primary_comparison.csv
  phase5_skipped_dates.csv
  phase5_metadata.json
```

### phase5_daily_summary.csv

날짜 × 방법 단위의 핵심 DES 결과이다. 논문의 일별 실험 원자료로 사용할 수 있다.

### phase5_method_summary.csv

방법 × KPI 단위의 전체 validation date 요약 통계이다.

### phase5_paired_comparison.csv

Entropy-based와 Baseline / Random / Equal / Volume Proportional을 KPI별로 paired 비교한다. 개선율, 승/무/패, Wilcoxon p-value가 포함된다.

### phase5_primary_comparison.csv

Phase 4에서 λ 선택에 사용한 primary KPI만 따로 추린 논문용 핵심 비교표이다.

## 7. 현재 Phase 4 결과에 대한 중요한 해석

현재 프로젝트의 `phase4_recommendation.json`에는 다음과 같이 저장되어 있다.

```text
selection metric = mean_flow_time_seconds
selected lambda  = 0
```

Phase 4 수식에서 `λ=0`은 Volume Proportional Allocation과 동일하다. 따라서 이 값을 그대로 Phase 5에 적용하면 Entropy-based와 Volume Proportional이 같은 worker allocation을 만들며 동일한 DES 결과가 나오는 것이 정상이다.

이 결과는 실패가 아니라 중요한 연구 결과이다. 현재 calibration 조건과 Mean Flow Time 기준에서는 **추가적인 entropy regularization(λ > 0)이 성능 개선으로 선택되지 않았다**는 뜻이다. Phase 5는 이 결론이 여러 날짜에서도 유지되는지 검증한다.

## 8. 권장 실행 순서

먼저 빠른 smoke test:

```powershell
python -m entropy_thesis.simulation.phase5 --data-dir data/raw --validation-days 3 --max-lists 20 --min-lists 10
```

그 다음 본 실험:

```powershell
python -m entropy_thesis.simulation.phase5 --data-dir data/raw --validation-days 12
```

논문 최종 robustness check가 필요하면 명시적으로 날짜 수를 늘리거나 `--all-dates`를 사용할 수 있다.
