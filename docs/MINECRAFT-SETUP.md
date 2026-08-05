# Minecraft — Arti sebagai pemain sungguhan

Arti masuk ke server Minecraft sebagai **pemain**, bukan skrip yang menempel di
klienmu. Server melihatnya seperti orang: punya HP, lapar, bisa mati, bisa
di-aggro mob. Dia "melihat" dunia lewat data server (blok, mob, waktu, posisi) —
bukan menebak dari gambar — jadi pengetahuannya akurat dan gratis.

Semua yang dibutuhkan **gratis**, dan botnya **tidak butuh akun Minecraft**
(server lokal offline-mode). Kamu tetap butuh Minecraft Java asli untuk main
sendiri.

## Bahan

| | |
|---|---|
| **Server** | [PaperMC](https://papermc.io) — versi harus cocok dengan dukungan mineflayer |
| **Java** | [Temurin JDK/JRE 21](https://adoptium.net) (Paper 1.21.x butuh Java 21) |
| **Node.js** | 18+ (untuk bot mineflayer) |
| **Klien** | Minecraft Java Edition, profil versi **sama** dengan server |

> **Versi itu penting.** mineflayer selalu telat beberapa versi dari rilis
> Mojang. Cek versi yang didukung di [README
> mineflayer](https://github.com/PrismarineJS/mineflayer) SEBELUM memilih versi
> server. Setup ini diuji pada **Paper 1.21.4 + mineflayer 4.37.1**. Jangan
> upgrade jar server tanpa mengecek dukungan mineflayer — begitu tidak cocok,
> bot langsung gagal join.

## 1. Server lokal

Taruh **di luar** folder repo (mis. `Documents\minecraft-server\`):

```powershell
# unduh paper-<versi>.jar dari papermc.io, lalu:
java -Xms1G -Xmx2G -jar paper-<versi>.jar nogui
# jalankan sekali -> akan bikin eula.txt -> ubah jadi eula=true -> jalankan lagi
```

`server.properties` yang penting:

```properties
server-ip=127.0.0.1        # WAJIB: kunci ke localhost
online-mode=false          # bot bisa join tanpa akun
view-distance=8            # hemat CPU; kamu lagi live juga
simulation-distance=6
spawn-protection=0
difficulty=normal          # survival = Arti bisa mati = konten
```

> **Kenapa `online-mode=false` aman di sini:** server terkunci di `127.0.0.1`,
> jadi tidak ada yang bisa menyambung dari luar mesinmu. Kalau suatu saat kamu
> buka ke LAN/internet, matikan lagi — offline-mode di jaringan terbuka berarti
> siapa pun bisa masuk memakai nama siapa pun.

## 2. Bot

```powershell
cd mc-bot
npm install
```

## 3. Konfigurasi bridge

Di `config_local.json`:

```json
{
  "minecraft_enabled": true,
  "minecraft_bot_name": "NamaBotKamu",
  "minecraft_streamer_name": "NamaInGameKamu",
  "minecraft_host": "127.0.0.1",
  "minecraft_port": 25565
}
```

`minecraft_streamer_name` harus **persis** nama in-game-mu — dari situ bot tahu
siapa yang harus diikuti, dan chat in-game-mu dihitung sebagai aktivitas manusia.

## 4. Jalankan

1. Nyalakan server, tunggu baris `Done (…)!`
2. Join `localhost` dari klien Minecraft (profil versi yang sama)
3. Nyalakan bridge, lalu ketik di console-nya: `mc on`

Bot join dan otomatis mengikutimu. Kalau kamu tidak ada di dunia, dia jelajah
sendiri.

## Perintah console

| Perintah | Efek |
|---|---|
| `mc on` / `mc off` | Join / keluar dari dunia |
| `mc status` | Kondisi bot + misi yang sedang berjalan |
| `mc goal <teks>` | Pasang misi ("cari desa"); `mc goal clear` untuk batal |
| `mc follow` / `mc roam` | Ikuti streamer / jelajah mandiri |
| `mc come` / `mc stop` | Samperin / diam di tempat |
| `mc say <teks>` | Bot mengetik di chat game |

Arti juga bisa memicu ini sendiri lewat tag di jawabannya
(`[MC: join]`, `[MC: goal …]`, `[MC: roam]`) — dan tag pengubah sesi hanya
diterima kalau perintahnya datang dari **streamer**, bukan penonton.

## Uji tanpa bridge

Dua skrip untuk memastikan lapisan bawah sehat sebelum menyalakan seluruh sistem:

```powershell
venv\Scripts\python scripts\spike_minecraft.py --auto    # bot join + protokol NDJSON
venv\Scripts\python scripts\spike_minecraft_runner.py    # runner asli: join, status, chat, keluar
venv\Scripts\python scripts\spike_minecraft_solo.py      # mode jelajah mandiri (tanpa klien)
```

## Skin custom (opsional)

Di server offline-mode, skin butuh tekstur bertanda tangan. Cara yang berhasil:
unggah PNG-mu ke [MineSkin](https://mineskin.org) untuk mendapat tekstur signed,
lalu pasang lewat plugin [SkinsRestorer](https://skinsrestorer.net).

> **Jebakan:** perintah pemasangan skin hanya menempel kalau pemainnya sedang
> **online**. Suruh bot join dulu, baru jalankan perintahnya.

## Beban mesin

Terukur pada laptop saat live: server ±1,5 GB RAM, bot Node kecil, dan **cuma
satu instance Minecraft** — punyamu. Bot tidak merender apa pun; dia bicara
langsung ke server lewat protokol jaringan.

Kalau berat, turunkan `view-distance` dan pakai mod performa sisi-klien
(Sodium/Lithium/FerriteCore) di klienmu sendiri.

## Batasan jujur

- Arti **belum bisa menambang atau membangun** — saat ini dia bisa jalan,
  mengikuti, menjelajah, bereaksi, dan mengetik di chat.
- Dia **belum bisa makan**, jadi di sesi panjang lapar terus berkurang.
- Pathfinding-nya standar mineflayer: parkour presisi dan PvP lawan manusia
  bukan keahliannya.
