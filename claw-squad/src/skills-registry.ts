/**
 * Skills marketplace (W7.3).
 *
 * The local skills loader (`skills.ts`) reads `.claw-squad/skills/*.md`
 * already committed to the repo. The marketplace is the layer above:
 * fetching skills from a remote source and installing them into that
 * directory, with a content-addressed integrity check.
 *
 * Manifest shape (one JSON file per skill, served alongside the .md):
 *
 *   {
 *     "id": "python-testing",          // stable identifier
 *     "version": "1.2.0",              // semver; comparison string-only
 *     "description": "...",
 *     "url": "https://.../python-testing-1.2.0.md",
 *     "sha256": "abcd1234...",         // sha256 of the .md body
 *     "applyTo": ["test_*.py", ...],   // optional glob list
 *     "license": "MIT",                // optional
 *     "homepage": "https://...",       // optional
 *     "publishedAt": "2026-04-26T..."  // optional ISO-8601
 *   }
 *
 * The registry index is a JSON list of these manifests served at a
 * stable URL (default `https://skills.claudestruct.dev/index.json`,
 * configurable via `CLAW_SKILLS_REGISTRY`). Out-of-the-box installs
 * read from there; air-gapped environments can point at a local file
 * or a self-hosted index.
 *
 * Why content-addressed instead of cosign? Cosign is heavy (Go binary
 * dep, OIDC flows, transparency log). For a v1, "the index author
 * publishes the hash and we verify on install" gets us the same
 * tamper-evidence as long as the index host is trusted. Cosign can
 * land later as an optional layer above the sha256 check.
 *
 * The CLI surface (in `cli.ts`):
 *   - `claw-squad skills list`            -- show installed + available
 *   - `claw-squad skills install <id>`    -- fetch by id from registry
 *   - `claw-squad skills install <url>`   -- ad-hoc install of a manifest URL
 *   - `claw-squad skills uninstall <id>`  -- remove from .claw-squad/skills/
 */
import { createHash, createPublicKey, verify as cryptoVerify } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, readdirSync, unlinkSync, writeFileSync } from "node:fs";
import { join } from "node:path";

import { skillsDir } from "./skills.js";


export const DEFAULT_REGISTRY_URL = "https://skills.claudestruct.dev/index.json";


/** Optional cryptographic signature attached to a manifest.
 *
 * Cosign-compatible offline verification: the signer publishes the
 * skill body once, signs it with their private key, and ships the
 * signature alongside the manifest. We verify before the sha256
 * check so a wrong key fails fast (no point hashing 50 KB of body
 * if the signer is wrong).
 *
 * Why not full cosign-with-Rekor? Cosign's transparency-log flow
 * needs network access at install time and pulls in the cosign Go
 * binary. The PEM-based offline verification implemented here gets
 * us the same tamper-evidence as long as the install host trusts
 * the configured public key. The Rekor layer can land later.
 */
export interface SkillSignature {
  /** "ed25519" (preferred — small keys, fast verify) or
   *  "rsa-pss-sha256" (broader compatibility). */
  algorithm: "ed25519" | "rsa-pss-sha256";
  /** PEM-encoded public key (SPKI). */
  publicKeyPem: string;
  /** Base64-encoded signature over the raw skill body bytes. */
  signatureBase64: string;
}


export interface SkillManifest {
  id: string;
  version: string;
  description: string;
  url: string;
  sha256: string;
  applyTo?: string[];
  license?: string;
  homepage?: string;
  publishedAt?: string;
  /** Optional cosign-style signature. When present we verify before
   * the sha256 check; when absent, sha256 alone is the integrity
   * signal (operators can opt-in to required-signature mode via
   * `CLAW_SKILLS_REQUIRE_SIGNATURE=1`). */
  signature?: SkillSignature;
}


/** Validate an unknown value as a SkillManifest. Returns the manifest
 * on success, or an error string. Never throws. */
