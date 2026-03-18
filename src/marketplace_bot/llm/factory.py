from marketplace_bot.config import SETTINGS
from marketplace_bot.llm.base import BaseLLMClient
from marketplace_bot.llm.gemini_provider import GeminiLLMClient
from marketplace_bot.llm.null_provider import NullLLMClient


def build_llm_client() -> BaseLLMClient:
    try:
        return GeminiLLMClient(
            model_name=SETTINGS.gemini_model,
            analysis_model_name=SETTINGS.gemini_index_model,
            live_model_name=SETTINGS.gemini_live_model,
        )
    except Exception as exc:
        return NullLLMClient(
            reason=(
                "Gemini provider initialization failed: "
                f"{exc}. Ensure GEMINI_API_KEY or GOOGLE_API_KEY is exported locally, "
                "or set GOOGLE_CLOUD_PROJECT for Vertex-backed execution before starting the app."
            )
        )
