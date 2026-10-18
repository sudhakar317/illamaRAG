# illamaRAG — local chatbot with persistent chats and sessions

A small, fully local chat application. A Qwen3.5-4B GGUF model runs on your own
CPU through llama.cpp — no Ollama, no API keys, nothing leaves the machine.
Conversations and sessions are stored in SQLite, so they survive a restart.

You can add your own PDFs, text files, HTML files and web links; answers are
then grounded in them and cite their sources.

```
┌─────────────────┐        SSE         ┌──────────────────┐
│  React + Vite   │ ◄───────────────── │  FastAPI server  │
│  (client :5173) │ ──── fetch ──────► │  (server :8000)  │
└─────────────────┘                    └────────┬─────────┘
                                                │
                                   ┌────────────┴────────────┐
                                   │                         │
                            ┌──────▼──────┐          ┌───────▼───────┐
                            │  llama.cpp  │          │    SQLite     │
                            │ Qwen3.5-4B  │          │ chats + msgs  │
                            └─────────────┘          └───────────────┘
```

## What works

- Streaming replies, token by token
- Multiple chat sessions, each with its own independent history
- Sessions auto-titled from the first message; rename, delete, clear
- Everything persisted to SQLite and restored on reload
- **Stop** genuinely cancels generation and keeps the partial reply
- Live model and memory status in the header
- Reasoning blocks separated from the answer and shown collapsed
- **Knowledge base**: add PDFs, TXT/Markdown, HTML files and URLs
- **Hybrid retrieval**: vector search and BM25 keyword search, fused
- **Citations**: inline markers plus source chips, persisted with the message
- Model replies rendered as formatted text rather than raw Markdown

## Requirements

- Python 3.11, Node 20+
- A Qwen GGUF chat model at `models/Qwen3.5-4B-Q4_K_M.gguf`
- An embedding model at `models/nomic-embed-text-v1.5.Q8_0.gguf` (139 MB)
- About 3.5 GB of free RAM while running

## Quick start

With the models in `models/` (see below), one command sets up everything that
is missing and runs both halves:

```bash
bash run.sh
```

On first run it creates `server/.venv`, installs the Python requirements (and
tries the prebuilt llama-cpp-python wheel if it is not already installed),
and runs `npm install` in `client/`. Later runs skip whatever is already done
and only reinstall when `requirements.txt` or `package-lock.json` changes.
It then starts the server, waits for it, starts the client, and prints the URL.
Ctrl+C stops both.

On Windows, from PowerShell or cmd, or by double-clicking it in Explorer:

```powershell
.\run.cmd
```

`run.cmd` finds Git Bash and runs `run.sh` through it. Typing plain `bash` in
PowerShell usually starts WSL (`C:\Windows\system32\bash.exe`), not Git Bash.
`run.sh` detects this and hands over to Git Bash itself, since the Windows
venv and `node_modules` cannot run under Linux.

`--setup` (for either script) does the setup only. `SERVER_PORT` and
`CLIENT_PORT` override the default ports.

The sections below cover the same steps by hand.

## Setup

### 1. The model

Place your chat GGUF at `models/Qwen3.5-4B-Q4_K_M.gguf`. The `models/` folder is
gitignored — multi-GB weights do not belong in git. To use a different path or
file, set `MODEL_PATH` in `server/.env`.

For retrieval you also need the embedding model (139 MB):

```bash
curl -L -o models/nomic-embed-text-v1.5.Q8_0.gguf https://huggingface.co/nomic-ai/nomic-embed-text-v1.5-GGUF/resolve/main/nomic-embed-text-v1.5.Q8_0.gguf
```

Chat works without it; only uploads and retrieval need it.

### 2. Server

```bash
cd server && python -m venv .venv && .venv/Scripts/python.exe -m pip install -r requirements.txt
```

#### Installing llama-cpp-python

This is the one awkward dependency, so it is deliberately left out of
`requirements.txt`. **There is no prebuilt Windows wheel on PyPI** — a plain
`pip install llama-cpp-python` tries to compile llama.cpp from source and fails
unless you have a full MSVC C++ toolchain installed.

