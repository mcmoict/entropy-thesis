# Phase 6 - Frozen Holdout Trade-off / Mechanism / Robustness Analysis

Phase 6는 Phase 5의 Frozen Holdout 결과를 **다시 최적화하지 않고** 분석한다. 목적은 세 가지이다.

1. Phase 4 Calibration에서 선택한 Pareto-knee trade-off가 Holdout에서도 같은 방향으로 나타나는지 확인한다.
2. Entropy(λ*)와 Volume의 정수 배치가 실제로 달라진 날짜를 추적하여 `수요 → 집중도 → 작업자 이동 → DES KPI` 메커니즘을 설명한다.
3. 4개 Pareto KPI에 대해 paired bootstrap CI와 Holm 보정 Wilcoxon을 추가하여 통계적 강건성을 확인한다.

Phase 6는 **λ를 다시 선택하지 않는다.** Holdout 날짜도 다시 고르지 않는다.

## 1. 입력 무결성

기본 입력은 다음과 같다.

```text
results/phase4/phase4_recommendation.json
results/phase5/phase5_metadata.json
results/phase5/phase5_daily_summary.csv
results/phase5/phase5_allocations.csv
results/phase5/phase5_allocation_equivalence.csv
```

실행 전에 다음을 자동 확인한다.

```text
- Phase 4 = 4E recommendation
- Phase 4 / Phase 5 model_revision 동일
- Phase 4 λ* = Phase 5 fixed λ
- selection_rule = pareto_knee
- Phase 4 Holdout = Phase 5 frozen Holdout
- 최종 실행에서는 frozen Holdout 전체 완료 + skip 0
```

따라서 과거 λ 또는 과거 물리모델의 Phase 5 결과를 잘못 섞어 분석하지 않는다.

## 2. 기본 실행

Phase 5 전체 Holdout 실행이 끝난 뒤 다음을 실행한다.

```powershell
python -m entropy_thesis.simulation.phase6
```

경로를 직접 지정하려면:

```powershell
python -m entropy_thesis.simulation.phase6 `
  --phase4-dir results/phase4 `
  --phase5-dir results/phase5 `
  --output-dir results/phase6
```

Bootstrap 기본 설정은 다음과 같다.

```text
samples = 10,000
seed    = 42
CI      = 95%
```

## 3. Calibration → Holdout Pareto 일반화

Phase 4에서 `λ=0`은 정확히 Volume Proportional 정수 배치를 control로 사용한다. 따라서 Phase 6에서는 Calibration의 `Entropy(λ*) vs λ=0`과 Holdout의 `Entropy(λ*) vs Volume`을 동일한 개념으로 비교한다.

보고 항목:

```text
Flow time cost (%)
Conflicts reduction (%)
Wait reduction (%)
Congestion ratio reduction (%)
Composite congestion reduction (%)
```

부호는 다음처럼 해석한다.

```text
Flow time cost > 0              : Entropy가 더 느림
Congestion reduction > 0        : Entropy가 혼잡을 감소시킴
```

Composite congestion index는 Phase 4와 동일하게 세 혼잡 KPI의 Entropy/Volume 평균비를 동일 가중으로 평균한다.

```text
I_cong = 1/3 × [
    Conflicts_entropy / Conflicts_volume
  + Wait_entropy      / Wait_volume
  + CongRatio_entropy / CongRatio_volume
]

