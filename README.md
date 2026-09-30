# Entrypoint

A single-file, offline dropper & reverse-shell build system for **authorized penetration
testing, red-team engagements and adversary simulation only**.

- **`forge.py` — desktop GUI** (Tkinter, Python stdlib only). Pick a payload →
  LHOST/LPORT → injection / PE-injection / encryption / evasion → Generate.
  Wraps `msfvenom` for **Windows, Linux and macOS** payloads, or embeds a custom
  C2 implant file (Sliver / Havoc / Mythic / Mettle raw output).

```
entrypoint/
├── forge.py              # desktop GUI app (pure stdlib)
├── offsec-setup.sh       # one-shot offensive tooling installer (Arch/Kali)
├── requirements.txt      # explains the zero pip deps + system packages
├── LICENSE               # MIT + authorized-use-only notice
└── README.md
```

## Quick start

```bash
# Kali / Debian
sudo apt install python3-tk metasploit-framework gcc-mingw-w64 openssl
python3 forge.py            # GUI (authorization gate first)
python3 forge.py --selftest # offline validation, no msfvenom needed
```

**Payload Type** — `MSFvenom` (any payload in the list, Windows/Linux/macOS),
or `Custom (C2 implant)` pointing at a raw shellcode / `.so` / `.exe` file from
your C2 (Sliver, Havoc, Mythic Athena/Apollo, Mettle). You can also import
msfvenom shellcode directly (`payload.c` / hex / raw `.bin`) — it is parsed,
key-XOR-encrypted and embedded in the generated loader.

**Process Injection (T1055)** — CurrentThread · Remote Process ·
Process Hollowing (T1055.012) · APC Injection (T1055.004); on Linux the
injector route uses ptrace (T1055.008).

**PE Injection (T1055.002) — true file embedding** — tick the box and pick a
host executable (e.g. putty.exe): forge appends a `.entp` section to the host
PE containing the XOR-encrypted shellcode plus an architecture-matched
(x64 or x86) entry stub, and
repoints the entry. The stub decrypts the stage in place, resolves
`CreateThread` via a PEB→Ldr→kernel32 export-table walk (ASLR-proof,
no relocations), fires the payload on a second thread, then jumps to the
original entry point. **The output IS the host program** — same name, icon,
version info — it opens and runs normally while the payload executes beside
it. One file in, one file out: deliver the patched executable, no separate
loader. x64 and x86 hosts; no extra dependencies — the entry stub ships pre-assembled.

**Encryption** — XOR Dynamic (rolling per-build key) · RC4 · AES-CTR (openssl,
Bcrypt/CryptoAPI in the loader). **Sandbox Evasion** — pre-exec sleep with ±35%
jitter (T1497.003). **x64** toggle picks the mingw cross-compiler arch.

**Host personas** — built-in preset list of common host executables, grouped
by architecture, so a patched build looks like a program users already trust.

Every build lands in `~/.entrypoint/builds/EP-XXXXXX-<target>/` with
`loader.c`, `build.sh`, `manifest.json` (keys, MITRE map, sizes) and, when a
cross-compiler is on PATH, the finished binary. Each build re-rolls its key and
identifiers — never ship the same file twice.

## Validation

`python3 forge.py --selftest` runs an offline check suite — loader templates,
crypto round-trips (XOR / RC4 / AES), PE-embed slot layout for both stub
architectures, manifest and deliverable integrity — without needing msfvenom
or a GUI.

The macOS loader is a C file you build with `clang -arch x86_64|arm64` (+ optional
`codesign`); the Android output is a Java/JNI project assembled by its `build.sh`
(SDK + NDK). The C# output is a template you compile with
`csc.exe /platform:x64 /optimize+ /out:client.exe Program.cs`.

## Rules of engagement

- **Authorized targets only.** Written authorization is a prerequisite, not a suggestion.
- Generate a **unique build per deployment** — never reuse payload files.
- Test every build in your own lab first; each AV/EDR behaves differently.
- **Remove persistence and clean up** at the end of the engagement. Leftover scheduled
  tasks / Run keys are findings in your own report.
- These techniques are all publicly documented tradecraft (MITRE ATT&CK). They are
  **detectable** — evasion is a race, not a win condition.

## Disclaimer

For educational and defensive-research purposes. Deploying payloads without authorization
is illegal in most jurisdictions. The author accepts no liability for misuse.
