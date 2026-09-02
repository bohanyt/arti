// Bot Minecraft Arti — mineflayer, dikendalikan bridge Python via NDJSON stdio.
//
// Kontrak protokol (Phase 0):
//   stdin  : satu JSON per baris  {"cmd": "follow|come|say|stop|status|quit", ...}
//   stdout : satu JSON per baris  {"ev": "...", "ts": <epoch detik>, ...}  — STERIL,
//            tidak ada log lain di stdout (reader Python mengabaikan non-JSON,
//            tapi jangan sengaja).
//   stderr : log manusia (diteruskan bridge dengan prefix [MC-bot]).
//
// TANPA reconnect di sini: kicked/error fatal => emit + exit(1). Respawn
// subprocess (backoff + deadman) adalah urusan Python — satu tempat kebijakan.

const mineflayer = require('mineflayer')
const { pathfinder, Movements, goals } = require('mineflayer-pathfinder')
const readline = require('readline')
const { Vec3 } = require('vec3')
// Nambang + ambil barangnya dalam satu panggilan. Ikut membawa
// mineflayer-tool (pilih alat terbaik otomatis), jadi dia tidak menebang
// pohon pakai tangan kalau punya kapak.
const collectBlock = require('mineflayer-collectblock').plugin
// Busur auto-aim (probe [date removed]: autoAttack menggerus sapi 100 HP ke 2.0
// dalam 12 dtk pada jarak 14 blok — bidikannya layak dipakai).
const hawkeye = require('minecrafthawkeye')

function arg(name, fallback) {
  const i = process.argv.indexOf('--' + name)
  return i >= 0 && process.argv[i + 1] ? process.argv[i + 1] : fallback
}

const HOST = arg('host', '127.0.0.1')
const PORT = parseInt(arg('port', '25565'), 10)
const USERNAME = arg('username', 'Arti')
const STREAMER = arg('streamer', 'streamer')
const STATUS_SEC = parseInt(arg('status-interval', '10'), 10)
// POV penonton (Phase 3): prismarine-viewer menyiarkan pandangan Arti ke
// halaman web lokal, lalu OBS memasangnya sebagai Browser Source. 0 = mati.
const POV_PORT = parseInt(arg('pov-port', '0'), 10)
const POV_VIEW_DISTANCE = parseInt(arg('pov-view-distance', '8'), 10)
const REFLEKS_AKTIF = arg('refleks', '1') !== '0'
// MODE TAMU ([date removed]) — main di server/dunia ORANG LAIN (mabar via e4mc).
// Permintaan operator: "aku berharap dia ga rusak-rusakin, rada passive dan
// ikutin aku aja". Semua aksi yang MENGUBAH DUNIA diblokir — menambang,
// menaruh blok, membangun, menggali, furnace, peti. Pengecualian SATU: tidur
// (naruh bed + pakai) — operator eksplisit mau Arti bisa respawn di bed.
// Kabur/makan/follow/roam/ngobrol tetap hidup: tamu yang sopan, bukan patung.
const MODE_TAMU = arg('tamu', '0') === '1'
// Aksi terlarang saat tamu; cek di dispatcher, SATU pintu — bukan disebar ke
// tiap handler (gampang bolong waktu handler baru lahir).
const TAMU_TERLARANG = new Set([
  'mine', 'place', 'bangun', 'turun', 'jembatan', 'menara', 'lubang_aman',
  'mundur_tembok', 'masak', 'craft', 'siapkan_alat', 'simpan', 'ambil',
  'portal', 'portal_cor', 'cor_uji',
])
const POV_MODE = arg('pov-mode', 'putar')       // pertama|belakang|depan|putar
const POV_CYCLE_SEC = parseFloat(arg('pov-cycle-sec', '20'))
const POV_BODY_SEC = parseFloat(arg('pov-body-sec', '4'))
const POV_SLIM = arg('pov-slim', '1') !== '0'
const POV_SMOOTH = parseFloat(arg('pov-smooth', '0.6'))
// Kabur. Di log 6-7 Agustus Arti mati 4x dalam 8 menit sambil terus jalan
// dikejar skeleton — dia tidak pernah menoleh, mundur, atau melawan.
const FLEE_HP = parseInt(arg('flee-hp', '10'), 10)   // 0 = kabur otomatis mati
// Batas satu episode bertarung. Tanpa ini dia bisa mengejar mob yang lari
// selamanya; dengan ini dia menyerah dan melapor jujur "lepas".
const SERANG_BATAS_MS = 30000
const SERANG_JANGKAUAN = 3.0
// Sesudah berhasil craft dia BERDIRI DIAM sebentar menatap hasilnya, selama
// panel resepnya masih tampil. Tanpa ini dia langsung ngeloyor dan panelnya
// ikut meluncur sambil dia jalan.
const CRAFT_PAUSE_MS = Math.max(0, Math.min(20,
  parseFloat(arg('craft-pause-sec', '5')) || 0)) * 1000

// Dependensi (mineflayer & kawan-kawan) kadang mencetak stack trace ke
// STDOUT — padahal stdout adalah kanal protokol yang harus steril. Audit
// [date removed] menemukan 15 baris sampah masuk ke sana saat koneksi putus.
// Python memang membuangnya diam-diam, tapi diagnosisnya ikut hilang.
console.log = (...a) => process.stderr.write(a.join(' ') + '\n')

function emit(obj) {
  process.stdout.write(JSON.stringify({ ts: Date.now() / 1000, ...obj }) + '\n')
}
function log(...args) {
  process.stderr.write(args.join(' ') + '\n')
}

const bot = mineflayer.createBot({
  host: HOST,
  port: PORT,
  username: USERNAME,
  auth: 'offline',
})
bot.loadPlugin(pathfinder)
bot.loadPlugin(collectBlock)
bot.loadPlugin(hawkeye.default || hawkeye)

let currentTask = 'idle'
// null = BELUM tahu HP sebenarnya. Audit [date removed]: nilai awal 20 membuat
// paket kesehatan PERTAMA sesudah login (HP tersimpan dari sesi lalu, mis. 4)
// terbaca sebagai luka mendadak -> "hurt" + "low_health" palsu -> Arti panik
// "aku sekarat!" di detik pertama dia join. Terjadi di 4 dari 4 relog.
let lastHealth = null
let movements = null
// Mode solo (permintaan operator [date removed]: "literally dia yang ambil alih 1
// stream"): kalau streamer TIDAK ada di dunia, bot jangan mematung — jelajah
// sendiri di sekitar titik home biar ada yang dikomentari & dilihat penonton.
let homePos = null
let roamTarget = null
let roamSince = 0
let roamManual = false
const ROAM_RADIUS = 48      // jarak jelajah maksimum dari home (blok)
const ROAM_TIMEOUT_MS = 45000  // satu tujuan gagal/kelamaan -> pilih tujuan lain
// Masa tenggang sesudah spawn sebelum berani bilang "streamer tidak ada".
// Bug live [date removed]: begitu spawn, bot.players[STREAMER].entity BELUM
// termuat walau operator sudah di dunia sejak 2 menit sebelumnya -> bot lapor
// "operator lagi nggak ada di dunia game" dan mulai jelajah sendiri. Nyaris
// terucap di depan penonton padahal operator berdiri di sebelahnya.
const STREAMER_GRACE_MS = 15000
const COME_TIMEOUT_MS = 25000   // `come` yang tak sampai wajib lapor, bukan diam
// BERTAHAN di balik tembok ([date removed]). Terlihat di harness & live [time removed]:
// refleks menembok diri -> `bangun` selesai -> setFollow -> roam LANGSUNG
// jalan -> semua kandidat tertolak (dia di DALAM tembok sendiri) -> dicap
// terkurung -> gali keluar -> keluar -> dipukuli -> menembok lagi... muter.
// Selesai menembok saat musuh masih dekat = DIAM DULU di dalam sampai aman.
const BERTAHAN_PERPANJANG_MS = 15000

// ---- TUGAS DISELA LALU DIKEMBALIKAN ([date removed]) ----------------------------
// Sampai sekarang aturannya "perintah terakhir menang": Arti lagi menambang di
// titik X, viewer minta sesuatu, dan titik X terlupakan SELAMANYA. Yang
// menariknya kembali cuma nudge/takdir — dan itu arah umum, bukan pekerjaan
// konkret yang tadi. Sekarang perintah panjangnya diingat utuh (beserta blok &
// jumlahnya) lalu diterbitkan ulang sesudah penyelanya selesai.
const TUGAS_BISA_LANJUT = new Set(
  ['mine', 'turun', 'jembatan', 'bangun', 'masak', 'menara', 'siapkan_alat'])
// Perintah yang artinya "sudahi", bukan "sebentar ya" (keputusan operator [date removed]):
// kalau dia bilang stop lalu 40 detik kemudian Arti balik menambang sendiri,
// itu bikin kesal. `come`/`follow` = dia minta perhatian penuh. `goal` = misi
// baru menggantikan pekerjaan lama. `kabur`/`serang` SENGAJA tidak di sini:
// justru sesudah lari dari zombie, balik ke tambang itu yang paling masuk akal.
const PEMBATAL_LANJUT = new Set(
  ['stop', 'leave', 'quit', 'pulang', 'tidur', 'come', 'follow', 'goal', 'goal_done'])
const LANJUT_KEDALUWARSA_MS = 180000  // 3 menit; lebih dari itu dunianya sudah beda

// Naik tiap perintah masuk. Tugas panjang menyimpan nilainya saat mulai dan
// berhenti sendiri begitu ketinggalan — tanpa ini dua aksi jalan BERSAMAAN dan
// saling rebut badan bot (loop `mine` tidak pernah memeriksa siapa pemilik giliran).
let epokTugas = 0
let perintahAktif = null   // perintah panjang yang sedang dikerjakan
let tugasTerpotong = null  // { c, at } — menunggu dilanjutkan
// Berapa perintah yang promise-nya BELUM selesai — termasuk yang tidak bisa
// dilanjut (`serang`, `kabur`, `come`). Tanpa hitungan ini, penyambungan bisa
// menyalakan tugas lama SEMENTARA penyelanya masih berjalan: nambang berhenti,
// diingat, lalu promise-nya selesai duluan -> Arti balik menambang di tengah
// perkelahian. Ditemukan saat crosscheck [date removed], sebelum sempat di-commit.
let perintahJalan = 0
const BERTAHAN_MUSUH_DEKAT = 12
let bertahanSampai = 0
let comeSince = 0
let spawnedAt = 0
let streamerSeenAt = 0

function player(name) {
  // bot.players bisa undefined di jendela sempit sebelum spawn / sesudah
  // koneksi putus — terlihat live [date removed]: `error (cmd:follow): Cannot
  // read properties of undefined (reading 'streamer_test')`.
  const e = bot.players && bot.players[name] && bot.players[name].entity
  if (!e) return null
  // ENTITY HANTU. Audit verifikasi [date removed]: mineflayer TIDAK membersihkan
  // `players[nama].entity` saat pemain pindah dimensi / keluar jangkauan.
  // Akibatnya bot "menemukan" operator yang sedang di Nether, memancarkan
  // roam_end "streamer_back" PALSU, lalu follow ke koordinat hantu dan
  // MEMATUNG — terbukti di log: `task:follow` dengan `nearby_players: []`
  // selama 80 detik. `come` juga melapor "sampai" ke posisi hantu itu.
  if (e.isValid === false) return null
  if (!bot.entities[e.id]) return null          // sudah dilepas dari dunia
  if (!e.position || !bot.entity) return null
  // Pagar terakhir: entity yang jaraknya di luar akal (beda dimensi biasanya
  // menyisakan koordinat lama) bukan kehadiran nyata.
  if (bot.entity.position.distanceTo(e.position) > 128) return null
  return e
}

function streamerHere() {
  const here = Boolean(player(STREAMER))
  if (here) streamerSeenAt = Date.now()
  return here
}


function setFollow() {
  // Fase bertahan: dia baru menembok diri dan musuhnya masih dekat — DIAM,
  // jangan roam keluar dari perlindungan yang barusan dia bangun.
  if (Date.now() < bertahanSampai) {
    currentTask = 'bertahan'
    bot.pathfinder.setGoal(null)
    return
  }
  const target = player(STREAMER)
  if (!target) {
    // Streamer belum kelihatan. JANGAN buru-buru menyimpulkan dia tidak ada:
    // (a) namanya masih terdaftar di bot.players = dia di dunia, entity-nya
    //     saja yang belum termuat;
    // (b) beberapa detik pertama sesudah spawn, dunia memang belum lengkap.
    // Dua-duanya cuma perlu ditunggu — ticker 5 dtk akan mencoba lagi.
    // Menunggu HANYA selama masa tenggang, dihitung dari kapan entity
    // streamer TERAKHIR benar-benar terlihat. Audit [date removed]: dulu syarat
    // tunggu ikut membaca daftar tab pemain, jadi selama operator online DI MANA
    // PUN (Nether, End, 300 blok jauhnya) bot menunggu SELAMANYA — terbukti
    // 90 detik mematung di koordinat yang sama persis, nol event, penonton
    // cuma melihat patung.
    const acuan = Math.max(streamerSeenAt, spawnedAt)
    if (Date.now() - acuan < STREAMER_GRACE_MS) {
      // Status TERSENDIRI, bukan 'idle': ticker harus mencoba lagi (kalau
      // dibiarkan idle, bot mematung selamanya), sedangkan 'idle' hasil
      // perintah `stop` memang harus dibiarkan diam.
      currentTask = 'wait_streamer'
      bot.pathfinder.setGoal(null)
      return
    }
    // currentTask sengaja TIDAK di-set 'follow' di sini: kalau di-set, ticker
    // 5 dtk memanggil setFollow() lagi -> roam_start beruntun tiap 5 dtk.
    startRoam('streamer_absent')
    return
  }
  currentTask = 'follow'
  roamTarget = null
  // Dicatat DI SINI juga, bukan cuma di streamerHere(): dulu streamerSeenAt
  // hanya diperbarui di cabang roam, jadi selama mode follow nilainya tetap 0
  // dan masa tenggang 15 dtk praktis cuma berlaku sesudah spawn.
  streamerSeenAt = Date.now()
  bot.pathfinder.setGoal(new goals.GoalFollow(target, 3), true)
}

function pickRoamTarget(minJarak, maksJarak) {
  const base = homePos || bot.entity.position
  const angle = Math.random() * Math.PI * 2
  const lo = minJarak || 12
  const hi = maksJarak || ROAM_RADIUS
  const dist = lo + Math.random() * Math.max(1, hi - lo)
  return {
    x: Math.round(base.x + Math.cos(angle) * dist),
    y: Math.round(base.y),
    z: Math.round(base.z + Math.sin(angle) * dist),
  }
}

// Tujuan jelajah dipilih dari beberapa kandidat, dan cuma yang JALURNYA
// BENAR-BENAR ADA yang dipakai. `getPathTo` menghitung A* tanpa menggerakkan
// dia, jadi ini murni pemeriksaan.
//
// TERUKUR di dua sesi live [date removed] (1,5 jam + 1,8 jam): 110 dan 101 kali
// dinyatakan nyangkut, plus 45 dan 23 kali kecebur. Sebabnya satu: tujuan acak
// di medan alami sering tidak terjangkau — di dalam pohon, seberang air, atas
// tebing. Dia mematung 20 detik, dinyatakan nyangkut, pilih tujuan acak baru,
// ulangi. Akibatnya 51 dari 152 tag-nya cuma `roam`, dan komentarnya berulang
// "kakimu ngambek".
//
// Pemeriksaan ini juga mengurangi kecebur dengan sebab yang sama: pathfinder
// MENOLAK simpul di dalam air, jadi jalur menyeberang air tidak pernah
// berstatus 'success' dan tujuan itu langsung dibuang.
// Tujuan jelajah dipilih dari beberapa kandidat, dan hanya yang JALURNYA
// BENAR-BENAR ADA yang dipakai. `getPathTo` menghitung A* tanpa menggerakkan
// dia, jadi ini murni pemeriksaan.
//
// Bisa dimatikan lewat env ROAM_TANPA_CEK=1 — dipakai untuk A/B, karena
// "kelihatannya membantu" bukan bukti (lihat catatan hasil di bawah).
const ROAM_COBA = 3
const ROAM_CEK_MS = 60
const ROAM_CEK_AKTIF = process.env.ROAM_TANPA_CEK !== '1'

function terjangkau(t) {
  if (!ROAM_CEK_AKTIF || !movements) return true
  try {
    const hasil = bot.pathfinder.getPathTo(
      movements, new goals.GoalNear(t.x, t.y, t.z, 3), ROAM_CEK_MS)
    return Boolean(hasil && hasil.status === 'success')
  } catch (e) {
    return false
  }
}

// ---------- COR OBSIDIAN: ember lava + air (rute nether tanpa diamond) ----
// Mekanika [time removed]: air MENGALIR ke sel berisi SUMBER lava -> obsidian.
// Semua hasil diverifikasi dari dunia (blockAt), bukan dari niat klik.

async function pegang(namaItem) {
  const it = bot.inventory.items().find((x) => x.name === namaItem)
  if (!it) return false
  try {
    await bot.equip(it, 'hand')
    return true
  } catch (e) {
    return false
  }
}

// Klik-kanan ember sambil menatap titik persis — activateItem memakai arah
// pandang server-side, jadi lookAt WAJIB force + jeda kecil.
async function klikEmber(titik) {
  await bot.lookAt(titik, true)
  await new Promise((r) => setTimeout(r, 150))
  bot.activateItem()
  await new Promise((r) => setTimeout(r, 350))
}

// Ciduk cairan sumber terdekat (lava/water) dalam jangkauan; bucket kosong
// harus di tangan. Sukses = nama item ember berubah (bukti inventaris).
async function cidukCairan(jenis) {
  const target = bot.findBlock({
    matching: (b) => b && b.name === jenis &&
      (b.getProperties().level === 0 || b.metadata === 0),
    maxDistance: 4,
  })
  if (!target) return `tidak_ada_${jenis}_dekat`
  if (!(await pegang('bucket'))) return 'tidak_punya_bucket'
  // Bidik PERMUKAAN ATAS sumber, bukan pusat blok — ray ke pusat bisa
  // menyerempet aliran tetangga duluan dan cidukannya kosong (uji #8:
  // jarak 3.2, klik 3x, ember tetap kosong).
  await klikEmber(target.position.offset(0.5, 0.85, 0.5))
  const mau = jenis === 'lava' ? 'lava_bucket' : 'water_bucket'
  return jumlahDiTas(mau) > 0 ? '' : 'gagal_menciduk'
}

// Taruh scaffold DI SEL ALIRAN (menimpa cairan mengalir). naruhSatu
// MENOLAK sel cairan by design (bolehDitimpa) — benar untuk pemakaian
// normalnya, tapi menimpa aliran adalah cara sah pemain mencetak pijakan
// di tepi kolam lava. Sukses dibaca dari dunia.
async function cetakPijakan(cel, jenis) {
  const alasnya = bot.blockAt(cel.offset(0, -1, 0))
  if (!alasnya || alasnya.boundingBox !== 'block') return false
  try {
    await bot.equip(jenis.id, 'hand')
    await bot.placeBlock(alasnya, new Vec3(0, 1, 0))
  } catch (e) { /* hasil dicek dari dunia, bukan dari lemparan */ }
  await new Promise((r) => setTimeout(r, 250))
  const jadi = bot.blockAt(cel)
  return Boolean(jadi && jadi.boundingBox === 'block')
}

// Jalan sampai dekat sebuah posisi lalu berhenti. true = tiba.
async function dekati(pos, radius, batasMs) {
  bot.pathfinder.setGoal(new goals.GoalNear(pos.x, pos.y, pos.z, 1), false)
  const tenggat = Date.now() + (batasMs || 8000)
  while (Date.now() < tenggat &&
         bot.entity.position.distanceTo(pos) > radius) {
    await new Promise((r) => setTimeout(r, 150))
  }
  bot.pathfinder.setGoal(null)
  return bot.entity.position.distanceTo(pos) <= radius + 0.4
}

// Cari sumber cairan yang BERTEPI PIJAKAN (punya tetangga padat yang bisa
// dipijak) — sumber di tengah kolam tidak bisa didekati <=2.2 (uji #9:
// mentok 6.5). nearest-first dari findBlocks.
function sumberBertepi(jenis, jarak) {
  const daftar = bot.findBlocks({
    matching: (b2) => b2 && b2.name === jenis &&
      (b2.getProperties().level === 0 || b2.metadata === 0),
    maxDistance: jarak || 16,
    count: 60,
  })
  // Pijakan = sel bersih (lantai padat, badan+kepala bukan cairan) dalam
  // radius 2 dari sumber — masih dalam jangkauan klik (~2.2). Persis
  // bersebelahan sering MUSTAHIL: tepian kolam tertutup aliran (uji #11:
  // 'lava_kurang' padahal kolamnya utuh), dan aliran ber-boundingBox
  // 'empty' jadi wajib dicek nama cairannya (uji #10: pijakan = aliran).
  const opsi = [[1, 0], [-1, 0], [0, 1], [0, -1],
                [1, 1], [1, -1], [-1, 1], [-1, -1],
                [2, 0], [-2, 0], [0, 2], [0, -2]]
  for (const v of daftar) {
    for (const [dx, dz] of opsi) {
      const lantai = bot.blockAt(v.offset(dx, -1, dz))
      const badan = bot.blockAt(v.offset(dx, 0, dz))
      const kepala = bot.blockAt(v.offset(dx, 1, dz))
      if (lantai && lantai.boundingBox === 'block' &&
          !CAIRAN.has(lantai.name) &&
          badan && badan.boundingBox === 'empty' && !CAIRAN.has(badan.name) &&
          kepala && kepala.boundingBox === 'empty' &&
          !CAIRAN.has(kepala.name)) {
        return { sumber: v, pijakan: v.offset(dx, 0, dz) }
      }
    }
  }
  return null
}

// Tuang isi ember ke SEL target dengan mengklik permukaan atas blok padat
// di bawahnya (cairan jatuh ke sel di atas permukaan yang diklik).
async function tuangKe(sel, namaEmber) {
  if (!(await pegang(namaEmber))) return `tidak_punya_${namaEmber}`
  const alas = bot.blockAt(sel.offset(0, -1, 0))
  if (!alas || alas.boundingBox !== 'block') return 'tanpa_alas'
  await klikEmber(alas.position.offset(0.5, 1.0, 0.5))
  return ''
}

// Nyalakan bingkai portal di (bx,by,bz) — flint & steel di alas dalam,
// verifikasi nether_portal DARI DUNIA. Return '' sukses / alasan gagal.
async function nyalakanPortal(bx, by, bz) {
  const pemantik = bot.inventory.items().find((it) => it.name === 'flint_and_steel')
  if (!pemantik) return 'tidak_ada_pemantik'
  await bot.equip(pemantik, 'hand')
  const alas = bot.blockAt(new Vec3(bx, by, bz))
  try {
    await bot.activateBlock(alas, new Vec3(0, 1, 0))
  } catch (e) { /* nyala dicek dari dunia, bukan dari lemparan */ }
  await new Promise((r) => setTimeout(r, 1500))
  const nyala = [0, 1].some((i) =>
    [1, 2, 3].some((dy) => {
      const b = bot.blockAt(new Vec3(bx + i, by + dy, bz))
      return b && b.name === 'nether_portal'
    }))
  return nyala ? '' : 'gagal_menyala'
}

// ---------- GALI KELUAR: pintu darurat epidemi nyangkut ----------
// operator [date removed]: "dia masih suka nyangkut". Lompat + ganti tujuan (lapis
// pertama) tidak menolong kalau dia TERKURUNG — batang spruce, ceruk tebing,
// sudut 1x1. Lapis kedua: bongkar penghalangnya. Dia hampir selalu bawa alat
// (tangga aman dimulai dari pickaxe), jadi menggali 1-2 blok ke arah terbuka
// itu murah dan jujur — bloknya benar-benar hilang dari dunia.
const GALI_KELUAR_JEDA_MS = 60000
let galiKeluarRiwayat = []          // timestamp; rem maksimal 3x per menit
let sedangGaliKeluar = false

async function galiKeluar(sebab) {
  // Tamu tidak menggali dunia orang — nyangkut ya jalan memutar/blacklist.
  if (MODE_TAMU) return false
  if (sedangGaliKeluar || !bot.entity) return false
  const now = Date.now()
  galiKeluarRiwayat = galiKeluarRiwayat.filter((t) => now - t < GALI_KELUAR_JEDA_MS)
  if (galiKeluarRiwayat.length >= 3) return false // buntu beneran: biar nudge/refleks ambil alih
  galiKeluarRiwayat.push(now)
  sedangGaliKeluar = true
  try {
    const pos = bot.entity.position.floored()
    const cair = (b) => Boolean(b && (b.name === 'water' || b.name === 'lava' || b.liquid))
    const padat = (b) => Boolean(b && b.boundingBox === 'block' && bot.canDigBlock(b))
    for (const [dx, dz] of [[1, 0], [-1, 0], [0, 1], [0, -1]]) {
      const mata = bot.blockAt(pos.offset(dx, 1, dz))
      const kaki = bot.blockAt(pos.offset(dx, 0, dz))
      // Arah yang layak: MENGHALANGI (ada blok padat) dan bebas cairan —
      // air/lava menyembur balik itu lebih buruk daripada tetap nyangkut.
      if (cair(mata) || cair(kaki)) continue
      if (!padat(mata) && !padat(kaki)) continue
      let tembus = 0
      for (const b of [mata, kaki]) {
        if (!padat(b)) continue
        try {
          if (bot.tool) await bot.tool.equipForBlock(b, { requireHarvest: false })
          await bot.dig(b)
          tembus++
        } catch (e) { break }
      }
      if (tembus > 0) {
        log(`gali keluar (${sebab}): ${tembus} blok arah ${dx},${dz} dibongkar`)
        return true
      }
    }
    log(`gali keluar (${sebab}): tidak ada arah yang bisa dibongkar`)
    if (sebab === 'terkurung') {
      // Tidak ada SATU PUN blok padat di sekelilingnya = dia di tempat
      // TERBUKA; vonis "terkurung" tadi salah — yang memblokir semua
      // kandidat adalah daftar hitam titik nyangkut yang sudah basi.
      // Bersihkan yang dekat supaya loop terkurung-palsu putus di sini.
      const pos = bot.entity.position
      const sebelum = titikNyangkut.length
      titikNyangkut = titikNyangkut.filter((sp) =>
        Math.hypot(pos.x - sp.x, pos.z - sp.z) > 24)
      if (titikNyangkut.length < sebelum) {
        log(`daftar hitam nyangkut dibersihkan (${sebelum - titikNyangkut.length} titik) — area ini terbuka`)
      }
    }
    return false
  } finally {
    sedangGaliKeluar = false
  }
}

