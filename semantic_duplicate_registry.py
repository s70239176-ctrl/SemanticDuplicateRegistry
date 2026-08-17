# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }
"""
SemanticDuplicateRegistry
==========================

A reusable Intelligent Contract *primitive* for registering text submissions
(proposals, forum posts, bounty entries, prior-art claims, DAO proposals,
content-platform posts, etc.) while rejecting entries that are semantically
— not just textually — duplicates of something already registered.

WHY THIS NEEDS GENLAYER
-----------------------
Exact-match / hash-based duplicate detection (what a normal smart contract
can do) only catches byte-identical resubmissions. It is trivially defeated
by paraphrasing. Deciding "is this substantially the same idea as entry #7,
just reworded?" is a judgment call that requires reading comprehension, not
string comparison — exactly the class of decision GenLayer's Equivalence
Principle exists for. A single centralized LLM call could make that same
judgment, but then every user has to trust one server/operator/model not to
wave through a friend's duplicate or reject a rival's original submission.
Here, a leader validator proposes a judgment and an independently-selected
set of validators must independently agree it was reasonable before the
registry's state changes — no single party controls the outcome.

CONSENSUS DESIGN
-----------------
* Uses `gl.eq_principle.prompt_non_comparative`, which is the right tool
  for a *subjective* task: the leader performs the duplicate-detection
  task once, and validators check the leader's JSON verdict against an
  explicit `criteria` string (structure + internal consistency + grounded
  reasoning) rather than each re-deriving their own independent verdict
  from scratch and comparing. This is cheaper than the comparative
  principle and is the pattern GenLayer's own docs recommend for anything
  where "equivalent" doesn't mean "identical."
* `gl.eq_principle.strict_eq` is deliberately NOT used for the LLM call —
  LLM output is non-deterministic, so exact-match consensus would almost
  always fail to reach agreement.
* All state access happens in deterministic code. Non-deterministic
  closures never touch `self.*` (storage is inaccessible from inside
  them); instead we snapshot exactly the plain values the closure needs
  into local variables *before* calling the equivalence-principle
  function, and only mutate storage *after* consensus has returned.
* The number of prior entries fed into any one duplicate check is capped
  (`COMPARISON_WINDOW`) and a full comparison is skipped entirely when
  the registry is empty. Unbounded "compare against all of history" is a
  common design mistake in this style of contract: it makes prompts grow
  without bound and makes the LLM call increasingly expensive and slow
  with the size of the registry, for shrinking marginal benefit against
  older, less-related entries.

ECONOMIC / SPAM DESIGN
-----------------------
* `submit_entry` is `payable` and requires a `SUBMISSION_BOND`. This is a
  posting fee, not a refundable deposit: if consensus finds the entry is a
  duplicate, the bond is *not* returned — it stays in the contract. This
  sidesteps having to reason about whether GenLayer refunds value on a
  reverted call (behavior that can vary/change), and it gives spammers a
  real cost per attempt regardless of outcome.
* Anyone can later challenge an already-registered entry with
  `challenge_entry`, backing the challenge with `CHALLENGE_BOND`. If
  validators agree the challenge is correct, the challenged entry is
  revoked and the challenger is paid the challenged entry's forfeited
  bond plus their own bond back. If the challenge fails, the challenger's
  bond is forfeited instead. This gives the community an economic reason
  to audit entries that slipped through, without requiring the registry
  to be perfect at submission time.
* An `admin` address (set at deploy) can sweep accumulated forfeited fees
  out of the contract with `withdraw_fees`. This is a minimal example of
  access control + `self.balance` + an outbound value transfer; swap it
  for a DAO-controlled multisig-style check in production.
* Bond accounting is split into two pools so `withdraw_fees` can never
  touch money that's earmarked for a future payout:
    - `locked_reserve` -- the sum of bonds backing currently-active
      entries. This is what a successful challenger is promised. It is
      only ever decremented by paying out a successful challenge, never
      by `withdraw_fees`.
    - `collected_fees` -- money that is genuinely free: forfeited
      submission bonds (rejected duplicates), forfeited challenge bonds
      (failed challenges), and any voluntary overpayment above the
      required bond. This is the only pool `withdraw_fees` can draw
      from. `withdraw_fees` also asserts against `self.balance -
      self.locked_reserve` as a defense-in-depth check, so even a future
      accounting mistake elsewhere can't let the admin drain funds a
      challenger is owed.

WHERE YOU'D REUSE THIS PRIMITIVE
----------------------------------
* Bounty / grant platforms: reject a bounty submission that is a reworded
  copy of an existing submission.
* DAO proposal queues: block near-duplicate governance proposals from
  splitting a vote.
* Prior-art / IP registries: flag a "new" invention disclosure that
  restates an existing one.
* Forums / Q&A platforms: merge-detect duplicate questions before they're
  posted, with on-chain, contestable state instead of a centralized mod
  queue.

WHAT THIS IS NOT
-----------------
This is a primitive, not a full application: there is no frontend, no
indexer, no notification system. It is meant to be deployed as-is for
testing and then wrapped by whatever product needs duplicate-aware
registration.
"""

