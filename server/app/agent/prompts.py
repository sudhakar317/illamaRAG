"""System prompts and output schemas for each agent step.

Schemas that mention claims are built per call with an `enum` of the claim ids
actually in play, so the grammar cannot produce a verdict for a claim that does
not exist, and min/max item counts force exactly one entry per claim. Field
order matters: the grammar emits properties in the order given, so each
object states its content before classifying it.
"""

from __future__ import annotations

# ------------------------------------------------------------------ decompose

# The plan opens with what the user *said*, then one forced decision,
# depends_on_user, and the grammar holds the rest to it: after "yes" at least
# one user fact must follow, after "no" none may. A small model left to fill
# a free list both ways inconsistently -- listing no user fact for "what
# features do I have", or filing the features themselves as a fact about the
# user. Deciding first, in one token, and naming the attribute by a one-word
# key before any prose, is what keeps it straight.
#
# stated_facts exists because a user often tells the agent something rather
# than asking: "custom functions are in the Standard plan, not Free". Without
# it a correction is treated as one more question and is never kept.
DECOMPOSE_SYSTEM = """You plan how to answer a user's message. Do not answer it.

stated_facts: facts the user states as true in the message, such as a correction or something about themselves. [] if they only ask.
- A correction says what is true. "You said X, that is wrong" states that X is false, so write the opposite of X, not X.
- about: user for a fact about themselves (key names the attribute, value is its value); world for anything else (key is "", value is the fact as one full standalone sentence).
depends_on_user: yes if the right answer differs from one user to another (it depends on their plan, role, version, device or setup), otherwise no.
user_facts: when yes, the single attribute of this user the answer depends on most, answerable in a word or two. key is its snake_case name, such as plan, role, app_version or operating_system. Never put the thing the question asks about here.
claims: what a document must state to answer, as short statements, never questions.
- subject: the specific plan, product, version or item the claim is about, such as "Team plan", or "" if it is general.
- uses_user_fact: the index of the user fact the claim depends on, or -1.
- importance: high if the question cannot be answered without it, otherwise low.
- query: a short search query in the words a document would use.

Use one or two claims, and never two that ask the same thing. If the message only states something, make one claim that checks it against the documents. If a fact about the user is already known, use its value in the claims and do not list it again."""

DECOMPOSE_EXAMPLE = (
    'Message: How much storage do I get?\n'
    '{"stated_facts":[],"depends_on_user":"yes",'
    '"user_facts":[{"key":"plan","text":"The plan the user is on"}],'
    '"claims":[{"text":"The storage limit of the user\'s plan","subject":"",'
    '"uses_user_fact":0,"importance":"high","query":"storage limit per plan"}]}\n\n'
    'Message: What does error E42 mean?\n'
    '{"stated_facts":[],"depends_on_user":"no","user_facts":[],'
    '"claims":[{"text":"The meaning of error code E42","subject":"error E42",'
    '"uses_user_fact":-1,"importance":"high","query":"error E42 meaning cause"}]}\n\n'
    'Message: Exports supported in team plan but you tell is available in basic plan, it is wrong. Which plan has webhooks?\n'
    '{"stated_facts":[{"about":"world","key":"","value":"Exports are included in the Team plan, not in the Basic plan."}],'
    '"depends_on_user":"no","user_facts":[],'
    '"claims":[{"text":"The plans that include webhooks","subject":"",'
    '"uses_user_fact":-1,"importance":"high","query":"webhooks plan availability"}]}\n\n'
    'Message: I am on the Team plan. Can I use webhooks?\n'
    '{"stated_facts":[{"about":"user","key":"plan","value":"Team"}],"depends_on_user":"yes",'
    '"user_facts":[{"key":"plan","text":"The plan the user is on"}],'
    '"claims":[{"text":"Whether the Team plan includes webhooks","subject":"Team plan",'
    '"uses_user_fact":0,"importance":"high","query":"Team plan webhooks"}]}'
)

_STATED = {
    "type": "array",
    "maxItems": 2,
    "items": {
        "type": "object",
        "properties": {
            "about": {"enum": ["user", "world"]},
            "key": {"type": "string", "maxLength": 30},
            "value": {"type": "string", "maxLength": 240},
        },
        "required": ["about", "key", "value"],
    },
}

_USER_FACT = {
    "type": "object",
    "properties": {
        "key": {"type": "string", "maxLength": 30},
        "text": {"type": "string", "maxLength": 120},
    },
    "required": ["key", "text"],
}

_CLAIMS = {
    "type": "array",
    "minItems": 1,
    "maxItems": 2,
    "items": {
        "type": "object",
        "properties": {
            "text": {"type": "string", "maxLength": 160},
            "subject": {"type": "string", "maxLength": 40},
            "uses_user_fact": {"enum": [-1, 0]},
            "importance": {"enum": ["high", "low"]},
            "query": {"type": "string", "maxLength": 120},
        },
        "required": ["text", "subject", "uses_user_fact", "importance", "query"],
    },
}


def _plan(depends: str, facts: dict) -> dict:
    return {
        "type": "object",
        "properties": {
            "stated_facts": _STATED,
            "depends_on_user": {"const": depends},
            "user_facts": facts,
            "claims": _CLAIMS,
        },
        "required": ["stated_facts", "depends_on_user", "user_facts", "claims"],
    }


DECOMPOSE_SCHEMA = {
    "anyOf": [
        _plan("yes", {"type": "array", "minItems": 1, "maxItems": 1, "items": _USER_FACT}),
        _plan("no", {"type": "array", "maxItems": 0, "items": _USER_FACT}),
    ]
}

# ---------------------------------------------------------------------- judge