// Titik nyangkut diingat 5 menit: kandidat roam di dekatnya ditolak, supaya
// dia tidak memilih ulang tujuan yang barusan membuatnya mematung.
const NYANGKUT_INGAT_MS = 5 * 60 * 1000
// 5, bukan 8: terlihat di harness [date removed] — radius 8 x sampai 20 titik
// meracuni seluruh cincin kandidat (6-16 blok), dan dia divonis "terkurung"
// di TEMPAT TERBUKA berulang-ulang (gali: "tidak ada arah yang bisa
// dibongkar" = buktinya). Radius kecil cukup untuk menghindari jebakan
// persisnya tanpa mengharamkan seluruh lingkungan.
const NYANGKUT_RADIUS = 5
let titikNyangkut = []
let nyangkutStreak = 0
let nyangkutPosTerakhir = null

function dekatTitikNyangkut(t) {
  const now = Date.now()
  titikNyangkut = titikNyangkut.filter((sp) => now - sp.ts < NYANGKUT_INGAT_MS)
  return titikNyangkut.some((sp) => Math.hypot(t.x - sp.x, t.z - sp.z) < NYANGKUT_RADIUS)
}

function pilihTujuanTerjangkau() {
  // Dunia belum termuat (barusan respawn / teleport jauh): SEMUA pemeriksaan
  // jalur pasti gagal instan dan dia divonis "terkurung" di udara kosong —
  // terlihat berulang pasca-death di harness [date removed]. Jalan dulu ke
  // kandidat mentah; pemilihan normal kembali begitu chunk-nya ada.
  const kaki = bot.blockAt(bot.entity.position.floored().offset(0, -1, 0))
  if (!kaki) return pickRoamTarget()
  let ditolak = 0
  for (let i = 0; i < ROAM_COBA; i++) {
    const t = pickRoamTarget()
    if (!dekatTitikNyangkut(t) && terjangkau(t)) {
      if (ditolak) log(`roam: ${ditolak} ditolak, pakai kandidat ke-${i + 1}`)
      return t
    }
    ditolak++
  }
  // Ronde dekat: kandidat jauh (berpusat di rumah) habis — coba langkah kecil
  // di sekitar POSISI DIA. Dulu jalur ini "jalan ke yang terakhir", yaitu ke
  // tujuan yang SUDAH TERBUKTI mustahil: resep pasti mematung 20 detik lalu
  // dicap nyangkut (terukur berulang di live [date removed]).
  const pos = bot.entity.position
  for (let i = 0; i < ROAM_COBA; i++) {
    const sudut = Math.random() * Math.PI * 2
    const jarak = 6 + Math.random() * 10
    const t = {
      x: Math.round(pos.x + Math.cos(sudut) * jarak),
      y: Math.round(pos.y),
      z: Math.round(pos.z + Math.sin(sudut) * jarak),
    }
    if (!dekatTitikNyangkut(t) && terjangkau(t)) {
      log(`roam: ronde dekat menang (${ditolak} kandidat jauh ditolak)`)
      return t
    }
    ditolak++
  }
  log(`roam: ${ditolak} kandidat ditolak semua — dia terkurung, gali keluar`)
  return null
}

function startRoam(reason) {
  if (!bot.entity) return
  if (currentTask !== 'roam') {
    currentTask = 'roam'
    emit({ ev: 'roam_start', reason: reason || 'manual' })
  }
  roamTarget = pilihTujuanTerjangkau()
  roamSince = Date.now()
  if (!roamTarget) {
    // Semua arah buntu berarti ganti tujuan tidak akan menolong: bongkar
    // kurungannya dulu. Tapi kalau galinya GAGAL, jangan mematung — A/B
    // [date removed] terukur: diam dengan task 'roam' (15 nyangkut) LEBIH BURUK
    // daripada jalan ke tujuan yang mustahil (10) — bergerak memancing
    // keadaan baru, mematung cuma memancing detektor nyangkut.
    galiKeluar('terkurung').then((tembus) => {
      if (currentTask !== 'roam') return
      if (tembus) { startRoam('lolos_gali'); return }
      roamTarget = pickRoamTarget()
      roamSince = Date.now()
      bot.pathfinder.setGoal(
        new goals.GoalNear(roamTarget.x, roamTarget.y, roamTarget.z, 3), false)
    })
    return
  }
  // GoalNear: cukup "sampai sekitar sana" — target acak bisa saja di dalam
  // tebing/air, jangan sampai bot ngotot pada satu blok mustahil.
  bot.pathfinder.setGoal(
    new goals.GoalNear(roamTarget.x, roamTarget.y, roamTarget.z, 3), false)
}

// KELUAR DARI AIR. operator melihatnya tenggelam [date removed], dan sebabnya bukan
// sekadar "belum ada perilaku berenang": mineflayer-pathfinder MENOLAK simpul
// di dalam air (`if (blockC.liquid) return // dont go underwater`). Jadi
// begitu Arti tercebur, tidak ada jalur yang bisa dihitung sama sekali — dia
// mematung di dalam air sampai napasnya habis. Pathfinder tidak bisa
// menolongnya; yang bisa cuma kendali langsung.
const RENANG_TIMEOUT_MS = 25000
let renangSampai = 0

function diAir() {
  return Boolean(bot.entity && bot.entity.isInWater)
}

// Tanah terdekat: 8 penjuru, cari pijakan padat dengan dua blok udara di
// atasnya. Pemindaian manual (8 x 16 blockAt) jauh lebih murah daripada
// findBlocks useExtraInfo yang menyisir seluruh bola.
function arahDarat() {
  const p = bot.entity.position.floored()
  const arah = [[1, 0], [-1, 0], [0, 1], [0, -1], [1, 1], [1, -1], [-1, 1], [-1, -1]]
  let terbaik = null
  for (const [dx, dz] of arah) {
    for (let d = 2; d <= 16; d++) {
      const x = p.x + dx * d
      const z = p.z + dz * d
      const bawah = bot.blockAt(new Vec3(x, p.y - 1, z))
      const kaki = bot.blockAt(new Vec3(x, p.y, z))
      const atas = bot.blockAt(new Vec3(x, p.y + 1, z))
      if (!bawah || !kaki || !atas) continue
      if (bawah.boundingBox === 'block' && bawah.name !== 'water' &&
          kaki.name === 'air' && atas.name === 'air') {
        if (!terbaik || d < terbaik.d) terbaik = { x, y: p.y, z, d }
        break
      }
    }
  }
  return terbaik
}

function lepasKendaliRenang() {
  try {
    bot.setControlState('jump', false)
    bot.setControlState('forward', false)
  } catch (e) { /* koneksi sudah tutup */ }
}

// Dicek tiap detik, bukan ikut ticker 5 dtk: napas habis dalam ~15 detik, jadi
// telat satu putaran saja sudah setengah nyawa.
// Pengumuman berenang DIREDAM, kendalinya TIDAK. TERUKUR [date removed]: satu sesi
// uji menghasilkan 64 `swim_start` -- di tepi air yang cetek dia bolak-balik
// masuk/keluar tiap detik, tiap kali memancarkan swim_start + roam_start dan
// tiap kali memicu reaksi "kamu kecebur". Itu membanjiri ring event DAN bikin
// dia menarasikan kecebur terus-terusan padahal napasnya penuh.
// Yang diumumkan cuma yang pantas diceritakan: napas sudah berkurang, atau
// sudah lama di air. Keluar dari air tetap langsung, tanpa jeda.
const SWIM_UMUM_MS = 20000
// Episode dianggap benar-benar selesai kalau dia sudah 4 dtk kering.
const SWIM_EPISODE_TUTUP_MS = 4000
let renangDiumumkan = 0
let renangEpisodeDiumumkan = false
let renangKeluarSejak = 0

setInterval(() => {
  if (!renangKeluarSejak) return
  if (diAir()) { renangKeluarSejak = 0; return }   // masuk lagi: episode lanjut
  if (Date.now() - renangKeluarSejak < SWIM_EPISODE_TUTUP_MS) return
  renangKeluarSejak = 0
  if (renangEpisodeDiumumkan) emit({ ev: 'swim_end', reason: 'sampai_darat' })
  renangEpisodeDiumumkan = false
}, 1000)

setInterval(() => {
  if (!bot.entity) return
  if (!diAir()) {
    if (currentTask === 'renang') {
      lepasKendaliRenang()
      renangSampai = 0
      // Episode belum ditutup kalau dia baru sedetik di darat: di tepi air dia
      // bolak-balik masuk/keluar, dan tiap keluar-sebentar dulu dihitung
      // episode baru sehingga redaman 20 dtk tidak pernah kena.
      renangKeluarSejak = renangKeluarSejak || Date.now()
      setFollow()
    }
    return
  }
  if (currentTask !== 'renang') {
    currentTask = 'renang'
    renangSampai = Date.now() + RENANG_TIMEOUT_MS
    roamTarget = null
    bot.pathfinder.setGoal(null)     // pathfinder tak berguna di dalam air
    // Napas berkurang = kepalanya benar-benar di dalam air, itu layak
    // diceritakan. Cetek dengan napas penuh: ditangani diam-diam.
    const napasKurang = typeof bot.oxygenLevel === 'number' && bot.oxygenLevel < 20
    if (!renangEpisodeDiumumkan
        && (napasKurang || Date.now() - renangDiumumkan > SWIM_UMUM_MS)) {
      renangDiumumkan = Date.now()
      renangEpisodeDiumumkan = true
      emit({ ev: 'swim_start', oxygen: bot.oxygenLevel })
    }
  }
  if (Date.now() > renangSampai) {
    lepasKendaliRenang()
    renangSampai = 0
    if (renangEpisodeDiumumkan) emit({ ev: 'swim_end', reason: 'menyerah' })
    renangEpisodeDiumumkan = false
    setFollow()
    return
  }
  // Lompat = naik ke permukaan (perilaku vanilla). Ini saja sudah menghentikan
  // tenggelam, bahkan kalau daratnya tidak ketemu.
  bot.setControlState('jump', true)
  const darat = arahDarat()
  if (darat) {
    try {
      bot.lookAt(new Vec3(darat.x + 0.5, darat.y, darat.z + 0.5), true)
    } catch (e) { /* abaikan */ }
  }
  bot.setControlState('forward', true)
}, 1000)

// Diperbesar [date removed] (operator: "compensate karena mikir lama, lebih banyak
// yang dilakuin — kabur 20 blok radius jadi lebih"): satu episode kabur kini
// membeli seluruh jendela mikir LLM (~15-20 dtk), bukan berhenti 8 detik
// lalu berdiri bingung menunggu keputusan berikutnya.
const FLEE_MS = 15000         // lama satu episode kabur
const FLEE_JARAK = 32         // sejauh apa dia menjauh dari ancaman
const FLEE_AMAN = 24          // tidak ada hostile sedekat ini = sudah aman
let fleeUntil = 0
let fleeDari = ''

function jumlahDiTas(nama) {
  return bot.inventory.items()
    .filter((it) => it.name === nama)
    .reduce((n, it) => n + it.count, 0)
}

// Resep prismarine -> matriks nama item untuk panel craft melayang.
// Dua bentuk yang berbeda: `inShape` (resep berbentuk, mis. pickaxe) punya
// tata letak; `ingredients` (resep bebas bentuk, mis. papan kayu) tidak, jadi
// bahannya dijejer saja. Slot kosong di inShape ditandai id -1, BUKAN null.
function bentukResep(resep) {
  const nama = (id) => {
    const it = id == null || id < 0 ? null : bot.registry.items[id]
    return it ? it.name : null
  }
  if (resep && resep.inShape && resep.inShape.length) {
    return resep.inShape.slice(0, 3).map((baris) =>
      (baris || []).slice(0, 3).map((sel) => nama(sel && sel.id)))
  }
  const bahan = ((resep && resep.ingredients) || []).slice(0, 9)
  const out = [[], [], []]
  bahan.forEach((sel, i) => { out[Math.floor(i / 3)].push(nama(sel && sel.id)) })
  return out.filter((b) => b.length)
}

// Arah hadap dibulatkan ke mata angin terdekat. Menaruh blok pada arah
// pandang mentah menghasilkan posisi diagonal yang membingungkan; pemain
// sungguhan juga menaruh blok lurus di depannya.
function arahDepan() {
  const yaw = bot.entity.yaw
  const x = -Math.sin(yaw)
  const z = Math.cos(yaw)
  return Math.abs(x) > Math.abs(z)
    ? new Vec3(Math.sign(x), 0, 0)
    : new Vec3(0, 0, Math.sign(z))
}

// Blok yang boleh ditimpa: udara, rumput, bunga. `boundingBox === 'empty'`
// menangkap semuanya sekaligus, termasuk yang belum ada di [time removed].4 nanti.
function bolehDitimpa(b) {
  return Boolean(b) && b.boundingBox === 'empty' && b.name !== 'water' && b.name !== 'lava'
}

// Tunggu sampai dia benar-benar berhenti (maks ~1,5 dtk). Momentum berjalan
// tidak hilang seketika saat goal pathfinder dilepas.
async function tungguDiam(maksMs = 1500) {
  const batas = Date.now() + maksMs
  while (Date.now() < batas) {
    const v = bot.entity && bot.entity.velocity
    if (v && Math.hypot(v.x, v.z) < 0.02 && Math.abs(v.y) < 0.08) return true
    await new Promise((r) => setTimeout(r, 100))
  }
  return false
}

// Kerusakan & kecepatan serang senjata. minecraft-data TIDAK menyimpan ini
// (dicek: item iron_sword tidak punya field attackSpeed sama sekali), dan
// satu-satunya paket yang punya tabelnya — mineflayer-pvp — rilis terakhirnya
// 2022 dan menyeret mineflayer-utils dari 2020 yang masih minta mineflayer v2.
// Angkanya sendiri tidak berubah sejak 1.9, jadi ditulis di sini saja.
const SENJATA = {
  netherite_sword: [8, 1.6], diamond_sword: [7, 1.6], iron_sword: [6, 1.6],
  stone_sword: [5, 1.6], golden_sword: [4, 1.6], wooden_sword: [4, 1.6],
  netherite_axe: [10, 1.0], diamond_axe: [9, 1.0], iron_axe: [9, 0.9],
  stone_axe: [9, 0.8], golden_axe: [7, 1.0], wooden_axe: [7, 0.8],
  trident: [9, 1.1], mace: [6, 0.6]
}
const TANGAN_KOSONG = [1, 4.0]

function _stat(item) {
  return (item && SENJATA[item.name]) || TANGAN_KOSONG
}

// Jeda antar pukulan (ms). Memukul sebelum cooldown penuh cuma memberi
// sebagian kerusakan sejak 1.9 — spam klik justru MELEMAHKAN dia.
function jedaSerang(item) {
  return Math.round(1000 / _stat(item)[1])
}

// Dipilih berdasarkan DPS, bukan kerusakan mentah: kapak besi memukul 9 tapi
// cuma 0,9x/dtk (8,1 dps), sedangkan pedang besi 6 x 1,6 = 9,6 dps.
function senjataTerbaik() {
  let terbaik = null
  let dpsTerbaik = TANGAN_KOSONG[0] * TANGAN_KOSONG[1]
  for (const it of bot.inventory.items()) {
    const st = SENJATA[it.name]
    if (!st) continue
    const dps = st[0] * st[1]
    if (dps > dpsTerbaik) { dpsTerbaik = dps; terbaik = it }
  }
  return terbaik
}

// Taruh SATU blok di posisi tertentu. Dipakai `place` (satu blok di depan)
// dan `bangun` (belasan blok berpola) supaya dua-duanya lewat jalur yang
// sama — termasuk pemeriksaan ulang di dunia sebelum dianggap berhasil.
// Balikan: '' kalau berhasil, atau alasan gagal.
async function naruhSatu(target, nama, jenis) {
  if (!bolehDitimpa(bot.blockAt(target))) return 'tempatnya_terisi'
  // Enam sisi dicari, bukan tiga titik tetap: dia sering berhenti di TEPI
  // blok, jadi "yang di bawah" bisa saja udara.
  const SISI = [
    [new Vec3(0, -1, 0), new Vec3(0, 1, 0)],
    [new Vec3(1, 0, 0), new Vec3(-1, 0, 0)],
    [new Vec3(-1, 0, 0), new Vec3(1, 0, 0)],
    [new Vec3(0, 0, 1), new Vec3(0, 0, -1)],
    [new Vec3(0, 0, -1), new Vec3(0, 0, 1)],
    [new Vec3(0, 1, 0), new Vec3(0, -1, 0)]
  ]
  let acuan = null
  let muka = null
  for (const [geser, face] of SISI) {
    const b = bot.blockAt(target.plus(geser))
    if (b && b.boundingBox === 'block') { acuan = b; muka = face; break }
  }
  if (!acuan) return 'tidak_ada_pijakan'
  try {
    await bot.equip(jenis.id, 'hand')
    await bot.placeBlock(acuan, muka)
  } catch (e) {
    return 'gagal_naruh'
  }
  // JANGAN percaya begitu saja: placeBlock menunggu satu blockUpdate, dan
  // update itu bisa saja bukan blok kita.
  const jadi = bot.blockAt(target)
  return (jadi && (jadi.name === nama || jadi.name === 'wall_' + nama)) ? '' : 'gagal_naruh'
}

// Slot badan untuk armor. Kalau helm cuma dipegang di tangan, dari kamera dia
// kelihatan MENGGENGGAM helm alih-alih memakainya — dan pelindungnya tidak
// berfungsi sama sekali. Nama slot dari mineflayer: head/torso/legs/feet.
function slotArmor(nama) {
  if (nama.endsWith('_helmet')) return 'head'
  if (nama.endsWith('_chestplate')) return 'torso'
  if (nama.endsWith('_leggings')) return 'legs'
  if (nama.endsWith('_boots')) return 'feet'
  if (nama === 'shield') return 'off-hand'
  return 'hand'
}

function hostileTerdekat() {
  return bot.nearestEntity((e) =>
    e && e.position && e !== bot.entity &&
    (e.kind === 'Hostile mobs' || e.type === 'hostile') &&
    bot.entity.position.distanceTo(e.position) <= 24) || null
}

// Kabur = GoalInvert dari goal "dekati dia" — pathfinder membalik arahnya.
// `dynamic: true` supaya dia terus menjauh saat mobnya mengejar, bukan lari
// ke satu titik lalu berhenti jadi sasaran lagi.
function mulaiKabur(dari, alasan) {
  if (!bot.entity || !dari || !dari.position) return false
  fleeDari = dari.username || dari.displayName || dari.name || 'sesuatu'
  if (currentTask !== 'kabur') {
    // Batas waktu dipasang HANYA saat episode dimulai. Terbukti di server
    // [date removed]: menyegarkannya tiap tick membuat batasnya tidak pernah
    // tercapai — selama mob masih mengejar dalam 14 blok, Arti kabur
    // SELAMANYA dan tidak pernah kembali menemani/menjelajah. Yang boleh
    // memperpanjang cuma luka baru (lihat cobaKaburOtomatis).
    fleeUntil = Date.now() + FLEE_MS
    currentTask = 'kabur'
    emit({ ev: 'flee_start', from: fleeDari, reason: alasan || 'terancam' })
  }
  roamTarget = null
  bot.pathfinder.setGoal(
    new goals.GoalInvert(new goals.GoalFollow(dari, FLEE_JARAK)), true)
  return true
}

function selesaiKabur(alasan) {
  fleeUntil = 0
  emit({ ev: 'flee_end', reason: alasan, from: fleeDari })
  fleeDari = ''
  setFollow()      // setFollow sendiri yang memutuskan: ikuti operator / roam
}

// Deteksi nyangkut. Audit [date removed]: `stuck_timeout` selama ini KODE MATI —
// bot.js tidak pernah memancarkannya, jadi reaksi "aku nyangkut" mustahil
// terpicu. Terukur: beku 41 dtk saat roam dan 61 dtk saat follow (follow tidak
// punya timeout sama sekali), sementara konteks ke LLM tetap bilang "lagi
// ngikutin operator" — Arti mengarang aktivitas yang tidak terjadi.
const STUCK_MS = 20000
const STUCK_JARAK = 1.0
let lastMovePos = null
let lastMoveAt = 0

setInterval(() => {
  if (!bot.entity) return
  // Menggali jalan keluar = berdiri diam DENGAN SENGAJA; jangan dicap
  // nyangkut di tengah usahanya membongkar kurungan.
  if (sedangGaliKeluar) return
  // Fase bertahan: selama musuh masih dekat, perpanjang; sudah aman ->
  // keluar dari fase dan biarkan setFollow memutuskan langkah berikutnya.
  if (currentTask === 'bertahan') {
    const musuh = hostileTerdekat()
    if (musuh && bot.entity.position.distanceTo(musuh.position)
        <= BERTAHAN_MUSUH_DEKAT) {
      bertahanSampai = Date.now() + BERTAHAN_PERPANJANG_MS
    } else if (Date.now() >= bertahanSampai) {
      emit({ ev: 'bertahan_selesai' })
      setFollow()
    }
    lastMoveAt = Date.now()   // diam di balik tembok itu SEHAT, bukan nyangkut
    return
  }
  const p = bot.entity.position
  const diam =
    lastMovePos &&
    Math.hypot(p.x - lastMovePos.x, p.y - lastMovePos.y, p.z - lastMovePos.z)
      < STUCK_JARAK
  // Diam DI TEMPAT YANG BENAR bukan nyangkut. GoalFollow(dist 3) memang
  // berhenti begitu bot sampai di dekat streamer — kalau operator berdiri diam
  // (baca chat, bangun sesuatu), bot ikut diam dan itu SEHAT. Tanpa
  // pengecualian ini, deteksi nyangkut malah menyuruh Arti ngeloyor pergi
  // padahal dia sedang berdiri di sebelah operator.
  const targetDekat = (() => {
    const t = player(STREAMER)
    return Boolean(t && bot.entity && bot.entity.position.distanceTo(t.position) <= 5)
  })()
  if (!diam) {
    lastMovePos = { x: p.x, y: p.y, z: p.z }
    lastMoveAt = Date.now()
  } else if (
    currentTask !== 'idle' &&
    currentTask !== 'wait_streamer' &&
    currentTask !== 'nambang' &&
    currentTask !== 'renang' &&
    // Menaruh blok & menyerahkan barang juga berdiri diam — pelajaran yang
    // sama dengan craft, di mana detektor ini membatalkan aksinya sendiri.
    currentTask !== 'naruh' &&
    currentTask !== 'kasih' &&
    // Bertarung di tempat sempit juga diam di tempat.
    currentTask !== 'serang' &&
    currentTask !== 'bangun' && currentTask !== 'turun' &&
      currentTask !== 'masak' && currentTask !== 'tidur' &&
      currentTask !== 'tembok' &&
      currentTask !== 'portal' && currentTask !== 'masuk_portal' &&
      currentTask !== 'menara' && currentTask !== 'jasad' &&
      currentTask !== 'jembatan' &&
      currentTask !== 'simpan' && currentTask !== 'ambil' &&
      currentTask !== 'panah' && currentTask !== 'bertahan' &&
      currentTask !== 'lubang' &&
    // Crafting = BERDIRI DIAM, itu perilaku yang benar, bukan nyangkut.
    // Terukur di server [date removed]: tanpa baris ini detektor memanggil
    // startRoam('unstuck') 2 ms sesudah dia mulai jalan ke meja (timer
    // nyangkutnya sudah jatuh tempo dari craft sebelumnya), goal-nya ganti,
    // dan craft-nya batal dengan alasan yang bohong: 'meja_tak_terjangkau'.
    currentTask !== 'craft' &&
    !(currentTask === 'follow' && targetDekat) &&
    Date.now() - lastMoveAt > STUCK_MS
  ) {
    emit({ ev: 'task_failed', task: currentTask, reason: 'stuck_timeout' })
    lastMoveAt = Date.now()      // jangan membanjiri
    roamManual = false
    // Ingat DUA tempat 5 menit ke depan: posisi dia mematung DAN tujuan roam
    // yang gagal dicapai — dua-duanya jangan dipilih lagi.
    titikNyangkut.push({ x: p.x, y: p.y, z: p.z, ts: Date.now() })
    if (currentTask === 'roam' && roamTarget) {
      titikNyangkut.push({ x: roamTarget.x, y: roamTarget.y,
                           z: roamTarget.z, ts: Date.now() })
    }
    if (titikNyangkut.length > 20) {
      titikNyangkut.splice(0, titikNyangkut.length - 20)
    }
    const samaTempat = nyangkutPosTerakhir &&
      Math.hypot(p.x - nyangkutPosTerakhir.x, p.z - nyangkutPosTerakhir.z) < 3
    nyangkutStreak = samaTempat ? nyangkutStreak + 1 : 1
    nyangkutPosTerakhir = { x: p.x, y: p.y, z: p.z }
    // Coba LOMPAT dulu — banyak "nyangkut" cuma tersangkut bibir blok
    // setinggi 1 (keluhan operator: "kakinya nyangkut itu gimana?"). Murah,
    // dan kalau memang buntu, ganti tujuan tetap jalan sesudahnya.
    bot.setControlState('jump', true)
    setTimeout(() => bot.setControlState('jump', false), 400)
    if (nyangkutStreak >= 2) {
      // Dua kali mematung di titik yang sama: lompat & tujuan baru sudah
      // terbukti tidak menolong — bongkar penghalangnya, baru jalan lagi.
      galiKeluar('nyangkut berulang').then(() => startRoam('unstuck'))
    } else {
      startRoam('unstuck')       // lepas dari tujuan yang mustahil
    }
    return
  }
  // `come` yang tak terjangkau tidak pernah memancarkan apa pun: pagar jarak
  // di goal_reached cuma jalan kalau goal-nya TERCAPAI, sedangkan target yang
  // mustahil (operator di pilar/dimensi lain) tidak pernah memicunya. Audit
  // verifikasi [date removed]: diam 45 detik tanpa satu pun laporan.
  if (currentTask === 'renang') return   // kendali langsung, bukan pathfinder
  if (currentTask === 'kabur') {
    const musuh = hostileTerdekat()
    const aman = !musuh ||
      bot.entity.position.distanceTo(musuh.position) > FLEE_AMAN
    if (aman) { selesaiKabur('aman'); return }
    if (Date.now() > fleeUntil) { selesaiKabur('cukup_jauh'); return }
    mulaiKabur(musuh, 'lanjut')     // segarkan tujuan; mobnya bergerak
    return
  }
  if (currentTask === 'come' && comeSince && Date.now() - comeSince > COME_TIMEOUT_MS) {
    comeSince = 0
    emit({ ev: 'task_failed', task: 'come', reason: 'unreachable' })
    setFollow()
    return
  }
  if (currentTask === 'follow') {
    setFollow()
    return
  }
  if (currentTask === 'wait_streamer') {
    setFollow()   // biar setFollow yang memutuskan: tunggu lagi / ikuti / roam
    return
  }
  if (currentTask === 'roam') {
    // Streamer nongol lagi -> otomatis balik nemenin, KECUALI roam disuruh
    // manual ([MC: roam] / 'mc roam' — operator sengaja nyuruh dia main sendiri).
    if (streamerHere() && !roamManual) {
      emit({ ev: 'roam_end', reason: 'streamer_back' })
      setFollow()
      return
    }
    if (Date.now() - roamSince > ROAM_TIMEOUT_MS) startRoam('next_leg')
  }
}, 5000)

