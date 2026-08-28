# Picking Animation Desktop Viewer

`picking_animation_desktop.py`는 기존 HTML 기반 Picking Animation의 월별 JSON 데이터를 그대로 사용하여, 브라우저 없이 Windows 데스크톱 프로그램 형태로 재생하는 PySide6 기반 시각화 프로그램입니다.

기존 시뮬레이션 결과를 다시 계산하지 않고 `picking_animation_actual_data/YYYY-MM.json` 파일을 직접 읽기 때문에, HTML 버전과 동일한 DES 결과를 유지하면서 렌더링 부분만 Qt 데스크톱 방식으로 변경합니다.

---

## 1. 주요 특징

- 브라우저 및 localhost 서버 없이 실행
- PySide6 / Qt 기반 데스크톱 GUI
- 약 60 FPS 기준 작업자 위치 애니메이션
- 시간 기반 좌표 보간으로 작업자 이동을 부드럽게 표현
- Observed / Equal / Random / Volume / Entropy 방법 지원
- 날짜 선택 및 날짜별 작업자 수 표시
- 전체 작업자 / 개별 작업자 선택
- 0.5x / 1x / 2x / 3x / 5x / 10x / 20x / 50x 재생 속도
- 시간 Slider 지원
- 다음 날짜 자동실행 지원
- Picking 대상 위치 표시
- Z01 ~ Z04 Macro-zone 표시
- 실제 DES Resource Contention 충돌 이벤트 표시
- 충돌 시 해당 작업자를 빨간색으로 표시
- 현재 충돌 / 누적 충돌 이벤트 및 작업자 수 표시
- 시뮬레이션 시간과 실제 날짜/시간 표시

---

## 2. 권장 파일 위치

`picking_animation_desktop.py`는 기존 시각화 모듈과 같은 `visualization` 디렉터리에 두는 것을 권장합니다.

예시:

```text
src/
└─ entropy_thesis/
   └─ visualization/
      ├─ picking_animation_actual.py
      ├─ picking_animation_desktop.py
      └─ README.md
```

---

## 3. 필요 패키지 설치

현재 사용하는 Conda 환경을 활성화한 뒤 PySide6를 설치합니다.

```powershell
conda activate thesis-env
python -m pip install PySide6
```

설치 확인:

```powershell
python -c "import PySide6; print(PySide6.__version__)"
```

---

## 4. 기본 실행

프로젝트 루트 디렉터리에서 실행합니다.

```powershell
python -m entropy_thesis.visualization.picking_animation_desktop
```

정상 실행되면 Chrome이나 Edge가 열리지 않고 Windows 데스크톱 프로그램 창이 바로 실행됩니다.

`--serve` 옵션이나 `python -m http.server`는 필요하지 않습니다.

---

## 5. 특정 날짜로 실행

예를 들어 2023-08-30부터 확인하려면 다음과 같이 실행합니다.

```powershell
python -m entropy_thesis.visualization.picking_animation_desktop --date 2023-08-30
```

---

## 6. 특정 배치 방법으로 실행

Entropy 방법으로 시작하려면:

```powershell
python -m entropy_thesis.visualization.picking_animation_desktop --method entropy
```

특정 날짜와 방법을 함께 지정하려면:

```powershell
python -m entropy_thesis.visualization.picking_animation_desktop --date 2023-08-30 --method entropy
```

사용 가능한 방법:

```text
observed
equal
random
volume
entropy
```

---

## 7. 기본 데이터 경로

Desktop Viewer는 기존 HTML 시각화에서 생성한 월별 JSON 파일을 그대로 사용합니다.

기본적으로 다음 구조를 사용합니다.

```text
results/
└─ figures/
   ├─ picking_animation_actual.html
   └─ picking_animation_actual_data/
      ├─ 2023-01.json
      ├─ 2023-02.json
      ├─ ...
      └─ 2023-10.json
```

창고 Layout과 Support Point 데이터는 다음 경로를 기준으로 사용합니다.

```text
data/raw_original/Layout_Z1.0.svg
data/raw/Support_Points_Navigation.csv
```

따라서 기존 HTML 시각화용 JSON 생성이 완료되어 있다면 DES 시뮬레이션을 다시 실행할 필요가 없습니다.

---

## 8. 데이터 처리 구조

기존 HTML 버전:

```text
DES Simulation
    ↓
Monthly JSON
    ↓
HTML / JavaScript
    ↓
Browser Rendering
```

Desktop 버전:

```text
기존 Monthly JSON
    ↓
Scenario Runtime
    ↓
QTimer + QElapsedTimer
    ↓
시간 기반 좌표 보간
    ↓
Qt QPainter
    ↓
Desktop Window
```

즉, 시뮬레이션 결과는 그대로 유지하고 화면 렌더링 계층만 변경합니다.

---

## 9. 렌더링 최적화 구조

Desktop Viewer는 모든 요소를 매 프레임 다시 그리지 않습니다.

다음 요소는 정적 화면으로 캐시합니다.

