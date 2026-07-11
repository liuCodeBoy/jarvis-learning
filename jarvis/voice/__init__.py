"""Speech synthesis adapters with time-aligned facial animation data."""

from .synthesis import (
    AzureSpeechSynthesizer,
    SpeechSynthesisError,
    SpeechSynthesisResult,
    UnavailableSpeechSynthesizer,
)

__all__ = [
    "AzureSpeechSynthesizer",
    "SpeechSynthesisError",
    "SpeechSynthesisResult",
    "UnavailableSpeechSynthesizer",
]