// ---------- refleks bertahan hidup ([date removed]) ----------
// TERUKUR [date removed]: dengan ambang 7 (dari 20) refleksnya menyala terlalu
// terlambat -- panah skeleton ~4, jadi 7 itu tinggal dua pukulan, sementara
// menembok butuh ~25 penaruhan blok. Rendaman dengan ambang 7: masih 3 mati.
const REFLEKS_HP = 12
const REFLEKS_PERUT = 6        // di bawah ini darahnya berhenti pulih sendiri
// Skeleton menembak dari ~16 blok; 12 membuat refleks tidak pernah menyala
// pada musuh yang justru paling sering membunuhnya.
const REFLEKS_JARAK = 16
const REFLEKS_JEDA_MS = 25000  // jangan menembok berulang-ulang
const REFLEKS_BLOK_MIN = 20
// Musuh yang menyerang dari JAUH. Lari dari mereka salah arah -- panahnya
// lebih cepat dari kakinya; yang benar memutus garis tembaknya.
const PENEMBAK = new Set(['skeleton', 'stray', 'bogged', 'pillager', 'piglin'])
let refleksTerakhir = 0
let refleksJenisTerakhir = ''
let pungutTerakhir = 0
let rakitTerakhir = 0
let lubangTerakhir = 0
// Dorongan ISI TAS (operator [date removed] [time removed]: "dia masih aja ga mau ambil wood,
// atau batu atau pickaxe, padahal itu kayaknya minimal loh, aku mau dia
// minimal punya KEINGINAN buat isi tasnya"). 60 dtk: cukup rajin untuk
// terlihat punya niat, cukup jarang untuk tidak jadi robot penebang.
const ISI_TAS_JEDA_MS = 60000
// Batuan yang sah dijadikan cobblestone/pengganti — bukan cuma 'stone'
// (permukaan sering andesite/granite/diorite; bawah tanah deepslate).
const BATUAN = ['stone', 'cobblestone', 'deepslate', 'cobbled_deepslate',
                'andesite', 'diorite', 'granite', 'tuff']
// Tugas yang TIDAK boleh diserobot refleks: yang sedang Arti kerjakan atas
// perintah, plus renang (tenggelam lebih mendesak dari apa pun).
//
// `kabur` SENGAJA TIDAK di sini. TERUKUR [date removed]: rendaman kedua = 2 mati,
// refleks menyala NOL kali -- karena saat darahnya tipis dan dia ditembak,
// tugasnya justru 'kabur', jadi refleks terkunci persis di situasi yang dia
// rancang untuk atasi. Dua-duanya "shot by Skeleton", dan lari memang tidak
// menyelesaikan musuh yang menembak dari jauh.
const TUGAS_TERLINDUNG = new Set([
  'renang', 'bangun', 'craft', 'nambang', 'naruh', 'kasih',
  'serang', 'turun', 'masak', 'tidur', 'tembok', 'portal', 'masuk_portal',
  'menara', 'jasad', 'jembatan', 'simpan', 'ambil', 'pulang', 'panah',
  'lubang',
])

function blokTerbanyakDiTas () {
  // Kayu (log/papan) itu MATA UANG rantai alat — papan, stick, meja, dan
  // pickaxe pertamanya lahir dari situ. Live [time removed]: refleks pungut dapat 4
  // spruce_log, lalu tembok panah darurat memakan 2 ("dia kayak make 2
  // log-nya buat build bridge sementara" — operator) dan rantai alatnya mati
  // diam-diam. Kayu kini CADANGAN TERAKHIR: dipakai hanya kalau tidak ada
  // blok padat lain sama sekali (nyawa tetap menang dari ekonomi).
  let nama = '', n = 0
  let namaKayu = '', nKayu = 0
  const berharga = new Set(KAYU_LOG.concat(KAYU_PAPAN))
  for (const it of bot.inventory.items()) {
    const jenis = bot.registry.blocksByName[it.name]
    if (!jenis) continue
    if (berharga.has(it.name)) {
      if (jenis.boundingBox === 'block' && it.count > nKayu) {
        namaKayu = it.name
        nKayu = it.count
      }
      continue
    }
    // WAJIB blok PADAT. Live [time removed]: dia menumpuk OBOR sebagai tembok panah
    // ("salah kira tumpukan obor itu tembok") — obor terdaftar sebagai blok
    // tapi tidak punya badan, panah skeleton menembusnya, dan dia mati
    // berkali-kali di belakang "tembok" yang menyala indah itu.
    if (jenis.boundingBox !== 'block') continue
    if (it.count > n) { nama = it.name; n = it.count }
  }
  if (!nama && namaKayu) return { nama: namaKayu, n: nKayu }
  return { nama, n }
}

setInterval(async () => {
  if (!REFLEKS_AKTIF) return
  if (!bot.entity || bot.health === undefined) return
  if (TUGAS_TERLINDUNG.has(currentTask)) return
  const jedaRefleks = refleksJenisTerakhir === 'tembok_panah' ? 8000 : REFLEKS_JEDA_MS
  if (Date.now() - refleksTerakhir < jedaRefleks) return

  // 0. ISI TAS — DORONGAN INTRINSIK. Versi lama ([date removed]) cuma memungut
  //    kalau pohon KEBETULAN dalam 6 blok; hasilnya di live [time removed]: NOL pungut
  //    kayu, NOL rakit alat, padahal Arti sendiri bilang "tas ku kosong
  //    melompong... harus cari kayu lagi deh" — dia sadar tapi tak bertindak.
  //    Sekarang dia MENCARI: radius 32 (kayu) / 24 (batuan), dan handler mine
  //    memverifikasi jalur tiap target sebelum berangkat. Urutannya sama
  //    dengan tangga aman: kayu dulu (bahan semua alat), lalu batuan untuk
  //    naik tier. Rakitnya ditangani refleks 0.5 di bawah.
  if (!MODE_TAMU &&
      (currentTask === 'roam' || currentTask === 'idle') &&
      Date.now() - pungutTerakhir > ISI_TAS_JEDA_MS &&
      bot.health > REFLEKS_HP &&
      !(nearbyEntities().hostiles || []).length) {
    const stokKayu = KAYU_LOG.concat(KAYU_PAPAN)
      .reduce((total, nama) => total + jumlahDiTas(nama), 0)
    const punyaPickaxe = ['wooden_pickaxe', 'stone_pickaxe', 'iron_pickaxe',
                          'diamond_pickaxe'].some((x) => jumlahDiTas(x) > 0)
    let target = null
    if (stokKayu < 8) {
      const idLog = KAYU_LOG
        .map((nama) => bot.registry.blocksByName[nama])
        .filter(Boolean)
        .map((jenis) => jenis.id)
      const pohon = idLog.length &&
        bot.findBlock({ matching: idLog, maxDistance: 32 })
      if (pohon) target = { block: pohon.name, count: 6, jenis: 'cari_kayu' }
    }
    // Batuan HANYA kalau sudah ada pickaxe — menambang batu pakai tangan
    // tidak menghasilkan apa pun (dan itu terlihat bodoh di siaran).
    if (!target && punyaPickaxe && jumlahDiTas('cobblestone') < 12) {
      const batu = bot.findBlock({
        matching: (b) => Boolean(b) && BATUAN.includes(b.name),
        maxDistance: 24,
      })
      if (batu) target = { block: batu.name, count: 12, jenis: 'cari_batu' }
    }
    if (target) {
      pungutTerakhir = Date.now()
      refleksJenisTerakhir = target.jenis
      emit({ ev: 'refleks', jenis: target.jenis, block: target.block })
      try {
        await handlers.mine({ block: target.block, count: target.count })
      } catch (e) { /* gagal cari: cooldown, coba lagi nanti */ }
      return
    }
  }

  // 0.5 RAKIT ALAT otomatis (operator [date removed]: "ada inisiatif bikin tools
  //     ga?"). Tenang (tanpa musuh), bahan ada, alat tertinggal -> rakit
  //     sendiri tanpa menunggu LLM. Cooldown 120 dtk.
  if (!MODE_TAMU &&
      Date.now() - rakitTerakhir > 75000 &&
      !(nearbyEntities().hostiles || []).length) {
    const adaKayu = KAYU_LOG.concat(KAYU_PAPAN)
      .some((x) => jumlahDiTas(x) > 0)
    const punyaPickaxe = ['wooden_pickaxe', 'stone_pickaxe', 'iron_pickaxe',
                         'diamond_pickaxe'].some((x) => jumlahDiTas(x) > 0)
    const naikBatu = jumlahDiTas('cobblestone') >= 3 &&
      !['stone_pickaxe', 'iron_pickaxe', 'diamond_pickaxe']
        .some((x) => jumlahDiTas(x) > 0)
    const naikBesi = jumlahDiTas('iron_ingot') >= 3 &&
      !['iron_pickaxe', 'diamond_pickaxe'].some((x) => jumlahDiTas(x) > 0)
    if ((!punyaPickaxe && adaKayu) || naikBatu || naikBesi) {
      rakitTerakhir = Date.now()
      refleksJenisTerakhir = 'rakit_alat'
      emit({ ev: 'refleks', jenis: 'rakit_alat' })
      try { await handlers.siapkan_alat() } catch (e) {}
      return
    }
  }

  // 0.8 MALAM TELANJANG — berlindung SEBELUM darah tipis (operator [date removed]:
  //     "malam dia cuma jalan jalan coba bertahan hidup"). Refleks berlindung
  //     biasa menunggu darah <= 12; saat dia tanpa senjata & tanpa armor,
  //     zombie/skeleton menghabiskannya dari darah PENUH lebih cepat dari itu
  //     — terukur 10 kematian dalam 40 menit (live [time removed]-[time removed]), dan tiap
  //     mati barangnya tercecer. Aturan pemain sungguhan: malam pertama tanpa
  //     senjata = jangan cari mati, sembunyi, keluar pagi.
  if (!MODE_TAMU &&
      bot.time && bot.time.timeOfDay > 12500 && bot.time.timeOfDay < 23500 &&
      Date.now() - lubangTerakhir > 90000) {
    const adaSenjata = ['wooden_sword', 'stone_sword', 'iron_sword',
                        'diamond_sword', 'netherite_sword', 'bow']
      .some((x) => jumlahDiTas(x) > 0)
    const musuhDekat = (nearbyEntities().hostiles || [])
      .filter((m) => (m.distance || 99) <= 14)
    if (!adaSenjata && musuhDekat.length) {
      lubangTerakhir = Date.now()
      refleksTerakhir = Date.now()
      refleksJenisTerakhir = 'sembunyi_malam'
      emit({ ev: 'refleks', jenis: 'sembunyi_malam', musuh: musuhDekat.length })
      const { nama, n } = blokTerbanyakDiTas()
      try {
        if (nama && n >= REFLEKS_BLOK_MIN) await handlers.bangun({ block: nama })
        else await handlers.lubang_aman()
      } catch (e) { /* gagal berlindung: refleks lain menyusul */ }
      return
    }
  }

  // 1. MAKAN. Perut kosong = darah tidak pulih, dan itu yang membunuhnya
  //    pelan-pelan sesudah tiap serangan.
  if (bot.food <= REFLEKS_PERUT) {
    const daftar = (bot.registry && bot.registry.foodsByName) || {}
    const ada = bot.inventory.items().some((it) => daftar[it.name])
    if (ada) {
      refleksTerakhir = Date.now()
      refleksJenisTerakhir = 'makan'
      emit({ ev: 'refleks', jenis: 'makan' })
      try { await handlers.eat({}) } catch (e) {}
      return
    }
  }

  // 1b. DIKEPUNG: >=3 musuh pukul dalam 10 blok -> naik menara 3 blok.
  //     (Permintaan operator [date removed].) Penembak TIDAK dihitung — di atas
  //     menara justru jadi sasaran empuk panah; mereka urusan tembok/kabur.
  if (!MODE_TAMU) {
    const kepung = (nearbyEntities().hostiles || []).filter((m) =>
      (m.distance || 999) <= 10 && !PENEMBAK.has(String(m.kind || '').toLowerCase()))
    if (kepung.length >= 3) {
      const { nama, n } = blokTerbanyakDiTas()
      if (nama && n >= 3) {
        refleksTerakhir = Date.now()
        refleksJenisTerakhir = 'menara'
        emit({ ev: 'refleks', jenis: 'menara', musuh: kepung.length })
        try { await handlers.menara({ count: 3 }) } catch (e) {}
        return
      }
    }
  }

  // 2. BERLINDUNG. Dipisah menurut jenis ancamannya, dari data kematian
  //    rendaman (8 tercatat, 4 oleh skeleton): PENEMBAK dapat tembok cepat
  //    2 blok (memutus garis tembak dalam ~2 dtk), musuh pukul dapat shelter
  //    penuh. Refleks dengan shelter penuh untuk semua terukur TIDAK
  //    menurunkan kematian (3/2/3/2) -- terlalu lambat saat panah beruntun.
  if (!MODE_TAMU && bot.health <= REFLEKS_HP) {
    const musuh = nearbyEntities().hostiles || []
    const dekat = musuh.filter((m) => (m.distance || 999) <= REFLEKS_JARAK)
    if (dekat.length) {
      const penembak = dekat.some((m) => PENEMBAK.has(String(m.kind || '').toLowerCase()))
      const { nama, n } = blokTerbanyakDiTas()
      if (penembak && nama && n >= 2) {
        refleksTerakhir = Date.now()
        refleksJenisTerakhir = 'tembok_panah'
        emit({ ev: 'refleks', jenis: 'tembok_panah', block: nama })
        try { await handlers.mundur_tembok({}) } catch (e) {}
      } else if (nama && n >= REFLEKS_BLOK_MIN) {
        refleksTerakhir = Date.now()
        refleksJenisTerakhir = 'berlindung'
        emit({ ev: 'refleks', jenis: 'berlindung', block: nama })
        try { await handlers.bangun({ block: nama }) } catch (e) {}
      }
    }
  }
}, 2000)

// Hewan yang menghasilkan makanan kalau dibunuh. Ayam & kelinci sengaja
// diikutkan walau dagingnya kecil — waktu perut kritis, apa pun berguna.
const HEWAN_MAKANAN = new Set([
  'cow', 'pig', 'sheep', 'chicken', 'rabbit', 'cod', 'salmon', 'mooshroom'
])

// PENGLIHATAN SUMBER DAYA (operator [date removed]: "penglihatannya gimana ya? rada
// deket buat scanning dia soalnya"). Sebelum ini status cuma memuat entity +
// isi tas: Arti BUTA terhadap kayu/batu/bijih, jadi saat disuruh cari kayu
// dia menjawab jujur "emang nggak kelihatan" (live [time removed]) padahal hutan bisa
// jadi 25 blok di sebelahnya. Sekarang dia melapor apa yang TERLIHAT beserta
// jaraknya, dan nudge bisa menyebut jarak itu.
//
// Di-CACHE 30 dtk: findBlock radius 40 memeriksa ratusan ribu blok — kalau
// dipanggil tiap status (10 dtk) itu tiga kali lebih banyak sampah untuk GC,
// dan kami baru saja keluar dari epidemi OOM.
const PANDANG_CACHE_MS = 30000
const PANDANG_JAUH = 40
let pandangCache = { ts: 0, isi: {} }

function _blokTerdekat(nama, jarak) {
  const daftar = (Array.isArray(nama) ? nama : [nama])
    .map((n) => bot.registry.blocksByName[n])
    .filter(Boolean)
    .map((j) => j.id)
  if (!daftar.length) return null
  const b = bot.findBlock({ matching: daftar, maxDistance: jarak })
  if (!b) return null
  return {
    kind: b.name,
    distance: Math.round(bot.entity.position.distanceTo(b.position)),
  }
}

function pandangan() {
  if (!bot.entity || !bot.registry) return {}
  if (Date.now() - pandangCache.ts < PANDANG_CACHE_MS) return pandangCache.isi
  const isi = {}
  try {
    const pohon = _blokTerdekat(KAYU_LOG, PANDANG_JAUH)
    if (pohon) isi.pohon = pohon
    const batu = _blokTerdekat(BATUAN, 24)
    if (batu) isi.batu = batu
    const arang = _blokTerdekat(['coal_ore', 'deepslate_coal_ore'], 24)
    if (arang) isi.batu_bara = arang
    const besi = _blokTerdekat(['iron_ore', 'deepslate_iron_ore'], 24)
    if (besi) isi.besi = besi
    const air = _blokTerdekat('water', 16)
    if (air) isi.air = air
  } catch (e) { /* dunia belum termuat: laporkan apa adanya */ }
  pandangCache = { ts: Date.now(), isi }
  return isi
}

function nearbyEntities() {
  const hostiles = []
  const players = []
  const hewan = []
  if (!bot.entity) return { hostiles, players, hewan }
  for (const e of Object.values(bot.entities)) {
    if (!e || !e.position || e === bot.entity) continue
    const d = e.position.distanceTo(bot.entity.position)
    // MUSUH dipindai lebih jauh (24) daripada yang lain (16): skeleton menembak
    // dari ~15-16 blok, jadi dengan batas 16 dia "menghilang" dari status
    // begitu Arti mundur dua langkah — live [time removed] Arti sendiri mengadu "script
    // skeleton-nya anggap musuh udah ilang, padahal nembakku terus".
    // 32 untuk SEMUA (operator [date removed]: "penglihatannya gimana ya? rada deket
    // buat scanning dia soalnya"). Hewan pada 16 blok berarti dia tidak tahu
    // ada makanan 20 blok di depan — padahal nudge "cari makan" bersandar
    // persis pada daftar ini. Jumlahnya tetap dipangkas di bawah supaya
    // status tidak membengkak.
    if (d > 32) continue
    const musuhkah = e.type === 'hostile' || (e.kind && String(e.kind).includes('Hostile'))
    if (e.type === 'player' && e.username && e.username !== bot.username) {
      players.push({ name: e.username, distance: Math.round(d) })
    } else if (musuhkah) {
      hostiles.push({ kind: e.name || 'unknown', distance: Math.round(d) })
    } else if (HEWAN_MAKANAN.has(String(e.name || '').toLowerCase())) {
      hewan.push({ kind: e.name, distance: Math.round(d) })
    }
  }
  hewan.sort((a, b) => a.distance - b.distance)
  hostiles.sort((a, b) => a.distance - b.distance)
  return {
    hostiles: hostiles.slice(0, 6),
    players,
    hewan: hewan.slice(0, 4),
  }
}

// Bahan mentah -> matang. Daging matang jauh lebih mengenyangkan, dan itu yang
// membuat makanan "stabil" bukan cuma "ada" (spek operator [date removed]).
const MASAKAN = {
  beef: 'cooked_beef', porkchop: 'cooked_porkchop', chicken: 'cooked_chicken',
  mutton: 'cooked_mutton', rabbit: 'cooked_rabbit', cod: 'cooked_cod',
  salmon: 'cooked_salmon', potato: 'baked_potato',
}
// Diurutkan dari yang paling boros nilainya kalau dipakai untuk hal lain:
// Kayu SEMUA jenis (cermin KAYU_LOG di arti_minecraft.py). operator [date removed]
// [time removed], hutan spruce: "kamu harus ambil lebih banyak lagi" — refleks pungut
// dulu cuma kenal oak, jadi di hutan non-oak dia buta kayu total.
const KAYU_JENIS = ['oak', 'spruce', 'birch', 'jungle', 'acacia', 'dark_oak',
                    'mangrove', 'cherry', 'pale_oak']
const KAYU_LOG = KAYU_JENIS.map((j) => j + '_log')
const KAYU_PAPAN = KAYU_JENIS.map((j) => j + '_planks')

// arang/batu bara dulu, kayu belakangan.
const BAHAN_BAKAR = ['coal', 'charcoal', 'oak_log', 'oak_planks', 'stick']
const CAIRAN = new Set(['water', 'flowing_water', 'lava', 'flowing_lava'])

// Titik kosong di SEKITAR kaki, bukan cuma yang tepat di depan.
// TERUKUR [date removed]: `masak` dan `tidur` dua-duanya gagal 'tempatnya_terisi'
// karena keduanya memakai arahDepan() -- dan blok di depan kaki hampir selalu
// terisi kalau dia menghadap dinding ATAU sedang berada di lubang hasil
// menggali turun sendiri, yaitu justru situasi saat dia paling butuh memasak.
// `lebar2` untuk benda yang makan DUA blok seperti bed.
function titikKosong (lebar2) {
  if (!bot.entity) return null
  const kaki = bot.entity.position.floored()
  const arah = [[1, 0], [-1, 0], [0, 1], [0, -1], [1, 1], [-1, -1], [1, -1], [-1, 1]]
  const layak = (t) => {
    if (!bolehDitimpa(bot.blockAt(t))) return false
    const bawah = bot.blockAt(t.offset(0, -1, 0))
    return !!(bawah && bawah.boundingBox === 'block')
  }
  for (const [dx, dz] of arah) {
    const t = kaki.offset(dx, 0, dz)
    if (!layak(t)) continue
    if (lebar2 && !arah.slice(0, 4).some(([ax, az]) => layak(t.offset(ax, 0, az)))) continue
    return t
  }
  return null
}

let jangkarSpawn = null
let rekorJelajah = 0
const biomeDikenal = new Set()

function jarakJelajah () {
  if (!bot.entity) return { jarak: 0, rekor: rekorJelajah }
  if (!jangkarSpawn) jangkarSpawn = bot.entity.position.clone()
  const d = Math.round(Math.hypot(
    bot.entity.position.x - jangkarSpawn.x,
    bot.entity.position.z - jangkarSpawn.z))
  if (d > rekorJelajah) rekorJelajah = d
  return { jarak: d, rekor: rekorJelajah }
}

// Biome baru = momen cerita ("aku nemu gurun!"). Dicek jarang (4 dtk) dan
// hanya memancarkan saat JENIS biome-nya belum pernah dia injak sesi ini,
// jadi tidak membanjiri ring event.
setInterval(() => {
  if (!bot.entity) return
  try {
    // BUKAN blockAt().biome — di mineflayer [time removed].1 field name-nya KOSONG
    // (diprobe [date removed]: {"name":"","id":21}). world.getBiome + registry
    // yang mengembalikan nama sungguhan ("forest").
    const id = bot.world.getBiome(bot.entity.position)
    const nama = bot.registry.biomes[id] && bot.registry.biomes[id].name
    if (!nama || biomeDikenal.has(nama)) return
    const pertama = biomeDikenal.size === 0
    biomeDikenal.add(nama)
    // Biome tempat dia lahir bukan penemuan.
    if (!pertama) emit({ ev: 'biome_baru', name: nama })
  } catch (e) { /* data biome tidak tersedia: menjelajah tetap jalan */ }
}, 4000)

async function urusPeti (c, mode) {
  const nama = String((c && c.item) || '').trim()
  const minta = Math.max(1, Math.min(64, parseInt((c && c.count) || 64, 10) || 64))
  const jenis = bot.registry.itemsByName[nama]
  if (!jenis) {
    emit({ ev: 'task_failed', task: mode, reason: 'item_tak_dikenal', detail: nama })
    return
  }
  const jenisPeti = bot.registry.blocksByName.chest
  let peti = jenisPeti
    ? bot.findBlock({ matching: jenisPeti.id, maxDistance: 16 })
    : null
  const tugasLama = currentTask
  currentTask = mode
  roamTarget = null
  let alasan = ''
  const sebelum = jumlahDiTas(nama)
  try {
    if (!peti && rumahPos) {
      // Tidak ada peti dekat tapi dia PUNYA rumah: pulang dulu.
      await bot.pathfinder.goto(new goals.GoalNear(
        rumahPos.x, rumahPos.y, rumahPos.z, 2))
      peti = jenisPeti
        ? bot.findBlock({ matching: jenisPeti.id, maxDistance: 8 })
        : null
    }
    if (!peti) { alasan = 'tidak_ada_peti'; return }
    await bot.pathfinder.goto(new goals.GoalNear(
      peti.position.x, peti.position.y, peti.position.z, 2))
    const w = await bot.openContainer(peti)
    try {
      if (mode === 'simpan') {
        const punya = jumlahDiTas(nama)
        if (punya <= 0) { alasan = 'tidak_punya_barang'; return }
        await w.deposit(jenis.id, null, Math.min(minta, punya))
      } else {
        await w.withdraw(jenis.id, null, minta)
      }
    } finally {
      try { w.close() } catch (e) {}
    }
  } catch (e) {
    // withdraw melempar kalau petinya tidak punya barangnya.
    alasan = alasan || (mode === 'ambil' ? 'peti_tidak_ada_barang' : 'gagal_peti')
  } finally {
    await new Promise((r) => setTimeout(r, 500))
    const selisih = jumlahDiTas(nama) - sebelum
    const pindah = Math.abs(selisih)
    if (!alasan && pindah > 0) {
      if (mode === 'simpan') statistik.simpan_n += pindah
      emit({ ev: mode === 'simpan' ? 'simpan_done' : 'ambil_done',
             item: nama, count: pindah })
    } else if (!alasan) {
      emit({ ev: 'task_failed', task: mode, reason: 'gagal_peti', detail: nama })
    } else {
      emit({ ev: 'task_failed', task: mode, reason: alasan, detail: nama })
    }
    if (currentTask === mode) {
      currentTask = tugasLama === 'roam' ? 'roam' : 'idle'
      setFollow()
    }
  }
}

