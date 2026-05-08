/**
 * Tests for the skills marketplace (W7.3).
 *
 * Stubs the fetcher with deterministic responses so the suite never
 * touches the network. Round-trips installs through real on-disk
 * `.claw-squad/skills/<id>.md` writes in temp directories.
 */
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import {
  installSkill,
  listInstalled,
  loadRegistryIndex,
  parseManifest,
  sha256Hex,
  uninstallSkill,
  verifySkillSignature,
} from "../src/skills-registry.js";


function fakeFetch(routes: Record<string, string | { status: number }>): typeof fetch {
  return (async (url: string | URL | Request): Promise<Response> => {
    const key = url.toString();
    const value = routes[key];
    if (value === undefined) {
      return new Response("not found", { status: 404 });
    }
    if (typeof value === "object" && "status" in value) {
      return new Response("", { status: value.status });
    }
    return new Response(value, { status: 200 });
  }) as unknown as typeof fetch;
}


describe("parseManifest", () => {
  const valid = {
    id: "py-test",
    version: "1.0.0",
    description: "...",
    url: "https://example/py-test.md",
    sha256: "a".repeat(64),
  };

  it("accepts a minimal valid manifest", () => {
    const out = parseManifest(valid);
    expect(typeof out).toBe("object");
    if (typeof out !== "string") {
      expect(out.id).toBe("py-test");
      expect(out.sha256).toBe("a".repeat(64));
    }
  });

  it("rejects non-objects", () => {
    expect(parseManifest(null)).toMatch(/not an object/);
    expect(parseManifest("manifest")).toMatch(/not an object/);
  });

  it("rejects when a required field is missing", () => {
    const { description, ...without } = valid;
    expect(parseManifest(without)).toMatch(/description/);
  });

  it("rejects when sha256 is the wrong length", () => {
    expect(parseManifest({ ...valid, sha256: "deadbeef" })).toMatch(/64 hex/);
  });

  it("rejects when applyTo contains non-strings", () => {
    expect(parseManifest({ ...valid, applyTo: [1, "x"] })).toMatch(/applyTo/);
  });

  it("normalizes the sha256 to lowercase", () => {
    const out = parseManifest({ ...valid, sha256: "A".repeat(64) });
    if (typeof out !== "string") {
      expect(out.sha256).toBe("a".repeat(64));
    }
  });

  it("preserves optional fields", () => {
    const out = parseManifest({
      ...valid,
      applyTo: ["test_*.py"],
      license: "MIT",
      homepage: "https://example.com",
    });
    if (typeof out !== "string") {
      expect(out.applyTo).toEqual(["test_*.py"]);
      expect(out.license).toBe("MIT");
    }
  });
});


describe("loadRegistryIndex", () => {
  it("loads from a file:// URL", async () => {
    const root = mkdtempSync(join(tmpdir(), "skills-idx-"));
    const indexPath = join(root, "index.json");
    writeFileSync(
      indexPath,
      JSON.stringify([
        {
          id: "py-test", version: "1.0.0", description: "...",
          url: "https://example/py-test.md", sha256: "a".repeat(64),
        },
      ]),
      "utf-8",
    );
    const { manifests, warnings } = await loadRegistryIndex(`file://${indexPath}`);
    expect(manifests).toHaveLength(1);
    expect(warnings).toEqual([]);
    rmSync(root, { recursive: true, force: true });
  });

  it("loads from an https URL via the injected fetcher", async () => {
    const fetcher = fakeFetch({
      "https://r/index.json": JSON.stringify([
        {
          id: "py-test", version: "1.0.0", description: "...",
          url: "https://example/py-test.md", sha256: "b".repeat(64),
        },
      ]),
    });
    const { manifests } = await loadRegistryIndex("https://r/index.json", fetcher);
    expect(manifests[0].id).toBe("py-test");
  });

  it("accepts both [..] and {manifests: [..]} index shapes", async () => {
    const fetcher = fakeFetch({
      "https://r/wrapped.json": JSON.stringify({
        manifests: [
          {
            id: "x", version: "1", description: "...",
            url: "https://e/x.md", sha256: "c".repeat(64),
          },
        ],
      }),
    });
    const { manifests } = await loadRegistryIndex("https://r/wrapped.json", fetcher);
    expect(manifests).toHaveLength(1);
  });

  it("collects warnings for invalid manifests but keeps loading the rest", async () => {
    const fetcher = fakeFetch({
      "https://r/index.json": JSON.stringify([
        { id: "ok", version: "1", description: "...",
          url: "https://e/ok.md", sha256: "d".repeat(64) },
        { id: "bad" /* missing fields */ },
      ]),
    });
    const { manifests, warnings } = await loadRegistryIndex(
      "https://r/index.json", fetcher,
    );
    expect(manifests.map((m) => m.id)).toEqual(["ok"]);
    expect(warnings).toHaveLength(1);
  });

  it("throws on non-2xx HTTP", async () => {
    const fetcher = fakeFetch({ "https://r/index.json": { status: 500 } });
    await expect(
      loadRegistryIndex("https://r/index.json", fetcher),
    ).rejects.toThrow(/500/);
  });
});


