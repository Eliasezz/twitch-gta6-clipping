"""Generate a fully original royalty-free-style instrumental bed (no sampled songs).

This simple generative synth uses standard chords and a procedural beat.
It is not a licensed trending commercial track and cannot guarantee a viral hit.
"""
import math
import struct
import wave
from pathlib import Path

SAMPLE_RATE = 16000
BPM = 110
CHORDS = (
    (110.00, (220.00, 261.63, 329.63)),   # A minor
    (87.31, (174.61, 220.00, 261.63)),    # F
    (130.81, (261.63, 329.63, 392.00)),   # C
    (98.00, (196.00, 246.94, 293.66)),   # G
)


def make_original_beat(path):
    """Create an 8.7s loop to be mixed quietly below narration in FFmpeg."""
    path = Path(path)
    beat = 60.0 / BPM
    total = beat * 16.0
    count = int(total * SAMPLE_RATE)
    samples = bytearray()
    twopi = 2.0 * math.pi
    for i in range(count):
        t = i / SAMPLE_RATE
        n = min(3, int(t // (4 * beat)))
        root, notes = CHORDS[n]
        local = t % beat
        half = t % (beat / 2)
        pluck_frequency = notes[int(t // (beat / 2)) % 3]
        kick = 0.22 * math.sin(twopi * (57 - 10 * local) * local) * math.exp(-17 * local)
        bass = 0.18 * math.sin(twopi * root * t) * math.exp(-6 * local)
        pluck = 0.10 * math.sin(twopi * pluck_frequency * t) * math.exp(-11 * half)
        pad = 0.013 * sum(math.sin(twopi * freq * t) for freq in notes)
        # Quiet original high-frequency shaker, not a sampled drum recording.
        shaker = 0.014 * math.sin(twopi * 4800 * t) * math.exp(-50 * half)
        val = max(-0.93, min(0.93, kick + bass + pluck + pad + shaker))
        samples.extend(struct.pack("<h", int(val * 32767)))
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(SAMPLE_RATE)
        output.writeframes(samples)
    return path
