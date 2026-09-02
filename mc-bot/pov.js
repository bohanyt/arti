// POV penonton (Phase 3): siarkan pandangan Arti ke halaman web lokal supaya
// OBS bisa memasangnya sebagai Browser Source. Tanpa ini, sesi Minecraft cuma
// suara — penonton tidak melihat apa pun yang dia lakukan.
//
// KENAPA TIDAK MEMAKAI `require('prismarine-viewer').mineflayer` LANGSUNG:
//
//   1. `prismarine-viewer/index.js` menarik `viewer/lib/entities.js`, yang
//      `require('canvas')` di lingkup modul — modul NATIVE yang tidak ikut
//      terpasang (cuma devDependency di sana). Di Windows + Node 24 itu
//      berarti kompilasi node-gyp, dan siapa pun yang mengkloning repo ini
//      ikut kena. Padahal canvas dipakai HANYA untuk menggambar nametag, dan
//      itu terjadi di bundel BROWSER yang sudah jadi (public/index.js) — sisi
//      Node tidak pernah benar-benar memerlukannya. `WorldView` sendiri cuma
//      butuh simpleUtils + vec3 + events. Jadi kita impor WorldView langsung
//      dan lewati rantai itu.
//
//   2. `lib/mineflayer.js` memanggil `http.listen(port)` tanpa listener
//      'error'. Port yang sudah dipakai = EADDRINUSE tak tertangkap = SELURUH
//      proses bot mati. Di sini servernya kita yang pegang, jadi errornya
//      bisa ditangani sebagai "POV tidak jadi" — bot terus jalan.
//
// Isi soket sengaja dijaga persis sama dengan lib/mineflayer.js: halaman yang
// dilayani adalah bundel bawaan prismarine-viewer, jadi kontraknya
// ('version', 'position', WorldView.listenToBot) tidak boleh menyimpang.

const path = require('path')

// Model pemain SLIM (lengan 3 px, ala Alex) — skin Arti memang varian slim
// (arti_skin_mineskin_slim.json: variant=slim). Di skin Minecraft, PNG-nya
// sama persis untuk classic & slim; yang membedakan cuma MODEL yang dipakai
// klien. prismarine-viewer hanya punya model lebar, jadi tanpa ini tekstur
// lengan 3 px Arti direntang ke kotak 4 px dan lengannya terlihat salah.
//
// Cukup ubah lebar cube 4 -> 3 (dan geser origin lengan kanan): UV di
// Entity.js diturunkan LANGSUNG dari `cube.size`
// (`u = (cube.uv[0] + dot(..., cube.size)) / texWidth`), jadi satu perubahan
// membetulkan geometri sekaligus pemetaan teksturnya. Nilai uv tidak perlu
// disentuh — hasilnya kebetulan persis tata letak slim vanilla.
//
// DIBATASI ke blok model pemain: pola cube yang sama muncul 13x di bundel
// (zombie, skeleton, husk, ...), dan mengecilkan lengan mereka semua jelas
// bukan yang diminta.
const SLIM_TAMBALAN = [
  ['"origin":[-8,12,-2],"size":[4,12,4],"uv":[40,16]',
   '"origin":[-7,12,-2],"size":[3,12,4],"uv":[40,16]'],   // rightArm
  ['"origin":[-8,12,-2],"size":[4,12,4],"uv":[40,32]',
   '"origin":[-7,12,-2],"size":[3,12,4],"uv":[40,32]'],   // rightSleeve
  ['"origin":[4,12,-2],"size":[4,12,4],"uv":[32,48]',
   '"origin":[4,12,-2],"size":[3,12,4],"uv":[32,48]'],    // leftArm
  ['"origin":[4,12,-2],"size":[4,12,4],"uv":[48,48]',
   '"origin":[4,12,-2],"size":[3,12,4],"uv":[48,48]'],    // leftSleeve
]