describe("installSkill", () => {
  let root: string;
  beforeEach(() => {
    root = mkdtempSync(join(tmpdir(), "skill-install-"));
  });
  afterEach(() => {
    rmSync(root, { recursive: true, force: true });
  });

  it("verifies sha256 and writes the .md plus sidecar manifest", async () => {
    const body = "# python testing\n\nUse pytest.\n";
    const manifest = {
      id: "py-test", version: "1.0.0", description: "...",
      url: "https://e/py-test.md", sha256: sha256Hex(body),
    };
    const fetcher = fakeFetch({ [manifest.url]: body });
    const res = await installSkill(root, manifest, fetcher);
    expect(res.installedPath).toMatch(/\.claw-squad\/skills\/py-test\.md$/);
    expect(readFileSync(res.installedPath, "utf-8")).toBe(body);
    const sidecar = readFileSync(`${res.installedPath}.manifest.json`, "utf-8");
    expect(JSON.parse(sidecar).sha256).toBe(manifest.sha256);
  });

  it("rejects on sha256 mismatch", async () => {
    const body = "tampered body";
    const manifest = {
      id: "py-test", version: "1.0.0", description: "...",
      url: "https://e/py-test.md",
      sha256: sha256Hex("the original body"),
    };
    const fetcher = fakeFetch({ [manifest.url]: body });
    await expect(installSkill(root, manifest, fetcher)).rejects.toThrow(/sha256 mismatch/);
  });

  it("supports file:// urls for air-gapped installs", async () => {
    const body = "local skill";
    const localPath = join(root, "local.md");
    writeFileSync(localPath, body, "utf-8");
    const manifest = {
      id: "local", version: "1.0.0", description: "...",
      url: `file://${localPath}`, sha256: sha256Hex(body),
    };
    const res = await installSkill(root, manifest);
    expect(readFileSync(res.installedPath, "utf-8")).toBe(body);
  });
});


// --- Cosign-style signature verification (W7.3 follow-up) -----------

