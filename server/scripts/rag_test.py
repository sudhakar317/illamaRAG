"""End-to-end check of the knowledge base, against a running server.

    .venv/Scripts/python.exe scripts/rag_test.py

Every fixture below states invented facts -- a made-up protocol, an invented
part number, an odd refund window. That is deliberate: if the model answers
them correctly it can only be because retrieval worked, since no amount of
pretraining could have taught it. A test built on real facts would pass even
with retrieval completely broken.
"""

import json
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

BASE = "http://127.0.0.1:8000"

# Try a URL too, but do not fail the suite when the network is unavailable.
TEST_URL = "https://example.com"

TXT = """Zyntharion Logistics - Internal Operations Handbook

Section 1: Returns
Customers may return any Zyntharion product within 17 days of delivery.
Returns after 17 days are refused unless the regional manager signs a
Form 22-B exemption.

Section 2: Spare Parts
The replacement thermal coupler for the Mark IV unit carries part number
QX-4471-B. It must never be fitted to a Mark III chassis, because the
mounting pitch differs by 3.2 millimetres.

Section 3: Escalation
Any incident lasting more than 45 minutes is escalated to the Hollow Pine
duty engineer, who is reachable on extension 8812.
"""

MD = """# Varnholt Coffee Roasters - Staff Guide

## Grind Settings
The Varnholt house blend is ground at setting 14 on the Mazzer grinder.
Setting 11 is reserved for the decaf blend only.

## Water
Brew water is held at 94 degrees Celsius. The filter cartridge is replaced
every 1,400 litres, tracked on the counter log.

## Closing
The last pour is at 17:45. Grinders are purged nightly with 20 grams of
sacrificial beans.
"""

HTML = """<!doctype html>
<html><head><title>Brindlewick Observatory Visitor Notes</title></head>
<body>
<nav><a href="/">Home</a><a href="/tickets">Tickets</a></nav>
<article>
<h1>Brindlewick Observatory</h1>
<p>The Brindlewick refractor has an aperture of 712 millimetres and was
installed in 1974 by the engineer Aldous Finnerty.</p>
<p>Public viewing nights run on the second Thursday of each month, weather
permitting. The dome is closed entirely during the month of August for
recalibration.</p>
</article>
<footer>Copyright Brindlewick Trust. Cookie notice: we use cookies.</footer>
</body></html>
"""

PDF_PAGES = [
    "Tarnhelm Rail Authority - Signalling Manual\n\n"
    "Chapter 1. Overview\n\n"
    "The Tarnhelm network uses the Kestrel-7 interlocking system across all "
    "fourteen junctions. Kestrel-7 replaced the older Merrow system in 2011.",
    "Chapter 2. Fault Codes\n\n"
    "Fault code E-903 indicates a track circuit failure between posts 40 and "
    "41. The prescribed response is to impose a 20 km/h speed restriction and "
    "dispatch a technician within 90 minutes.",
]


