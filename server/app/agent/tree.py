"""The claim tree: query, documents, ask, documents again, answer, store.

Code owns the loop. The model is called only to fill a structure that code
then acts on, and always for a whole level of the tree at once -- every claim
being judged goes into one judging call, every thin search into one
rewording call. That keeps a run to roughly seven or eight model calls
however many claims it holds, which is what makes a tree affordable on a CPU.

The stages, each streamed to the client as a timed step:

    understand  the message becomes classified claims, plus any facts the
                user stated in it (a correction, their plan)
    search      kb_search runs for every open shared_knowledge claim
    check       one call names what each passage is about and whether it
                settles its claim; code then verifies the verdict
    reword      one call rewords every search that came back thin
    ask         the claims no search can settle become questions; the run
                is saved and the stream ends, waiting for the answers
    integrate   answers settle their claims and rewrite the ones waiting on
                them, which go back to search -- the second documents pass
    answer      streamed from the settled tree, citing what supports it
    store       what the user told us becomes proposals, never writes

Model output is never taken on trust. A verdict counts only if its quote is
really in the passage and, for a claim about one plan or version, the
passage actually names it. Those two checks are what stop "Standard plan
features: ... custom functions" from proving the Free plan has them.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import asdict

from app.agent import model, prompts, tools
from app.agent import store as agent_store
from app.agent.claims import Claim, Run
from app.core import llm
from app.rag import embedder

logger = logging.getLogger(__name__)

# Searches per claim each time it is opened: the first query and two rewordings.
MAX_SEARCHES = 3
# Search rounds per pass through the documents; a backstop on top of the above.
MAX_ROUNDS = 4
# Times one run may stop to ask. The second exists for the case the first
# answer unlocks a search that still comes back empty.
MAX_ASK_ROUNDS = 2
MAX_QUESTIONS = 2
# Related passages kept per unsettled claim, for the answer and the question.
MAX_RELATED = 2
# Passages shown to the judge per claim. Search returns more; judging time
# grows with every passage, and the best few carry nearly all the signal.
JUDGE_HITS = 3
# Two searchable claims this close in meaning are one claim worded twice.
# Measured on nomic-embed: the duplicates a 4B model writes ("features based
# on their plan" / "...based on their role") score 0.91 and up, while claims
# that are genuinely different but about the same subject reach 0.87 ("what
# error E42 means" / "how to fix E42") and "Pro plan features" against "Free
# plan features" scores 0.84. The margin is narrow, so a merge also requires
# both claims to depend on the same thing.
DUPLICATE_SIMILARITY = 0.90
# How often a long model call reports progress to the client.
PROGRESS_SECONDS = 0.5

DONT_KNOW = {
    "i don't know", "i dont know", "don't know", "dont know", "not sure",
    "no idea", "unknown", "i do not know",
}

Event = dict


class Cancelled(Exception):
    """The client went away or pressed Stop."""


def new_run(session_id: str, question: str) -> Run:
    return Run(id=str(uuid.uuid4()), session_id=session_id, question=question)


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:40] or "fact"


class Agent:
    """Drives one run. Every public method is a generator of client events.

    `emit` is for events produced *inside* a blocking model call -- token
    progress -- which cannot be yielded from there; everything else is yielded.
    """

    def __init__(
        self,
        run: Run,
        should_stop: Callable[[], bool],
        emit: Callable[[Event], None] | None = None,
    ):
        self.run = run
        self.should_stop = should_stop
        self.emit = emit or (lambda event: None)
        # Read once per request: stated facts update it as the run goes.
        self.profile = agent_store.profile_all()
        self._step: dict | None = None
        self._step_t0 = 0.0
        self._progress_at = 0.0

    # ------------------------------------------------------------ entry points

    def start(self, history: list[dict]) -> Iterator[Event]:
        """A new message: build the tree, then drive it."""
        yield self._begin("understand", "Understanding your message")
        data = self._call(
            prompts.DECOMPOSE_SYSTEM,
            _decompose_user(self.run.question, history, self.profile),
            prompts.DECOMPOSE_SCHEMA,
            max_tokens=480,
        )

        # User facts become the first claims, classified at birth, so a fact no
        # document holds is asked for at once instead of burning a search
        # budget first.
        fact_ids: list[str] = []
        for raw in data["user_facts"]:
            key = slug(raw["key"] or raw["text"])
            if any(self.run.claim(f).key == key for f in fact_ids):
                continue
            claim = self._add(
                text=raw["text"].strip() or key.replace("_", " "),
                kind="profile_fact",
                key=key,
                state="unsearchable",
            )
            known = self.profile.get(key)
            if known:
                claim.state = "answered"
                claim.answer = known
                claim.answer_source = "profile"
            fact_ids.append(claim.id)

        for raw in data["claims"]:
            dep = raw["uses_user_fact"]
            text = raw["text"].strip() or self.run.question
            self._add(
                text=text,
                kind="shared_knowledge",
                subject=raw["subject"].strip(),
                importance=1.0 if raw["importance"] == "high" else 0.5,
                depends_on=fact_ids[dep] if 0 <= dep < len(fact_ids) else None,
                query=raw["query"].strip() or text,
            )

        yield from self._take_statements(data["stated_facts"])

        for c in self.run.claims:
            yield self._note(_describe(c))
        yield from self._merge_duplicates()
        yield self._tree()
        yield from self._drive()

    def resume(self, answers: dict[str, str]) -> Iterator[Event]:
        """The user answered: settle those claims, reopen what waited on them."""
        run = self.run
        run.status = "running"
        yield self._begin("integrate", "Using your answers")
        answered: set[str] = set()
        unknown: set[str] = set()

        for question in run.questions:
            claim = run.claim(question["claim_id"])
            if claim is None or claim.state != "asked":
                continue
            reply = (answers.get(claim.id) or "").strip()
            if not reply or reply.lower().strip(" .!") in DONT_KNOW:
                claim.state = "unknown"
                unknown.add(claim.id)
                yield self._note(f"{claim.id}: you don't know either, so it stays open")
                continue
            claim.answer = reply
            claim.answer_source = "user"
            claim.state = "answered"
            if claim.kind == "profile_fact":
                # Used at once by the subject check on the searches to come.
                self.profile[claim.key] = reply
            answered.add(claim.id)
            yield self._note(f'{claim.id}: you answered "{_clip(reply, 80)}"')
        run.questions = []
        yield self._tree()

        waiting = [
            c
            for c in run.claims
            if c.kind == "shared_knowledge"
            and c.state in ("blocked", "exhausted")
            and c.depends_on in answered | unknown
        ]
        rewrite = [c for c in waiting if c.depends_on in answered]
        if rewrite:
            # The answer usually turns a vague claim ("that plan's features")
            # into one a search can find ("Pro plan features").
            data = self._call(
                prompts.INTEGRATE_SYSTEM,
                _integrate_user(run, rewrite),
                prompts.integrate_schema([c.id for c in rewrite]),
                max_tokens=110 * len(rewrite) + 48,
            )
            by_id = {r["claim"]: r for r in data["claims"]}
            for c in rewrite:
                r = by_id.get(c.id, {})
                c.text = r.get("text", "").strip() or c.text
                c.query = r.get("query", "").strip() or c.text
                yield self._note(f'{c.id}: now "{c.text}"')

        for c in waiting:
            c.state = "open"
            c.budget_used = 0  # a fresh budget: the claim is a new search now
        # Rewriting with the same answer often turns two claims into one.
        yield from self._merge_duplicates()
        yield self._tree()
        yield from self._drive()

    # --------------------------------------------------------------- building

    def _add(self, **fields) -> Claim:
        claim = Claim(id=f"c{len(self.run.claims) + 1}", **fields)
        self.run.claims.append(claim)
        return claim

    def _take_statements(self, stated: list[dict]) -> Iterator[Event]:
        """Facts the user asserted in their message.

        About themselves ("I am on the Team plan") settles or creates a profile
        claim: only they can know it. About the world ("custom functions are
        in Standard, not Free") becomes a claim to *check*, searched and judged
        like any other, with the user's words kept beside it. Saying something
        does not make it so -- and a small model can misread a correction into
        its opposite -- so the answer reports what the documents say about
        it, and the store step proposes keeping it only with that verdict
        attached. This is how a correction stops being lost without ever
        being repeated back as fact.
        """
        for raw in stated:
            value = raw["value"].strip()
            if not value:
                continue
            if raw["about"] == "user":
                key = slug(raw["key"] or "fact")
                claim = next(
                    (c for c in self.run.claims if c.kind == "profile_fact" and c.key == key),
                    None,
                ) or self._add(
                    text=f"The user's {key.replace('_', ' ')}",
                    kind="profile_fact",
                    key=key,
                )
                claim.state = "answered"
                claim.answer = value
                claim.answer_source = "user"
                claim.origin = "stated"
                self.profile[key] = value
            else:
                self._add(
                    text=value,
                    kind="shared_knowledge",
                    importance=0.5,
                    query=value,
                    answer=value,
                    answer_source="user",
                    origin="stated",
                )
        if stated:
            yield self._tree()

    def _merge_duplicates(self) -> Iterator[Event]:
        """Drop searchable claims that repeat an earlier one.

        Done by code, not by asking the model to avoid it: a small model
        reliably words the same claim twice, and every duplicate costs a
        search budget and, worse, the same question asked twice.
        """
        live = [
            c for c in self.run.claims
            if c.kind == "shared_knowledge" and c.state in ("open", "blocked")
        ]
        if len(live) < 2:
            return
        vectors = embedder.embed_documents([c.text for c in live])
        dropped: set[str] = set()
        for i, first in enumerate(live):
            if first.id in dropped:
                continue
            for j in range(i + 1, len(live)):
                other = live[j]
                if other.id in dropped or other.depends_on != first.depends_on:
                    continue
                # "Custom functions in the Free plan" and "...in the Standard
                # plan" embed at 0.9+, yet are opposite questions. Claims with
                # different subjects are never one claim, however alike.
                if _subject_words(first) != _subject_words(other):
                    continue
                similarity = sum(x * y for x, y in zip(vectors[i], vectors[j]))
                if similarity >= DUPLICATE_SIMILARITY:
                    dropped.add(other.id)
                    first.importance = max(first.importance, other.importance)
                    first.subject = first.subject or other.subject
                    if other.origin == "stated":
                        # The user's words outlive the merge: the claim that
                        # checks a statement is the statement, still to verify.
                        first.origin, first.answer, first.answer_source = (
                            "stated", other.answer, other.answer_source,
                        )
                    yield self._note(f"{other.id}: says the same as {first.id}, merged")
        if dropped:
            self.run.claims = [c for c in self.run.claims if c.id not in dropped]

    # ------------------------------------------------------------------- drive

    def _drive(self) -> Iterator[Event]:
        """Search until nothing searchable is left, then ask or answer."""
        for _ in range(MAX_ROUNDS):
            self._check()
            self._update_blocked()
            targets = [
                c for c in self.run.claims
                if c.kind == "shared_knowledge" and c.state == "open"
            ]
            if not targets:
                break
            self.run.rounds += 1
            yield from self._resolve(targets)

        for c in self.run.claims:
            if c.state == "open":
                c.state = "exhausted"
        self._update_blocked()
        yield self._tree()

        askable = self._askable()
        if askable and self.run.ask_rounds < MAX_ASK_ROUNDS:
            yield from self._ask(askable)
            return
        yield from self._answer()

    def _resolve(self, targets: list[Claim]) -> Iterator[Event]:
        label = "Searching the knowledge base"
        yield self._begin("search", label if self.run.rounds == 1 else f"{label} again")
        batch: list[tuple[Claim, list[tools.Hit]]] = []
        for c in targets:
            self._check()
            query = c.query or c.text
            hits = tools.kb_search(query)
            c.queries_tried.append(query)
            c.budget_used += 1
            scores = [h.score for h in hits if h.score is not None]
            if scores:
                c.best_score = max(c.best_score, max(scores))
            # Every hit is a candidate related passage, ranked by relevance --
            # including those the subject filter below will keep from the judge.
            self._remember_related(c, hits)
            # A passage already judged for this claim gave its verdict; a
            # reworded search in a small knowledge base mostly finds it again.
            fresh = [h for h in hits if h.chunk_id not in c.seen]
            c.seen += [h.chunk_id for h in fresh]
            batch.append((c, fresh))
            if not hits:
                found = "nothing relevant"
            elif not fresh:
                found = "only passages already checked"
            else:
                found = f"{len(fresh)} new passage{'s' if len(fresh) != 1 else ''}"
            yield self._note(f'{c.id}: "{query}" found {found}')
            if hits and not fresh:
                # Rewording again would only find the same text once more.
                c.state = "exhausted"
        yield self._tree()

        # Only passages that name the claim's subject can settle it. When none
        # does, the judge is skipped outright: on a CPU it costs tens of
        # seconds, and its verdict would be rejected by _rejection anyway.
        with_hits = []
        for c, hits in batch:
            if not hits or c.state != "open":
                continue
            subjects = self._subjects(c)
            named = [
                h for h in hits
                if all(_mentions(f"{h.title} {h.text}", s) for s in subjects)
            ]
            if not named:
                yield self._note(f'{c.id}: no new passage mentions "{subjects[0]}", so none can settle it')
                continue
            with_hits.append((c, named[:JUDGE_HITS]))
        if with_hits:
            yield self._begin("check", "Checking what the passages actually say")
            yield from self._judge(with_hits)

        thin = [c for c, _ in batch if c.state == "open"]
        retry = [c for c in thin if c.budget_used < MAX_SEARCHES]
        for c in thin:
            if c not in retry:
                c.state = "exhausted"
                yield self._note(f"{c.id}: searched {c.budget_used} ways, nothing settles it")
        yield self._tree()

        if retry:
            yield self._begin("reword", "Rewording searches that came back empty")
            data = self._call(
                prompts.REFORMULATE_SYSTEM,
                _reformulate_user(retry),
                prompts.reformulate_schema([c.id for c in retry]),
                max_tokens=48 * len(retry) + 48,
            )
            by_id = {r["claim"]: r for r in data["rewrites"]}
            for c in retry:
                query = by_id.get(c.id, {}).get("query", "").strip()
                tried = {q.lower() for q in c.queries_tried}
                if not query or query.lower() in tried:
                    c.state = "exhausted"
                    yield self._note(f"{c.id}: no new wording left to try")
                else:
                    c.query = query
                    yield self._note(f'{c.id}: next, "{query}"')

    def _judge(self, batch: list[tuple[Claim, list[tools.Hit]]]) -> Iterator[Event]:
        data = self._call(
            prompts.JUDGE_SYSTEM,
            _judge_user(batch),
            prompts.judge_schema([c.id for c, _ in batch], max(len(h) for _, h in batch)),
            max_tokens=150 * len(batch) + 48,
        )
        by_id = {v["claim"]: v for v in data["verdicts"]}

        for c, hits in batch:
            verdict = by_id.get(c.id)
            n = verdict["passage"] if verdict else 0
            hit = hits[n - 1] if verdict and 1 <= n <= len(hits) else None

            if hit is None or verdict["stance"] == "neither":
                if verdict and hit and verdict["same_subject"] == "no":
                    about = _clip(verdict["about"], 50) or "something else"
                    yield self._note(f"{c.id}: closest passage is about {about}, not this")
                else:
                    yield self._note(f"{c.id}: the passages don't settle it")
                continue

            reason = self._rejection(c, hit, verdict["quote"])
            if reason:
                yield self._note(f"{c.id}: {verdict['stance']} not counted, {reason}")
                continue

            c.evidence.append(
                {
                    "stance": verdict["stance"],
                    "quote": verdict["quote"].strip(),
                    "chunk_id": hit.chunk_id,
                    "text": hit.text,
                    "source": hit.as_source(),
                }
            )
            c.state = "supported" if verdict["stance"] == "supports" else "refuted"
            yield self._note(f"{c.id}: {verdict['stance']} by {hit.citation}")

    def _rejection(self, claim: Claim, hit: tools.Hit, quote: str) -> str | None:
        """Why a verdict cannot be trusted, or None if it can.

        Two checks a small model cannot be relied on to make itself: that it
        quoted the passage rather than paraphrasing it into agreement, and
        that a claim about a particular plan or version is backed by a
        passage that names it.
        """
        if not _quoted(quote, hit.text):
            return "its quote is not in the passage"
        haystack = f"{hit.title} {hit.text}"
        for subject in self._subjects(claim):
            if not _mentions(haystack, subject):
                return f'the passage never mentions "{subject}"'
        return None

    def _subjects(self, claim: Claim) -> list[str]:
        """The words a passage must contain to say anything about this claim.

        From three places: the profile fact the claim depends on (the user's
        plan, "Free"), any known profile value the claim names outright
        ("features of the Pro plan"), and the claim's own subject with its
        generic words dropped ("Standard plan" -> "Standard"). A generic page
        about custom functions says nothing about which plan has them, and
        this is what stops it counting as if it did. Long values are skipped:
        a sentence cannot be looked for word for word.
        """
        values: list[str] = []
        dep = self.run.claim(claim.depends_on)
        if dep is not None and dep.state == "answered" and dep.answer:
            values.append(dep.answer)
        values += [v for v in self.profile.values() if _mentions(claim.text, v)]
        values += [w for w in _WORD.findall(claim.subject) if w.lower() not in _GENERIC]
        return [v for v in dict.fromkeys(values) if 0 < len(v.split()) <= 3]

    def _remember_related(self, claim: Claim, hits: list[tools.Hit]) -> None:
        seen = {r["chunk_id"] for r in claim.related}
        for h in hits:
            if h.chunk_id not in seen:
                claim.related.append(
                    {"chunk_id": h.chunk_id, "text": h.text, "score": h.score or 0.0, "source": h.as_source()}
                )
        claim.related.sort(key=lambda r: r["score"], reverse=True)
        del claim.related[MAX_RELATED:]

    def _update_blocked(self) -> None:
        """A claim waits while the claim it depends on is unsettled."""
        for c in self.run.claims:
            if c.kind != "shared_knowledge" or c.state not in ("open", "blocked"):
                continue
            dep = self.run.claim(c.depends_on)
            # "unknown" unblocks too: the user cannot fill the gap, so search
            # with what there is rather than wait for ever.
            waiting = dep is not None and not dep.settled and dep.state != "unknown"
            c.state = "blocked" if waiting else "open"

    # --------------------------------------------------------------------- ask

    def _askable(self) -> list[Claim]:
        claims = self.run.claims
        blockers = {c.depends_on for c in claims if c.state == "blocked"}
        candidates = [
            c
            for c in claims
            if c.state in ("unsearchable", "exhausted")
            and (c.importance >= 1.0 or c.id in blockers)
            # The user just said it; asking them to confirm their own words
            # back is no question at all.
            and c.origin != "stated"
        ]
        # A claim others wait on goes first: its answer can unlock searches,
        # so it is worth more than any single gap.
        candidates.sort(key=lambda c: (c.id not in blockers, -c.importance))
        return candidates[:MAX_QUESTIONS]

    def _ask(self, askable: list[Claim]) -> Iterator[Event]:
        yield self._begin("ask", "Preparing a question for you")
        data = self._call(
            prompts.ASK_SYSTEM,
            _ask_user(self.run, askable),
            prompts.ask_schema([c.id for c in askable]),
            max_tokens=140 * len(askable) + 48,
        )
        by_id = {q["claim"]: q for q in data["questions"]}

        questions = []
        for c in askable:
            q = by_id.get(c.id, {})
            options = [o.strip() for o in q.get("options", []) if o.strip()]
            if q.get("type") != "choice":
                options = []
            questions.append(
                {
                    "claim_id": c.id,
                    "kind": c.kind,
                    "text": q.get("text", "").strip() or f"Can you tell me: {c.text}?",
                    "type": "choice" if options else "text",
                    "options": options,
                    "why": q.get("why", "").strip(),
                    # What the search did find, so the user sees why it asks.
                    "related": [
                        {"citation": r["source"]["citation"], "snippet": _clip(r["text"], 180)}
                        for r in c.related[:1]
                    ],
                }
            )
            c.state = "asked"

        self.run.questions = questions
        self.run.ask_rounds += 1
        self.run.status = "awaiting"
        yield self._note(f"{len(questions)} question{'s' if len(questions) != 1 else ''} for you")
        yield self._tree()
        yield self._close()
        agent_store.save_run(self.run)
        yield {"type": "questions", "run_id": self.run.id, "questions": questions}

    # ------------------------------------------------------------------ answer

    def _answer(self) -> Iterator[Event]:
        yield self._begin("answer", "Writing the answer")
        sources: list[dict] = []
        passages: list[str] = []
        number: dict[int, int] = {}

        def cite(chunk_id: int, source: dict, text: str) -> int:
            if chunk_id not in number:
                number[chunk_id] = len(sources) + 1
                sources.append({"n": number[chunk_id], **source})
                passages.append(f"[{number[chunk_id]}] {source['citation']}\n{text}")
            return number[chunk_id]

        facts: list[str] = []
        gaps: list[str] = []
        stated: list[str] = []
        for c in self.run.claims:
            if c.origin == "stated" and c.kind == "shared_knowledge":
                # Reported by what was searched, never by the model's rewording
                # of the user's words: a small model can read "you said X, it
                # is wrong" as X itself, and repeating that back would tell the
                # user the opposite of what they said. The answer reads their
                # message directly instead (see ANSWER_SYSTEM).
                if c.state in ("supported", "refuted") and c.evidence:
                    ev = c.evidence[-1]
                    n = cite(ev["chunk_id"], ev["source"], ev["text"])
                    verdict = "the documents confirm" if c.state == "supported" else "the documents contradict"
                    stated.append(f'- Checked "{c.text}": {verdict} it [{n}], "{ev["quote"]}"')
                else:
                    related = ", ".join(
                        f"[{cite(r['chunk_id'], r['source'], r['text'])}]" for r in c.related
                    )
                    stated.append(
                        f'- Checked "{c.text}": the documents do not say'
                        + (f"; closest passages: {related}" if related else "")
                    )
                continue
            if c.state in ("supported", "refuted") and c.evidence:
                for ev in c.evidence:
                    cite(ev["chunk_id"], ev["source"], ev["text"])
                ev = c.evidence[-1]
                verb = "confirmed" if c.state == "supported" else "contradicted"
                facts.append(f'- {c.text}: {verb} by [{number[ev["chunk_id"]]}], "{ev["quote"]}"')
            elif c.state == "answered":
                if c.answer_source == "user":
                    # Labels read as the answer should speak: to "you".
                    who = "you told me"
                else:
                    who = "from your saved profile"
                facts.append(f"- {c.text}: {c.answer} ({who})")
            else:
                related = ", ".join(
                    f"[{cite(r['chunk_id'], r['source'], r['text'])}]" for r in c.related
                )
                gaps.append(f"- {c.text}" + (f" (related, but not settling: {related})" if related else ""))

        # A profile fact known before the run never became a claim -- the plan
        # was written straight into the claims ("features of the Pro plan").
        # The answer still has to know the user is on it, or it hedges about
        # whether the features apply to them.
        for key, value in self.profile.items():
            covered = any(c.kind == "profile_fact" and c.key == key for c in self.run.claims)
            if not covered and any(_mentions(c.text, value) for c in self.run.claims):
                facts.append(f"- Your {key.replace('_', ' ')}: {value} (from your saved profile)")

        # Said outright when there are no sources: told only "(none)", a small
        # model still decorates its answer with a [1] that points nowhere.
        source_block = "\n\n".join(passages) if passages else "(none, so do not use any [n] citation markers)"
        prompt = (
            f"Message: {self.run.question}\n\n"
            + (f"The message asserts something. What the documents say about it:\n{chr(10).join(stated)}\n\n" if stated else "")
            + f"Established:\n{chr(10).join(facts) or '- nothing'}\n\n"
            f"Not established:\n{chr(10).join(gaps) or '- nothing'}\n\n"
            f"Sources:\n{source_block}"
        )

        pieces: list[str] = []
        for kind, text in llm.stream_reply(
            history=[],
            user_message=prompt,
            system_prompt=prompts.ANSWER_SYSTEM,
            should_stop=self.should_stop,
        ):
            if kind == "token":
                pieces.append(text)
            yield {"type": kind, "text": text}
        self._check()

        yield {"type": "answer", "content": "".join(pieces).strip(), "sources": sources}

        try:
            yield from self._propose()
        except model.AgentError:
            # Losing a proposal must never cost the answer already given.
            logger.exception("Could not draft what to store for run %s", self.run.id)
            yield self._note("Could not draft a note to keep this time")

        yield self._close()
        self.run.status = "done"
        agent_store.save_run(self.run)

    def _propose(self) -> Iterator[Event]:
        """Turn what the user told us into proposals. Nothing is written here."""
        told = [
            c for c in self.run.claims
            if c.answer_source == "user" and (c.state == "answered" or c.origin == "stated")
        ]
        if not told:
            return

        items: list[dict] = [
            {"destination": "profile", "key": c.key, "value": c.answer}
            for c in told
            if c.kind == "profile_fact"
        ]

        knowledge = [c for c in told if c.kind == "shared_knowledge"]
        if knowledge:
            yield self._begin("store", "Drafting what to keep")
            data = self._call(
                prompts.NOTE_SYSTEM,
                _note_user(self.run, knowledge),
                prompts.note_schema([c.id for c in knowledge]),
                max_tokens=240 * len(knowledge) + 48,
            )
            by_id = {n["claim"]: n for n in data["notes"]}
            for c in knowledge:
                n = by_id.get(c.id, {})
                item = {
                    "destination": "knowledge",
                    "title": n.get("title", "").strip() or _clip(c.text, 80),
                    "note": n.get("note", "").strip() or f"{c.text}: {c.answer}",
                }
                if c.state == "refuted" and c.evidence:
                    # Still offered -- the documents may be the ones out of
                    # date -- but never quietly.
                    item["warning"] = (
                        f"{c.evidence[-1]['source']['citation']} says otherwise: "
                        f"“{_clip(c.evidence[-1]['quote'], 120)}”"
                    )
                items.append(item)

        saved = agent_store.add_proposals(self.run.id, self.run.session_id, items)
        yield self._note(f"{len(saved)} thing{'s' if len(saved) != 1 else ''} to keep, waiting for your approval")
        yield {"type": "proposals", "items": saved}

    # ------------------------------------------------------------------ steps

    def _begin(self, stage: str, label: str) -> Event:
        """Open a step, closing the one before. One event carries both."""
        closed = self._finish()
        self._step = {
            "id": len(self.run.steps) + 1,
            "stage": stage,
            "label": label,
            "status": "running",
            "ms": None,
            "details": [],
        }
        self.run.steps.append(self._step)
        self._step_t0 = time.monotonic()
        return {"type": "step", "step": dict(self._step), "closed": closed}

    def _note(self, text: str) -> Event:
        """Add a line to the running step; sent as the step, re-rendered whole."""
        if self._step is None:
            self._begin("work", "Working")
        self._step["details"].append(text)
        return {"type": "step", "step": dict(self._step, details=list(self._step["details"]))}

    def _close(self) -> Event:
        return {"type": "step", "step": None, "closed": self._finish()}

    def _finish(self) -> dict | None:
        if self._step is None or self._step["status"] != "running":
            return None
        self._step["status"] = "done"
        self._step["ms"] = int((time.monotonic() - self._step_t0) * 1000)
        return dict(self._step, details=list(self._step["details"]))

    def _on_token(self, count: int) -> None:
        """Called per token inside a model call; reports at most twice a second."""
        now = time.monotonic()
        if self._step is None or now - self._progress_at < PROGRESS_SECONDS:
            return
        self._progress_at = now
        self.emit(
            {
                "type": "progress",
                "step": self._step["id"],
                "tokens": count,
                "phase": "writing",
                "seconds": round(now - self._step_t0, 1),
            }
        )

    # ----------------------------------------------------------------- helpers

    def _call(self, system: str, user: str, schema: dict, max_tokens: int) -> dict:
        self._check()
        # Before the first token there is a silent phase: loading 2.7 GB of
        # weights on the very first call, then reading the prompt. Said
        # outright, so a clock with no token count is not mistaken for a hang.
        if self._step is not None:
            self.emit(
                {
                    "type": "progress",
                    "step": self._step["id"],
                    "tokens": 0,
                    "phase": "loading the model" if not llm.is_loaded() else "reading",
                    "seconds": round(time.monotonic() - self._step_t0, 1),
                }
            )
        try:
            return model.call_json(
                system,
                user,
                schema,
                max_tokens=max_tokens,
                should_stop=self.should_stop,
                on_token=self._on_token,
            )
        except llm.GenerationCancelled as exc:
            raise Cancelled() from exc

    def _check(self) -> None:
        if self.should_stop():
            raise Cancelled()

    def _tree(self) -> Event:
        return {"type": "tree", "claims": claims_view(self.run)}


def claims_view(run: Run) -> list[dict]:
    """The tree as the client draws it: quotes and sources, not whole passages."""
    claims = []
    for c in run.claims:
        view = asdict(c)
        view["evidence"] = [
            {"stance": e["stance"], "quote": e["quote"], "citation": e["source"]["citation"]}
            for e in c.evidence
        ]
        view["related"] = [
            {"citation": r["source"]["citation"], "snippet": _clip(r["text"], 160)}
            for r in c.related
        ]
        claims.append(view)
    return claims


def run_view(run: Run) -> dict:
    """Everything the client needs to redraw a run's reasoning after a reload."""
    return {"run_id": run.id, "status": run.status, "claims": claims_view(run), "steps": run.steps}


# ------------------------------------------------------------- text helpers


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^\w]+", " ", text.lower()).split())