function tandaiRumah (nama, pos) {
  if (nama === 'chest' && !rumahPos && pos) {
    rumahPos = pos.clone ? pos.clone() : pos
    emit({ ev: 'rumah_baru', pos: { x: rumahPos.x, y: rumahPos.y, z: rumahPos.z } })
  }
}

function statusEvent() {
  if (!bot.entity) return null
  const p = bot.entity.position
  const near = nearbyEntities()
  return {
    ev: 'status',
    health: Math.round(bot.health ?? 0),
    food: Math.round(bot.food ?? 0),
    pos: { x: Math.round(p.x), y: Math.round(p.y), z: Math.round(p.z) },
    task: currentTask,
    solo: currentTask === 'roam',
    dim: bot.game ? String(bot.game.dimension) : '?',
    is_night: bot.time ? bot.time.timeOfDay > 12500 && bot.time.timeOfDay < 23500 : false,
    // Mode tamu ikut status: nudge/takdir/prompt sisi Python membacanya
    // supaya tidak mendorong "ayo nambang" di dunia orang.
    tamu: MODE_TAMU,
    nearby_players: near.players,
    nearby_hostiles: near.hostiles,
    // Sumber makanan yang kelihatan. Arti sudah bisa `serang <hewan>` dan
    // `eat` (daging mentah pun diterima `foodsByName`), tapi tanpa daftar ini
    // dia tidak tahu ada apa di sekitarnya waktu perutnya tipis.
    nearby_animals: near.hewan,
    // Apa yang MATANYA lihat di sekitar (cache 30 dtk) — pohon/batu/bijih/air
    // beserta jaraknya. Tanpa ini dia buta sumber daya (lihat pandangan()).
    terlihat: pandangan(),
    // Isi tas ikut status. RCON BUKAN jalan keluar: server memotong balasan
    // `data get entity <p> Inventory` di ~128 karakter dengan "..." (dicoba
    // [date removed]), jadi lewat sana isinya tidak pernah lengkap. Bot memang
    // pemilik datanya.
    inv: bot.inventory.items().slice(0, 40).map((it) => ({
      name: it.name, count: it.count, slot: it.slot,
    })),
    held: (bot.heldItem && bot.heldItem.name) || null,
    // Tenggelam salah satu dari tiga hal yang operator minta jangan sampai
    // terjadi. Napas penuh 20 dan cuma turun waktu kepalanya di dalam air.
    oxygen: typeof bot.oxygenLevel === 'number' ? bot.oxygenLevel : 20,
    // Terang di tempat dia berdiri. Dibutuhkan sesudah `turun` ada: di dalam
    // cave `is_night` SELALU false padahal gelapnya justru di situ, dan gelap
    // itulah yang memanggil mob. Diambil dari kepala, bukan kaki, karena blok
    // kaki sering blok padat yang cahayanya 0.
    light: terangDiKepala(),
    // Inilah sinyal gelap yang dipakai perilaku, bukan `light` di atas.
    underground: langitTerhalang(),
    in_water: !!(bot.entity && bot.entity.isInWater),
    // Armor yang benar-benar DIPAKAI DI BADAN, bukan yang ada di tas. Dia
    // pernah membikin empat potong lalu membiarkannya di tas, jadi "punya"
    // bukan ukuran yang benar untuk rasa aman.
    armor: armorDipakai(),
    // Alat ukur menjelajah: jarak sekarang & rekor terjauh dari jangkar spawn.
    jelajah: jarakJelajah(),
    // Jasad yang barangnya mungkin masih tergeletak (drop despawn ~5 menit).
    // null = tidak ada yang layak dikejar.
    jasad: (jasadPos && Date.now() - jasadTs < 300000) ? {
      x: jasadPos.x, y: jasadPos.y, z: jasadPos.z,
      umur_dtk: Math.round((Date.now() - jasadTs) / 1000),
    } : null,
    // Rumah (peti pertama yang dia taruh) — titik pulang & titipan barang.
    rumah: rumahPos ? { x: rumahPos.x, y: rumahPos.y, z: rumahPos.z } : null,
    // Penghitung kumulatif untuk takdir; biome_n dari set biome yang terinjak.
    statistik: { bunuh: statistik.bunuh, bunuh_panah: statistik.bunuh_panah,
                 simpan_n: statistik.simpan_n, biome_n: biomeDikenal.size },
  }
}

// Cahaya paling terang di kepala: gabungan cahaya blok dan cahaya langit.
// 0 = gelap total (mob bisa muncul), 15 = paling terang.
function terangDiKepala () {
  if (!bot.entity) return 15
  const b = bot.blockAt(bot.entity.position.offset(0, 1, 0))
  if (!b) return 15
  const blok = typeof b.light === 'number' ? b.light : 0
  const langit = typeof b.skyLight === 'number' ? b.skyLight : 0
  return Math.max(blok, langit)
}

// Langit terhalang? TERUKUR [date removed]: `light` dari mineflayer TIDAK bisa
// dipakai -- di rongga batu tertutup penuh dia tetap melaporkan 15, jadi
// perilaku "gelap" tidak akan pernah terpicu di dalam cave. Yang ini dihitung
// sendiri dari blok di atas kepalanya, dan itu deterministik.
function langitTerhalang () {
  if (!bot.entity) return false
  const kaki = bot.entity.position.floored()
  for (let dy = 2; dy <= 24; dy++) {
    const b = bot.blockAt(kaki.offset(0, dy, 0))
    if (b && b.boundingBox === 'block') return true
  }
  return false
}

// Slot armor pemain di mineflayer: 5 kepala, 6 badan, 7 kaki, 8 sepatu,
// 45 tangan kiri. Dibaca dari `inventory.slots` supaya shield ikut terhitung.
function armorDipakai () {
  const slot = (bot.inventory && bot.inventory.slots) || []
  const out = []
  for (const i of [5, 6, 7, 8, 45]) {
    if (slot[i] && slot[i].name) out.push(slot[i].name)
  }
  return out
}

// ---------- events → stdout ----------

// POV penonton (Phase 3). Dipanggil SESUDAH spawn: WorldView membaca
// `bot.entity.position` saat ada yang connect, dan itu belum ada sebelumnya.
//
// Aturan keras yang sama dengan seluruh integrasi ini: POV itu hiasan, bot itu
// isinya. Apa pun yang gagal di sini TIDAK BOLEH menjatuhkan bot — tidak
// modulnya hilang, tidak portnya bentrok, tidak versinya tak didukung.
// Alasan lengkap kenapa server web-nya kita pegang sendiri: mc-bot/pov.js.
let povServer = null

function stopPovServer(selesai) {
  const srv = povServer
  povServer = null
  if (!srv) { if (selesai) selesai(); return }
  try { srv.close(selesai) } catch (e) { if (selesai) selesai() }
}

const GENGGAM_JEDA_MS = 5000
let genggamTerakhir = ''

setInterval(async () => {
  if (!bot.entity || TUGAS_TERLINDUNG.has(currentTask)) return
  const slotKiri = bot.inventory.slots[45]
  const sekarang = (slotKiri && slotKiri.name) || ''
  const musuhDekat = (nearbyEntities().hostiles || [])
    .some((m) => (m.distance || 999) <= 16)
  const gelap = langitTerhalang() ||
    (bot.time && bot.time.timeOfDay > 12500 && bot.time.timeOfDay < 23500)
  let mau = sekarang
  if (musuhDekat && jumlahDiTas('shield') > 0) mau = 'shield'
  else if (gelap && jumlahDiTas('torch') > 0 && !musuhDekat) mau = 'torch'
  else if (!gelap && sekarang === 'torch' && jumlahDiTas('shield') > 0) mau = 'shield'
  if (mau === sekarang || mau === genggamTerakhir) return
  genggamTerakhir = mau
  try {
    const it = bot.inventory.items().find((x) => x.name === mau)
    if (it) await bot.equip(it, 'off-hand')
  } catch (e) { /* slot sibuk: coba lagi putaran depan */ }
}, GENGGAM_JEDA_MS)

// Penjaga memori — pertahanan TERAKHIR, kini TIGA LAPIS ([date removed]).
// Reproduksi terukur di server: `mine stone 32` saat batunya terkubur +
// malam + mob mengeroyok = heap 286MB -> 3GB dalam ~30 DETIK (~100 MB/dtk;
// badai retry pathfinding di collectBlock), mati di ~4.3GB dengan GC 7 detik
// yang membekukan semuanya. Live [time removed]: 4x OOM, semuanya persis sesudah
// "Aksi Arti: mine ... nambang batu malam". Patroli lama 30 dtk KELEWATAN
// ledakan secepat itu (dua tick = sudah tewas) — kini 5 dtk, dan urutannya:
//   1. > HEAP_POV_MB    : korbankan POV (hiasan dulu, bot itu isinya)
//   2. > HEAP_BATAL_MB  : BATALKAN tugas yang rakus (cancelTask collectBlock
//                         + lepas goal pathfinder) — bot tetap hidup di dunia
//   3. > HEAP_KRITIS_MB : restart TERKENDALI (process.exit; runner respawn
//                         bersih dalam hitungan detik) — jauh lebih baik
//                         daripada sekarat 4GB sambil nge-freeze GC
// Ambang = persentase dari LIMIT NYATA (runner memasang cap via
// --max-old-space-size; tanpa cap, limit default ~4GB dan persentase yang
// sama tetap masuk akal). Angka absolut lama (2200/1600/2600) jadi bohong
// begitu cap-nya 1200.
const HEAP_LIMIT_MB = Math.round(
  require('v8').getHeapStatistics().heap_size_limit / 1048576)
const HEAP_POV_MB = Math.round(HEAP_LIMIT_MB * 0.55)
const HEAP_BATAL_MB = Math.round(HEAP_LIMIT_MB * 0.70)
const HEAP_KRITIS_MB = Math.round(HEAP_LIMIT_MB * 0.85)
let povDikorbankan = false
let heapBatalTerakhir = 0
setInterval(() => {
  const mb = Math.round(process.memoryUsage().heapUsed / 1048576)
  if (!povDikorbankan && povServer && mb > HEAP_POV_MB) {
    povDikorbankan = true
    log(`memori ${mb}MB — POV dimatikan demi kelangsungan bot`)
    stopPovServer()
    emit({ ev: 'pov_mati', reason: 'hemat_memori', heap_mb: mb })
  }
  if (mb > HEAP_KRITIS_MB) {
    log(`MEMORI KRITIS ${mb}MB — restart terkendali; runner menghidupkan ulang`)
    emit({ ev: 'bot_restart_memori', heap_mb: mb })
    setTimeout(() => process.exit(1), 300)
    return
  }
  if (mb > HEAP_BATAL_MB && Date.now() - heapBatalTerakhir > 20000) {
    heapBatalTerakhir = Date.now()
    log(`memori ${mb}MB — tugas '${currentTask}' dibatalkan paksa demi bot`)
    try {
      if (bot.collectBlock && bot.collectBlock.cancelTask) {
        bot.collectBlock.cancelTask()
      }
    } catch (e) { /* sudah tidak ada task */ }
    try { bot.pathfinder.setGoal(null) } catch (e) { /* goal kosong */ }
    emit({ ev: 'task_failed', task: currentTask, reason: 'memori_bengkak' })
    currentTask = 'idle'
  }
}, 5000)

function startPovServer() {
  try {
    const { startPov } = require('./pov')
    povServer = startPov(bot, {
      port: POV_PORT,
      mode: POV_MODE,
      cycleSec: POV_CYCLE_SEC,
      bodySec: POV_BODY_SEC,
      slim: POV_SLIM,
      viewDistance: POV_VIEW_DISTANCE,
      smooth: POV_SMOOTH,
      log,
    })
  } catch (e) {
    log('POV gagal dinyalakan (bot tetap jalan):', String(e.message || e))
  }
}

bot.once('spawn', () => {
  movements = new Movements(bot)
  // JANGAN biarkan pathfinder menumpuk blok di bawah kaki saat merencanakan
  // jalur naik. Keluhan operator [date removed] malam: "pas dia mau taruh blok di
  // bawah kaki sering miss, jadi loncat-loncat doang di tempat" — eksekusi
  // scaffolding pathfinder memang rawan meleset di Paper, dan hasilnya
  // lompatan kosong berulang (sebagian dari epidemi "nyangkut"). Naik yang
  // DISENGAJA tetap ada lewat verb `menara` yang menunggu ketinggian nyata
  // dan memverifikasi tiap blok dari dunia.
  movements.allow1by1towers = false
  bot.pathfinder.setMovements(movements)
  // AKAR OOM 4GB (reproduksi terukur [date removed]): goal yang TAK TERJANGKAU
  // membuat pathfinder menghitung ulang A* SINKRON tiap path kosong, dengan
  // default thinkTimeout 5000 ms dan searchRadius -1 (TANPA BATAS) — plus
  // movements boleh menggali, ruang pencariannya jutaan node. Hasil ukur:
  // heap 286MB -> 3GB dalam ~30 dtk (mine stone yang tak terjangkau / roam
  // saat terkurung), event loop beku (watchdog ikut kelaparan), mati ~4.3GB.
  // Dua rem ini membatasi TIAP pencarian, bukan melarang perilakunya.
  bot.pathfinder.thinkTimeout = 200
  bot.pathfinder.searchRadius = 64
  const p = bot.entity.position
  homePos = { x: p.x, y: p.y, z: p.z }  // titik acuan jelajah solo
  spawnedAt = Date.now()                // mulai masa tenggang deteksi streamer
  // `health` sengaja TIDAK dilaporkan di sini: `bot.health` belum termuat saat
  // 'spawn', jadi `?? 20` dulu selalu berbohong 20 walau HP tersimpan 6.
  // Nilai sebenarnya datang lewat event `status` beberapa detik kemudian.
  emit({ ev: 'spawned', pos: { x: Math.round(p.x), y: Math.round(p.y), z: Math.round(p.z) },
         username: bot.username })
  log('spawned as', bot.username, 'at', p)
  startPovServer()
  setFollow()
  setInterval(() => {
    const s = statusEvent()
    if (s) emit(s)
  }, STATUS_SEC * 1000)
})

// Siapa yang barusan melukai bot — dari paket damage_event server (MC [time removed].4+).
// Sebelum ini sumber luka cuma DITEBAK dari mob terdekat: pukulan PEMAIN tidak
// pernah terdeteksi (pemain tidak masuk daftar hostile), dan kalau kebetulan
// ada zombie nongkrong di dekat situ, pukulan operator malah dituduhkan ke zombie.
// Nilai id di paket ber-offset +1 (0 = tidak ada).
let lastDamager = null      // { name, kind } | null
let lastDamagerAt = 0
let lastDamageType = ''     // 'fall' | 'arrow' | 'magic' | 'lava' | ...

// Peta id -> nama tipe luka, DIAMBIL dari server saat login (paket
// `registry_data`, MC [time removed].5+). mineflayer tidak menyediakannya sendiri
// (`bot.registry.damageTypes` tidak ada — sudah dicek [date removed]), dan
// menghardcode angkanya rapuh: urutannya milik server/datapack, bukan konstan.
// Diverifikasi di server operator: 49 entri, fall=10, arrow=0, magic=27,
// lava=24, mob_attack=28.
const damageTypes = {}
try {
  bot._client.on('registry_data', (p) => {
    if (!p || !String(p.id || '').includes('damage_type')) return
    ;(p.entries || []).forEach((e, i) => {
      const nama = String(e.key || e.name || '').replace(/^minecraft:/, '')
      if (nama) damageTypes[i] = nama
    })
    log('tipe luka dikenali:', Object.keys(damageTypes).length)
  })
} catch (e) {
  log('registry tipe luka tidak tersedia:', e.message)
}

function resolveEntityLabel(rawId) {
  if (!rawId) return null
  const e = bot.entities[rawId - 1]
  if (!e) return null
  if (e.type === 'player' && e.username) return { name: e.username, kind: 'player' }
  return { name: e.name || 'unknown', kind: e.type || 'entity' }
}

try {
  bot._client.on('damage_event', (packet) => {
    if (!bot.entity || packet.entityId !== bot.entity.id) return
    const who =
      resolveEntityLabel(packet.sourceCauseId) ||
      resolveEntityLabel(packet.sourceDirectId)
    // Tipe luka dicatat SELALU, walau tidak ada pelakunya — justru di situ
    // gunanya: jatuh, lava, racun, dan potion tidak punya entity penyebab
    // tapi tetap kejadian yang berbeda-beda buat Arti.
    lastDamageType = damageTypes[packet.sourceTypeId] || ''
    lastDamagerAt = Date.now()
    lastDamager = who || null
  })
} catch (e) {
  log('damage_event tidak tersedia, pakai tebakan mob terdekat:', e.message)
}

// Kabur otomatis: dipanggil TIAP kali dia kehilangan darah. Ini yang
// membedakan "bereaksi" dari "jadi sasaran" — di log 6-7 Agustus dia terus
// berjalan sambil ditembaki sampai mati, empat kali.
function cobaKaburOtomatis(h) {
  if (!(FLEE_HP > 0) || h > FLEE_HP) return
  if (currentTask === 'renang') return   // kendali langsung, bukan pathfinder
  if (currentTask === 'kabur') { fleeUntil = Date.now() + FLEE_MS; return }
  const musuh = hostileTerdekat()
  if (musuh) mulaiKabur(musuh, 'darah_tipis')
}

bot.on('health', () => {
  const h = Math.round(bot.health ?? 0)
  if (lastHealth === null) {   // pembacaan pertama = titik acuan, bukan luka
    lastHealth = h
    return
  }
  if (h < lastHealth) {
    const near = nearbyEntities()
    // Paket damage_event datang tepat sebelum health berubah; > 1 dtk = basi.
    // Paket bisa datang tanpa pelaku (jatuh, lava, racun, potion) — yang
    // penting kesegarannya, bukan adanya `lastDamager`.
    const segar = Date.now() - lastDamagerAt < 1000
    const tipe = segar ? lastDamageType : ''
    const fresh = segar && (lastDamager || tipe)
    if (fresh) {
      emit({
        ev: 'hurt',
        health: h,
        // Tanpa pelaku, tipe lukanya YANG jadi sumber: "fall", "lava",
        // "magic" (potion), "arrow". Aturan operator [date removed] — sumber baru
        // apa pun bentuknya boleh menyela, dan itu termasuk fall damage.
        source: (lastDamager && lastDamager.name) || tipe || 'unknown',
        source_kind: (lastDamager && lastDamager.kind) || 'lingkungan',
        damage_type: tipe,
      })
      if (h <= 6) emit({ ev: 'low_health', health: h })
      cobaKaburOtomatis(h)
      lastHealth = h
      // JANGAN null-kan di sini: `death` datang di milidetik yang sama dan
      // butuh membacanya. Audit verifikasi [date removed]: hanya 1 dari 3
      // kematian yang dapat nama pembunuh — sisanya kosong padahal pelakunya
      // sudah diketahui 0 ms sebelumnya. TTL 3 dtk yang mengurus kebasian.
      return
    }
    emit({ ev: 'hurt', health: h,
           source: near.hostiles.length ? near.hostiles[0].kind : 'unknown' })
    if (h <= 6) emit({ ev: 'low_health', health: h })
    cobaKaburOtomatis(h)
  }
  lastHealth = h
})

let jasadPos = null
let jasadTs = 0
// Rumah = peti PERTAMA yang dia taruh sendiri (fitur #4, [date removed] malam).
// Satu titik pulang: tempat menitip barang berharga supaya kematian murah.
let rumahPos = null
// Statistik seumur-sesi (fondasi predikat takdir, [date removed]).
const statistik = { bunuh: 0, bunuh_panah: 0, simpan_n: 0 }

bot.on('death', () => {
  // Pembunuhnya SUDAH diketahui dari damage_event sepersekian detik lalu.
  // Audit [date removed]: dulu killer selalu dikosongkan, jadi momen paling
  // dramatis sesi dikirim ke LLM tanpa pelakunya dan Arti harus mengarang.
  const segar = lastDamager && Date.now() - lastDamagerAt < 3000
  // Posisi JASAD dicatat SEBELUM respawn menimpanya: keepInventory=false
  // berarti seluruh gear tergeletak di sini dan despawn ~5 menit. 7-15 mati
  // per sesi live tanpa satu pun upaya balik = progres tangga aman menguap
  // berulang-ulang (akar fitur "kembali ke jasad", [date removed]).
  if (bot.entity && bot.entity.position) {
    jasadPos = bot.entity.position.floored()
    jasadTs = Date.now()
  }
  emit({
    ev: 'death',
    message: '',
    killer: segar ? lastDamager.name : '',
    killer_kind: segar ? lastDamager.kind : '',
    pos: jasadPos ? { x: jasadPos.x, y: jasadPos.y, z: jasadPos.z } : null,
  })
  log('died')
})

bot.on('respawn', () => {
  if (!bot.entity) return
  setTimeout(() => {
    if (!bot.entity) return
    const p = bot.entity.position
    // Posisi dibaca SESUDAH jeda: tepat saat event respawn, bot.entity masih
    // memegang posisi KEMATIAN, bukan tempat dia bangun.
    emit({ ev: 'respawn', pos: { x: Math.round(p.x), y: Math.round(p.y), z: Math.round(p.z) } })
    // Titik acuan jelajah diperbarui — kalau tidak, sesudah mati dia berjalan
    // puluhan blok balik ke tempat yang barusan membunuhnya (terukur 89 blok).
    homePos = { x: p.x, y: p.y, z: p.z }
    // Perintah `stop` HARUS bertahan melewati kematian. Audit [date removed]:
    // dulu setFollow() dipanggil tanpa syarat, jadi bot yang disuruh diam
    // ngeloyor jalan-jalan begitu dia mati.
    if (currentTask === 'idle') return
    setFollow()
  }, 3000)
})

bot.on('chat', (username, message) => {
  if (username === bot.username) return
  emit({ ev: 'chat', from: username, text: String(message).slice(0, 200) })
})

bot.on('death_screen', () => {})

bot.on('kicked', (reason) => {
  // `reason` di versi mineflayer ini adalah objek komponen chat — String()
  // menghasilkan "[object Object]", yang lalu masuk prompt dan berpotensi
  // TERUCAP di depan penonton (audit [date removed], terbukti 2x).
  const teks =
    reason && typeof reason === 'object'
      ? String(reason.text ?? reason.translate ?? JSON.stringify(reason))
      : String(reason)
  emit({ ev: 'kicked', reason: teks.slice(0, 200) })
  process.exit(1)
})

bot.on('error', (err) => {
  emit({ ev: 'error', where: 'bot', message: String(err && err.message || err).slice(0, 200) })
  process.exit(1)
})

bot.on('end', (reason) => {
  emit({ ev: 'error', where: 'end', message: String(reason || 'connection ended') })
  process.exit(1)
})

// Jendela/GUI terbuka-tertutup. Berguna untuk dua hal: memastikan momen
// "klik E" (invsee) benar-benar sampai ke klien, dan nanti untuk buka peti.
bot.on('windowOpen', (w) => {
  emit({ ev: 'window_open', kind: String(w && w.type), title: String(w && w.title).slice(0, 60) })
})
bot.on('windowClose', () => { emit({ ev: 'window_close' }) })

// Pemain masuk. Dipakai kamera siaran: kalau klien kamera relog, mode
// spectator-nya tersimpan tapi TARGET tontonannya hilang — dan itu tidak
// terdeteksi dari gamemode. Event ini pemicu yang tepat untuk mengunci ulang.
bot.on('playerJoined', (p) => {
  if (p && p.username && p.username !== bot.username) {
    emit({ ev: 'player_join', name: String(p.username).slice(0, 16) })
  }
})

// hostile mendekat (cek tiap 3 dtk, edge-trigger sederhana; rate-limit di Python)
let lastHostileEmit = 0
setInterval(() => {
  if (!bot.entity) return
  const near = nearbyEntities()
  const close = near.hostiles.find(h =>
    (h.kind === 'creeper' && h.distance <= 6) || h.distance <= 4)
  if (close && Date.now() - lastHostileEmit > 10000) {
    lastHostileEmit = Date.now()
    emit({ ev: 'hostile_near', kind: close.kind, distance: close.distance })
  }
}, 3000)

// ---------- commands ← stdin ----------

