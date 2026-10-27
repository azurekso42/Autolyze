
  // ── Backend URL ──────────────────────────────────────────────────────────
  // To use from another machine, run:
  //   cloudflared tunnel --url http://localhost:8000
  // Then replace the empty string below with the tunnel URL, e.g.:
  //   const API_BASE = 'https://something.trycloudflare.com';
  // Leave empty to use localhost (only works on the server machine itself).
  const API_BASE = ''; // relative URLs - frontend served by FastAPI

/* ═══════════════════════════════════════════════════════
   AUTH GATE
   Session persisted via localStorage key "autolyze_token" and an HttpOnly refresh cookie
═══════════════════════════════════════════════════════ */
// Credentials moved to backend - do not store here
const _authed = !!localStorage.getItem('autolyze_token');
function _getStoredAccessToken() {
  return localStorage.getItem('autolyze_token');
}

function _storeSession(data) {
  if (data && data.access_token) {
    localStorage.setItem('autolyze_token', data.access_token);
  } else {
    localStorage.removeItem('autolyze_token');
  }

  if (data && data.email) {
    localStorage.setItem('autolyze_email', data.email);
  }

  localStorage.removeItem('autolyze_logged_out');
}

function _clearSession() {
  localStorage.removeItem('autolyze_token');
  localStorage.removeItem('autolyze_email');
  localStorage.removeItem('autolyze_refresh_token');
}

// ── Auth header helper - injected into every protected API call ───────────────
function _authHeaders() {
  const token = _getStoredAccessToken();
  return token ? { 'Authorization': 'Bearer ' + token } : {};
}

let _refreshInFlight = null;

function _setLoginLockedState(message) {
  const overlay = document.getElementById('login-overlay');
  const errEl = document.getElementById('login-error');
  if (overlay) overlay.classList.remove('hidden');
  document.body.classList.add('login-locked');
  document.documentElement.classList.add('login-locked');
  _enableScrollBlock();
  if (message && errEl) {
    errEl.textContent = message;
    errEl.classList.remove('show');
    void errEl.offsetWidth;
    errEl.classList.add('show');
  }
}

async function _refreshSession({ quiet = false } = {}) {
  if (_refreshInFlight) return _refreshInFlight;

  _refreshInFlight = (async () => {
    try {
      const response = await fetch('/auth/refresh', {
        method: 'POST',
        credentials: 'include',
      });

      if (!response.ok) {
        if (response.status === 401) {
          _clearSession();
          if (!quiet) {
            _setLoginLockedState('Your session expired. Please sign in again.');
          }
        }
        return false;
      }

      const data = await response.json();
      _storeSession(data);
      return true;
    } catch (_) {
      return false;
    } finally {
      _refreshInFlight = null;
    }
  })();

  return _refreshInFlight;
}

async function _authFetch(input, init = {}) {
  const headers = { ...(init.headers || {}), ..._authHeaders() };
  let response = await fetch(input, { ...init, credentials: 'include', headers });

  if (response.status !== 401) {
    return response;
  }

  const refreshed = await _refreshSession();
  if (!refreshed) {
    return response;
  }

  const retryHeaders = { ...(init.headers || {}), ..._authHeaders() };
  return fetch(input, { ...init, credentials: 'include', headers: retryHeaders });
}

async function _bootstrapAuth() {
  const hadStoredAccessToken = _authed;

  if (localStorage.getItem('autolyze_logged_out') === '1' && !_authed) {
    return;
  }

  if (!_authed) {
    _enableScrollBlock();
    const refreshed = await _refreshSession({ quiet: true });
    if (!refreshed) return;
  }

  const response = await _authFetch('/api/v1/auth/verify');
  if (response.ok) {
    const _lo = document.getElementById('login-overlay');
    if (_lo) _lo.classList.add('hidden');
    _forcePageScrollUnlocked();
    _disableScrollBlock();
    tl.play();
    return;
  }

  if (response.status === 401) {
    _clearSession();
    _setLoginLockedState('Your session expired. Please sign in again.');
    return;
  }

  if (hadStoredAccessToken) {
    _setLoginLockedState('Unable to verify your session right now.');
  }
}

_bootstrapAuth().catch(() => {
  if (_authed) {
    _setLoginLockedState('Unable to verify your session right now.');
  }
});

// Wire up interactions after DOM is ready
// ── Scroll blocker - active until user logs in ──────────────────────────────
function _blockScroll(e) { e.preventDefault(); e.stopPropagation(); return false; }
function _blockKeys(e) {
  const blocked = ['ArrowDown','ArrowUp','PageDown','PageUp','Home','End',' '];
  if (blocked.includes(e.key)) { e.preventDefault(); }
}
let _scrollBlockEnabled = false;
function _enableScrollBlock() {
  if (_scrollBlockEnabled) return;
  _scrollBlockEnabled = true;
  window.addEventListener('wheel',      _blockScroll, { passive: false });
  window.addEventListener('touchmove',  _blockScroll, { passive: false });
  window.addEventListener('keydown', _blockKeys, { passive: false });
  document.documentElement.style.overflow = 'hidden';
  document.body.style.overflow = 'hidden';
}
function _forcePageScrollUnlocked() {
  document.body.classList.remove('login-locked');
  document.documentElement.classList.remove('login-locked');
  document.documentElement.style.overflow = '';
  document.documentElement.style.position = '';
  document.documentElement.style.height = '';
  document.documentElement.style.top = '';
  document.documentElement.style.width = '';
  document.body.style.overflow = '';
  document.body.style.position = '';
  document.body.style.height = '';
  document.body.style.top = '';
  document.body.style.width = '';
}
function _disableScrollBlock() {
  if (!_scrollBlockEnabled) return;
  _scrollBlockEnabled = false;
  window.removeEventListener('wheel',     _blockScroll);
  window.removeEventListener('touchmove', _blockScroll);
  window.removeEventListener('keydown', _blockKeys);
  _forcePageScrollUnlocked();
}

// Block immediately if not authenticated
if (!_authed) { _enableScrollBlock(); }

document.addEventListener('DOMContentLoaded', () => {
  const btn = document.getElementById('login-btn');
  const inp = document.getElementById('login-email');
  const pwd = document.getElementById('login-password');

  if (btn) btn.addEventListener('click', _doLogin);
  if (inp) inp.addEventListener('keydown', e => { if (e.key === 'Enter') _doLogin(); });
  if (pwd) pwd.addEventListener('keydown', e => { if (e.key === 'Enter') _doLogin(); });
});

async function _doLogin() {
  const email   = (document.getElementById('login-email').value || '').trim().toLowerCase();
  const pass    = (document.getElementById('login-password').value || '');
  const errEl   = document.getElementById('login-error');
  const overlay = document.getElementById('login-overlay');
  const btn     = document.getElementById('login-btn');

  if (!email || !pass) {
    errEl.textContent = 'Please enter your email and password.';
    errEl.classList.remove('show'); void errEl.offsetWidth; errEl.classList.add('show');
    return;
  }

  // Show loading state
  btn.textContent = 'Verifying...';
  btn.disabled = true;

  try {
    const response = await fetch('/login', {
      method: 'POST',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email, password: pass }),
    });

    if (response.ok) {
      const data = await response.json();
      // Store tokens - never store plaintext password
      _storeSession(data);
      errEl.classList.remove('show');

      // Fade out login overlay, then release the cinematic intro
      gsap.to('#login-overlay', {
        opacity: 0,
        duration: 0.85,
        ease: 'power2.inOut',
        onComplete: () => {
          overlay.classList.add('hidden');
          _disableScrollBlock();   // also removes the wheel/touch/key blockers, not just the CSS lock
          tl.play();
        }
      });
    } else {
      // Wrong credentials
      errEl.textContent = 'Invalid credentials';
      errEl.classList.remove('show');
      void errEl.offsetWidth;
      errEl.classList.add('show');
      gsap.fromTo('#login-card',
        { x: -12 },
        { x: 0, duration: 0.5, ease: 'elastic.out(1, 0.3)' }
      );
    }
  } catch (err) {
    errEl.textContent = 'Cannot reach server. Is the backend running?';
    errEl.classList.remove('show'); void errEl.offsetWidth; errEl.classList.add('show');
    console.error('Login error:', err);
  } finally {
    btn.textContent = 'Access Platform';
    btn.disabled = false;
  }
}

/* ═══════════════════════════════════════════════════════
   PERFORMANCE DETECTION
   Detects low-end devices using CPU cores, DPR, motion
   preference, AND a lightweight GPU benchmark (WebGL
   renderer string). Office laptops with integrated Intel
   graphics are caught by the renderer check even when
   they have 8+ cores.
═══════════════════════════════════════════════════════ */
const PERF_MODE = (() => {
  const cores  = navigator.hardwareConcurrency || 4;
  const dpr    = window.devicePixelRatio       || 1;
  const mobile = /Mobi|Android/i.test(navigator.userAgent);
  const prefersReduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  if (prefersReduced) return true;
  if (mobile && dpr <= 1.5) return true;
  if (cores <= 4) return true;
  // GPU heuristic - integrated Intel/ARM graphics → perf mode
  try {
    const gl = document.createElement('canvas').getContext('webgl');
    if (gl) {
      const dbg = gl.getExtension('WEBGL_debug_renderer_info');
      if (dbg) {
        const renderer = gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL).toLowerCase();
        if (/intel|llvmpipe|swiftshader|microsoft basic|apple m[12]/.test(renderer)) return true;
      }
    }
  } catch(_) {}
  return false;
})();
if (PERF_MODE) document.body.classList.add('perf-mode');

/* ═══════════════════════════════════════════════════════
   UNIFIED rAF MASTER LOOP + TAB VISIBILITY PAUSE
   All background canvas systems register here instead
   of spawning their own requestAnimationFrame loops.
   When the tab is hidden every loop pauses automatically.
═══════════════════════════════════════════════════════ */
const _rafSystems = [];
let   _rafPaused  = false;
let   _rafId      = null;

