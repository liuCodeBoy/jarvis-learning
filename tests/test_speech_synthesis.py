from types import SimpleNamespace

from jarvis.voice import AzureSpeechSynthesizer


class FakeSignal:
    def __init__(self):
        self.callback = None

    def connect(self, callback):
        self.callback = callback


class FakeSpeechConfig:
    def __init__(self, subscription, region=None, endpoint=None):
        self.subscription = subscription
        self.region = region
        self.endpoint = endpoint
        self.speech_synthesis_voice_name = ""
        self.output_format = None

    def set_speech_synthesis_output_format(self, output_format):
        self.output_format = output_format


class FakeAsyncResult:
    def __init__(self, synthesizer, result):
        self.synthesizer = synthesizer
        self.result = result

    def get(self):
        for offset, viseme_id in ((825_000, 6), (0, 0), (1_400_000, 21)):
            self.synthesizer.viseme_received.callback(SimpleNamespace(
                audio_offset=offset,
                viseme_id=viseme_id,
            ))
        return self.result


class FakeSdkSynthesizer:
    def __init__(self, speech_config, audio_config):
        self.speech_config = speech_config
        self.audio_config = audio_config
        self.viseme_received = FakeSignal()

    def speak_text_async(self, _text):
        result = SimpleNamespace(reason="completed", audio_data=b"RIFFtest-wave")
        return FakeAsyncResult(self, result)


def fake_speech_sdk():
    return SimpleNamespace(
        SpeechConfig=FakeSpeechConfig,
        SpeechSynthesizer=FakeSdkSynthesizer,
        SpeechSynthesisOutputFormat=SimpleNamespace(
            Riff24Khz16BitMonoPcm="wav-24khz"
        ),
        ResultReason=SimpleNamespace(
            SynthesizingAudioCompleted="completed",
            Canceled="canceled",
        ),
    )


def test_azure_speech_keeps_audio_and_viseme_offsets_together():
    synthesizer = AzureSpeechSynthesizer(
        subscription_key="secret",
        region="test-region",
        voice="zh-CN-test",
    )
    synthesizer._sdk = fake_speech_sdk()

    result = synthesizer.synthesize("你好")

    assert result.audio == b"RIFFtest-wave"
    assert result.mime_type == "audio/wav"
    assert result.voice == "zh-CN-test"
    assert result.visemes == [
        {"offset_ms": 0.0, "id": 0},
        {"offset_ms": 82.5, "id": 6},
        {"offset_ms": 140.0, "id": 21},
    ]


def test_azure_speech_requires_key_and_region_without_importing_sdk():
    no_key = AzureSpeechSynthesizer("", "test-region")
    no_region = AzureSpeechSynthesizer("secret", "")

    assert no_key.available is False
    assert no_key.reason == "尚未配置 AZURE_SPEECH_KEY"
    assert no_region.available is False
    assert no_region.reason == "尚未配置 AZURE_SPEECH_REGION"