export function parseManifest(raw: unknown): SkillManifest | string {
  if (!raw || typeof raw !== "object") return "manifest is not an object";
  const m = raw as Record<string, unknown>;
  for (const k of ["id", "version", "description", "url", "sha256"]) {
    if (typeof m[k] !== "string" || !m[k]) {
      return `manifest field "${k}" is missing or not a string`;
    }
  }
  if (m.applyTo !== undefined) {
    if (!Array.isArray(m.applyTo) || !m.applyTo.every((g) => typeof g === "string")) {
      return `manifest field "applyTo" must be a list of strings`;
    }
  }
  // sha256 is hex-encoded SHA-256 = 64 hex chars.
  if (!/^[a-f0-9]{64}$/i.test(m.sha256 as string)) {
    return `manifest sha256 must be 64 hex chars`;
  }
  // Optional signature block. Validate shape; we don't verify here —
  // verification happens at install time against the body.
  let signature: SkillSignature | undefined;
  if (m.signature !== undefined) {
    if (!m.signature || typeof m.signature !== "object") {
      return `manifest field "signature" must be an object`;
    }
    const s = m.signature as Record<string, unknown>;
    if (s.algorithm !== "ed25519" && s.algorithm !== "rsa-pss-sha256") {
      return `manifest signature.algorithm must be "ed25519" or "rsa-pss-sha256"`;
    }
    if (typeof s.publicKeyPem !== "string" || !s.publicKeyPem.includes("BEGIN PUBLIC KEY")) {
      return `manifest signature.publicKeyPem must be a PEM-encoded public key`;
    }
    if (typeof s.signatureBase64 !== "string" || !s.signatureBase64) {
      return `manifest signature.signatureBase64 must be a non-empty base64 string`;
    }
    signature = {
      algorithm: s.algorithm,
      publicKeyPem: s.publicKeyPem,
      signatureBase64: s.signatureBase64,
    };
  }
  return {
    id: m.id as string,
    version: m.version as string,
    description: m.description as string,
    url: m.url as string,
    sha256: (m.sha256 as string).toLowerCase(),
    applyTo: m.applyTo as string[] | undefined,
    license: typeof m.license === "string" ? m.license : undefined,
    homepage: typeof m.homepage === "string" ? m.homepage : undefined,
    publishedAt: typeof m.publishedAt === "string" ? m.publishedAt : undefined,
    signature,
  };
}


/** Verify a cosign-style signature against the raw skill body bytes.
 *
 * Returns null on success, or a human-readable error string on
 * failure. We never throw — callers decide whether to abort install
 * or fall back to sha256-only.
 */
export function verifySkillSignature(
  body: string,
  signature: SkillSignature,
): string | null {
  let publicKey;
  try {
    publicKey = createPublicKey(signature.publicKeyPem);
  } catch (err) {
    return `signature publicKeyPem could not be parsed: ${(err as Error).message}`;
  }
  let sigBytes: Buffer;
  try {
    sigBytes = Buffer.from(signature.signatureBase64, "base64");
    if (sigBytes.length === 0) {
      return "signatureBase64 decoded to zero bytes";
    }
  } catch (err) {
    return `signatureBase64 could not be decoded: ${(err as Error).message}`;
  }
  const bodyBytes = Buffer.from(body, "utf-8");
  // ed25519 takes algorithm=null (the algorithm is implicit in the
  // key). RSA-PSS-SHA256 takes 'sha256' + the salt-length default.
  const algForVerify = signature.algorithm === "ed25519" ? null : "sha256";
  let ok = false;
  try {
    if (signature.algorithm === "rsa-pss-sha256") {
      // Must explicitly request PSS padding; the default for an RSA
      // key is PKCS#1 v1.5 which would silently accept a different
      // signature scheme.
      ok = cryptoVerify(
        algForVerify,
        bodyBytes,
        {
          key: publicKey,
          padding: 6, // crypto.constants.RSA_PKCS1_PSS_PADDING
        },
        sigBytes,
      );
    } else {
      ok = cryptoVerify(algForVerify, bodyBytes, publicKey, sigBytes);
    }
  } catch (err) {
    return `signature verification crashed: ${(err as Error).message}`;
  }
  if (!ok) {
    return `signature did not verify against the provided publicKeyPem`;
  }
  return null;
}


/** Read a JSON list of manifests from disk or HTTPS. The fetcher is
 * injected so tests can stub it; default is `globalThis.fetch`. */
export async function loadRegistryIndex(
  url: string,
  fetcher: typeof fetch = fetch,
): Promise<{ manifests: SkillManifest[]; warnings: string[] }> {
  let body: string;
  if (url.startsWith("file://") || url.startsWith("/")) {
    const path = url.startsWith("file://") ? url.slice("file://".length) : url;
    body = readFileSync(path, "utf-8");
  } else {
    const res = await fetcher(url);
    if (!res.ok) {
      throw new Error(`registry GET ${url} failed: ${res.status} ${res.statusText}`);
    }
    body = await res.text();
  }
  const parsed = JSON.parse(body);
  const list = Array.isArray(parsed) ? parsed : parsed.manifests;
  if (!Array.isArray(list)) {
    throw new Error(`registry index has no manifests list`);
  }
  const manifests: SkillManifest[] = [];
  const warnings: string[] = [];
  for (const item of list) {
    const result = parseManifest(item);
    if (typeof result === "string") {
      warnings.push(`skipped invalid manifest: ${result}`);
      continue;
    }
    manifests.push(result);
  }
  return { manifests, warnings };
}


/** Compute sha256 hex of a string, matching the manifest format. */
export function sha256Hex(body: string): string {
  return createHash("sha256").update(body, "utf-8").digest("hex");
}


