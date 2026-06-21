#!/usr/bin/env node
/*
 * generate-icons.js
 *
 * Generates the Wax Digger toolbar icons (16/32/48/128) with zero dependencies.
 * Pure Node: builds RGBA pixels, draws the WAX DIGGER mark (a cream "↓" arrow
 * inside a framed dark tile, matching the app palette), supersamples 4× for
 * smooth edges, and encodes a PNG by hand using the built-in zlib.
 *
 *   node generate-icons.js
 */

const fs = require("fs");
const path = require("path");
const zlib = require("zlib");

// ---- Palette (mirrors the app's light theme) ----
const DARK = [0x21, 0x1c, 0x13]; // --accent / --text
const CREAM = [0xe8, 0xe1, 0xce]; // --accent-text / --bg

const SIZES = [16, 32, 48, 128];
const SS = 4; // supersample factor

// ---- CRC32 (for PNG chunks) ----
const CRC_TABLE = (() => {
  const t = new Uint32Array(256);
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    t[n] = c >>> 0;
  }
  return t;
})();

function crc32(buf) {
  let c = 0xffffffff;
  for (let i = 0; i < buf.length; i++) c = CRC_TABLE[(c ^ buf[i]) & 0xff] ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}

function chunk(type, data) {
  const len = Buffer.alloc(4);
  len.writeUInt32BE(data.length, 0);
  const typeBuf = Buffer.from(type, "ascii");
  const crcBuf = Buffer.alloc(4);
  crcBuf.writeUInt32BE(crc32(Buffer.concat([typeBuf, data])), 0);
  return Buffer.concat([len, typeBuf, data, crcBuf]);
}

function encodePNG(width, height, rgba) {
  const sig = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);

  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(width, 0);
  ihdr.writeUInt32BE(height, 4);
  ihdr[8] = 8; // bit depth
  ihdr[9] = 6; // color type RGBA
  ihdr[10] = 0; // compression
  ihdr[11] = 0; // filter
  ihdr[12] = 0; // interlace

  // raw scanlines, each prefixed with filter byte 0
  const stride = width * 4;
  const raw = Buffer.alloc((stride + 1) * height);
  for (let y = 0; y < height; y++) {
    raw[y * (stride + 1)] = 0;
    rgba.copy(raw, y * (stride + 1) + 1, y * stride, y * stride + stride);
  }

  const idat = zlib.deflateSync(raw, { level: 9 });

  return Buffer.concat([
    sig,
    chunk("IHDR", ihdr),
    chunk("IDAT", idat),
    chunk("IEND", Buffer.alloc(0)),
  ]);
}

// ---- Drawing: is pixel (x,y) part of the down-arrow glyph? (unit coords 0..N) ----
function inArrow(x, y, N) {
  const cx = N / 2;

  // shaft (vertical bar)
  const shaftHalf = N * 0.085;
  if (x >= cx - shaftHalf && x <= cx + shaftHalf && y >= N * 0.2 && y <= N * 0.56) {
    return true;
  }

  // arrowhead (triangle pointing down)
  const headTop = N * 0.46;
  const headBottom = N * 0.82;
  const headHalfMax = N * 0.26;
  if (y >= headTop && y <= headBottom) {
    const t = (y - headTop) / (headBottom - headTop); // 0 at top, 1 at tip
    const half = headHalfMax * (1 - t);
    if (Math.abs(x - cx) <= half) return true;
  }

  return false;
}

function renderIcon(size) {
  const N = size * SS;
  const buf = Buffer.alloc(N * N * 4);

  const frame = Math.max(1, Math.round(N * 0.06)); // cream inner border
  const gap = Math.round(N * 0.02);

  for (let y = 0; y < N; y++) {
    for (let x = 0; x < N; x++) {
      let color;
      const edge = x < frame || y < frame || x >= N - frame || y >= N - frame;
      const ring =
        !edge &&
        (x < frame + gap || y < frame + gap || x >= N - frame - gap || y >= N - frame - gap);

      if (edge) {
        color = CREAM; // outer frame
      } else if (ring) {
        color = DARK; // breathing gap between frame and arrow field
      } else if (inArrow(x, y, N)) {
        color = CREAM; // the ↓ glyph
      } else {
        color = DARK; // tile background
      }

      const i = (y * N + x) * 4;
      buf[i] = color[0];
      buf[i + 1] = color[1];
      buf[i + 2] = color[2];
      buf[i + 3] = 255;
    }
  }

  // downscale (box filter) from N×N to size×size
  const out = Buffer.alloc(size * size * 4);
  const area = SS * SS;
  for (let y = 0; y < size; y++) {
    for (let x = 0; x < size; x++) {
      let r = 0,
        g = 0,
        b = 0,
        a = 0;
      for (let sy = 0; sy < SS; sy++) {
        for (let sx = 0; sx < SS; sx++) {
          const i = ((y * SS + sy) * N + (x * SS + sx)) * 4;
          r += buf[i];
          g += buf[i + 1];
          b += buf[i + 2];
          a += buf[i + 3];
        }
      }
      const o = (y * size + x) * 4;
      out[o] = Math.round(r / area);
      out[o + 1] = Math.round(g / area);
      out[o + 2] = Math.round(b / area);
      out[o + 3] = Math.round(a / area);
    }
  }

  return encodePNG(size, size, out);
}

function main() {
  const outDir = path.join(__dirname, "icons");
  fs.mkdirSync(outDir, { recursive: true });

  for (const size of SIZES) {
    const png = renderIcon(size);
    const file = path.join(outDir, `icon${size}.png`);
    fs.writeFileSync(file, png);
    console.log(`✓ icons/icon${size}.png  (${png.length} bytes)`);
  }
  console.log("Done.");
}

main();
