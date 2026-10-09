# Offline test results

Date: 2026-10-07

**100 passed in 2.01s** using `python -m pytest -q tests`.

Syntax compilation also passed for all five implementation Python files.

Environment inspected locally:

- Python 3.13.14
- NumPy 2.3.5
- Pandas 2.2.3
- SciPy 1.17.1
- CCXT 4.5.85

The CCXT check only instantiated an empty exchange object and inspected method
availability. It did **not** load markets, authenticate, contact Binance or
submit/cancel orders. All venue/database operations in execution tests are
in-memory mocks. An autouse test fixture rejects socket connections.

## Coverage

- Correct `algoOrder` payload, `triggerPrice`, close-all, and client identity.
- One-way and hedge closing directions for long and short positions.
- No conflicting `quantity` / `reduceOnly` with `closePosition=true`.
- Independent confirmation rather than trusting the creation response.
- Lost creation ACK recovered without a duplicate POST.
- Rejected/mismatched/expired/canceled protective orders not called verified.
- Position read errors are unknown, not zero/flat.
- `contractSize` is never interpreted as a live position quantity.
- Live-quantity flattening, bounded attempts, terminal-close verification.
- Targeted cleanup preserves unrelated orders.
- Completed, sorted, contiguous 30m candles; invalid data rejected.
- Wilder ATR seed, recursion and previous-close gap handling.
- Single trade persistence and actual filled quantity/entry identity.
- `fee=None` does not bypass protection or crash after entry.
- Lost entry ACK queried by the same client ID, not blindly resubmitted.
- SL failure de-risks; TP failure keeps confirmed SL and halts entries.
- Database write failure after entry de-risks rather than losing tracking.
- Startup gate, duplicate symbol/order blocking, prospective risk/margin caps.
- Dry run sends no private order operations.
- Explicit order opt-in required even when configuration says live.
- Startup verifies venue state instead of cached `PROTECTED` flags.
- Restart is idempotent when protection actually exists.
- Caller cancellation/shutdown does not cancel an in-flight protection task.
- Source builder removes known embedded credential defaults, sets weights and
  30m defaults, fixes AQII named arguments, rejects incompatible/repatched source.
- Enabled-factor policy shared by legacy numerator and denominator; disabled
  object flags and configuration flags contribute zero, with liquidity excluded
  from the directional normalization.
- CLI generates a sanitized standalone without executing the source, protects
  the original even with `--force`, and rejects report-path aliases to the input.
- Schema migration is idempotent and refuses a newer database version instead
  of downgrading it.
- First-day loss baseline initialized; timer alone does not erase a loss halt.

## Not covered / not claimed

- No live or Binance Demo acceptance/fill/trigger test.
- No deployment or authenticated exchange-connection test.
- No full runtime test of the user's pasted original script. Builder tests use
  a synthetic structural fixture; core/execution templates are tested separately.
- No evidence of profitability or superiority of 30m or 50/50 weighting.
- No comprehensive audit of every original mathematical model, API/dashboard,
  financial ledger, backtester or dependency combination.

The produced full script needs local syntax review and an independently verified
Demo smoke test before any funded deployment. External API changes, network
failure, liquidation, fee/funding effects, and market gaps remain risks.