function _masterFrame(ts) {
  _rafId = requestAnimationFrame(_masterFrame);
  if (_rafPaused) return;
  for (let i = 0; i < _rafSystems.length; i++) _rafSystems[i](ts);
}
_rafId = requestAnimationFrame(_masterFrame);

document.addEventListener('visibilitychange', () => {
  _rafPaused = document.hidden;
});

/* ═══════════════════════════════════════════════════════
   CURSOR - throttled via mousemove flag, no own rAF loop
═══════════════════════════════════════════════════════ */
const cur  = document.getElementById('cursor');
const curR = document.getElementById('cursor-ring');
let mx=0, my=0, rx=0, ry=0;
let _cursorDirty = false;

if (!PERF_MODE) {
  document.addEventListener('mousemove', e => {
    mx = e.clientX; my = e.clientY;
    // Dot follows instantly via style (no layout - already fixed+transformed)
    cur.style.left = mx + 'px';
    cur.style.top  = my + 'px';
    _cursorDirty = true;
  });

  // Ring lerp runs inside the master loop - no separate rAF
  _rafSystems.push(() => {
    if (!_cursorDirty && Math.abs(rx - mx) < 0.5 && Math.abs(ry - my) < 0.5) return;
    rx += (mx - rx) * .12;
    ry += (my - ry) * .12;
    curR.style.left = rx + 'px';
    curR.style.top  = ry + 'px';
    _cursorDirty = false;
  });

  document.querySelectorAll('#scroll-indicator').forEach(el => {
    el.addEventListener('mouseenter', () => {
      cur.style.transform = 'translate(-50%,-50%) scale(2.5)';
      curR.style.width = '56px'; curR.style.height = '56px';
      curR.style.borderColor = 'rgba(155,93,229,.8)';
    });
    el.addEventListener('mouseleave', () => {
      cur.style.transform = 'translate(-50%,-50%) scale(1)';
      curR.style.width = '36px'; curR.style.height = '36px';
      curR.style.borderColor = 'rgba(155,93,229,.5)';
    });
  });
}

/* ═══════════════════════════════════════════════════════
   NEBULA CANVAS - skipped entirely in perf mode
   Blob positions update every 3rd frame to cut CPU cost.
═══════════════════════════════════════════════════════ */
if (!PERF_MODE) {
  (function() {
    const c   = document.getElementById('nebula-canvas');
    const ctx = c.getContext('2d');
    let W = 0, H = 0;

    function resize() {
      // Only resize if dimensions actually changed
      if (c.width === innerWidth && c.height === innerHeight) return;
      W = c.width  = innerWidth;
      H = c.height = innerHeight;
    }
    resize();
    window.addEventListener('resize', resize, { passive: true });

    const blobs = Array.from({length: 5}, () => ({   // 7→5 blobs
      x: Math.random(), y: Math.random(),
      r: .18 + Math.random() * .22,
      hue: Math.random() < .5 ? 270 : 210,
      vx: (Math.random()-.5) * .00006,
      vy: (Math.random()-.5) * .00004,
      alpha: .04 + Math.random() * .07,
    }));

    let frame = 0;
    _rafSystems.push(() => {
      frame++;
      if (frame % 3 !== 0) return;   // update every 3rd frame ~20fps
      ctx.clearRect(0, 0, W, H);
      blobs.forEach(b => {
        b.x += b.vx; b.y += b.vy;
        if (b.x < -.3) b.x = 1.3; if (b.x > 1.3) b.x = -.3;
        if (b.y < -.3) b.y = 1.3; if (b.y > 1.3) b.y = -.3;
        const grd = ctx.createRadialGradient(
          b.x*W, b.y*H, 0, b.x*W, b.y*H, b.r*Math.max(W,H)
        );
        grd.addColorStop(0, `hsla(${b.hue},75%,38%,${b.alpha})`);
        grd.addColorStop(1, 'transparent');
        ctx.fillStyle = grd;
        ctx.fillRect(0, 0, W, H);
      });
    });
  })();
}

/* ═══════════════════════════════════════════════════════
   AMBIENT FLOATING PARTICLES - reduced count, fixed resize
   BUG FIX: original loop reset c.width every frame which
   clears canvas AND triggers layout. Now resize only on
   actual window resize event.
═══════════════════════════════════════════════════════ */
(function() {
  const c   = document.getElementById('ambient-particles');
  const ctx = c.getContext('2d');
  let W = innerWidth, H = innerHeight;

  function resize() {
    if (c.width === innerWidth && c.height === innerHeight) return;
    W = c.width  = innerWidth;
    H = c.height = innerHeight;
  }
  resize();
  window.addEventListener('resize', resize, { passive: true });

  // 220 stars → 80 in perf mode, 140 normal
  const COUNT = PERF_MODE ? 80 : 140;
  const STARS = Array.from({length: COUNT}, () => ({
    x: Math.random() * innerWidth,
    y: Math.random() * innerHeight,
    r: Math.random() * 1.0 + 0.5,
    baseAlpha: Math.random() * 0.45 + 0.2,
    twinkleSpeed: Math.random() * 1.2 + 0.4,
    twinklePhase: Math.random() * Math.PI * 2,
    vy: Math.random() * 0.1 + 0.02,
  }));

  let t = 0;
  // In perf mode update every 2nd frame (~30fps) to halve CPU
  let frame = 0;
  _rafSystems.push(() => {
    frame++;
    if (PERF_MODE && frame % 2 !== 0) return;
    t += 0.01;
    ctx.clearRect(0, 0, W, H);
    STARS.forEach(s => {
      s.y -= s.vy;
      if (s.y < -4) { s.y = H + 4; s.x = Math.random() * W; }
      const alpha = s.baseAlpha * (0.45 + 0.55 * Math.sin(t * s.twinkleSpeed + s.twinklePhase));
      // Use fillRect instead of arc+radialGradient for half the stars in perf mode
      if (PERF_MODE) {
        ctx.globalAlpha = alpha;
        ctx.fillStyle = 'rgba(210,190,255,1)';
        ctx.fillRect(s.x - s.r, s.y - s.r, s.r * 2, s.r * 2);
        ctx.globalAlpha = 1;
      } else {
        const grd = ctx.createRadialGradient(s.x, s.y, 0, s.x, s.y, s.r * 2.8);
        grd.addColorStop(0,    `rgba(230, 210, 255, ${alpha})`);
        grd.addColorStop(0.45, `rgba(200, 180, 255, ${alpha * 0.45})`);
        grd.addColorStop(1,    `rgba(180, 160, 255, 0)`);
        ctx.beginPath();
        ctx.arc(s.x, s.y, s.r * 2.8, 0, Math.PI * 2);
        ctx.fillStyle = grd;
        ctx.fill();
      }
    });
  });
})();

/* ═══════════════════════════════════════════════════════
   THREE.JS GLOBE - optimized
   • DPR capped at 1.5 (was unlimited - kills integrated GPU)
   • Particle count halved in perf mode
   • Uses master rAF loop - pauses when tab hidden
   • Three.js objects disposed on page unload
═══════════════════════════════════════════════════════ */
let globeScene, globeCamera, globeRenderer, globeMesh;
let globeParticles = [], orbitalRings = [];
let targetRotX = 0, targetRotY = 0;
let mouseNX = 0, mouseNY = 0; // normalized -1..1
let scrollBoost = 1.0; // declared here so initGlobe IIFE can access it
let bootBoost   = 0.1; // starts near-zero; ramped up by introTL boot sequence
let transitionBoost = 0.3; // starts slow on boot, spikes during the move
let mouseBoost = 0.3; // starts at idle
let cursorBallBoost = 1.0; // extra boost applied only to orbiting balls on mouse move
let _cursorVel = 0; // smoothed cursor velocity - carries momentum