Three ways around it, cheapest first:

1. **Copy an already-compiled install** (what this project did). If another
   virtualenv on the machine has a working `llama_cpp`, copy both directories
   into this venv's `site-packages`. The Python versions must match (both 3.11
   here); the compiled DLLs live in `llama_cpp/lib/` and come along with the copy.

   ```bash
   cp -r <other-venv>/Lib/site-packages/llama_cpp server/.venv/Lib/site-packages/
   ```

2. **Try the prebuilt wheel index.** Availability varies by Python version and
   llama-cpp-python release.

   ```bash
   pip install llama-cpp-python --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu
   ```

3. **Build from source**, which needs Visual Studio Build Tools with the C++
   workload plus CMake. Slowest, but always works.

Verify whichever route you took:

```bash
server/.venv/Scripts/python.exe -c "import llama_cpp; print(llama_cpp.__version__)"
```

> **Version pin:** `0.3.34` is used here because its bundled `llama.dll` supports
> the `qwen35` architecture that Qwen3.5 GGUFs declare. An older llama.cpp will
> refuse the model with `unknown model architecture: 'qwen35'`.

### 3. Client

```bash
cd client && npm install
```

## Running

`bash run.sh` does all of this in one terminal. To run the halves by hand, use
two terminals.

Server:

```bash
cd server && .venv/Scripts/python.exe -m uvicorn app.main:app --port 8000
```

Client:

```bash
cd client && npm run dev
```

Then open http://localhost:5173.

The model is loaded lazily on the first message, not at startup — so the server
is ready instantly and only commits ~2.5 GB once you actually chat. The first
reply therefore takes an extra ~7 seconds while the weights load.

> Avoid `uvicorn --reload` while working on model code: every reload reloads the
> model too.

## Using it from your phone

The Vite dev server listens on every interface, so the app can be opened from a
phone on the same Wi-Fi. It prints the LAN address on startup:

```
➜  Local:   http://localhost:5173/
➜  Network: http://192.168.x.x:5173/
```

Open that Network URL on the phone.

**Only port 5173 needs to be reachable.** The `/api` proxy runs inside the dev
server, so the phone talks to Vite and Vite forwards to the backend over
loopback. FastAPI stays bound to `127.0.0.1` and is never exposed — one open
port instead of two.

### Windows firewall

Windows blocks the inbound connection by default, so the phone will simply time
out until a rule exists. Both commands need an **Administrator** PowerShell.

If the network is shown as Public (check with `Get-NetConnectionProfile`), set
your own Wi-Fi to Private first — Public is the profile meant for untrusted
networks like cafés and airports:

```powershell
Set-NetConnectionProfile -InterfaceAlias "Wi-Fi" -NetworkCategory Private
```

Then allow the port, scoped to the Private profile only:

```powershell
New-NetFirewallRule -DisplayName "illamaRAG dev client (5173)" -Direction Inbound -LocalPort 5173 -Protocol TCP -Action Allow -Profile Private
```

To remove it later:

```powershell
Remove-NetFirewallRule -DisplayName "illamaRAG dev client (5173)"
```

### Before you expose it

**There is no authentication.** Anyone on the same network who opens that URL
can read every conversation and send new ones. That is fine on a home network
you control; think twice on office, campus or shared Wi-Fi. Stopping the dev
server closes the door, and deleting the firewall rule closes it properly.

The phone is only a screen — the model still runs on this laptop, at the same
few tokens per second, and the laptop must stay awake.

### On the phone specifically

- The chat list becomes a slide-out drawer behind the ☰ button
- Enter inserts a newline and the Send button sends, since phone keyboards have
  no practical Shift+Enter
- Layout accounts for notches and home indicators, and uses `dvh` so the
  composer is not hidden behind the browser's toolbar

## Checking it works

Two scripts, both runnable on their own:

```bash
cd server && .venv/Scripts/python.exe scripts/smoke_test.py
```

```bash
cd server && .venv/Scripts/python.exe scripts/api_test.py
```

```bash
cd server && .venv/Scripts/python.exe scripts/rag_test.py
```