export interface InstallResult {
  manifest: SkillManifest;
  installedPath: string;
}


/** Fetch the .md body, verify the sha256 (and optional cosign-style
 * signature) match the manifest, and write to
 * `<repoRoot>/.claw-squad/skills/<id>.md`. Throws on any mismatch —
 * silent corruption would defeat the whole point.
 *
 * When `process.env.CLAW_SKILLS_REQUIRE_SIGNATURE=1` is set, an
 * unsigned manifest is rejected before any IO happens. This is the
 * lever air-gapped / regulated environments flip when sha256 alone
 * isn't sufficient (the registry author could swap both the body
 * and the manifest hash; only a private key they don't have can't
 * fake a signature).
 */
export async function installSkill(
  repoRoot: string,
  manifest: SkillManifest,
  fetcher: typeof fetch = fetch,
): Promise<InstallResult> {
  if (
    process.env.CLAW_SKILLS_REQUIRE_SIGNATURE === "1" &&
    !manifest.signature
  ) {
    throw new Error(
      `skill "${manifest.id}" has no signature but CLAW_SKILLS_REQUIRE_SIGNATURE=1 ` +
      `is set. Refusing to install (sha256 alone is insufficient when the registry ` +
      `host isn't trusted).`,
    );
  }
  let body: string;
  if (manifest.url.startsWith("file://") || manifest.url.startsWith("/")) {
    const path = manifest.url.startsWith("file://")
      ? manifest.url.slice("file://".length)
      : manifest.url;
    body = readFileSync(path, "utf-8");
  } else {
    const res = await fetcher(manifest.url);
    if (!res.ok) {
      throw new Error(`skill body GET ${manifest.url} failed: ${res.status} ${res.statusText}`);
    }
    body = await res.text();
  }
  // Signature first: a wrong key fails fast before we hash a 50 KB
  // body, and the error message tells the operator the actual
  // problem (key drift) rather than just "sha256 mismatch".
  if (manifest.signature) {
    const sigErr = verifySkillSignature(body, manifest.signature);
    if (sigErr) {
      throw new Error(
        `signature verification failed for skill "${manifest.id}": ${sigErr}. ` +
        `Refusing to install (key drift, tampered body, or wrong manifest).`,
      );
    }
  }
  const observed = sha256Hex(body);
  if (observed !== manifest.sha256.toLowerCase()) {
    throw new Error(
      `sha256 mismatch for skill "${manifest.id}": manifest=${manifest.sha256}, ` +
      `observed=${observed}. Refusing to install (tampered manifest, stale URL, or wrong file).`,
    );
  }
  const dir = skillsDir(repoRoot);
  mkdirSync(dir, { recursive: true });
  const path = join(dir, `${manifest.id}.md`);
  writeFileSync(path, body, "utf-8");
  // Sidecar JSON keeps the manifest metadata next to the .md so
  // `skills list` can show provenance later without re-fetching.
  writeFileSync(`${path}.manifest.json`, JSON.stringify(manifest, null, 2), "utf-8");
  return { manifest, installedPath: path };
}


/** Remove a skill (and its sidecar) by id. Returns true if anything
 * was removed, false if nothing matched. */
export function uninstallSkill(repoRoot: string, id: string): boolean {
  const dir = skillsDir(repoRoot);
  if (!existsSync(dir)) return false;
  const md = join(dir, `${id}.md`);
  const sidecar = `${md}.manifest.json`;
  let removed = false;
  for (const path of [md, sidecar]) {
    if (existsSync(path)) {
      try {
        unlinkSync(path);
        removed = true;
      } catch {
        // Best-effort: tolerate concurrent removals / read-only mounts.
      }
    }
  }
  return removed;
}


/** List installed skills with their sidecar manifests when available. */
export function listInstalled(repoRoot: string): {
  id: string;
  manifest: SkillManifest | null;
  path: string;
}[] {
  const dir = skillsDir(repoRoot);
  if (!existsSync(dir)) return [];
  const out: { id: string; manifest: SkillManifest | null; path: string }[] = [];
  for (const entry of readdirSync(dir)) {
    if (!entry.endsWith(".md")) continue;
    const path = join(dir, entry);
    const id = entry.slice(0, -".md".length);
    const sidecar = `${path}.manifest.json`;
    let manifest: SkillManifest | null = null;
    if (existsSync(sidecar)) {
      try {
        const raw = JSON.parse(readFileSync(sidecar, "utf-8"));
        const parsed = parseManifest(raw);
        if (typeof parsed !== "string") manifest = parsed;
      } catch {
        // Tolerate a malformed sidecar — the .md is still installed.
      }
    }
    out.push({ id, manifest, path });
  }
  return out.sort((a, b) => a.id.localeCompare(b.id));
}
