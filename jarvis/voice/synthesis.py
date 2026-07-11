"""TTS providers that return audio and a matching viseme timeline."""

from __future__ import annotations

import base64
import importlib
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


logger = logging.getLogger(__name__)


class SpeechSynthesisError(RuntimeError):
    """A safe, classified speech provider failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class SpeechSynthesisResult:
    """One audio payload and the visemes emitted while creating it."""

    audio: bytes
    mime_type: str
    visemes: List[Dict[str, Any]]
    provider: str
    voice: str

    def as_payload(self) -> Dict[str, Any]:
        return {
            "audio_base64": base64.b64encode(self.audio).decode("ascii"),
            "mime_type": self.mime_type,
            "visemes": self.visemes,
            "provider": self.provider,
            "voice": self.voice,
        }


class UnavailableSpeechSynthesizer:
    """Stable null object used when precise speech synthesis is not configured."""

    available = False

    def __init__(self, provider: str, reason: str) -> None:
        self.provider = provider
        self.reason = reason
        self.voice = ""

    def synthesize(self, _text: str) -> SpeechSynthesisResult:
        raise SpeechSynthesisError("speech_unavailable", self.reason)


class AzureSpeechSynthesizer:
    """Azure Speech adapter using service-emitted audio offsets and viseme IDs."""

    provider = "azure"
    mime_type = "audio/wav"

    def __init__(
        self,
        subscription_key: str,
        region: str,
        voice: str = "zh-CN-YunxiNeural",
        endpoint: Optional[str] = None,
    ) -> None:
        self.subscription_key = str(subscription_key or "").strip()
        self.region = str(region or "").strip()
        self.voice = str(voice or "zh-CN-YunxiNeural").strip()
        self.endpoint = str(endpoint or "").strip()
        self._sdk: Optional[Any] = None

    @property
    def available(self) -> bool:
        if not self.subscription_key or (not self.region and not self.endpoint):
            return False
        try:
            self._load_sdk()
        except SpeechSynthesisError:
            return False
        return True

    @property
    def reason(self) -> str:
        if not self.subscription_key:
            return "尚未配置 AZURE_SPEECH_KEY"
        if not self.region and not self.endpoint:
            return "尚未配置 AZURE_SPEECH_REGION"
        try:
            self._load_sdk()
        except SpeechSynthesisError as error:
            return error.message
        return ""

    def _load_sdk(self):
        if self._sdk is not None:
            return self._sdk
        try:
            self._sdk = importlib.import_module("azure.cognitiveservices.speech")
        except ImportError as error:
            raise SpeechSynthesisError(
                "speech_sdk_missing",
                "未安装 Azure Speech SDK，请重新执行 pip install -r requirements.txt",
            ) from error
        return self._sdk

    def synthesize(self, text: str) -> SpeechSynthesisResult:
        if not self.subscription_key or (not self.region and not self.endpoint):
            raise SpeechSynthesisError("speech_unavailable", self.reason)

        speechsdk = self._load_sdk()
        try:
            if self.endpoint:
                speech_config = speechsdk.SpeechConfig(
                    subscription=self.subscription_key,
                    endpoint=self.endpoint,
                )
            else:
                speech_config = speechsdk.SpeechConfig(
                    subscription=self.subscription_key,
                    region=self.region,
                )
            speech_config.speech_synthesis_voice_name = self.voice
            speech_config.set_speech_synthesis_output_format(
                speechsdk.SpeechSynthesisOutputFormat.Riff24Khz16BitMonoPcm
            )
            synthesizer = speechsdk.SpeechSynthesizer(
                speech_config=speech_config,
                audio_config=None,
            )
            visemes: List[Dict[str, Any]] = []

            def capture_viseme(event) -> None:
                visemes.append({
                    "offset_ms": round(float(event.audio_offset) / 10_000, 3),
                    "id": int(event.viseme_id),
                })

            synthesizer.viseme_received.connect(capture_viseme)
            result = synthesizer.speak_text_async(text).get()
        except SpeechSynthesisError:
            raise
        except Exception as error:
            logger.warning("Azure speech request failed: %s", type(error).__name__)
            raise SpeechSynthesisError(
                "speech_provider_failed", "语音服务请求失败，请检查 Azure Speech 配置"
            ) from error

        if result.reason != speechsdk.ResultReason.SynthesizingAudioCompleted:
            detail = "unknown"
            if result.reason == speechsdk.ResultReason.Canceled:
                cancellation = speechsdk.SpeechSynthesisCancellationDetails(result)
                detail = str(getattr(cancellation, "reason", "canceled"))
            logger.warning("Azure speech synthesis did not complete: %s", detail)
            raise SpeechSynthesisError(
                "speech_provider_failed", "语音服务未能生成音频，请检查凭据、区域和语音名称"
            )
        if not result.audio_data:
            raise SpeechSynthesisError(
                "empty_speech_audio", "语音服务返回了空音频"
            )

        return SpeechSynthesisResult(
            audio=bytes(result.audio_data),
            mime_type=self.mime_type,
            visemes=sorted(visemes, key=lambda item: item["offset_ms"]),
            provider=self.provider,
            voice=self.voice,
        )