(function initGlobe() {
  const canvas = document.getElementById('globe-canvas');
  const W = Math.min(560, window.innerWidth * 0.88);
  const H = W;

  globeScene = new THREE.Scene();
  globeCamera = new THREE.PerspectiveCamera(50, 1, .1, 1000);
  globeCamera.position.z = 4.2;

  globeRenderer = new THREE.WebGLRenderer({ canvas, alpha: true, antialias: !PERF_MODE });
  // Cap DPR at 1.5 - retina beyond 1.5x wastes GPU on no visible gain
  globeRenderer.setPixelRatio(Math.min(window.devicePixelRatio, PERF_MODE ? 1 : 1.5));
  globeRenderer.setSize(W, H);
  globeRenderer.setClearColor(0x000000, 0);

  /* Wireframe globe */
  const geo = new THREE.IcosahedronGeometry(1, 4);
  const mat = new THREE.MeshStandardMaterial({
    color: 0x7c3aed, emissive: 0x4c1d95, emissiveIntensity: .6,
    wireframe: true, transparent: true, opacity: .6,
  });
  globeMesh = new THREE.Mesh(geo, mat);
  globeScene.add(globeMesh);

  /* Inner glow sphere */
  const innerMat = new THREE.MeshStandardMaterial({
    color: 0x1e0050, emissive: 0x3b0088, emissiveIntensity: .5,
    transparent: true, opacity: .5,
  });
  globeScene.add(new THREE.Mesh(new THREE.SphereGeometry(.92, 32, 32), innerMat));

  /* Orbital rings */
  function makeRing(radius, color, op) {
    const m = new THREE.MeshBasicMaterial({ color, transparent: true, opacity: op });
    return new THREE.Mesh(new THREE.TorusGeometry(radius, .005, 12, 100), m);
  }
  const r1 = makeRing(1.2, 0x9b5de5, .65); r1.rotation.x = Math.PI/2; globeScene.add(r1);
  const r2 = makeRing(1.38, 0x3b82f6, .35); r2.rotation.x = Math.PI/3; r2.rotation.z = Math.PI/5; globeScene.add(r2);
  orbitalRings.push(r1, r2);


  /* Orbiting particles - halved in perf mode */
  const orbitColors = [0x9b5de5, 0x3b82f6, 0x06b6d4, 0xc4b5fd, 0x7c3aed];
  const ORBIT_COUNT = PERF_MODE ? 12 : 26;
  for(let i=0; i<ORBIT_COUNT; i++) {
    const g2 = new THREE.SphereGeometry(.02 + Math.random()*.025, 6, 6);
    const m2 = new THREE.MeshBasicMaterial({ color: orbitColors[i % 5], transparent: true, opacity: .9 });
    const p = new THREE.Mesh(g2, m2);
    p._orbitR = 1.05 + Math.random()*.55;
    p._phase  = (i/26) * Math.PI*2;
    p._speed  = (.4+Math.random()*.6) * (Math.random()>.5?1:-1);
    p._tilt   = (Math.random()-.5)*Math.PI*.6;
    globeScene.add(p); globeParticles.push(p);
  }

  /* Round star point cloud - soft circle sprite, rendered BEHIND globe */
  const sc = document.createElement('canvas'); sc.width=64; sc.height=64;
  const sx = sc.getContext('2d');
  const sg = sx.createRadialGradient(32,32,0,32,32,32);
  sg.addColorStop(0,   'rgba(255,255,255,1)');
  sg.addColorStop(0.35,'rgba(210,190,255,0.9)');
  sg.addColorStop(0.7, 'rgba(180,160,255,0.4)');
  sg.addColorStop(1,   'rgba(180,160,255,0)');
  sx.fillStyle = sg; sx.fillRect(0,0,64,64);
  const starTex = new THREE.CanvasTexture(sc);

  const pcCount = PERF_MODE ? 300 : 700;  // halved in perf mode
  const pcPos = [];
  for(let i=0; i<pcCount; i++) {
    const r = 1.55 + Math.random()*0.5;
    const th = Math.random()*Math.PI*2;
    const ph = Math.acos(2*Math.random()-1);
    pcPos.push(r*Math.sin(ph)*Math.cos(th), r*Math.sin(ph)*Math.sin(th), r*Math.cos(ph));
  }
  const pcGeo = new THREE.BufferGeometry();
  pcGeo.setAttribute('position', new THREE.Float32BufferAttribute(pcPos, 3));
  const pcMat = new THREE.PointsMaterial({
    map: starTex, size: .055, transparent: true, opacity: .8,
    alphaTest: 0.05, depthWrite: false, depthTest: false,
    blending: THREE.AdditiveBlending,
  });
  const pcPoints = new THREE.Points(pcGeo, pcMat);
  pcPoints.renderOrder = -1; // draw behind everything else
  globeScene.add(pcPoints);

  /* Lighting */
  globeScene.add(new THREE.AmbientLight(0x2a0060, 3));
  const pl = new THREE.PointLight(0x9b5de5, 5, 9); pl.position.set(2,2,2); globeScene.add(pl);
  const pl2 = new THREE.PointLight(0x3b82f6, 2.5, 8); pl2.position.set(-2,-1,1); globeScene.add(pl2);

  /* Animate - registered in master rAF loop, not its own */
  let t = 0;
  let orbitTime = 0;
  const _globeWrapEl = document.getElementById('globe-wrap');

  // Globe skips update every 2nd frame in perf mode (~30fps)
  let _globeFrame = 0;
  _rafSystems.push(function animateGlobe() {
    _globeFrame++;
    if (PERF_MODE && _globeFrame % 2 !== 0) return;
    if (typeof _globeVisible !== 'undefined' && !_globeVisible) return; // off-screen

    t += .004;

    scrollBoost      += (1.0 - scrollBoost)     * 0.04;
    transitionBoost  += (0.3 - transitionBoost)  * 0.018;
    mouseBoost       += (0.3 - mouseBoost)        * 0.025;

    const boost = scrollBoost * transitionBoost * mouseBoost * bootBoost;
    _cursorVel *= 0.97;
    cursorBallBoost = 1.0 + _cursorVel;
    orbitTime += 0.004 * boost * cursorBallBoost;

    globeMesh.rotation.y += .003 * boost;
    globeMesh.rotation.x += .0005 * boost;
    targetRotX += (mouseNY*.3 - targetRotX)*.04;
    targetRotY += (mouseNX*.3 - targetRotY)*.04;
    globeMesh.rotation.x += targetRotX*.008;
    globeMesh.rotation.y += targetRotY*.008;

    orbitalRings[0].rotation.y += .007 * boost;
    orbitalRings[1].rotation.y -= .005 * boost;
    orbitalRings[1].rotation.x += .003 * boost;

    pcPoints.rotation.y += .0006 * boost;
    pcPoints.rotation.x += .0002 * boost;
    pcMat.opacity = Math.min(0.95, (0.5 + 0.35 * Math.sin(t * 0.9)) * (0.8 + 0.2 * boost));

    globeParticles.forEach(p => {
      const a = p._phase + orbitTime * p._speed;
      p.position.set(p._orbitR*Math.cos(a), p._orbitR*Math.sin(a)*Math.sin(p._tilt), p._orbitR*Math.sin(a)*Math.cos(p._tilt));
    });

    globeRenderer.render(globeScene, globeCamera);
  });

  // Dispose Three.js resources on page unload to prevent memory leaks
  window.addEventListener('beforeunload', () => {
    globeRenderer.dispose();
    globeScene.traverse(obj => {
      if (obj.geometry) obj.geometry.dispose();
      if (obj.material) {
        if (Array.isArray(obj.material)) obj.material.forEach(m => m.dispose());
        else obj.material.dispose();
      }
    });
  });
})();

/* ═══════════════════════════════════════════════════════
   MOUSE PARALLAX
═══════════════════════════════════════════════════════ */
let globeTransitioned = false;
let _lastMX = 0, _lastMY = 0, _mouseIdleTimer = null;

document.addEventListener('mousemove', e => {
  mouseNX = (e.clientX / innerWidth  - .5) * 2;
  mouseNY = (e.clientY / innerHeight - .5) * 2;

  // velocity magnitude → nudge mouseBoost up
  const dx = e.clientX - _lastMX, dy = e.clientY - _lastMY;
  const vel = Math.sqrt(dx*dx + dy*dy);
  _lastMX = e.clientX; _lastMY = e.clientY;

  mouseBoost = Math.min(mouseBoost + vel * 0.0018, 1.0);
  // feed velocity into momentum tank - capped so it doesn't go crazy
  _cursorVel = Math.min(_cursorVel + vel * 0.008, 4.0);

  // reset idle timer - if cursor stops, decay back to 0.3
  clearTimeout(_mouseIdleTimer);
  _mouseIdleTimer = setTimeout(() => { /* decay handled in rAF */ }, 150);
});

/* ═══════════════════════════════════════════════════════
   CINEMATIC SEQUENCE  (GSAP)
═══════════════════════════════════════════════════════ */
gsap.registerPlugin(ScrollTrigger);

// Set initial centred position via GSAP so it owns the transform from frame 0
// Place welcome-block in viewport centre using pure x/y - no top/left conflict
// GSAP owns the centering - no CSS transform conflict
gsap.set('#welcome-block', { xPercent: -50, yPercent: -50 });

const tl = gsap.timeline({ paused: !_authed, defaults: { ease: 'power3.out' } });

// 0.0s – bg un-blur
tl.to('#galaxy-bg', { filter:'blur(0px)', duration:1.2 }, 0);

// 0.9s – welcome fades in at true screen centre
gsap.set('#welcome-text', { opacity: 0 });
tl.to('#welcome-text', { opacity:1, duration: .8, ease: 'power2.out', onComplete: () => {
  const svgText = document.getElementById('welcome-svg-text');
  svgText.style.animation = 'welcome-draw 2s cubic-bezier(0.4,0,0.2,1) forwards';
  setTimeout(() => svgText.classList.add('fill-in'), 2000);
}}, .9);

// 2.4s – globe flies right, glow follows; Welcome drifts to left-centre simultaneously
tl.to('#globe-wrap', {
  top: '38%',
  left: '63%',
  xPercent: -50,
  yPercent: -50,
  width: '340px',
  height: '340px',
  duration: 1.3,
  ease: 'expo.inOut',
  onStart: () => {
    globeTransitioned = true;
    transitionBoost = 3.5;
    const trail = document.getElementById('globe-trail');
    const wrap  = document.getElementById('globe-wrap');
    let tracking = true;
    let fadeIn = 0;

    // Register in master loop - no separate rAF
    const trailFn = () => {
      if (!tracking) {
        const idx = _rafSystems.indexOf(trailFn);
        if (idx !== -1) _rafSystems.splice(idx, 1);
        return;
      }
      fadeIn = Math.min(fadeIn + 0.06, 1);
      const r = wrap.getBoundingClientRect();
      trail.style.width   = (r.width  + 160) + 'px';
      trail.style.height  = (r.height + 160) + 'px';
      trail.style.left    = (r.left   -  80) + 'px';
      trail.style.top     = (r.top    -  80) + 'px';
      trail.style.opacity = fadeIn;
    };
    _rafSystems.push(trailFn);

    setTimeout(() => {
      tracking = false;
      trail.style.transition = 'opacity 1s ease';
      trail.style.opacity    = '0';
    }, 1350);
  }
}, 2.4);

// Welcome drifts left-centre (left ~22%, stays vertically centred)
tl.to('#welcome-block', {
  top: '50%',
  left: '22%',
  xPercent: -50,
  yPercent: -50,
  textAlign: 'left',
  duration: 1.2,
  ease: 'power2.inOut',
  onStart: () => {
    const svg = document.getElementById('welcome-svg-text');
    svg.setAttribute('x', '0');
    svg.setAttribute('text-anchor', 'start');
  }
}, 2.4);

