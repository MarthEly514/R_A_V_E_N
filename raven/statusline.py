"""Bottom status line: small, ordered, config-selected segments.

Each segment is a function (provider, assistant) -> str. Add a new one by
dropping it in SEGMENTS, then list its name in settings.json -> statusline.segments.
"""
import psutil

from raven.assistant import MAX_CONTEXT_MESSAGES

SEP = "  •  "


def _model(provider, assistant) -> str:
    """model that answered the last request"""
    name = getattr(provider, "last_model", None) or provider.model
    return name.split("/")[-1].replace(":free", "")


def _context(provider, assistant) -> str:
    """history messages sent to the model / cap"""
    return f"ctx {assistant.context_len}/{MAX_CONTEXT_MESSAGES}"


def _tokens(provider, assistant) -> str:
    """cumulative tokens used this session"""
    return f"{provider.total_tokens:,} tok"


def _memory(provider, assistant) -> str:
    """system memory in use"""
    return f"mem {psutil.virtual_memory().percent:.0f}%"


def _cpu(provider, assistant) -> str:
    """system CPU load"""
    return f"cpu {psutil.cpu_percent():.0f}%"


SEGMENTS = {
    "model": _model,
    "context": _context,
    "tokens": _tokens,
    "memory": _memory,
    "cpu": _cpu,
}


def render(provider, assistant, names: list[str]) -> str:
    parts = []
    for name in names:
        fn = SEGMENTS.get(name)
        if fn is None:
            continue
        try:
            parts.append(fn(provider, assistant))
        except Exception:
            pass
    return SEP.join(parts)