from genlayer import *
from dataclasses import dataclass
import json
import typing

# ---------------------------------------------------------------------------
# Tunable constants
# ---------------------------------------------------------------------------

# How many of the most recent ACTIVE entries are shown to the LLM when
# checking a new submission for duplicates. Keeps prompt size (and cost)
# bounded regardless of how large the registry grows. Raise this if your
# use case needs a deeper duplicate-detection horizon; lower it to cut cost.
COMPARISON_WINDOW = 25

# Anti-spam posting fee for submit_entry, forfeited on a rejected (duplicate)
# submission. Denominated in wei (1 GEN = 10**18 wei) per GenLayer convention.
SUBMISSION_BOND = u256(10 ** 16)  # 0.01 GEN

# Bond required to challenge an existing entry as a duplicate.
# Kept smaller than SUBMISSION_BOND since challenging is a lighter action
# than authoring new content, but still costly enough to deter spam challenges.
CHALLENGE_BOND = u256(5 * 10 ** 15)  # 0.005 GEN

MAX_TITLE_LEN = 200
MAX_BODY_LEN = 4000


# ---------------------------------------------------------------------------
# Storage types
# ---------------------------------------------------------------------------


@allow_storage
@dataclass
class Entry:
    id: u256
    submitter: Address
    title: str
    body: str
    bond: u256
    active: bool


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


class EntryRegistered(gl.Event):
    def __init__(self, entry_id: u256, submitter: Address, title: str, /):
        ...


class SubmissionRejected(gl.Event):
    def __init__(
        self, submitter: Address, title: str, duplicate_of: u256, /
    ):
        ...


class EntryRevoked(gl.Event):
    def __init__(self, entry_id: u256, challenger: Address, /):
        ...


class ChallengeRejected(gl.Event):
    def __init__(self, entry_id: u256, challenger: Address, /):
        ...


# ---------------------------------------------------------------------------
# Helper for sending GEN to an arbitrary EOA / EVM address. See GenLayer's
# "Value Transfers" docs: sending to an EOA is an external message that goes
# through the same contract-interface mechanism used for EVM contracts, even
# though the recipient holds no code.
# ---------------------------------------------------------------------------


@gl.evm.contract_interface
class _PayoutRecipient:
    class View:
        pass

    class Write:
        pass


# ---------------------------------------------------------------------------
# Contract
# ---------------------------------------------------------------------------