// Ekso fades in below Welcome (block already has it stacked)
tl.to('#username-text', { opacity:1, duration:.7, ease:'power2.out' }, 3.7);

// desc fades in under Ekso
tl.to('#desc-text', { opacity:1, duration:.8, ease:'power2.out' }, 4.2);

// scroll indicator appears once the intro settles
tl.to('#scroll-indicator', { opacity:1, duration:.6, ease:'power2.out' }, 4.7);

// hide loader
tl.set('#loader', { display:'none' }, 0);

// Release will-change after intro - keeps GPU layers only while needed
tl.call(() => {
  ['#welcome-text','#username-text','#desc-text'].forEach(sel => {
    const el = document.querySelector(sel);
    if (el) el.style.willChange = 'auto';
  });
}, [], 5.5);

/* ═══════════════════════════════════════════════════════
   SCROLL VELOCITY → GLOBE BOOST  (throttled)
═══════════════════════════════════════════════════════ */
let lastScrollY = 0, scrollVelTimer = null, _lastScrollTime = 0;
window.addEventListener('scroll', () => {
  // Throttle to max 30 updates/sec - scroll fires every pixel otherwise
  const now = Date.now();
  if (now - _lastScrollTime < 33) return;
  _lastScrollTime = now;

  const delta = Math.abs(window.scrollY - lastScrollY);
  lastScrollY = window.scrollY;
  scrollBoost = Math.min(scrollBoost + delta * 0.012, 3.2);
  document.body.classList.add('is-scrolling');
  clearTimeout(scrollVelTimer);
  scrollVelTimer = setTimeout(() => {
    document.body.classList.remove('is-scrolling');
  }, 180);
}, { passive: true });

/* ═══════════════════════════════════════════════════════
   SCROLL → CARDS
═══════════════════════════════════════════════════════ */
// Fade out welcome block when scrolling away - guarded until intro is done
let introComplete = false;
tl.call(() => { introComplete = true; }, [], 5.2); // just after last fade-in

let _wbScrollTime = 0;
window.addEventListener('scroll', () => {
  if (!introComplete) return;
  const now = Date.now();
  if (now - _wbScrollTime < 33) return;
  _wbScrollTime = now;
  const progress = Math.min(window.scrollY / 300, 1);
  document.getElementById('welcome-block').style.opacity = 1 - progress;
  document.getElementById('scroll-indicator').style.opacity = 1 - progress * 2;
  // Fade the Data Analysis section in once the user starts scrolling down
  if (!sectionOpen && window.scrollY > 60) {
    sectionOpen = true;
    document.getElementById('da-section').classList.add('active');
    _dockGlobe(); // same corner dock whether via arrow click or manual scroll
  }
});

// ── Section open helper ─────────────────────────────
let sectionOpen = false;

function openSection(id) {
  _forcePageScrollUnlocked();
  sectionOpen = true;
  const s = document.getElementById(id);
  s.classList.add('active');
  setTimeout(() => {
    const pt = parseFloat(window.getComputedStyle(s).paddingTop) || 0;
    window.scrollTo({ top: s.offsetTop + pt - 24, behavior: 'smooth' });
  }, 50);
}

async function logout() {
  try {
    await fetch('/auth/logout', { method: 'POST', credentials: 'include' });
  } catch (_) {}
  localStorage.setItem('autolyze_logged_out', '1');
  _clearSession();
  location.reload();
}

/* ═══════════════════════════════════════════════════════
   DATA ANALYSIS WORKFLOW
═══════════════════════════════════════════════════════ */

// ── Visibility ──────────────────────────────────────
let _globeDocked = false;

// Move + shrink the globe into the top-right corner.
// Strategy: freeze the globe's current on-screen position as plain
// top/right pixels (no transforms at all), then tween right->30, top->10.
// A fixed element anchored with `right` is pinned to the viewport's right
// edge by the browser itself - it cannot drift off-screen and it tracks
// window resizes natively.
function _dockGlobe() {
  if (_globeDocked) return;
  _globeDocked = true;
  gsap.killTweensOf('#globe-wrap');
  const el = document.getElementById('globe-wrap');
  const r  = el.getBoundingClientRect();
  // Zero the transform INLINE - clearProps would let the stylesheet's
  // translate(-50%,-50%) rule re-apply and shift the globe off-target.
  gsap.set(el, { x: 0, y: 0, xPercent: 0, yPercent: 0 });
  el.style.left  = 'auto';
  el.style.top   = r.top + 'px';
  el.style.right = (document.documentElement.clientWidth - r.right) + 'px';
  gsap.to(el, {
    right: 90, top: 10, width: 140, height: 140,
    duration: 1.1,
    ease: 'expo.inOut',
    onComplete: () => {
      const d = el.getBoundingClientRect();
      console.log('[autolyze] globe docked v3:',
        Math.round(d.left), Math.round(d.top), Math.round(d.right),
        'viewport', document.documentElement.clientWidth);
    },
  });
}

function scrollToDataAnalysis() {
  openSection('da-section');
  _dockGlobe(); // globe flies to the top-right corner as the page scrolls
}

// ── Drop zone listeners are set up lazily in initDataAnalysis() ─────────────

function daHandleFile(file) {
  const dz = document.getElementById('da-drop-zone');
  const ps = document.getElementById('da-processing');

  // Store file for API call
  window._daCurrentFile = file;
  window._daContext = '';

  setGlobeState('thinking');
  // State change happens immediately - never inside an animation callback.
  // rAF/GSAP are throttled to zero in background tabs, so an onComplete
  // there can stall the upload indefinitely. Animation is decoration only.
  dz.style.display = 'none';
  ps.style.display = 'flex';
  void ps.offsetWidth;            // flush so the .visible transition runs
  ps.classList.add('visible');
  daStartNeural();
  daCallBackend(file);
}

// ── Neural canvas animation ─────────────────────────
let daNeural = null;
function daStartNeural() {
  const canvas = document.getElementById('da-neural-canvas');
  const ctx = canvas.getContext('2d');
  function resize() {
    canvas.width  = canvas.offsetWidth;
    canvas.height = canvas.offsetHeight;
  }
  resize();

  // Fewer nodes in perf mode
  const NODE_COUNT = PERF_MODE ? 10 : 18;
  const nodes = Array.from({ length: NODE_COUNT }, () => ({
    x: Math.random() * canvas.width,
    y: Math.random() * canvas.height,
    vx: (Math.random() - .5) * .4,
    vy: (Math.random() - .5) * .4,
    r: 2 + Math.random() * 2,
    phase: Math.random() * Math.PI * 2,
  }));

  let running = true;
  let _neuralFrame = 0;

  // Stop function - called by daShowResults
  daNeural = () => { running = false; };

  // Register in master loop instead of own rAF
  const neuralFn = () => {
    if (!running) {
      // Remove self from master loop
      const idx = _rafSystems.indexOf(neuralFn);
      if (idx !== -1) _rafSystems.splice(idx, 1);
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      return;
    }
    // Throttle to ~30fps
    _neuralFrame++;
    if (_neuralFrame % 2 !== 0) return;

    ctx.clearRect(0, 0, canvas.width, canvas.height);

    nodes.forEach(n => {
      n.x += n.vx; n.y += n.vy;
      if (n.x < 0 || n.x > canvas.width)  n.vx *= -1;
      if (n.y < 0 || n.y > canvas.height) n.vy *= -1;
    });

    for (let i = 0; i < nodes.length; i++) {
      for (let j = i + 1; j < nodes.length; j++) {
        const dx = nodes[i].x - nodes[j].x, dy = nodes[i].y - nodes[j].y;
        const d = Math.sqrt(dx*dx + dy*dy);
        if (d < 120) {
          ctx.beginPath();
          ctx.moveTo(nodes[i].x, nodes[i].y);
          ctx.lineTo(nodes[j].x, nodes[j].y);
          ctx.strokeStyle = `rgba(155,93,229,${(1 - d/120) * 0.25})`;
          ctx.lineWidth = .8;
          ctx.stroke();
        }
      }
    }

    nodes.forEach(n => {
      const pulse = .5 + .5 * Math.sin(Date.now() * .002 + n.phase);
      const r = ctx.createRadialGradient(n.x, n.y, 0, n.x, n.y, n.r * 3);
      r.addColorStop(0,   `rgba(196,181,253,${.8 * pulse})`);
      r.addColorStop(1,   'rgba(155,93,229,0)');
      ctx.beginPath();
      ctx.arc(n.x, n.y, n.r * 3, 0, Math.PI * 2);
      ctx.fillStyle = r; ctx.fill();
      ctx.beginPath();
      ctx.arc(n.x, n.y, n.r, 0, Math.PI * 2);
      ctx.fillStyle = `rgba(220,200,255,${.9 * pulse})`; ctx.fill();
    });
  };
  _rafSystems.push(neuralFn);
}

// Total pipeline stages streamed by the backend - used to size the bar
const DA_TOTAL_STAGES = 7;

function daSetStatus(msg) {
  const el = document.getElementById('da-status-text');
  el.classList.add('out');
  setTimeout(() => { el.textContent = msg; el.classList.remove('out'); }, 360);
}

// Aborts the in-flight analysis. Closing the connection is also what lets the
// backend cancel its pipeline task - see the finally block in data_analyze.
let daAbortController = null;

function daCancelAnalysis() {
  if (daAbortController) daAbortController.abort();
  daResetAnalysis();
}

// ── AI availability dot (chat panel) ────────────────────────────────────────
// Shown as a dot; the wording lives in the hover tooltip so the panel title
// stays uncluttered. '' hides it entirely.
const DA_AI_TIPS = {
  connecting: 'Connecting to the local AI model...',
  on:  'AI online - insights and follow-up questions are available',
  off: 'AI offline - computed results are shown without interpretation',
};

