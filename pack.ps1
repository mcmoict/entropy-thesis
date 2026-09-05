# ============================================================
# entropy-thesis 업로드용 ZIP 생성
# - ZIP 생성 위치 : 현재 프로젝트의 한 단계 위
# - 파일명        : entropy-thesis_YYYYMMDD_HH24MI.zip
# - ZIP 내부      : entropy-thesis\... 구조 유지
# Set-ExecutionPolicy -Scope Process Bypass   # 필요할 때만(오류 발생 등..)
# 한글 깨지는 경우: Save with Encoding → UTF-8 with BOM
# 실행하는 방법: (thesis-env) PS D:\workspace\entropy-thesis> ./pack.ps1
# ============================================================

# ============================================================
# UTF-8 Console Encoding
# ============================================================
chcp 65001 > $null
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

# ------------------------------------------------------------
# 1. 포함할 디렉터리
#    프로젝트 루트 기준 상대경로
# ------------------------------------------------------------
$includeDirs = @(
    "data",
    "dist",
    "docs",
    "notebooks",
    "results/phase2",
    "results/phase3",
    "results/phase4",
    "results/phase5",
    "results/phase6",
    "results/phase8",
    "src",
    "tests"
)


# ------------------------------------------------------------
# 2. 제외할 디렉터리
#    프로젝트 루트 기준 상대경로
# ------------------------------------------------------------
$excludeDirs = @(
    "configs",
    "generated_files",
    "data/raw_analysis",
    "results/figures"

    # 필요하면 아래처럼 추가
    # "src/entropy_thesis/__pycache__",
    # "src/entropy_thesis/allocation/__pycache__",
    # "src/entropy_thesis/simulation/__pycache__",
    # "src/entropy_thesis/visualization/__pycache__",
    # "tests/__pycache__",
    # "src/entropy_thesis.egg-info"
)


# ------------------------------------------------------------
# 3. 제외할 파일명
#    * 와일드카드 사용 가능
#    * 모든 하위 디렉터리에 적용
# ------------------------------------------------------------
$excludeFiles = @(
    "*.zip",
    "*.exe"

    # 필요하면 아래처럼 추가
    # "*.pyc",
    # "*.pyo",
    # "*.log",
    # "*.tmp",
    # ".DS_Store",
    # "Thumbs.db"
)


# ------------------------------------------------------------
# 4. 프로젝트 경로 확인
# ------------------------------------------------------------
$projectDir  = (Resolve-Path ".").Path
$projectName = Split-Path $projectDir -Leaf
$parentDir   = Split-Path $projectDir -Parent

if ($projectName -ne "entropy-thesis") {
    throw "entropy-thesis 프로젝트 루트에서 실행해주세요."
}


# ------------------------------------------------------------
# 5. ZIP 파일명 생성
# ------------------------------------------------------------
$stamp = Get-Date -Format "yyyyMMdd_HHmm"
$out   = Join-Path $parentDir "entropy-thesis_$stamp.zip"


# ------------------------------------------------------------
# 6. 프로젝트 루트 파일 수집
# ------------------------------------------------------------
$rootFiles = Get-ChildItem $projectDir -File |
    Where-Object {

        $fileName = $_.Name
        $excluded = $false

        foreach ($pattern in $excludeFiles) {
            if ($fileName -like $pattern) {
                $excluded = $true
                break
            }
        }

        -not $excluded
    } |
    ForEach-Object {
        "entropy-thesis/$($_.Name)"
    }


# ------------------------------------------------------------
# 7. 포함할 디렉터리 수집
# ------------------------------------------------------------
$dirItems = $includeDirs |
    Where-Object {

        $currentDir = $_ -replace '\\', '/'
        $excluded   = $false

        foreach ($excludeDir in $excludeDirs) {

            $excludeNormalized = $excludeDir -replace '\\', '/'

            if (
                $currentDir -eq $excludeNormalized -or
                $currentDir.StartsWith("$excludeNormalized/")
            ) {
                $excluded = $true
                break
            }
        }

        (-not $excluded) -and
        (Test-Path (Join-Path $projectDir $_))
    } |
    ForEach-Object {
        "entropy-thesis/$($_ -replace '\\','/')"
    }


# ------------------------------------------------------------
# 8. tar 옵션 생성
# ------------------------------------------------------------
$tarArgs = @(
    "-a",
    "-c",
    "-f",
    $out
)


# 제외 디렉터리 적용
foreach ($excludeDir in $excludeDirs) {

    $excludeNormalized = $excludeDir -replace '\\', '/'

    $tarArgs += "--exclude=entropy-thesis/$excludeNormalized"
    $tarArgs += "--exclude=entropy-thesis/$excludeNormalized/*"
}


# 제외 파일 적용
foreach ($pattern in $excludeFiles) {

    $tarArgs += "--exclude=$pattern"
    $tarArgs += "--exclude=*/$pattern"
}


# ------------------------------------------------------------
# 9. 최종 압축 대상
# ------------------------------------------------------------
$items = @($rootFiles) + @($dirItems)


# ------------------------------------------------------------
# 10. ZIP 생성
# ------------------------------------------------------------
Push-Location $parentDir

try {

    & tar @tarArgs @items

    if ($LASTEXITCODE -ne 0) {
        throw "ZIP 생성 중 오류가 발생했습니다."
    }

    Write-Host ""
    Write-Host "============================================================"
    Write-Host " ZIP 생성 완료"
    Write-Host "============================================================"
    Write-Host ""
    Write-Host "파일 : $out"
    Write-Host ""
    Write-Host "포함 디렉터리:"
    $includeDirs | ForEach-Object {
        Write-Host "  + $_"
    }

    Write-Host ""
    Write-Host "제외 디렉터리:"
    $excludeDirs | ForEach-Object {
        Write-Host "  - $_"
    }

    Write-Host ""
    Write-Host "제외 파일:"
    $excludeFiles | ForEach-Object {
        Write-Host "  - $_"
    }

    Write-Host ""
}
finally {
    Pop-Location
}