Composite reduction (%) = 100 × (1 - I_cong)
```

이 값은 새로운 목적함수 가중치가 아니며, Holdout에서 λ를 재조정하는 데 사용하지 않는다.

## 4. 4개 Pareto KPI 통계 강건성

Phase 4 Pareto 선택에 사용한 다음 네 KPI를 동일하게 검증한다.

```text
mean_flow_time_seconds
congestion_conflicts
congestion_wait_seconds
congestion_delay_ratio
```

각 KPI에 대해:

```text
- Entropy / Volume Holdout mean
- mean 기준 improvement(%)
- W / T / L
- two-sided Wilcoxon signed-rank p-value
- Holm-adjusted p-value (4개 KPI family)
- paired nonparametric bootstrap 95% CI
```

Bootstrap CI의 native difference는 **Entropy 개선 방향이 +**가 되도록 방향을 통일한다.

```text
minimize KPI : Volume - Entropy
maximize KPI : Entropy - Volume
```

따라서 Flow Time CI가 음수이면 Entropy가 Volume보다 느린 방향이다.

## 5. 정수 배치 변경 날짜 메커니즘

Phase 5에서는 정수 제약 때문에 Entropy와 Volume이 같은 배치를 선택하는 날짜가 존재한다. Phase 6는 `same_as_volume = false`인 날짜만 별도로 추출한다.

이 subset은 **메커니즘 설명용**이다. 이 날짜만 골라 λ를 재선택하거나 최종 유의성 결론을 내리지 않는다.

각 변경 날짜에서 다음을 계산한다.

```text
Volume worker vector
Entropy worker vector
실제로 이동한 최소 worker 수
D_volume / D_entropy
R_volume / R_entropy
J_volume / J_entropy
작업자를 빼낸 Zone의 평균 concentration
작업자를 추가한 Zone의 평균 concentration
Flow / Conflicts / Wait / Congestion ratio / Release Delay / Makespan 변화
```

목적함수는 Phase 4와 동일하다.

```text
D(n) = 0.5 × Σ |n_z/N - V_z/ΣV|
R(n) = Σ C_z × C(n_z, 2)
J(n;λ) = D(n) + λR(n)
```

`concentration_shift_added_minus_removed < 0`이면 작업자가 상대적으로 **고집중 Zone에서 저집중 Zone으로 이동**했다는 뜻이다.

## 6. DES 메커니즘 분류

변경 날짜는 Flow Time과 실제 DES 혼잡의 방향으로 다음처럼 분류한다.

```text
win_win                         : Flow 개선 + 혼잡 개선
congestion_gain_efficiency_cost : Flow 악화 + 혼잡 개선
efficiency_gain_congestion_cost : Flow 개선 + 혼잡 악화
loss_loss                       : Flow 악화 + 혼잡 악화
neutral                         : 실질 변화 없음
```

여기서 DES 혼잡 방향은 Conflicts / Wait / Congestion Ratio의 Entropy-Volume 차이를 전체 Holdout Volume 평균으로 정규화한 뒤 평균한 진단용 index로 판단한다.

## 7. 목적함수 proxy 검증

`R(n)`은 실제 DES 혼잡 자체가 아니라 **동일 macro-zone 작업자 쌍 × micro-zone concentration**으로 만든 위험 proxy이다. Phase 6는 배치가 실제로 달라진 날짜에서 다음 exploratory correlation을 저장한다.

```text
R 감소량 ↔ Conflict 감소량
R 감소량 ↔ Wait 감소량
R 감소량 ↔ Congestion Ratio 감소량
D 증가량 ↔ Flow Time 증가량
```

Pearson / Spearman을 모두 저장하지만, 변경 날짜 수가 작고 subset 자체가 사후적으로 선택되므로 **확증적 유의성 검정으로 사용하지 않는다.** 논문에서는 메커니즘 일치 여부를 설명하는 보조 근거로 사용한다.

## 8. 출력 파일

```text
results/phase6/
  phase6_calibration_holdout_generalization.csv
  phase6_pareto_metric_statistics.csv
  phase6_changed_dates.csv
  phase6_changed_date_zone_details.csv
  phase6_proxy_validation.csv
  phase6_metadata.json
```

### phase6_calibration_holdout_generalization.csv

Calibration과 Holdout의 Flow–혼잡 trade-off가 같은 방향으로 유지되는지 비교한다.

### phase6_pareto_metric_statistics.csv

4개 Pareto KPI의 Wilcoxon, Holm 보정, bootstrap CI를 저장한다.

### phase6_changed_dates.csv

Entropy와 Volume 배치가 다른 날짜의 objective 및 DES 변화 요약이다.

### phase6_changed_date_zone_details.csv

변경 날짜 × Zone 수준의 workload / concentration / worker 이동 / pair-risk contribution이다. 논문 case study 표나 그림을 만들 때 사용한다.

### phase6_proxy_validation.csv

Phase 4의 `D`, `R` proxy와 실제 DES 성과 변화의 exploratory correlation이다.

## 9. 논문 해석 원칙

Phase 6에서 가장 중요한 점은 Entropy 방법이 Volume을 모든 KPI에서 반드시 지배해야 한다고 가정하지 않는 것이다.

Pareto-knee 방법의 주장은 다음처럼 표현하는 것이 적절하다.

```text
Entropy(λ*)는 workload 적합도만 최적화하는 Volume 배치와 달리,
미시 수요 집중도가 높은 Zone에 작업자가 동시에 몰리는 위험을 목적함수에 포함한다.
그 결과 일부 날짜에서 작업자 한 명 이상의 정수 배치가 실제로 변경되며,
처리효율 손실과 혼잡 감소 사이의 운영 trade-off가 발생한다.
```

따라서 Holdout에서 Flow Time이 악화되고 혼잡이 감소한다면 그것은 실패를 숨길 대상이 아니라 **Pareto trade-off의 out-of-sample 재현 여부**로 보고해야 한다. 반대로 혼잡 감소가 통계적으로 강하지 않다면 “Volume보다 우월하다”보다 “혼잡을 고려한 대안적 배치 정책이며 추가 검증이 필요하다”는 수준으로 결론을 제한한다.