const handlers = {
  follow() {
    roamManual = false
    setFollow()
    emit({ ev: 'task_done', task: 'follow', detail: 'mode follow aktif' })
  },
  roam() {
    roamManual = true
    startRoam('manual')
    emit({ ev: 'task_done', task: 'roam', detail: 'jelajah sendiri' })
  },
  come() {
    const target = player(STREAMER)
    if (!target) { emit({ ev: 'task_failed', task: 'come', reason: 'streamer_not_visible' }); return }
    currentTask = 'come'
    comeSince = Date.now()
    bot.pathfinder.setGoal(new goals.GoalNear(
      target.position.x, target.position.y, target.position.z, 2))
  },
  say(c) {
    const text = String(c.text || '').slice(0, 80)
    if (text && !text.startsWith('/')) bot.chat(text)
  },
  stop() {
    currentTask = 'idle'
    roamManual = false
    roamTarget = null
    bot.pathfinder.setGoal(null)
    emit({ ev: 'task_done', task: 'stop', detail: 'diam' })
  },
  status() { const s = statusEvent(); if (s) emit(s) },
  async mine(c) {
    const nama = String((c && c.block) || '').trim()
    const minta = Math.max(1, Math.min(32, parseInt((c && c.count) || 1, 10) || 1))
    // Nama blok DIVERIFIKASI lagi di sini: parser Python sudah menyaring
    // dengan allowlist, tapi nama yang lolos regex belum tentu blok nyata di
    // versi ini (mis. halusinasi "copper_log").
    const jenis = bot.registry.blocksByName[nama]
    if (!jenis) {
      emit({ ev: 'task_failed', task: 'mine', reason: 'blok_tak_dikenal', detail: nama })
      return
    }
    const titik = bot.findBlocks({ matching: jenis.id, maxDistance: 32, count: minta })
    if (!titik.length) {
      emit({ ev: 'task_failed', task: 'mine', reason: 'tidak_ketemu', detail: nama })
      return
    }
    // HANYA blok terekspos (punya sisi udara). Blok terkubur rapat adalah
    // pemicu badai retry collectBlock yang terukur meledakkan heap 100 MB/dtk
    // (reproduksi [date removed]: mine stone 32 di taiga malam). Menggali ke
    // kedalaman itu tugasnya `turun`, bukan `mine`.
    const terekspos = (v) => [[1, 0, 0], [-1, 0, 0], [0, 1, 0],
                              [0, -1, 0], [0, 0, 1], [0, 0, -1]]
      .some(([dx, dy, dz]) => {
        const t = bot.blockAt(v.offset(dx, dy, dz))
        return t && t.boundingBox === 'empty'
      })
    let kandidat = titik.filter(terekspos)
    if (!kandidat.length) {
      emit({ ev: 'task_failed', task: 'mine', reason: 'terkubur_semua', detail: nama })
      return
    }
    // Saring sekali lagi: hanya target yang JALURNYA BENAR-BENAR ADA (A* 60
    // ms per target, wajib status 'success'). "Terekspos" saja tidak cukup —
    // blok bisa menghadap gua di kedalaman yang tak tercapai, dan SATU target
    // tanpa jalur = badai retry collectBlock (~120 MB/dtk terukur; bot uji #5
    // tewas OOM di detik 30 walau ada batas waktu per target, karena event
    // loop ikut tersumbat). Pola yang sama dengan pemeriksaan kandidat roam.
    kandidat = kandidat.filter((v) => {
      if (!movements) return true
      try {
        const hasil = bot.pathfinder.getPathTo(
          movements, new goals.GoalGetToBlock(v.x, v.y, v.z), 60)
        return Boolean(hasil && hasil.status === 'success')
      } catch (e) {
        return false
      }
    })
    if (!kandidat.length) {
      emit({ ev: 'task_failed', task: 'mine', reason: 'terkubur_semua', detail: nama })
      return
    }
    const blok = kandidat.map((v) => bot.blockAt(v)).filter(Boolean)
    const sebelum = jumlahDiTas(nama)
    const tugasLama = currentTask
    currentTask = 'nambang'
    roamTarget = null
    emit({ ev: 'collect_start', block: nama, count: blok.length })
    // SATU TARGET SEKALI JALAN, maksimal 10 dtk per target, 45 dtk total.
    // Dulu seluruh array diserahkan ke collectBlock sekaligus — satu target
    // yang jadi tak terjangkau (mob mendorong, blok berubah) membuatnya
    // mengulang pathfinding tanpa henti, dan bot #3 uji [date removed] tewas OOM
    // di detik 48 (timeout total 45 dtk kalah cepat dari badainya). Per
    // target: badai terburuk dibatasi 10 dtk lalu targetnya DILEWATI.
    const batasTotal = Date.now() + 45000
    const epok = epokTugas
    try {
      for (const b of blok) {
        if (Date.now() > batasTotal) break
        // Sudah diambil alih perintah lain? Berhenti di target berikutnya.
        // Tanpa ini loop ini terus menambang di belakang layar sementara aksi
        // baru juga berjalan — dua tugas berebut badan yang sama.
        if (epok !== epokTugas) {
          log(`nambang dihentikan: diambil alih perintah baru (epok ${epok} -> ${epokTugas})`)
          break
        }
        const satu = setTimeout(() => {
          try { bot.collectBlock.cancelTask() } catch (e2) { /* selesai */ }
        }, 10000)
        try {
          await bot.collectBlock.collect(b)
        } catch (e) {
          // target ini gagal/di-cancel: lanjut target berikutnya
        } finally {
          clearTimeout(satu)
        }
      }
      const dapat = Math.max(0, jumlahDiTas(nama) - sebelum)
      if (dapat > 0) {
        emit({ ev: 'collect_done', block: nama, count: dapat })
      } else {
        emit({ ev: 'task_failed', task: 'mine', reason: 'gagal_nambang',
               detail: 'semua target gagal dijangkau' })
      }
    } finally {
      // Balik ke apa pun yang tadi dia lakukan; setFollow sendiri yang
      // memutuskan menemani atau menjelajah.
      if (currentTask === 'nambang') {
        currentTask = tugasLama === 'roam' ? 'roam' : 'idle'
        setFollow()
      }
    }
  },
  // LUBANG AMAN — berlindung TANPA MODAL. Live [time removed]-[time removed]: 10 kematian
  // dalam 40 menit karena dia jalan-jalan di malam pertama tanpa senjata,
  // tanpa armor, TANPA BLOK — jadi `bangun` (butuh ~20 blok) pun mustahil.
  // Yang dilakukan pemain sungguhan: gali 2 blok ke bawah, tutup atas kepala
  // dengan blok hasil galian itu sendiri. Zombie tidak bisa menjangkau,
  // skeleton kehilangan garis tembak.
  async lubang_aman () {
    const tugasLama = currentTask
    currentTask = 'lubang'
    roamTarget = null
    bot.pathfinder.setGoal(null)
    const yAwal = Math.round(bot.entity.position.y)
    emit({ ev: 'lubang_start' })
    let alasan = ''
    try {
      await tungguDiam()
      for (let i = 0; i < 2; i++) {
        const kaki = bot.entity.position.floored()
        const bawah = bot.blockAt(kaki.offset(0, -1, 0))
        if (!bawah || bawah.name === 'bedrock' || bawah.boundingBox !== 'block') {
          alasan = alasan || 'tidak_bisa_digali'
          break
        }
        if (CAIRAN.has(bawah.name)) { alasan = 'ada_cairan_di_bawah'; break }
        try {
          if (bot.tool) await bot.tool.equipForBlock(bawah, { requireHarvest: false })
          await bot.dig(bawah)
        } catch (e) { alasan = 'gagal_menggali'; break }
        const batas = Date.now() + 2500
        while (Date.now() < batas &&
               Math.round(bot.entity.position.y) >= kaki.y) {
          await new Promise((r) => setTimeout(r, 100))
        }
      }
      const dalam = Math.max(0, yAwal - Math.round(bot.entity.position.y))
      // TUTUP atas kepala. Blok apa pun boleh — ini soal nyawa, bukan gaya
      // (blokTerbanyakDiTas sudah menyimpan kayu sebagai cadangan terakhir).
      let tertutup = false
      if (dalam >= 1) {
        const { nama } = blokTerbanyakDiTas()
        const jenis = nama && bot.registry.itemsByName[nama]
        const atas = bot.entity.position.floored().offset(0, 2, 0)
        const isi = bot.blockAt(atas)
        if (isi && isi.boundingBox === 'block') tertutup = true
        else if (jenis) {
          await naruhSatu(atas, nama, jenis)
          const cek = bot.blockAt(atas)
          tertutup = Boolean(cek && cek.boundingBox === 'block')
        }
      }
      if (dalam < 1) {
        alasan = alasan || 'gagal_menggali'
      } else {
        emit({ ev: 'lubang_done', dalam, tertutup })
        // Diam di dalam sampai aman/pagi — pola yang sama dengan `bertahan`
        // sesudah menembok diri.
        bertahanSampai = Date.now() + BERTAHAN_PERPANJANG_MS
      }
    } finally {
      if (alasan) {
        emit({ ev: 'task_failed', task: 'lubang_aman', reason: alasan })
      }
      if (currentTask === 'lubang') {
        currentTask = tugasLama === 'roam' ? 'roam' : 'idle'
        setFollow()
      }
    }
  },
  // UJI COR: cetak SATU obsidian di sel (x,y,z) — primitif portal tanpa
  // diamond. Urutan: ciduk lava dari pool terdekat -> tuang ke sel target ->
  // tuang air ke sel tetangga (mengalir ke lava) -> obsidian -> ciduk air
  // balik. Verifikasi: blockAt(target) === obsidian.
  async cor_uji (c) {
    const sel = bot.entity.position.floored()
      .offset(parseInt(c.dx || 2, 10), parseInt(c.dy || 0, 10),
              parseInt(c.dz || 0, 10))
    log(`cor_uji: target ${sel}`)
    let r = await cidukCairan('lava')
    if (r) { emit({ ev: 'task_failed', task: 'cor', reason: r }); return }
    log('cor_uji: lava terciduk')
    r = await tuangKe(sel, 'lava_bucket')
    if (r) { emit({ ev: 'task_failed', task: 'cor', reason: r }); return }
    await new Promise((rr) => setTimeout(rr, 400))
    const isi = bot.blockAt(sel)
    log(`cor_uji: sel kini ${isi && isi.name}`)
    if (!isi || isi.name !== 'lava') {
      emit({ ev: 'task_failed', task: 'cor', reason: 'lava_meleset',
             detail: String(isi && isi.name) })
      return
    }
    // Air dituang ke sel TETANGGA (bukan langsung ke lava — klik ke lava
    // malah menciduk/nabrak): pilih tetangga horizontal yang kosong dan
    // beralas padat.
    let selAir = null
    for (const [dx, dz] of [[1, 0], [-1, 0], [0, 1], [0, -1]]) {
      const t = sel.offset(dx, 0, dz)
      const b = bot.blockAt(t)
      const alas = bot.blockAt(t.offset(0, -1, 0))
      if (b && b.boundingBox === 'empty' && alas && alas.boundingBox === 'block') {
        selAir = t
        break
      }
    }
    if (!selAir) { emit({ ev: 'task_failed', task: 'cor', reason: 'tanpa_sel_air' }); return }
    r = await tuangKe(selAir, 'water_bucket')
    if (r) { emit({ ev: 'task_failed', task: 'cor', reason: r }); return }
    await new Promise((rr) => setTimeout(rr, 800))
    // Ciduk airnya balik (sumbernya di selAir).
    if (await pegang('bucket')) {
      await klikEmber(selAir.offset(0.5, 0.5, 0.5))
    }
    const hasil = bot.blockAt(sel)
    log(`cor_uji: hasil ${hasil && hasil.name}`)
    if (hasil && hasil.name === 'obsidian') {
      emit({ ev: 'cor_done', pos: { x: sel.x, y: sel.y, z: sel.z } })
    } else {
      emit({ ev: 'task_failed', task: 'cor', reason: 'bukan_obsidian',
             detail: String(hasil && hasil.name) })
    }
  },
  // RANTAI ALAT dalam SATU perintah (operator [date removed]: "ada inisiatif bikin
  // tools ga? capek sih ini kalau step by step, kapan ke nethernya wkwk").
  // Dulu tiap anak tangga kayu butuh SATU giliran LLM (~20-30 dtk, kadang
  // tersapu TTL) — kini satu tag merakit SEMUA yang bisa dirakit dari isi
  // tas, memakai handler craft yang sama (verifikasi dunia + panel + event
  // per langkah — kejujurannya tidak berubah, cuma tidak menunggu LLM
  // mengeja tiap langkah).
  async siapkan_alat () {
    const punya = (...nama) => nama.some((x) => jumlahDiTas(x) > 0)
    const totalPapan = () => KAYU_PAPAN.reduce((a, x) => a + jumlahDiTas(x), 0)
    const logTerbanyak = () => {
      let terbaik = ''
      let n = 0
      for (const nama of KAYU_LOG) {
        const c = jumlahDiTas(nama)
        if (c > n) { terbaik = nama; n = c }
      }
      return terbaik
    }
    const dibuat = []
    const bikin = async (item, count) => {
      const sebelum = jumlahDiTas(item)
      try { await handlers.craft({ item, count }) } catch (e) { /* lanjut */ }
      if (jumlahDiTas(item) > sebelum) dibuat.push(item)
    }
    if (totalPapan() < 5) {
      const kayu = logTerbanyak()
      if (kayu) await bikin(kayu.replace('_log', '_planks'), 4)
    }
    if (jumlahDiTas('crafting_table') === 0 && totalPapan() >= 5) {
      await bikin('crafting_table', 1)
    }
    if (jumlahDiTas('stick') < 2 && totalPapan() >= 2) await bikin('stick', 1)
    if (!punya('wooden_pickaxe', 'stone_pickaxe', 'iron_pickaxe',
               'diamond_pickaxe') &&
        totalPapan() >= 3 && jumlahDiTas('stick') >= 2) {
      await bikin('wooden_pickaxe', 1)
    }
    if (jumlahDiTas('cobblestone') >= 3) {
      if (!punya('stone_pickaxe', 'iron_pickaxe', 'diamond_pickaxe')) {
        await bikin('stone_pickaxe', 1)
      }
      if (!punya('stone_sword', 'iron_sword', 'diamond_sword') &&
          jumlahDiTas('cobblestone') >= 2) {
        await bikin('stone_sword', 1)
      }
    }
    if (jumlahDiTas('iron_ingot') >= 3 &&
        !punya('iron_pickaxe', 'diamond_pickaxe')) {
      await bikin('iron_pickaxe', 1)
    }
    if (jumlahDiTas('iron_ingot') >= 2 && !punya('iron_sword', 'diamond_sword')) {
      await bikin('iron_sword', 1)
    }
    if (dibuat.length) {
      emit({ ev: 'alat_siap', dibuat })
    } else {
      emit({ ev: 'task_failed', task: 'siapkan_alat',
             reason: 'tidak_ada_yang_bisa_dirakit' })
    }
  },
  // Craft (Phase 2). Dua jalur yang beda di mata penonton: resep 2x2 dikerjakan
  // di inventory sendiri (berdiri di tempat), resep 3x3 wajib meja — dia harus
  // JALAN ke meja dulu. `bot.craft` sendiri yang memanggil `activateBlock`,
  // dan `activateBlock` memanggil `lookAt(meja)` sebelum mengirim paket
  // klik-kanan; jadi Arti benar-benar menghadap meja dan mengayun tangan.
  async craft (c) {
    const nama = String((c && c.item) || '').trim()
    const minta = Math.max(1, Math.min(8, parseInt((c && c.count) || 1, 10) || 1))
    const jenis = bot.registry.itemsByName[nama]
    if (!jenis) {
      emit({ ev: 'task_failed', task: 'craft', reason: 'item_tak_dikenal', detail: nama })
      return
    }

    // Jalur 2x2 dulu: kalau muat di inventory, tidak perlu cari meja sama sekali.
    let resep = bot.recipesFor(jenis.id, null, minta, null)[0]
    let meja = null
    let ukuran = 2
    if (!resep) {
      ukuran = 3
      const jenisMeja = bot.registry.blocksByName.crafting_table
      let posMeja = jenisMeja
        ? bot.findBlock({ matching: jenisMeja.id, maxDistance: 48 })
        : null
      if (!posMeja && jumlahDiTas('crafting_table') > 0) {
        // Dia MEMBAWA mejanya — pasang, jangan menyerah (pola yang sama
        // dengan furnace di `masak`). Live [time removed]: keluhan operator "dia muter
        // muter doang" salah satunya karena rantai kayu putus di sini: meja
        // sudah dibikin tapi craft tetap gagal tidak_ada_meja.
        await tungguDiam()
        const spot = titikKosong(false)
        if (spot) {
          const gagal = await naruhSatu(spot, 'crafting_table',
                                        bot.registry.itemsByName.crafting_table)
          if (!gagal) posMeja = bot.blockAt(spot)
        }
      }
      if (!posMeja) {
        // Bedakan "tidak ada meja" dari "bahan kurang": kalau resepnya memang
        // tidak ada pun, jangan suruh dia mencari meja yang percuma.
        const adaResep = bot.recipesAll(jenis.id, null, true).length > 0
        emit({ ev: 'task_failed', task: 'craft',
               reason: adaResep ? 'tidak_ada_meja' : 'tidak_ada_resep', detail: nama })
        return
      }
      // Bahan dicek SEBELUM melangkah. Terukur di audit [date removed]: dia jalan
      // 25 blok ke meja lalu baru lapor 'bahan_kurang' — buang waktu, dan di
      // siaran terlihat seperti dia tidak tahu isi tasnya sendiri. Cek bahan
      // tidak butuh dia berdiri di sebelah mejanya; blok yang sudah ketemu
      // cukup untuk memenuhi syarat `requiresTable`.
      resep = bot.recipesFor(jenis.id, null, minta, posMeja)[0]
      if (!resep) {
        emit({ ev: 'task_failed', task: 'craft', reason: 'bahan_kurang', detail: nama })
        return
      }
      const tugasLama = currentTask
      currentTask = 'craft'
      roamTarget = null
      emit({ ev: 'craft_walk', item: nama,
             distance: Math.round(bot.entity.position.distanceTo(posMeja.position)) })
      try {
        await bot.pathfinder.goto(new goals.GoalNear(
          posMeja.position.x, posMeja.position.y, posMeja.position.z, 2))
      } catch (e) {
        currentTask = tugasLama
        setFollow()
        // pathfinder membedakan dua hal yang terdengar sama: TIDAK ADA JALAN
        // ke meja, versus ada yang mengambil alih tujuannya di tengah jalan
        // (perintah `stop`, kabur dari mob, deteksi nyangkut). Yang kedua
        // bukan salah mejanya, dan Arti tidak boleh menceritakannya begitu.
        const pesan = String((e && e.message) || e)
        emit({ ev: 'task_failed', task: 'craft',
               reason: /goal was changed|goal_changed|interrupt/i.test(pesan)
                 ? 'dibatalkan' : 'meja_tak_terjangkau',
               detail: pesan.slice(0, 80) })
        return
      }
      // Sampai. Acuan bloknya disegarkan (chunk bisa saja dimuat ulang di
      // jalan), lalu resep dipilih ulang dari isi tas TERBARU — dia bisa saja
      // kehilangan bahan di perjalanan (mati, dirampok, jatuh ke lava).
      meja = bot.blockAt(posMeja.position)
      resep = (meja && bot.recipesFor(jenis.id, null, minta, meja)[0]) || null
      if (!resep) {
        currentTask = tugasLama
        setFollow()
        emit({ ev: 'task_failed', task: 'craft',
               reason: meja ? 'bahan_kurang' : 'meja_hilang', detail: nama })
        return
      }
    }

    const tugasLama = currentTask === 'craft' ? 'idle' : currentTask
    currentTask = 'craft'
    roamTarget = null
    bot.pathfinder.setGoal(null)   // berhenti jalan: orang crafting itu diam
    const sebelum = jumlahDiTas(nama)
    // Panel butuh resepnya SEBELUM crafting mulai — kalau dikirim belakangan,
    // penonton cuma lihat hasilnya muncul tanpa tahu bahannya apa.
    // `table_dist` ikut dikirim supaya laporan seperti "dia craft padahal jauh
    // dari meja" (operator, [date removed]) bisa DIBUKTIKAN dari log, bukan jadi
    // cerita. Sudah dicoba direproduksi dalam isolasi dan TIDAK terjadi — tapi
    // tanpa angka ini, kejadian berikutnya tetap tidak bisa didiagnosis.
    emit({ ev: 'craft_start', item: nama, count: minta, size: ukuran,
           table: !!meja, grid: bentukResep(resep), result: nama,
           table_dist: meja
             ? Math.round(bot.entity.position.distanceTo(meja.position) * 10) / 10
             : null })
    try {
      await bot.craft(resep, Math.ceil(minta / (resep.result.count || 1)), meja)
      const dapat = Math.max(0, jumlahDiTas(nama) - sebelum)
      // Pegang hasilnya: tanpa ini kerjanya tidak kelihatan sama sekali dari
      // kamera — barangnya cuma masuk tas.
      // Dipakai di tempat yang benar, bukan selalu di tangan: armor ke badan,
      // shield ke tangan kiri. Tanpa ini dia menggenggam helm di depan kamera
      // dan pelindungnya tidak berfungsi.
      try { await bot.equip(jenis.id, slotArmor(nama)) } catch (e) { /* penuh/aneh: bukan gagal craft */ }
      emit({ ev: 'crafted', item: nama, count: dapat, table: !!meja })
      // Berdiri menatap hasilnya. `currentTask` tetap 'craft', jadi detektor
      // nyangkut membiarkannya dan penjaga di `finally` tidak akan menimpa
      // kalau ada yang lebih penting mengambil alih (kabur dari mob).
      if (CRAFT_PAUSE_MS) await new Promise((r) => setTimeout(r, CRAFT_PAUSE_MS))
    } catch (e) {
      emit({ ev: 'task_failed', task: 'craft', reason: 'gagal_craft',
             detail: String(e.message || e).slice(0, 80) })
    } finally {
      if (currentTask === 'craft') {
        currentTask = tugasLama === 'roam' ? 'roam' : 'idle'
        setFollow()
      }
    }
  },
  // BANGUN tempat berlindung. Bentuk yang paling sering dipakai pemain
  // sungguhan saat malam keburu datang: berdiri di satu titik lalu menembok
  // diri sendiri. Semua target ada dalam jangkauan tangan, jadi TIDAK perlu
  // navigasi sama sekali — dan itu yang membuatnya bisa diandalkan.
  //
  // Ini BUKAN "membangun apa saja". Rumah berbentuk bebas butuh perencanaan
  // ruang, dan prompt tetap jujur bahwa dia belum bisa itu.
  async bangun (c) {
    const nama = String((c && c.block) || 'cobblestone').trim()
    const jenis = bot.registry.itemsByName[nama]
    if (!jenis) {
      emit({ ev: 'task_failed', task: 'bangun', reason: 'blok_tak_dikenal', detail: nama })
      return
    }
    // Tempat berlindung wajib blok PADAT. Allowlist place memuat torch (sah
    // untuk `place`), tapi shelter dari obor itu pagar cahaya yang ditembus
    // panah — live [time removed] dia "bangun tembok obor darurat" dan tetap tertembak.
    const wujud = bot.registry.blocksByName[nama]
    if (!wujud || wujud.boundingBox !== 'block') {
      emit({ ev: 'task_failed', task: 'bangun', reason: 'blok_tak_padat', detail: nama })
      return
    }
    const tugasLama = currentTask
    currentTask = 'bangun'
    roamTarget = null
    bot.pathfinder.setGoal(null)
    try {
      await tungguDiam()
      // CATATAN JUJUR: cangkangnya sering menyisakan 2-3 lubang, hampir
      // selalu di sisi tempat dia mencondong — Minecraft menolak menaruh blok
      // yang menembus badan pemain, dan kotak tubuhnya (lebar 0,6) menjulur
      // ke kolom sebelah kalau dia berhenti tidak di tengah blok.
      // Memusatkannya dulu lewat GoalBlock SUDAH DICOBA dan dibatalkan:
      // hasilnya tidak konsisten (7 lubang, lalu 0), satu putaran malah lebih
      // buruk daripada tanpa itu. Yang penting jumlah lubangnya DILAPORKAN
      // apa adanya, dan itu sudah dibuktikan cocok dengan dunia.
      const kaki = bot.entity.position.floored()
      // Kotak 3x3x3 dengan ruang dalam 1x1x2. URUTANNYA penting: cincin
      // atap dipasang SEBELUM penutup tengahnya.
      //
      // Terukur [date removed]: versi pertama cuma bertembok dua tingkat lalu
      // menaruh atap di dy=2 — dan atap itu TIDAK PERNAH bisa dipasang,
      // karena keenam tetangganya udara semua, tidak ada permukaan untuk
      // menempelkannya. Cincin dy=2 yang memberinya pijakan.
      const titik = []
      for (const dy of [0, 1, 2]) {
        for (const dx of [-1, 0, 1]) {
          for (const dz of [-1, 0, 1]) {
            if (dx === 0 && dz === 0) continue      // ruang untuk dia berdiri
            titik.push(kaki.offset(dx, dy, dz))
          }
        }
      }
      titik.push(kaki.offset(0, 2, 0))              // penutup atap, paling akhir
      const punya = jumlahDiTas(nama)
      emit({ ev: 'build_start', block: nama, need: titik.length, have: punya })
      if (punya < 1) {
        emit({ ev: 'task_failed', task: 'bangun', reason: 'tidak_punya_blok', detail: nama })
        return
      }
      let dipasang = 0
      let dilewati = 0
      const ulang = []
      for (const t of titik) {
        if (!jumlahDiTas(nama)) break
        const gagal = await naruhSatu(t, nama, jenis)
        if (!gagal) dipasang++
        else if (gagal === 'tempatnya_terisi') dilewati++   // sudah ada tembok/tanah
        else ulang.push(t)
        await new Promise((r) => setTimeout(r, 120))
      }
      // Sekali percobaan ulang. Sebagian kegagalan cuma soal urutan: blok
      // tetangganya belum ada waktu giliran pertama lewat.
      for (const t of ulang) {
        if (!jumlahDiTas(nama)) break
        const gagal = await naruhSatu(t, nama, jenis)
        if (!gagal) dipasang++
        else if (gagal === 'tempatnya_terisi') dilewati++
        await new Promise((r) => setTimeout(r, 120))
      }
      // Lubang dihitung dari DUNIA, bukan dari pembukuan per langkah.
      // Terukur [date removed]: pembukuan bilang 0 bolong padahal satu sisi masih
      // menganga — satu posisi tercatat "sudah terisi" padahal udara (blok
      // tidak bisa ditaruh menembus badannya sendiri kalau dia berdiri terlalu
      // merapat ke sisi itu). Pelajaran yang sama dengan menghitung hasil
      // tambang dari selisih isi tas: percayai dunia, bukan niat.
      let bolong = 0
      for (const t of titik) {
        const b2 = bot.blockAt(t)
        if (!b2 || b2.boundingBox !== 'block') bolong++
      }
      emit({ ev: 'build_done', block: nama, placed: dipasang,
             reused: dilewati, missing: bolong })
      // Tembok berdiri dan musuh masih dekat: masuk fase bertahan — kalau
      // langsung roam, dia menggali keluar dari perlindungannya sendiri.
      if (bolong === 0 && hostileTerdekat()) {
        bertahanSampai = Date.now() + BERTAHAN_PERPANJANG_MS
        emit({ ev: 'bertahan_start' })
      }
    } catch (e) {
      emit({ ev: 'task_failed', task: 'bangun', reason: 'gagal_bangun',
             detail: String((e && e.message) || e).slice(0, 80) })
    } finally {
      if (currentTask === 'bangun') {
        currentTask = tugasLama === 'roam' ? 'roam' : 'idle'
        setFollow()
      }
    }
  },
  // NYERANG. Di log 6-7 Agustus dia mati 4x dalam 10 menit ke zombie tanpa
  // sekali pun melawan — yang dia punya cuma `kabur`.
  //
  // Ditulis sendiri, TIDAK memakai mineflayer-pvp: paket itu rilis terakhir
  // 2022, menyeret mineflayer-utils dari 2020 yang masih minta mineflayer v2,
  // tabel kecepatan serangnya tidak kenal item [time removed], dan yang paling
  // mengganggu — dia merebut goal pathfinder, yang akan bertabrakan dengan
  // logika kabur dan mesin `currentTask` di sini.
  async serang (c) {
    const mau = String((c && c.target) || '').trim().toLowerCase()
    let sasaran = null
    if (mau) {
      sasaran = bot.nearestEntity((e) => e && e.position && e !== bot.entity &&
        String(e.name || '').toLowerCase() === mau &&
        bot.entity.position.distanceTo(e.position) <= 24) || null
    } else {
      sasaran = hostileTerdekat()
    }
    if (!sasaran) {
      emit({ ev: 'task_failed', task: 'serang', reason: 'tidak_ada_sasaran', detail: mau || 'musuh' })
      return
    }
    // Creeper TIDAK dilawan jarak dekat: memukulnya berarti meledak di
    // mukanya. Menghindar itu jawaban yang benar, bukan keberanian.
    if (String(sasaran.name || '').toLowerCase() === 'creeper') {
      emit({ ev: 'task_failed', task: 'serang', reason: 'jangan_creeper' })
      return
    }

    const jenisMusuh = String(sasaran.name || 'musuh')
    // Hewan pasif tidak bisa melukainya, jadi ambang kabur TIDAK berlaku.
    // TERUKUR [date removed]: dengan darah di bawah FLEE_HP dia mengumumkan mulai
    // bertarung lalu LANGSUNG menyatakan kalah dan "kabur dari Cow". Itu jebakan
    // bertahan hidup — saat lemah DAN lapar, berburu justru yang paling dia
    // butuhkan, tapi aturan kabur memblokirnya sehingga dia tidak akan pernah
    // bisa makan.
    const pasif = HEWAN_MAKANAN.has(jenisMusuh.toLowerCase())
    const id = sasaran.id
    const senjata = senjataTerbaik()
    if (senjata) { try { await bot.equip(senjata, 'hand') } catch (e) {} }
    // Shield ke tangan kiri kalau ada dan belum terpasang. operator: "aman itu
    // minimal punya shield dan bisa pake". SENGAJA tidak diangkat
    // (`activateItem`) selama bertarung: mengangkat shield di [time removed] menghalangi
    // ayunan, jadi mengangkatnya justru membuatnya berhenti menyerang.
    if (!armorDipakai().includes('shield')) {
      const sh = bot.inventory.items().find((it) => it.name === 'shield')
      if (sh) { try { await bot.equip(sh, 'off-hand') } catch (e) {} }
    }

    // Kejujuran laporan: `entityDead` saja tidak cukup — mob bisa mati kena
    // jatuh, lava, atau dibunuh orang lain di tengah kita bertarung. Paket
    // `damage_event` ([time removed]+) membawa SUMBER kerusakan, jadi kita catat kapan
    // terakhir kali KITA yang memukulnya.
    let kenaKita = 0
    let mati = false
    const onHurt = (e, sumber) => {
      if (e && e.id === id && sumber && bot.entity && sumber.id === bot.entity.id) {
        kenaKita = Date.now()
      }
    }
    const onDead = (e) => { if (e && e.id === id) mati = true }
    bot.on('entityHurt', onHurt)
    bot.on('entityDead', onDead)

    const tugasLama = currentTask
    currentTask = 'serang'
    roamTarget = null
    roamManual = false
    emit({ ev: 'fight_start', kind: jenisMusuh,
           weapon: senjata ? senjata.name : 'tangan kosong' })
    let hasil = 'lepas'
    try {
      const batas = Date.now() + SERANG_BATAS_MS
      let pukulBerikutnya = 0
      while (Date.now() < batas) {
        if (mati) break
        const t = bot.entities[id]
        if (!t || t.isValid === false || !t.position) break
        if (!pasif && FLEE_HP > 0 && bot.health > 0 && bot.health <= FLEE_HP) {
          hasil = 'kabur'
          break
        }
        const d = bot.entity.position.distanceTo(t.position)
        if (d > 24) break
        if (d > SERANG_JANGKAUAN) {
          bot.pathfinder.setGoal(new goals.GoalFollow(t, 2), true)
        } else {
          bot.pathfinder.setGoal(null)
          if (Date.now() >= pukulBerikutnya) {
            try {
              await bot.lookAt(t.position.offset(0, (t.height || 1.8) * 0.5, 0))
              bot.attack(t)
            } catch (e) { /* sasaran hilang di tengah ayunan */ }
            pukulBerikutnya = Date.now() + jedaSerang(bot.heldItem)
          }
        }
        await new Promise((r) => setTimeout(r, 100))
      }
      if (mati) {
        // Diklaim sebagai bunuhannya HANYA kalau pukulan kita yang terakhir
        // mendarat beberapa detik terakhir.
        hasil = (Date.now() - kenaKita) < 5000 ? 'tumbang' : 'mati_bukan_olehmu'
      }
      if (hasil === 'tumbang') {
        statistik.bunuh += 1
      emit({ ev: 'killed', kind: jenisMusuh })
      } else if (hasil === 'kabur') {
        emit({ ev: 'fight_lost', kind: jenisMusuh })
        mulaiKabur(bot.entities[id] || sasaran, 'kalah')
      } else {
        emit({ ev: 'fight_end', kind: jenisMusuh, reason: hasil })
      }
    } catch (e) {
      emit({ ev: 'task_failed', task: 'serang', reason: 'gagal_serang',
             detail: String((e && e.message) || e).slice(0, 80) })
    } finally {
      bot.removeListener('entityHurt', onHurt)
      bot.removeListener('entityDead', onDead)
      if (currentTask === 'serang' && hasil !== 'kabur') {
        bot.pathfinder.setGoal(null)
        currentTask = tugasLama === 'roam' ? 'roam' : 'idle'
        setFollow()
      }
    }
  },
  // Kasih barang ke pemain lain. Minecraft tidak punya "serah terima": yang
  // ada cuma MELEMPAR ke tanah lalu orangnya memungut. Jadi dia mendekat,
  // menghadap orangnya, baru melempar — supaya barangnya jatuh di kaki yang
  // dituju, bukan di tempat dia berdiri tadi.
  async give (c) {
    const nama = String((c && c.item) || '').trim()
    const pemain = String((c && c.player) || '').trim()
    const minta = Math.max(1, Math.min(64, parseInt((c && c.count) || 1, 10) || 1))
    const jenis = bot.registry.itemsByName[nama]
    if (!jenis) {
      emit({ ev: 'task_failed', task: 'give', reason: 'item_tak_dikenal', detail: nama })
      return
    }
    const punya = jumlahDiTas(nama)
    if (!punya) {
      emit({ ev: 'task_failed', task: 'give', reason: 'tidak_punya_barang', detail: nama })
      return
    }
    const target = player(pemain)
    if (!target) {
      emit({ ev: 'task_failed', task: 'give', reason: 'orang_tak_kelihatan', detail: pemain })
      return
    }
    const tugasLama = currentTask
    currentTask = 'kasih'
    roamTarget = null
    try {
      if (bot.entity.position.distanceTo(target.position) > 3) {
        emit({ ev: 'give_walk', item: nama, player: pemain,
               distance: Math.round(bot.entity.position.distanceTo(target.position)) })
        await bot.pathfinder.goto(new goals.GoalNear(
          target.position.x, target.position.y, target.position.z, 2))
      }
      bot.pathfinder.setGoal(null)
      await tungguDiam()
      const lagi = player(pemain)
      if (!lagi) {
        emit({ ev: 'task_failed', task: 'give', reason: 'orang_tak_kelihatan', detail: pemain })
        return
      }
      // Menghadap MATA-nya, bukan kakinya — dari kamera itu terlihat seperti
      // menyerahkan, bukan melempar ke tanah.
      await bot.lookAt(lagi.position.offset(0, 1.6, 0))
      const sebelum = jumlahDiTas(nama)
      await bot.toss(jenis.id, null, Math.min(minta, sebelum))
      // Jumlah dari SELISIH ISI TAS, bukan dari yang diminta — pelajaran yang
      // sama dengan hasil tambang: yang dilaporkan harus yang benar terjadi.
      const lepas = Math.max(0, sebelum - jumlahDiTas(nama))
      if (!lepas) {
        emit({ ev: 'task_failed', task: 'give', reason: 'gagal_kasih', detail: nama })
        return
      }
      emit({ ev: 'gave', item: nama, player: pemain, count: lepas })
    } catch (e) {
      const pesan = String((e && e.message) || e)
      emit({ ev: 'task_failed', task: 'give',
             reason: /goal was changed|interrupt/i.test(pesan) ? 'dibatalkan'
               : /path|goal|reach/i.test(pesan) ? 'tak_terjangkau' : 'gagal_kasih',
             detail: pesan.slice(0, 80) })
    } finally {
      if (currentTask === 'kasih') {
        currentTask = tugasLama === 'roam' ? 'roam' : 'idle'
        setFollow()
      }
    }
  },
  // Naruh blok (Phase 2). SATU blok di depan kakinya — bukan membangun.
  // Membangun rumah itu urutan panjang dan tetap "belum bisa"; ini pondasinya,
  // dan sudah cukup untuk hal yang benar-benar sering dia butuhkan: menaruh
  // meja craft, peti, obor.
  async place (c) {
    // (rumah ditandai di bawah, sesudah penaruhan terverifikasi)
    const nama = String((c && c.block) || '').trim()
    const jenis = bot.registry.itemsByName[nama]
    if (!jenis) {
      emit({ ev: 'task_failed', task: 'place', reason: 'blok_tak_dikenal', detail: nama })
      return
    }
    if (!jumlahDiTas(nama)) {
      emit({ ev: 'task_failed', task: 'place', reason: 'tidak_punya_blok', detail: nama })
      return
    }
    const tugasLama = currentTask
    currentTask = 'naruh'
    roamTarget = null
    bot.pathfinder.setGoal(null)
    // BERHENTI DULU, baru hitung sasarannya. Terukur [date removed]: sasaran
    // dihitung sambil dia masih melangkah, dia bergeser 3,9 blok sebelum
    // paketnya sampai, dan hasilnya dia melapor BERHASIL untuk blok yang tidak
    // pernah ada. Menunggu diam menghapus seluruh kelas kesalahan itu.
    await tungguDiam()
    const target = bot.entity.position.floored().plus(arahDepan())
    try {
      // Jalur yang SAMA dengan `bangun` — termasuk pencarian enam sisi dan
      // pemeriksaan ulang di dunia sebelum dianggap berhasil.
      const gagal = await naruhSatu(target, nama, jenis)
      if (gagal) {
        emit({ ev: 'task_failed', task: 'place', reason: gagal, detail: nama })
        return
      }
      tandaiRumah(nama, target)
      emit({ ev: 'placed', block: nama,
             at: { x: target.x, y: target.y, z: target.z } })
    } finally {
      if (currentTask === 'naruh') {
        currentTask = tugasLama === 'roam' ? 'roam' : 'idle'
        setFollow()
      }
    }
  },
  kabur() {
    const musuh = hostileTerdekat()
    if (!musuh) {
      emit({ ev: 'task_failed', task: 'kabur', reason: 'tidak_ada_ancaman' })
      return
    }
    roamManual = false
    mulaiKabur(musuh, 'disuruh')
  },
  // Makan (Phase 2, potongan pertama). Di log 6-7 Agustus Arti mengeluh lapar
  // berkali-kali sambil MEMEGANG roti dari operator tanpa bisa memakannya.
  // TURUN (permintaan operator [date removed]). Dia tidak pernah kepikiran turun ke
  // cave, dan cara yang dia mau BUKAN melompat ke lubang: "kalo susah, yaaa
  // dia bisa bikin pickaxe buat gali dinding". Jadi ini menggali lurus ke
  // bawah, satu blok sekali, dengan dua pagar yang membuatnya tidak bunuh
  // diri: menolak kalau di bawahnya cairan (lava/air), dan menolak kalau
  // di bawahnya JURANG — persis kasus "lubang besar cave" yang bikin dia
  // jatuh dan kehilangan darah.
  async turun (c) {
    // TANGGA, bukan sumur lurus ke bawah (operator [date removed]: "dig 10 blok ke
    // bawah jadi ... steps/tangga gitu, ga straight down — compensate karena
    // mikir lama"). Tiga alasan: satu keputusan LLM membeli pekerjaan jauh
    // lebih panjang (32 anak tangga vs 5 blok), turunannya BISA DIDAKI BALIK
    // (sumur = perangkap satu arah), dan kepalanya tidak pernah menggantung
    // di atas lubang yang baru dia gali.
    const minta = Math.max(1, Math.min(32, parseInt((c && c.count) || 8, 10) || 8))
    const yAwal = Math.round(bot.entity.position.y)
    const tugasLama = currentTask
    currentTask = 'turun'
    roamTarget = null
    emit({ ev: 'turun_start', target: minta })
    // Arah dikunci SEKALI dari hadapannya (pola jembatan) — tangga yang
    // berbelok tiap langkah bukan tangga, itu spiral kebingungan.
    const yaw = bot.entity.yaw
    const adx = Math.round(-Math.sin(yaw))
    const adz = Math.round(Math.cos(yaw))
    const maju = (Math.abs(adx) >= Math.abs(adz))
      ? [adx >= 0 ? 1 : -1, 0] : [0, adz >= 0 ? 1 : -1]
    let alasan = ''
    try {
      for (let i = 0; i < minta; i++) {
        const kaki = bot.entity.position.floored()
        const F = kaki.offset(maju[0], 0, maju[1])
        // TIGA blok per anak tangga: atas (y+1), badan (y), injak (y-1).
        // Tanpa `atas`, kepala menabrak langit-langit kolom depan saat
        // melangkah turun — terukur presisi di server: macet di x=33.7,
        // persis 0.3 (setengah lebar badan) dari bibir kolom. Tangga 2-blok
        // bisa DIDAKI tapi tidak bisa DITURUNI begitu masuk bawah tanah.
        const atas = bot.blockAt(F.offset(0, 1, 0))
        const badan = bot.blockAt(F)                   // badan barunya (y)
        const injak = bot.blockAt(F.offset(0, -1, 0))  // kaki barunya (y-1)
        const dasar = bot.blockAt(F.offset(0, -2, 0))  // pijakan barunya
        if (!badan || !injak) { alasan = 'tidak_bisa_digali'; break }
        if ((atas && atas.name === 'bedrock') || badan.name === 'bedrock' ||
            injak.name === 'bedrock') {
          alasan = 'tidak_bisa_digali'
          break
        }
        if ((atas && CAIRAN.has(atas.name)) || CAIRAN.has(badan.name) ||
            CAIRAN.has(injak.name) || (dasar && CAIRAN.has(dasar.name))) {
          alasan = 'ada_cairan_di_bawah'
          break
        }
        // Jurang di kolom depan: pijakan harus ada dalam 4 blok.
        let dalam = 0
        for (let d = 2; d <= 5; d++) {
          const b = bot.blockAt(F.offset(0, -d, 0))
          if (!b || b.boundingBox === 'empty') dalam++
          else break
        }
        if (dalam >= 4) { alasan = 'jurang_di_bawah'; break }
        // Pakai ALAT, bukan tangan (keluhan operator: "kalo dia ada tool, pake").
        // Gali -> tunggu -> CEK BERSIH -> ulangi sekali: gravel/pasir dari
        // atas bisa runtuh mengisi ulang celah yang baru digali (tes server:
        // langkah macet di anak tangga yang sama berulang kali).
        let bersih = false
        const selTangga = [F.offset(0, 1, 0), F, F.offset(0, -1, 0)]
        for (let ronde = 0; ronde < 3 && !bersih && !alasan; ronde++) {
          for (const pos of selTangga) {
            const b = bot.blockAt(pos)
            if (!b || b.boundingBox !== 'block') continue
            try {
              if (bot.tool) await bot.tool.equipForBlock(b, { requireHarvest: false })
            } catch (e) { /* tanpa alat cocok: tangan */ }
            try {
              await bot.dig(b)
            } catch (e) {
              // Blok keburu berubah (runtuhan gravel, retak ganda) — bukan
              // vonis: ronde verifikasi di bawah yang menilai. Gagal beneran
              // = tiga ronde tetap kotor.
              break
            }
          }
          await new Promise((r) => setTimeout(r, 300))   // beri waktu reruntuhan
          bersih = selTangga.every((pos) => {
            const b = bot.blockAt(pos)
            return !b || b.boundingBox !== 'block'
          })
        }
        if (!alasan && !bersih) alasan = 'gagal_menggali'
        if (alasan) break
        // Melangkah turun satu anak tangga — KENDALI LANGSUNG (pola renang),
        // bukan pathfinder: terukur di server, pathfinder kadang menolak
        // melangkah ke celah 2-blok di bawah overhang (tes pertama berhenti
        // di anak tangga ke-3 dengan gagal_melangkah). Satu blok maju-turun
        // tidak butuh perencana jalur. Verifikasi tetap POSISI, bukan niat.
        try {
          await bot.lookAt(F.offset(0.5, 0.2, 0.5), true)
        } catch (e) { /* menoleh gagal bukan alasan berhenti */ }
        bot.setControlState('forward', true)
        const batas = Date.now() + 4000
        let sampai = false
        while (Date.now() < batas) {
          const p = bot.entity.position
          if (Math.round(p.y) <= kaki.y - 1 &&
              Math.abs(p.x - (F.x + 0.5)) < 0.9 &&
              Math.abs(p.z - (F.z + 0.5)) < 0.9) { sampai = true; break }
          await new Promise((r) => setTimeout(r, 100))
        }
        bot.setControlState('forward', false)
        if (!sampai) {
          const cekB = bot.blockAt(F)
          const cekI = bot.blockAt(F.offset(0, -1, 0))
          log(`turun macet di anak tangga ${i + 1}: badan=${cekB && cekB.name} ` +
              `injak=${cekI && cekI.name} pos=${bot.entity.position}`)
          alasan = 'gagal_melangkah'
          break
        }
      }
    } finally {
      // Kedalaman DIBACA DARI DUNIA, bukan dari hitungan putaran — kalau dia
      // tersangkut atau berhenti di tengah, angkanya tetap jujur.
      const turun = Math.max(0, yAwal - Math.round(bot.entity.position.y))
      if (alasan) emit({ ev: 'task_failed', task: 'turun', reason: alasan, detail: String(turun) })
      else emit({ ev: 'turun_done', turun, y: Math.round(bot.entity.position.y) })
      if (currentTask === 'turun') {
        currentTask = tugasLama === 'roam' ? 'roam' : 'idle'
        setFollow()
      }
    }
  },
  // MASAK (permintaan operator [date removed]: "trus masak makan"). Daging mentah
  // sudah bikin dia tidak kelaparan, tapi yang matang jauh lebih mengenyangkan
  // — itu bedanya "ada makanan" dan "makanan stabil".
  async masak (c) {
    const mentah = bot.inventory.items().filter((it) => MASAKAN[it.name])
    if (!mentah.length) {
      emit({ ev: 'task_failed', task: 'masak', reason: 'tidak_ada_bahan' })
      return
    }
    const bakar = bot.inventory.items().find((it) => BAHAN_BAKAR.includes(it.name))
    if (!bakar) {
      emit({ ev: 'task_failed', task: 'masak', reason: 'tidak_ada_bahan_bakar' })
      return
    }
    const jenisF = bot.registry.blocksByName.furnace
    let meja = jenisF ? bot.findBlock({ matching: jenisF.id, maxDistance: 16 }) : null
    const tugasLama = currentTask
    currentTask = 'masak'
    roamTarget = null
    const bahan = mentah[0]
    const hasil = MASAKAN[bahan.name]
    const sebelum = jumlahDiTas(hasil)
    emit({ ev: 'cook_start', item: bahan.name, into: hasil })
    let alasan = ''
    let f = null
    try {
      if (!meja) {
        // Belum ada furnace di sekitar: taruh punyanya sendiri kalau ada.
        if (jumlahDiTas('furnace') <= 0) { alasan = 'tidak_ada_furnace'; return }
        await tungguDiam()
        const depan = titikKosong(false)
        if (!depan) { alasan = 'tidak_ada_tempat'; return }
        // Parameter ke-3 itu OBJEK JENIS ITEM, bukan label tugas: naruhSatu
        // memanggil bot.equip(jenis.id). Mengirim string ke situ membuat
        // equip melempar dan hasilnya 'gagal_naruh' yang menyesatkan.
        const jenisFurnace = bot.registry.itemsByName.furnace
        const gagal = await naruhSatu(depan, 'furnace', jenisFurnace)
        if (gagal) { alasan = gagal; return }
        meja = bot.blockAt(depan)
        if (!meja || meja.name !== 'furnace') { alasan = 'tidak_ada_furnace'; return }
      }
      await bot.pathfinder.goto(new goals.GoalNear(meja.position.x, meja.position.y, meja.position.z, 2))
      f = await bot.openFurnace(meja)
      await f.putFuel(bakar.type, null, 1)
      await f.putInput(bahan.type, null, Math.min(bahan.count, 3))
      // Satu potong butuh 10 dtk. Ditunggu sampai ADA output, bukan sampai
      // semuanya matang — supaya dia tidak berdiri diam kelewat lama.
      const batas = Date.now() + 26000
      while (Date.now() < batas && !f.outputItem()) {
        await new Promise((r) => setTimeout(r, 500))
      }
      if (!f.outputItem()) { alasan = 'belum_matang'; return }
      await f.takeOutput()
    } catch (e) {
      alasan = alasan || 'gagal_masak'
    } finally {
      try { if (f) f.close() } catch (e) {}
      // Dibaca dari isi tas, bukan dari yang dimasukkan.
      const dapat = Math.max(0, jumlahDiTas(hasil) - sebelum)
      if (alasan) emit({ ev: 'task_failed', task: 'masak', reason: alasan })
      else emit({ ev: 'cook_done', item: hasil, count: dapat })
      if (currentTask === 'masak') {
        currentTask = tugasLama === 'roam' ? 'roam' : 'idle'
        setFollow()
      }
    }
  },
  // TIDUR (permintaan operator [date removed]: malam "di-skip" pakai bed). Ini beda
  // dari `bangun`: menembok diri cuma MENUNGGU malam lewat, tidur MELEWATKANNYA.
  async tidur (c) {
    const malam = bot.time && bot.time.timeOfDay > 12541 && bot.time.timeOfDay < 23458
    if (!malam && !(bot.isRaining && bot.thunderState > 0)) {
      emit({ ev: 'task_failed', task: 'tidur', reason: 'belum_malam' })
      return
    }
    const tugasLama = currentTask
    currentTask = 'tidur'
    roamTarget = null
    let alasan = ''
    try {
      let bed = bot.findBlock({ maxDistance: 8, matching: (b) => b && bot.isABed(b) })
      if (!bed) {
        const punya = bot.inventory.items().find((it) => it.name.endsWith('_bed'))
        if (!punya) { alasan = 'tidak_punya_bed'; return }
        await tungguDiam()
        const depan = titikKosong(true)
        if (!depan) { alasan = 'tidak_ada_tempat'; return }
        const gagal = await naruhSatu(depan, punya.name,
                                      bot.registry.itemsByName[punya.name])
        if (gagal) { alasan = gagal; return }
        bed = bot.blockAt(depan)
        if (!bed || !bot.isABed(bed)) { alasan = 'tidak_ada_tempat'; return }
      }
      await bot.pathfinder.goto(new goals.GoalNear(bed.position.x, bed.position.y, bed.position.z, 2))
      await bot.sleep(bed)
      emit({ ev: 'tidur_start' })
      // Tidur sampai pagi. Server membangunkannya sendiri; batas ini cuma
      // jaring supaya dia tidak menggantung kalau paketnya hilang.
      const batas = Date.now() + 20000
      while (Date.now() < batas && bot.isSleeping) {
        await new Promise((r) => setTimeout(r, 300))
      }
      try { if (bot.isSleeping) await bot.wake() } catch (e) {}
      // Jam dunia baru ikut BERUBAH sesudah server mengirim paket waktu, jadi
      // membacanya langsung memberi nilai LAMA. TERUKUR [date removed]: malamnya
      // benar-benar lewat (14120 -> 69 menurut RCON) tapi event-nya melapor
      // `pagi: false` -- pola yang sama seperti `bot.food` sesudah makan.
      const tungguPagi = Date.now() + 4000
      while (Date.now() < tungguPagi) {
        const t = bot.time ? bot.time.timeOfDay : 0
        if (t < 12541 || t > 23458) break
        await new Promise((r) => setTimeout(r, 250))
      }
    } catch (e) {
      alasan = alasan || String((e && e.message) || e).slice(0, 60)
    } finally {
      // Berhasil = malamnya benar-benar lewat, dibaca dari jam dunia.
      const pagi = bot.time ? (bot.time.timeOfDay < 12541 || bot.time.timeOfDay > 23458) : false
      if (alasan) emit({ ev: 'task_failed', task: 'tidur', reason: alasan })
      else emit({ ev: 'tidur_done', pagi })
      if (currentTask === 'tidur') {
        currentTask = tugasLama === 'roam' ? 'roam' : 'idle'
        setFollow()
      }
    }
  },
  // TEMBOK PANAH ([date removed]). Dua blok bertumpuk di arah musuh terdekat --
  // bukan shelter penuh. Musuh jarak dekat tetap urusan serang/kabur/bangun;
  // ini khusus memutus GARIS TEMBAK penembak.
  async mundur_tembok (c) {
    let sasaran = null
    let jarak = 999
    for (const e of Object.values(bot.entities)) {
      if (!e || !e.position || e === bot.entity) continue
      if (e.type !== 'hostile' && !(e.kind && String(e.kind).includes('Hostile'))) continue
      const d = e.position.distanceTo(bot.entity.position)
      if (d < jarak) { jarak = d; sasaran = e }
    }
    if (!sasaran) {
      emit({ ev: 'task_failed', task: 'tembok', reason: 'tidak_ada_ancaman' })
      return
    }
    const { nama, n } = blokTerbanyakDiTas()
    if (!nama || n < 2) {
      emit({ ev: 'task_failed', task: 'tembok', reason: 'tidak_punya_blok' })
      return
    }
    const tugasLama = currentTask
    currentTask = 'tembok'
    roamTarget = null
    bot.pathfinder.setGoal(null)     // berdiri diam; menembok sambil jalan gagal
    let dipasang = 0
    let alasan = ''
    let sudahTertutup = 0
    try {
      const kaki = bot.entity.position.floored()
      const arah = sasaran.position.minus(bot.entity.position)
      // Sel tetangga SEARAH musuh, dipilih dari komponen dominan supaya
      // temboknya benar-benar di antara dia dan penembaknya.
      const dx = Math.abs(arah.x) >= Math.abs(arah.z) ? Math.sign(arah.x) : 0
      const dz = dx === 0 ? Math.sign(arah.z) : 0
      const jenis = bot.registry.itemsByName[nama]
      for (const dy of [0, 1]) {
        const t = kaki.offset(dx, dy, dz)
        // Sel yang SUDAH padat = garis tembak dari arah itu sudah terputus.
        // TERUKUR di rendaman kelima: refleks menyala saat dia berada DI DALAM
        // shelter penuh miliknya sendiri, kedua sel sudah terisi, dan dia
        // melaporkan 'gagal_naruh' 1 ms kemudian -- padahal kondisinya justru
        // sudah benar. Itu bukan kegagalan.
        const isi = bot.blockAt(t)
        if (isi && isi.boundingBox === 'block') { sudahTertutup++; continue }
        const gagal = await naruhSatu(t, nama, jenis)
        if (!gagal) dipasang++
        else alasan = alasan || gagal
      }
    } finally {
      // Dihitung dari yang benar-benar terpasang, bukan dari niat.
      if (dipasang > 0) emit({ ev: 'tembok_done', count: dipasang, kind: String(sasaran.name || 'musuh') })
      else if (sudahTertutup >= 2) emit({ ev: 'task_failed', task: 'tembok', reason: 'sudah_tertutup' })
      else emit({ ev: 'task_failed', task: 'tembok', reason: alasan || 'gagal_naruh' })
      if (currentTask === 'tembok') {
        currentTask = tugasLama === 'roam' ? 'roam' : 'idle'
        setFollow()
      }
    }
  },
  // COR PORTAL dari LAVA POOL — nether TANPA diamond (ide operator [date removed]:
  // "kalo nemu lava pool, dia bisa scan area dan ngerti cara bikinnya").
  // Teknik dinding punggung: dinding scaffold 4x6 dibangun DI BELAKANG bidang
  // bingkai, sehingga TIAP sel bingkai punya muka klik yang seragam:
  //   lava  -> klik muka depan dinding setinggi sel  -> sumber lava di sel
  //   air   -> klik muka depan dinding SATU di atas  -> mengalir turun ke
  //            lava -> OBSIDIAN -> airnya diciduk balik
  // Baris atas di luar jangkauan tangan -> `menara` 2 blok, lalu turun lagi
  // untuk menciduk lava berikutnya. Lambat tidak apa-apa: kerja bot gratis,
  // yang mahal itu giliran LLM (prinsip "compensate mikir lama").
  async portal_cor (c) {
    const airEmber = jumlahDiTas('water_bucket')
    const emberKosong = jumlahDiTas('bucket')
    if (airEmber < 1 || emberKosong < 1) {
      emit({ ev: 'task_failed', task: 'portal', reason: 'butuh_dua_ember',
             detail: `water=${airEmber} kosong=${emberKosong}` })
      return
    }
    // Saring SUMBER langsung di matcher — pakai id lalu filter belakangan
    // terbukti salah hitung: 40 slot pemindaian habis oleh lava MENGALIR di
    // tepi kolam, sumber asli cuma kehitung 7/16 (uji #7, 'lava_kurang').
    const kolam = bot.findBlocks({
      matching: (b2) => b2 && b2.name === 'lava' &&
        (b2.getProperties().level === 0 || b2.metadata === 0),
      maxDistance: 12,
      count: 40,
    })
    if (kolam.length < 14) {
      emit({ ev: 'task_failed', task: 'portal', reason: 'lava_kurang',
             detail: String(kolam.length) })
      return
    }
    const { nama: namaScaffold, n: nScaffold } = blokTerbanyakDiTas()
    if (!namaScaffold || nScaffold < 30) {
      emit({ ev: 'task_failed', task: 'portal', reason: 'blok_kurang',
             detail: `${namaScaffold || '-'} ${nScaffold}/30` })
      return
    }
    const tugasLama = currentTask
    currentTask = 'portal'
    roamTarget = null
    bot.pathfinder.setGoal(null)
    let dicor = 0
    let alasan = ''
    let matiSaatCor = false
    const tandaiMati = () => { matiSaatCor = true }
    bot.once('death', tandaiMati)
    try {
      await tungguDiam()
      const kaki = bot.entity.position.floored()
      const bx = kaki.x
      const by = kaki.y
      // Bidang bingkai 2 blok ke +Z, dinding punggung di +Z+1 — MENJAUHI
      // kolam lava kalau bisa (jangan membangun di atas lava).
      const bz = kaki.z + 2
      const wz = bz + 1
      const jenisScaffold = bot.registry.itemsByName[namaScaffold]
      emit({ ev: 'portal_start', at: { x: bx, y: by, z: bz }, cara: 'cor' })
      // 1. DINDING PUNGGUNG 4x6 (x: bx-1..bx+2, dy: 0..5).
      for (let dy = 0; dy <= 5; dy++) {
        for (let dx = -1; dx <= 2; dx++) {
          const t = new Vec3(bx + dx, by + dy, wz)
          const isi = bot.blockAt(t)
          if (isi && isi.boundingBox === 'block') continue
          const gagal = await naruhSatu(t, namaScaffold, jenisScaffold)
          if (gagal && dy <= 1) { alasan = 'gagal_dinding'; break }
        }
        if (alasan) break
      }
      if (alasan) return
      // 1b. TANGGUL di depan baris dasar (z = bz-1): sumber lava di sel
      // bingkai MENGALIR ~3 blok ke depan sebelum sempat jadi obsidian, dan
      // itu persis tempat dia berdiri — terukur: mati terbakar di uji #4
      // sesudah sel ke-2. Tanggul memutus jalur alirannya.
      for (let dx = -1; dx <= 2; dx++) {
        const t = new Vec3(bx + dx, by, bz - 1)
        const isi = bot.blockAt(t)
        if (isi && isi.boundingBox === 'block') continue
        await naruhSatu(t, namaScaffold, jenisScaffold)
      }
      // 2. COR 14 sel bingkai, kolom demi kolom dari bawah.
      const rencana = []
      for (let dx = -1; dx <= 2; dx++) {
        for (let dy = 0; dy <= 4; dy++) {
          const tepi = (dx === -1 || dx === 2)
          if (tepi || dy === 0 || dy === 4) rencana.push([dx, dy])
        }
      }
      const standDekat = new Vec3(bx, by, bz - 2)
      // Penyelamat air universal: sumber air nyasar bisa menempel di frame
      // pada ketinggian mana pun — cari MENTAH (tanpa syarat pijakan; syarat
      // pijakan mustahil untuk sumber melayang), naik tanggul di kolomnya,
      // ciduk dari sana (jarak ~1-2 dari atas tanggul ke semua ketinggian
      // frame yang terjangkau).
      const selamatkanAir = async () => {
        const src = bot.findBlock({
          matching: (b2) => b2 && b2.name === 'water' &&
            (b2.getProperties().level === 0 || b2.metadata === 0),
          maxDistance: 12,
        })
        if (!src) return false
        await dekati(new Vec3(src.position.x, by + 1, bz - 1), 0.9, 6000)
        if (src.position.y >= by + 4) {
          // Sumber di baris atas: jangkauan ciduk fluida (~2.5) tidak sampai
          // dari tanggul — naik pilar 2 dulu, HANYA di sini.
          try { await handlers.menara({ count: 2 }) } catch (e) { /* tetap coba */ }
          currentTask = 'portal'
        }
        if (!(await pegang('bucket'))) return false
        await klikEmber(src.position.offset(0.5, 0.5, 0.5))
        return jumlahDiTas('water_bucket') > 0
      }
      let selAktif = null
      for (const [dx, dy] of rencana) {
        if (matiSaatCor) { alasan = 'mati_saat_cor'; break }
        const sel = new Vec3(bx + dx, by + dy, bz)
        selAktif = sel
        // Panggung kerja kolom ini (atas tanggul) — dideklarasikan DI SINI,
        // bukan di dalam blok putaran: dipakai juga oleh siram loop di luar
        // blok itu (uji #21: ReferenceError standCor mematikan koreografi
        // senyap di sel 6).
        const standCor = new Vec3(sel.x, by + 1, bz - 1)
        const isi0 = bot.blockAt(sel)
        if (isi0 && isi0.name === 'obsidian') { dicor++; continue }
        if (isi0 && isi0.boundingBox === 'block') {
          try {
            if (bot.tool) await bot.tool.equipForBlock(isi0, { requireHarvest: false })
            await bot.dig(isi0)
          } catch (e) { /* biarkan cor yang gagal melapor */ }
        }
        // a. ke kolam: cari sumber TERKINI (kolam menyusut tiap ciduk — target
        // beku dari awal = menciduk bekas lubang sendiri), dan tunggu sampai
        // BENAR-BENAR tiba, bukan tidur 2,5 dtk lalu berharap (terukur:
        // 'tidak_ada_lava_dekat' karena dia belum sampai).
        // Siraman lava bisa meleset (ray tergelincir — uji #16: sel tetap
        // air, lava nyasar entah ke mana, lalu dia menginjaknya). Seluruh
        // siklus sel diulang maksimal 2 putaran, dan lava nyasar DIBERSIHKAN
        // sebelum putaran berikutnya.
        let adaLava = false
        for (let putaran = 0; putaran < 2 && !adaLava && !alasan; putaran++) {
        // Ember sudah berisi lava (sisa pembersihan nyasar / putaran gagal)?
        // Pakai itu — jangan maksa ke kolam dengan ember yang tidak kosong
        // (uji #17: 'tidak_punya_bucket' padahal lavanya sudah di tangan).
        let r1 = jumlahDiTas('lava_bucket') > 0 ? '' : 'belum'
        for (let coba = 0; coba < 3 && r1; coba++) {
          let tepi = sumberBertepi('lava', 24)
          if (!tepi) {
            // Tidak ada pijakan alami — aliran menutup seluruh tepian (uji
            // #11-12). CETAK pijakan: DEKATI dulu (placeBlock butuh
            // jangkauan — uji #13 menaruh dari 8 blok = gagal senyap), lalu
            // timpa sel aliran yang bersebelahan sumber; coba sampai 5
            // kandidat, jangan menyerah di kandidat pertama.
            const daftar = bot.findBlocks({
              matching: (b2) => b2 && b2.name === 'lava' &&
                (b2.getProperties().level === 0 || b2.metadata === 0),
              maxDistance: 24,
              count: 60,
            })
            const kandidatPijak = []
            for (const v of daftar) {
              for (const [ddx, ddz] of [[1, 0], [-1, 0], [0, 1], [0, -1]]) {
                const cel = v.offset(ddx, 0, ddz)
                const isi2 = bot.blockAt(cel)
                const bawah2 = bot.blockAt(cel.offset(0, -1, 0))
                if (isi2 && isi2.name === 'lava' &&
                    isi2.getProperties().level !== 0 &&
                    bawah2 && bawah2.boundingBox === 'block') {
                  kandidatPijak.push({ sumber: v, cel })
                }
              }
              if (kandidatPijak.length >= 5) break
            }
            for (const k of kandidatPijak) {
              await dekati(k.cel, 3.5, 7000)
              if (await cetakPijakan(k.cel, jenisScaffold)) {
                tepi = { sumber: k.sumber, pijakan: k.cel.offset(0, 1, 0) }
                log('portal_cor: pijakan DICETAK di tepi kolam')
                break
              }
            }
          }
          if (!tepi) { r1 = 'lava_kurang'; break }
          await dekati(tepi.pijakan, 1.4, 9000)
          log(`portal_cor: ciduk coba#${coba + 1} jarak=` +
              bot.entity.position.distanceTo(tepi.sumber).toFixed(1))
          r1 = await cidukCairan('lava')
        }
        if (r1) { alasan = r1; break }
        // b. NAIK KE TANGGUL di kolom sel — panggung kerja semua penuangan.
        // Menuang dari tanah terhalang tanggul sendiri (uji #18: ray baris
        // bawah cuma lolos [time removed] blok di atas tanggul — kadang terserempet,
        // lava nyasar). Dari atas tanggul, geometri bersih untuk semua baris.
        await dekati(standCor, 0.9, 9000)
        // c. (menara DIHAPUS dari alur tuang — uji #22: jatuh dari pilar di
        // sebelah lava segar setinggi kaki = mati. Jangkauan TUANG itu 4.5
        // blok (interaksi blok), cukup untuk semua baris dari atas tanggul;
        // yang pendek cuma jangkauan CIDUK fluida, dan itu urusan
        // selamatkanAir yang naik pilar hanya saat perlu.)
        // d. lava ke sel: klik muka depan dinding setinggi sel.
        // [time removed], bukan 0.5: dari tanggul, ray ke titik-tengah muka dinding
        // baris tinggi menyerempet bibir obsidian di bawahnya dengan margin
        // [time removed] blok (uji #23: lava_meleset dua putaran di dy3). Membidik
        // lebih tinggi di muka yang sama menambah clearance ~0.3 blok.
        const mukaLava = new Vec3(bx + dx + 0.5, by + dy + 0.75, wz)
        if (!(await pegang('lava_bucket'))) { alasan = 'lava_hilang'; break }
        await klikEmber(mukaLava)
        await new Promise((r) => setTimeout(r, 120))
        const cekLava = bot.blockAt(sel)
        adaLava = Boolean(cekLava &&
          (cekLava.name === 'lava' || cekLava.name === 'obsidian'))
        if (!adaLava) {
          log(`portal_cor: tuang lava putaran#${putaran + 1} meleset — sel ` +
              `${sel} kini ${cekLava && cekLava.name}; bersihkan nyasar`)
          // Ciduk balik lava nyasar terdekat dari BIDANG BINGKAI (bukan
          // kolam!) supaya tidak jadi ranjau injak.
          const nyasar = bot.findBlock({
            matching: (b2) => b2 && b2.name === 'lava' &&
              (b2.getProperties().level === 0 || b2.metadata === 0),
            maxDistance: 5,
          })
          if (nyasar && (await pegang('bucket'))) {
            await dekati(nyasar.position.offset(0, 0, -1), 1.6, 5000)
            await klikEmber(nyasar.position.offset(0.5, 0.85, 0.5))
          }
        }
        }  // tutup putaran sel
        if (alasan) break
        if (!adaLava) {
          alasan = 'lava_meleset'
          break
        }
        // e. air ke SATU di atas sel -> mengalir turun -> obsidian. Siraman
        // bisa meleset (ray menyerempet bibir dinding) — verifikasi dan
        // ulangi maksimal 2x, jangan langsung memvonis.
        const selAtas = sel.offset(0, 1, 0)
        const mukaAir = new Vec3(bx + dx + 0.5, by + dy + 1.75, wz)
        let jadi = false
        for (let siram = 0; siram < 2 && !jadi; siram++) {
          if (!(await pegang('water_bucket'))) {
            // Sisa insiden sel sebelumnya — selamatkan dari mana pun airnya
            // menempel (uji #20: sumber melayang di frame, pencari berbasis
            // pijakan mustahil menemukannya).
            await selamatkanAir()
            await dekati(standCor, 0.9, 6000)
            if (!(await pegang('water_bucket'))) { alasan = 'air_hilang'; break }
          }
          await klikEmber(mukaAir)
          // MUNDUR selama konversi ke TITIK yang dihitung (menjauh dari
          // bidang bingkai) — mundur BUTA pakai kontrol 'back' terbukti
          // menabrak kolam lava di belakangnya (mati di uji #5). Pathfinder
          // tidak mau menginjak lava.
          bot.pathfinder.setGoal(new goals.GoalNear(
            standDekat.x, standDekat.y, standDekat.z - 2, 1), false)
          await new Promise((r) => setTimeout(r, 900))
          bot.pathfinder.setGoal(null)
          const tenggat = Date.now() + 3000
          while (Date.now() < tenggat) {
            const b = bot.blockAt(sel)
            if (b && b.name === 'obsidian') { jadi = true; break }
            await new Promise((r) => setTimeout(r, 200))
          }
          // balik mendekat untuk menciduk air.
          bot.pathfinder.setGoal(new goals.GoalNear(
            standDekat.x, standDekat.y, standDekat.z, 1), false)
          await new Promise((r) => setTimeout(r, 1200))
          bot.pathfinder.setGoal(null)
          const airDi = bot.blockAt(selAtas)
          log(`portal_cor: siram#${siram + 1} sel=${(bot.blockAt(sel) || {}).name} ` +
              `atas=${airDi && airDi.name}`)
          // f. ciduk airnya balik (sumber di sel atas). Dari lantai, jarak
          // ke air sel-tinggi ~2.7 — PAS di luar jangkauan fluida (~2.5);
          // itulah kenapa water_bucket raib jelang sel-3 di TIGA run
          // beruntun. Naik ke TANGGUL dulu (persis di depan kolom): dari
          // atasnya jaraknya ~1.1.
          const atasTanggul = new Vec3(sel.x, by + 1, bz - 1)
          const tibaTanggul = await dekati(atasTanggul, 0.9, 5000)
          // Ciduk SUMBER air sesungguhnya, bukan asumsi selAtas: siraman bisa
          // mendarat di sel lain dan yang tampak di selAtas cuma ALIRAN —
          // ember kosong tidak bisa menciduk aliran (uji #17: wb=0 padahal
          // jarak [time removed] dan di tanggul).
          const srcAir = bot.findBlock({
            matching: (b2) => b2 && b2.name === 'water' &&
              (b2.getProperties().level === 0 || b2.metadata === 0),
            maxDistance: 6,
          })
          if (srcAir && (await pegang('bucket'))) {
            await klikEmber(srcAir.position.offset(0.5, 0.5, 0.5))
          }
          log(`portal_cor: ciduk-balik wb=${jumlahDiTas('water_bucket')} ` +
              `tanggul=${tibaTanggul} srcAir=` +
              (srcAir ? srcAir.position : 'null'))
          if (jumlahDiTas('water_bucket') < 1) {
            await selamatkanAir()
          }
        }
        if (alasan) break
        if (jadi) {
          dicor++
          emit({ ev: 'cor_maju', ke: dicor, dari: 14 })
        } else {
          alasan = 'gagal_cor'
          log(`portal_cor: sel ${sel} berakhir ${(bot.blockAt(sel) || {}).name}`)
          // JANGAN tinggalkan sumber lava telanjang di bingkai — dia sendiri
          // yang menginjaknya begitu roam jalan lagi (terbukti: mati 3 dtk
          // sesudah kegagalan pertama di uji). Ciduk balik best-effort.
          const sisa = bot.blockAt(sel)
          if (sisa && sisa.name === 'lava' && (await pegang('bucket'))) {
            await klikEmber(sel.offset(0.5, 0.5, 0.5))
          }
          break
        }
      }
      if (alasan) {
        // Kegagalan APA PUN tidak boleh meninggalkan sumber lava telanjang
        // di bingkai — dia menginjaknya begitu roam jalan (terbukti mati
        // berulang di uji). Ciduk balik best-effort.
        if (selAktif) {
          const sisa = bot.blockAt(selAktif)
          if (sisa && sisa.name === 'lava' && (await pegang('bucket'))) {
            await klikEmber(selAktif.offset(0.5, 0.5, 0.5))
          }
        }
        return
      }
      // 3. Interior wajib kosong, lalu nyalakan.
      for (let dx = 0; dx <= 1; dx++) {
        for (let dy = 1; dy <= 3; dy++) {
          const b = bot.blockAt(new Vec3(bx + dx, by + dy, bz))
          if (b && b.boundingBox === 'block') {
            try { await bot.dig(b) } catch (e) {}
          }
        }
      }
      alasan = await nyalakanPortal(bx, by, bz)
      if (alasan) return
      emit({ ev: 'portal_done', at: { x: bx, y: by + 1, z: bz }, cara: 'cor' })
    } finally {
      bot.removeListener('death', tandaiMati)
      if (alasan) {
        emit({ ev: 'task_failed', task: 'portal', reason: alasan,
               detail: `dicor ${dicor}/14` })
      }
      if (currentTask === 'portal') {
        currentTask = tugasLama === 'roam' ? 'roam' : 'idle'
        setFollow()
      }
    }
  },
  // PORTAL NETHER. Bingkai LENGKAP DENGAN SUDUT (14 obsidian, 4x5): sudut
  // memang tidak wajib untuk menyala, tapi tanpa sudut baris atas tidak punya
  // blok acuan untuk ditempel — naruhSatu butuh tetangga padat. Bingkai
  // dibangun di bidang X (kolom dalam bx..bx+1), dua blok di depannya.
  async portal (c) {
    if (jumlahDiTas('obsidian') < 14) {
      emit({ ev: 'task_failed', task: 'portal', reason: 'obsidian_kurang',
             detail: String(jumlahDiTas('obsidian')) })
      return
    }
    const tugasLama = currentTask
    currentTask = 'portal'
    roamTarget = null
    bot.pathfinder.setGoal(null)
    let dipasang = 0
    let alasan = ''
    try {
      await tungguDiam()
      const kaki = bot.entity.position.floored()
      // Bidang bingkai ditaruh 2 blok ke arah Z; kalau sisi itu terhalang,
      // coba sisi sebaliknya.
      let bz = kaki.z + 2
      const bx = kaki.x
      const by = kaki.y
      const dalamAman = (z) => {
        for (let dx = 0; dx <= 1; dx++) {
          for (let dy = 1; dy <= 3; dy++) {
            const b = bot.blockAt(new Vec3(bx + dx, by + dy, z))
            if (b && b.boundingBox === 'block') return false
          }
        }
        return true
      }
      if (!dalamAman(bz)) bz = kaki.z - 2
      if (!dalamAman(bz)) { alasan = 'tempat_portal_terhalang'; return }
      const jenis = bot.registry.itemsByName.obsidian
      // Urutan menjamin tiap blok punya acuan: alas kiri->kanan, tiang naik,
      // sudut atas dulu baru tengah atas.
      const rencana = []
      for (let dx = -1; dx <= 2; dx++) rencana.push([bx + dx, by, bz])
      for (let dy = 1; dy <= 3; dy++) rencana.push([bx - 1, by + dy, bz])
      for (let dy = 1; dy <= 3; dy++) rencana.push([bx + 2, by + dy, bz])
      rencana.push([bx - 1, by + 4, bz], [bx + 2, by + 4, bz],
                   [bx, by + 4, bz], [bx + 1, by + 4, bz])
      emit({ ev: 'portal_start', at: { x: bx, y: by, z: bz } })
      for (const [x, y, z] of rencana) {
        const t = new Vec3(x, y, z)
        let isi = bot.blockAt(t)
        if (isi && isi.name === 'obsidian') { dipasang++; continue }
        // Medan asli tidak rata: sel bingkai bisa berisi tanah/rumput
        // (TERUKUR: 12/14 dengan 'tempatnya_terisi'). Dia punya alat —
        // bersihkan dulu, jangan menyerah.
        if (isi && isi.boundingBox === 'block') {
          try {
            if (bot.tool) await bot.tool.equipForBlock(isi, { requireHarvest: false })
            await bot.dig(isi)
          } catch (e) { /* tak tergali: biarkan naruhSatu yang melaporkan */ }
        }
        const gagal = await naruhSatu(t, 'obsidian', jenis)
        if (!gagal) dipasang++
        else alasan = alasan || gagal
      }
      // Sel DALAM juga wajib kosong — api tidak menyala di atas rumput/blok.
      for (let dx = 0; dx <= 1; dx++) {
        for (let dy = 1; dy <= 3; dy++) {
          const b = bot.blockAt(new Vec3(bx + dx, by + dy, bz))
          if (b && b.boundingBox === 'block') {
            try { await bot.dig(b) } catch (e) {}
          }
        }
      }
      if (dipasang < 14) return
      alasan = ''
      alasan = await nyalakanPortal(bx, by, bz)
      if (alasan) return
      emit({ ev: 'portal_done', at: { x: bx, y: by + 1, z: bz } })
    } finally {
      if (alasan) {
        emit({ ev: 'task_failed', task: 'portal', reason: alasan,
               detail: `terpasang ${dipasang}/14` })
      }
      if (currentTask === 'portal') {
        currentTask = tugasLama === 'roam' ? 'roam' : 'idle'
        setFollow()
      }
    }
  },
  // Melangkah ke portal menyala terdekat dan menunggu dunia berganti.
  async masuk_portal (c) {
    const jenis = bot.registry.blocksByName.nether_portal
    const gerbang = jenis
      ? bot.findBlock({ matching: jenis.id, maxDistance: 24 })
      : null
    if (!gerbang) {
      emit({ ev: 'task_failed', task: 'masuk_portal', reason: 'tidak_ada_portal' })
      return
    }
    const dimAwal = bot.game ? String(bot.game.dimension) : '?'
    const tugasLama = currentTask
    currentTask = 'masuk_portal'
    roamTarget = null
    let alasan = ''
    try {
      await bot.pathfinder.goto(new goals.GoalNear(
        gerbang.position.x, gerbang.position.y, gerbang.position.z, 1))
      // Berdiri DI DALAM portal: pathfinder tidak mau masuk blok portal,
      // jadi didorong manual sebentar.
      await bot.lookAt(gerbang.position.offset(0.5, 0.5, 0.5), true)
      bot.setControlState('forward', true)
      await new Promise((r) => setTimeout(r, 900))
      bot.setControlState('forward', false)
      // Ganti dimensi butuh ~4 dtk berdiri diam di survival.
      const batas = Date.now() + 15000
      while (Date.now() < batas) {
        await new Promise((r) => setTimeout(r, 500))
        const dim = bot.game ? String(bot.game.dimension) : '?'
        if (dim !== dimAwal) {
          emit({ ev: 'ganti_dimensi', from: dimAwal, to: dim })
          return
        }
      }
      alasan = 'tidak_terkirim'
    } catch (e) {
      alasan = 'portal_tak_terjangkau'
    } finally {
      bot.setControlState('forward', false)
      if (alasan) emit({ ev: 'task_failed', task: 'masuk_portal', reason: alasan })
      if (currentTask === 'masuk_portal') {
        currentTask = tugasLama === 'roam' ? 'roam' : 'idle'
        setFollow()
      }
    }
  },
  // MENARA (permintaan operator [date removed]): "kalo mulai banyak mob, taruh blok
  // di kakinya 3 blok biar dia aman". Lompat + taruh blok di bawah kaki,
  // diulang. Mob pukul tidak bisa menjangkaunya di ketinggian 3.
  async menara (c) {
    const target = Math.max(2, Math.min(5, parseInt((c && c.count) || 3, 10) || 3))
    const { nama, n } = blokTerbanyakDiTas()
    if (!nama || n < target) {
      emit({ ev: 'task_failed', task: 'menara', reason: 'tidak_punya_blok' })
      return
    }
    const tugasLama = currentTask
    currentTask = 'menara'
    roamTarget = null
    bot.pathfinder.setGoal(null)
    const jenis = bot.registry.itemsByName[nama]
    let terpasang = 0
    let gagalBeruntun = 0
    try {
      await tungguDiam(800)
      for (let i = 0; i < target; i++) {
        const kaki = bot.entity.position.floored()
        const alas = bot.blockAt(kaki.offset(0, -1, 0))
        if (!alas || alas.boundingBox !== 'block') break
        await bot.equip(jenis.id, 'hand')
        await bot.lookAt(kaki.offset(0.5, -0.5, 0.5), true)
        // Tunggu KETINGGIAN NYATA, bukan tebakan milidetik: percobaan pertama
        // menaruh di 180 ms — server menolak diam-diam (badan masih menutup
        // sel), klien berdiri di blok hantu, dan event melapor naik 3 padahal
        // dunia bilang 0.
        bot.setControlState('jump', true)
        // Taruh DI PUNCAK lompatan: tinggi cukup (>= +1.0) DAN laju vertikal
        // sudah habis. Ambang-tinggi saja terukur rapuh (regresi [date removed]
        // malam: dua penaruhan beruntun ditolak server karena badannya masih
        // menutup sel saat paketnya tiba).
        const batasLompat = Date.now() + 900
        let siap = false
        while (Date.now() < batasLompat) {
          const dy = bot.entity.position.y - kaki.y
          const vy = bot.entity.velocity ? bot.entity.velocity.y : 1
          if (dy >= 1.0 && vy <= 0.05) { siap = true; break }
          await new Promise((r) => setTimeout(r, 20))
        }
        if (siap) {
          try { await bot.placeBlock(alas, new Vec3(0, 1, 0)) } catch (e) {}
        }
        bot.setControlState('jump', false)
        await new Promise((r) => setTimeout(r, 550))
        // Yang dihitung: blok yang BENAR-BENAR ada di dunia di sel kaki lama.
        const jadi = bot.blockAt(kaki)
        if (jadi && jadi.name === nama) { terpasang++; gagalBeruntun = 0 }
        else if (++gagalBeruntun >= 2) break
        // Satu kegagalan = coba lagi level yang sama (i tidak membaca posisi,
        // kaki dibaca ulang tiap putaran); dua beruntun = berhenti — lompat
        // kosong berulang persis yang dikeluhkan operator.
      }
    } finally {
      bot.setControlState('jump', false)
      if (terpasang > 0) emit({ ev: 'menara_done', naik: terpasang })
      else emit({ ev: 'task_failed', task: 'menara', reason: 'gagal_naruh' })
      if (currentTask === 'menara') {
        // DIAM DI PUNCAK, jangan balik roam: terukur, roam langsung membawanya
        // turun dari pilar yang baru dia bangun — padahal menara itu justru
        // untuk MENUNGGU kepungan bubar. Turun lagi itu keputusan LLM.
        currentTask = terpasang > 0 ? 'idle' : (tugasLama === 'roam' ? 'roam' : 'idle')
        if (terpasang === 0) setFollow()
      }
    }
  },
  // KEMBALI KE JASAD ([date removed]). Jalan ke titik kematian terakhir dan
  // pungut item yang tergeletak. Hasil dihitung dari SELISIH ISI TAS —
  // barang bisa saja sudah despawn, terbakar, atau jatuh ke jurang.
  async ambil_jasad (c) {
    if (!jasadPos || Date.now() - jasadTs > 300000) {
      emit({ ev: 'task_failed', task: 'jasad', reason: 'jasad_tidak_ada' })
      return
    }
    const tujuan = jasadPos.clone()
    // Batas jarak JUJUR: respawn tanpa bed bisa ribuan blok dari jasad, dan
    // berjalan sejauh itu melewati umur despawn (5 menit ~ 550 blok jalan).
    // Lebih baik mengaku tidak terkejar daripada berjalan tanpa harapan.
    const jarakAwal = bot.entity.position.distanceTo(tujuan)
    if (jarakAwal > 250) {
      emit({ ev: 'task_failed', task: 'jasad', reason: 'jasad_kejauhan',
             detail: String(Math.round(jarakAwal)) })
      jasadPos = null
      return
    }
    const tugasLama = currentTask
    currentTask = 'jasad'
    roamTarget = null
    const sebelum = bot.inventory.items().reduce((a, it) => a + it.count, 0)
    emit({ ev: 'jasad_jalan', pos: { x: tujuan.x, y: tujuan.y, z: tujuan.z },
           jarak: Math.round(bot.entity.position.distanceTo(tujuan)) })
    let alasan = ''
    try {
      await bot.pathfinder.goto(new goals.GoalNear(tujuan.x, tujuan.y, tujuan.z, 2))
      // Pungut: item entities tersedot sendiri dalam ~1.5 blok — sapu titik
      // sekitarnya beberapa detik supaya drop yang menyebar ikut terambil.
      const batas = Date.now() + 9000
      while (Date.now() < batas) {
        const item = bot.nearestEntity((e) =>
          e && e.name === 'item' && e.position &&
          e.position.distanceTo(bot.entity.position) < 12)
        if (!item) break
        try {
          await bot.pathfinder.goto(new goals.GoalNear(
            item.position.x, item.position.y, item.position.z, 0))
        } catch (e) { break }
      }
    } catch (e) {
      alasan = 'jasad_tak_terjangkau'
    } finally {
      const dapat = Math.max(0,
        bot.inventory.items().reduce((a, it) => a + it.count, 0) - sebelum)
      if (dapat > 0) {
        // Ada yang terselamatkan = jasadnya selesai diurus; jangan dikejar lagi.
        jasadPos = null
        emit({ ev: 'jasad_dapat', count: dapat })
      } else if (!alasan) {
        emit({ ev: 'task_failed', task: 'jasad', reason: 'jasad_kosong' })
        jasadPos = null      // sudah dicek, memang tidak ada apa-apa
      } else {
        emit({ ev: 'task_failed', task: 'jasad', reason: alasan })
      }
      if (currentTask === 'jasad') {
        currentTask = tugasLama === 'roam' ? 'roam' : 'idle'
        setFollow()
      }
    }
  },
  // NGEBRIDGE (permintaan operator: "menjauh lagi dengan ngebridge"). Menyusun
  // jembatan satu-blok ke arah hadapnya: taruh blok di sel depan (acuan = sisi
  // blok pijakan, dicari naruhSatu), verifikasi dari DUNIA, baru melangkah.
  // Sneak sepanjang jalan supaya tidak terpeleset dari bibir jembatan.
  async jembatan (c) {
    const target = Math.max(2, Math.min(16, parseInt((c && c.count) || 8, 10) || 8))
    const { nama, n } = blokTerbanyakDiTas()
    if (!nama || n < 2) {
      emit({ ev: 'task_failed', task: 'jembatan', reason: 'tidak_punya_blok' })
      return
    }
    const tugasLama = currentTask
    currentTask = 'jembatan'
    roamTarget = null
    bot.pathfinder.setGoal(null)
    // Arah dikunci SEKALI di awal dari hadapannya — jembatan itu garis lurus,
    // bukan ular. Komponen dominan yaw.
    const yaw = bot.entity.yaw
    const fx = -Math.sin(yaw)
    const fz = Math.cos(yaw)
    const dx = Math.abs(fx) >= Math.abs(fz) ? Math.sign(Math.round(fx * 2)) : 0
    const dz = dx === 0 ? Math.sign(Math.round(fz * 2)) : 0
    if (dx === 0 && dz === 0) {
      emit({ ev: 'task_failed', task: 'jembatan', reason: 'gagal_naruh' })
      currentTask = tugasLama
      return
    }
    const jenis = bot.registry.itemsByName[nama]
    const mulai = bot.entity.position.clone()
    let langkah = 0
    let gagalBeruntun = 0
    emit({ ev: 'jembatan_start', arah: { dx, dz }, target })
    try {
      await tungguDiam(800)
      bot.setControlState('sneak', true)
      for (let i = 0; i < target; i++) {
        const kaki = bot.entity.position.floored()
        const selDepan = kaki.offset(dx, -1, dz)     // pijakan berikutnya
        const isi = bot.blockAt(selDepan)
        if (!isi) break
        if (isi.boundingBox !== 'block') {
          await bot.lookAt(selDepan.offset(0.5, 0.5, 0.5), true)
          const gagal = await naruhSatu(selDepan, nama, jenis)
          if (gagal) {
            if (++gagalBeruntun >= 2) break
            continue
          }
          gagalBeruntun = 0
        }
        // Melangkah SATU sel — pandangan ke depan datar, maju sebentar,
        // lalu verifikasi posisi benar-benar berpindah.
        await bot.lookAt(bot.entity.position.offset(dx * 4, 1.0, dz * 4), true)
        bot.setControlState('forward', true)
        const batas = Date.now() + 2200
        while (Date.now() < batas) {
          const p2 = bot.entity.position.floored()
          if (p2.x === kaki.x + dx && p2.z === kaki.z + dz) break
          await new Promise((r) => setTimeout(r, 50))
        }
        bot.setControlState('forward', false)
        const kini = bot.entity.position.floored()
        if (kini.x === kaki.x + dx && kini.z === kaki.z + dz) langkah++
        else if (++gagalBeruntun >= 2) break
        if (jumlahDiTas(nama) <= 0) break
      }
    } finally {
      bot.setControlState('forward', false)
      bot.setControlState('sneak', false)
      // Kemajuan dibaca dari DUNIA: jarak datar dari titik awal.
      const maju = Math.round(Math.hypot(
        bot.entity.position.x - mulai.x, bot.entity.position.z - mulai.z))
      if (maju >= 2) emit({ ev: 'jembatan_done', maju, langkah })
      else emit({ ev: 'task_failed', task: 'jembatan', reason: 'jembatan_putus',
                  detail: `maju ${maju}` })
      if (currentTask === 'jembatan') {
        // Sukses = DIAM di seberang (pelajaran menara: balik roam terukur
        // langsung membawanya lari balik lewat jembatannya sendiri dalam
        // hitungan detik). Melanjutkan perjalanan itu keputusan LLM.
        currentTask = maju >= 2 ? 'idle' : (tugasLama === 'roam' ? 'roam' : 'idle')
        if (maju < 2) setFollow()
      }
    }
  },
  // SIMPAN / AMBIL barang di peti (fitur #4; probe [date removed]: openContainer
  // deposit/withdraw BEKERJA PENUH di [time removed].4). Hasil dihitung dari SELISIH
  // isi tas — kontainer bisa penuh/kosong tanpa bot menyadarinya.
  async simpan (c) { await urusPeti(c, 'simpan') },
  async ambil (c) { await urusPeti(c, 'ambil') },
  // PULANG ke peti-rumah.
  async pulang (c) {
    if (!rumahPos) {
      emit({ ev: 'task_failed', task: 'pulang', reason: 'belum_punya_rumah' })
      return
    }
    const tugasLama = currentTask
    currentTask = 'pulang'
    roamTarget = null
    try {
      await bot.pathfinder.goto(new goals.GoalNear(
        rumahPos.x, rumahPos.y, rumahPos.z, 2))
      emit({ ev: 'pulang_done' })
    } catch (e) {
      emit({ ev: 'task_failed', task: 'pulang', reason: 'rumah_tak_terjangkau' })
    } finally {
      if (currentTask === 'pulang') {
        currentTask = 'idle'
        // Sampai rumah = berhenti; ngapain di rumah itu keputusan LLM.
      }
    }
  },
  // PANAH (fitur #3, gerbang blaze/dragon). Serangan jarak jauh — jawaban
  // sejati untuk skeleton (mundur_tembok bertahan, panah MEMBALAS).
  async panah (c) {
    if (jumlahDiTas('bow') <= 0) {
      emit({ ev: 'task_failed', task: 'panah', reason: 'tidak_punya_busur' })
      return
    }
    if (jumlahDiTas('arrow') <= 0) {
      emit({ ev: 'task_failed', task: 'panah', reason: 'tidak_punya_panah' })
      return
    }
    const mau = String((c && c.target) || '').trim().toLowerCase()
    let sasaran = null
    if (mau) {
      sasaran = bot.nearestEntity((e) => e && e.position && e !== bot.entity &&
        String(e.name || '').toLowerCase() === mau &&
        bot.entity.position.distanceTo(e.position) <= 40) || null
    } else {
      sasaran = bot.nearestEntity((e) => e && e.position && e !== bot.entity &&
        (e.type === 'hostile' || (e.kind && String(e.kind).includes('Hostile'))) &&
        bot.entity.position.distanceTo(e.position) <= 40) || null
    }
    if (!sasaran) {
      emit({ ev: 'task_failed', task: 'panah', reason: 'tidak_ada_sasaran',
             detail: mau || 'musuh' })
      return
    }
    const jenisMusuh = String(sasaran.name || 'musuh')
    const tugasLama = currentTask
    currentTask = 'panah'
    roamTarget = null
    bot.pathfinder.setGoal(null)
    const panahAwal = jumlahDiTas('arrow')
    emit({ ev: 'panah_start', kind: jenisMusuh,
           jarak: Math.round(bot.entity.position.distanceTo(sasaran.position)) })
    let hasil = 'timeout'
    try {
      bot.hawkEye.autoAttack(sasaran, 'bow')
      const batas = Date.now() + 30000
      while (Date.now() < batas) {
        await new Promise((r) => setTimeout(r, 500))
        const e = bot.entities[sasaran.id]
        if (!e || e.isValid === false) { hasil = 'tumbang'; break }
        if (jumlahDiTas('arrow') <= 0) { hasil = 'panah_habis'; break }
        // Musuh merangsek dekat: busur bukan senjata jarak pendek.
        if (e.position.distanceTo(bot.entity.position) < 4) { hasil = 'kedekatan'; break }
      }
    } finally {
      try { bot.hawkEye.stop() } catch (e) { /* sudah berhenti */ }
      const terpakai = Math.max(0, panahAwal - jumlahDiTas('arrow'))
      if (hasil === 'tumbang') {
        statistik.bunuh += 1
        statistik.bunuh_panah += 1
        emit({ ev: 'panah_tumbang', kind: jenisMusuh, panah: terpakai })
      } else if (hasil === 'kedekatan') {
        emit({ ev: 'task_failed', task: 'panah', reason: 'musuh_kedekatan',
               detail: jenisMusuh })
      } else if (hasil === 'panah_habis') {
        emit({ ev: 'task_failed', task: 'panah', reason: 'tidak_punya_panah',
               detail: jenisMusuh })
      } else {
        emit({ ev: 'task_failed', task: 'panah', reason: 'panah_meleset',
               detail: jenisMusuh })
      }
      if (currentTask === 'panah') {
        currentTask = tugasLama === 'roam' ? 'roam' : 'idle'
        setFollow()
      }
    }
  },
  async eat(c) {
    if (bot.food >= 20) {
      emit({ ev: 'task_failed', task: 'eat', reason: 'sudah_kenyang' })
      return
    }
    // Lewat NAMA, bukan id. Diverifikasi di server nyata [date removed]: roti
    // punya `item.type` 886 sementara kunci di `registry.foods` 800/849/855/...
    // — tabel makanan TIDAK diindeks id item, jadi `foods[it.type]` selalu
    // undefined dan Arti selalu lapor "tidak punya makanan" walau tasnya
    // penuh roti. `foodsByName` cocok.
    const daftar = (bot.registry && bot.registry.foodsByName) || {}
    const gizi = (it) => daftar[it.name] || (bot.registry.foods || {})[it.type]
    const diminta = String((c && c.item) || '').trim()
    const makanan = bot.inventory.items().filter((it) => {
      if (!gizi(it)) return false
      return diminta ? it.name === diminta : true
    })
    if (!makanan.length) {
      emit({ ev: 'task_failed', task: 'eat', reason: 'tidak_punya_makanan' })
      return
    }
    // Yang paling mengenyangkan duluan — kalau dia cuma punya satu jenis ini
    // tidak berpengaruh, tapi mencegah menghabiskan steak untuk 1 bar.
    makanan.sort((a, b) =>
      ((gizi(b) || {}).foodPoints || 0) - ((gizi(a) || {}).foodPoints || 0))
    const pilih = makanan[0]
    const sebelum = bot.food
    try {
      await bot.equip(pilih, 'hand')
      await bot.consume()
    } catch (e) {
      emit({ ev: 'task_failed', task: 'eat', reason: 'gagal_makan',
             detail: String(e.message || e).slice(0, 80) })
      return
    }
    // SENGAJA tanpa `food_after`: `bot.food` baru berubah saat server mengirim
    // paket kesehatan, jadi membacanya di sini memberi nilai LAMA (terbukti
    // [date removed]: 0 -> 0 padahal steak masuk), sementara MENUNGGUNYA menunda
    // reaksi Arti beberapa detik. Nilai barunya toh datang sendiri di event
    // `status` berikutnya, dan reaksinya memang dilarang menyebut angka.
    emit({ ev: 'ate', item: pilih.name, food_before: sebelum })
  },
  quit() {
    // POV ditutup DULU: keluar selagi soketnya hidup bikin libuv abort, dan
    // exit code aneh itu terbaca Python seperti bot yang jatuh sendiri.
    stopPovServer(() => {
      try { bot.quit() } catch (e) {}
      process.exit(0)
    })
    setTimeout(() => process.exit(0), 1500).unref()
  },
}

