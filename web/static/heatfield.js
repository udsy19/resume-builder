/* The heat field — the design system's signature visual.
 *
 * A continuous scalar field on a fixed lattice, quantised into hard bands. Built to the
 * poster-series spec, including the four rules that decide whether it looks right:
 * quantise hard (never interpolate), keep DIFFUSE low or the bands fuse into a weather
 * map, add a STABLE per-cell grain so band edges dissolve into surviving cells rather
 * than clean contours, and fix the cell size in screen pixels so a wider viewport holds
 * more cells instead of bigger ones.
 *
 * Rendered as an ImageData at grid resolution and upscaled with image-rendering:
 * pixelated — one pixel per cell, not thousands of fillRect calls.
 */

const CELL = 9;        // px, fixed in SCREEN space, never scaled to viewport
const BRUSH = 10;      // pointer heat radius, in cells
const RELAX = 0.055;   // how fast the field chases its target
const DIFFUSE = 0.045; // neighbour bleed — the single most sensitive constant
const COOL = 0.94;     // pointer heat decay per frame
const FREQ = 0.052;    // noise frequency; higher = finer structure
const GRAIN = 0.17;    // static per-cell dither

// Below the first cut the cell is left unpainted and the ground shows through, which is
// what lets one field sit on any plate colour.
const CUTS = [0.30, 0.46, 0.62, 0.78];

function hash(x, y) {
  const n = Math.sin(x * 127.1 + y * 311.7) * 43758.5453;
  return n - Math.floor(n);
}

// Value noise: smooth-interpolated lattice hash, cheap and stable across frames.
function noise(x, y) {
  const xi = Math.floor(x), yi = Math.floor(y);
  const xf = x - xi, yf = y - yi;
  const u = xf * xf * (3 - 2 * xf), v = yf * yf * (3 - 2 * yf);
  const a = hash(xi, yi), b = hash(xi + 1, yi);
  const c = hash(xi, yi + 1), d = hash(xi + 1, yi + 1);
  return a * (1 - u) * (1 - v) + b * u * (1 - v) + c * (1 - u) * v + d * u * v;
}

function rgb(el, varName) {
  const raw = getComputedStyle(el).getPropertyValue(varName).trim() || '#000';
  const m = raw.match(/^#([0-9a-f]{6})$/i);
  if (!m) return [0, 0, 0];
  const n = parseInt(m[1], 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

export function mountHeatField(canvas) {
  if (!canvas) return () => {};
  const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const ctx = canvas.getContext('2d', { alpha: true });
  const parent = canvas.parentElement;

  // Colours are resolved from CSS custom properties so the field re-themes with the page
  // rather than hard-coding the palette in two places.
  let bands = [];
  const readBands = () => {
    bands = [rgb(canvas, '--ink'), rgb(canvas, '--cobalt'),
             rgb(canvas, '--sky'), rgb(canvas, '--signal')];
  };

  let cols = 0, rows = 0, img = null, field = null, heat = null, t = 0, raf = 0;
  const pointer = { x: -999, y: -999, on: false };

  function resize() {
    const w = parent.clientWidth, h = parent.clientHeight;
    if (!w || !h) return;
    cols = Math.max(1, Math.ceil(w / CELL));
    rows = Math.max(1, Math.ceil(h / CELL));
    canvas.width = cols;
    canvas.height = rows;          // grid resolution; CSS stretches it back up
    img = ctx.createImageData(cols, rows);
    field = new Float32Array(cols * rows);
    heat = new Float32Array(cols * rows);
    readBands();
  }

  // Falloff, mass upper-left, dissolving down and right. The exponent keeps the boundary
  // from reading as a straight line.
  //
  // The spec's coefficients (0.42u + 0.62v, ×1.9) are tuned for a tall plate. This band
  // is wide and short — about 125×22 cells — so a v-dominant falloff put the whole mass
  // along the top edge as a ceiling rather than a diagonal. Weighting u instead keeps the
  // intended read: mass at the left, dissolving right. The amplitude comes down from 1.9
  // because at 1.9 the top band covered a third of the field, and the hot colour is
  // specified as a core of roughly 2% of pixels, never a region.
  const ramp = (u, v) => Math.pow(Math.max(0, 1 - (u * 0.62 + v * 0.25)), 1.35) * 1.25;

  function advance() {
    t += 0.004;
    const data = img.data;
    for (let y = 0; y < rows; y++) {
      const v = y / rows;
      for (let x = 0; x < cols; x++) {
        const i = y * cols + x;
        const u = x / cols;
        const target = noise(x * FREQ + t, y * FREQ - t * 0.6) * ramp(u, v);

        // Neighbour bleed, kept deliberately low.
        const l = x > 0 ? field[i - 1] : field[i];
        const r = x < cols - 1 ? field[i + 1] : field[i];
        const up = y > 0 ? field[i - cols] : field[i];
        const dn = y < rows - 1 ? field[i + cols] : field[i];
        const blend = (l + r + up + dn) * 0.25 - field[i];

        field[i] += (target - field[i]) * RELAX + blend * DIFFUSE;
        heat[i] *= COOL;

        // Stable per-cell grain — the dissolve. Per-frame noise here would shimmer.
        const value = field[i] + heat[i] + (hash(x, y) - 0.5) * GRAIN;

        let band = -1;
        for (let b = CUTS.length - 1; b >= 0; b--) {
          if (value >= CUTS[b]) { band = b; break; }
        }
        const o = i * 4;
        if (band < 0) {
          data[o + 3] = 0;                       // unpainted: the ground shows through
        } else {
          const c = bands[band];
          data[o] = c[0]; data[o + 1] = c[1]; data[o + 2] = c[2]; data[o + 3] = 255;
        }
      }
    }

    if (pointer.on) {
      const px = pointer.x / CELL, py = pointer.y / CELL;
      const x0 = Math.max(0, Math.floor(px - BRUSH)), x1 = Math.min(cols - 1, Math.ceil(px + BRUSH));
      const y0 = Math.max(0, Math.floor(py - BRUSH)), y1 = Math.min(rows - 1, Math.ceil(py + BRUSH));
      for (let y = y0; y <= y1; y++) {
        for (let x = x0; x <= x1; x++) {
          const d = Math.hypot(x - px, y - py);
          if (d < BRUSH) heat[y * cols + x] += (1 - d / BRUSH) * 0.09;
        }
      }
    }

  }

  function step() {
    advance();
    ctx.putImageData(img, 0, 0);
    raf = requestAnimationFrame(step);
  }

  const onMove = (e) => {
    const r = canvas.getBoundingClientRect();
    pointer.x = e.clientX - r.left;
    pointer.y = e.clientY - r.top;
    pointer.on = true;
  };
  const onLeave = () => { pointer.on = false; };

  const ro = new ResizeObserver(resize);
  ro.observe(parent);
  resize();

  // The pointer is a heat source, not decoration — but a coarse pointer has none, and
  // reduced-motion users get a single settled frame instead of a live field.
  const fine = window.matchMedia('(pointer: fine)').matches;
  if (fine && !reduced) {
    parent.addEventListener('pointermove', onMove);
    parent.addEventListener('pointerleave', onLeave);
  }

  if (reduced) {
    // Settle to a still frame rather than animating: same field, no motion.
    for (let i = 0; i < 220; i++) advance();
    ctx.putImageData(img, 0, 0);
  } else {
    raf = requestAnimationFrame(step);
  }

  return () => {
    cancelAnimationFrame(raf);
    ro.disconnect();
    parent.removeEventListener('pointermove', onMove);
    parent.removeEventListener('pointerleave', onLeave);
  };
}
