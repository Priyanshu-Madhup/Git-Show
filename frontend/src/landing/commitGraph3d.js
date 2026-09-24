import * as THREE from 'three'

// Colors match the agent badges inside the app, so the landing page and the
// product share one visual language: a branch is colored by the agent whose
// work it stands for.
const COLORS = {
  main: 0x34e58f,
  planner: 0xc9a6ff,
  reader: 0x7cc7ff,
  writer: 0xffc36b,
  memory: 0xff9ecf,
  quiet: 0x2b6b4d,
}

const SPACING = 2.2
const MAIN_COMMITS = 72
const INK = 0x050706

// Side branches. `stage` ties a branch to a section of the scroll story, so
// it lights up while that section is on screen; the rest are ambient history.
const BRANCHES = [
  { fork: 3, merge: 7, color: 'quiet', dir: [-1, 0.5], amp: 2.2 },
  { fork: 9, merge: 17, color: 'planner', dir: [1, 0.55], amp: 3.4, stage: 1 },
  { fork: 12, merge: 16, color: 'reader', dir: [-1, 0.8], amp: 2.6, stage: 1 },
  { fork: 23, merge: 31, color: 'writer', dir: [1.1, -0.45], amp: 3.2, stage: 2 },
  { fork: 26, merge: 29, color: 'quiet', dir: [-1, -0.6], amp: 1.8 },
  { fork: 37, merge: null, color: 'memory', dir: [-1, -0.7], amp: 5.5, length: 9, stage: 3 },
  { fork: 45, merge: 53, color: 'quiet', dir: [1, 0.8], amp: 2.8 },
  { fork: 56, merge: 63, color: 'quiet', dir: [-1, 0.3], amp: 3.1 },
  { fork: 60, merge: 68, color: 'quiet', dir: [0.8, -1], amp: 2.4 },
]

// Camera keyframes along the scroll: hero, then one per story stage, then the close.
const KEYFRAMES = [
  { p: 0.0, pos: [5.2, 2.6, 5.5], look: [0, 0, -13] },
  { p: 0.29, pos: [3.2, 1.8, -3], look: [0, 0.2, -18] },
  { p: 0.46, pos: [-5.2, 3.0, -14], look: [0.5, 0.6, -32] },
  { p: 0.64, pos: [2.6, 1.6, -40], look: [-3.2, -0.6, -58] },
  { p: 0.81, pos: [-4.8, -1.4, -72], look: [-3.5, -2, -92] },
  { p: 1.0, pos: [0.5, 7.5, -84], look: [0, 0, -128] },
]

const mainPoint = (i) => new THREE.Vector3(Math.sin(i * 0.35) * 0.7, Math.cos(i * 0.23) * 0.45, -i * SPACING)

const smooth = (t) => t * t * (3 - 2 * t)

function glowTexture() {
  const size = 128
  const canvas = document.createElement('canvas')
  canvas.width = canvas.height = size
  const ctx = canvas.getContext('2d')
  const g = ctx.createRadialGradient(size / 2, size / 2, 0, size / 2, size / 2, size / 2)
  g.addColorStop(0, 'rgba(255,255,255,1)')
  g.addColorStop(0.18, 'rgba(255,255,255,0.55)')
  g.addColorStop(0.5, 'rgba(255,255,255,0.12)')
  g.addColorStop(1, 'rgba(255,255,255,0)')
  ctx.fillStyle = g
  ctx.fillRect(0, 0, size, size)
  const texture = new THREE.CanvasTexture(canvas)
  texture.colorSpace = THREE.SRGBColorSpace
  return texture
}

function branchPath(branch) {
  const start = mainPoint(branch.fork)
  const offset = new THREE.Vector3(branch.dir[0], branch.dir[1], 0).normalize().multiplyScalar(branch.amp)
  const points = []
  if (branch.merge != null) {
    const end = mainPoint(branch.merge)
    const n = (branch.merge - branch.fork) * 2
    for (let k = 0; k <= n; k++) {
      const t = k / n
      points.push(start.clone().lerp(end, t).addScaledVector(offset, Math.sin(Math.PI * t)))
    }
  } else {
    const n = branch.length * 2
    for (let k = 0; k <= n; k++) {
      const t = k / n
      const along = mainPoint(branch.fork + t * branch.length)
      points.push(along.addScaledVector(offset, 1 - Math.cos((Math.PI / 2) * t)))
    }
  }
  return points
}