def _quoted(quote: str, passage: str) -> bool:
    """True if the quote is really in the passage.

    Exact after normalising case, spacing and punctuation; otherwise nearly
    every word of it must appear, which forgives a dropped comma or an
    ellipsis but not a paraphrase that reads agreement into the passage.
    """
    q = _norm(quote)
    if not q:
        return False
    p = _norm(passage)
    if q in p:
        return True
    words = q.split()
    present = set(p.split())
    return len(words) >= 3 and sum(w in present for w in words) / len(words) >= 0.85


def _mentions(text: str, value: str) -> bool:
    """Whole-word, case-insensitive: "Pro" is in "Pro plan" but not in "Professional"."""
    value = value.strip()
    return bool(value) and re.search(rf"(?<!\w){re.escape(value)}(?!\w)", text, re.IGNORECASE) is not None


_WORD = re.compile(r"[\w'-]+")
# Words that name a kind of thing rather than which one: "Standard plan" is
# about Standard, and a passage need not say "plan" to be about it.
_GENERIC = frozenset(
    """a an the of for and or in on to with plan plans tier tiers edition editions
    version versions product products package user user's users your my their
    account subscription feature features""".split()
)


def _subject_words(c: Claim) -> frozenset[str]:
    """A claim's distinctive subject words, lower-cased, for comparing claims."""
    return frozenset(w.lower() for w in _WORD.findall(c.subject) if w.lower() not in _GENERIC)