// SUMBAT BADAI A* (akar OOM, lapis sumber): goal yang tak terjangkau membuat
// pathfinder menghitung ulang jalur SINKRON berulang-ulang (partial/noPath ->
// path kosong -> hitung lagi) tanpa pernah berhenti sendiri — terukur
// ~30-100 MB alokasi/dtk sampai heap tewas, DAN event loop tersumbat sampai
// watchdog ikut kelaparan. Aturan: 5 hasil non-success beruntun TANPA
// kemajuan posisi (<2 blok) = goal itu buntu, BUANG.
let jalurBuntu = { n: 0, pos: null }
bot.on('path_update', (r) => {
  // Koreografi portal (cor) mengelola anggaran geraknya sendiri — semua
  // langkahnya dekati() ber-timeout. Pagar ini pernah membuang goal-nya di
  // tengah panjat tanggul/menara (uji #19: task_failed jalur_buntu saat
  // 'portal') dan menghentikan pengecoran yang sehat.
  if (currentTask === 'portal') { jalurBuntu.n = 0; return }
  const st = r && r.status
  if (st === 'success') { jalurBuntu.n = 0; return }
  const p = bot.entity && bot.entity.position
  if (!p) return
  if (jalurBuntu.pos && p.distanceTo(jalurBuntu.pos) < 2) jalurBuntu.n += 1
  else jalurBuntu.n = 1
  jalurBuntu.pos = p.clone()
  if (jalurBuntu.n < 5) return
  jalurBuntu.n = 0
  log(`jalur buntu (${st} x5 tanpa maju) — goal dibuang, tugas '${currentTask}'`)
  try { bot.pathfinder.setGoal(null) } catch (e) { /* goal kosong */ }
  if (currentTask === 'roam') {
    // Tujuan buntu di-blacklist supaya pemilih tidak mengulanginya.
    if (roamTarget) {
      titikNyangkut.push({ x: roamTarget.x, y: roamTarget.y,
                           z: roamTarget.z, ts: Date.now() })
    }
    startRoam('jalur_buntu')
  } else if (currentTask !== 'idle' && currentTask !== 'nambang') {
    // nambang: collectBlock punya penjaganya sendiri (batas waktu di mine).
    emit({ ev: 'task_failed', task: currentTask, reason: 'jalur_buntu' })
  }
})

