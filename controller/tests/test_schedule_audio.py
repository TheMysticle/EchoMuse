"""Exercise the real decoder for every format advertised by schedule settings."""
import asyncio
import hashlib
import shutil
import subprocess

import pytest

from em_schedule import MAX_INPUT, convert_audio


@pytest.mark.parametrize("extension", ["wav", "mp3", "ogg", "opus", "flac", "m4a", "aac"])
def test_supported_sound_formats_decode_to_native_pcm(tmp_path, extension):
    assert shutil.which("ffmpeg"), "FFmpeg is required by the controller"
    path = tmp_path / ("sound." + extension)
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
        "-i", "sine=frequency=440:sample_rate=44100:duration=2",
        "-ac", "2", str(path),
    ], check=True)
    asset, pcm = asyncio.run(convert_audio(path.read_bytes()))
    assert asset == hashlib.sha256(pcm).hexdigest()
    assert 48000 * 2 <= len(pcm) < 48000 * 2 * 3
    assert len(pcm) % 2 == 0
    assert any(pcm)


@pytest.mark.parametrize("data", [b"", b"not an audio file", b"x" * (MAX_INPUT + 1)])
def test_invalid_sound_uploads_are_rejected(data):
    with pytest.raises(ValueError):
        asyncio.run(convert_audio(data))


@pytest.mark.parametrize("extension", ["flac", "m4a"])
def test_long_audio_uses_only_first_43_seconds(tmp_path, extension):
    # Ordinary M4A puts its index at EOF; this exceeds FFmpeg's pipe buffers
    # and requires seeking back to the audio after reading that index.
    path = tmp_path / ("long." + extension)
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
        "-i", "sine=frequency=440:sample_rate=48000:duration=45", str(path),
    ], check=True)
    _, pcm = asyncio.run(convert_audio(path.read_bytes()))
    assert len(pcm) == 43 * 48000 * 2
