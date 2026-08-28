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

---

## 19. Macro-zone 표시 영역 축소

HTML Viewer와 Desktop Viewer의 Z01 ~ Z04 Macro-zone은 실제 인력배치 구역의 논리적 정의는 그대로 유지하면서, 화면에서 보이는 외곽 사각형만 조금 안쪽으로 축소하여 표시합니다.

현재 공통 축소 기준:

```python
MACRO_ZONE_HORIZONTAL_INSET_RATIO = 0.23
MACRO_ZONE_VERTICAL_INSET_RATIO = 0.06
```

적용 방식:

```text
좌측 외곽 경계   → 오른쪽으로 축소
우측 외곽 경계   → 왼쪽으로 축소
위쪽 외곽 경계   → 아래쪽으로 축소
아래쪽 외곽 경계 → 위쪽으로 축소
```

다음 중앙 경계는 변경하지 않습니다.

```text
CC-08 기준 중앙 세로 분할선
Near / Far 중앙 가로 분할선
```

즉, Z01 ~ Z04의 연구상 구역 구분 자체를 변경하는 것이 아니라 **시각화용 외곽 표시 범위만 축소**합니다.

HTML과 Desktop Viewer 모두 동일한 비율을 사용하는 것을 권장합니다.

### HTML Viewer의 Macro-zone만 다시 반영

기존 월별 JSON이 이미 생성되어 있다면 DES 시뮬레이션을 다시 실행할 필요가 없습니다.

```powershell
python -m entropy_thesis.visualization.picking_animation_actual --html-only --serve
```

이 명령은 기존 월별 JSON을 유지하면서 `picking_animation_actual.html`만 다시 생성합니다.

---

## 20. PyInstaller로 Windows EXE 생성

Desktop Viewer는 PyInstaller를 이용하여 Windows 실행 파일로 패키징할 수 있습니다.

최종 실행 파일 예시:

```text
PickingSimulation.exe
```

EXE로 빌드하면 Python 모듈 실행 명령을 직접 입력하지 않고 일반 Windows 프로그램처럼 실행할 수 있습니다.

Windows용 EXE는 **Windows 환경에서 PyInstaller를 실행하여 생성**해야 합니다.

---

## 21. PyInstaller 관련 권장 파일 구성

`visualization` 디렉터리에 다음 파일을 함께 두는 것을 권장합니다.

```text
src/
└─ entropy_thesis/
   └─ visualization/
      ├─ picking_animation_actual.py
      ├─ picking_animation_desktop.py
      ├─ PickingSimulation.spec
      ├─ build_PickingSimulation.bat
      ├─ PYINSTALLER_README.md
      └─ README.md
```

각 파일의 역할:

```text
picking_animation_actual.py
    HTML Actual Animation 및 월별 JSON 생성

picking_animation_desktop.py
    PySide6 기반 Desktop Viewer

PickingSimulation.spec
    PyInstaller 빌드 설정

build_PickingSimulation.bat
    Windows에서 PickingSimulation.exe를 자동 빌드하는 배치 파일

PYINSTALLER_README.md
    PyInstaller 전용 상세 빌드 설명

README.md
    Visualization 전체 사용 방법
```

---

## 22. EXE 빌드 전 준비

Conda 환경을 활성화합니다.

```powershell
conda activate thesis-env
```

필요 패키지를 설치합니다.

```powershell
python -m pip install PySide6 pyinstaller orjson
```

`orjson`은 필수는 아니지만 큰 월별 JSON 파일을 빠르게 읽는 데 도움이 되므로 권장합니다.

설치 확인:

```powershell
python -m pip show PySide6
python -m pip show pyinstaller
python -m pip show orjson
```

---

## 23. PickingSimulation.exe 자동 빌드

프로젝트 루트에서 다음 배치 파일을 실행합니다.

```powershell
.\src\entropy_thesis\visualization\build_PickingSimulation.bat
```

또는 Windows Explorer에서 `build_PickingSimulation.bat`를 더블클릭할 수 있습니다.

빌드가 정상 완료되면 일반적으로 다음 위치에 실행 파일이 생성됩니다.

```text
entropy-thesis/
├─ data/
├─ results/
├─ src/
└─ dist/
   └─ PickingSimulation.exe
```

PyInstaller의 중간 빌드 파일은 일반적으로 다음 경로에 생성됩니다.

```text
build/
dist/
```

---

## 24. PickingSimulation.exe 실행

빌드 후 프로젝트 루트에서 실행:

```powershell
.\dist\PickingSimulation.exe
```

또는 Windows Explorer에서 다음 파일을 더블클릭합니다.

```text
dist\PickingSimulation.exe
```

Desktop Viewer이므로 다음 항목은 필요하지 않습니다.

```text
Chrome / Edge
localhost 서버
--serve
python -m http.server
```

---

## 25. EXE에서 사용하는 데이터

월별 JSON은 EXE 내부에 포함하지 않는 구성을 권장합니다.

이유:

- 월별 JSON의 크기가 큼
- 실험 결과를 다시 생성할 수 있음
- EXE와 데이터 파일을 분리하면 유지보수가 쉬움
- HTML Viewer와 Desktop Viewer가 동일한 JSON을 공유할 수 있음

권장 프로젝트 구조:

```text
entropy-thesis/
├─ data/
│  ├─ raw/
│  │  └─ Support_Points_Navigation.csv
│  └─ raw_original/
│     └─ Layout_Z1.0.svg
├─ results/
│  └─ figures/
│     ├─ picking_animation_actual.html
│     └─ picking_animation_actual_data/
│        ├─ 2023-01.json
│        ├─ 2023-02.json
│        ├─ ...
│        └─ 2023-10.json
└─ dist/
   └─ PickingSimulation.exe
```

EXE는 기본적으로 프로젝트의 기존 `data` 및 `results` 파일을 사용합니다.

---

## 26. EXE를 다른 위치에서 실행하는 경우

`PickingSimulation.exe`를 프로젝트의 `dist` 디렉터리가 아닌 다른 위치로 복사해 실행하는 경우, 프로젝트 루트를 명시할 수 있습니다.

```powershell
PickingSimulation.exe --project-root "C:\workspace\entropy-thesis"
```

이 옵션을 사용하면 EXE가 다음 파일을 해당 프로젝트 루트를 기준으로 찾을 수 있습니다.

```text
results/figures/picking_animation_actual_data/
results/figures/picking_animation_actual.html
data/raw_original/Layout_Z1.0.svg
data/raw/Support_Points_Navigation.csv
```

---

## 27. EXE에서 특정 날짜 및 방법으로 시작

특정 날짜:

```powershell
PickingSimulation.exe --date 2023-08-30
```

Entropy:

```powershell
PickingSimulation.exe --method entropy
```

특정 날짜의 Entropy 시나리오:

```powershell
PickingSimulation.exe --date 2023-08-30 --method entropy
```

프로젝트 루트까지 함께 지정:

```powershell
PickingSimulation.exe ^
  --project-root "C:\workspace\entropy-thesis" ^
  --date 2023-08-30 ^
  --method entropy
```

---

## 28. Python 실행과 EXE 실행 비교

Python 모듈 실행:

```text
Conda / Python 환경 필요
PySide6 설치 필요
python -m ... 명령 사용
개발 및 수정에 적합
```

PyInstaller EXE 실행:

```text
PickingSimulation.exe 직접 실행
Python 명령 입력 불필요
논문 발표 및 시연에 적합
사용자 입장에서 일반 Windows 프로그램처럼 실행
```

따라서 개발 중에는 Python 모듈 방식으로 실행하고, 최종 시연이나 배포 시에는 `PickingSimulation.exe`를 사용하는 것을 권장합니다.

---

## 29. 최종 권장 운영 구조

```text
                    ┌─ HTML Viewer
DES / Monthly JSON ─┼─ Desktop Viewer
                    └─ PickingSimulation.exe
```

구체적으로:

```text
picking_animation_actual.py
    ↓
월별 JSON 생성
    ↓
┌───────────────────────────────────────┐
│ picking_animation_actual.html         │  웹 Viewer
│ picking_animation_desktop.py          │  Python Desktop Viewer
│ PickingSimulation.exe                 │  Windows 실행 파일
└───────────────────────────────────────┘
```

세 Viewer가 동일한 DES 결과와 월별 JSON 데이터를 공유하므로 시각화 방식이 달라져도 연구 결과 자체는 동일하게 유지됩니다.

---

## 30. 최종 권장 사용 순서

### 연구 데이터 또는 시뮬레이션 결과를 새로 생성한 경우

```powershell
conda activate thesis-env
python -m entropy_thesis.visualization.picking_animation_actual --all-dates
```

### HTML Viewer의 표시만 수정한 경우

```powershell
python -m entropy_thesis.visualization.picking_animation_actual --html-only --serve
```

### Desktop Viewer 개발 및 테스트

```powershell
python -m entropy_thesis.visualization.picking_animation_desktop
```

### 특정 날짜 Entropy 테스트

```powershell
python -m entropy_thesis.visualization.picking_animation_desktop --date 2023-08-30 --method entropy
```

### 최종 Windows EXE 빌드

```powershell
.\src\entropy_thesis\visualization\build_PickingSimulation.bat
```

### 최종 EXE 실행

```powershell
.\dist\PickingSimulation.exe
```

---

## 31. 최종 정리

Visualization 모듈의 역할은 DES 실험 결과를 변경하는 것이 아니라 동일한 결과를 여러 방식으로 확인하고 시연하기 위한 것입니다.

```text
HTML Viewer
    웹 기반 확인 및 공유

Desktop Viewer
    Qt 기반 고성능 애니메이션 및 개발

PickingSimulation.exe
    최종 Windows 시연 및 실행
```

특히 논문 발표나 결과 시연 시에는 `PickingSimulation.exe`를 사용하면 브라우저 주소창이나 localhost 서버 없이 독립적인 프로그램 형태로 실행할 수 있어 보다 완성된 시각화 결과를 보여줄 수 있습니다.
