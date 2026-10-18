"""Phase 2 step 5: the four handoff contracts (implementation plan, Appendix H).

The only way the agents talk to each other. Each is a Pydantic model with unknown fields forbidden and size
limits; the handoff API refuses anything that does not validate. Amounts are always copied by code from tool
results, never written by a model. Handoffs never carry prompts, reasoning text, raw tool output, signals
(trace) or message history.
"""
from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

SCHEMA_VERSION = 1
MAX_ACCOUNTS = 500
MAX_TRANSFERS = 2_000
RATIONALE_CHARS = 400

AccountId = Annotated[str, StringConstraints(pattern=r"^[0-9]{1,6}-[0-9A-Z]{6,12}$")]
TransferId = Annotated[str, StringConstraints(pattern=r"^T[0-9]{9}$")]
ComplaintId = Annotated[str, StringConstraints(pattern=r"^CMP-[0-9]{4}$")]
InvestigationId = Annotated[str, StringConstraints(pattern=r"^INV-[0-9A-Za-z-]{4,40}$")]
DecisionId = Annotated[str, StringConstraints(pattern=r"^DEC-[0-9A-Za-z-]{4,60}$")]
Paise = Annotated[int, Field(ge=0, le=10**15)]
ShortId = Annotated[str, StringConstraints(max_length=80)]


def two_sentences(text: str) -> str:
    """At most two sentences and RATIONALE_CHARS characters."""
    text = text.strip()
    if len(text) > RATIONALE_CHARS:
        raise ValueError(f"at most {RATIONALE_CHARS} characters")
    ends = re.findall(r"[.!?](?:\s|$)", text)
    if len(ends) > 2:
        raise ValueError("at most two sentences")
    return text


def clip_two_sentences(text: str) -> str:
    """For code that relays a model's text: keep the first two sentences, within RATIONALE_CHARS."""
    text = " ".join(str(text or "").split())
    parts = re.split(r"(?<=[.!?])\s+", text)
    out = " ".join(parts[:2]).strip()
    if len(out) > RATIONALE_CHARS:
        out = out[: RATIONALE_CHARS - 1].rsplit(" ", 1)[0] + "…"
    return out or "No reason given."


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


# ---- trace handoff: Tracer -> Adjudicator ---------------------------------------------------
BranchEnd = Literal["cashed_out", "absorbed_genuine", "no_later_outflow", "below_minimum", "depth_limit",
                    "returned_to_victim"]


class TraceLayer(Strict):
    layer: Annotated[int, Field(ge=1, le=30)]
    account_ids: list[AccountId] = Field(max_length=MAX_ACCOUNTS)
    transfer_ids: list[TransferId] = Field(max_length=MAX_TRANSFERS)


class TracedAccount(Strict):
    account_id: AccountId
    layer: Annotated[int, Field(ge=1, le=30)]
    tainted_paise: Paise          # copied by code from compute_taint
    balance_paise: Paise          # copied by code from compute_taint
    branch_end: BranchEnd | None = None   # why the branch ended here; None when the money moved on
    transfer_ids: list[TransferId] = Field(default_factory=list, max_length=50)   # tainted money arriving


class TraceHandoff(Strict):
    kind: Literal["trace"] = "trace"
    schema_version: Literal[1] = SCHEMA_VERSION
    investigation_id: InvestigationId
    complaint_id: ComplaintId
    disputed_paise: Paise
    trail: list[TraceLayer] = Field(min_length=1, max_length=30)
    accounts: list[TracedAccount] = Field(min_length=1, max_length=MAX_ACCOUNTS)
    unrecoverable_paise: Paise = 0          # left the banking graph (cash, Bitcoin)
    not_followed_paise: Paise = 0           # taint on branches below the minimum traceable amount
    trace_id: ShortId

    @model_validator(mode="after")
    def _consistent(self):
        ids = [a.account_id for a in self.accounts]
        if len(ids) != len(set(ids)):
            raise ValueError("an account appears twice")
        held = sum(a.tainted_paise for a in self.accounts)
        if held + self.unrecoverable_paise + self.not_followed_paise != self.disputed_paise:
            raise ValueError("tainted amounts do not add up to the disputed amount (conservation)")
        return self