class SemanticDuplicateRegistry(gl.Contract):
    admin: Address
    entries: DynArray[Entry]
    entry_count: u256
    collected_fees: u256
    locked_reserve: u256

    def __init__(self, admin: str = "0xF79cFE699bB098c129Df37918d384dd7B6531157"):
        self.admin = Address(admin)
        self.entry_count = u256(0)
        self.collected_fees = u256(0)
        self.locked_reserve = u256(0)

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    @gl.public.write.payable
    def submit_entry(self, title: str, body: str) -> str:
        """
        Register a new entry, unless GenLayer consensus determines it is a
        semantic duplicate of an existing active entry, in which case the
        submission fee is forfeited and nothing is written to the registry.
        """
        # --- cheap deterministic validation first, before spending a
        #     non-deterministic (LLM) call on obviously-bad input ---
        if gl.message.value < SUBMISSION_BOND:
            raise gl.vm.UserError(
                f"submission requires a bond of at least {SUBMISSION_BOND} wei"
            )
        title = title.strip()
        body = body.strip()
        if len(title) == 0 or len(body) == 0:
            raise gl.vm.UserError("title and body must be non-empty")
        if len(title) > MAX_TITLE_LEN:
            raise gl.vm.UserError(f"title exceeds {MAX_TITLE_LEN} characters")
        if len(body) > MAX_BODY_LEN:
            raise gl.vm.UserError(f"body exceeds {MAX_BODY_LEN} characters")

        # The bond is accepted regardless of outcome (see module docstring),
        # but where it goes depends on the outcome, decided below:
        #   - duplicate  -> forfeited, moves to collected_fees (withdrawable)
        #   - unique     -> locked_reserve (reserved for a future successful
        #                    challenger, NOT withdrawable by admin)
        # Any amount paid above SUBMISSION_BOND is treated as a voluntary
        # fee either way, since Entry.bond always records exactly
        # SUBMISSION_BOND regardless of overpayment.
        excess = gl.message.value - SUBMISSION_BOND
        if excess > u256(0):
            self.collected_fees = self.collected_fees + excess

        # --- snapshot the comparison window into plain values BEFORE
        #     building the non-deterministic closure; storage objects are
        #     not usable inside eq_principle closures. ---
        window: list[tuple[u256, str, str]] = []
        n = len(self.entries)
        start = 0 if n <= COMPARISON_WINDOW else n - COMPARISON_WINDOW
        for i in range(start, n):
            e = self.entries[i]
            if e.active:
                window.append((e.id, e.title, e.body))

        submitter = gl.message.sender_address

        if len(window) == 0:
            # Nothing to compare against — skip the LLM call entirely.
            # Entry is registered, so its bond is locked (reserved), not
            # collected as a fee.
            self.locked_reserve = self.locked_reserve + SUBMISSION_BOND
            return self._register(submitter, title, body)

        new_title = title
        new_body = body

        def build_context() -> str:
            lines = [
                "NEW SUBMISSION",
                f"title: {new_title}",
                f"body: {new_body}",
                "",
                "EXISTING ENTRIES (id: title -- body):",
            ]
            for eid, etitle, ebody in window:
                lines.append(f"[{eid}] {etitle} -- {ebody}")
            return "\n".join(lines)

        verdict_json = gl.eq_principle.prompt_non_comparative(
            build_context,
            task=(
                "Decide whether NEW SUBMISSION is a semantic (near-)duplicate "
                "of any entry in EXISTING ENTRIES -- i.e. it makes substantially "
                "the same claim, proposal, or point, even if reworded, "
                "reordered, translated in tone, or padded with extra detail. "
                "Respond with STRICT JSON ONLY, no prose outside the JSON, "
                "matching exactly this shape: "
                '{"is_duplicate": true|false, "duplicate_of": <id-or-null>, '
                '"rationale": "<one sentence>"}'
            ),
            criteria="""
The response must be valid JSON with exactly the keys is_duplicate,
duplicate_of, and rationale, and no other top-level keys.
If is_duplicate is true, duplicate_of must equal the numeric id of one of
the entries actually listed under EXISTING ENTRIES in the input.
If is_duplicate is false, duplicate_of must be null.
The rationale must be consistent with the is_duplicate/duplicate_of values
and must reference concrete content from the compared entries rather than
generic boilerplate language.
The judgment should reflect genuine overlap in the core claim, proposal, or
content -- not merely a shared broad topic, shared keywords, or similar
length.
""",
        )

        try:
            verdict = json.loads(verdict_json)
            is_duplicate = bool(verdict["is_duplicate"])
            duplicate_of = verdict.get("duplicate_of")
            rationale = str(verdict.get("rationale", ""))
        except Exception:
            # Defensive fallback: if consensus somehow produced something
            # that doesn't parse as the agreed schema, fail closed (treat
            # as a duplicate / do not register) rather than trusting
            # unparseable output. This should be rare given the criteria
            # above enforces valid JSON.
            raise gl.vm.UserError(
                "duplicate-check consensus returned an unparseable verdict; "
                "submission rejected, please retry"
            )

        if is_duplicate:
            # Forfeited: this bond is now genuinely free for the admin to
            # withdraw, since no entry was created and nothing needs to be
            # reserved for a future challenger.
            self.collected_fees = self.collected_fees + SUBMISSION_BOND
            dup_id = u256(int(duplicate_of)) if duplicate_of is not None else u256(0)
            SubmissionRejected(submitter, title, dup_id).emit()
            return (
                f"rejected: semantic duplicate of entry #{dup_id}. {rationale}"
            )

        # Unique: bond is locked, not collected -- it backs this entry
        # against a future successful challenge.
        self.locked_reserve = self.locked_reserve + SUBMISSION_BOND
        return self._register(submitter, title, body)

    def _register(self, submitter: Address, title: str, body: str) -> str:
        new_id = self.entry_count
        self.entries.append(
            Entry(
                id=new_id,
                submitter=submitter,
                title=title,
                body=body,
                bond=SUBMISSION_BOND,
                active=True,
            )
        )
        self.entry_count = self.entry_count + u256(1)
        EntryRegistered(new_id, submitter, title).emit()
        return f"registered as entry #{new_id}"

    @gl.public.write.payable
    def challenge_entry(self, entry_id: u256, alleged_duplicate_of: u256) -> str:
        """
        Challenge an already-registered entry as being a semantic duplicate
        of an earlier one. If GenLayer consensus agrees, the challenged
        entry is revoked and the challenger is paid its forfeited bond plus
        their own challenge bond back. If consensus disagrees, the
        challenger's bond is forfeited instead.
        """
        if gl.message.value < CHALLENGE_BOND:
            raise gl.vm.UserError(
                f"challenge requires a bond of at least {CHALLENGE_BOND} wei"
            )
        if entry_id >= self.entry_count or alleged_duplicate_of >= self.entry_count:
            raise gl.vm.UserError("entry id out of range")
        if entry_id == alleged_duplicate_of:
            raise gl.vm.UserError("an entry cannot duplicate itself")
        if alleged_duplicate_of >= entry_id:
            raise gl.vm.UserError(
                "alleged_duplicate_of must be an earlier entry than entry_id"
            )

        # As with submit_entry: only a genuine overpayment is immediately
        # treated as a free fee. The CHALLENGE_BOND itself is not credited
        # to collected_fees yet -- its fate (forfeited fee vs. returned to
        # challenger) depends on the verdict below.
        excess = gl.message.value - CHALLENGE_BOND
        if excess > u256(0):
            self.collected_fees = self.collected_fees + excess

        challenged = self.entries[int(entry_id)]
        original = self.entries[int(alleged_duplicate_of)]
        if not challenged.active:
            raise gl.vm.UserError("entry is already revoked")

        challenger = gl.message.sender_address

        c_title, c_body = challenged.title, challenged.body
        o_title, o_body = original.title, original.body
        c_id, o_id = challenged.id, original.id

        def build_context() -> str:
            return (
                f"ENTRY A (id={c_id}, the CHALLENGED entry)\n"
                f"title: {c_title}\nbody: {c_body}\n\n"
                f"ENTRY B (id={o_id}, the ALLEGED ORIGINAL)\n"
                f"title: {o_title}\nbody: {o_body}\n"
            )

        verdict_json = gl.eq_principle.prompt_non_comparative(
            build_context,
            task=(
                "A challenger claims ENTRY A is a semantic duplicate of "
                "ENTRY B (i.e. makes substantially the same claim or "
                "proposal, even if reworded). Verify this claim carefully "
                "and respond with STRICT JSON ONLY matching exactly: "
                '{"claim_upheld": true|false, "rationale": "<one sentence>"}'
            ),
            criteria="""
The response must be valid JSON with exactly the keys claim_upheld and
rationale, and no other top-level keys.
claim_upheld should be true only if ENTRY A and ENTRY B genuinely make the
same core claim, proposal, or content -- not merely share a topic or some
keywords.
The rationale must reference concrete content from both entries and must be
consistent with the claim_upheld value.
""",
        )

        try:
            verdict = json.loads(verdict_json)
            claim_upheld = bool(verdict["claim_upheld"])
            rationale = str(verdict.get("rationale", ""))
        except Exception:
            raise gl.vm.UserError(
                "challenge consensus returned an unparseable verdict; "
                "challenge rejected, please retry"
            )

        if claim_upheld:
            self.entries[int(entry_id)] = Entry(
                id=challenged.id,
                submitter=challenged.submitter,
                title=challenged.title,
                body=challenged.body,
                bond=challenged.bond,
                active=False,
            )
            # Release the reserve that was backing the now-revoked entry,
            # and pay it out together with the challenger's own bond back.
            # Neither leg of this payout ever touches collected_fees --
            # it was never fee money to begin with.
            self.locked_reserve = self.locked_reserve - challenged.bond
            payout = challenged.bond + CHALLENGE_BOND
            _PayoutRecipient(challenger).emit_transfer(value=payout)
            EntryRevoked(entry_id, challenger).emit()
            return f"challenge upheld: entry #{entry_id} revoked. {rationale}"
        else:
            # Challenge failed: challenger's bond is forfeited and becomes
            # a genuine, withdrawable fee.
            self.collected_fees = self.collected_fees + CHALLENGE_BOND
            ChallengeRejected(entry_id, challenger).emit()
            return f"challenge rejected. {rationale}"

    @gl.public.write
    def withdraw_fees(self, to: str, amount: u256) -> None:
        """
        Admin-only sweep of accumulated forfeited/collected bonds.

        Cannot draw on locked_reserve: that pool is reserved to pay out a
        future successful challenger against a currently-active entry, and
        is checked here in two independent ways -- against the tracked
        collected_fees ledger, and as a defense-in-depth check against the
        contract's actual balance minus locked_reserve, so a future
        accounting bug elsewhere in the contract still can't let the admin
        withdraw money a challenger is owed.
        """
        if gl.message.sender_address != self.admin:
            raise gl.vm.UserError("only admin may withdraw fees")
        if amount > self.collected_fees:
            raise gl.vm.UserError("amount exceeds collected fees")
        withdrawable_by_balance = self.balance - self.locked_reserve
        if amount > withdrawable_by_balance:
            raise gl.vm.UserError(
                "amount exceeds balance available after reserving "
                "locked entry bonds"
            )
        self.collected_fees = self.collected_fees - amount
        _PayoutRecipient(Address(to)).emit_transfer(value=amount)

    # ------------------------------------------------------------------
    # Views
    # ------------------------------------------------------------------

    @gl.public.view
    def get_entry(self, entry_id: u256) -> TreeMap[str, typing.Any]:
        e = self.entries[int(entry_id)]
        return {
            "id": e.id,
            "submitter": e.submitter,
            "title": e.title,
            "body": e.body,
            "bond": e.bond,
            "active": e.active,
        }

    @gl.public.view
    def get_entry_count(self) -> u256:
        return self.entry_count

    @gl.public.view
    def list_active_entries(self, offset: u256, limit: u256) -> DynArray[TreeMap[str, typing.Any]]:
        out = []
        i = int(offset)
        n = len(self.entries)
        collected = 0
        while i < n and collected < int(limit):
            e = self.entries[i]
            if e.active:
                out.append(
                    {
                        "id": e.id,
                        "submitter": e.submitter,
                        "title": e.title,
                        "active": e.active,
                    }
                )
                collected += 1
            i += 1
        return out

    @gl.public.view
    def get_collected_fees(self) -> u256:
        return self.collected_fees

    @gl.public.view
    def get_locked_reserve(self) -> u256:
        """Sum of bonds currently reserved to pay out a successful
        challenge against an active entry. Not withdrawable by admin."""
        return self.locked_reserve

    @gl.public.view
    def get_balance(self) -> u256:
        """Actual GEN balance held by the contract. Should always equal
        get_collected_fees() + get_locked_reserve() -- if it doesn't,
        that's a real accounting bug, not just an unfamiliar number."""
        return self.balance