function daSetAiStatus(state) {
  const dot = document.getElementById('da-ai-status');
  if (!dot) return;
  const tip = DA_AI_TIPS[state] || '';
  dot.className = state || '';
  dot.setAttribute('data-tip', tip);
  // The dot carries no text, so screen readers need the wording spelled out.
  dot.setAttribute('aria-label', tip);
  dot.setAttribute('role', 'status');
  dot.tabIndex = state ? 0 : -1;
}


// ── Real backend call - streams actual pipeline stages (NDJSON) ─────────────
async function daCallBackend(file) {
  const bar = document.getElementById('da-progress-bar');
  const cancelBtn = document.getElementById('da-cancel-btn');

  daSetAiStatus('connecting');
  daSetStatus('Uploading dataset...');
  bar.style.width = '5%';
  setGlobeState('processing');
  connectGlobeTo('da-section');

  daAbortController = new AbortController();
  if (cancelBtn) cancelBtn.style.display = 'inline-block';

  // Tracked outside the try so the error handler can tell "never connected"
  // apart from "connection dropped part-way through the analysis".
  let streamStarted = false, lastStage = '';

  try {
    const formData = new FormData();
    formData.append('file', file);

    const response = await _authFetch('/api/v1/data/analyze', {
      method: 'POST',
      headers: { ..._authHeaders() },
      body: formData,
      signal: daAbortController.signal,
    });

    if (!response.ok) {
      const err = await response.json().catch(() => ({ detail: response.statusText }));
      throw new Error(err.detail || `Server error ${response.status}`);
    }

    // Read the NDJSON stream: {"stage": ...} events, then the final result
    streamStarted = true;
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buf = '', final = null, stageNum = 0;

    const handleEvent = evt => {
      if (evt.stage) {
        stageNum++;
        lastStage = evt.stage;
        daSetStatus(evt.stage);
        bar.style.width = Math.min(5 + (stageNum / DA_TOTAL_STAGES) * 90, 95) + '%';
      } else {
        final = evt;
      }
    };

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let nl;
      while ((nl = buf.indexOf('\n')) >= 0) {
        const line = buf.slice(0, nl).trim();
        buf = buf.slice(nl + 1);
        if (line) handleEvent(JSON.parse(line));
      }
    }
    // Final event may arrive without a trailing newline (older backend or
    // proxies) - parse whatever remains in the buffer.
    if (buf.trim()) handleEvent(JSON.parse(buf.trim()));

    if (!final) throw new Error('Connection lost during analysis.');
    if (final.status !== 'success') throw new Error(final.detail || 'Analysis failed');

    bar.style.width = '100%';
    daSetStatus('complete!');
    setGlobeState('complete');
    disconnectBeam();
    // Demo insurance: survive an accidental refresh
    try { sessionStorage.setItem('autolyze_last_analysis', JSON.stringify(final.data)); } catch (_) {}
    setTimeout(() => { setGlobeState('idle'); daShowResults(final.data); }, 500);

  } catch (err) {
    // The user cancelled - daCancelAnalysis has already reset the UI, so
    // rendering an error box here would just undo that with a false alarm.
    if (err && err.name === 'AbortError') return;

    daSetStatus('error');
    // Never leave the dot pulsing on "connecting" after the run has failed.
    daSetAiStatus('');
    bar.style.width = '0%';
    console.error('Data analysis error:', err);

    const ps = document.getElementById('da-processing');
    const box = document.createElement('div');
    box.className = 'da-error-box';
    box.style.cssText = 'display:flex;flex-direction:column;align-items:center;gap:.7rem;margin-top:1rem;';

    const errEl = document.createElement('p');
    errEl.style.cssText = 'color:rgba(239,68,68,.9);font-size:.85rem;margin:0;text-align:center;';
    const isNetworkErr = err instanceof TypeError && /fetch|network/i.test(err.message);
    if (isNetworkErr && streamStarted) {
      // The request landed and the pipeline was running - the connection died
      // mid-stream. Usually a proxy (e.g. a Cloudflare tunnel) timing out, not
      // a backend that is down, so don't send the user chasing the wrong thing.
      errEl.textContent = lastStage
        ? `Connection lost during "${lastStage}". The analysis may still be running on the server - try again.`
        : 'Connection lost during analysis. Try again.';
    } else if (isNetworkErr) {
      errEl.textContent = 'Cannot reach the server - make sure the backend is running, then try again.';
    } else {
      errEl.textContent = 'Error: ' + err.message;
    }
    box.appendChild(errEl);

    const retryBtn = document.createElement('button');
    retryBtn.className = 'action-btn';
    retryBtn.style.cssText = 'font-size:.78rem;padding:.4rem 1.1rem;';
    retryBtn.textContent = 'Try Again';
    retryBtn.onclick = () => daResetAnalysis();
    box.appendChild(retryBtn);

    ps.appendChild(box);
  } finally {
    // Runs on the cancel path too - the early return above still hits this.
    if (cancelBtn) cancelBtn.style.display = 'none';
    daAbortController = null;
  }
}

// ── Render real results ──────────────────────────────
function daShowResults(data) {
  if (daNeural) { daNeural(); daNeural = null; }

  const ps = document.getElementById('da-processing');
  const rs = document.getElementById('da-results');
  const summary = data.summary;

  // Reveal happens immediately - never inside an animation callback.
  // GSAP/rAF are throttled to zero in background tabs, so the results
  // would otherwise stay hidden until the tab is refocused.
  ps.classList.remove('visible'); ps.style.display = 'none'; ps.style.opacity = '';
  {
    // Build preview table from real rows
    const thead = document.getElementById('da-thead');
    const tbody = document.getElementById('da-tbody');
    // Safe DOM builder - never inject backend data via innerHTML
    const htr = document.createElement('tr');
    summary.column_names.forEach(c => {
      const th = document.createElement('th');
      th.textContent = c;
      htr.appendChild(th);
    });
    thead.innerHTML = '';
    thead.appendChild(htr);

    tbody.innerHTML = '';
    summary.preview_rows.forEach(row => {
      const tr = document.createElement('tr');
      row.forEach(cell => {
        const td = document.createElement('td');
        td.textContent = (cell !== null && cell !== undefined) ? String(cell) : '';
        tr.appendChild(td);
      });
      tbody.appendChild(tr);
    });

    // Build insights from LLM
    const list = document.getElementById('da-insights-list');
    const icons = ['📈','💡','📉','🔗','⚡'];
    // Safe DOM builder for insights - no innerHTML with LLM content
    list.innerHTML = '';
    data.insights.forEach((ins, i) => {
      const div = document.createElement('div');
      div.className = 'da-insight';
      const icon = document.createElement('span');
      icon.style.marginRight = '.5rem';
      icon.textContent = icons[i] || '•';
      const text = document.createElement('span');
      text.textContent = ins;
      div.appendChild(icon);
      div.appendChild(text);
      list.appendChild(div);
    });

    // Keep the full result for the Power BI export endpoint
    window._daResult = data;

    // AI availability dot on the chat panel
    daSetAiStatus(data.llm_available !== false ? 'on' : 'off');

    // Store context for chat (summary as text)
    window._daContext = [
      `File: ${data.filename}`,
      `Shape: ${summary.rows} rows x ${summary.columns} columns`,
      `Columns: ${summary.column_names.join(', ')}`,
      `Insights: ${data.insights.join(' | ')}`,
      summary.top_correlations.length
        ? `Top correlations: ${summary.top_correlations.map(c => `${c.col_a}<->${c.col_b} r=${(c.correlation||0).toFixed(3)}`).join(', ')}`
        : '',
    ].filter(Boolean).join('\n');

    rs.style.display = 'flex';
    void rs.offsetWidth;          // flush so the .visible transition runs
    rs.classList.add('visible');

    // Stagger insight cards in
    setTimeout(() => {
      document.querySelectorAll('.da-insight').forEach((el, i) => {
        setTimeout(() => el.classList.add('show'), i * 320);
      });
    }, 400);

    // Build charts from real numeric data
    setTimeout(() => daBuildCharts(summary), 500);

    // Computed ML results: model metrics + segments
    daBuildMlPanels(data.ml_results || {});

    // Show recommended questions as starter chips
    if (data.recommended_questions && data.recommended_questions.length) {
      const chatPanel = document.getElementById('da-chat-panel');
      const existingChips = document.getElementById('da-question-chips');
      if (!existingChips) {
        const chips = document.createElement('div');
        chips.id = 'da-question-chips';
        chips.style.cssText = 'display:flex;flex-wrap:wrap;gap:.5rem;margin-bottom:.8rem;';
        data.recommended_questions.forEach(q => {
          const chip = document.createElement('button');
          chip.className = 'action-btn';
          chip.style.cssText = 'font-size:.72rem;padding:.35rem .8rem;';
          chip.textContent = q;
          chip.onclick = () => {
            document.getElementById('da-chat-input').value = q;
            daChatSend();
          };
          chips.appendChild(chip);
        });
        const title = chatPanel.querySelector('.da-panel-title');
        title.after(chips);
      }
    }
  }
}

// ── Charts from real data ────────────────────────────
// ── Computed ML results panels (metrics table + segment bars) ──────────
function daMlSkipNote(part) {
  const d = document.createElement('div');
  d.className = 'da-ml-note';
  d.style.marginTop = '0';
  d.textContent = 'Not available for this dataset'
    + (part && part.skip_reason ? ` - ${part.skip_reason}` : '');
  return d;
}

