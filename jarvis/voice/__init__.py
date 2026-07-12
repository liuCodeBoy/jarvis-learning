"""Speech synthesis adapters with time-aligned facial animation data."""

from .synthesis import (
    AzureSpeechSynthesizer,
    EdgeSpeechSynthesizer,
    SpeechSynthesisError,
    SpeechSynthesisResult,
    UnavailableSpeechSynthesizer,
)

__all__ = [
    "AzureSpeechSynthesizer",
    "EdgeSpeechSynthesizer",
    "SpeechSynthesisError",
    "SpeechSynthesisResult",
    "UnavailableSpeechSynthesizer",
]
