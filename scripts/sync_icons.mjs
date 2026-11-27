#!/usr/bin/env node
/**
 * Derive all HORUS documentation and PWA icons from the single canonical SVG:
 *   horus/commander/web/icon.svg
 */

import { copyFileSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const SOURCE_SVG = resolve(ROOT, "horus/commander/web/icon.svg");
const MASKABLE_SVG = resolve(ROOT, "horus/commander/web/icon-maskable.svg");
const PNG_TARGETS = [
  { input: SOURCE_SVG, output: resolve(ROOT, "horus/commander/web/icon-192.png"), size: 192 },
  { input: SOURCE_SVG, output: resolve(ROOT, "horus/commander/web/icon-512.png"), size: 512 },
  { input: MASKABLE_SVG, output: resolve(ROOT, "horus/commander/web/icon-maskable-512.png"), size: 512 },
];
const SVG_COPIES = [
  resolve(ROOT, "site/src/assets/logo.svg"),
  resolve(ROOT, "site/public/favicon.svg"),
];

const require = createRequire(resolve(ROOT, "site/package.json"));
const sharp = require("sharp");

function buildMaskableSvg(sourceText) {
  const viewBoxMatch = sourceText.match(/viewBox="([^"]+)"/);
  const viewBox = viewBoxMatch ? viewBoxMatch[1] : "0 0 512 512";
  const parts = viewBox.trim().split(/\s+/).map(Number);
  const width = parts[2] || 512;
  const height = parts[3] || 512;
  const cx = width / 2;
  const cy = height / 2;

  const rectMatch = sourceText.match(/<rect\b[^>]*\bfill="([^"]+)"[^>]*\/?>/);
  const bgFill = rectMatch ? rectMatch[1] : "#10233f";

  const markMatch = sourceText.match(/<g\b[^>]*\bid="mark"[^>]*>([\s\S]*?)<\/g>\s*<\/svg>/);
  let inner = markMatch ? markMatch[1].trim() : "";
  if (!inner) {
    const bodyMatch = sourceText.match(/<svg\b[^>]*>([\s\S]*?)<\/svg>/);
    if (!bodyMatch) {
      throw new Error(`Could not parse SVG body in ${SOURCE_SVG}`);
    }
    inner = bodyMatch[1]
      .replace(/<title>[\s\S]*?<\/title>/i, "")
      .replace(/<rect\b[^>]*\/?>/, "")
      .trim();
  }

  return [
    `<svg xmlns="http://www.w3.org/2000/svg" viewBox="${viewBox}" width="${width}" height="${height}">`,
    `  <rect width="${width}" height="${height}" fill="${bgFill}"/>`,
    `  <g transform="translate(${cx} ${cy}) scale(0.74) translate(${-cx} ${-cy})">`,
    ...inner.split("\n").map((line) => `  ${line}`),
    `  </g>`,
    `</svg>`,
    "",
  ].join("\n");
}

async function main() {
  const sourceText = readFileSync(SOURCE_SVG, "utf8");

  for (const dest of SVG_COPIES) {
    mkdirSync(dirname(dest), { recursive: true });
    copyFileSync(SOURCE_SVG, dest);
  }

  const maskableText = buildMaskableSvg(sourceText);
  writeFileSync(MASKABLE_SVG, maskableText, "utf8");

  for (const { input, output, size } of PNG_TARGETS) {
    await sharp(input, { density: 384 })
      .resize(size, size)
      .png()
      .toFile(output);
  }

  console.log("Synced icons from horus/commander/web/icon.svg:");
  for (const dest of [MASKABLE_SVG, ...SVG_COPIES, ...PNG_TARGETS.map((t) => t.output)]) {
    console.log(" -", dest.replace(`${ROOT}/`, ""));
  }
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