# The judge names what a passage is about, then decides whether that is the
# claim's subject, and only then takes a stance -- and the grammar forbids any
# stance but "neither" after same_subject "no". Without this a small model
# reads "Standard plan features: ... Custom functions" as proof that the Free
# plan has custom functions, because the right words are there. Code checks
# the verdict again afterwards (see tree._rejection).
JUDGE_SYSTEM = """You check whether a passage from the documents settles a claim. Be strict.

For each claim, pick the one passage that bears on it most, then fill in:
about: in a few words, the subject that passage describes, such as which plan, product, version or feature.
same_subject: yes only if that subject is the one the claim is about. A passage about the Standard plan is not about the Free plan, and a general description of a feature does not say which plans include it.
stance: supports if the passage states the claim is true of that subject; refutes if it states the opposite; neither otherwise.
quote: the exact words copied from the passage that show it, at most 25 words, or "" for neither.
A passage that merely mentions the right words does not support a claim."""


def judge_schema(claim_ids: list[str], max_passage: int) -> dict:
    def branch(same: str, stances: list[str]) -> dict:
        props = {
            "claim": {"enum": claim_ids},
            "passage": {"enum": list(range(0, max_passage + 1))},
            "about": {"type": "string", "maxLength": 60},
            "same_subject": {"const": same},
            "stance": {"enum": stances},
            "quote": {"type": "string", "maxLength": 200},
        }
        return {"type": "object", "properties": props, "required": list(props)}

    return {
        "type": "object",
        "properties": {
            "verdicts": {
                "type": "array",
                "minItems": len(claim_ids),
                "maxItems": len(claim_ids),
                "items": {
                    "anyOf": [
                        branch("yes", ["supports", "refutes", "neither"]),
                        branch("no", ["neither"]),
                    ]
                },
            }
        },
        "required": ["verdicts"],
    }


# ---------------------------------------------------------------- reformulate

REFORMULATE_SYSTEM = """A search for each claim below found nothing useful. Write one new search query for each.
Use the words a document or manual would use, not the user's words. Take a different angle from every query already tried. Keep each under ten words."""


def reformulate_schema(claim_ids: list[str]) -> dict:
    return _per_claim("rewrites", claim_ids, {"query": {"type": "string", "maxLength": 100}})


# ------------------------------------------------------------------------ ask

ASK_SYSTEM = """You write one question for the user per claim, to fill a gap the documents could not.
text: one short, plain question.
type: choice only if the conversation itself names the possible answers; otherwise text. Never invent options.
options: the possible answers for a choice question, otherwise [].
why: one short sentence on what the answer decides.
For a fact a document should have held, ask the user to state it or to upload a document that does.
When a nearby passage was found, use it to make the question specific, for example: "The documents list custom functions under the Standard plan. Does the Free plan include them too?"
"""


def ask_schema(claim_ids: list[str]) -> dict:
    return _per_claim(
        "questions",
        claim_ids,
        {
            "text": {"type": "string", "maxLength": 160},
            "type": {"enum": ["choice", "text"]},
            "options": {
                "type": "array",
                "maxItems": 5,
                "items": {"type": "string", "maxLength": 40},
            },
            "why": {"type": "string", "maxLength": 140},
        },
    )


# ------------------------------------------------------------------ integrate

INTEGRATE_SYSTEM = """The user has just answered some questions. Rewrite each claim listed below so it uses those answers: replace vague references such as "that plan" with the actual value. Then write a search query for it in the words a document would use."""


def integrate_schema(claim_ids: list[str]) -> dict:
    return _per_claim(
        "claims",
        claim_ids,
        {
            "text": {"type": "string", "maxLength": 160},
            "query": {"type": "string", "maxLength": 100},
        },
    )


# ----------------------------------------------------------------------- note

NOTE_SYSTEM = """Turn what the user said into a short standalone note for a knowledge base. It will be read months later with no conversation around it, so name the subject in full, keep only facts the user gave, and add nothing of your own.
A correction states what is true: "you said X, it is wrong" means X is false, so the note says the opposite of X. Where the documents are given, make the note agree with what they say.
title: a few words naming the subject."""


def note_schema(claim_ids: list[str]) -> dict:
    return _per_claim(
        "notes",
        claim_ids,
        {
            "title": {"type": "string", "maxLength": 80},
            "note": {"type": "string", "maxLength": 800},
        },
    )


# --------------------------------------------------------------------- answer

ANSWER_SYSTEM = """You answer the user's message from the established facts and sources given, and nothing else.
Speak to the user directly as "you". Never refer to them as "the user".
Cite document sources inline by number, like [1].
Say "as you told me" only for facts marked "you told me". Facts from the user's saved profile are simply true of them; use them without comment.
When the message asserts something, read the message itself to see what the user claims: "you said X, it is wrong" means the user says X is false. Then say whether the documents agree, using what they say about it. Never call the user right or wrong unless the documents settle it, and cite them when they do.
If something was not established, say in plain words that the documents do not say it. You may then say what a related passage states, cited, but never present it as applying to the user's case.
Do not guess, and do not fill gaps from general knowledge. Be concise and direct."""


# --------------------------------------------------------------------- helper


def _per_claim(name: str, claim_ids: list[str], fields: dict) -> dict:
    """An array with exactly one object per claim, each naming its claim first."""
    return {
        "type": "object",
        "properties": {
            name: {
                "type": "array",
                "minItems": len(claim_ids),
                "maxItems": len(claim_ids),
                "items": {
                    "type": "object",
                    "properties": {"claim": {"enum": claim_ids}, **fields},
                    "required": ["claim", *fields],
                },
            }
        },
        "required": [name],
    }
