# SemanticDuplicateRegistry

A standalone GenLayer Intelligent Contract **primitive**: an on-chain
registry that accepts text submissions and uses validator consensus to
reject entries that are *semantic* duplicates of something already
registered — even when the wording is different.

File: [`semantic_duplicate_registry.py`](./semantic_duplicate_registry.py)

## Deployment (GenLayer Studio)

**Current deployment** (includes the `locked_reserve`/`collected_fees` fix
and `get_balance()`):

| | |
|---|---|
| Contract address | [`0x3387a5EaAAbF7f36d9b37e3A9909883f709Aa766`](https://studio.genlayer.com/?import-contract=0x3387a5EaAAbF7f36d9b37e3A9909883f709Aa766) |
| Deployment tx | [`0xe64019f2...5af3c`](https://explorer-studio.genlayer.com/tx/0xe64019f2a1377066f2287d64aad1d6d723b2c77b4ec7a871df57ac9ad685af3c) |

Verified: `get_balance()` matches `get_collected_fees() + get_locked_reserve()`
after a live `submit_entry` call, confirming the fix holds on-chain.

<details>
<summary>Superseded deployments (older, kept for reference)</summary>

**Second deployment** (had the fix, superseded by the current one above):

| | |
|---|---|
| Contract address | [`0xf4C07301179C0cb22E4c7F0e713F15A2E2560373`](https://studio.genlayer.com/?import-contract=0xf4C07301179C0cb22E4c7F0e713F15A2E2560373) |
| Deployment tx | [`0xc343951e...82eacae`](https://explorer-studio.genlayer.com/tx/0xc343951e50dd57dcae075f6578420ecc7fc3a9d150c33f19ec45203ab82eacae) |

**Original deployment** (predates the fix entirely — do not use):

| Transaction | Link |
|---|---|
| Deployment | [`0xe84e058d...86875`](https://explorer-studio.genlayer.com/tx/0xe84e058d9451fd6c2c0deee396396fff68317d7eb80d3b33cc4987216b986875) |
| First `submit_entry` call | [`0xfeec1815...86990`](https://explorer-studio.genlayer.com/tx/0xfeec1815c1a5aa5b77da8fa9a1ec7e450ef3af7899efedc9dafd1af6c8386990) |

</details>

## Fixed: locked bonds vs. withdrawable fees

**The bug:** the original version tracked a single `collected_fees` pool
that included bonds backing currently-active entries — money implicitly
promised to a future successful challenger. `withdraw_fees` only checked
against that pool, so `admin` could legally withdraw funds a challenger
was owed, leaving `challenge_entry`'s payout unable to complete. A second,
related drift existed in `challenge_entry` itself: an upheld challenge
paid out `challenged.bond + CHALLENGE_BOND` but only decremented
`collected_fees` by `challenged.bond`, silently overstating the fee
ledger relative to the contract's real balance every time a challenge
succeeded.

**The fix:** bond accounting is now split into two pools:

- `locked_reserve` — bonds backing currently-active entries. Only ever
  released by paying out a successful challenge; `withdraw_fees` cannot
  touch it.
- `collected_fees` — bonds that are genuinely free: forfeited submission
  bonds (rejected duplicates), forfeited challenge bonds (failed
  challenges), and any voluntary overpayment above the required bond.
  This is the only pool `withdraw_fees` draws from.

`withdraw_fees` also checks `self.balance - self.locked_reserve` as a
second, independent guard, so even a future bookkeeping mistake elsewhere
in the contract still can't let `admin` withdraw money that's reserved
for a challenger. A new view, `get_locked_reserve()`, exposes the reserve
pool alongside `get_collected_fees()`.

## Why this is a primitive, not a demo

Exact-match duplicate checks are a solved, deterministic problem (hash the
input, look it up in a map). They're also trivially defeated by
paraphrasing. Whether two pieces of text express *the same idea* is a
judgment call — the exact class of problem traditional smart contracts
can't touch and GenLayer's Equivalence Principle exists for. This contract
is meant to be dropped into anything that needs "reject reworded
resubmissions" as a building block:

- Bounty / grant platforms rejecting a reworded copy of an existing bounty
  submission
- DAO proposal queues blocking near-duplicate proposals that would split a
  vote
- Prior-art / IP disclosure registries flagging a "new" idea that restates
  an existing one
- Forums or Q&A platforms merge-detecting duplicate questions before
  they're posted, with contestable on-chain state instead of a
  centralized mod queue

It deliberately stops at the contract layer — no frontend, no indexer —
so it can be read end-to-end in one sitting and reused as-is.

## How GenLayer consensus is used

1. **Non-comparative Equivalence Principle for the duplicate check.**
   `submit_entry` snapshots a bounded window of existing active entries
   into plain values, then calls
   `gl.eq_principle.prompt_non_comparative(build_context, task=..., criteria=...)`.
   The leader validator performs the duplicate-classification task once;
   other validators don't redo the classification from scratch, they check
   the leader's structured JSON verdict against an explicit `criteria`
   string (valid schema, internally consistent, grounded in the actual
   compared text, genuine semantic overlap rather than shared keywords).
   This is the pattern GenLayer's own documentation recommends for
   subjective tasks, and it is deliberately *not* `strict_eq` — LLM output
   is non-deterministic, so demanding byte-identical validator agreement
   on free text would almost never reach consensus.

2. **Same pattern for challenges.** `challenge_entry` lets anyone contest
   an already-registered entry after the fact, using the identical
   consensus pattern to independently verify the challenger's claim before
   revoking anything.

3. **Deterministic code never runs inside the non-deterministic closure,
   and vice versa.** Storage (`self.entries`, etc.) is read into local
   variables *before* the closure is built (storage objects aren't valid
   inside `eq_principle` closures per GenVM's execution model), and state
   is only mutated *after* the equivalence-principle call returns an
   agreed value. Parsing the returned JSON and updating `self.entries`
   happens in ordinary deterministic Python, so it executes identically
   for every validator once the consensus value itself is fixed.

4. **Bounded, cost-aware design.** The comparison window
   (`COMPARISON_WINDOW`, default 25 most-recent active entries) keeps the
   LLM prompt — and therefore the cost and latency of every submission —
   bounded regardless of how large the registry grows, and the LLM call is
   skipped entirely when the registry is empty. This is called out
   explicitly in the source because "compare the new item against
   everything that has ever been submitted" is an easy trap to fall into
   with this style of contract.

## Economic design (anti-spam)

- `submit_entry` is `payable` and requires `SUBMISSION_BOND`. It is a
  **posting fee, not a refundable deposit** — accepted whether or not the
  entry turns out to be a duplicate. This sidesteps relying on
  revert-refund semantics for a value-bearing call and gives spam attempts
  a real, non-zero cost regardless of outcome.
- `challenge_entry` requires `CHALLENGE_BOND`. A successful challenge pays
  the challenger the challenged entry's forfeited bond plus their own bond
  back; a failed challenge forfeits the challenger's bond. This gives the
  community a standing economic incentive to audit entries that slipped
  through, instead of requiring the registry to be perfect at submission
  time.
- `withdraw_fees` is a minimal admin-gated sweep of the fees the contract
  has accumulated, demonstrating `self.balance`-adjacent bookkeeping and
  an outbound value transfer to an arbitrary address via the
  `gl.evm.contract_interface` pattern documented under GenLayer's *Value
  Transfers* page.

## Public interface

| Method | Type | Description |
|---|---|---|
| `submit_entry(title, body)` | write, payable | Register a new entry unless consensus flags it as a semantic duplicate |
| `challenge_entry(entry_id, alleged_duplicate_of)` | write, payable | Contest an existing entry as a duplicate of an earlier one |
| `withdraw_fees(to, amount)` | write | Admin-only sweep of collected bonds |
| `get_entry(entry_id)` | view | Fetch one entry |
| `get_entry_count()` | view | Total entries ever registered |
| `list_active_entries(offset, limit)` | view | Paginated listing of active entries |
| `get_collected_fees()` | view | Withdrawable fees (forfeited bonds + overpayment only) |
| `get_locked_reserve()` | view | Bonds reserved against active entries; not withdrawable |
| `get_balance()` | view | Actual GEN balance held by the contract — should always equal `get_collected_fees() + get_locked_reserve()` |

## Manual test plan

This primitive has been checked for consistency against GenLayer's current
public SDK documentation (storage rules, `eq_principle` signatures, value
transfer mechanics), but has not been executed against a live GenVM/Studio
instance in this environment. Before relying on it, run through this plan
in [GenLayer Studio](https://studio.genlayer.com) (or Studionet/Localnet):

1. **Deploy** with your own address as `admin`.
2. **First submission is always free of comparison** — call
   `submit_entry("Solar farm bounty", "Build a 5MW solar farm in region X")`
   with `value >= SUBMISSION_BOND`. Confirm it returns
   `"registered as entry #0"` and `get_entry(0)` reflects it.
3. **Exact duplicate** — submit the same title/body again. Expect a
   `"rejected: semantic duplicate of entry #0"` result and `get_entry_count()`
   unchanged.
4. **Paraphrased duplicate** — submit a reworded version (different words,
   same ask). This is the core test: confirm it is still caught, which is
   the whole reason this needs GenLayer instead of a hash check.
5. **Genuinely distinct submission** — submit an unrelated title/body.
   Confirm it registers as entry #1.
6. **Insufficient bond** — call `submit_entry` with `value` below
   `SUBMISSION_BOND`. Confirm it reverts with the bond-requirement message
   and no entry is created.
7. **Challenge flow** — from a second account, call
   `challenge_entry(1, 0)` claiming entry #1 duplicates entry #0 when it
   doesn't. Confirm the challenge is rejected and the challenger's bond is
   forfeited (`get_collected_fees()` increases). Then construct a case
   where a duplicate genuinely did slip through and confirm
   `challenge_entry` correctly revokes it and pays out the challenger.
8. **Admin withdrawal** — call `withdraw_fees` from a non-admin account and
   confirm it reverts; call it from `admin` and confirm the balance moves.
9. **Window bound** — register more than `COMPARISON_WINDOW` entries and
   confirm submissions still succeed at reasonable cost/latency (i.e. the
   contract isn't silently comparing against the entire history).
10. **Reserve isolation (the fix)** — after step 2's entry is registered,
    call `get_locked_reserve()` and confirm it now includes that entry's
    bond, while `get_collected_fees()` does **not**. As `admin`, call
    `withdraw_fees` for an amount greater than `get_collected_fees()` but
    still less than the contract's total balance — confirm it reverts.
    Then run a successful challenge against that entry (per step 7) and
    confirm `get_locked_reserve()` drops by exactly the entry's bond,
    while `get_collected_fees()` is unaffected by that payout.

If you adapt this for production, also pressure-test prompt-injection
resistance (a submission body that tries to instruct the LLM to always
return `is_duplicate: false`) per GenLayer's *Security and Best Practices*
guidance, and confirm the exact `emit_transfer` call shape against the
GenVM SDK version you deploy against — signatures have shifted across SDK
releases (see GenLayer's Changelog) and should be re-checked at deploy
time.
