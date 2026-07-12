# Preferred Stack & Package Choices

**What this is:** Chris's handpicked package choices, each with *why* it was
picked and *when* it applies. These were chosen deliberately as best-in-class.

**How an AI should use this in a NEW project:**
1. Read this list against the project you're actually in.
2. Adopt the **Universal defaults** almost always.
3. For the domain sections, **judge whether the domain applies** (does this
   project have a web UI? a Python backend? heavy dataframes? trading/market
   data? browser automation?). Adopt the picks for domains that apply; skip the
   rest.
4. If the project already has an equivalent library in place, keep it (handpicked
   = intentional). Only propose a swap when something is clearly, materially
   better — and flag it, don't swap silently.
5. When you introduce one of these, follow its idioms and note it in the
   project's `AI-Development-Guide.md`.

Tiers: **[T1]** = default/primary, **[T2]** = strong secondary, **[T3]** =
niche/occasional. The `↳` items are ranked fallbacks under a primary.

---

## Universal defaults (use in almost any project)

| Pick | Role | Why picked |
|------|------|-----------|
| **uv** [T1] | Python pkg/proj manager | Ultra-fast, unified. Never bare pip/poetry/venv. Use `uv add/sync/run/uvx`. |
| **pnpm** [T1] | Node pkg manager | Fast, disk-efficient, strict. Never npm/yarn. |
| **ruff** [T1] | Lint + format (Python) | One tool replaces black/isort/flake8/pyupgrade. By uv's makers (Astral). |
| **ty** [T2] | Type checker (Python) | Astral's fast checker (preview); prefer where adopted, else mypy/pyright. |
| **Pydantic v2** [T1] | Validation/serialization | Strict typing at the boundary; pairs with FastAPI + typed frontends. |
| **whenever** [T1] | Date/time | Timezone-safe, DST-correct, type-safe. Replaces stdlib `datetime`. |
| **dotenv** [T1] | Config | Simple env-var management for local/bootstrap settings. |
| **Tenacity** [T1] | Retry logic | Declarative retry + backoff for flaky network/API calls. |
| **icecream** [T1] | Debugging | `ic(x)` prints value + expression + context. Far better than `print`. |
| **tabulate** [T1] | CLI output | Clean ASCII tables for debug/report output. |
| **Playwright** [T1] | Browser automation / E2E | Robust, modern, cross-browser. See Testing & Debugging below. |

## Python backend (if the project has one)

| Pick | Role | Why picked |
|------|------|-----------|
| **FastAPI** [T1] | API framework | High-performance async, Pydantic-native, great DX. |
| **psycopg3** [T1] | Postgres adapter | Async-native. **PostgreSQL only — no SQLite.** |
| **aio-pika** [T2] | Messaging | Async RabbitMQ wrapper for signals/notifications. |

## Frontend & visualization (if the project has a web UI)

| Pick | Role | Why picked |
|------|------|-----------|
| **TanStack Start** [T1] | Framework/routing | Vite-native speed, type-safe routing + loaders, monorepo-friendly, no black-box caching. ↳ Next.js for SEO/marketing; ↳ Vite SPA for internal-only dashboards. |
| **TanStack Query** [T1] | Server state | Caching/sync of backend data; the default for anything fetching an API. ↳ Zustand for lightweight global client/UI state. |
| **shadcn/ui** [T1] | Components | Own-the-code components; consistent, themeable. |
| **Perspective** [T1] | Streaming tables | WASM-based blotter/grid for live-updating data. ↳ TanStack Table for static/headless tables. |
| **Lightweight Charts** [T1] | Charts | Canvas price/PnL curves. ↳ Plotly Resampler for 100k+ points; ↳ visx for custom heatmaps/surfaces. |

## Data engineering (if the project is data-heavy)

| Pick | Role | Why picked |
|------|------|-----------|
| **Polars** [T1] | DataFrames | Rust-based, fast, lazy. ↳ Narwhals for DF-agnostic code. |
| **ArcticDB** [T1] | Time-series store | Serverless DataFrame DB for tick/time-series. |
| **Streamable** [T2] | Concurrency | Lazy chained concurrent ops. ↳ Joblib for caching + process parallelism. |
| **openpyxl** [T3] | Excel I/O | Reporting/exports to `.xlsx`. |

## Orchestration, queues & durability (if you have background/multi-step work)

| Pick | Role | Why picked |
|------|------|-----------|
| **Temporal** [T1] | Durable workflows | Primary for durable, multi-step execution (e.g. orders). ↳ Prefect for data flows; ↳ Kew (Redis async queue for FastAPI); ↳ Celery (legacy heavy). |
| **DBOS (Transact)** [T1] | Durable state in Postgres | Library-level durable state. ↳ PGQueuer for minimalist PG job queueing. |

## Trading & market data (domain-specific — adopt only for trading apps)

| Pick | Role | Why picked |
|------|------|-----------|
| **Nautilus Trader** [T1] | Backtest/live engine | Rust/Python execution + backtesting. |
| **schwabdev** [T1] | Broker API | Schwab execution/data wrapper. |
| **ORATS** [T1] | Options data | Historical + live Greeks. |

## Automation & scraping (if you scrape or drive browsers)

| Pick | Role | Why picked |
|------|------|-----------|
| **Playwright** [T1] | Browser automation | Primary. ↳ nodriver (undetectable, no chromedriver); ↳ pyppeteer (async headless); ↳ PyAutoGUI (OS-level input). |
| **Crawlee-Python** [T2] | Scraping framework | Full crawler. ↳ cloudscraper (Cloudflare bypass); ↳ UIVision (extension-based). |

---

## Testing & debugging setup

### Tests: unittest structure, pytest runner
This repo deliberately combines both — **write tests as `unittest` classes, run
them with `pytest`.**

- **Structure with `unittest`**: `unittest.TestCase` (sync) or
  `unittest.IsolatedAsyncioTestCase` (async), with `setUp`/`asyncSetUp` and
  `self.assert*`. Portable, dependency-light, self-describing.
- **Mock with `unittest.mock`**: `MagicMock`, `AsyncMock`, `patch`. Shared
  fixtures live in `tests/conftest.py`.
- **Run with `pytest`**: better output, discovery, and fixtures. Plain
  pytest-style module functions (`def test_x():`) are fine too and coexist with
  the unittest classes.
- **Async**: `pytest-asyncio` with `asyncio_mode = auto` in `pytest.ini`, so
  async tests need no `@pytest.mark.asyncio` decorator.
- **Layout**: tests mirror the source tree under `tests/` (e.g.
  `apps/api/services/x.py` -> `tests/api/services/test_x.py`).

Minimal `pytest.ini`:
```ini
[pytest]
testpaths = tests
asyncio_mode = auto
filterwarnings =
    ignore::DeprecationWarning
    ignore::UserWarning
```

Why both: `unittest` gives structure that isn't locked to a runner; `pytest`
gives the ergonomics (fixtures, output, `-k` filters). You get portable tests
with a great runner.

### Debugging: icecream + tabulate
- `ic(value)` prints the expression, its value, and call context — use it
  instead of `print()` for inspection. `ic()` with no args marks that a line
  executed. Remove or disable (`ic.disable()`) before shipping.
- `tabulate(rows, headers=...)` for readable ASCII tables in CLI/debug output.

### Browser automation: Playwright
- Primary for E2E and any "drive a real browser" task.
- **Save screenshots to `.screenshots/`** (relative to the web root), e.g.
  `.screenshots/<name>.png`.
- Fallbacks by need: nodriver (stealth), pyppeteer (fast async headless),
  PyAutoGUI (OS-level input when the target isn't a browser).