describe("verifySkillSignature", () => {
  // Generate a real ed25519 keypair once per describe — using stdlib
  // crypto so the test suite stays free of any signing tools. Sign a
  // known body, then assert verifySkillSignature accepts the right
  // body and rejects everything else.
  const { generateKeyPairSync, sign: cryptoSign } = require("node:crypto");
  const body = "# python testing\n\nUse pytest.\n";

  it("accepts a valid ed25519 signature", () => {
    const { publicKey, privateKey } = generateKeyPairSync("ed25519");
    const sig = cryptoSign(null, Buffer.from(body, "utf-8"), privateKey);
    const err = verifySkillSignature(body, {
      algorithm: "ed25519",
      publicKeyPem: publicKey.export({ type: "spki", format: "pem" }) as string,
      signatureBase64: sig.toString("base64"),
    });
    expect(err).toBeNull();
  });

  it("rejects when the body is tampered (signature stops matching)", () => {
    const { publicKey, privateKey } = generateKeyPairSync("ed25519");
    const sig = cryptoSign(null, Buffer.from(body, "utf-8"), privateKey);
    const err = verifySkillSignature("# tampered\n", {
      algorithm: "ed25519",
      publicKeyPem: publicKey.export({ type: "spki", format: "pem" }) as string,
      signatureBase64: sig.toString("base64"),
    });
    expect(err).toMatch(/did not verify/);
  });

  it("rejects when the public key is from a different signer", () => {
    const { privateKey: signerKey } = generateKeyPairSync("ed25519");
    const { publicKey: attackerPub } = generateKeyPairSync("ed25519");
    const sig = cryptoSign(null, Buffer.from(body, "utf-8"), signerKey);
    const err = verifySkillSignature(body, {
      algorithm: "ed25519",
      publicKeyPem: attackerPub.export({ type: "spki", format: "pem" }) as string,
      signatureBase64: sig.toString("base64"),
    });
    expect(err).toMatch(/did not verify/);
  });

  it("rejects a malformed publicKeyPem cleanly (no crash)", () => {
    const err = verifySkillSignature(body, {
      algorithm: "ed25519",
      publicKeyPem: "-----BEGIN PUBLIC KEY-----\nnot-a-real-key\n-----END PUBLIC KEY-----",
      signatureBase64: Buffer.from("garbage").toString("base64"),
    });
    expect(err).toMatch(/publicKeyPem could not be parsed|did not verify/);
  });

  it("rejects an empty signatureBase64", () => {
    const { publicKey } = generateKeyPairSync("ed25519");
    const err = verifySkillSignature(body, {
      algorithm: "ed25519",
      publicKeyPem: publicKey.export({ type: "spki", format: "pem" }) as string,
      signatureBase64: "",
    });
    expect(err).toMatch(/zero bytes|decoded/);
  });

  it("verifies an RSA-PSS-SHA256 signature too", () => {
    const { publicKey, privateKey } = generateKeyPairSync("rsa", {
      modulusLength: 2048,
    });
    const sig = cryptoSign(
      "sha256",
      Buffer.from(body, "utf-8"),
      { key: privateKey, padding: 6 /* RSA_PKCS1_PSS_PADDING */ },
    );
    const err = verifySkillSignature(body, {
      algorithm: "rsa-pss-sha256",
      publicKeyPem: publicKey.export({ type: "spki", format: "pem" }) as string,
      signatureBase64: sig.toString("base64"),
    });
    expect(err).toBeNull();
  });
});


describe("parseManifest signature shape", () => {
  const validBody = {
    id: "x", version: "1.0.0", description: "...",
    url: "https://e/x.md", sha256: "a".repeat(64),
  };

  it("accepts a manifest without a signature (back-compat)", () => {
    const out = parseManifest(validBody);
    expect(typeof out).toBe("object");
  });

  it("accepts a manifest with a well-formed signature block", () => {
    const out = parseManifest({
      ...validBody,
      signature: {
        algorithm: "ed25519",
        publicKeyPem: "-----BEGIN PUBLIC KEY-----\nMCowBQYDK2VwAyEA...\n-----END PUBLIC KEY-----",
        signatureBase64: "abcd",
      },
    });
    expect(typeof out).toBe("object");
    if (typeof out !== "string") {
      expect(out.signature?.algorithm).toBe("ed25519");
    }
  });

  it("rejects an unknown signature algorithm", () => {
    const out = parseManifest({
      ...validBody,
      signature: {
        algorithm: "secp256k1",
        publicKeyPem: "-----BEGIN PUBLIC KEY-----\nx\n-----END PUBLIC KEY-----",
        signatureBase64: "abcd",
      },
    });
    expect(typeof out).toBe("string");
    expect(out).toMatch(/algorithm/);
  });

  it("rejects a publicKeyPem that isn't PEM-shaped", () => {
    const out = parseManifest({
      ...validBody,
      signature: {
        algorithm: "ed25519",
        publicKeyPem: "not-a-pem-block",
        signatureBase64: "abcd",
      },
    });
    expect(typeof out).toBe("string");
    expect(out).toMatch(/PEM/);
  });
});


