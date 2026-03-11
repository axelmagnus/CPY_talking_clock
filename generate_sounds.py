from gtts import gTTS
import os
import subprocess

# Directory to save sound files
sound_dir = "sounds"
os.makedirs(sound_dir, exist_ok=True)

# Generate numbers 00-23 (hours)
for i in range(24):
    num_str = f"{i:02d}"
    tts = gTTS(text=str(i), lang='en')
    mp3_path = os.path.join(sound_dir, f"{num_str}.mp3")
    wav_path = os.path.join(sound_dir, f"{num_str}.wav")
    tts.save(mp3_path)
    # Convert to WAV mono 16kHz
    subprocess.run([
        "ffmpeg", "-y", "-i", mp3_path, "-ac", "1", "-ar", "16000", wav_path
    ], check=True)
    os.remove(mp3_path)

# Generate numbers 00-59 (minutes)
for i in range(24, 60):
    num_str = f"{i:02d}"
    tts = gTTS(text=str(i), lang='en')
    mp3_path = os.path.join(sound_dir, f"{num_str}.mp3")
    wav_path = os.path.join(sound_dir, f"{num_str}.wav")
    tts.save(mp3_path)
    subprocess.run([
        "ffmpeg", "-y", "-i", mp3_path, "-ac", "1", "-ar", "16000", wav_path
    ], check=True)
    os.remove(mp3_path)

# Generate 'hours' and 'minutes'
for word in ["hours", "minutes"]:
    tts = gTTS(text=word, lang='en')
    mp3_path = os.path.join(sound_dir, f"{word}.mp3")
    wav_path = os.path.join(sound_dir, f"{word}.wav")
    tts.save(mp3_path)
    subprocess.run([
        "ffmpeg", "-y", "-i", mp3_path, "-ac", "1", "-ar", "16000", wav_path
    ], check=True)
    os.remove(mp3_path)

print("All sound files generated and converted to WAV.")
