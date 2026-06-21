import asyncio
import os
import tempfile
import sounddevice as sd
import soundfile as sf
import edge_tts

# Nama target untuk VB-Audio Cable
VIRTUAL_CABLE_NAME = "CABLE Input"
VOICE = "id-ID-GadisNeural" # Suara cewek Indonesia natural, atau "en-US-AriaNeural"

def list_and_find_cable():
    print("=== DAFTAR AUDIO DEVICES ===")
    devices = sd.query_devices()
    cable_id = None
    for i, dev in enumerate(devices):
        name = dev['name']
        max_out = dev['max_output_channels']
        print(f"[{i}] {name} (Output Channels: {max_out})")
        if VIRTUAL_CABLE_NAME.lower() in name.lower() and max_out > 0:
            cable_id = i
    print("============================")
    if cable_id is not None:
        print(f"\n[OK] VB-Audio Virtual Cable DITEMUKAN pada Device ID: {cable_id}")
    else:
        print(f"\n[INFO] VB-Audio Virtual Cable '{VIRTUAL_CABLE_NAME}' TIDAK DITEMUKAN!")
        print("Menggunakan DEFAULT output device komputermu sebagai fallback.")
    return cable_id

import numpy as np

def resample_audio(data, orig_sr, target_sr=44100):
    if orig_sr == target_sr:
        return data, target_sr
    
    duration = len(data) / orig_sr
    target_length = int(duration * target_sr)
    
    orig_xs = np.linspace(0, duration, len(data))
    target_xs = np.linspace(0, duration, target_length)
    
    if len(data.shape) > 1:  # multi-channel
        resampled_channels = []
        for i in range(data.shape[1]):
            resampled_channels.append(np.interp(target_xs, orig_xs, data[:, i]))
        return np.column_stack(resampled_channels), target_sr
    else:  # mono
        return np.interp(target_xs, orig_xs, data), target_sr

async def test_speak(text, device_id):
    print(f"\nMensintesis suara: \"{text}\"...")
    communicate = edge_tts.Communicate(text, VOICE)
    
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
        tmp_path = tmp.name
    
    try:
        await communicate.save(tmp_path)
        raw_data, raw_sr = sf.read(tmp_path)
        
        # Coba beberapa standard sample rates yang biasanya didukung driver Windows
        supported_rates = [48000, 44100, 96000, 24000, 16000]
        success = False
        
        for rate in supported_rates:
            try:
                # Resample ke rate target
                data, samplerate = resample_audio(raw_data, raw_sr, rate)
                
                # Coba mainkan
                sd.play(data, samplerate, device=device_id)
                sd.wait()
                success = True
                print(f"Berhasil memutar suara dengan Sample Rate: {samplerate}Hz")
                break
            except Exception as play_err:
                # Jika gagal, coba rate berikutnya
                continue
                
        if not success:
            raise RuntimeError("Semua percobaan sample rate ditolak oleh driver audio!")
            
    except Exception as e:
        print(f"Error saat memutar audio: {e}")
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)

if __name__ == "__main__":
    cable_device_id = list_and_find_cable()
    
    # Kalimat tes
    test_text = "halo semua, kenalin aku ARTI! halo juga yuki hehe"
    
    # Jalankan tes
    asyncio.run(test_speak(test_text, cable_device_id))
