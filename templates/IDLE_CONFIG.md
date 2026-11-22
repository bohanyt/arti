# IDLE ANIMATION CONFIG — Arti VTuber
# Configuration untuk RNG-based idle animation system
#
# CARA PAKAI (ekspresi kamu sendiri; berkas exp3.json-nya tidak ada di repo ini):
# 1. Buat ekspresi ArtiIdle*/Arti* di VTS Expression Editor untuk model kamu
# 2. Simpan di folder model VTS kamu dengan nama berkas yang sama
# 3. Adjust parameter values sesuai kebutuhan
# 4. Tambah hotkey untuk manual trigger (optional)
#
# IDLE ANIMATION SYSTEM:
# - ArtiIdle1: Celingak kanan (kepala miring kanan + mata kanan)
# - ArtiIdle2: Celingak kiri (kepala miring kiri + mata kiri)
# - ArtiIdle3: Lihat atas (kepala naik + mata atas)
# - ArtiIdle4: Lihat bawah (kepala turun + mata bawah)
#
# RNG TIMER SETTINGS (rancangan; kunci ini belum ada di arti_bridge.py):
# - idle_check_interval: 5-15 detik (random) — berapa lama sebelum cek mau idle
# - idle_action_chance: 30-50% — probability Arti gerak saat idle
# - idle_hold_duration: 2-4 detik — berapa lama pose idle ditahan
# - follow_mouse_interval: 0.1-0.5 detik — berapa sering cek mouse position
# - follow_mouse_smooth: 0.3 — smoothing factor (0=instant, 1=very smooth)
#
# OFFLINE STATE (rancangan, belum ada di kode):
# - Pas bridge nggak konek ke VTS → Arti ngilang (hide model)
# - Pas konek lagi → Arti muncul lagi
#
# FOLLOW MOUSE (rancangan, belum ada di kode):
# - Mata Arti mengikuti cursor mouse di OBS
# - ParamEyeBallX dan ParamEyeBallY di-adjust berdasarkan mouse position
# - Smooth movement biar nggak kaku
#
# EMOTION EXPRESSIONS:
# - ArtiSenyum: Mata senyum + mulut tersenyum
# - ArtiSedih: Mata turun + alis sedih + mulut cemberut
# - ArtiMarah: Mata menyipit + alis marah + mulut ketat
# - ArtiBingung: Kepala miring + mata lebar + mulut O
# - ArtiExcited: Mata lebar senyum + mulut terbuka
#
# NOTE: Parameter values di atas adalah ESTIMASI.
# Model Live2D kamu mungkin punya parameter names yang berbeda.
# Silakan adjust di VTS Expression Editor untuk hasil optimal.