function daBuildMlPanels(ml) {
  const row = document.getElementById('da-ml-row');
  const modelsBody = document.getElementById('da-models-body');
  const segBody = document.getElementById('da-segments-body');
  if (!row || !modelsBody || !segBody) return;
  modelsBody.innerHTML = '';
  segBody.innerHTML = '';

  const f = (ml && ml.forecasting) || {};
  const s = (ml && ml.segmentation) || {};
  const fcOk = !f.skipped && f.models;
  const segOk = !s.skipped && s.segments;
  if (!fcOk && !segOk) { row.style.display = 'none'; return; }
  row.style.display = 'grid';

  // ── Forecast model comparison ──
  if (fcOk) {
    const tbl = document.createElement('table');
    tbl.className = 'da-ml-table';
    const head = tbl.createTHead().insertRow();
    ['Model', 'RMSE', 'MAE', 'R²'].forEach(h => {
      const th = document.createElement('th');
      th.textContent = h;
      head.appendChild(th);
    });
    const tb = tbl.createTBody();
    Object.entries(f.models).forEach(([name, m]) => {
      const tr = tb.insertRow();
      if (name === f.best_model) tr.className = 'best';
      [name === f.best_model ? `${name} ★` : name, m.rmse, m.mae, m.r2]
        .forEach(v => { tr.insertCell().textContent = v; });
    });
    modelsBody.appendChild(tbl);

    const note = document.createElement('div');
    note.className = 'da-ml-note';
    note.textContent = `Target '${f.target_column}' · ${f.n_features} features · `
      + `${(f.train_rows || 0).toLocaleString()} train / ${(f.test_rows || 0).toLocaleString()} test rows (chronological split)`;
    modelsBody.appendChild(note);

    const imp = f.feature_importance || {};
    const impKeys = Object.keys(imp).slice(0, 5);
    if (impKeys.length) {
      const t = document.createElement('div');
      t.className = 'da-ml-note';
      t.textContent = 'Top predictive features (Random Forest importance):';
      modelsBody.appendChild(t);
      const maxV = Math.max(...impKeys.map(k => imp[k])) || 1;
      impKeys.forEach(k => {
        const r = document.createElement('div'); r.className = 'da-bar-row';
        const l = document.createElement('span'); l.className = 'da-bar-label'; l.textContent = k; l.title = k;
        const track = document.createElement('div'); track.className = 'da-bar-track';
        const fill = document.createElement('div'); fill.className = 'da-bar-fill';
        fill.style.width = (imp[k] / maxV * 100) + '%';
        track.appendChild(fill);
        const v = document.createElement('span'); v.className = 'da-bar-val'; v.textContent = imp[k];
        r.append(l, track, v);
        modelsBody.appendChild(r);
      });
    }
  } else {
    modelsBody.appendChild(daMlSkipNote(f));
  }

  // ── Segment sizes + profiles ──
  if (segOk) {
    const note = document.createElement('div');
    note.className = 'da-ml-note';
    note.style.marginTop = '0';
    note.textContent = `${s.method} · k=${s.k} · Silhouette ${s.silhouette} · `
      + `${(s.n_entities || 0).toLocaleString()} entities`;
    segBody.appendChild(note);

    const segs = s.segments || [];
    const maxSize = Math.max(...segs.map(x => x.size)) || 1;
    segs.forEach(x => {
      const r = document.createElement('div'); r.className = 'da-bar-row';
      const l = document.createElement('span'); l.className = 'da-bar-label';
      l.textContent = `Segment ${x.segment} (n=${(x.size || 0).toLocaleString()})`;
      const track = document.createElement('div'); track.className = 'da-bar-track';
      const fill = document.createElement('div'); fill.className = 'da-bar-fill';
      fill.style.width = (x.size / maxSize * 100) + '%';
      track.appendChild(fill);
      r.append(l, track);
      segBody.appendChild(r);
      const prof = document.createElement('div');
      prof.className = 'da-ml-note';
      prof.style.cssText = 'margin:.1rem 0 .6rem 34%;';
      prof.textContent = Object.entries(x.profile || {})
        .map(([k, v]) => `${k}: ${typeof v === 'number' ? v.toLocaleString() : v}`).join(' · ');
      segBody.appendChild(prof);
    });
  } else {
    segBody.appendChild(daMlSkipNote(s));
  }
}

// ── Reset back to the upload state for another dataset ─────────────────
function daResetAnalysis() {
  // Reset can be reached while a run is still in flight (e.g. "New Analysis"
  // after a partial render), so drop the connection here too. abort() is a
  // no-op if daCancelAnalysis already called it.
  if (daAbortController) daAbortController.abort();

  const cancelBtn = document.getElementById('da-cancel-btn');
  if (cancelBtn) cancelBtn.style.display = 'none';

  try { sessionStorage.removeItem('autolyze_last_analysis'); } catch (_) {}
  window._daResult = null;
  window._daContext = '';

  if (window.Chart) {
    ['da-chart-bar', 'da-chart-line', 'da-chart-doughnut'].forEach(id => {
      const c = Chart.getChart(id);
      if (c) c.destroy();
    });
  }

  const rs = document.getElementById('da-results');
  rs.classList.remove('visible');
  rs.style.display = 'none';
  document.getElementById('da-insights-list').innerHTML = '';
  document.getElementById('da-thead').innerHTML = '';
  document.getElementById('da-tbody').innerHTML = '';
  document.getElementById('da-models-body').innerHTML = '';
  document.getElementById('da-segments-body').innerHTML = '';
  document.getElementById('da-ml-row').style.display = 'none';
  const chips = document.getElementById('da-question-chips');
  if (chips) chips.remove();
  document.getElementById('da-chat-messages').innerHTML = '';
  daSetAiStatus('');

  const ps = document.getElementById('da-processing');
  ps.classList.remove('visible');
  ps.style.display = 'none';
  ps.querySelectorAll('p:not(#da-processing-sub), .da-error-box').forEach(el => el.remove());
  document.getElementById('da-progress-bar').style.width = '0%';
  daSetStatus('reading dataset...');

  document.getElementById('da-file-input').value = '';
  const dz = document.getElementById('da-drop-zone');
  dz.style.display = 'block';
  dz.style.opacity = '';
}

// ── Export computed results as an Excel workbook for Power BI ──────────
async function daExportPowerBI() {
  if (!window._daResult) return;
  const btn = document.getElementById('da-export-btn');
  const orig = btn.textContent;
  btn.disabled = true;
  btn.textContent = 'Exporting...';
  try {
    const res = await _authFetch(`${API_BASE}/api/v1/data/export`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(window._daResult),
    });
    if (!res.ok) throw new Error(`Export failed (${res.status})`);
    const blob = await res.blob();
    const disposition = res.headers.get('Content-Disposition') || '';
    const match = disposition.match(/filename="([^"]+)"/);
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = match ? match[1] : 'autolyze_powerbi.xlsx';
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(a.href);
    btn.textContent = 'Exported ✓';
    setTimeout(() => { btn.textContent = orig; btn.disabled = false; }, 2000);
  } catch (err) {
    console.error('Power BI export failed:', err);
    btn.textContent = 'Export failed - retry';
    btn.disabled = false;
  }
}

function daBuildCharts(summary) {
  if (window.Chart) {
    // Chart.js already loaded (pre-fetched by initDataAnalysis)
    daDrawCharts(summary);
  } else if (window._chartJsLoading) {
    // Pre-fetch is in flight - poll until it lands rather than adding a second tag
    const wait = setInterval(() => {
      if (window.Chart) { clearInterval(wait); daDrawCharts(summary); }
    }, 50);
  } else {
    // Fallback: load on demand (e.g. if section was never observed)
    window._chartJsLoading = true;
    const s = document.createElement('script');
    s.src = 'https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.0/chart.umd.min.js';
    s.onload = () => daDrawCharts(summary);
    document.head.appendChild(s);
  }
}

