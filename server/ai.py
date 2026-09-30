"""Provider-neutral LLM client.

Two wire styles cover almost every provider:
  * "anthropic" – Claude through the official ``anthropic`` Python SDK.
  * "openai"    – any OpenAI-compatible /chat/completions endpoint (OpenAI, Gemini,
                  OpenRouter, Groq, DeepSeek, Mistral, Ollama, LM Studio, custom …),
                  spoken with the standard library so no extra package is needed.

Each provider keeps its own key / model / URL. When the chosen one fails (quota, outage, no image
support, wrong model name …) the other providers that have a saved key are tried in turn, and a
mistyped model name is corrected from the provider's own model list.
"""

from __future__ import annotations

import difflib
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from typing import Any

import settings
from i18n import t as tr

PRESETS: list[dict[str, Any]] = [
    {"id": "none", "label": "بدون ذكاء اصطناعي (أوامر أساسية فقط)", "label_en": "No AI (basic commands only)",
     "style": "none"},
    {"id": "anthropic", "label": "Anthropic Claude", "style": "anthropic",
     "base_url": "https://api.anthropic.com", "model": "claude-opus-5-5", "env": "ANTHROPIC_API_KEY"},
    {"id": "openai", "label": "OpenAI", "style": "openai",
     "base_url": "https://api.openai.com/v1", "model": "gpt-5-mini", "env": "OPENAI_API_KEY"},
    {"id": "gemini", "label": "Google Gemini", "style": "openai",
     "base_url": "https://generativelanguage.googleapis.com/v1beta/openai", "model": "gemini-2.5-flash",
     "env": "GEMINI_API_KEY"},
    {"id": "openrouter", "label": "OpenRouter", "style": "openai",
     "base_url": "https://openrouter.ai/api/v1", "model": "openrouter/auto", "env": "OPENROUTER_API_KEY"},
    {"id": "groq", "label": "Groq", "style": "openai",
     "base_url": "https://api.groq.com/openai/v1", "model": "llama-3.3-70b-versatile", "env": "GROQ_API_KEY"},
    {"id": "deepseek", "label": "DeepSeek", "style": "openai",
     "base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat", "env": "DEEPSEEK_API_KEY"},
    {"id": "mistral", "label": "Mistral", "style": "openai",
     "base_url": "https://api.mistral.ai/v1", "model": "mistral-small-latest", "env": "MISTRAL_API_KEY"},
    {"id": "ollama", "label": "Ollama (محلي بدون إنترنت)", "label_en": "Ollama (local, offline)", "style": "openai",
     "base_url": "http://localhost:11434/v1", "model": "llama3.1", "no_key": True},
    {"id": "lmstudio", "label": "LM Studio (محلي)", "label_en": "LM Studio (local)", "style": "openai",
     "base_url": "http://localhost:1234/v1", "model": "", "no_key": True},
    {"id": "custom", "label": "مخصص (أي مزود متوافق مع OpenAI)", "label_en": "Custom (any OpenAI-compatible provider)",
     "style": "openai", "base_url": "", "model": ""},
]
_BY_ID = {p["id"]: p for p in PRESETS}


def label_of(preset: dict[str, Any]) -> str:
    return tr(preset["label"], preset.get("label_en") or preset["label"])

# Anthropic request options that only some models accept.
_EFFORT_MODELS = ("claude-opus-5", "claude-opus-4-5", "claude-opus-4-6", "claude-opus-4-7", "claude-opus-4-8",
                  "claude-fable-5", "claude-mythos-5", "claude-sonnet-5", "claude-sonnet-4-6")
