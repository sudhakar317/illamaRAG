"""Application settings, read from environment variables or server/.env."""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# config.py lives at <repo>/server/app/config.py, so:
#   parents[0] = app, parents[1] = server, parents[2] = <repo root>
SERVER_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=SERVER_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---- Model ----
    model_path: Path = REPO_ROOT / "models" / "Qwen3.5-4B-Q4_K_M.gguf"

    # Context window. Pinned deliberately and never left at the model default:
    # this GGUF declares a native context of 262144 tokens, and a KV cache that
    # size would need roughly 21 GB of RAM. 4096 keeps it near 170 MB (q8_0).
    n_ctx: int = 4096

    # Physical cores, not logical. The i5-1135G7 has 4 cores / 8 threads, and
    # oversubscribing hyperthreads costs memory bandwidth without adding speed.
    n_threads: int = 4

    # Prompt-processing batch size. Smaller batch = smaller compute buffer.
    n_batch: int = 256

    # Keep at 0: the Iris Xe iGPU has no dedicated VRAM, so "offloading" would
    # allocate the same system RAM twice.
    n_gpu_layers: int = 0

    # Quantized KV cache (needs flash attention for the V side) roughly halves
    # KV memory versus f16.
    kv_cache_type: str = "q8_0"

    # Tokens reserved for the reply. History is trimmed to fit in the remainder.
    max_output_tokens: int = 1024

    # ---- Sampling ----
    temperature: float = 0.7
    top_p: float = 0.9
    repeat_penalty: float = 1.1

    # Qwen3.x models can emit <think>...</think> reasoning blocks. They are off
    # by default here purely for usability: this CPU produces roughly 3-8
    # tokens/sec, so reasoning tokens the user never reads are expensive.
    enable_thinking: bool = False

    # Render the prompt with the GGUF's own Jinja chat template instead of the
    # hand-written ChatML formatter. Off by default -- this model ships the
    # multimodal Qwen3.5 template, which is far more machinery than a text-only
    # chat needs. See core/prompt.py.
    use_builtin_chat_template: bool = False

    system_prompt: str = (
        "You are a helpful, friendly AI assistant running locally on the "
        "user's own computer. Answer clearly and concisely. If you do not know "
        "something, say so plainly instead of guessing."
    )

    # ---- Storage ----
    data_dir: Path = SERVER_DIR / "data"

    # ---- CORS ----
    # Listed explicitly: allow_origins=["*"] combined with
    # allow_credentials=True is invalid per the CORS spec and browsers reject it.
    cors_origins: list[str] = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ]

    @property
    def db_path(self) -> Path:
        return self.data_dir / "chats.db"


@lru_cache
def get_settings() -> Settings:
    """Settings are read once and cached for the process lifetime."""
    settings = Settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    return settings


settings = get_settings()