describe("installSkill with signatures", () => {
  let root: string;
  beforeEach(() => {
    root = mkdtempSync(join(tmpdir(), "skill-sig-"));
  });
  afterEach(() => {
    rmSync(root, { recursive: true, force: true });
    delete process.env.CLAW_SKILLS_REQUIRE_SIGNATURE;
  });

  it("installs a signed skill when the signature verifies", async () => {
    const { generateKeyPairSync, sign } = require("node:crypto");
    const { publicKey, privateKey } = generateKeyPairSync("ed25519");
    const body = "# signed skill\n";
    const sig = sign(null, Buffer.from(body, "utf-8"), privateKey);
    const manifest = {
      id: "signed", version: "1.0.0", description: "...",
      url: "https://e/signed.md",
      sha256: sha256Hex(body),
      signature: {
        algorithm: "ed25519" as const,
        publicKeyPem: publicKey.export({ type: "spki", format: "pem" }) as string,
        signatureBase64: sig.toString("base64"),
      },
    };
    const fetcher = fakeFetch({ [manifest.url]: body });
    const res = await installSkill(root, manifest, fetcher);
    expect(res.installedPath).toMatch(/signed\.md$/);
  });

  it("refuses to install when the signature is wrong", async () => {
    const { generateKeyPairSync, sign } = require("node:crypto");
    const { privateKey } = generateKeyPairSync("ed25519");
    const { publicKey: otherPub } = generateKeyPairSync("ed25519");
    const body = "# signed skill\n";
    const sig = sign(null, Buffer.from(body, "utf-8"), privateKey);
    // publicKey is from a *different* keypair → verification fails.
    const manifest = {
      id: "signed", version: "1.0.0", description: "...",
      url: "https://e/signed.md",
      sha256: sha256Hex(body),
      signature: {
        algorithm: "ed25519" as const,
        publicKeyPem: otherPub.export({ type: "spki", format: "pem" }) as string,
        signatureBase64: sig.toString("base64"),
      },
    };
    const fetcher = fakeFetch({ [manifest.url]: body });
    await expect(installSkill(root, manifest, fetcher)).rejects.toThrow(
      /signature verification failed/,
    );
  });

  it("CLAW_SKILLS_REQUIRE_SIGNATURE=1 rejects unsigned manifests", async () => {
    process.env.CLAW_SKILLS_REQUIRE_SIGNATURE = "1";
    const body = "# unsigned\n";
    const manifest = {
      id: "unsigned", version: "1.0.0", description: "...",
      url: "https://e/unsigned.md", sha256: sha256Hex(body),
    };
    const fetcher = fakeFetch({ [manifest.url]: body });
    await expect(installSkill(root, manifest, fetcher)).rejects.toThrow(
      /no signature/,
    );
  });

  it("CLAW_SKILLS_REQUIRE_SIGNATURE unset still permits unsigned (default)", async () => {
    const body = "# unsigned but pre-signature env\n";
    const manifest = {
      id: "unsigned", version: "1.0.0", description: "...",
      url: "https://e/unsigned.md", sha256: sha256Hex(body),
    };
    const fetcher = fakeFetch({ [manifest.url]: body });
    const res = await installSkill(root, manifest, fetcher);
    expect(res.installedPath).toMatch(/unsigned\.md$/);
  });
});


describe("uninstallSkill + listInstalled", () => {
  let root: string;
  beforeEach(() => {
    root = mkdtempSync(join(tmpdir(), "skill-list-"));
  });
  afterEach(() => {
    rmSync(root, { recursive: true, force: true });
  });

  it("returns empty when no skills are installed", () => {
    expect(listInstalled(root)).toEqual([]);
  });

  it("lists installed skills, with manifest when sidecar is present", async () => {
    const dir = join(root, ".claw-squad", "skills");
    mkdirSync(dir, { recursive: true });
    writeFileSync(join(dir, "a.md"), "hi", "utf-8");
    writeFileSync(join(dir, "b.md"), "hi", "utf-8");
    writeFileSync(
      join(dir, "a.md.manifest.json"),
      JSON.stringify({
        id: "a", version: "1.0", description: "...",
        url: "https://x", sha256: "f".repeat(64),
      }),
      "utf-8",
    );
    const installed = listInstalled(root);
    expect(installed.map((i) => i.id)).toEqual(["a", "b"]);
    expect(installed[0].manifest?.id).toBe("a");
    expect(installed[1].manifest).toBeNull();
  });

  it("uninstall removes both the .md and the sidecar; returns false when absent", async () => {
    const body = "skill body";
    const manifest = {
      id: "rm-me", version: "1.0.0", description: "...",
      url: "https://e/rm.md", sha256: sha256Hex(body),
    };
    const fetcher = fakeFetch({ [manifest.url]: body });
    await installSkill(root, manifest, fetcher);
    expect(uninstallSkill(root, "rm-me")).toBe(true);
    expect(uninstallSkill(root, "rm-me")).toBe(false);
    expect(uninstallSkill(root, "never-existed")).toBe(false);
  });
});
