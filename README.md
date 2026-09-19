# NewsAgent V2

Day-1 goal: a clean, isolated, zero-paid-model-first news pipeline.

## Principles
- Aadi stays untouched.
- Deterministic code first; AI only where it improves quality.
- Test/dry-run publishing only until benchmarked.
- Every model call and pipeline stage is measurable.

## Quick start (Windows PowerShell)

```powershell
cd C:\
mkdir NewsAgent-V2
# unzip this starter into C:\NewsAgent-V2

cd C:\NewsAgent-V2
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env

.\.venv\Scripts\python.exe -m newsagent_v2.main --dry-run
```

The first run collects RSS items, normalizes them, removes obvious duplicates,
scores/ranks them deterministically, and writes a JSON report to `output/`.

No paid model is required for the first milestone.
