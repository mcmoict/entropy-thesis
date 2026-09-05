# Run from the entropy-thesis project root.
# Creates a compact ZIP for Phase 8 review without .git, raw data, dist, or visualization JSON files.
$items = @(
    "src",
    "tests",
    "docs",
    "results/phase4",
    "results/phase5",
    "results/phase6",
    "results/phase8",
    "README.md",
    "pyproject.toml",
    "requirements.txt",
    "requirements-ai.txt",
    "requirements-lock.txt",
    "requirements-pip.txt",
    "environment.yml",
    "environment-full.yml"
) | Where-Object { Test-Path $_ }

$stamp = Get-Date -Format "yyyyMMdd_HHmm"
$out = "entropy-thesis_$stamp.zip"
tar -a -c -f $out $items
Write-Host "Created: $out"