```text
Warehouse SVG Layout
Z01 ~ Z04 Macro-zone
Picking Target Points
```

60 FPS 재생 중에는 주로 다음 요소만 갱신합니다.

```text
Operator 위치
Operator 충돌 상태
현재 시간
충돌 이벤트 상태
```

이를 통해 HTML/SVG DOM 기반 애니메이션보다 작업자 이동을 더 부드럽게 표시하는 것을 목표로 합니다.

---

## 10. DES 충돌 표시

충돌 표시는 거리 기반 근사값을 사용하지 않습니다.

기존 `picking_animation_actual.py`가 월별 JSON에 저장한 실제 DES Resource Contention 이벤트를 그대로 사용합니다.

```text
conflict_events
```

현재 시간이 충돌 이벤트의 `t0 ~ t1` 구간에 포함되면 해당 작업자를 빨간색으로 표시합니다.

따라서 Desktop Viewer의 충돌 표시 기준은 HTML Actual 버전과 동일합니다.

---

## 11. Picking Target 표시

당일 Picking Wave에서 실제 피킹이 발생하는 위치를 표시합니다.

월별 JSON의 다음 데이터를 우선 사용합니다.

```text
pick_targets
```

동일한 물리적 위치에서 여러 번 피킹하는 경우 하나의 Target Point로 묶어서 표시합니다.

---

## 12. Macro-zone

인력 배치 실험에서 사용하는 4개 Macro-zone을 표시합니다.

```text
Z01 = Left / Near
Z02 = Left / Far
Z03 = Right / Near
Z04 = Right / Far
```

기존 시각화와 동일하게 Support Point 좌표를 기준으로 영역을 구성합니다.

---

## 13. HTML 버전과 Desktop 버전의 관계

HTML 버전을 제거할 필요는 없습니다.

두 Viewer를 함께 유지하는 것을 권장합니다.

```text
Simulation Result
      │
      ├─ picking_animation_actual.py
      │      ↓
      │   HTML Viewer
      │
      └─ picking_animation_desktop.py
             ↓
          Desktop Viewer
```

HTML Viewer는 공유 및 웹 브라우저 기반 확인에 적합하고, Desktop Viewer는 논문 발표나 시연 시 보다 부드러운 애니메이션을 보여주는 용도로 사용할 수 있습니다.

---

## 14. JSON이 없는 경우

Desktop Viewer 자체는 DES 시뮬레이션을 다시 생성하지 않습니다.

월별 JSON이 없다면 먼저 기존 Actual Animation 모듈로 데이터를 생성합니다.

예시:

```powershell
python -m entropy_thesis.visualization.picking_animation_actual --all-dates
```

프로젝트의 실제 패키지 경로가 다르면 현재 프로젝트 구조에 맞게 모듈명을 조정합니다.

JSON 생성이 완료된 뒤 Desktop Viewer를 실행합니다.

```powershell
python -m entropy_thesis.visualization.picking_animation_desktop
```

---

## 15. 자주 발생할 수 있는 문제

### PySide6 모듈 오류

오류 예시:

```text
ModuleNotFoundError: No module named 'PySide6'
```

설치:

```powershell
python -m pip install PySide6
```

현재 `thesis-env`의 Python에 설치되었는지 확인합니다.

```powershell
where python
python -m pip show PySide6
```

### 월별 JSON을 찾지 못하는 경우

다음 디렉터리가 존재하는지 확인합니다.

```text
results/figures/picking_animation_actual_data/
```

그리고 내부에 다음과 같은 파일이 있어야 합니다.

```text
2023-01.json
2023-02.json
...
```

### SVG Layout을 찾지 못하는 경우

기본 경로를 확인합니다.

```text
data/raw_original/Layout_Z1.0.svg
```

### Support Point 파일을 찾지 못하는 경우

기본 경로를 확인합니다.

```text
data/raw/Support_Points_Navigation.csv
```

---

## 16. 권장 실행 순서

최초 한 번:

```powershell
conda activate thesis-env
python -m pip install PySide6
```

기존 월별 JSON이 이미 있다면 바로 실행:

```powershell
python -m entropy_thesis.visualization.picking_animation_desktop
```

특정 날짜 Entropy 시나리오 확인:

```powershell
python -m entropy_thesis.visualization.picking_animation_desktop --date 2023-08-30 --method entropy
```

---

## 17. 관련 파일

```text
picking_animation_actual.py
    기존 HTML Actual Animation 및 월별 JSON 생성

picking_animation_desktop.py
    PySide6 기반 Desktop Animation Viewer

README.md
    Desktop Viewer 실행 및 사용 방법
```

---

## 18. 참고

Desktop Viewer의 목적은 기존 DES 분석 결과를 변경하는 것이 아니라, 동일한 결과를 보다 부드럽고 독립적인 데스크톱 UI에서 재생하는 것입니다.

따라서 논문 실험 결과 및 통계 분석에는 기존 시뮬레이션 결과를 그대로 사용하고, Desktop Viewer는 결과 확인 및 시각적 시연 도구로 활용하는 것을 권장합니다.