def _describe(c: Claim) -> str:
    if c.origin == "stated":
        if c.kind == "profile_fact":
            return f'{c.id}: you stated {c.key} = "{_clip(c.answer or "", 60)}"'
        return f'{c.id}: you stated "{_clip(c.answer or c.text, 90)}", checking it against the documents'
    if c.kind == "profile_fact":
        if c.state == "answered":
            return f"{c.id}: {c.text}, already known ({c.key} = {c.answer})"
        return f"{c.id}: {c.text}, only you can know this"
    after = f", once {c.depends_on} is known" if c.depends_on else ""
    return f"{c.id}: {c.text}, to search{after}"


# First-person phrasing is the strongest single sign that an answer depends on
# who is asking. It is passed to the model as a hint, not applied as a rule:
# "what does error E42 mean for me" is still the same for everyone.
_FIRST_PERSON = re.compile(r"\b(i|i'm|i've|me|my|mine|myself)\b", re.IGNORECASE)


def _decompose_user(question: str, history: list[dict], profile: dict[str, str]) -> str:
    known = ", ".join(f"{k} = {v}" for k, v in profile.items()) or "nothing yet"
    recent = "\n".join(
        f"{m['role']}: {_clip(m['content'], 200)}" for m in history[-4:]
    ) or "(none)"
    hint = (
        "\nNote: the message is about the user personally, so check "
        "whether the answer depends on their plan, role or setup."
        if _FIRST_PERSON.search(question)
        else ""
    )
    return (
        f"Examples\n{prompts.DECOMPOSE_EXAMPLE}\n\n"
        f"Known about this user: {known}\n\n"
        f"Recent conversation:\n{recent}\n\n"
        f"Message: {question}{hint}"
    )


