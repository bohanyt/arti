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

function arg(name, fallback) {
  const i = process.argv.indexOf('--' + name)
  return i >= 0 && process.argv[i + 1] ? process.argv[i + 1] : fallback
}

const HOST = arg('host', '127.0.0.1')
const PORT = parseInt(arg('port', '25565'), 10)
const USERNAME = arg('username', 'Arti')
const STREAMER = arg('streamer', 'Bohan')
const STATUS_SEC = parseInt(arg('status-interval', '10'), 10)

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

let currentTask = 'idle'
let lastHealth = 20
let movements = null
// Mode solo (permintaan Bohan 2026-08-04: "literally dia yang ambil alih 1
// stream"): kalau streamer TIDAK ada di dunia, bot jangan mematung — jelajah
// sendiri di sekitar titik home biar ada yang dikomentari & dilihat penonton.
let homePos = null
let roamTarget = null
let roamSince = 0
let roamManual = false
const ROAM_RADIUS = 48      // jarak jelajah maksimum dari home (blok)
const ROAM_TIMEOUT_MS = 45000  // satu tujuan gagal/kelamaan -> pilih tujuan lain

function player(name) {
  return bot.players[name] && bot.players[name].entity
}

function streamerHere() {
  return Boolean(player(STREAMER))
}

function setFollow() {
  const target = player(STREAMER)
  if (!target) {
    // Streamer belum kelihatan (jauh/belum join): JANGAN diam — solo dulu.
    // currentTask sengaja TIDAK di-set 'follow' di sini: kalau di-set, ticker
    // 5 dtk memanggil setFollow() lagi -> roam_start beruntun tiap 5 dtk.
    startRoam('streamer_absent')
    return
  }
  currentTask = 'follow'
  roamTarget = null
  bot.pathfinder.setGoal(new goals.GoalFollow(target, 3), true)
}

function pickRoamTarget() {
  const base = homePos || bot.entity.position
  const angle = Math.random() * Math.PI * 2
  const dist = 12 + Math.random() * (ROAM_RADIUS - 12)
  return {
    x: Math.round(base.x + Math.cos(angle) * dist),
    y: Math.round(base.y),
    z: Math.round(base.z + Math.sin(angle) * dist),
  }
}

function startRoam(reason) {
  if (!bot.entity) return
  if (currentTask !== 'roam') {
    currentTask = 'roam'
    emit({ ev: 'roam_start', reason: reason || 'manual' })
  }
  roamTarget = pickRoamTarget()
  roamSince = Date.now()
  // GoalNear: cukup "sampai sekitar sana" — target acak bisa saja di dalam
  // tebing/air, jangan sampai bot ngotot pada satu blok mustahil.
  bot.pathfinder.setGoal(
    new goals.GoalNear(roamTarget.x, roamTarget.y, roamTarget.z, 3), false)
}

setInterval(() => {
  if (!bot.entity) return
  if (currentTask === 'follow') {
    setFollow()
    return
  }
  if (currentTask === 'roam') {
    // Streamer nongol lagi -> otomatis balik nemenin, KECUALI roam disuruh
    // manual ([MC: roam] / 'mc roam' — Bohan sengaja nyuruh dia main sendiri).
    if (streamerHere() && !roamManual) {
      emit({ ev: 'roam_end', reason: 'streamer_back' })
      setFollow()
      return
    }
    if (Date.now() - roamSince > ROAM_TIMEOUT_MS) startRoam('next_leg')
  }
}, 5000)

function nearbyEntities() {
  const hostiles = []
  const players = []
  if (!bot.entity) return { hostiles, players }
  for (const e of Object.values(bot.entities)) {
    if (!e || !e.position || e === bot.entity) continue
    const d = e.position.distanceTo(bot.entity.position)
    if (d > 16) continue
    if (e.type === 'player' && e.username && e.username !== bot.username) {
      players.push({ name: e.username, distance: Math.round(d) })
    } else if (e.type === 'hostile' || (e.kind && String(e.kind).includes('Hostile'))) {
      hostiles.push({ kind: e.name || 'unknown', distance: Math.round(d) })
    }
  }
  return { hostiles, players }
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
    nearby_players: near.players,
    nearby_hostiles: near.hostiles,
  }
}

// ---------- events → stdout ----------

bot.once('spawn', () => {
  movements = new Movements(bot)
  bot.pathfinder.setMovements(movements)
  const p = bot.entity.position
  homePos = { x: p.x, y: p.y, z: p.z }  // titik acuan jelajah solo
  emit({ ev: 'spawned', pos: { x: Math.round(p.x), y: Math.round(p.y), z: Math.round(p.z) },
         health: Math.round(bot.health ?? 20), username: bot.username })
  log('spawned as', bot.username, 'at', p)
  setFollow()
  setInterval(() => {
    const s = statusEvent()
    if (s) emit(s)
  }, STATUS_SEC * 1000)
})

bot.on('health', () => {
  const h = Math.round(bot.health ?? 0)
  if (h < lastHealth) {
    const near = nearbyEntities()
    emit({ ev: 'hurt', health: h,
           source: near.hostiles.length ? near.hostiles[0].kind : 'unknown' })
    if (h <= 6) emit({ ev: 'low_health', health: h })
  }
  lastHealth = h
})

bot.on('death', () => {
  emit({ ev: 'death', message: '', killer: '' })
  log('died')
})

bot.on('respawn', () => {
  if (!bot.entity) return
  const p = bot.entity.position
  emit({ ev: 'respawn', pos: { x: Math.round(p.x), y: Math.round(p.y), z: Math.round(p.z) } })
  setTimeout(() => setFollow(), 3000)
})

bot.on('chat', (username, message) => {
  if (username === bot.username) return
  emit({ ev: 'chat', from: username, text: String(message).slice(0, 200) })
})

bot.on('death_screen', () => {})

bot.on('kicked', (reason) => {
  emit({ ev: 'kicked', reason: String(reason).slice(0, 200) })
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
  quit() { try { bot.quit() } catch (e) {} ; process.exit(0) },
}

bot.on('goal_reached', () => {
  if (currentTask === 'come') {
    emit({ ev: 'task_done', task: 'come', detail: 'sampai' })
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
  if (!fn) { log('unknown cmd:', c && c.cmd); return }
  try { fn(c) } catch (e) {
    emit({ ev: 'error', where: 'cmd:' + c.cmd, message: String(e.message || e).slice(0, 200) })
  }
})
rl.on('close', () => { try { bot.quit() } catch (e) {} ; process.exit(0) })
