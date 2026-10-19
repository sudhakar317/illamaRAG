"""The claim tree's data: claims, and the run that holds them between turns.

A run must survive the gap between asking the user a question and getting the
answer, which is a separate HTTP request and possibly a page reload later, so
both types round-trip through plain dicts for storage as JSON.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields

# Claim states. "open" is searchable now; "blocked" waits on a claim it depends
# on; "unsearchable" is a profile fact no document could hold; "exhausted" has
# used its search budget without support; "asked" waits on the user; "unknown"
# is what the user could not answer either.
SETTLED = {"supported", "refuted", "answered"}


@dataclass
class Claim:
    id: str
    text: str
    kind: str  # shared_knowledge | profile_fact
    importance: float = 1.0
    # The specific thing the claim is about ("Standard plan"), or "". A
    # passage can only settle the claim if it names this; see tree._subjects.
    subject: str = ""
    key: str = ""  # profile facts only: the profile store key
    depends_on: str | None = None
    query: str = ""  # the next search to run
    state: str = "open"
    # "plan" for claims the agent derived from the question; "stated" for a
    # fact the user asserted in their message, such as a correction.
    origin: str = "plan"
    queries_tried: list[str] = field(default_factory=list)
    # Searches spent since the claim was last reopened. Separate from
    # queries_tried so an answer can grant a fresh budget without losing history.
    budget_used: int = 0
    # Chunk ids already put to the judge for this claim; never judged twice.
    seen: list[int] = field(default_factory=list)
    best_score: float = 0.0
    evidence: list[dict] = field(default_factory=list)
    # The closest passages found that did not settle the claim. Kept because
    # "the documents list this under the Standard plan" is worth saying even
    # when it cannot be said to apply to the user's plan.
    related: list[dict] = field(default_factory=list)
    answer: str | None = None
    answer_source: str | None = None  # user | profile

    @property
    def settled(self) -> bool:
        return self.state in SETTLED


@dataclass
class Run:
    id: str
    session_id: str
    question: str
    status: str = "running"  # running | awaiting | done | stopped | failed
    claims: list[Claim] = field(default_factory=list)
    questions: list[dict] = field(default_factory=list)
    # What the agent did, in order: {id, stage, label, status, ms, details}.
    steps: list[dict] = field(default_factory=list)
    rounds: int = 0
    ask_rounds: int = 0

    def claim(self, claim_id: str | None) -> Claim | None:
        return next((c for c in self.claims if c.id == claim_id), None)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> Run:
        # Unknown keys are dropped rather than fatal, so a run saved by an
        # older version still loads (its flat "log" predates steps).
        claim_fields = {f.name for f in fields(Claim)}
        run_fields = {f.name for f in fields(cls)} - {"claims"}
        claims = [
            Claim(**{k: v for k, v in c.items() if k in claim_fields})
            for c in data.get("claims", [])
        ]
        return cls(**{k: v for k, v in data.items() if k in run_fields}, claims=claims)