_SENTENCE = re.compile(r"(?<=[.!?\n])\s+")
_TERM = re.compile(r"[a-z0-9]{3,}")
FOCUS_CHARS = 320


def _focus(text: str, terms: set[str], limit: int = FOCUS_CHARS) -> str:
    """The stretch of a passage that bears on the claim, up to `limit` chars.

    The judge reads every character it is shown, at a few dozen tokens a
    second, so it is shown the sentences around the claim's own words rather
    than 500 characters of page. The sentence matching the most terms anchors
    the window, which then grows to whichever side matches better.
    """
    if len(text) <= limit:
        return text
    sentences = [s for s in _SENTENCE.split(text) if s.strip()]
    if len(sentences) < 2:
        return text[:limit]
    hits = [len(terms & set(_TERM.findall(s.lower()))) for s in sentences]
    lo = hi = max(range(len(sentences)), key=lambda i: hits[i])
    size = len(sentences[lo])
    while True:
        left = hits[lo - 1] if lo > 0 else -1
        right = hits[hi + 1] if hi + 1 < len(sentences) else -1
        if left < 0 and right < 0:
            break
        nxt = lo - 1 if left > right else hi + 1
        if size + len(sentences[nxt]) + 1 > limit:
            break
        size += len(sentences[nxt]) + 1
        lo, hi = min(lo, nxt), max(hi, nxt)
    return " ".join(sentences[lo : hi + 1])[:limit]