def call(method, path, body=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try:
            return e.code, json.loads(body)
        except ValueError:
            return e.code, body


def upload(path: Path):
    """Multipart upload, hand-rolled to keep the script dependency-free."""
    boundary = f"----illama{uuid.uuid4().hex}"
    body = b"".join([
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="file"; filename="{path.name}"\r\n'.encode(),
        b"Content-Type: application/octet-stream\r\n\r\n",
        path.read_bytes(),
        f"\r\n--{boundary}--\r\n".encode(),
    ])
    req = urllib.request.Request(
        BASE + "/api/documents", data=body, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def wait_indexed(document_id, timeout=300):
    deadline = time.time() + timeout
    while time.time() < deadline:
        _, doc = call("GET", f"/api/documents/{document_id}")
        if isinstance(doc, dict) and doc.get("status") in ("indexed", "failed"):
            return doc
        time.sleep(1.5)
    return {"status": "timeout"}


def ask(message, session_id=None, use_rag=True):
    body = json.dumps(
        {"message": message, "session_id": session_id, "use_rag": use_rag}
    ).encode()
    req = urllib.request.Request(
        BASE + "/api/chat/stream", data=body, method="POST",
        headers={"Content-Type": "application/json"},
    )
    answer, sources, sid, buffer = [], [], session_id, ""
    with urllib.request.urlopen(req, timeout=900) as resp:
        while True:
            chunk = resp.read1(1024)
            if not chunk:
                break
            buffer += chunk.decode("utf-8", errors="replace")
            while "\n\n" in buffer:
                frame, buffer = buffer.split("\n\n", 1)
                if not frame.startswith("data: "):
                    continue
                event = json.loads(frame[6:])
                if event["type"] == "token":
                    answer.append(event["text"])
                elif event["type"] == "sources":
                    sources = event["sources"]
                elif event["type"] == "session":
                    sid = event["session_id"]
    return sid, "".join(answer).strip(), sources


results = []


def check(label, ok, detail=""):
    results.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{(' - ' + detail) if detail else ''}")
    return ok


def main():
    tmp = Path(tempfile.mkdtemp(prefix="illamarag_"))
    print(f"fixtures in {tmp}\n")

    # ---------------------------------------------------------- ingestion
    print("1. ingest all four source types")
    files = {
        "zyntharion_handbook.txt": TXT,
        "varnholt_guide.md": MD,
        "brindlewick.html": HTML,
    }
    doc_ids = {}
    for name, content in files.items():
        path = tmp / name
        path.write_text(content, encoding="utf-8")
        status, doc = upload(path)
        if not check(f"upload {name}", status == 201, str(doc)[:70]):
            continue
        doc_ids[name] = doc["id"]

    # PDF, generated with PyMuPDF so the suite needs no checked-in binary.
    try:
        import fitz

        pdf_path = tmp / "tarnhelm_manual.pdf"
        pdf = fitz.open()
        for text in PDF_PAGES:
            page = pdf.new_page()
            page.insert_textbox(fitz.Rect(60, 60, 540, 760), text, fontsize=11)
        pdf.save(pdf_path)
        pdf.close()
        status, doc = upload(pdf_path)
        if check("upload tarnhelm_manual.pdf", status == 201, str(doc)[:70]):
            doc_ids["tarnhelm_manual.pdf"] = doc["id"]
    except ImportError:
        print("  [SKIP] PDF test (PyMuPDF unavailable)")

    print("\n2. indexing completes")
    for name, doc_id in list(doc_ids.items()):
        doc = wait_indexed(doc_id)
        ok = doc.get("status") == "indexed" and doc.get("chunk_count", 0) > 0
        check(f"{name} indexed", ok, f"{doc.get('status')}, {doc.get('chunk_count')} chunks")

    print("\n3. ingest a URL")
    status, doc = call("POST", "/api/documents/url", {"url": TEST_URL}, timeout=90)
    if status == 201:
        doc = wait_indexed(doc["id"])
        check("URL indexed", doc.get("status") == "indexed", str(doc.get("status")))
        doc_ids["url"] = doc["id"]
    else:
        print(f"  [SKIP] URL ingestion unavailable ({str(doc)[:60]})")

    print("\n4. private addresses are refused (SSRF guard)")
    status, doc = call("POST", "/api/documents/url", {"url": "http://127.0.0.1:8000/"})
    check("localhost URL rejected", status == 400, f"HTTP {status}")

    print("\n5. retrieval scores (no generation)")
    status, out = call("POST", "/api/documents/search",
                       {"query": "how many days do I have to return a product?"})
    top = out["results"][0] if out.get("results") else {}
    check("semantic match finds the returns section",
          "17 days" in top.get("preview", ""), top.get("citation", "none"))

    # The case pure vector search handles badly, and BM25 handles well.
    status, out = call("POST", "/api/documents/search", {"query": "QX-4471-B"})
    found = any("QX-4471-B" in r["preview"] for r in out.get("results", []))
    check("exact part number found (keyword side of the hybrid)", found,
          f"{out.get('count', 0)} results")

    print("\n6. grounded answers")
    sid, answer, sources = ask(
        "According to the handbook, how many days do customers have to return a product?"
    )
    check("answers 17 days from the document", "17" in answer, repr(answer[:90]))
    check("cites a source", len(sources) > 0,
          sources[0]["citation"] if sources else "none")

    _, answer, sources = ask("What grind setting is used for the house blend?")
    check("answers grind setting 14", "14" in answer, repr(answer[:90]))

    _, answer, sources = ask("What is the aperture of the Brindlewick refractor?")
    check("answers 712 mm from the HTML page", "712" in answer, repr(answer[:90]))

    if "tarnhelm_manual.pdf" in doc_ids:
        _, answer, sources = ask("What does fault code E-903 mean?")
        check("answers from the PDF", "track circuit" in answer.lower(),
              repr(answer[:90]))
        check("PDF citation names a page",
              any(s.get("page") for s in sources),
              str([s["citation"] for s in sources][:2]))

    print("\n7. refuses to invent what is not there")
    _, answer, sources = ask(
        "What is the Zyntharion warranty period for underwater use?"
    )
    lowered = answer.lower()
    admits = any(
        phrase in lowered
        for phrase in ("not", "no ", "does not", "doesn't", "cannot", "unable", "no mention")
    )
    check("says the documents do not cover it", admits, repr(answer[:110]))

    print("\n8. deleting removes the knowledge")
    victim = doc_ids.get("zyntharion_handbook.txt")
    if victim:
        status, _ = call("DELETE", f"/api/documents/{victim}")
        check("delete returns 204", status == 204)
        status, out = call("POST", "/api/documents/search", {"query": "QX-4471-B"})
        gone = not any("QX-4471-B" in r["preview"] for r in out.get("results", []))
        check("its chunks are no longer searchable", gone,
              f"{out.get('count', 0)} results remain")

    print("\n9. cleanup")
    for name, doc_id in doc_ids.items():
        if doc_id != victim:
            call("DELETE", f"/api/documents/{doc_id}")
    _, health = call("GET", "/api/health")
    check("knowledge base is empty again",
          health["knowledge"]["chunks"] == 0,
          f"{health['knowledge']['chunks']} chunks")

    print("\n" + "=" * 56)
    passed, total = sum(results), len(results)
    print(f"  {passed}/{total} checks passed")
    print("=" * 56)
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