bot.on('goal_reached', () => {
  if (currentTask === 'come') {
    // Jangan percaya goal_reached mentah: audit [date removed] menemukan bot
    // melapor "sampai" padahal streamer 39 blok di ATASNYA (pathfinder
    // menyerah di titik terdekat yang bisa dijangkau).
    const t = player(STREAMER)
    const jarak = t && bot.entity
      ? bot.entity.position.distanceTo(t.position)
      : Infinity
    if (jarak > 5) {
      emit({ ev: 'task_failed', task: 'come', reason: 'unreachable' })
    } else {
      emit({ ev: 'task_done', task: 'come', detail: 'sampai' })
    }
    setFollow()
  } else if (currentTask === 'roam') {
    // Satu etape selesai -> laporkan sekali (bahan cerita "aku nyampe di...")
    // lalu langsung pilih tujuan berikutnya; solo tidak boleh berhenti.
    const p = bot.entity && bot.entity.position
    emit({
      ev: 'roam_leg',
      pos: p ? { x: Math.round(p.x), y: Math.round(p.y), z: Math.round(p.z) } : null,
    })
    startRoam('leg_done')
  }
})

const rl = readline.createInterface({ input: process.stdin })
rl.on('line', (line) => {
  let c
  try { c = JSON.parse(line) } catch (e) { log('bad cmd json:', line.slice(0, 80)); return }
  const fn = handlers[c && c.cmd]
  if (!fn) {
    // Whitelist Python memuat mine/place/give (disiapkan untuk Phase 2) yang
    // BELUM ada handler-nya di sini. Dulu diam-diam dibuang: Arti mengira
    // aksinya jalan lalu bercerita menambang ke penonton. Laporkan gagal.
    log('unknown cmd:', c && c.cmd)
    emit({ ev: 'task_failed', task: String((c && c.cmd) || '?'), reason: 'belum_bisa' })
    return
  }
  // MODE TAMU: aksi pengubah dunia DITOLAK BERSUARA (invariant kejujuran —
  // dia tidak boleh mengaku menambang yang tidak pernah terjadi). Refleks
  // internal digerbang senyap di tempatnya masing-masing; ini pintu untuk
  // perintah LLM/console. `tidur` sengaja TIDAK terlarang: bed = respawn.
  if (MODE_TAMU && TAMU_TERLARANG.has(c && c.cmd)) {
    emit({ ev: 'task_failed', task: String(c.cmd), reason: 'mode_tamu' })
    return
  }
  epokTugas += 1
  ingatSebelumJalan(c)
  perintahJalan += 1

  let hasil
  try { hasil = fn(c) } catch (e) {
    emit({ ev: 'error', where: 'cmd:' + c.cmd, message: String(e.message || e).slice(0, 200) })
  }
  // Handler-nya async: `try/catch` di atas cuma menangkap lemparan SINKRON.
  // Penyambungan tugas harus menunggu promise-nya benar-benar selesai.
  Promise.resolve(hasil).catch((e) => {
    emit({ ev: 'error', where: 'cmd:' + c.cmd, message: String((e && e.message) || e).slice(0, 200) })
  }).then(() => sesudahPerintah(c))
})

