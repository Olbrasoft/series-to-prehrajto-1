"""Exercise the real audio decoder without downloading a Whisper model."""

import array
import importlib.util
import math
import tempfile
import unittest
import wave
from pathlib import Path


@unittest.skipUnless(importlib.util.find_spec("faster_whisper"), "Whisper is optional")
class WhisperDependencyTests(unittest.TestCase):
    def test_decode_and_resample_wav(self):
        from faster_whisper.audio import decode_audio

        samples = array.array("h", (
            int(8000 * math.sin(2 * math.pi * 440 * i / 8000))
            for i in range(8000)
        ))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.wav"
            with wave.open(str(path), "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(8000)
                output.writeframes(samples.tobytes())
            decoded = decode_audio(str(path), sampling_rate=16000)

        self.assertEqual(decoded.shape, (16000,))
        self.assertEqual(str(decoded.dtype), "float32")
        self.assertGreater(float(abs(decoded).max()), 0.1)
