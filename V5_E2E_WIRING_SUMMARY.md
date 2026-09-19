# V5 E2E Wiring Implementation Summary

## Overview

This implementation wires the RUN STORY callback to actually invoke the generation pipeline while maintaining controlled E2E mode support.

## Files Created

### 1. `src/newsagent_v2/v5_generation/persistent_store.py`
**Purpose:** Persistent storage for V5 state using JSON files

**Key Classes:**
- `DiscoveryRun`: Stores discovery run results (run_id, timestamp, event_ids[])
- `GenerationJob`: Stores generation job state with full lifecycle tracking
- `SessionState`: Tracks followed/ignored/selected IDs per discovery run
- `PersistentStore`: Thread-safe JSON persistence with atomic writes

**Storage Location:** `./data/v5_state/`

### 2. `src/newsagent_v2/v5_generation/provider_preflight.py`
**Purpose:** Credential readiness inspection

**Key Features:**
- Checks `GROQ_API_KEY`, `VERTEX_PROJECT`, `VERTEX_LOCATION`, `GOOGLE_APPLICATION_CREDENTIALS`
- Reports `SET`/`MISSING` without exposing secrets
- Returns readiness dict for Telegram display
- Masks credential values (shows prefix...suffix)

**Usage:**
```python
preflight = ProviderPreflight(os.environ)
report = preflight.run_all_checks()
can_write = preflight.is_ready_for_writing()
```

### 3. `src/newsagent_v2/v5_generation/cost_ledger.py`
**Purpose:** Track actual costs per provider call

**Key Features:**
- Tracks provider, model, tokens, cost for each call
- Supports `UNKNOWN` cost status when cannot be determined
- Thread-safe with persistence
- Exports final cost report as JSON

**Output:** Cost report with total known/estimated costs, breakdown by provider

### 4. `src/newsagent_v2/v5_generation/generation_worker.py`
**Purpose:** Async generation with progress updates

**Key Features:**
- Threading-based async execution (non-blocking)
- Progress callback to Telegram (edits progress message)
- State machine: `NOT_REQUESTED` → `RESERVED` → `REQUESTING` → `RESEARCHING` → `WRITING` → `QA` → `IMAGE_GEN` → `SUCCEEDED`/`FAILED`
- `MAX_ACTIVE_PAID_STORIES = 1` enforcement
- Controlled E2E mode blocks automatic generation

**Events:**
- `request_generation()` returns immediately with job info
- Background thread runs actual generation via RunStoryAdapter
- Progress emits to callback for Telegram display

## Files Modified

### 1. `src/newsagent_v2/telegram/v5_callbacks.py`
**Changes:**
- Added `generation_worker`, `preflight` parameters to `V5CallbackHandler`
- Updated `handle_run_story()` to:
  1. Check controlled E2E mode
  2. Validate provider readiness via preflight
  3. Persist job state to disk via generation_worker
  4. Invoke async generation (if providers ready and not E2E mode)
  5. Return progress message immediately

**Factory Updated:**
```python
def make_v5_callback_handler(
    event_store,
    telegram_store,
    generation_worker=None,  # NEW
    preflight=None,          # NEW
):
```

### 2. `start_v5_bot.py`
**Changes:**
- Added imports for new generation components
- Extended `V5BotRuntime` to include:
  - `preflight: ProviderPreflight`
  - `generation_worker: GenerationWorker`
  - `persistent_store: PersistentStore`
  - `adapter: RunStoryAdapter`
- Added `_progress_callback()` for generation updates
- Modified `build_runtime()` to:
  1. Initialize persistent store
  2. Run provider preflight check
  3. Create RunStoryAdapter
  4. Create GenerationWorker (with None adapter in E2E mode)
- Extended `execute_callback()` to pass runtime for generation access
- Added generation status logging at startup

## Controlled E2E Mode

**Environment Variable:** `NEWSAGENT_V5_CONTROLLED_E2E=true`

**Behavior:**
- RUN STORY still marks events as selected
- Generation job is created with RESERVED state
- Actual generation does NOT start (adapter=None)
- Message indicates "Waiting for manual generation start (E2E mode)"
- Telegram bot shows E2E status on startup

## State Persistence

**JSON Files Structure:**
```
data/v5_state/
├── discovery_run_20240919T123456.json
├── job_evt-001_20240919T123501.json
└── session_run_20240919T123456.json
```

**Generation Job States:**
- `NOT_REQUESTED`: Initial state
- `RESERVED`: Slot reserved
- `REQUESTING`: Starting generation
- `RESEARCHING`: Research phase active
- `WRITING`: Article writing
- `QA`: QA review
- `IMAGE_GEN`: Image generation
- `SUCCEEDED`: Complete
- `FAILED`: Error occurred

## Testing

**Test File:** `tests/test_v5_generation_wiring.py`

**Coverage:**
- Persistent store CRUD operations
- Discovery run persistence
- Generation job lifecycle
- Session state management
- Provider preflight checks (all credentials)
- Cost ledger tracking
- Generation worker state machine
- Callback wiring (RUN STORY, FOLLOW, IGNORE, SEE NEXT)
- Controlled E2E mode blocking
- Content masking security

**Run Tests:**
```bash
python -m pytest tests/test_v5_generation_wiring.py -v
```

## Integration

**Usage Flow:**

1. User clicks RUN STORY button
2. Telegram sends callback with `run:event_id`
3. `V5CallbackHandler.handle_run_story()` called
4. Provider preflight checks credentials
5. If OK: `generation_worker.request_generation(event)` called
6. `GenerationWorker` creates job with RESERVED state
7. Background thread started for actual generation
8. RUN STORY callback returns immediately with status
9. Generation runs async via RunStoryAdapter
10. Progress updates sent via callback to Telegram
11. Final result persisted to disk

## Safety Features

1. **Credential Masking:** Secrets never exposed in logs/UI
2. **Atomic Writes:** JSON files written to temp then renamed
3. **Idempotent RUN STORY:** Duplicate requests return existing job
4. **Capacity Enforcement:** Max 1 active paid story
5. **E2E Mode:** Complete safety shutoff for testing
6. **Thread-Safe:** All state operations use locks
