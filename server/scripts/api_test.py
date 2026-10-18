"""End-to-end API check against a running server.

Start the server first, then:
    .venv/Scripts/python.exe scripts/api_test.py

Exercises the behaviours that are easy to get subtly wrong: that history is fed
back into the prompt, that sessions are genuinely isolated from each other, and
that deleting a session takes its messages with it.
"""

import json
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8000"


def call(method: str, path: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def ask(message: str, session_id: str | None = None) -> tuple[str, str]:
    """Send a message, consume the SSE stream, return (session_id, answer)."""
    body = json.dumps({"message": message, "session_id": session_id}).encode()
    req = urllib.request.Request(
        BASE + "/api/chat/stream",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )

    answer: list[str] = []
    resolved = session_id or ""
    buffer = ""

    with urllib.request.urlopen(req, timeout=600) as resp:
        while True:
            chunk = resp.read1(1024)
            if not chunk:
                break
            buffer += chunk.decode("utf-8", errors="replace")
            # Frames are separated by a blank line; a read can land mid-frame.
            while "\n\n" in buffer:
                frame, buffer = buffer.split("\n\n", 1)
                if not frame.startswith("data: "):
                    continue
                event = json.loads(frame[len("data: ") :])
                kind = event.get("type")
                if kind == "session":
                    resolved = event["session_id"]
                elif kind == "token":
                    answer.append(event["text"])
                    print(event["text"], end="", flush=True)
                elif kind == "error":
                    print(f"\n  !! server error: {event['message']}")

    return resolved, "".join(answer).strip()


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{(' - ' + detail) if detail else ''}")
    return ok


def main() -> int:
    results = []

    print("\n1. health")
    status, health = call("GET", "/api/health")
    results.append(check("health returns 200", status == 200))
    results.append(check("model file present", health["model"]["exists"]))

    print("\n2. create session")
    status, session = call("POST", "/api/sessions", {})
    sid = session["id"]
    results.append(check("session created", status == 201, sid[:8]))

    print("\n3. remembers earlier turns within a session")
    print("   > My name is Sudhakar. Remember it.\n   < ", end="")
    _, _ = ask("My name is Sudhakar. Remember it.", sid)
    print("\n   > What is my name? Reply with just the name.\n   < ", end="")
    _, answer = ask("What is my name? Reply with just the name.", sid)
    results.append(
        check("recalls the name from history", "sudhakar" in answer.lower(), repr(answer[:60]))
    )

    print("\n4. sessions are isolated")
    print("   > (new session) What is my name?\n   < ", end="")
    other_sid, answer2 = ask("What is my name? If you do not know, say you do not know.")
    results.append(
        check(
            "new session does NOT know the name",
            "sudhakar" not in answer2.lower(),
            repr(answer2[:60]),
        )
    )

    print("\n5. transcript persists and is titled")
    status, messages = call("GET", f"/api/sessions/{sid}/messages")
    roles = [m["role"] for m in messages]
    results.append(check("4 messages stored", len(messages) == 4, str(roles)))
    results.append(
        check("alternates user/assistant", roles == ["user", "assistant", "user", "assistant"])
    )
    _, s = call("GET", f"/api/sessions/{sid}")
    results.append(check("auto-titled from first message", s["title"] != "New chat", s["title"]))

    print("\n6. rename")
    status, renamed = call("PATCH", f"/api/sessions/{sid}", {"title": "Renamed chat"})
    results.append(check("rename works", renamed["title"] == "Renamed chat"))

    print("\n7. list")
    status, sessions = call("GET", "/api/sessions")
    results.append(check("both sessions listed", len(sessions) >= 2, f"{len(sessions)} sessions"))

    print("\n8. delete cascades")
    status, _ = call("DELETE", f"/api/sessions/{other_sid}")
    results.append(check("delete returns 204", status == 204))
    status, _ = call("GET", f"/api/sessions/{other_sid}/messages")
    results.append(check("deleted session is gone", status == 404))

    print("\n" + "=" * 54)
    passed, total = sum(results), len(results)
    print(f"  {passed}/{total} checks passed")
    print("=" * 54)
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
