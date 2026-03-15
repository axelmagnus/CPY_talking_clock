"""
Air by Bach timing test for PyPortal.

Source used for rhythm reference:
Mutopia Project (public domain), BWV 1068 "Air" LilyPond source:
https://www.mutopiaproject.org/ftp/BachJS/BWV1068/bach_air_bmv_1068/bach_air_bmv_1068.ly

This script focuses on note lengths (durations) for an opening phrase and omits
ornaments (acciaccaturas/trills) so timing can be tuned quickly by ear.
"""

import time
import math
import array
import board
import digitalio

try:
    import synthio
except ImportError:
    synthio = None

try:
    import audiopwmio
except ImportError:
    audiopwmio = None

try:
    import audioio
except ImportError:
    audioio = None


BPM = 72  # LilyPond tempo in the source is quarter note = 72.
BEAT = 60.0 / BPM
VIBRATO_RATE_HZ = 5.2
VIBRATO_DEPTH_CENTS = 10.0

# Approximate opening rhythm from flute line, ornaments removed.
# Tuple format: (MIDI note, beats, amplitude)
# None as note means rest.
PHRASE = (
    (78, 3.0, 0.14),   # F#5 (opening length +50%)
    (78, 0.75, 0.13),  # F#5 (opening length +50%)
    (83, 0.25, 0.12),  # B5 sixteenth
    (79, 0.25, 0.12),  # G5 sixteenth
    (76, 0.25, 0.12),  # E5 sixteenth
    (74, 0.25, 0.12),  # D5 sixteenth
    (73, 0.25, 0.12),  # C#5 sixteenth
    (74, 0.25, 0.12),  # D5 sixteenth
    (73, 1.0, 0.12),   # C#5 quarter
    (69, 1.0, 0.11),   # A4 quarter
    (81, 2.0, 0.13),   # A5 half
    (81, 0.25, 0.12),  # A5 sixteenth
    (78, 0.25, 0.12),  # F#5 sixteenth
    (72, 0.25, 0.11),  # C5 sixteenth
    (71, 0.25, 0.11),  # B4 sixteenth
    (76, 0.25, 0.12),  # E5 sixteenth
    (75, 0.25, 0.11),  # D#5 sixteenth
    (81, 0.25, 0.12),  # A5 sixteenth
    (79, 0.25, 0.12),  # G5 sixteenth
    (79, 2.0, 0.12),   # G5 half
    (78, 1.5, 0.12),   # F#5 dotted quarter
    (79, 0.25, 0.11),  # G5 sixteenth
    (81, 0.25, 0.11),  # A5 sixteenth
    (74, 0.5, 0.10),   # D5 eighth
    (74, 0.125, 0.10), # D5 32nd
    (76, 0.125, 0.10), # E5 32nd
    (78, 0.25, 0.11),  # F#5 sixteenth
    (76, 0.25, 0.11),  # E5 sixteenth
    (76, 0.25, 0.11),  # E5 sixteenth
    (74, 0.25, 0.11),  # D5 sixteenth
    (73, 0.25, 0.11),  # C#5 sixteenth
    (71, 0.25, 0.11),  # B4 sixteenth
    (69, 0.25, 0.11),  # A4 sixteenth
    (81, 0.375, 0.12), # A5 dotted eighth
    (78, 0.125, 0.11), # F#5 32nd
    (72, 0.125, 0.10), # C5 32nd
    (71, 0.125, 0.10), # B4 32nd
    (76, 0.125, 0.11), # E5 32nd
    (75, 0.125, 0.10), # D#5 32nd
    (81, 0.125, 0.11), # A5 32nd
    (79, 0.125, 0.11), # G5 32nd
    (79, 2.0, 0.12),   # G5 half
    (73, 0.25, 0.11),  # C#5 sixteenth
    (71, 0.25, 0.11),  # B4 sixteenth
    (71, 0.125, 0.11), # B4 32nd
    (73, 0.125, 0.11), # C#5 32nd
    (74, 0.25, 0.11),  # D5 sixteenth
    (73, 0.5, 0.11),   # C#5 eighth
    (71, 0.25, 0.11),  # B4 sixteenth
    (69, 2.0, 0.11),   # A4 half
)


def open_audio_out():
    for pin_name in ("SPEAKER", "A0"):
        if not hasattr(board, pin_name):
            continue
        pin = getattr(board, pin_name)
        if audiopwmio is not None:
            try:
                return audiopwmio.PWMAudioOut(pin)
            except Exception:
                pass
        if audioio is not None:
            try:
                return audioio.AudioOut(pin)
            except Exception:
                pass
    return None


def main():
    if synthio is None:
        print("synthio unavailable")
        return

    if hasattr(board, "SPEAKER_ENABLE"):
        spk = digitalio.DigitalInOut(board.SPEAKER_ENABLE)
        spk.direction = digitalio.Direction.OUTPUT
        spk.value = True

    audio = open_audio_out()
    if audio is None:
        print("no working audio output pin")
        return

    env = synthio.Envelope(
        attack_time=0.01,
        decay_time=0.09,
        sustain_level=0.72,
        release_time=0.12,
    )
    warm_wave = array.array("h", [0] * 256)
    for i in range(256):
        phase = (2.0 * math.pi * i) / 256.0
        sample = (
            math.sin(phase)
            + 0.33 * math.sin(2.0 * phase)
            + 0.16 * math.sin(3.0 * phase)
        )
        warm_wave[i] = int(sample * 21000)

    synth = synthio.Synthesizer(sample_rate=22050, envelope=env, waveform=warm_wave)
    audio.play(synth)

    print("Air timing test start; BPM=", BPM)
    print("Press Ctrl+C to stop")

    try:
        while True:
            for midi_note, beats, amp in PHRASE:
                duration = beats * BEAT
                if midi_note is None:
                    time.sleep(duration)
                    continue

                note = synthio.Note(
                    frequency=synthio.midi_to_hz(midi_note),
                    amplitude=1.0,
                )
                synth.press((note,))
                note_start = time.monotonic()
                base_freq = synthio.midi_to_hz(midi_note)
                note_end = note_start + duration
                while time.monotonic() < note_end:
                    lfo_t = time.monotonic() - note_start
                    cents = math.sin(2.0 * math.pi * VIBRATO_RATE_HZ * lfo_t) * VIBRATO_DEPTH_CENTS
                    note.frequency = base_freq * (2.0 ** (cents / 1200.0))
                    time.sleep(0.01)
                synth.release((note,))
                time.sleep(0.02)

            # Small phrase gap to hear loop boundary.
            time.sleep(BEAT * 0.5)
    finally:
        try:
            audio.stop()
        except Exception:
            pass
        try:
            audio.deinit()
        except Exception:
            pass


main()