def _judge_user(batch: list[tuple[Claim, list[tools.Hit]]]) -> str:
    blocks = []
    for c, hits in batch:
        terms = set(_TERM.findall(f"{c.text} {c.query}".lower()))
        lines = [f"Claim {c.id}: {c.text}", "Passages:"]
        lines += [
            f"[{i}] ({h.citation}) {_focus(h.text, terms)}" for i, h in enumerate(hits, start=1)
        ]
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def _reformulate_user(claims: list[Claim]) -> str:
    return "\n\n".join(
        f"Claim {c.id}: {c.text}\nAlready tried: "
        + "; ".join(f'"{q}"' for q in c.queries_tried)
        for c in claims
    )


def _known(run: Run) -> str:
    lines = [f"- {c.text}: {c.answer}" for c in run.claims if c.state == "answered"]
    return "\n".join(lines) or "- nothing yet"


def _ask_user(run: Run, askable: list[Claim]) -> str:
    blockers = {c.depends_on for c in run.claims if c.state == "blocked"}
    lines = []
    for c in askable:
        why = (
            "only the user can know this"
            if c.kind == "profile_fact"
            else "no document in the knowledge base settles this"
        )
        if c.id in blockers:
            why += "; other claims cannot be searched until it is known"
        line = f"Claim {c.id}: {c.text} ({why})"
        if c.related:
            r = c.related[0]
            line += f'\n  Closest passage found ({r["source"]["citation"]}): {_clip(r["text"], 240)}'
        lines.append(line)
    return (
        f"The user asked: {run.question}\n\n"
        f"Already known:\n{_known(run)}\n\n"
        "Write a question for each claim:\n" + "\n".join(lines)
    )


def _integrate_user(run: Run, claims: list[Claim]) -> str:
    return (
        f"The user asked: {run.question}\n\n"
        f"The user's answers and other known facts:\n{_known(run)}\n\n"
        "Claims to rewrite:\n"
        + "\n".join(f"Claim {c.id}: {c.text}" for c in claims)
    )


def _note_user(run: Run, claims: list[Claim]) -> str:
    blocks = []
    for c in claims:
        if c.origin == "stated":
            # The user's own words, not a rewording of them, plus what the
            # documents said: the note writer reads the correction itself.
            block = f"Claim {c.id}: {c.text}\nThe user's own words: {run.question}"
            if c.evidence:
                block += f'\nThe documents say: "{c.evidence[-1]["quote"]}"'
            elif c.related:
                block += f'\nThe closest document passage: "{_clip(c.related[0]["text"], 200)}"'
        else:
            block = f"Claim {c.id}: {c.text}\nThe user said: {c.answer}"
        blocks.append(block)
    return f"Context:\n{_known(run)}\n\n" + "\n\n".join(blocks)