`smoke_test.py` checks the model loads and generates, with timings and memory.
`api_test.py` needs the server running and covers the behaviours that are easy
to get subtly wrong: that history is really fed back to the model, that two
sessions cannot see each other, and that deleting a session removes its messages.

`rag_test.py` exercises the knowledge base across all four source types. Every
fixture states invented facts — a made-up protocol, an invented part number, an
odd refund window — so a correct answer can only come from retrieval. A suite
built on real facts would pass even with retrieval completely broken.

## Performance on modest hardware

Measured on an i5-1135G7 (4 cores) with 8 GB RAM, CPU only:

| | |
|---|---|
| Model load (first message) | ~7 s |
| Generation | ~4–6 tokens/sec |
| Resident memory while chatting | ~2.7 GB |

A few tokens per second is normal for a 4B model on a laptop CPU. Close heavy
applications before starting if RAM is tight.

### How memory is kept down

These choices live in `server/app/core/llm.py` and `server/app/config.py`:

- **`n_ctx` is pinned to 4096.** The most important one. This model declares a
  native context of 262144 tokens; a KV cache that size would need roughly
  21 GB. At 4096 it is about 170 MB.
- **Quantized KV cache** (`q8_0` with flash attention), roughly half of f16.
  Falls back to f16 automatically if unsupported.
- **`use_mmap=True`, `use_mlock=False`.** Weights are paged in on demand and can
  be evicted under pressure. Turning `mlock` on would pin all 2.5 GB in RAM.
- **One lazily-created model instance** for the whole process.
- **`n_threads=4`** — physical cores, not the 8 logical ones.
- **`n_gpu_layers=0`** — an integrated GPU shares system RAM, so offloading
  would allocate the same memory twice.

## Configuration

Copy `server/.env.example` to `server/.env`. Everything is optional; the
defaults in `app/config.py` are sensible.

| Variable | Default | Notes |
|---|---|---|
| `MODEL_PATH` | `models/Qwen3.5-4B-Q4_K_M.gguf` | Absolute or repo-relative |
| `N_CTX` | `4096` | Raising this grows the KV cache |
| `N_THREADS` | `4` | Physical cores |
| `MAX_OUTPUT_TOKENS` | `1024` | Reserved out of the context window |
| `ENABLE_THINKING` | `false` | Reasoning is slow on CPU |
| `TEMPERATURE` | `0.7` | |

## API

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/health` | Model, memory and session status |
| `POST` | `/api/sessions` | Create a session |
| `GET` | `/api/sessions` | List, most recent activity first |
| `GET` | `/api/sessions/{id}` | One session |
| `GET` | `/api/sessions/{id}/messages` | Full transcript |
| `PATCH` | `/api/sessions/{id}` | Rename |
| `DELETE` | `/api/sessions/{id}` | Delete, cascading to messages |
| `POST` | `/api/sessions/{id}/clear` | Clear messages, keep the session |
| `POST` | `/api/chat/stream` | Stream a reply (SSE) |
| `POST` | `/api/documents` | Upload a PDF, TXT, MD or HTML file |
| `POST` | `/api/documents/url` | Index a web page |
| `GET` | `/api/documents` | List the knowledge base |
| `GET` | `/api/documents/events` | Live status updates (SSE): a snapshot, then one event per change |
| `DELETE` | `/api/documents/{id}` | Delete a document and its vectors |
| `POST` | `/api/documents/search` | Retrieval only, with scores — for tuning |

Interactive docs while the server runs: http://127.0.0.1:8000/docs

### The SSE format

Each frame is one JSON object with a `type`, rather than SSE `event:` lines, so
the client has a single parsing path:

```
data: {"type": "session",  "session_id": "...", "title": "...", "created": true}
data: {"type": "sources",  "sources": [...], "searched": true}
data: {"type": "thinking", "text": "..."}
data: {"type": "token",    "text": "..."}
data: {"type": "done",     "content": "...", "message_id": 12, "stopped": false}
data: {"type": "error",    "message": "..."}
```

## Project layout

```
illamaProjects/
├── models/                      # GGUF weights (gitignored)
├── server/
│   ├── app/
│   │   ├── main.py              # app, CORS, /api/health
│   │   ├── config.py            # settings and tuning
│   │   ├── core/
│   │   │   ├── llm.py           # the one model instance + generation lock
│   │   │   ├── prompt.py        # ChatML, history trimming, think-tag splitting
│   │   │   └── memory.py        # RAM reporting
│   │   ├── db/
│   │   │   ├── database.py      # connections and schema
│   │   │   └── repository.py    # all session/message SQL
│   │   ├── rag/
│   │   │   ├── embedder.py      # nomic-embed, prefixes + pooling
│   │   │   ├── loaders.py       # PDF / text / HTML / URL -> text
│   │   │   ├── chunker.py       # structure-aware splitting
│   │   │   ├── store.py         # Chroma vectors + FTS5 keywords
│   │   │   ├── retriever.py     # hybrid search, RRF, filtering
│   │   │   └── ingest.py        # background indexing
│   │   ├── api/
│   │   │   ├── chat.py          # streaming chat
│   │   │   ├── documents.py     # knowledge base
│   │   │   └── sessions.py      # session CRUD
│   │   └── schemas/chat.py
│   ├── scripts/                 # smoke_test.py, api_test.py, rag_test.py
│   └── data/                    # chats.db, vector_db, uploads (gitignored)
└── client/
    └── src/
        ├── App.jsx              # state, send/stop flow
        ├── api.js               # fetch + SSE reader
        └── components/
            ├── KnowledgePanel.jsx   # uploads, links, indexing status
            ├── RichText.jsx         # Markdown -> React, no raw HTML
            └── ...