# ---- proposal handoff: Adjudicator -> Verifier -------------------------------------------------
Label = Literal["mule", "likely_genuine", "uncertain"]
Action = Literal["lien", "review", "review_and_request_info", "none"]


class Evidence(Strict):
    transfer_ids: list[TransferId] = Field(default_factory=list, max_length=50)
    signals: list[Annotated[str, StringConstraints(pattern=r"^[a-z_]{3,40}$")]] = Field(default_factory=list,
                                                                                        max_length=12)
    claim_ids: list[ShortId] = Field(default_factory=list, max_length=20)


class AccountProposal(Strict):
    account_id: AccountId
    label: Label
    proposed_action: Action
    lien_proposal_id: DecisionId | None = None      # the Decision written by propose_lien
    lien_amount_paise: Paise | None = None          # copied by code from propose_lien's result
    evidence: Evidence
    lessons_applied: list[ShortId] = Field(default_factory=list, max_length=10)
    rationale: str

    _rationale = field_validator("rationale")(lambda cls, v: two_sentences(v))

    @model_validator(mode="after")
    def _lien_fields(self):
        if self.proposed_action == "lien" and not (self.lien_proposal_id and self.lien_amount_paise is not None):
            raise ValueError("a lien needs its proposal ID and code-calculated amount")
        if self.proposed_action != "lien" and (self.lien_proposal_id or self.lien_amount_paise):
            raise ValueError("only a lien carries a proposal ID and amount")
        return self


class ProposalHandoff(Strict):
    kind: Literal["proposal"] = "proposal"
    schema_version: Literal[1] = SCHEMA_VERSION
    investigation_id: InvestigationId
    attempt: Annotated[int, Field(ge=1, le=2)] = 1          # 2 after a check-failure note
    accounts: list[AccountProposal] = Field(min_length=1, max_length=MAX_ACCOUNTS)
    trace_id: ShortId


# ---- check-failure note: checks -> Adjudicator (at most once per case) ------------------------
class FailedCheck(Strict):
    check: Annotated[int, Field(ge=1, le=7)]
    account_ids: list[AccountId] = Field(default_factory=list, max_length=MAX_ACCOUNTS)


class CheckFailureNote(Strict):
    kind: Literal["check_failure"] = "check_failure"
    schema_version: Literal[1] = SCHEMA_VERSION
    investigation_id: InvestigationId
    failed: list[FailedCheck] = Field(min_length=1, max_length=7)


# ---- verdict record: Verifier -> gate code ----------------------------------------------------
class LienVerdict(Strict):
    decision_id: DecisionId
    verdict: Literal["approve", "reject"]
    reason: str
    decided_by: Literal["verifier_agent", "human"]
    lessons_cited: list[ShortId] = Field(default_factory=list, max_length=10)   # Phase 3: lessons the verdict relied on

    _reason = field_validator("reason")(lambda cls, v: two_sentences(v))


class VerdictRecord(Strict):
    kind: Literal["verdict"] = "verdict"
    schema_version: Literal[1] = SCHEMA_VERSION
    investigation_id: InvestigationId
    verdicts: list[LienVerdict] = Field(max_length=MAX_ACCOUNTS)
    checks_failed: bool = False          # a second check failure: no liens were proposed
    trace_id: ShortId


HANDOFFS = {"trace": TraceHandoff, "proposal": ProposalHandoff, "check_failure": CheckFailureNote,
            "verdict": VerdictRecord}
ROUTES = {"trace": ("tracer", "adjudicator"), "proposal": ("adjudicator", "verifier"),
          "check_failure": ("checks", "adjudicator"), "verdict": ("verifier", "gate")}


def parse(kind: str, payload: dict):
    """Validate a handoff payload; raises KeyError for an unknown kind and ValidationError for a bad payload."""
    return HANDOFFS[kind].model_validate(payload)