export function createCommitGraph(canvas, { reducedMotion = false } = {}) {
  const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true, powerPreference: 'high-performance' })
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.75))
  renderer.setClearColor(INK, 0)

  const scene = new THREE.Scene()
  scene.fog = new THREE.FogExp2(INK, 0.03)
  const camera = new THREE.PerspectiveCamera(48, 1, 0.1, 400)

  const glow = glowTexture()
  const disposables = [glow]

  // --- branches as tubes -----------------------------------------------------
  const tubes = []
  const addTube = (points, color, radius, startOrder, endOrder, stage) => {
    const curve = new THREE.CatmullRomCurve3(points)
    const geometry = new THREE.TubeGeometry(curve, points.length * 6, radius, 8, false)
    const material = new THREE.MeshBasicMaterial({ color, transparent: true, opacity: 0.9 })
    const mesh = new THREE.Mesh(geometry, material)
    geometry.setDrawRange(0, 0)
    scene.add(mesh)
    disposables.push(geometry, material)
    tubes.push({ mesh, geometry, material, startOrder, endOrder, stage, baseColor: new THREE.Color(color) })
  }

  const mainPoints = Array.from({ length: MAIN_COMMITS }, (_, i) => mainPoint(i))
  addTube(mainPoints, COLORS.main, 0.055, 0, MAIN_COMMITS - 1, 0)

  const commits = [] // { position, color, order, stage }
  mainPoints.forEach((p, i) => commits.push({ position: p, color: COLORS.main, order: i, stage: 0 }))

  BRANCHES.forEach((b) => {
    const points = branchPath(b)
    const color = COLORS[b.color]
    const end = b.merge ?? b.fork + b.length
    addTube(points, color, b.color === 'quiet' ? 0.03 : 0.045, b.fork, end, b.stage ?? -1)
    for (let k = 2; k < points.length - (b.merge != null ? 1 : 0); k += 2) {
      commits.push({ position: points[k], color, order: b.fork + (k / (points.length - 1)) * (end - b.fork), stage: b.stage ?? -1 })
    }
  })

  // --- commits as instanced spheres + glow sprites ---------------------------
  const sphere = new THREE.SphereGeometry(0.15, 18, 14)
  const sphereMaterial = new THREE.MeshBasicMaterial({ color: 0xffffff })
  const nodes = new THREE.InstancedMesh(sphere, sphereMaterial, commits.length)
  nodes.instanceMatrix.setUsage(THREE.DynamicDrawUsage)
  const tint = new THREE.Color()
  commits.forEach((c, i) => nodes.setColorAt(i, tint.set(c.color).lerp(new THREE.Color(0xffffff), 0.35)))
  scene.add(nodes)
  disposables.push(sphere, sphereMaterial)

  const glowGeometry = new THREE.BufferGeometry()
  const glowPositions = new Float32Array(commits.length * 3)
  const glowColors = new Float32Array(commits.length * 3)
  commits.forEach((c, i) => {
    glowPositions.set([c.position.x, c.position.y, c.position.z], i * 3)
    tint.set(c.color)
    glowColors.set([tint.r, tint.g, tint.b], i * 3)
  })
  glowGeometry.setAttribute('position', new THREE.BufferAttribute(glowPositions, 3))
  glowGeometry.setAttribute('color', new THREE.BufferAttribute(glowColors, 3))
  const glowMaterial = new THREE.PointsMaterial({
    size: 1.6,
    map: glow,
    vertexColors: true,
    transparent: true,
    depthWrite: false,
    blending: THREE.AdditiveBlending,
    opacity: 0.85,
  })
  const glows = new THREE.Points(glowGeometry, glowMaterial)
  scene.add(glows)
  disposables.push(glowGeometry, glowMaterial)

  // --- drifting dust, for depth ------------------------------------------------
  const dustCount = 1400
  const dustPositions = new Float32Array(dustCount * 3)
  for (let i = 0; i < dustCount; i++) {
    dustPositions.set([(Math.random() - 0.5) * 60, (Math.random() - 0.5) * 36, 20 - Math.random() * 190], i * 3)
  }
  const dustGeometry = new THREE.BufferGeometry()
  dustGeometry.setAttribute('position', new THREE.BufferAttribute(dustPositions, 3))
  const dustMaterial = new THREE.PointsMaterial({
    size: 0.28,
    map: glow,
    color: 0x8bffce,
    transparent: true,
    opacity: 0.22,
    depthWrite: false,
    blending: THREE.AdditiveBlending,
  })
  const dust = new THREE.Points(dustGeometry, dustMaterial)
  scene.add(dust)
  disposables.push(dustGeometry, dustMaterial)

  // --- state -------------------------------------------------------------------
  let progress = 0
  let stage = -1
  const pointer = { x: 0, y: 0 }
  const camPos = new THREE.Vector3(...KEYFRAMES[0].pos)
  const camLook = new THREE.Vector3(...KEYFRAMES[0].look)
  const growDuration = reducedMotion ? 0 : 3200
  const startTime = performance.now()
  const matrix = new THREE.Matrix4()
  const scale = new THREE.Vector3()
  const identityQuat = new THREE.Quaternion()
  let frame = 0
  let running = true

  const targetFor = (p) => {
    let a = KEYFRAMES[0]
    let b = KEYFRAMES[KEYFRAMES.length - 1]
    for (let i = 0; i < KEYFRAMES.length - 1; i++) {
      if (p >= KEYFRAMES[i].p && p <= KEYFRAMES[i + 1].p) {
        a = KEYFRAMES[i]
        b = KEYFRAMES[i + 1]
        break
      }
    }
    const t = smooth(Math.min(1, Math.max(0, (p - a.p) / (b.p - a.p || 1))))
    return {
      pos: new THREE.Vector3(...a.pos).lerp(new THREE.Vector3(...b.pos), t),
      look: new THREE.Vector3(...a.look).lerp(new THREE.Vector3(...b.look), t),
    }
  }

  let viewShift = { x: NaN, y: NaN }
  const resize = () => {
    const { clientWidth: w, clientHeight: h } = canvas
    if (!w || !h) return
    renderer.setSize(w, h, false)
    camera.aspect = w / h
    // Pull back on narrow screens so the graph still fits.
    camera.fov = w < 700 ? 62 : 48
    viewShift = { x: NaN, y: NaN }
    camera.updateProjectionMatrix()
  }

  // Slide the rendered view so the graph sits in the empty part of the
  // layout: right of the hero copy, left of the story cards, and above the
  // copy on phones. An off-center projection, not a camera move, so the
  // perspective itself doesn't change.
  const layoutShift = (p) => {
    const { clientWidth: w } = canvas
    if (w < 760) return { x: 0, y: 0.2 }
    const hero = -0.24
    const story = 0.2
    let x = story
    if (p < 0.2) x = hero + (story - hero) * smooth(p / 0.2)
    else if (p > 0.92) x = story * (1 - smooth((p - 0.92) / 0.08))
    return { x, y: 0 }
  }
  resize()
  const observer = new ResizeObserver(resize)
  observer.observe(canvas)

  const render = (now) => {
    if (!running) return
    frame = requestAnimationFrame(render)
    const elapsed = now - startTime
    const grow = growDuration ? Math.min(1, elapsed / growDuration) : 1
    const revealOrder = smooth(grow) * (MAIN_COMMITS + 4)

    // Tubes draw along their length as history "grows" on load.
    tubes.forEach((t) => {
      const span = Math.max(1, t.endOrder - t.startOrder)
      const f = Math.min(1, Math.max(0, (revealOrder - t.startOrder) / span))
      t.geometry.setDrawRange(0, Math.floor(t.geometry.index.count * f))
      const lit = t.stage === -1 ? 0.35 : t.stage === 0 || t.stage === stage ? 1 : 0.4
      t.material.opacity += (lit - t.material.opacity) * 0.08
    })

    commits.forEach((c, i) => {
      const f = Math.min(1, Math.max(0, revealOrder - c.order))
      const emphasis = c.stage === stage && stage > 0 ? 1.35 : 1
      scale.setScalar(smooth(f) * emphasis)
      matrix.compose(c.position, identityQuat, scale)
      nodes.setMatrixAt(i, matrix)
    })
    nodes.instanceMatrix.needsUpdate = true
    glowMaterial.opacity = 0.85 * smooth(grow)

    // Camera eases toward the scroll position; the pointer adds a little parallax.
    const target = targetFor(progress)
    const idle = reducedMotion ? 0 : Math.sin(elapsed / 2600) * 0.35
    target.pos.x += pointer.x * 0.9 + idle
    target.pos.y += pointer.y * 0.6
    const ease = reducedMotion ? 1 : 0.06
    camPos.lerp(target.pos, ease)
    camLook.lerp(target.look, ease)
    camera.position.copy(camPos)
    camera.lookAt(camLook)

    const shift = layoutShift(progress)
    const sx = reducedMotion ? shift.x : viewShift.x + (shift.x - viewShift.x) * 0.08 || shift.x
    const sy = reducedMotion ? shift.y : viewShift.y + (shift.y - viewShift.y) * 0.08 || shift.y
    if (Math.abs(sx - viewShift.x) > 1e-4 || Math.abs(sy - viewShift.y) > 1e-4 || Number.isNaN(viewShift.x)) {
      viewShift = { x: sx, y: sy }
      const { clientWidth: w, clientHeight: h } = canvas
      // A positive x offset renders a window further right, moving content left.
      camera.setViewOffset(w, h, sx * w, sy * h, w, h)
    }

    if (!reducedMotion) dust.rotation.z = elapsed / 90000
    renderer.render(scene, camera)
  }
  frame = requestAnimationFrame(render)

  const onVisibility = () => {
    if (document.hidden) {
      running = false
      cancelAnimationFrame(frame)
    } else if (!running) {
      running = true
      frame = requestAnimationFrame(render)
    }
  }
  document.addEventListener('visibilitychange', onVisibility)

  return {
    setProgress(p) {
      progress = Math.min(1, Math.max(0, p))
    },
    setStage(i) {
      stage = i
    },
    setPointer(x, y) {
      pointer.x = x
      pointer.y = y
    },
    dispose() {
      running = false
      cancelAnimationFrame(frame)
      observer.disconnect()
      document.removeEventListener('visibilitychange', onVisibility)
      disposables.forEach((d) => d.dispose())
      nodes.dispose()
      renderer.dispose()
    },
  }
}