function daDrawCharts(summary) {
  // Destroy any existing charts to avoid canvas reuse errors
  ['da-chart-bar','da-chart-line','da-chart-doughnut'].forEach(id => {
    const existing = Chart.getChart(id);
    if (existing) existing.destroy();
  });

  const truncateChartText = (value, max = 20) => {
    const text = String(value ?? '').trim();
    if (!text || text.length <= max) return text;
    return `${text.slice(0, Math.max(0, max - 3)).trimEnd()}...`;
  };
  const truncateLegendLabels = chart =>
    Chart.defaults.plugins.legend.labels.generateLabels(chart).map(item => ({
      ...item,
      text: truncateChartText(item.text, 18),
    }));
  // Line datasets: draw the legend swatch as a line sample rather than a box.
  // A box takes its fill from backgroundColor, so the transparent, dashed
  // forecast series renders as a ragged 12px outline instead of a dashed
  // line. Chart.js's built-in 'line' point style ignores the dash pattern,
  // which would make the projection indistinguishable from the history, so
  // the swatch is drawn here and handed over as a canvas. pointStyle is set
  // on the legend item, not the dataset, so the markers plotted on the chart
  // itself stay circles.
  const legendLineSwatch = (item, dash) => {
    const line = 26, h = 8;
    const c = document.createElement('canvas');
    c.width = line; c.height = h;
    const g = c.getContext('2d');
    g.strokeStyle = item.strokeStyle || 'rgba(214,204,255,.95)';
    g.lineWidth = 2;
    g.setLineDash(dash && dash.length ? dash : []);
    g.beginPath();
    g.moveTo(0, h / 2);
    g.lineTo(line, h / 2);
    g.stroke();
    return c;
  };
  // Chart.js 4.4 does not copy borderDash onto the legend item, so the dash
  // is read from the dataset the item points at.
  const lineLegendLabels = chart =>
    truncateLegendLabels(chart).map(item => {
      const ds = chart.data.datasets[item.datasetIndex] || {};
      return { ...item, pointStyle: legendLineSwatch(item, ds.borderDash) };
    });

  // Chart colours
  const COLOURS = [
    'rgba(124,58,237,.85)','rgba(6,182,212,.85)','rgba(196,181,253,.85)',
    'rgba(59,130,246,.85)','rgba(139,92,246,.85)','rgba(34,211,238,.85)',
  ];

  const defaults = {
    responsive: true, maintainAspectRatio: true,
    layout: { padding: { left: 18, right: 18 } },
    plugins: {
      legend: {
        position: 'top',
        align: 'center',
        labels: {
          color:'rgba(214,204,255,.95)',
          font:{family:'Plus Jakarta Sans',size:10},
          boxWidth: 12,
          generateLabels: truncateLegendLabels,
        },
      },
    },
    scales: {
      x: {
        ticks:{
          color:'rgba(214,204,255,.85)',
          font:{size:9},
          maxRotation: 22,
          minRotation: 22,
          callback(value) {
            const label = typeof this.getLabelForValue === 'function'
              ? this.getLabelForValue(value)
              : value;
            return truncateChartText(label, 18);
          },
        },
        grid:{color:'rgba(155,93,229,.15)'},
      },
      y: { ticks:{color:'rgba(214,204,255,.85)',font:{size:9}}, grid:{color:'rgba(155,93,229,.15)'} },
    },
    animation: { duration: 900, easing: 'easeOutQuart' },
  };

  // Bar chart: backend-computed measure totals per top category
  const cat = summary.category_totals || {};
  const barCanvas = document.getElementById('da-chart-bar');
  barCanvas.style.display = (cat.labels && cat.labels.length) ? '' : 'none';
  if (cat.labels && cat.labels.length) {
    new Chart(barCanvas, {
      type: 'bar',
      data: {
        labels: cat.labels,
        datasets: [{
          label: cat.title,
          data: cat.values,
          backgroundColor: COLOURS[0],
          borderColor: COLOURS[0].replace('.6', '.9'),
          borderWidth: 1, borderRadius: 4,
        }],
      },
      options: { ...defaults, plugins: { ...defaults.plugins,
        legend: { display: false },
        title: { display:true, text: truncateChartText(cat.title, 34), color:'rgba(240,236,252,.95)', font:{size:11} }
      }},
    });
  }

  // Line chart: backend-computed measure aggregated over time, with the
  // forward forecast drawn as a dashed continuation
  const ts = summary.timeseries || {};
  const fc = summary.forecast || {};
  const lineCanvas = document.getElementById('da-chart-line');
  lineCanvas.style.display = (ts.labels && ts.labels.length) ? '' : 'none';
  if (ts.labels && ts.labels.length) {
    const hasFc = !!(fc.labels && fc.labels.length);
    const labels = hasFc ? ts.labels.concat(fc.labels) : ts.labels;
    const histData = hasFc ? ts.values.concat(Array(fc.labels.length).fill(null)) : ts.values;
    const datasets = [{
      label: ts.title,
      data: histData,
      borderColor: 'rgba(196,181,253,.9)',
      backgroundColor: 'rgba(155,93,229,.08)',
      tension: .4, pointBackgroundColor: 'rgba(155,93,229,.8)', fill: true,
    }];
    if (hasFc) {
      // Start the dashed line at the last historical point so it connects
      const fcData = Array(ts.labels.length - 1).fill(null)
        .concat([ts.values[ts.values.length - 1]], fc.values);
      datasets.push({
        label: `Forecast (next ${fc.horizon})`,
        data: fcData,
        borderColor: 'rgba(155,93,229,.95)',
        borderDash: [6, 4],
        backgroundColor: 'transparent',
        tension: .4, pointRadius: 2, pointBackgroundColor: 'rgba(155,93,229,.6)',
        fill: false,
      });
    }
    new Chart(lineCanvas, {
      type: 'line',
      data: { labels, datasets },
      options: { ...defaults, plugins: { ...defaults.plugins,
        legend: { display: hasFc, position: 'top',
          labels: { color:'rgba(214,204,255,.95)', font:{family:'Plus Jakarta Sans',size:9},
                    // usePointStyle clamps boxWidth to the font size
                    // (min(boxWidth, fontSize)) unless pointStyleWidth is
                    // given, which is what reserves room for the swatch and
                    // keeps it off the label.
                    usePointStyle: true, pointStyleWidth: 38,
                    boxWidth: 38, boxHeight: 8,
                    generateLabels: lineLegendLabels } },
        title: { display:true,
          text: truncateChartText(hasFc ? `${ts.title} + forecast` : ts.title, 34),
          color:'rgba(240,236,252,.95)', font:{size:11} }
      }},
    });
  }

  // Doughnut: backend-computed distribution (category / bin counts)
  const dist = summary.distribution || {};
  const doughnutLabels = Object.keys(dist);
  const doughnutCanvas = document.getElementById('da-chart-doughnut');
  doughnutCanvas.style.display = doughnutLabels.length ? '' : 'none';
  if (doughnutLabels.length) {
    new Chart(doughnutCanvas, {
      type: 'doughnut',
      data: {
        labels: doughnutLabels,
        datasets: [{ data: doughnutLabels.map(k => dist[k]), backgroundColor: COLOURS, borderWidth: 0 }],
      },
      options: {
        responsive: true, maintainAspectRatio: true,
        layout: { padding: { left: 18, right: 18 } },
        plugins: {
          legend: {
            position:'bottom',
            align:'center',
            labels:{
              color:'rgba(214,204,255,.95)',
              font:{family:'Plus Jakarta Sans',size:9},
              boxWidth:10,
              generateLabels: truncateLegendLabels,
            },
          },
          title: { display:true, text: truncateChartText(summary.distribution_label || 'Distribution', 34), color:'rgba(240,236,252,.95)', font:{size:11} },
        },
        animation: { duration:900, easing:'easeOutQuart' },
      },
    });
  }
}

// ── Real LLM chat ────────────────────────────────────
async function daChatSend() {
  const input = document.getElementById('da-chat-input');
  const msgs  = document.getElementById('da-chat-messages');
  const q = input.value.trim();
  if (!q) return;
  input.value = '';

  const userMsg = document.createElement('div');
  userMsg.className = 'da-msg user';
  userMsg.textContent = q;
  msgs.appendChild(userMsg);
  msgs.scrollTop = msgs.scrollHeight;

  // Typing indicator
  const typing = document.createElement('div');
  typing.className = 'da-msg ai';
  typing.innerHTML = '<em style="opacity:.5">thinking...</em>';
  msgs.appendChild(typing);
  msgs.scrollTop = msgs.scrollHeight;

  try {
    const response = await _authFetch('/api/v1/data/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ..._authHeaders() },
      body: JSON.stringify({
        question: q,
        context: window._daContext || '',
      }),
    });

    const json = await response.json();
    if (response.ok) {
      typing.textContent = json.data.answer;
    } else {
      typing.textContent = 'Error: ' + (json.detail || 'Backend error');
      typing.style.color = 'rgba(239,68,68,.8)';
    }
  } catch (err) {
    typing.textContent = 'Could not reach backend: ' + err.message;
    typing.style.color = 'rgba(239,68,68,.8)';
  }

  msgs.scrollTop = msgs.scrollHeight;
}

// ── Chat Enter listener is set up lazily in initDataAnalysis() ──────────────

/* ═══════════════════════════════════════════════════════
   LAZY SECTION INITIALIZATION
═══════════════════════════════════════════════════════ */

// ── Data Analysis lazy init ──────────────────────────
let _daInitialized = false;

function initDataAnalysis() {
  if (_daInitialized) return;
  _daInitialized = true;

  // Wire up drop zone
  const dz = document.getElementById('da-drop-zone');
  const fi = document.getElementById('da-file-input');
  document.getElementById('da-drop-browse').addEventListener('click', e => { e.stopPropagation(); fi.click(); });
  dz.addEventListener('click', () => fi.click());
  fi.addEventListener('change', e => { if (e.target.files[0]) daHandleFile(e.target.files[0]); });
  dz.addEventListener('dragenter', e => { e.preventDefault(); dz.classList.add('drag-over'); });
  dz.addEventListener('dragover',  e => { e.preventDefault(); dz.classList.add('drag-over'); });
  dz.addEventListener('dragleave', () => dz.classList.remove('drag-over'));
  dz.addEventListener('drop', e => {
    e.preventDefault(); dz.classList.remove('drag-over');
    if (e.dataTransfer.files[0]) daHandleFile(e.dataTransfer.files[0]);
  });

  // Wire AI chat Enter key - no backend touch until user sends a message
  document.getElementById('da-chat-input').addEventListener('keydown', e => {
    if (e.key === 'Enter') daChatSend();
  });

  // Pre-fetch Chart.js in the background so it is cached by the time
  // analysis results come back from the API - avoids a waterfall delay.
  if (!window.Chart && !window._chartJsLoading) {
    window._chartJsLoading = true;
    const s = document.createElement('script');
    s.src = 'https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.0/chart.umd.min.js';
    s.async = true;
    document.head.appendChild(s);
  }

  // Restore the last analysis after a page refresh (session-scoped) so an
  // accidental F5 doesn't force a multi-minute re-run.
  try {
    const cached = sessionStorage.getItem('autolyze_last_analysis');
    if (cached && !window._daResult) {
      const data = JSON.parse(cached);
      dz.style.display = 'none';
      daShowResults(data);
    }
  } catch (err) {
    console.warn('Could not restore previous analysis:', err);
  }
}

// ── AI chat lazy guard ────────────────────────────────
// daChatSend already only hits the backend when the user fires it.
// This flag makes the intent explicit: no backend connection before
// the first user message.
let _chatInitialized = false;
const _daChatSendOrig = daChatSend;
daChatSend = function() {
  _chatInitialized = true;
  return _daChatSendOrig();
};

// ── IntersectionObservers ─────────────────────────────
// threshold: 0.05 - fires once even a small sliver of the section
// is visible, which happens the moment scrollIntoView brings it in.
(function() {
  const obs = (selector, initFn) => {
    const el = document.querySelector(selector);
    if (!el) return;
    const io = new IntersectionObserver(entries => {
      if (entries[0].isIntersecting) { initFn(); io.disconnect(); }
    }, { threshold: 0.05 });
    io.observe(el);
  };

  obs('#da-section',  initDataAnalysis);
})();

