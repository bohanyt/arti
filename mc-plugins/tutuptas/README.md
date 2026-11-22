# TutupTas — plugin 30 baris untuk menutup layar tas di siaran

Menutup layar/GUI yang sedang terbuka di klien seorang pemain:

    tutuptas <pemain>

## Kenapa harus ada

Menampilkan isi tas Arti di layar kamera siaran dibuat dengan
`sudo player <kamera> invsee arti_berarti`. Itu MEMBUKA jendela. Masalahnya
menutupnya lagi — dan ini diuji satu per satu di server uji, bukan
diasumsikan:

| cara | hasil |
|---|---|
| `tp` di tempat / geser 20 blok | tidak menutup |
| ganti gamemode | tidak menutup |
| `spectate` entitas lain | tidak menutup |
| efek blindness | tidak menutup |
| naik entitas (`ride mount`) | tidak menutup |
| `damage` | tidak menutup |
| `clear` isi tas | tidak menutup |
| reload plugin invsee | tidak menutup |
| **pindah dimensi** | menutup — tapi merusak siaran |
| **mati** | menutup — tapi merusak siaran |

Bukkit sendiri cuma butuh satu panggilan: `Player#closeInventory()`. Daripada
menambah plugin pihak ketiga lagi, ini ditulis sendiri supaya seluruh isinya
muat dalam satu layar dan bisa diaudit.

Tanpa penutup, GUI-nya menempel di layar penonton SELAMANYA.

## Build (tanpa JDK — cukup JRE yang sudah ada di folder server)

Eclipse Compiler (ecj) berjalan di JRE biasa:

    JRE="$ARTI_MC_SERVER_DIR/jre21/bin/java.exe"   # folder server, contoh C:/Users/Laptop/minecraft-server
    SD="$ARTI_MC_SERVER_DIR"
    curl -sLO https://repo1.maven.org/maven2/org/eclipse/jdt/ecj/3.40.0/ecj-3.40.0.jar
    "$JRE" -jar ecj-3.40.0.jar -nowarn -source 21 -target 21 \
      -cp "$SD/libraries/io/papermc/paper/paper-api/1.21.4-R0.1-SNAPSHOT/paper-api-1.21.4-R0.1-SNAPSHOT.jar" \
      -d out src/arti/tutuptas/TutupTas.java
    cp plugin.yml out/ && (cd out && zip -r ../TutupTas.jar .)

Classpath WAJIB path gaya Windows (`C:/...`). Path gaya MSYS (`/c/...`) tidak
dikenali Java di Windows dan semua import gagal resolve.

Taruh `TutupTas.jar` di `plugins/`, restart server.

## Plugin lain yang dibutuhkan momen ini

* **InvSee++ 0.30.3** — JANGAN versi terbaru: rilis 0.31.x membundel modul
  untuk MC 26.x yang dikompilasi Java 25 (class file 69), dan remapper Paper
  di Java 21 gagal total sehingga plugin tidak dimuat sama sekali.
* **Sudo 1.0.0** — `execute as <p> run invsee` DITOLAK ("This command can only
  be used by players!"): semua plugin invsee mendaftarkan perintahnya lewat
  plugin.yml gaya lama dan menolak ProxiedCommandSender. `sudo` memanggil
  `player.performCommand`, jadi pengirimnya benar-benar pemain itu.
* `permissions.yml` memberi node invsee ke semua pemain — akun kamera
  SENGAJA tidak di-op, dia cuma perlu satu kemampuan itu.