function slimkanPemain(src, log) {
  const i = src.indexOf('"minecraft:player"')
  if (i < 0) { log('POV: model pemain tidak ketemu — lengan tetap lebar'); return src }
  const AKHIR = 3000                       // blok geometri pemain jauh lebih pendek
  const kepala = src.slice(0, i)
  let badan = src.slice(i, i + AKHIR)
  const ekor = src.slice(i + AKHIR)
  let kena = 0
  for (const [cari, ganti] of SLIM_TAMBALAN) {
    if (badan.includes(cari)) { badan = badan.replace(cari, ganti); kena++ }
  }
  log(kena === SLIM_TAMBALAN.length
    ? 'POV: model pemain jadi slim (lengan 3 px)'
    : `POV: ${kena}/4 lengan slim cocok — sisanya tetap lebar`)
  return kepala + badan + ekor
}

function startPov(bot, opts) {
  const port = Number(opts.port)
  const viewDistance = Number(opts.viewDistance) || 8
  // 0 = mentah (seperti dulu), makin tinggi makin halus tapi makin telat.
  const smooth = Number.isFinite(Number(opts.smooth))
    ? Math.min(0.95, Math.max(0, Number(opts.smooth))) : 0.6
  // Sudut pandang: pertama | belakang | depan | putar (gantian otomatis).
  const MODE_SAH = ['pertama', 'belakang', 'depan', 'putar']
  const mode = MODE_SAH.includes(String(opts.mode)) ? String(opts.mode) : 'putar'
  const cycleSec = Number(opts.cycleSec) > 0 ? Number(opts.cycleSec) : 20
  const slim = opts.slim !== false
  const bodySec = Number(opts.bodySec) > 0 ? Number(opts.bodySec) : 4
  const log = opts.log || (() => {})

  if (!(port > 0)) {
    log('POV mati (pov-port 0)')
    return null
  }

  let express, compression, socketio, WorldView, pvPublic
  try {
    express = require('express')
    compression = require('compression')
    socketio = require('socket.io')
    WorldView = require('prismarine-viewer/viewer/lib/worldView').WorldView
    pvPublic = path.join(
      path.dirname(require.resolve('prismarine-viewer/package.json')),
      'public'
    )
  } catch (e) {
    log('POV dilewati — dependensinya belum lengkap:', String(e.message || e))
    log('  Perbaiki: cd mc-bot && npm install')
    return null
  }

  let http, io
  try {
    const app = express()
    app.use(compression())

    // Tekstur pemain. Keluhan operator [date removed]: "povnya liat aku steve" —
    // dan memang itu yang terjadi, apa adanya: prismarine-viewer memetakan
    // SEMUA pemain ke satu tekstur (`textures/entity/steve`) dan tidak pernah
    // mengambil skin dari server. Bukan tekstur hilang, melainkan skin bawaan.
    //
    // Rute ini jalan keluarnya: simpan PNG di mc-bot/pov-textures/steve.png
    // dan pemain lain memakai skin itu — praktisnya operator tampil sebagai
    // dirinya sendiri. Regex, bukan path harfiah: versi yang diminta bisa
    // berubah (model entity dipatok '[time removed].4', atlas dunia ikut versi server).
    const skinSendiri = path.join(__dirname, 'pov-textures', 'steve.png')
    app.get(/^\/textures\/([^/]+)\/entity\/steve\.png$/, (req, res, next) => {
      const fs = require('fs')
      if (fs.existsSync(skinSendiri)) return res.sendFile(skinSendiri)
      // Tanpa skin sendiri: pakai bawaan. Versi yang diminta dicoba DULU,
      // lalu versi lain sebagai jaring — Entity.js mematok '[time removed].4' untuk
      // model entity sementara atlas dunia memakai versi server, dan sejak
      // [time removed] Mojang memindahkan berkasnya ke entity/player/wide/. Dua-duanya
      // ditelusuri supaya tidak ada jalur yang berakhir polos.
      const kandidat = [req.params[0], '1.21.4', '1.21.1', '1.20.1', '1.19']
      for (const v of kandidat) {
        for (const sub of [['player', 'wide', 'steve.png'], ['steve.png']]) {
          const f = path.join(pvPublic, 'textures', v, 'entity', ...sub)
          if (fs.existsSync(f)) return res.sendFile(f)
        }
      }
      return next()
    })

    // --- blok ungu ---------------------------------------------------------
    // prismarine-viewer [time removed] belum punya blockstate untuk 35 blok yang MASUK di
    // [time removed].4 (seluruh isi Pale Garden: pale oak, pale moss, eyeblossom, resin,
    // creaking heart). Blok tanpa blockstate = tekstur hilang = kotak ungu-hitam
    // (operator [date removed]: "ada ungu ungu"). Server dunianya [time removed].4, jadi biome
    // itu memang ada dan pasti kelewatan.
    //
    // Kompromi yang disengaja: dipetakan ke blok mirip yang teksturnya ADA.
    // Pale oak jadi birch, resin jadi bata. SALAH secara harfiah, tapi ungu
    // menyala di tengah siaran jauh lebih buruk daripada kayu yang warnanya
    // meleset. Kalau prismarine-viewer nanti menambahkan aslinya, entri asli
    // yang menang (kita hanya mengisi yang KOSONG).
    const MIRIP = {
      pale_oak_wood: 'birch_wood', pale_oak_planks: 'birch_planks',
      pale_oak_sapling: 'birch_sapling', pale_oak_log: 'birch_log',
      stripped_pale_oak_log: 'stripped_birch_log',
      stripped_pale_oak_wood: 'stripped_birch_wood',
      pale_oak_leaves: 'birch_leaves', pale_oak_sign: 'birch_sign',
      pale_oak_wall_sign: 'birch_wall_sign',
      pale_oak_hanging_sign: 'birch_hanging_sign',
      pale_oak_wall_hanging_sign: 'birch_wall_hanging_sign',
      pale_oak_pressure_plate: 'birch_pressure_plate',
      pale_oak_trapdoor: 'birch_trapdoor', pale_oak_button: 'birch_button',
      pale_oak_stairs: 'birch_stairs', pale_oak_slab: 'birch_slab',
      pale_oak_fence_gate: 'birch_fence_gate', pale_oak_fence: 'birch_fence',
      pale_oak_door: 'birch_door',
      potted_pale_oak_sapling: 'potted_birch_sapling',
      pale_moss_block: 'moss_block', pale_moss_carpet: 'moss_carpet',
      pale_hanging_moss: 'hanging_roots',
      open_eyeblossom: 'oxeye_daisy', closed_eyeblossom: 'white_tulip',
      potted_open_eyeblossom: 'potted_oxeye_daisy',
      potted_closed_eyeblossom: 'potted_white_tulip',
      creaking_heart: 'oak_log',
      resin_clump: 'glow_lichen', resin_block: 'honeycomb_block',
      resin_bricks: 'bricks', resin_brick_stairs: 'brick_stairs',
      resin_brick_slab: 'brick_slab', resin_brick_wall: 'brick_wall',
      chiseled_resin_bricks: 'bricks',
    }
    const CADANGAN_TERAKHIR = 'oak_planks'
    const cacheState = {}
    app.get(/^\/blocksStates\/([^/]+)\.json$/, (req, res, next) => {
      const versi = req.params[0]
      if (cacheState[versi]) return res.type('json').send(cacheState[versi])
      const fs = require('fs')
      const berkas = path.join(pvPublic, 'blocksStates', versi + '.json')
      if (!fs.existsSync(berkas)) return next()
      try {
        const data = JSON.parse(fs.readFileSync(berkas, 'utf8'))
        let ditambal = 0
        for (const [hilang, pengganti] of Object.entries(MIRIP)) {
          if (data[hilang]) continue                 // sudah ada aslinya
          const sumber = data[pengganti] || data[CADANGAN_TERAKHIR]
          if (!sumber) continue
          data[hilang] = sumber
          ditambal++
        }
        cacheState[versi] = JSON.stringify(data)
        if (ditambal) log(`POV: ${ditambal} blok tanpa tekstur ditambal (${versi})`)
        return res.type('json').send(cacheState[versi])
      } catch (e) {
        log('POV: gagal menambal blockstate, pakai aslinya:', String(e.message || e))
        return next()
      }
    })

    // --- terlalu datar -----------------------------------------------------
    // Renderer ini cuma punya AmbientLight + satu DirectionalLight: tidak ada
    // smooth lighting, ambient occlusion, cahaya blok, maupun kabut. operator
    // [date removed]: "keliatan flat". Tidak ada shader yang bisa dinyalakan —
    // bundel browsernya sudah jadi. Yang bisa: dua tambalan bedah pada bundel
    // saat DISAJIKAN (node_modules tidak pernah disentuh, jadi npm install
    // tidak menghapusnya):
    //   1. kabut sewarna langit -> dunia memudar di batas chunk, bukan
    //      terpotong seperti meja. Ini yang paling terasa.
    //   2. ambient diturunkan + directional dinaikkan -> sisi blok punya beda
    //      terang, jadi bentuknya terbaca.
    // Kalau polanya tidak ketemu (prismarine-viewer naik versi), bundel ASLI
    // yang disajikan — tampilannya balik seperti sekarang, tidak rusak.
    const kepadatanKabut = (1.2 / (viewDistance * 16)).toFixed(5)
    let bundelCache = null
    app.get('/index.js', (req, res, next) => {
      if (bundelCache) return res.type('application/javascript').send(bundelCache)
      const fs = require('fs')
      const berkas = path.join(pvPublic, 'index.js')
      if (!fs.existsSync(berkas)) return next()
      let src = fs.readFileSync(berkas, 'utf8')
      const tambalan = [
        ['this.scene.add(this.directionalLight)',
         'this.scene.add(this.directionalLight),this.scene.fog=new n.FogExp2('
         + `this.scene.background.getHex(),${kepadatanKabut})`],
        ['new n.DirectionalLight(16777215,.5)',
         'new n.DirectionalLight(16777215,.9)'],
        ['new n.AmbientLight(13421772)', 'new n.AmbientLight(10066329)'],
      ]
      let kena = 0
      for (const [cari, ganti] of tambalan) {
        if (src.includes(cari)) { src = src.replace(cari, ganti); kena++ }
      }
      if (slim) src = slimkanPemain(src, log)
      log(kena === tambalan.length
        ? `POV: kabut + pencahayaan dipertegas (kepadatan ${kepadatanKabut})`
        : `POV: ${kena}/${tambalan.length} tambalan tampilan cocok — sisanya pakai bawaan`)
      bundelCache = src
      return res.type('application/javascript').send(bundelCache)
    })

    app.use('/', express.static(pvPublic))
    http = require('http').createServer(app)

    io = socketio(http, { path: '/socket.io' })
    io.on('connection', (socket) => {
      // Versi dikirim duluan: bundel browser memakainya untuk memilih atlas
      // tekstur (public/textures/<versi>). Server operator [time removed].4 ada di sana.
      socket.emit('version', bot.version)
      const worldView = new WorldView(
        bot.world, viewDistance, bot.entity.position, socket
      )
      worldView.init(bot.entity.position)

      // --- kamera goyang -------------------------------------------------
      // operator [date removed]: "puyeng liatnya jitter jitter". Dua sebab, dua-duanya
      // di luar kendali bundel browser:
      //
      //   1. ROTASI TIDAK DIINTERPOLASI SAMA SEKALI. viewer.js men-tween posisi
      //      selama 50 ms tapi arah kamera dipasang mentah:
      //      `camera.rotation.set(pitch, yaw, 0, 'ZYX')`. Pathfinder mematok
      //      kepala bot ke waypoint berikutnya, jadi tiap belokan = sentakan.
      //   2. `bot.emit('move')` dipancarkan dari TIGA tempat di physics.js,
      //      jadi kedatangannya tidak berjarak tetap — tween 50 ms kadang
      //      tumpang tindih, kadang telat.
      //
      // Perbaikannya di sisi kita: pancarkan pada cadence TETAP, dan redam
      // sudut dengan rata-rata bergerak yang sadar putaran -PI..PI. Redaman
      // memang menambah sedikit jeda (~100 ms bersama tween) — untuk kamera
      // tontonan itu jauh lebih murah daripada goyangan.
      const RATE_MS = 33                       // ~30 fps, di bawah tween 50 ms
      const a = Math.min(1, Math.max(0.05, 1 - smooth))   // 0 = beku, 1 = mentah
      let hYaw = bot.entity.yaw
      let hPitch = bot.entity.pitch
      let hPos = { x: bot.entity.position.x, y: bot.entity.position.y,
                   z: bot.entity.position.z }

      function redamSudut(lama, baru, t) {
        // Selisih dinormalkan ke (-PI, PI] dulu — tanpa ini, yaw yang
        // melewati batas putaran bikin kamera berputar penuh ke arah salah.
        const d = ((baru - lama + Math.PI) % (Math.PI * 2) + Math.PI * 2)
          % (Math.PI * 2) - Math.PI
        return lama + d * t
      }

      // --- ganti sudut pandang (F5) ----------------------------------------
      // operator [date removed]: "f5 si arti gabisa? gabisa perspective lain?"
      //
      // Jalur bawaan renderer buntu: begitu mode orang-pertama menyala, ia
      // MEMBUANG OrbitControls secara permanen (`controls.dispose(); controls
      // = null`), jadi cabang orang-ketiga tidak bisa dipakai lagi — dan cabang
      // itu pun cuma kamera orbit diam 20 blok di atas, bukan F5.
      //
      // Jadi dikerjakan dari sisi server, tanpa tambalan bundel tambahan:
      //   1. worldView SENGAJA tidak pernah mengirim entity bot sendiri
      //      (`if (e === bot.entity) return`) — itu sebabnya badan Arti tidak
      //      ada di layar. Kita kirim sendiri paket `entity` untuk dia, dan
      //      renderer menggambar model pemain lengkap dengan skinnya.
      //   2. Kamera tetap lewat jalur orang-pertama (satu-satunya yang
      //      MENGIKUTI dia), tapi posisinya kita mundurkan/majukan sepanjang
      //      arah pandang. Hasilnya persis F5: belakang bahu & depan muka.
      const JARAK_BADAN = 4.0        // blok di belakang/depan Arti
      const ID_ARTI = (bot.entity && bot.entity.id) || 999999
      let badanTampil = false
      let modeTerakhir = ''

      function modeSaatIni() {
        if (mode !== 'putar') return mode
        // Orang-pertama dapat giliran panjang, orang-ketiga sebentar saja —
        // tujuannya "biar pada liat skin dia", bukan bikin penonton pusing.
        const siklus = (cycleSec + bodySec) * 1000
        const t = Date.now() % siklus
        return t < cycleSec * 1000 ? 'pertama' : 'belakang'
      }

      function arahPandang(yaw, pitch) {
        // Sesuai Euler 'ZYX' yang dipakai renderer: R = Ry(yaw)*Rx(pitch),
        // kamera menghadap -Z lokal.
        const cp = Math.cos(pitch)
        return { x: -Math.sin(yaw) * cp, y: Math.sin(pitch), z: -Math.cos(yaw) * cp }
      }

      function kirimPosisi() {
        const e = bot.entity
        if (!e || !e.position) return
        if (smooth <= 0) {
          hPos = { x: e.position.x, y: e.position.y, z: e.position.z }
          hYaw = e.yaw
          hPitch = e.pitch
        } else {
          hPos.x += (e.position.x - hPos.x) * a
          hPos.y += (e.position.y - hPos.y) * a
          hPos.z += (e.position.z - hPos.z) * a
          hYaw = redamSudut(hYaw, e.yaw, a)
          hPitch += (e.pitch - hPitch) * a
        }

        const m = modeSaatIni()
        let camPos = hPos
        let camYaw = hYaw
        let camPitch = hPitch
        if (m === 'belakang' || m === 'depan') {
          const maju = m === 'depan'
          // Mode depan: kamera di DEPAN muka, menghadap balik ke arah dia.
          if (maju) { camYaw = hYaw + Math.PI; camPitch = -hPitch }
          const f = arahPandang(hYaw, hPitch)
          const s = maju ? JARAK_BADAN : -JARAK_BADAN
          camPos = { x: hPos.x + f.x * s, y: hPos.y + f.y * s, z: hPos.z + f.z * s }
        }

        // Badan Arti hanya ada saat dibutuhkan: kalau dibiarkan tampil di mode
        // orang-pertama, yang terlihat justru bagian dalam kepalanya sendiri.
        if (m !== 'pertama') {
          if (!badanTampil) {
            socket.emit('entity', {
              id: ID_ARTI, name: 'player', username: bot.username,
              pos: e.position, width: e.width || 0.6, height: e.height || 1.8,
            })
            badanTampil = true
          }
          socket.emit('entity', { id: ID_ARTI, pos: e.position, yaw: hYaw })
        } else if (badanTampil) {
          socket.emit('entity', { id: ID_ARTI, delete: true })
          badanTampil = false
        }
        if (m !== modeTerakhir) { modeTerakhir = m; log(`POV: sudut pandang -> ${m}`) }

        const packet = { pos: camPos, yaw: camYaw, addMesh: true }
        // pitch WAJIB ada: cabang tanpa pitch di renderer memakai OrbitControls
        // yang tidak mengikuti Arti sama sekali.
        packet.pitch = camPitch
        socket.emit('position', packet)
        // Muat/buang chunk memakai posisi ASLI, bukan yang diredam: kamera
        // boleh tertinggal sedikit, tapi dunia jangan sampai telat dimuat.
        worldView.updatePosition(e.position)
      }

      const detak = setInterval(() => {
        // PENJAGA BACKPRESSURE ([date removed]). Live [time removed]: bot MATI OOM di 4GB
        // setelah ~11 menit dengan POV tersambung ke OBS Browser Source. OBS
        // men-throttle browser yang tidak tampil (hipotesis operator 8 Agustus
        // soal lazy rendering — benar), kliennya berhenti mengonsumsi, dan
        // socket.io menimbun buffer kiriman TANPA BATAS. Klien yang tidak
        // mengejar diputus; OBS menyambung ulang sendiri dengan buffer segar.
        try {
          const antre = socket.conn && socket.conn.writeBuffer
            ? socket.conn.writeBuffer.length : 0
          if (antre > 400) {
            log(`POV: klien tersendat (${antre} paket menumpuk) — diputus demi memori`)
            socket.disconnect(true)
            return
          }
        } catch (_) {}
        kirimPosisi()
      }, RATE_MS)
      worldView.listenToBot(bot)
      socket.on('disconnect', () => {
        // WAJIB dilepas: OBS memutus & menyambung ulang tiap kali scene-nya
        // dipakai lagi. Tanpa ini detak menumpuk sepanjang siaran dan tiap
        // langkah Arti mengirim posisi ke soket yang sudah mati.
        clearInterval(detak)
        worldView.removeListenersFromBot(bot)
      })
    })

    http.on('error', (e) => {
      const kode = e && (e.code || e.message)
      log(`POV dilewati — tidak bisa memakai port ${port} (${kode}).`,
          'Ganti minecraft_pov_port di config_local.json.')
      try { http.close() } catch (_) {}
    })
    http.listen(port, '127.0.0.1', () => {
      log(`POV siap di http://localhost:${port}`,
          `(jarak pandang=${viewDistance} chunk,`,
          `redam goyang=${smooth}, sudut=${mode})`)
    })
  } catch (e) {
    log('POV gagal dinyalakan (bot tetap jalan):', String(e.message || e))
    return null
  }
  // Penutup eksplisit. Tanpa ini `process.exit(0)` saat 'mc off' berjalan
  // selagi soket masih hidup, dan libuv menjeritkan assertion abort
  // ("!(handle->flags & UV_HANDLE_CLOSING)") — terlihat di uji [date removed].
  // Keluarnya jadi tampak seperti crash padahal disengaja.
  return {
    close(selesai) {
      try { io.close() } catch (_) {}
      try { http.close(selesai) } catch (_) { if (selesai) selesai() }
    },
  }
}

module.exports = { startPov }