function ingatSebelumJalan (c) {
  const nama = c && c.cmd
  if (PEMBATAL_LANJUT.has(nama)) {
    if (tugasTerpotong) {
      emit({ ev: 'lanjut_batal', task: tugasTerpotong.c.cmd, sebab: nama })
      tugasTerpotong = null
    }
    perintahAktif = null
    return
  }
  if (perintahAktif && perintahAktif !== c && TUGAS_BISA_LANJUT.has(perintahAktif.cmd)) {
    tugasTerpotong = { c: perintahAktif, at: Date.now() }
    emit({ ev: 'tugas_disela', task: perintahAktif.cmd, oleh: nama })
  }
  // Penyela yang tidak bisa dilanjut (`serang`, `kabur`) TETAP melepas pegangan:
  // kalau dibiarkan menunjuk perintah lama, perintah pendek berikutnya akan
  // mencatat tugas terpotong yang sama untuk kedua kalinya dan me-reset jam
  // kedaluwarsanya.
  perintahAktif = TUGAS_BISA_LANJUT.has(nama) ? c : null
}

function sesudahPerintah (c) {
  perintahJalan = Math.max(0, perintahJalan - 1)
  if (perintahAktif === c) perintahAktif = null
  const t = tugasTerpotong
  if (!t) return
  if (PEMBATAL_LANJUT.has(c && c.cmd)) return  // sudah dibersihkan di depan
  // Masih ada perintah lain yang berjalan — termasuk penyela yang tidak masuk
  // daftar tugas-panjang. Jangan menyerobot; yang terakhir selesai nanti yang
  // memicu penyambungan.
  if (perintahJalan > 0 || perintahAktif) return
  const jeda = Date.now() - t.at
  if (jeda > LANJUT_KEDALUWARSA_MS) {
    emit({ ev: 'lanjut_batal', task: t.c.cmd, sebab: 'kedaluwarsa' })
    tugasTerpotong = null
    return
  }
  tugasTerpotong = null
  emit({ ev: 'lanjut_tugas', task: t.c.cmd, jeda_dtk: Math.round(jeda / 1000) })
  const fn = handlers[t.c.cmd]
  if (!fn) return
  epokTugas += 1
  perintahAktif = t.c
  perintahJalan += 1
  Promise.resolve(fn(t.c)).catch(() => {}).then(() => sesudahPerintah(t.c))
}
rl.on('close', () => {
  stopPovServer(() => {
    try { bot.quit() } catch (e) {}
    process.exit(0)
  })
  setTimeout(() => process.exit(0), 1500).unref()
})