_FALLBACK_MODELS = {"claude-opus-5", "claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5", "claude-fable-5-1"}
_FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AIError(Exception):
    """``kind`` says what the user has to fix: key, model, provider, network, timeout, quota, setup, reply, refused.
    ``details``: one entry per provider that was tried."""

    def __init__(self, message: str, kind: str = "provider", details: list[dict[str, Any]] | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.details = details or []


class ModelError(AIError):
    """The provider does not know the requested model; ``available`` may list the ones it does."""

    def __init__(self, message: str, available: list[str] | None = None) -> None:
        super().__init__(message, "model")
        self.available = available or []


_KINDS: dict[str, tuple[tuple[str, str], tuple[str, str]]] = {  # title, what to do
    "key": (("مشكلة في مفتاح API", "API key problem"),
            ("انسخ المفتاح كاملاً من موقع المزود والصقه في الإعدادات ← API key، ثم «حفظ واختبار».",
             "Copy the whole key from the provider's site, paste it in Settings → API key, then “Save & test”.")),
    "model": (("اسم النموذج غير صحيح", "Wrong model name"),
              ("في الإعدادات اضغط «القائمة» بجانب خانة النموذج واختر اسماً منها، ثم «حفظ واختبار».",
               "In Settings tap “List” next to the model field and pick one, then “Save & test”.")),
    "provider": (("المزود لا يعمل الآن", "The provider is not working"),
                 ("جرّب بعد قليل، أو اختر مزوداً آخر من الإعدادات.", "Try again shortly, or choose another provider in Settings.")),
    "network": (("لا يوجد اتصال بالمزود", "Can't reach the provider"),
                ("تحقق من إنترنت الكمبيوتر ومن عنوان المزود (Base URL) في الإعدادات.",
                 "Check the PC's internet and the provider address (Base URL) in Settings.")),
    "timeout": (("المزود تأخر في الرد", "The provider is too slow"),
                ("أعد المحاولة، أو اختر نموذجاً أسرع (مثل flash أو mini).", "Try again, or pick a faster model (e.g. flash or mini).")),
    "quota": (("انتهى الرصيد أو حد الطلبات", "Balance or rate limit used up"),
              ("اشحن رصيد الحساب لدى المزود أو انتظر قليلاً، أو اختر مزوداً آخر.",
               "Top up the account at the provider or wait a little, or choose another provider.")),
    "setup": (("إعداد الذكاء الاصطناعي ناقص", "AI setup incomplete"),
              ("افتح الإعدادات وأكمل بيانات المزود.", "Open Settings and complete the provider details.")),
    "reply": (("رد غير مفهوم من النموذج", "Unreadable reply from the model"),
              ("أعد المحاولة، وإن تكرر جرّب نموذجاً آخر.", "Try again; if it keeps happening, try another model.")),
    "refused": (("النموذج رفض الطلب", "The model declined"), ("أعد صياغة الطلب.", "Rephrase the request.")),
}


def describe(exc: AIError) -> dict[str, Any]:
    """An AI failure for the phone: what went wrong, what to do, and which provider / model (never the key)."""
    kind = getattr(exc, "kind", "provider")
    title, hint = _KINDS.get(kind, _KINDS["provider"])
    c = resolved()
    out: dict[str, Any] = {"kind": kind, "title": tr(*title), "hint": tr(*hint), "message": str(exc),
                           "provider": c["label"], "model": c["model"], "base_url": c["base_url"],
                           "details": getattr(exc, "details", [])}
    models = getattr(exc, "available", None) or [m for d in out["details"] for m in d.get("models", [])]
    if models:
        out["models"] = models[:30]
    return out


_notice = threading.local()


def _note(text: str) -> None:
    _notice.items = getattr(_notice, "items", []) + [text]


def take_notices() -> list[str]:
    """Messages about automatic fixes (fallback provider, corrected model) since the last call."""
    items = getattr(_notice, "items", [])
    _notice.items = []
    return items


def add_notices(items: list[str]) -> None:
    """Hand notices collected on a worker thread to the current one."""
    for text in items:
        _note(text)


def config_for(pid: str, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    """Effective config of one provider: its saved values, preset defaults and env-var key."""
    cfg = settings.get()["ai"]
    preset = _BY_ID.get(pid or "none", _BY_ID["custom"])
    o = overrides or {}
    key = (o.get("api_key") or (cfg.get("keys") or {}).get(preset["id"]) or
           (os.environ.get(preset["env"], "") if preset.get("env") else ""))
    return {
        "provider": preset["id"],
        "style": preset["style"],
        "label": label_of(preset),
        "base_url": (o.get("base_url") or (cfg.get("bases") or {}).get(preset["id"]) or preset.get("base_url") or "").strip(),
        "model": (o.get("model") or (cfg.get("models") or {}).get(preset["id"]) or preset.get("model") or "").strip(),
        "api_key": str(key).strip(),
        "effort": cfg.get("effort") or "medium",
        "no_key": bool(preset.get("no_key")),
    }


def resolved() -> dict[str, Any]:
    return config_for(settings.get()["ai"].get("provider") or "none")


def usable(c: dict[str, Any]) -> bool:
    if c["style"] == "none" or not c["base_url"]:
        return False
    return bool(c["api_key"]) or c["no_key"] or c["provider"] == "custom"


def enabled() -> bool:
    cfg = settings.get()["ai"]
    if (cfg.get("provider") or "none") == "none":
        return False
    return usable(resolved()) or (bool(cfg.get("fallback", True)) and bool(configured()))


def configured() -> list[str]:
    """Providers the user has set up (saved key, or a local one they configured)."""
    cfg = settings.get()["ai"]
    touched = set(cfg.get("keys") or {}) | set(cfg.get("models") or {}) | set(cfg.get("bases") or {})
    out = []
    for p in PRESETS:
        if p["id"] == "none":
            continue
        c = config_for(p["id"])
        if usable(c) and (c["api_key"] or p["id"] in touched):
            out.append(p["id"])
    return out


def complete(system: str, messages: list[dict[str, Any]], images: list[tuple[str, str]] | None = None,
             json_mode: bool = True, effort: str | None = None, max_tokens: int = 16000,
             timeout: float = 120.0, deadline: float | None = None,
             on_try: Any = None) -> str:
    """Send a chat and return the assistant text, falling back to other saved providers on failure.

    ``messages`` is a list of {"role": "user"|"assistant", "content": str}. ``images`` is a list of
    (base64 data, mime type) attached to the last user message. ``timeout``: seconds without an answer
    before giving up on a provider; ``deadline`` (time.monotonic()): no provider is tried after it.
    ``on_try(label, model)`` is called before each provider is asked (the phone shows it while waiting).
    """
    cfg = settings.get()["ai"]
    primary = cfg.get("provider") or "none"
    chain = [primary]
    if cfg.get("fallback", True):
        chain += [p for p in configured() if p != primary]
    errors: list[tuple[str, str, AIError]] = []  # (provider, model, error)
    for pid in chain:
        c = config_for(pid)
        if not usable(c):
            continue
        wait = timeout
        if deadline is not None:
            wait = min(timeout, deadline - time.monotonic())
            if wait < 3:
                errors.append((c["label"], c["model"], AIError(tr("لم يبق وقت", "no time left"), "timeout")))
                break
        c = {**c, "timeout": wait}
        if on_try:
            on_try(c["label"], c["model"])
        try:
            text = _call(c, system, messages, images or [], json_mode, effort or c["effort"], max_tokens)
        except AIError as exc:
            errors.append((c["label"], c["model"], exc))
            continue
        if errors:
            _note(tr(f"استخدمت {c['label']} لأن {errors[0][0]} لم يعمل ({_short(str(errors[0][2]))})",
                     f"Used {c['label']} because {errors[0][0]} failed ({_short(str(errors[0][2]))})"))
        return text
    if not errors:
        raise AIError(tr("لم يتم اختيار مزود ذكاء اصطناعي", "No AI provider is selected"), "setup")
    msg = f"{errors[0][0]}: {errors[0][2]}"
    if len(errors) > 1:
        msg += tr(" — وجربت أيضاً: ", " — also tried: ") + tr("، ", ", ").join(
            f"{n} ({_short(str(e))})" for n, _m, e in errors[1:])
    details = [{"provider": n, "model": m, "kind": e.kind, "message": str(e),
                **({"models": e.available[:30]} if isinstance(e, ModelError) and e.available else {})}
               for n, m, e in errors]
    raise AIError(msg, errors[0][2].kind, details)


def _short(text: str, n: int = 90) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


def _call(c: dict[str, Any], system: str, messages: list[dict[str, Any]], images: list[tuple[str, str]],
          json_mode: bool, effort: str, max_tokens: int) -> str:
    """One provider, with automatic model-name repair."""
    if not c["model"]:
        picked = best_model("", list_models(c))
        if not picked:
            raise AIError(tr("لم أجد نموذجاً — اختره من الإعدادات", "No model found — choose one in Settings"), "model")
        c = {**c, "model": picked}
        _remember_model(c["provider"], picked)
    try:
        return _once(c, system, messages, images, json_mode, effort, max_tokens)
    except ModelError as exc:
        # The name the user typed is kept as it is (never replaced by another model). Only the same name
        # written the provider's way ("DeepSeek-X" -> "deepseek-x") is tried, for this call.
        available = exc.available
        if not available:
            try:
                available = list_models(c)
            except AIError:
                available = []
        tries = []
        same = exact_model(c["model"], available)
        if same and same != c["model"]:
            tries.append(same)
        if c["model"].lower() != c["model"] and c["model"].lower() not in tries:
            tries.append(c["model"].lower())
        for name in tries:
            try:
                text = _once({**c, "model": name}, system, messages, images, json_mode, effort, max_tokens)
            except ModelError:
                continue
            _note(tr(f"المزود يكتب اسم النموذج «{name}»", f"The provider writes the model name as \"{name}\""))
            return text
        exc.available = exc.available or available  # shown to the user to pick from
        raise exc


def exact_model(requested: str, available: list[str]) -> str | None:
    """The provider's id for the same name, ignoring case and punctuation only (never a different model)."""
    key = re.sub(r"[^a-z0-9]", "", (requested or "").lower())
    if not key:
        return None
    for m in available:
        if re.sub(r"[^a-z0-9]", "", m.lower()) == key:
            return m
    return None


def _once(c: dict[str, Any], system: str, messages: list[dict[str, Any]], images: list[tuple[str, str]],
          json_mode: bool, effort: str, max_tokens: int) -> str:
    if c["style"] == "anthropic":
        return _anthropic(c, system, messages, images, effort, max_tokens)
    return _openai(c, system, messages, images, json_mode, effort)


def _remember_model(pid: str, model: str) -> None:
    settings.update({"ai": {"models": {pid: model}}})


_NOT_CHAT = ("embed", "tts", "whisper", "dall-e", "image", "moderation", "audio", "realtime", "transcribe",
             "search", "rerank", "guard", "vision-preview")


def best_model(requested: str, available: list[str]) -> str | None:
    """The provider's model that best matches what the user typed (case, punctuation, typos)."""
    models = [m for m in available if not any(w in m.lower() for w in _NOT_CHAT)] or list(available)
    if not models:
        return None
    r = (requested or "").strip().lower()
    if r:
        low = {m.lower(): m for m in models}
        if r in low:
            return low[r]
        squash = {re.sub(r"[^a-z0-9]", "", m.lower()): m for m in models}
        if re.sub(r"[^a-z0-9]", "", r) in squash:
            return squash[re.sub(r"[^a-z0-9]", "", r)]
        words = set(re.findall(r"[a-z]{2,}", r))

        def fit(m: str) -> float:
            ml = m.lower()
            f = difflib.SequenceMatcher(None, r, ml).ratio() + 0.3 * len(words & set(re.findall(r"[a-z]{2,}", ml)))
            if re.search(r"reason|think", ml) and not re.search(r"reason|think|r1", r):
                f -= 0.5  # a slow thinking model is never the fix for a fast one
            return f

        best = max(models, key=fit)
        if fit(best) >= 0.35:
            return best
    for hint in ("flash", "mini", "chat", "sonnet", "haiku", "turbo", "small", "instruct"):
        for m in models:
            if hint in m.lower():
                return m
    return models[0]


def _names_in_error(text: str) -> list[str]:
    m = re.search(r"(?:supported|available|valid)[^:]*?(?:are|is|:)\s*(.+?)(?:,?\s+but\b|\.\s|\(|$)", text, re.I)
    if not m:
        return []
    return [n for n in re.split(r"[,\s]+", m.group(1)) if re.fullmatch(r"[A-Za-z0-9][\w.\-:/]{2,}", n)]


def list_models(c: dict[str, Any]) -> list[str]:
    """Model ids the provider offers (GET /models, or the Anthropic Models API)."""
    if c["style"] == "anthropic":
        try:
            import anthropic
        except ImportError as exc:
            raise AIError(tr("مكتبة anthropic غير مثبتة", "The anthropic package is not installed"), "setup") from exc
        try:
            client = anthropic.Anthropic(api_key=c["api_key"], base_url=c["base_url"] or None, timeout=30.0)
            return [m.id for m in client.models.list(limit=100)]
        except anthropic.APIError as exc:
            raise AIError(tr(f"تعذر جلب قائمة النماذج: {exc}", f"Could not list the models: {exc}")) from exc
    if c["style"] != "openai" or not c["base_url"]:
        return []
    headers = {"Authorization": f"Bearer {c['api_key']}"} if c["api_key"] else {}
    req = urllib.request.Request(c["base_url"].rstrip("/") + "/models", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise AIError(tr(f"تعذر جلب قائمة النماذج ({exc.code})", f"Could not list the models ({exc.code})"),
                      "key" if exc.code in (401, 403) else "provider") from exc
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise AIError(tr("تعذر جلب قائمة النماذج: ", "Could not list the models: ") + str(getattr(exc, "reason", exc)),
                      "network") from exc
    items = data.get("data") if isinstance(data, dict) else data
    ids = [str(m.get("id") if isinstance(m, dict) else m) for m in (items or [])]
    if "generativelanguage" in c["base_url"]:
        ids = [i.removeprefix("models/") for i in ids]
    return sorted({i for i in ids if i})


# ---------------------------------------------------------------------------
# Anthropic (official SDK)
# ---------------------------------------------------------------------------


def _anthropic(c: dict[str, Any], system: str, messages: list[dict[str, Any]], images: list[tuple[str, str]],
               effort: str, max_tokens: int) -> str:
    try:
        import anthropic
    except ImportError as exc:
        raise AIError(tr("مكتبة anthropic غير مثبتة على الكمبيوتر. شغّل: pip install anthropic",
                          "The anthropic package is not installed on the PC. Run: pip install anthropic"), "setup") from exc
    if not c["api_key"]:
        raise AIError(tr("أدخل مفتاح Anthropic API من إعدادات التطبيق", "Enter an Anthropic API key in the app settings"), "key")

    msgs = [dict(m) for m in messages]
    if images:
        last = msgs[-1]
        blocks: list[dict[str, Any]] = [
            {"type": "image", "source": {"type": "base64", "media_type": mime, "data": data}} for data, mime in images
        ]
        blocks.append({"type": "text", "text": last["content"]})
        last["content"] = blocks

    wait = float(c.get("timeout") or 120.0)
    client = anthropic.Anthropic(api_key=c["api_key"], base_url=c["base_url"] or None, timeout=wait,
                                 max_retries=2 if wait >= 120 else 1)
    model = c["model"]
    kwargs: dict[str, Any] = {"model": model, "max_tokens": max_tokens, "system": system, "messages": msgs}
    if effort and model.startswith(_EFFORT_MODELS):
        kwargs["output_config"] = {"effort": effort}
    try:
        stream_cm = None
        if model in _FALLBACK_MODELS:
            try:
                # On a safety decline the API re-runs the request on Anthropic's recommended model.
                stream_cm = client.beta.messages.stream(betas=[_FALLBACK_BETA], fallbacks="default", **kwargs)
            except TypeError:  # installed SDK predates the fallbacks parameter
                stream_cm = None
        if stream_cm is None:
            stream_cm = client.messages.stream(**kwargs)
        with stream_cm as stream:
            resp = stream.get_final_message()
    except anthropic.AuthenticationError as exc:
        raise AIError(tr("مفتاح Anthropic API غير صالح", "Invalid Anthropic API key"), "key") from exc
    except anthropic.PermissionDeniedError as exc:
        raise AIError(tr("المفتاح لا يملك صلاحية لهذا النموذج", "The key has no access to this model"), "key") from exc
    except anthropic.NotFoundError as exc:
        raise ModelError(tr(f"النموذج غير موجود: {model}", f"Model not found: {model}")) from exc
    except anthropic.RateLimitError as exc:
        raise AIError(tr("تجاوزت حد الطلبات لدى Anthropic، حاول بعد قليل", "Anthropic rate limit reached, try again shortly"), "quota") from exc
    except anthropic.APIStatusError as exc:
        if exc.status_code == 400 and "model" in str(exc.message).lower():
            raise ModelError(tr(f"خطأ في اسم النموذج: {exc.message}", f"Model name error: {exc.message}")) from exc
        raise AIError(tr(f"خطأ من Anthropic ({exc.status_code}): {exc.message}", f"Anthropic error ({exc.status_code}): {exc.message}")) from exc
    except anthropic.APITimeoutError as exc:
        raise AIError(tr("Anthropic تأخر في الرد", "Anthropic took too long to answer"), "timeout") from exc
    except anthropic.APIConnectionError as exc:
        raise AIError(tr("تعذر الاتصال بـ Anthropic — تحقق من الإنترنت", "Could not reach Anthropic — check the internet"), "network") from exc

    if resp.stop_reason == "refusal":
        raise AIError(tr("رفض النموذج تنفيذ هذا الطلب", "The model declined this request"), "refused")
    text = "".join(b.text for b in resp.content if b.type == "text")
    if not text.strip():
        raise AIError(tr("رد فارغ من النموذج", "Empty response from the model"), "reply")
    return text


# ---------------------------------------------------------------------------
# OpenAI-compatible (stdlib HTTP)
# ---------------------------------------------------------------------------


def _is_model_error(detail: str) -> bool:
    d = detail.lower()
    return "model" in d and any(w in d for w in ("not found", "does not exist", "not exist", "invalid", "unknown",
                                                  "supported", "not available", "no such", "unrecognized"))


def _post_json(url: str, body: dict[str, Any], headers: dict[str, str], timeout: float) -> dict[str, Any]:
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _thinks(c: dict[str, Any]) -> bool:
    """OpenAI / Gemini models that reason before answering and accept "reasoning_effort"."""
    m = c["model"].lower()
    if "api.openai.com" in c["base_url"]:
        return bool(re.match(r"(?:gpt-5|o[134])", m))
    if "generativelanguage.googleapis.com" in c["base_url"]:
        return bool(re.match(r"(?:models/)?gemini-(?:2\.5|[3-9])", m))
    return False


def _openai(c: dict[str, Any], system: str, messages: list[dict[str, Any]], images: list[tuple[str, str]],
            json_mode: bool, effort: str = "") -> str:
    if not c["base_url"]:
        raise AIError(tr("أدخل عنوان المزود (Base URL) من الإعدادات", "Enter the provider Base URL in Settings"), "setup")
    if not c["model"]:
        raise AIError(tr("أدخل اسم النموذج من الإعدادات", "Enter the model name in Settings"), "model")
    msgs: list[dict[str, Any]] = [{"role": "system", "content": system}] + [dict(m) for m in messages]
    if images:
        last = msgs[-1]
        parts: list[dict[str, Any]] = [{"type": "text", "text": last["content"]}]
        parts += [{"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}} for data, mime in images]
        last["content"] = parts
    body: dict[str, Any] = {"model": c["model"], "messages": msgs}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    if effort == "low" and _thinks(c):
        body["reasoning_effort"] = "low"  # easy request: little thinking, quick answer
    headers = {"Content-Type": "application/json"}
    if c["api_key"]:
        headers["Authorization"] = f"Bearer {c['api_key']}"
    if "openrouter.ai" in c["base_url"]:
        headers["X-Title"] = "Wi-Fi Remote"
    url = c["base_url"].rstrip("/") + "/chat/completions"

    for attempt in range(3):
        try:
            data = _post_json(url, body, headers, timeout=float(c.get("timeout") or 120.0))
            break
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:400]
            if c["api_key"]:
                detail = detail.replace(c["api_key"], "****")  # some providers echo the key back
            if attempt < 2 and exc.code in (400, 422) and "reasoning" in detail.lower() and "reasoning_effort" in body:
                body.pop("reasoning_effort")  # this model does not take it
                continue
            if exc.code in (400, 404, 422) and _is_model_error(detail):
                raise ModelError(tr(f"النموذج «{c['model']}» غير معروف لدى المزود",
                                    f"The provider does not know the model \"{c['model']}\""), _names_in_error(detail)) from exc
            if attempt < 2 and exc.code in (400, 422) and "response_format" in body:
                body.pop("response_format")  # some servers do not support JSON mode
                continue
            if exc.code in (400, 404, 422) and _is_model_error(detail):
                raise ModelError(tr(f"النموذج «{c['model']}» غير معروف لدى المزود",
                                    f"The provider does not know the model \"{c['model']}\""), _names_in_error(detail)) from exc
            if exc.code in (401, 403):
                raise AIError(tr(f"مفتاح API غير صالح أو بدون صلاحية ({exc.code}): {detail[:200]}",
                                 f"Invalid API key or missing permission ({exc.code}): {detail[:200]}"), "key") from exc
            if exc.code == 404:
                raise ModelError(tr(f"العنوان أو النموذج غير موجود ({c['model']})", f"URL or model not found ({c['model']})")) from exc
            if exc.code == 402:
                raise AIError(tr(f"رصيد الحساب لدى المزود لا يكفي (402): {detail[:200]}",
                                 f"Not enough balance at the provider (402): {detail[:200]}"), "quota") from exc
            if exc.code == 429:
                raise AIError(tr("تجاوزت حد الطلبات لدى المزود، حاول بعد قليل", "Rate limit reached at the provider, try again shortly"), "quota") from exc
            raise AIError(tr(f"خطأ من المزود ({exc.code}): {detail}", f"Provider error ({exc.code}): {detail}")) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(exc, TimeoutError) or isinstance(reason, TimeoutError):
                raise AIError(tr("المزود تأخر في الرد", "The provider took too long to answer"), "timeout") from exc
            raise AIError(tr(f"تعذر الاتصال بـ {c['base_url']}: {reason}", f"Could not connect to {c['base_url']}: {reason}"),
                          "network") from exc
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise AIError(tr("رد غير متوقع من المزود", "Unexpected response from the provider"), "reply") from exc
    if isinstance(content, list):
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    if not str(content or "").strip():
        raise AIError(tr("رد فارغ من النموذج", "Empty response from the model"), "reply")
    return str(content)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def parse_json(text: str) -> dict[str, Any]:
    """Extract the first JSON object from a model reply (tolerates code fences / prose)."""
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.S)
    try:
        obj = json.loads(t)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    start = t.find("{")
    while start != -1:
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(t)):
            ch = t[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            elif ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(t[start:i + 1])
                        if isinstance(obj, dict):
                            return obj
                    except json.JSONDecodeError:
                        break
        start = t.find("{", start + 1)
    raise AIError(tr("لم أفهم رد النموذج (ليس JSON)", "Could not read the model reply (not JSON)"), "reply")


def test() -> str:
    take_notices()
    reply = complete(
        "Reply with a JSON object only.",
        [{"role": "user", "content": 'Return {"ok": true, "msg": "<one short greeting in ' + tr("Arabic", "English") + '>"}'}],
        effort="low",
        max_tokens=2000,
    )
    obj = parse_json(reply)
    return str(obj.get("msg") or "OK")