/* ═══════════════════════════════════════════════════════
   SHOOTING STARS - skipped in perf mode, uses master loop
═══════════════════════════════════════════════════════ */
if (!PERF_MODE) {
(function() {
  const canvas = document.getElementById('shooting-stars-canvas');
  const ctx    = canvas.getContext('2d');
  let W = 0, H = 0;

  function resize() {
    if (canvas.width === innerWidth && canvas.height === innerHeight) return;
    W = canvas.width = innerWidth;
    H = canvas.height = innerHeight;
  }
  resize();
  window.addEventListener('resize', resize, { passive: true });

  const stars = [];
  const MAX   = 3;

  function spawnStar() {
    const edge  = Math.floor(Math.random() * 4);
    let sx, sy, angle;
    if (edge === 0) {
      sx = Math.random() * W; sy = -20;
      angle = (Math.PI * 0.1) + Math.random() * Math.PI * 0.8;
    } else if (edge === 1) {
      sx = W + 20; sy = Math.random() * H;
      angle = Math.PI * 0.6 + Math.random() * Math.PI * 0.8;
    } else if (edge === 2) {
      sx = Math.random() * W; sy = H + 20;
      angle = Math.PI * 1.1 + Math.random() * Math.PI * 0.8;
    } else {
      sx = -20; sy = Math.random() * H;
      angle = -Math.PI * 0.3 + Math.random() * Math.PI * 0.6;
    }
    const speed = 1.2 + Math.random() * 1.8;
    const length = 80 + Math.random() * 140;
    const hue = 240 + Math.random() * 60;
    const maxLife = (length + W * 0.6) / speed;
    stars.push({ x: sx, y: sy, angle, speed, hue, life: 0, maxLife, alpha: 0, trail: [] });
  }

  _rafSystems.push(function() {
    ctx.clearRect(0, 0, W, H);
    if (stars.length < MAX && Math.random() < 0.006) spawnStar();

    for (let i = stars.length - 1; i >= 0; i--) {
      const s = stars[i];
      s.x += Math.cos(s.angle) * s.speed;
      s.y += Math.sin(s.angle) * s.speed;
      s.life++;
      const progress = s.life / s.maxLife;
      s.alpha = Math.max(0, Math.min(1,
        progress < 0.12 ? progress / 0.12 : 1 - ((progress - 0.12) / 0.88)
      ));
      s.trail.push({ x: s.x, y: s.y });
      if (s.trail.length > 55) s.trail.shift();

      if (s.trail.length > 2) {
        for (let t = 1; t < s.trail.length; t++) {
          const tp = t / s.trail.length;
          ctx.beginPath();
          ctx.moveTo(s.trail[t-1].x, s.trail[t-1].y);
          ctx.lineTo(s.trail[t].x,   s.trail[t].y);
          ctx.strokeStyle = `hsla(${s.hue - (1-tp)*30}, 85%, 65%, ${tp * s.alpha * 0.7})`;
          ctx.lineWidth   = tp * 2.2;
          ctx.stroke();
        }
      }

      const grd = ctx.createRadialGradient(s.x, s.y, 0, s.x, s.y, 8);
      grd.addColorStop(0,   `hsla(${s.hue+20}, 100%, 95%, ${s.alpha})`);
      grd.addColorStop(0.4, `hsla(${s.hue},    90%,  70%, ${s.alpha * 0.6})`);
      grd.addColorStop(1,   `hsla(${s.hue},    80%,  50%, 0)`);
      ctx.beginPath(); ctx.arc(s.x, s.y, 8, 0, Math.PI * 2);
      ctx.fillStyle = grd; ctx.fill();

      if (s.life > s.maxLife) stars.splice(i, 1);
    }
  });
})();
}

/* ═══════════════════════════════════════════════════════
   GLOBE STATE - controls glow appearance
   States: idle | thinking | processing | complete
═══════════════════════════════════════════════════════ */
function setGlobeState(state) {
  document.body.classList.remove('glow-idle', 'glow-thinking', 'glow-processing', 'glow-complete');
  document.body.classList.add('glow-' + state);
  // Disconnect beam when returning to idle or complete (beam shown during active processing only)
  if (state === 'idle') disconnectBeam();
}

/* ═══════════════════════════════════════════════════════
   AI ENERGY BEAM - connects globe to the active module
═══════════════════════════════════════════════════════ */
const beam = document.createElement('div');
beam.id = 'ai-beam';
document.body.appendChild(beam);

function connectGlobeTo(targetId) {
  const globe  = document.getElementById('globe-wrap');
  const target = document.getElementById(targetId);
  if (!globe || !target) return;

  const g = globe.getBoundingClientRect();
  const t = target.getBoundingClientRect();
  const beamTop    = g.bottom;
  const beamHeight = t.top - g.bottom;

  if (beamHeight <= 0) return; // section already overlaps globe - skip beam

  beam.style.left    = (g.left + g.width / 2 - 1.5) + 'px'; // center on globe
  beam.style.top     = beamTop + 'px';
  beam.style.height  = beamHeight + 'px';
  beam.style.opacity = '1';
}

function disconnectBeam() {
  beam.style.opacity = '0';
}

// Keep beam position in sync during scroll/resize while visible
// Throttled - beam moves slowly, no need to update every scroll pixel
let _beamSyncTimer = null;
function _syncBeamIfVisible() {
  if (beam.style.opacity === '0' || !beam.style.opacity) return;
  clearTimeout(_beamSyncTimer);
  _beamSyncTimer = setTimeout(() => {
    const activeIds = ['da-section'];
    for (const id of activeIds) {
      const s = document.getElementById(id);
      if (s && s.classList.contains('active')) { connectGlobeTo(id); return; }
    }
  }, 50);
}
window.addEventListener('scroll', _syncBeamIfVisible, { passive: true });
window.addEventListener('resize', _syncBeamIfVisible, { passive: true });

/* ═══════════════════════════════════════════════════════
   SYNC PURPLE GLOW TO GLOBE CANVAS
   Throttled to every 4th frame (~15fps) - glow moves
   slowly so sub-60fps tracking is imperceptible.
   Registered in master rAF loop, not its own.
═══════════════════════════════════════════════════════ */
function syncGlowToGlobe() {
  const glow   = document.getElementById('purple-glow');
  const canvas = document.getElementById('globe-canvas');
  if (!glow || !canvas) return;
  const rect = canvas.getBoundingClientRect();
  glow.style.left = (rect.left + rect.width  / 2) + 'px';
  glow.style.top  = (rect.top  + rect.height / 2) + 'px';
}

let _glowSyncFrame = 0;
_rafSystems.push(() => {
  _glowSyncFrame++;
  if (_glowSyncFrame % 4 !== 0) return;
  syncGlowToGlobe();
});

// Start in idle state
setGlobeState('idle');

/* ── Pause globe render when off-screen (saves GPU when sections open) ── */
let _globeVisible = true;
const _globeIO = new IntersectionObserver(entries => {
  _globeVisible = entries[0].isIntersecting;
  // Also pause CSS halo-pulse animation when globe is off-screen
  document.body.classList.toggle('globe-hidden', !_globeVisible);
}, { threshold: 0 });
const _globeWrapEl2 = document.getElementById('globe-wrap');
if (_globeWrapEl2) _globeIO.observe(_globeWrapEl2);

/* ═══════════════════════════════════════════════════════
   CINEMATIC BOOT-UP SEQUENCE (introTL)
   Runs on every page load - animates:
     1. Globe fades in
     2. Login card + logo slide up
     3. Purple glow expands
     4. Globe rotation ramps up (bootBoost)
     5. Final AI-ready glow pulse
   Mouse parallax on the globe continues to work because
   bootBoost only scales the existing boost multiplier -
   it does not touch targetRotX/Y or the mouse listeners.
═══════════════════════════════════════════════════════ */
const introTL = gsap.timeline({ defaults: { ease: 'power3.out' } });

// 1. Globe fades/rises in
introTL.to('#globe-wrap', { opacity: 1, y: 0, scale: 1, duration: 1.2 }, 0);

// 2. Login card slides up (only when login overlay is visible)
if (!_authed) {
  introTL
    .to('#login-card',  { opacity: 1, y: 0, scale: 1, duration: 0.9, ease: 'power3.out' }, '-=0.6')
    .to('#login-logo',  { opacity: 1, y: 0, duration: 0.7, ease: 'power2.out' }, '-=0.5');
}

// 3. Purple glow expands as globe powers on
introTL.to('#purple-glow', { scale: 1.15, duration: 1, ease: 'power2.out' }, '-=0.8');

// 4. Ramp bootBoost from 0.1 → 1.0 over 1.2s so globe visibly spins up
introTL.to({ val: bootBoost }, {
  val:      1.0,
  duration: 1.2,
  ease:     'power2.inOut',
  onUpdate: function() { bootBoost = this.targets()[0].val; },
}, '-=0.4');

// 5. Final AI-ready pulse - settle glow to resting scale
introTL.to('#purple-glow', { scale: 1.05, duration: 0.6, ease: 'power2.out' });

/* ═══════════════════════════════════════════════════════
   HIDE LOADER
═══════════════════════════════════════════════════════ */

window.addEventListener('load', () => {
  window.scrollTo(0, 0);
  _forcePageScrollUnlocked();
  // Fade is decorative; the loader must be dismissed even when rAF/GSAP
  // is throttled (background tab, low-power webview), so hide on a timer.
  gsap.to('#loader', { opacity: 0, duration: .6 });
  setTimeout(() => {
    document.getElementById('loader').style.display = 'none';
    // If the user is already authenticated (returning session),
    // the overlay is hidden and we start the cinematic intro now.
    if (_authed) tl.play();
  }, 650);
});
