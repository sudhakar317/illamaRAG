# illamaRAG — local chatbot with persistent chats and sessions

A small, fully local chat application. A Qwen3.5-4B GGUF model runs on your own
CPU through llama.cpp — no Ollama, no API keys, nothing leaves the machine.
Conversations and sessions are stored in SQLite, so they survive a restart.

This is **v1: the chat foundation**. Retrieval (RAG) and the agent loop are
deliberately not here yet — see [Where RAG plugs in later](#where-rag-plugs-in-later).

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

## Requirements

- Python 3.11, Node 20+
- A Qwen GGUF chat model at `models/Qwen3.5-4B-Q4_K_M.gguf`
- About 3.5 GB of free RAM while running

## Setup

### 1. The model

Place your GGUF file at `models/Qwen3.5-4B-Q4_K_M.gguf`. The `models/` folder is
gitignored — multi-GB weights do not belong in git. To use a different path or
file, set `MODEL_PATH` in `server/.env`.

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

Two terminals.

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

## Checking it works

Two scripts, both runnable on their own:

```bash
cd server && .venv/Scripts/python.exe scripts/smoke_test.py
```

```bash
cd server && .venv/Scripts/python.exe scripts/api_test.py
```

`smoke_test.py` checks the model loads and generates, with timings and memory.
`api_test.py` needs the server running and covers the behaviours that are easy
to get subtly wrong: that history is really fed back to the model, that two
sessions cannot see each other, and that deleting a session removes its messages.

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

Interactive docs while the server runs: http://127.0.0.1:8000/docs

### The SSE format

Each frame is one JSON object with a `type`, rather than SSE `event:` lines, so
the client has a single parsing path:

```
data: {"type": "session",  "session_id": "...", "title": "...", "created": true}
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
│   │   ├── api/
│   │   │   ├── chat.py          # streaming chat
│   │   │   └── sessions.py      # session CRUD
│   │   └── schemas/chat.py
│   ├── scripts/                 # smoke_test.py, api_test.py
│   └── data/                    # chats.db (gitignored)
└── client/
    └── src/
        ├── App.jsx              # state, send/stop flow
        ├── api.js               # fetch + SSE reader
        └── components/
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

## Where RAG plugs in later

The seam is deliberate: `build_messages()` in `app/core/prompt.py` is the single
place that decides what context the model sees. Adding retrieval means adding an
embedder, a vector store and document upload, then changing only that function
to prepend retrieved chunks. Routes, storage and the client stay as they are.
The agent tool-calling loop replaces the same function's body a step after that.

One caution for that stage: **do not use a 4 GB embedding model** on a machine
like this. A ~600 MB `Qwen3-Embedding-0.6B-Q8_0` gives comparable retrieval
quality here, and is what makes running an embedder alongside the 4B chat model
possible at all within 8 GB.