```

## Design notes

Three things here are less obvious than they look, and each exists because the
straightforward version is wrong:

**One model, one lock.** llama.cpp is not thread-safe, and FastAPI runs sync
routes in a threadpool. Without the lock in `core/llm.py`, two simultaneous
chats share one KV cache and corrupt each other's output.

**Cancellation has to be cooperative.** Generation is a blocking loop that
nothing can interrupt from outside. So `api/chat.py` runs it in a worker thread
feeding a queue; when the client disconnects, the route sets a flag that the
loop checks once per token. Without this, pressing Stop leaves the model
generating to `max_tokens` while still holding the lock — blocking every other
chat for a minute or more on this hardware. This was measured, not assumed.

**History is trimmed by tokenizing, not by counting messages.** A fixed "last N
messages" rule either wastes most of the context window on short turns or
overflows it on long ones. `prompt.trim_to_budget` renders the candidate prompt,
measures it, and drops whole oldest turns until it fits.

## Troubleshooting

**`unknown model architecture: 'qwen35'`** — llama-cpp-python is too old for
this model. Use 0.3.34 or newer.

**`FileNotFoundError` for the model** — check `models/` contains the `.gguf`, or
set `MODEL_PATH` in `server/.env`.

**First message takes ages** — that is the model loading (~7 s). Later messages
start immediately.

**Replies are very slow** — expected at ~4–6 tokens/sec on CPU. Check free RAM
in the header badge; if it is near zero the machine is swapping. Close other
applications, or lower `N_CTX`.

**Client shows "server offline"** — the server is not running on port 8000, or
it crashed on startup. Check its terminal.

## How retrieval works

Retrieval is a pipeline, and each stage is there because the naive version has
a specific failure mode:

**1. Chunking** (`rag/chunker.py`) splits on paragraph then sentence
boundaries, never across a page or section break, with overlap between
neighbours. A chunk cut mid-sentence embeds poorly, because the vector then
describes a fragment. Each chunk is embedded with its document and section name
prepended — "the limit is 30 days" is meaningless once lifted out of the policy
it belonged to.

**2. Two indexes** (`rag/store.py`), because they fail differently:

| | Good at | Bad at |
|---|---|---|
| Vectors (Chroma, cosine) | meaning, paraphrase | rare literal strings |
| BM25 (SQLite FTS5) | exact terms, codes, names | any rephrasing |

Search for part number `QX-4471-B` and pure vector search struggles — the
string carries almost no semantic signal. BM25 finds it instantly. Ask "how do
I get my money back" and BM25 finds nothing while the vector lands on the
refund section. Running both is what makes retrieval dependable rather than
occasionally impressive.

**3. Fusion with Reciprocal Rank Fusion** (`rag/retriever.py`). RRF combines
the two *rankings*, not their scores. Cosine similarity and BM25 live on
different unnormalised scales, so any weighted sum of them needs retuning
whenever the corpus changes; RRF needs none.

**4. A relevance floor.** Measured, not guessed: nomic-embed scores two
completely unrelated sentences at 0.36–0.48, while genuine matches land at
0.70–0.90. The floor sits at 0.50, between the two populations. A threshold
near zero would admit everything, which is exactly how a RAG system ends up
answering confidently from irrelevant text. When nothing clears the floor the
model is told there is no context, rather than being handed noise.

**5. Neighbour expansion.** The chunks either side of a strong hit are pulled
in, so an answer straddling a boundary is not cut in half.

**6. Follow-up questions.** "What about the second one?" is unanswerable alone,
so a short or anaphoric question is rewritten with the previous turn before
searching. Deliberately a heuristic rather than an LLM rewrite — a rewrite call
would cost several seconds per message at 4–6 tokens/sec.

Everything is tunable in `server/app/config.py`: `chunk_chars`,
`rag_candidates`, `rag_top_k`, `rag_min_score`, `rag_neighbour_window`,
`rag_max_context_chars`.

### Debugging retrieval

`POST /api/documents/search` runs retrieval *without* generating an answer and
returns both scores per chunk. It is the only practical way to tell a retrieval
problem from a generation problem — if the right chunk is not in those results,
no amount of prompt tuning will help.

### Choosing an embedding model

The embedder shares 8 GB with a 2.7 GB chat model, so size matters more than
leaderboard position. Measured GGUF sizes:

| Model | Q8_0 size | Dims | Notes |
|---|---|---|---|
| **nomic-embed-text-v1.5** | **139 MB** | 768 | Used here. Retrieval-trained, fast on CPU, English-only. |
| Qwen3-Embedding-0.6B | 610 MB | 1024 | Better quality and multilingual, but 4.4× the size and slower. |
| EmbeddingGemma-300M | ~307 MB | 768 | Middle ground; llama.cpp support less proven. |
| Qwen3-Embedding-4B | 2.5 GB | 2560 | **Will not fit** beside the chat model. |

Use Q8_0 rather than a smaller quant: embedding vectors are more sensitive to
quantization than generated text, and the saving below Q8 is a few dozen MB.
Note that this GGUF declares a **2048-token** context, not the 8192 the model
card advertises, so chunks are sized well under it.

Switch models with `EMBED_MODEL_PATH` in `server/.env` — but two models produce
incompatible vectors, so **delete `server/data/vector_db` and re-index
everything** afterwards. Mixed vectors return nonsense rather than an error.

Three details about nomic-embed are easy to get wrong and silently degrade
retrieval rather than raising an error, all handled in `rag/embedder.py`: task
prefixes (`search_query:` on questions, `search_document:` on passages), mean
pooling, and L2 normalisation.

### A note on exposure

The URL fetcher refuses links that resolve to private, loopback or link-local
addresses. With the app reachable from the LAN and no authentication, an
unrestricted fetcher would let anyone on the Wi-Fi reach services that are not
otherwise exposed. Override with `ALLOW_PRIVATE_URL_FETCH=true` only if you
intend to index something on your own network.

## Not done yet

- **Agent tool loop.** Retrieval currently always runs when documents exist.
  The next step is letting the model decide per turn whether to search, and
  re-search with a rewritten query when the first attempt comes back weak.
  `retriever.retrieve()` is the seam.
- **Reranking.** A cross-encoder over the fused candidates would improve
  ordering, at the cost of another model in memory and more latency per query.
- **OCR** for scanned PDFs; image-only pages are currently rejected with a
  clear message.
- Auth and multi-user, Docker, tests beyond the three suites.
