#!/usr/bin/env python3
"""
GoW PS2 Model Tool v1.0  (God of War 1 & 2, PS2)

Alur: Buka ISO -> klik panah di samping PARTx.PAK -> daftar WAD (hanya yang ada model)
      -> panah WAD -> daftar model -> Ekstrak / Replace (glTF 2.0)

Replace:
  - glTF = model asli yang bentuknya diedit (topologi sama) -> vertex digeser di tempat.
  - selain itu (model baru) -> semua segitiga dari glTF dipakai persis, mengisi ulang slot vertex model;
    harus muat di kapasitas slot model aslinya (ukuran WAD tidak berubah).

Disembunyikan:
  - file non-WAD di PAK : VPK, PSS, PSW, VAG, TXT
  - WAD tanpa model, WAD SBK/SBP, WAD bahasa
  - di dalam WAD       : texture (GFX/PAL/TXR/SEM), SBK/SBP, bahasa, dan semua entry non-model

Hanya butuh Python 3.8+ (tkinter bawaan). Jalan di Windows dan Wine.
"""
import os, re, sys, json, base64, struct, traceback, threading

SECTOR = 2048
LOG = []


def log(*a):
    LOG.append(" ".join(str(x) for x in a))


# ======================================================================
# ISO9660
# ======================================================================
class ISO:
    def __init__(self, path):
        self.path = path
        self.f = open(path, "rb")
        self.ssz = self.off = None
        for ssz, off in ((2048, 0), (2352, 16), (2336, 8)):
            self.f.seek(16 * ssz + off)
            if self.f.read(6)[1:6] == b"CD001":
                self.ssz, self.off = ssz, off
                break
        if self.ssz is None:
            raise ValueError("Bukan ISO9660 yang valid")
        self.files = {}
        pvd = self.sectors(16, 1)
        root = pvd[156:190]
        self._walk(struct.unpack("<I", root[2:6])[0], struct.unpack("<I", root[10:14])[0], "")

    def sectors(self, lba, count):
        if self.ssz == 2048:
            self.f.seek(lba * 2048)
            return self.f.read(count * 2048)
        out = []
        for i in range(count):
            self.f.seek((lba + i) * self.ssz + self.off)
            out.append(self.f.read(2048))
        return b"".join(out)

    def _walk(self, lba, size, prefix):
        data = self.sectors(lba, (size + 2047) // 2048)
        p = 0
        while p < len(data):
            ln = data[p]
            if ln == 0:
                p = (p // 2048 + 1) * 2048
                continue
            rec = data[p:p + ln]
            p += ln
            elba, esize = struct.unpack("<I", rec[2:6])[0], struct.unpack("<I", rec[10:14])[0]
            flags, nlen = rec[25], rec[32]
            name = rec[33:33 + nlen]
            if name in (b"\x00", b"\x01"):
                continue
            name = name.decode("ascii", "replace").split(";")[0]
            if flags & 2:
                self._walk(elba, esize, prefix + name + "/")
            else:
                self.files[(prefix + name).upper()] = (elba, esize)

    # --- antarmuka "Disc"
    def find(self, name):
        for k, v in self.files.items():
            if k.split("/")[-1] == name.upper():
                return v
        return None

    def toc_bytes(self):
        v = self.find("GODOFWAR.TOC")
        return self.read_range(v[0], 0, v[1]) if v else None

    def paks(self):
        r = {}
        for k, v in self.files.items():
            n = k.split("/")[-1]
            if re.fullmatch(r"PART\d+\.PAK", n):
                r[n] = v[1]
        return r

    def read_pak(self, pak, offset, length):
        v = self.find(pak)
        if not v or offset < 0 or offset >= v[1]:
            return b""
        return self.read_range(v[0], offset, min(length, v[1] - offset))

    def write_pak(self, pak, offset, data):
        """Tulis langsung ke ISO (hanya ISO 2048-byte/sektor biasa). Ukuran data tidak berubah."""
        if self.ssz != 2048:
            raise ValueError("Replace hanya didukung untuk ISO 2048 byte/sektor (bukan .bin raw)")
        v = self.find(pak)
        if not v or offset + len(data) > v[1]:
            raise ValueError("Lokasi tulis di luar PAK")
        with open(self.path, "r+b") as w:
            w.seek(v[0] * 2048 + offset)
            w.write(data)
        self.f.close()
        self.f = open(self.path, "rb")

    def read_range(self, lba, offset, length):
        first = offset // 2048
        last = (offset + length + 2047) // 2048
        buf = self.sectors(lba + first, last - first)
        s = offset - first * 2048
        return buf[s:s + length]

    def scan_pak(self, pak, magic_tags, progress=None):
        """Cari semua sektor yang diawali header WAD (tag, size, 'WAD_...'). Return list offset byte."""
        v = self.find(pak)
        hits = []
        if not v:
            return hits
        CH = 64 * 1024 * 1024
        pos = 0
        while pos < v[1]:
            chunk = self.read_range(v[0], pos, min(CH, v[1] - pos))
            i = chunk.find(b"WAD_")
            while i != -1:
                s = i - 8
                if s >= 0 and s % 2048 == 0 and struct.unpack_from("<I", chunk, s)[0] in magic_tags:
                    hits.append(pos + s)
                i = chunk.find(b"WAD_", i + 1)
            pos += CH
            if progress:
                progress("Scan %s: %d%%" % (pak, min(100, pos * 100 // v[1])))
        return hits


# ======================================================================
# TOC
# ======================================================================
class TocEntry:
    """Satu file di TOC. locs = daftar salinan [(PARTn.PAK, offset_byte)] (file bisa ada di 2 PAK)."""
    __slots__ = ("name", "size", "locs")

    def __init__(self, name, size):
        self.name, self.size, self.locs = name, size, []


PAK2_BASE = 10000000  # GoW2: nilai offset >= 10.000.000 berarti PART2.PAK (sektor = nilai - 10.000.000)


def parse_toc(d):
    """Return (game, entries). game = 1 atau 2.
    GoW2 : u32 jumlah | entry 0x24 {name[24], size, jumlah_salinan, index} | tabel u32 offset-sektor
    GoW1 : entry 0x18 {name[12], u32 pak(0=PART1,1=PART2), size, offset-sektor}"""
    byname = {}
    if len(d) >= 4:
        n = struct.unpack_from("<I", d, 0)[0]
        tab = 4 + n * 0x24
        if 0 < n < 20000 and tab < len(d) and (len(d) - tab) % 4 == 0:
            T = struct.unpack_from("<%dI" % ((len(d) - tab) // 4), d, tab)
            for i in range(n):
                o = 4 + i * 0x24
                nm = d[o:o + 24].split(b"\0")[0].decode("latin1")
                size, copies, idx = struct.unpack_from("<3I", d, o + 24)
                e = byname.setdefault(nm, TocEntry(nm, size))
                for k in range(copies):
                    if idx + k < len(T):
                        v = T[idx + k]
                        e.locs.append(("PART2.PAK", (v - PAK2_BASE) * SECTOR) if v >= PAK2_BASE
                                      else ("PART1.PAK", v * SECTOR))
            return 2, list(byname.values())
    for i in range(len(d) // 24):
        o = i * 24
        nm = d[o:o + 12].split(b"\0")[0].decode("latin1")
        if not nm:
            continue
        pk, size, off = struct.unpack_from("<3I", d, o + 12)
        e = byname.setdefault(nm, TocEntry(nm, size))
        e.locs.append(("PART%d.PAK" % (pk + 1), off * SECTOR))
    return 1, list(byname.values())


def wad_magic_ok(h):
    return len(h) >= 12 and h[8:12] == b"WAD_"


def verify_toc(ents, disc):
    """Cek sampel: header WAD ('WAD_') harus ada di lokasi hasil TOC. Hasil dicatat di log."""
    wads = [e for e in ents if e.name.upper().endswith(".WAD") and e.locs]
    sample = wads[::max(1, len(wads) // 30)][:30]
    ok = 0
    for e in sample:
        pk, off = e.locs[0]
        if wad_magic_ok(disc.read_pak(pk, off, 32)):
            ok += 1
        else:
            log("lokasi tidak cocok:", e.name, e.locs)
    log("verifikasi TOC: %d/%d WAD sampel valid" % (ok, len(sample)))
    return ok, len(sample)


# ======================================================================
# WAD
# ======================================================================
KNOWN_TAGS = {0x1e, 0x28, 0x32, 0x70, 0x71, 0x72, 0x1001e, 0x3001e, 0x4001e, 0x6001e, 0x7001e, 0x8001e,
              0x2001e, 0x14001e, 0x16001e, 0x19001e, 0x378, 0x29a, 0x3e7,
              1, 2, 3, 9, 0x13, 0x15, 0x16, 0x10001, 0x30001, 0x40001, 0x50001, 0x60001, 0x70001, 0x80001,
              0x170001, 0x1a0001, 0x1d0001}


def _hdr(d, p):
    if p + 0x20 > len(d):
        return None
    tag, size = struct.unpack_from("<II", d, p)
    raw = d[p + 8:p + 0x20]
    nm = raw.split(b"\0")[0]
    if any(c < 32 or c >= 127 for c in nm) or raw[len(nm):].strip(b"\0"):
        return None
    return tag, size, nm.decode("latin1")


def parse_wad(d):
    """Daftar entry: dict(off, tag, size, name). Header 0x20 (tag,size,name[24]), data rata 0x10.
    Jika ketemu data yang bukan header (mis. SND_/SBlk), lompat ke header valid berikutnya."""
    out, p, n = [], 0, len(d)
    while p + 0x20 <= n:
        h = _hdr(d, p)
        if h and h[2] and p + 0x20 + h[1] <= n and not (h[2].startswith("SND_") and h[0] == 0x18):
            out.append({"off": p + 0x20, "tag": h[0], "size": h[1], "name": h[2]})
            p += 0x20 + ((h[1] + 15) & ~15)
            continue
        q = p + 0x10
        while q + 0x20 <= n:
            hh = _hdr(d, q)
            if hh and hh[2] and hh[0] in KNOWN_TAGS and q + 0x20 + hh[1] <= n:
                break
            q += 0x10
        p = q
    return out


LANG_RE = re.compile(r"SBK|SBP|LANG|ENGL|FRENCH|GERMAN|SPANISH|ITALIAN|JAPAN", re.I)
DEF_TAGS = {0x16001e, 0x170001}            # MDL_ (GoW1, GoW2)
GEOM_TAGS = {0x19001e, 0x1a0001}           # blob geometri MDL_x_0 / x_0


def find_models(ents):
    """Model = entry MDL_<nama> + blob geometri (<nama>_0.. atau MDL_<nama>_0..)."""
    models = []
    for e in ents:
        if e["tag"] in DEF_TAGS and e["name"].startswith("MDL_") and e["size"] > 0:
            base = e["name"][4:]
            pat = re.compile(r"^(MDL_)?" + re.escape(base) + r"_\d+$")
            geo = [g for g in ents if g["tag"] in GEOM_TAGS and pat.match(g["name"])]
            mats = []
            for x in ents[ents.index(e) + 1:]:
                if x["name"].startswith("MAT_") and x["size"] == 0:
                    mats.append(x["name"])
                else:
                    break
            models.append({"name": base, "def": e, "geom": geo, "mats": mats, "ents": ents})
    return models


def wad_hidden(name):
    return bool(LANG_RE.search(name))


# ======================================================================
# Decoder mesh GoW (paket VIF PS2)  ->  glTF 2.0
# ======================================================================
# Blob geometri = rangkaian "paket" VIF. Tiap paket: STCYCL, UNPACK
#   V2-16 (UV, 12-bit fixed /4096) | V3-8 (normal /127) | V4-16 (posisi x,y,z int16 /16, w bit15 = ADC)
#   V4-8 (warna vertex RGBA) | V4-32 (GIF tag + bounding sphere) ... lalu MSCAL.
# Vertex berupa triangle strip; vertex ber-ADC (w & 0x8000) tidak menggambar segitiga.
POS_SCALE = 16.0
FS = {0: 4, 1: 2, 2: 1, 4: 8, 5: 4, 6: 2, 8: 12, 9: 6, 10: 3, 12: 16, 13: 8, 14: 4, 15: 2}
STCYCLS = (0x101, 0x102, 0x103, 0x104, 0x204, 0x404)


def _vif_walk(b, p):
    """Baca perintah VIF berurutan. Return (daftar UNPACK, posisi akhir). Berhenti di MSCAL/MSCNT/data tak dikenal."""
    cmds = []
    while p + 4 <= len(b):
        w = struct.unpack_from("<I", b, p)[0]
        imm, num, cmd = w & 0xFFFF, (w >> 16) & 0xFF, w >> 24
        if cmd & 0x60 == 0x60:
            f = cmd & 0xF
            if f not in FS:
                break
            sz = (num * FS[f] + 3) & ~3
            if p + 4 + sz > len(b):
                break
            cmds.append(("U", f, num, p + 4, imm))
            p += 4 + sz
        elif cmd in (0x14, 0x15, 0x17):         # MSCAL / MSCALF / MSCNT -> akhir paket
            p += 4
            break
        elif cmd == 0x20:                        # STMASK (+4 byte)
            p += 8
        elif cmd in (0x30, 0x31):                # STROW / STCOL (+16 byte)
            p += 20
        elif cmd in (0x01, 0x00, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x10, 0x11, 0x13):
            p += 4
        else:
            break
    return cmds, p


def _vif_blocks(b):
    """Cari paket VIF: mulai dari STCYCL, baca perintah (UNPACK, STMOD, STROW, ...) sampai MSCAL."""
    out, p = [], 0
    while p + 8 <= len(b):
        w = struct.unpack_from("<I", b, p)[0]
        if w >> 24 == 0x01 and (w & 0xFFFF) in STCYCLS:
            cmds, e = _vif_walk(b, p)
            if cmds:
                out.append(cmds)
                p = e
                continue
        p += 4
    return out


def decode_blob(b):
    """Return dict: positions, normals, uvs, indices, refs (offset byte tiap atribut di blob, untuk Replace)."""
    pos, nrm, uvs, refs, idx, hasn, grp = [], [], [], [], [], [], []
    heads = seg_headers(b)
    for cmds in _vif_blocks(b):
        A = {5: [], 10: [], 13: [], 14: []}
        for c in cmds:
            if c[1] in A:
                A[c[1]].append(c)
        for gi, pc in enumerate(A[13]):
            n = pc[2]
            uv = A[5][gi] if gi < len(A[5]) and A[5][gi][2] == n else None
            nm = A[10][gi] if gi < len(A[10]) and A[10][gi][2] == n else None
            base = len(pos)
            for i in range(n):
                po = pc[3] + i * 8
                x, y, z, w = struct.unpack_from("<4h", b, po)
                pos.append((x / POS_SCALE, y / POS_SCALE, z / POS_SCALE))
                uo = uv[3] + i * 4 if uv else -1
                no = nm[3] + i * 3 if nm else -1
                uvs.append((struct.unpack_from("<h", b, uo)[0] / 4096.0,
                            struct.unpack_from("<h", b, uo + 2)[0] / 4096.0) if uv else (0.0, 0.0))
                nrm.append(tuple(c / 127.0 for c in struct.unpack_from("<3b", b, no)) if nm else (0.0, 0.0, 1.0))
                refs.append((po, uo, no))
                hasn.append(no >= 0)
                grp.append(max([gp for h, gp in heads if h <= po], default=0))
                k = len(pos) - 1
                if not (w & 0x8000) and k - base >= 2:
                    idx.append((k - 2, k - 1, k) if ((k - base) & 1) == 0 else (k - 1, k - 2, k))
    return {"positions": pos, "normals": nrm, "uvs": uvs, "indices": idx, "refs": refs, "hasn": hasn, "grp": grp}


def orient_triangles(P, N, hasn, I):
    """Arah putar segitiga diselaraskan dengan normal vertex (paritas strip GoW tidak konsisten)."""
    out = []
    for a, b, c in I:
        if hasn[a] and hasn[b] and hasn[c]:
            ux, uy, uz = (P[b][k] - P[a][k] for k in range(3))
            vx, vy, vz = (P[c][k] - P[a][k] for k in range(3))
            n = (uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx)
            d = sum(n[k] * (N[a][k] + N[b][k] + N[c][k]) for k in range(3))
            if d < 0:
                b, c = c, b
        out.append((a, b, c))
    return out


SEG_RE = re.compile(rb"[\x00-\xff]\x00\x00\x00\x02\x00\x00\x00[\x00-\xff]{8}[\x00-\xff]\x00\x00\x00[\x00-\xff]\xff\xff\xff", re.S)


def seg_headers(b):
    """Header 0x50 tiap kelompok mesh: (offset, group). group = indeks material di daftar MAT model."""
    return [(m.start(), struct.unpack_from("<H", b, m.start() + 8)[0])
            for m in SEG_RE.finditer(b) if m.start() % 16 == 0]


def _cstr(b):
    return b.split(b"\0")[0].decode("latin1")


def material_gfx(wad, ents, mat_name):
    """MAT_x -> TXR_y -> (GFX_z, PAL_w). Nama string dicari langsung di data (GoW1 & GoW2)."""
    md = next((e for e in ents if e["name"] == mat_name and e["size"] > 0x40), None)
    if not md:
        return None
    m = re.search(rb"TXR_[\x21-\x7e]+", wad[md["off"]:md["off"] + md["size"]])
    if not m:
        return None
    tn = m.group(0).decode("latin1")
    tx = next((e for e in ents if e["name"] == tn and e["size"] > 0x20), None)
    if not tx:
        return None
    td = wad[tx["off"]:tx["off"] + tx["size"]]
    g = re.search(rb"GFX_[\x21-\x7e]+", td)
    p = re.search(rb"PAL_[\x21-\x7e]+", td)
    if not g:
        return None
    return g.group(0).decode("latin1"), (p.group(0).decode("latin1") if p else None)


def blob_of(wad, g):
    return wad[g["off"]:g["off"] + g["size"]]


def has_mesh(b):
    for cmds in _vif_blocks(b):
        if any(c[1] == 13 and c[2] >= 3 for c in cmds):
            return True
    return False


# ----------------------------------------------------------------------
# Texture GoW (GFX_ + PAL_): indeks 8/4-bit PS2 ter-swizzle + palet RGBA (alpha 0x80 = 1.0)
# ----------------------------------------------------------------------
import zlib


def unswizzle8(buf, w, h):
    out = bytearray(w * h)
    for y in range(h):
        for x in range(w):
            bl = (y & ~0xF) * w + (x & ~0xF) * 2
            sw = (((y + 2) >> 2) & 1) * 4
            py = (((y & ~3) >> 1) + (y & 1)) & 7
            cl = py * w * 2 + ((x + sw) & 7) * 4
            bn = ((y >> 1) & 1) + ((x >> 2) & 2)
            i = bl + cl + bn
            out[y * w + x] = buf[i] if i < len(buf) else 0
    return out


def unswizzle4(buf, w, h):
    out = bytearray(w * h)
    ph = (w + 127) // 128
    pv = (h + 127) // 128
    for y in range(h):
        for x in range(w):
            pn = ((y & ~0x7F) // 128) * ph + ((x & ~0x7F) // 128)
            p32y = (pn // pv) * 32
            p32x = (pn % pv) * 64
            ploc = p32y * w * 2 + p32x * 4
            lx, ly = x & 0x7F, y & 0x7F
            bloc = (lx & ~0x1F) >> 1
            sw = (((y + 2) >> 2) & 1) * 4
            py = (((y & ~3) >> 1) + (y & 1)) & 7
            cl = py * w * 2 + ((x + sw) & 7) * 4
            bn = (x >> 3) & 3
            i = ploc + bloc + cl + bn
            if i < len(buf):
                out[y * w + x] = (buf[i] >> 4) if ((y >> 1) & 1) else (buf[i] & 15)
    return out


def png_bytes(w, h, rgba):
    raw = b"".join(b"\0" + rgba[y * w * 4:(y + 1) * w * 4] for y in range(h))
    def ch(t, d):
        c = struct.pack(">I", len(d)) + t + d
        return c + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + ch(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + ch(b"IDAT", zlib.compress(raw, 6)) + ch(b"IEND", b""))


TEX_TAGS = (0x3001e, 0x30001)   # GFX_/PAL_ di GoW1 / GoW2


def find_pal(ents, gname):
    """PAL_ untuk GFX_: nama sama, atau nama tanpa angka di belakang, atau prefix terdekat."""
    pals = {e["name"][4:]: e for e in ents if e["name"].startswith("PAL_") and e["tag"] in TEX_TAGS}
    base = gname[4:]
    for cand in (base, base.rstrip("0123456789"), base[:-1]):
        if cand in pals:
            return pals[cand]
    best = [k for k in pals if base.startswith(k) or k.startswith(base)]
    return pals[max(best, key=len)] if best else None


def decode_texture(wad, ents, gname, pname=None):
    """Return (w, h, rgba bytes) atau None."""
    g = next((e for e in ents if e["name"] == gname and e["tag"] in TEX_TAGS), None)
    if not g:
        return None
    gd = wad[g["off"]:g["off"] + g["size"]]
    if len(gd) < 0x18:
        return None
    w, h = struct.unpack_from("<II", gd, 4)
    bpp = struct.unpack_from("<I", gd, 16)[0]
    pe = next((e for e in ents if pname and e["name"] == pname and e["tag"] in TEX_TAGS), None) \
        or find_pal(ents, gname)
    if not pe or bpp not in (4, 8) or w * h == 0 or w > 2048 or h > 2048:
        return None
    pb = wad[pe["off"] + 0x18:pe["off"] + pe["size"]]
    n = len(pb) // 4
    pal = [tuple(pb[i * 4:i * 4 + 4]) for i in range(n)]
    if n == 256:   # palet 256 warna ter-swizzle (tukar bit 3 & 4)
        q = [None] * 256
        for i in range(256):
            q[(i & ~0x18) | ((i & 8) << 1) | ((i & 0x10) >> 1)] = pal[i]
        pal = q
    if bpp == 8:
        px = unswizzle8(gd[0x18:], w, h)
    else:   # 4-bit: linear, nibble rendah dulu (terverifikasi pada texture rantai)
        px = bytearray()
        for bt in gd[0x18:0x18 + (w * h + 1) // 2]:
            px += bytes((bt & 15, bt >> 4))
    out = bytearray()
    for v in px:
        r, g_, b, a = pal[v] if v < len(pal) else (0, 0, 0, 0)
        out += bytes((r, g_, b, min(255, a * 2)))
    return w, h, bytes(out)


def decode_model(wad, model):
    """Gabungkan semua blob geometri model. Return dict atau None kalau tidak ada mesh."""
    P, N, U, I, R, H, Gp, parts = [], [], [], [], [], [], [], []
    for gi, g in enumerate(model["geom"]):
        r = decode_blob(blob_of(wad, g))
        o = len(P)
        if r["positions"]:
            parts.append({"name": g["name"], "v0": o, "v1": o + len(r["positions"])})
        P += r["positions"]; N += r["normals"]; U += r["uvs"]; H += r["hasn"]; Gp += r["grp"]
        I += [(a + o, b + o, c + o) for a, b, c in r["indices"]]
        R += [(gi,) + x for x in r["refs"]]
    if not P or not I:
        return None
    I = orient_triangles(P, N, H, I)
    ents = model.get("ents") or []
    mats = []
    for mn in model.get("mats", []):
        tex = None
        gp_ = material_gfx(wad, ents, mn) if ents else None
        if gp_:
            try:
                tex = decode_texture(wad, ents, gp_[0], gp_[1])
            except Exception:
                log(traceback.format_exc())
        mats.append({"name": mn, "tex": tex})
    return {"positions": P, "normals": N, "uvs": U, "indices": I, "refs": R,
            "tri_group": [Gp[a] for a, _, _ in I], "materials": mats, "parts": parts}


import heapq
from collections import defaultdict


class CapacityError(ValueError):
    """Model tidak muat di slot vertex model asli."""
    def __init__(self, msg, need=0, cap=0, left=0):
        super().__init__(msg)
        self.need, self.cap, self.left = need, cap, left


def collect_packets(wad, model):
    """Semua paket vertex (slot) di blob geometri model."""
    out = []
    for gi, g in enumerate(model["geom"]):
        b = blob_of(wad, g)
        heads = seg_headers(b)
        for cmds in _vif_blocks(b):
            A = {5: [], 10: [], 12: [], 13: [], 14: []}
            for c in cmds:
                if c[1] in A:
                    A[c[1]].append(c)
            quads = []                     # tiap quad V4-32 (16 byte): [GIF tag, bounding sphere] per paket
            for c in A[12]:
                quads += [c[3] + 16 * j for j in range(c[2])]
            for k, pc in enumerate(A[13]):
                n = pc[2]
                pick = lambda f: A[f][k] if k < len(A[f]) and A[f][k][2] == n else None
                uv, nm, co = pick(5), pick(10), pick(14)
                sph = None
                if len(quads) >= 2 * (k + 1) and (struct.unpack_from("<I", b, quads[2 * k])[0] & 0x7FFF) == n:
                    cand = quads[2 * k + 1]
                    cx, cy, cz, cr = struct.unpack_from("<4f", b, cand)
                    if 0 < cr < 4000 and all(abs(v) < 4000 for v in (cx, cy, cz)):
                        pts = [struct.unpack_from("<3h", b, pc[3] + i * 8) for i in range(n)]
                        bx = [(max(p[j] for p in pts) + min(p[j] for p in pts)) / 32.0 for j in range(3)]
                        md = max(((p[0] / 16 - cx) ** 2 + (p[1] / 16 - cy) ** 2 + (p[2] / 16 - cz) ** 2) ** 0.5
                                 for p in pts)
                        # hanya dianggap bounding sphere kalau cocok dengan vertex aslinya
                        if max(abs(bx[0] - cx), abs(bx[1] - cy), abs(bx[2] - cz)) < 0.6 and cr >= md - 0.05:
                            sph = cand
                seg = max([hi for hi, (h, _) in enumerate(heads) if h <= pc[3]], default=0)
                out.append({"gi": gi, "base": g["off"], "n": n, "pos": pc[3],
                            "uv": uv[3] if uv else -1, "nrm": nm[3] if nm else -1,
                            "col": co[3] if co else -1, "sph": sph if sph is not None else -1,
                            "seg": (gi, seg), "verts": []})
    return out


class _Grid:
    def __init__(self, pts, cell):
        self.pts, self.cell, self.c = pts, cell, defaultdict(list)
        for i, p in enumerate(pts):
            self.c[self.key(p)].append(i)

    def key(self, p):
        c = self.cell
        return (int(p[0] // c), int(p[1] // c), int(p[2] // c))

    def nearest(self, p):
        kx, ky, kz = self.key(p)
        best, bd = -1, 1e30
        for r in range(0, 7):          # cincin sel terdekat dulu
            for dx in range(-r, r + 1):
                for dy in range(-r, r + 1):
                    for dz in range(-r, r + 1):
                        if max(abs(dx), abs(dy), abs(dz)) != r:
                            continue
                        for i in self.c.get((kx + dx, ky + dy, kz + dz), ()):
                            q = self.pts[i]
                            d = (q[0] - p[0]) ** 2 + (q[1] - p[1]) ** 2 + (q[2] - p[2]) ** 2
                            if d < bd:
                                best, bd = i, d
            if best >= 0 and (r * self.cell) ** 2 >= bd:
                return best
        if best >= 0 and bd <= (6 * self.cell) ** 2:
            return best
        for i, q in enumerate(self.pts):   # jauh dari semua vertex lama: cari penuh
            d = (q[0] - p[0]) ** 2 + (q[1] - p[1]) ** 2 + (q[2] - p[2]) ** 2
            if d < bd:
                best, bd = i, d
        return best


def stripify(tris):
    """Strip segitiga greedy: mulai dari segitiga ber-derajat terendah, pilih 1 dari 3 arah yang menghasilkan
    strip terpanjang (lookahead), lalu perpanjang juga ke belakang. tris = [(a,b,c)] id vertex."""
    tris = [t for t in tris if len(set(t)) == 3]
    em = defaultdict(list)
    for i, (a, b, c) in enumerate(tris):
        for x, y in ((a, b), (b, c), (c, a)):
            em[(x, y) if x < y else (y, x)].append(i)
    used = [False] * len(tris)

    def nb(i, x, y):
        for t in em[(x, y) if x < y else (y, x)]:
            if t != i and not used[t]:
                return t
        return None

    def deg(i):
        a, b, c = tris[i]
        return sum(1 for x, y in ((a, b), (b, c), (c, a)) if nb(i, x, y) is not None)

    def run(i, rot):
        seen, strip, cur = {i}, list(rot), i
        while True:
            x, y = strip[-2], strip[-1]
            n = None
            for t in em[(x, y) if x < y else (y, x)]:
                if t != cur and not used[t] and t not in seen:
                    n = t
                    break
            if n is None:
                break
            z = [v for v in tris[n] if v != x and v != y]
            if len(z) != 1:
                break
            seen.add(n)
            strip.append(z[0])
            cur = n
        return strip, seen

    heap = [(deg(i), i) for i in range(len(tris))]
    heapq.heapify(heap)
    strips = []
    while heap:
        d, i = heapq.heappop(heap)
        if used[i]:
            continue
        dn = deg(i)
        if dn != d:
            heapq.heappush(heap, (dn, i))
            continue
        a, b, c = tris[i]
        best = None
        for rot in ((a, b, c), (b, c, a), (c, a, b)):
            st, seen = run(i, rot)
            if best is None or len(st) > len(best[0]):
                best = (st, seen)
        st, seen = best
        for t in seen:
            used[t] = True
        cur = i
        while True:                     # perpanjang ke belakang
            x, y = st[0], st[1]
            n = None
            for t in em[(x, y) if x < y else (y, x)]:
                if t != cur and not used[t]:
                    n = t
                    break
            if n is None:
                break
            z = [v for v in tris[n] if v != x and v != y]
            if len(z) != 1:
                break
            used[n] = True
            st.insert(0, z[0])
            cur = n
        strips.append(st)
    return strips


def _pack_strips(packets, strips):
    """Tempatkan strip ke slot paket. Awal strip: 2 vertex ADC (3 vertex untuk segitiga pertama). Jika ujung
    strip = vertex terakhir yang baru ditaruh, cukup 1 vertex ADC (antrian GS masih memegang vertex itu).
    Return segitiga yang tidak muat."""
    left = []
    pi = 0
    ends = defaultdict(list)
    for k, st in enumerate(strips):
        ends[st[0]].append(k)
        ends[st[-1]].append(k)
    done = [False] * len(strips)
    order = iter(range(len(strips)))
    nxt_free = 0
    last = None
    while True:
        k = None
        if last is not None:
            for c in ends.get(last, ()):
                if not done[c]:
                    k = c
                    break
        if k is None:
            while nxt_free < len(strips) and done[nxt_free]:
                nxt_free += 1
            if nxt_free >= len(strips):
                break
            k = nxt_free
        done[k] = True
        rem = strips[k]
        chained = False
        if last is not None and rem[0] != last and rem[-1] == last:
            rem = rem[::-1]
        if last is not None and rem[0] == last:
            chained = True
        last = None
        while len(rem) >= 3:
            while pi < len(packets) and packets[pi]["n"] - len(packets[pi]["verts"]) < 3:
                pi += 1
            if pi >= len(packets):
                left += [(rem[i], rem[i + 1], rem[i + 2]) for i in range(len(rem) - 2)]
                rem = []
                break
            pk = packets[pi]
            free = pk["n"] - len(pk["verts"])
            if chained and pk["verts"] and pk["verts"][-1][0] == rem[0]:
                body = rem[1:]                          # vertex pertama sudah ada di antrian: kirim sisanya
                m = min(free, len(body))
                for i in range(m):
                    pk["verts"].append((body[i], i < 1))
                last = body[m - 1]
                rem = body[m - 2:] if m < len(body) else []
                chained = False
                continue
            chained = False
            m = min(free, len(rem))
            for i in range(m):
                pk["verts"].append((rem[i], i < 2))
            last = rem[m - 1]
            rem = rem[m - 2:] if m < len(rem) else []
    return left


def full_replace(wad, model, old, mesh):
    """Isi ulang SEMUA slot vertex dengan model baru (tiap segitiga dari glTF persis dipakai).
    Struktur paket/header WAD tidak diubah; slot sisa dibuat degenerate (tidak tergambar)."""
    packets = collect_packets(wad, model)
    NP = mesh["positions"]
    tris = [t for t in mesh["tris"] if len(set(t)) == 3]
    if not tris:
        raise ValueError("glTF tidak punya segitiga")
    NU = mesh.get("uvs") or [(0.0, 0.0)] * len(NP)
    NC = mesh.get("colors")
    NN = mesh.get("normals")
    if NN is None:                      # normal halus dari segitiga
        acc = [[0.0, 0.0, 0.0] for _ in NP]
        for a, b, c in tris:
            ux, uy, uz = (NP[b][k] - NP[a][k] for k in range(3))
            vx, vy, vz = (NP[c][k] - NP[a][k] for k in range(3))
            n = (uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx)
            for i in (a, b, c):
                for k in range(3):
                    acc[i][k] += n[k]
        NN = [tuple(v) for v in acc]
    xs, ys, zs = zip(*NP)
    if max(map(abs, xs + ys + zs)) * POS_SCALE > 32768:
        raise ValueError("Model terlalu besar: koordinat GoW dibatasi +-%d unit. Kecilkan skala model di Blender." % (32767 // POS_SCALE))
    # w (bit bone/flag) & segmen dari vertex asli terdekat
    op = old["positions"]
    wbits = []
    for (gi, po, uo, no) in old["refs"]:
        wbits.append(struct.unpack_from("<h", wad, model["geom"][gi]["off"] + po + 6)[0] & 0x7FFF)
    seg_of_old = {}
    for pk in packets:
        for i in range(pk["n"]):
            seg_of_old[(pk["gi"], pk["pos"] + i * 8)] = pk["seg"]
    ov_seg = [seg_of_old.get((gi, po)) for (gi, po, uo, no) in old["refs"]]
    ox, oy, oz = zip(*op)
    odiag = ((max(ox) - min(ox)) ** 2 + (max(oy) - min(oy)) ** 2 + (max(oz) - min(oz)) ** 2) ** 0.5 or 1.0
    grid = _Grid(op, max(odiag / 24.0, 1e-3))
    nv_old = {}
    def near(v):
        if v not in nv_old:
            nv_old[v] = grid.nearest(NP[v])
        return nv_old[v]
    segs = defaultdict(list)           # seg -> paket
    for pk in packets:
        segs[pk["seg"]].append(pk)
    seg_tris = defaultdict(list)
    for t in tris:
        c = tuple(sum(NP[i][k] for i in t) / 3.0 for k in range(3))
        j = grid.nearest(c)
        seg_tris[ov_seg[j]].append(t)
    # pusat tiap segmen (untuk spill)
    cen = {}
    for sg in segs:
        pts = [op[i] for i in range(len(op)) if ov_seg[i] == sg]
        cen[sg] = tuple(sum(p[k] for p in pts) / len(pts) for k in range(3)) if pts else (0, 0, 0)
    leftover = []
    for sg, tl in seg_tris.items():
        leftover += _pack_strips(segs[sg], stripify(tl)) if sg in segs else tl
    spill = len(leftover)
    for _ in range(8):
        if not leftover:
            break
        free = [sg for sg in segs if any(p["n"] - len(p["verts"]) >= 3 for p in segs[sg])]
        if not free:
            break
        groups = defaultdict(list)
        for t in leftover:
            c = tuple(sum(NP[i][k] for i in t) / 3.0 for k in range(3))
            sg = min(free, key=lambda g_: sum((cen[g_][k] - c[k]) ** 2 for k in range(3)))
            groups[sg].append(t)
        leftover = []
        for sg, tl in groups.items():
            leftover += _pack_strips(segs[sg], stripify(tl))
    cap = sum(p["n"] for p in packets)
    if leftover:
        raise CapacityError("Model terlalu detail untuk slot model ini: %d segitiga tidak muat "
                            "(kapasitas %d vertex di %d paket). Kurangi poligon, atau izinkan pengurangan otomatis."
                            % (len(leftover), cap, len(packets)), len(tris), cap, len(leftover))
    # ---- tulis
    out = bytearray(wad)
    cl = lambda v, lo, hi: max(lo, min(hi, v))
    mid = ((max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2, (max(zs) + min(zs)) / 2)
    used_v = 0
    for pk in packets:
        base = pk["base"]
        pts = []
        for i in range(pk["n"]):
            if i < len(pk["verts"]):
                v, adc = pk["verts"][i]
                used_v += 1
                p = NP[v]
                w = wbits[near(v)] | (0x8000 if adc else 0)
                uv, nn = NU[v], NN[v]
                col = NC[v] if NC else (1.0, 1.0, 1.0, 1.0)
            else:                       # slot sisa: degenerate (ADC, tidak tergambar)
                p, w, uv, nn, col = mid, 0x8000, (0.0, 0.0), (0.0, 0.0, 1.0), (1.0, 1.0, 1.0, 1.0)
            pts.append(p)
            struct.pack_into("<3h", out, base + pk["pos"] + i * 8, *[cl(int(round(c * POS_SCALE)), -32768, 32767) for c in p])
            struct.pack_into("<h", out, base + pk["pos"] + i * 8 + 6, w - 0x10000 if w > 0x7FFF else w)
            if pk["uv"] >= 0:
                struct.pack_into("<2h", out, base + pk["uv"] + i * 4, cl(int(round(uv[0] * 4096)), -32768, 32767),
                                 cl(int(round(uv[1] * 4096)), -32768, 32767))
            if pk["nrm"] >= 0:
                ln = (nn[0] ** 2 + nn[1] ** 2 + nn[2] ** 2) ** 0.5 or 1.0
                struct.pack_into("<3b", out, base + pk["nrm"] + i * 3, *[cl(int(round(c / ln * 127)), -128, 127) for c in nn])
            if pk["col"] >= 0:
                out[base + pk["col"] + i * 4:base + pk["col"] + i * 4 + 4] = bytes(cl(int(round(c * 128)), 0, 255) for c in col)
        if pk["sph"] >= 0:              # bounding sphere paket (culling)
            px, py, pz = zip(*pts)
            c = ((max(px) + min(px)) / 2, (max(py) + min(py)) / 2, (max(pz) + min(pz)) / 2)
            r = max(((q[0] - c[0]) ** 2 + (q[1] - c[1]) ** 2 + (q[2] - c[2]) ** 2) ** 0.5 for q in pts) + 0.25
            struct.pack_into("<4f", out, base + pk["sph"], c[0], c[1], c[2], r)
    info = "model baru: %d segitiga, %d vertex terpakai dari %d slot (spill %d)" % (len(tris), used_v, cap, spill)
    return out, info


def cluster_simplify(mesh, cell):
    """Kurangi poligon dengan vertex clustering: vertex yang berdekatan (posisi & UV) digabung."""
    P, N, U = mesh["positions"], mesh.get("normals"), mesh.get("uvs")
    C = mesh.get("colors")
    xs, ys, zs = zip(*P)
    diag = ((max(xs) - min(xs)) ** 2 + (max(ys) - min(ys)) ** 2 + (max(zs) - min(zs)) ** 2) ** 0.5 or 1.0
    uq = max(0.004, 2.0 * cell / diag)
    key, groups, remap = {}, [], []
    for i, p in enumerate(P):
        k = (int(p[0] // cell), int(p[1] // cell), int(p[2] // cell))
        if U:
            k += (int(U[i][0] // uq), int(U[i][1] // uq))
        if k not in key:
            key[k] = len(groups)
            groups.append([])
        groups[key[k]].append(i)
        remap.append(key[k])
    avg = lambda L, ids, n: tuple(sum(L[i][c] for i in ids) / len(ids) for c in range(n))
    out = {"positions": [avg(P, g, 3) for g in groups]}
    if N:
        out["normals"] = [avg(N, g, 3) for g in groups]
    if U:
        out["uvs"] = [avg(U, g, 2) for g in groups]
    if C:
        out["colors"] = [avg(C, g, 4) for g in groups]
    seen, tris = set(), []
    for a, b, c in mesh["tris"]:
        t = (remap[a], remap[b], remap[c])
        if len(set(t)) == 3 and tuple(sorted(t)) not in seen:
            seen.add(tuple(sorted(t)))
            tris.append(t)
    out["tris"] = tris
    return out


def fit_by_budget(wad, model, old, mesh):
    """Model kecil sekali (billboard/efek): pakai sebanyak mungkin segitiga TERBESAR yang muat."""
    P = mesh["positions"]

    def area(t):
        a, b, c = (P[i] for i in t)
        ux, uy, uz = (b[k] - a[k] for k in range(3))
        vx, vy, vz = (c[k] - a[k] for k in range(3))
        return (uy * vz - uz * vy) ** 2 + (uz * vx - ux * vz) ** 2 + (ux * vy - uy * vx) ** 2

    tris = sorted((t for t in mesh["tris"] if len(set(t)) == 3), key=area, reverse=True)
    lo, hi, best = 0, len(tris), None
    while lo < hi:
        mid = (lo + hi + 1) // 2
        try:
            out, info = full_replace(wad, model, old, dict(mesh, tris=tris[:mid]))
            lo, best = mid, (out, "%s | slot sangat kecil: hanya %d dari %d segitiga terbesar yang dipakai"
                             % (info, mid, len(tris)))
        except CapacityError:
            hi = mid - 1
    if best is None:
        raise CapacityError("Slot model ini terlalu kecil (kurang dari 1 segitiga muat)", len(tris), 0, 0)
    return best


def fit_by_simplify(wad, model, old, mesh):
    """Cari tingkat pengurangan poligon TERKECIL yang benar-benar muat (dicoba nyata lewat full_replace)."""
    xs, ys, zs = zip(*mesh["positions"])
    diag = ((max(xs) - min(xs)) ** 2 + (max(ys) - min(ys)) ** 2 + (max(zs) - min(zs)) ** 2) ** 0.5 or 1.0
    total = len(mesh["tris"])

    def attempt(cell):
        m2 = cluster_simplify(mesh, cell)
        if not m2["tris"]:
            return None
        try:
            out, info = full_replace(wad, model, old, m2)
        except CapacityError:
            return None
        return out, "%s | dikurangi otomatis: %d -> %d segitiga" % (info, total, len(m2["tris"]))

    lo, hi = diag / 6000.0, diag / 2.0
    best = attempt(hi)
    if best is None:
        return fit_by_budget(wad, model, old, mesh)
    for _ in range(9):
        mid = (lo * hi) ** 0.5
        r = attempt(mid)
        if r:
            hi, best = mid, r
        else:
            lo = mid
    return best


def same_topology(old, mesh, idx):
    """True jika glTF = model asli (urutan vertex identik + segitiga identik) -> cukup geser vertex."""
    if idx != list(range(len(old["positions"]))) or len(mesh["positions"]) != len(old["positions"]):
        return False
    a = {tuple(sorted(t)) for t in old["indices"]}
    b = {tuple(sorted(t)) for t in mesh.get("tris", [])}
    return a == b


def encode_model(mesh, wad, model, simplify=False):
    """Replace. Return (WAD baru, keterangan).
    - glTF = model asli dengan bentuk diedit (topologi sama): vertex digeser di tempat (struktur strip asli utuh).
    - selain itu: model baru, semua slot vertex diisi ulang dengan segitiga dari glTF."""
    old = decode_model(wad, model)
    if old is None:
        raise ValueError("Model tidak punya mesh yang bisa dibaca")
    if mesh.get("uvs") is not None and len(mesh["positions"]) == len(old["positions"]):
        idx, hit = match_vertices(old, mesh)
        if same_topology(old, mesh, idx):
            out = bytearray(wad)
            clamp = lambda v, lo, hi: max(lo, min(hi, v))
            for i, (gi, po, uo, no) in enumerate(old["refs"]):
                base = model["geom"][gi]["off"]
                x, y, z = mesh["positions"][i]
                struct.pack_into("<3h", out, base + po, *[clamp(int(round(c * POS_SCALE)), -32768, 32767) for c in (x, y, z)])
                if no >= 0 and mesh.get("normals"):
                    nx, ny, nz = mesh["normals"][i]
                    ln = (nx * nx + ny * ny + nz * nz) ** 0.5 or 1.0
                    qn = [clamp(int(round(c / ln * 127)), -128, 127) for c in (nx, ny, nz)]
                    cur = struct.unpack_from("<3b", out, base + no)
                    if max(abs(a - b) for a, b in zip(qn, cur)) > 1:      # normal tak berubah -> jangan disentuh
                        struct.pack_into("<3b", out, base + no, *qn)
            return out, "bentuk model diedit di tempat (%d vertex)" % len(old["positions"])
    try:
        return full_replace(wad, model, old, mesh)
    except CapacityError:
        if not simplify:
            raise
        return fit_by_simplify(wad, model, old, mesh)


def write_gltf(mesh, path):
    """glTF 2.0. Tiap blob geometri (bagian/LOD model) = satu node/mesh sendiri; di dalamnya satu primitif
    per material. Urutan vertex digabung = urutan blob (dipakai Replace)."""
    P, N, U, I = mesh["positions"], mesh["normals"], mesh["uvs"], mesh["indices"]
    TG = mesh.get("tri_group") or [0] * len(I)
    mats = mesh.get("materials") or []
    parts = mesh.get("parts") or [{"name": "mesh", "v0": 0, "v1": len(P)}]
    buf = bytearray()
    views, accs = [], []

    def add(data, target=None):
        while len(buf) % 4:
            buf.append(0)
        v = {"buffer": 0, "byteOffset": len(buf), "byteLength": len(data)}
        if target:
            v["target"] = target
        views.append(v)
        buf.extend(data)
        return len(views) - 1

    def acc(view, ct, count, typ, **extra):
        a = {"bufferView": view, "componentType": ct, "count": count, "type": typ}
        a.update(extra)
        accs.append(a)
        return len(accs) - 1

    images, textures, materials, gmat = [], [], [], {}
    for gp in sorted(set(TG)):
        m = mats[gp] if gp < len(mats) else None
        pm = {"name": m["name"] if m else "group%d" % gp, "doubleSided": True,
              "pbrMetallicRoughness": {"metallicFactor": 0.0, "roughnessFactor": 1.0}}
        if m and m.get("tex"):
            w, h, rgba = m["tex"]
            vi = add(png_bytes(w, h, rgba))
            images.append({"bufferView": vi, "mimeType": "image/png"})
            textures.append({"source": len(images) - 1, "sampler": 0})
            pm["pbrMetallicRoughness"]["baseColorTexture"] = {"index": len(textures) - 1}
            transp = sum(1 for i in range(3, len(rgba), 4) if rgba[i] < 128)
            pm["alphaMode"] = "MASK" if transp > (w * h) // 50 else "OPAQUE"
            if pm["alphaMode"] == "MASK":
                pm["alphaCutoff"] = 0.5
        gmat[gp] = len(materials)
        materials.append(pm)
    meshes, nodes = [], []
    for part in parts:
        v0, v1 = part["v0"], part["v1"]
        if v1 <= v0:
            continue
        PP, NN, UU = P[v0:v1], N[v0:v1], U[v0:v1]
        xs, ys, zs = zip(*PP)
        ap = acc(add(b"".join(struct.pack("<3f", *v) for v in PP), 34962), 5126, len(PP), "VEC3",
                 min=[min(xs), min(ys), min(zs)], max=[max(xs), max(ys), max(zs)])
        an = acc(add(b"".join(struct.pack("<3f", *v) for v in NN), 34962), 5126, len(NN), "VEC3")
        au = acc(add(b"".join(struct.pack("<2f", *v) for v in UU), 34962), 5126, len(UU), "VEC2")
        prims = []
        for gp in sorted(set(TG)):
            tris = [(a - v0, b_ - v0, c - v0) for (a, b_, c), g_ in zip(I, TG) if g_ == gp and v0 <= a < v1]
            if not tris:
                continue
            ai = acc(add(b"".join(struct.pack("<3I", *t) for t in tris), 34963), 5125, len(tris) * 3, "SCALAR")
            prims.append({"attributes": {"POSITION": ap, "NORMAL": an, "TEXCOORD_0": au},
                          "indices": ai, "material": gmat[gp]})
        if prims:
            meshes.append({"name": part["name"], "primitives": prims})
            nodes.append({"name": part["name"], "mesh": len(meshes) - 1})
    g = {
        "asset": {"version": "2.0", "generator": "GoW Model Tool"},
        "scene": 0, "scenes": [{"nodes": list(range(len(nodes)))}], "nodes": nodes, "meshes": meshes,
        "buffers": [{"byteLength": len(buf),
                     "uri": "data:application/octet-stream;base64," + base64.b64encode(bytes(buf)).decode()}],
        "bufferViews": views, "accessors": accs, "materials": materials,
    }
    if images:
        g["images"], g["textures"] = images, textures
        g["samplers"] = [{"magFilter": 9729, "minFilter": 9729, "wrapS": 10497, "wrapT": 10497}]
    with open(path, "w") as f:
        json.dump(g, f)


def _mat_mul(a, b):
    return [sum(a[r * 4 + k] * b[k * 4 + c] for k in range(4)) for r in range(4) for c in range(4)]


def _node_matrix(n):
    """Matriks 4x4 (row-major) dari node glTF (matrix atau TRS)."""
    if "matrix" in n:
        m = n["matrix"]           # glTF column-major -> row-major
        return [m[c * 4 + r] for r in range(4) for c in range(4)]
    tx, ty, tz = n.get("translation", (0, 0, 0))
    qx, qy, qz, qw = n.get("rotation", (0, 0, 0, 1))
    sx, sy, sz = n.get("scale", (1, 1, 1))
    r = [1 - 2 * (qy * qy + qz * qz), 2 * (qx * qy - qz * qw), 2 * (qx * qz + qy * qw),
         2 * (qx * qy + qz * qw), 1 - 2 * (qx * qx + qz * qz), 2 * (qy * qz - qx * qw),
         2 * (qx * qz - qy * qw), 2 * (qy * qz + qx * qw), 1 - 2 * (qx * qx + qy * qy)]
    return [r[0] * sx, r[1] * sy, r[2] * sz, tx,
            r[3] * sx, r[4] * sy, r[5] * sz, ty,
            r[6] * sx, r[7] * sy, r[8] * sz, tz,
            0, 0, 0, 1]


def read_gltf(path):
    """Baca SEMUA mesh/primitif di scene (termasuk hasil export Blender: banyak primitif, banyak node,
    transform node). Return positions, normals, uvs, colors, tris (digabung, transform node diterapkan)."""
    g = json.load(open(path))
    bufs = []
    for bf in g["buffers"]:
        uri = bf.get("uri", "")
        bufs.append(base64.b64decode(uri.split(",", 1)[1]) if uri.startswith("data:")
                    else open(os.path.join(os.path.dirname(path), uri), "rb").read())
    CT = {5120: ("b", 1, 127.0), 5121: ("B", 1, 255.0), 5122: ("h", 2, 32767.0),
          5123: ("H", 2, 65535.0), 5125: ("I", 4, 4294967295.0), 5126: ("f", 4, None)}

    def acc(i, nc):
        a = g["accessors"][i]
        v = g["bufferViews"][a["bufferView"]]
        fmt, sz, norm = CT[a["componentType"]]
        o = v.get("byteOffset", 0) + a.get("byteOffset", 0)
        stride = v.get("byteStride") or nc * sz
        out = []
        for k in range(a["count"]):
            t = struct.unpack_from("<%d%s" % (nc, fmt), bufs[v["buffer"]], o + k * stride)
            out.append(tuple(c / norm for c in t) if norm and a.get("normalized") else t)
        return out

    P, N, U, C, T = [], [], [], [], []
    flags = {"n": True, "u": True, "c": True}
    seen = {}

    def visit(ni, parent):
        n = g["nodes"][ni]
        m = _mat_mul(parent, _node_matrix(n))
        if "mesh" in n:
            for prim in g["meshes"][n["mesh"]]["primitives"]:
                at = prim["attributes"]
                key = (ni, at["POSITION"], at.get("NORMAL"), at.get("TEXCOORD_0"), at.get("COLOR_0"))
                if key in seen:      # primitif yang berbagi vertex buffer: vertex dibaca sekali
                    base = seen[key]
                else:
                    base = seen[key] = len(P)
                    pp = acc(at["POSITION"], 3)
                    cnt = len(pp)
                    P.extend((m[0] * x + m[1] * y + m[2] * z + m[3],
                              m[4] * x + m[5] * y + m[6] * z + m[7],
                              m[8] * x + m[9] * y + m[10] * z + m[11]) for x, y, z in pp)
                    if "NORMAL" in at:
                        N.extend((m[0] * x + m[1] * y + m[2] * z, m[4] * x + m[5] * y + m[6] * z,
                                  m[8] * x + m[9] * y + m[10] * z) for x, y, z in acc(at["NORMAL"], 3))
                    else:
                        flags["n"] = False
                        N.extend([(0.0, 0.0, 1.0)] * cnt)
                    if "TEXCOORD_0" in at:
                        U.extend(acc(at["TEXCOORD_0"], 2))
                    else:
                        flags["u"] = False
                        U.extend([(0.0, 0.0)] * cnt)
                    if "COLOR_0" in at:
                        a_ = g["accessors"][at["COLOR_0"]]
                        nc = 4 if a_["type"] == "VEC4" else 3
                        C.extend((c[0], c[1], c[2], c[3] if nc == 4 else 1.0) for c in acc(at["COLOR_0"], nc))
                    else:
                        flags["c"] = False
                        C.extend([(1.0, 1.0, 1.0, 1.0)] * cnt)
                if "indices" in prim:
                    ids = [t[0] + base for t in acc(prim["indices"], 1)]
                else:
                    ids = list(range(base, base + g["accessors"][at["POSITION"]]["count"]))
                mode = prim.get("mode", 4)
                if mode == 4:
                    T.extend((ids[i], ids[i + 1], ids[i + 2]) for i in range(0, len(ids) - 2, 3))
                elif mode == 5:
                    T.extend((ids[i], ids[i + 1], ids[i + 2]) if i % 2 == 0 else (ids[i + 1], ids[i], ids[i + 2])
                             for i in range(len(ids) - 2))
                elif mode == 6:
                    T.extend((ids[0], ids[i], ids[i + 1]) for i in range(1, len(ids) - 1))
        for c in n.get("children", []):
            visit(c, m)

    ident = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]
    sc = g["scenes"][g.get("scene", 0)] if g.get("scenes") else {"nodes": range(len(g["nodes"]))}
    for ni in sc["nodes"]:
        visit(ni, ident)
    if not P:
        raise ValueError("glTF tidak berisi mesh")
    mesh = {"positions": P, "tris": T}
    if flags["n"]:
        mesh["normals"] = N
    if flags["u"]:
        mesh["uvs"] = U
    if flags["c"]:
        mesh["colors"] = C
    return mesh


def match_vertices(old, new):
    """Petakan tiap vertex asli -> vertex glTF baru. Return (list indeks, jumlah cocok-UV).
    Kunci utama: UV (tidak berubah saat edit bentuk), lalu posisi terdekat. Tahan terhadap
    urutan acak, vertex terpecah (split), dan beberapa primitif dari Blender."""
    n_old = len(old["positions"])
    nu = new.get("uvs")
    ou = old["uvs"]
    if len(new["positions"]) == n_old and nu is not None and \
            all(abs(a[0] - b[0]) < 2.5e-4 and abs(a[1] - b[1]) < 2.5e-4 for a, b in zip(ou, nu)):
        return list(range(n_old)), n_old                      # urutan identik
    buckets = {}
    if nu is not None:
        for j, (u, v) in enumerate(nu):
            buckets.setdefault((round(u * 2048), round(v * 2048)), []).append(j)
    NP = new["positions"]
    NN = new.get("normals")
    ON = old["normals"]
    res, hit = [], 0
    for i in range(n_old):
        x, y, z = old["positions"][i]
        cand = None
        if nu is not None:
            u, v = ou[i]
            ku, kv = round(u * 2048), round(v * 2048)
            cand = []
            for du in (0, -1, 1):
                for dv in (0, -1, 1):
                    cand += buckets.get((ku + du, kv + dv), [])
            if cand:
                hit += 1
        if not cand:
            cand = range(len(NP))
        best, bd = -1, 1e30
        nx, ny, nz = ON[i]
        for j in cand:
            p = NP[j]
            d = (p[0] - x) ** 2 + (p[1] - y) ** 2 + (p[2] - z) ** 2
            if NN is not None:      # normal ikut menentukan (membedakan vertex ber-UV sama)
                q = NN[j]
                ln = (q[0] * q[0] + q[1] * q[1] + q[2] * q[2]) ** 0.5 or 1.0
                d += 16.0 * (1.0 - (nx * q[0] + ny * q[1] + nz * q[2]) / ln)
            if d < bd:
                best, bd = j, d
        res.append(best)
    return res, hit


# ======================================================================
# Katalog model (hasil pemindaian semua WAD GoW 1 & GoW 2 yang diberikan) -> daftar WAD instan
# ======================================================================
CATALOG_B64 = (
    "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"
    "pSmV1pfhEDRrTBvHVHUg96yHrjskK2lZpaQ6S0lJ95VNmqpRqToiT0lVqsfIUlL+Hm2HTF9MZSYryUakRP6pUsHJXkWNSHVCKtJKFEl/VWX6Yi"
    "oRVennqKq0J4q09qkMVqWbhqpKX6zT7Gsi62RUpk43SanOSpEDRS2dPyCbJUsgOTbUbJmwYpKpInYMiuqriJJOEW2/Ikc9Kue7lfTb1kTlZn2v"
    "eLgOMkgQAqKztDLtbJFKz2IiVZXUQzTJIJDpDHFHUIyUDmGp6vQQvEz1ftFy8v21gGdXZGRTSnyOAf8yFwOX2+MMuI5D5hRZUOSCPxH0HUFzE8"
    "kmRKa8W6aj3e09OSnttMjkR3132+32J2TvyURco5x8tt4hvJG7y6oiMmU4TuPiW0kvHCRF6+cyJaXSY7LzKoupvCZkzDT7akI4Lf7O8ionJWuP"
    "JqWVqNO86jSvJs2rESkpbXaTltikvdqkPdEklSiTPagmiZQkU5JKSXlKKlJSmZKqlFSnpLT2Iq2ESCshJjw2qHzCi8OEy4lcTNCmvEJMeZKYcG"
    "sx4dWjKibKmPI8UUx4yikmyignyignyignyign2lFO9FU50aflhHefcqLO5USdq4k6VxN1ribqV0188nIi2USVq6l0E82oJppRp/NETtS4TueO"
    "NTPYvmyziVMvoMt0GZwyZaOMnheRpzMjn0iVTqkirW6hSFFNWCcK9LCgt2+vi/bQH1+t/1aCW5fA55PZF7QHMPBfnwejTB2bkNQSfpxZr6XG9G"
    "R7eBqFN4oOFOkp635nbliO118+XD45mvUy8PnxEXwbHoifw4M9fdcAHNB/7XY7nxGtqnVn3790H7qdInDOm3C1HcCJl/4yBrR6cI8oihQUKSlS"
    "UaSmSEMQa59vagGrrnQacf8YlQGcIhOKSih5QikSSpmWlaUkkTqpYUaN0u0DpYnDEJn9JDtI+1A3Fu7AmNfEdXvY7Iz+3xIsCrm5K3qriSt7Xj"
    "k3ny1Xi8vZJ6bJC8q98eX1ZknGcxjQp/4b6Ac/dIdu2K6vzEWZXd/DEXdMkxM0MC2Z/WAcQ2D2QVVnLxTjjWSwvHkG/yPuxo7xDTFbr7txdARr"
    "Rrg6bZ/sFucW/Jh0G38QMWnnCpf93OV7E4Bkgjb1bf7wxwCjF8Mm5v3OGFH+y+WX1cbWxdn6XzBsyjNZSGbcHB4+H97trIzcUYF7Inm4t7rzAN"
    "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"
)
_CAT = None


def catalog():
    global _CAT
    if _CAT is None:
        try:
            _CAT = json.loads(zlib.decompress(base64.b64decode(CATALOG_B64)))
        except Exception:
            _CAT = {"GoW1": {}, "GoW2": {}}
    return _CAT


def app_dir():
    base = sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__)
    return os.path.dirname(base)


ERR_FILE = "gow_model_tool_error.txt"


def log_error(text):
    """Catat error ke file di samping program (seperti tool SBK)."""
    try:
        with open(os.path.join(app_dir(), ERR_FILE), "a", encoding="utf-8") as f:
            f.write(text + "\n" + "-" * 60 + "\n")
    except Exception:
        pass
    return os.path.join(app_dir(), ERR_FILE)


# ======================================================================
# Sumber: ISO / satu WAD / folder WAD   +   perubahan tertunda (SAVE)   +   backup
# ======================================================================
BK_MAGIC = b"GOWMBK1\n"


def bk_read(path):
    recs = []
    if not os.path.exists(path):
        return recs
    with open(path, "rb") as f:
        if f.read(8) != BK_MAGIC:
            return recs
        while True:
            h = f.read(4)
            if len(h) < 4:
                break
            jl = struct.unpack("<I", h)[0]
            meta = json.loads(f.read(jl).decode())
            recs.append((meta["pak"], meta["off"], f.read(meta["len"])))
    return recs


def bk_add(path, pak, off, data):
    """Simpan isi ASLI sebelum ditimpa. Lokasi yang sudah pernah dibackup tidak ditimpa lagi."""
    have = {(p, o, len(d)) for p, o, d in bk_read(path)}
    if (pak, off, len(data)) in have:
        return
    new = not os.path.exists(path)
    with open(path, "ab") as f:
        if new:
            f.write(BK_MAGIC)
        meta = json.dumps({"pak": pak, "off": off, "len": len(data)}).encode()
        f.write(struct.pack("<I", len(meta)) + meta + data)


def wad_game(d):
    """GoW1: entry pertama tag 0x378; GoW2: tag 0x15."""
    if len(d) >= 12 and d[8:12] == b"WAD_":
        return 1 if struct.unpack_from("<I", d, 0)[0] == 0x378 else 2
    return None


class WadRef:
    def __init__(self, name, size, game, locs=None, path=None):
        self.name, self.size, self.game = name, size, game
        self.locs = locs or []      # ISO: [(PARTn.PAK, offset)]
        self.path = path            # WAD lepas: path file


class Session:
    """kind: 'iso' | 'wad' | 'folder'."""

    def __init__(self, kind, disc=None, path=None):
        self.kind, self.disc, self.path = kind, disc, path
        self.refs, self.game = [], None
        self.wad_cache, self.model_cache, self.pending = {}, {}, {}
        if kind == "iso":
            toc = disc.toc_bytes()
            if not toc:
                raise ValueError("GODOFWAR.TOC tidak ada di ISO")
            self.game, ents = parse_toc(toc)
            log("Game terdeteksi: GoW%d, file di TOC: %d" % (self.game, len(ents)))
            log("PAK di ISO:", disc.paks())
            ok, tot = verify_toc(ents, disc)
            self.method = "TOC OK %d/%d" % (ok, tot)
            self.refs = [WadRef(e.name, e.size, self.game, e.locs) for e in ents
                         if e.name.upper().endswith(".WAD") and e.locs]
            self.backup_path = disc.path + ".modbak"
        else:
            files = [path] if kind == "wad" else sorted(
                os.path.join(path, f) for f in os.listdir(path) if f.upper().endswith(".WAD"))
            for fp in files:
                with open(fp, "rb") as f:
                    head = f.read(32)
                g = wad_game(head)
                if g:
                    self.refs.append(WadRef(os.path.basename(fp).upper(), os.path.getsize(fp), g, path=fp))
            self.game = self.refs[0].game if self.refs else None
            self.method = "%d WAD" % len(self.refs)
            self.backup_path = None
        self.by_name = {r.name: r for r in self.refs}

    # ---- daftar
    def pak_names(self):
        return sorted(self.disc.paks()) if self.kind == "iso" else []

    def wads_in(self, pak=None):
        out = []
        for r in sorted(self.refs, key=lambda x: x.name):
            if wad_hidden(r.name):
                continue
            if pak is not None and not any(l[0] == pak for l in r.locs):
                continue
            out.append(r)
        return out

    def catalog_models(self, ref):
        """Daftar nama model dari katalog (None kalau WAD belum ada di katalog)."""
        return catalog().get("GoW%d" % ref.game, {}).get(ref.name.upper())

    # ---- baca
    def wad_data(self, ref, pak=None):
        if ref.name in self.pending:
            return bytes(self.pending[ref.name])
        k = (ref.name, pak)
        if k not in self.wad_cache:
            if len(self.wad_cache) > 6:
                self.wad_cache.pop(next(iter(self.wad_cache)))
            if ref.path:
                self.wad_cache[k] = open(ref.path, "rb").read()
            else:
                loc = next((l for l in ref.locs if pak is None or l[0] == pak), ref.locs[0])
                self.wad_cache[k] = self.disc.read_pak(loc[0], loc[1], ref.size)
        return self.wad_cache[k]

    def models_of(self, ref, pak=None):
        k = (ref.name, pak)
        if k not in self.model_cache:
            try:
                d = self.wad_data(ref, pak)
                if not wad_magic_ok(d[:32]):
                    log("WAD tidak valid di lokasi:", ref.name, ref.locs or ref.path)
                    self.model_cache[k] = None
                else:
                    ents = parse_wad(d)
                    models = [m for m in find_models(ents)
                              if any(has_mesh(blob_of(d, g)) for g in m["geom"])]
                    self.model_cache[k] = (ents, models)
            except Exception:
                log(traceback.format_exc())
                self.model_cache[k] = None
        return self.model_cache[k]

    def model_count(self, ref, pak=None):
        cm = self.catalog_models(ref)
        if cm is not None:
            return len(cm)
        r = self.models_of(ref, pak)
        return len(r[1]) if r else 0

    # ---- ekstrak / replace
    def export_model(self, ref, model, outdir, pak=None):
        d = self.wad_data(ref, pak)
        mesh = decode_model(d, model)
        base = os.path.join(outdir, model["name"])
        if mesh:
            write_gltf(mesh, base + ".gltf")
            return base + ".gltf", True
        open(base + "_def.bin", "wb").write(d[model["def"]["off"]:model["def"]["off"] + model["def"]["size"]])
        for g in model["geom"]:
            open(os.path.join(outdir, g["name"] + ".bin"), "wb").write(d[g["off"]:g["off"] + g["size"]])
        return base + "_def.bin", False

    def capacity(self, ref, model, pak=None):
        """(vertex terpakai model asli, total slot vertex). Model baru muat kira-kira cap/1.6 segitiga."""
        d = self.wad_data(ref, pak)
        pk = collect_packets(d, model)
        used = len(decode_blob(blob_of(d, model["geom"][0]))["positions"]) if model["geom"] else 0
        return used, sum(p["n"] for p in pk)

    def export_all(self, ref, outdir, pak=None, progress=None):
        """Ekstrak semua model di satu WAD ke outdir/NAMA_WAD/. Return (jumlah glTF, jumlah data mentah, folder)."""
        r = self.models_of(ref, pak)
        models = r[1] if r else []
        folder = os.path.join(outdir, os.path.splitext(ref.name)[0])
        os.makedirs(folder, exist_ok=True)
        ok = raw = 0
        for i, m in enumerate(models):
            if progress:
                progress(i + 1, len(models), m["name"])
            try:
                _, is_gltf = self.export_model(ref, m, folder, pak)
                ok += is_gltf
                raw += (not is_gltf)
            except Exception:
                log(traceback.format_exc())
                raw += 1
        return ok, raw, folder

    def stage_replace(self, ref, model, gltf_path, pak=None, simplify=False):
        """Siapkan Replace di memori (belum ditulis). Tulis ke ISO/file lewat save()."""
        d = self.wad_data(ref, pak)
        new, info = encode_model(read_gltf(gltf_path), d, model, simplify)
        if len(new) != len(d):
            raise ValueError("Ukuran WAD berubah, dibatalkan")
        self.pending[ref.name] = bytearray(new)
        return info

    def has_pending(self):
        return bool(self.pending)

    def discard(self):
        self.pending.clear()
        self.wad_cache.clear()
        self.model_cache.clear()

    # ---- simpan
    def save(self):
        """Tulis semua perubahan. ISO: ke SEMUA salinan WAD, dengan backup & pemeriksaan penanda WAD.
        Return daftar baris ringkasan."""
        if not self.pending:
            return []
        lines = []
        if self.kind == "iso":
            for name, data in self.pending.items():          # periksa dulu semua lokasi
                for pk, off in self.by_name[name].locs:
                    if not wad_magic_ok(self.disc.read_pak(pk, off, 32)):
                        raise ValueError("Penanda WAD tidak cocok di %s @%d (%s). Tidak ada yang ditulis."
                                         % (pk, off, name))
            for name, data in self.pending.items():
                ref = self.by_name[name]
                for pk, off in ref.locs:
                    bk_add(self.backup_path, pk, off, self.disc.read_pak(pk, off, ref.size))
                    self.disc.write_pak(pk, off, bytes(data))
                lines.append("%s -> %d lokasi di ISO" % (name, len(ref.locs)))
            lines.append("Backup: " + self.backup_path)
        else:
            for name, data in self.pending.items():
                fp = self.by_name[name].path
                if not os.path.exists(fp + ".bak"):
                    with open(fp, "rb") as f, open(fp + ".bak", "wb") as b:
                        b.write(f.read())
                with open(fp, "wb") as f:
                    f.write(bytes(data))
                lines.append("%s ditulis (backup %s.bak)" % (name, os.path.basename(fp)))
        self.pending.clear()
        self.wad_cache.clear()
        self.model_cache.clear()
        return lines

    def save_as(self, ref, outpath, pak=None):
        open(outpath, "wb").write(self.wad_data(ref, pak))

    def restore(self):
        """Kembalikan ISO dari backup (semua lokasi yang pernah ditimpa)."""
        if self.kind != "iso":
            raise ValueError("Kembalikan hanya untuk ISO")
        recs = bk_read(self.backup_path)
        if not recs:
            raise ValueError("Tidak ada backup (%s)" % self.backup_path)
        for pk, off, data in recs:
            self.disc.write_pak(pk, off, data)
        self.wad_cache.clear()
        self.model_cache.clear()
        return len(recs)


def open_source(path):
    """Buka ISO / WAD / folder dari satu path (juga dipakai kolom path untuk Wine)."""
    path = path.strip().strip('"')
    if os.path.isdir(path):
        return Session("folder", path=path)
    ext = os.path.splitext(path)[1].lower()
    if ext == ".wad":
        return Session("wad", path=path)
    return Session("iso", disc=ISO(path), path=path)


# ======================================================================
# Bahasa (Indonesia / English / Português)
# ======================================================================
TXT = {
    "id": {
        "open_iso": "Buka ISO", "open_wad": "Buka WAD", "open_folder": "Buka folder WAD", "open_path": "Buka path",
        "export": "Ekstrak Model (glTF)", "replace": "Replace Model (glTF)", "save": "SAVE",
        "save_as": "Simpan sebagai", "restore": "Kembalikan ISO", "discard": "Batalkan perubahan",
        "catalog": "Katalog Model", "dump": "Dump Log", "lang": "Bahasa", "path": "Path ISO / WAD / folder:",
        "none": "Belum ada sumber dibuka", "reading": "Membaca...", "checking": "Memeriksa %d WAD...",
        "pick_model": "Pilih satu model di dalam WAD dulu.", "pick_wad": "Pilih satu WAD dulu.",
        "dest": "Folder tujuan", "saved_to": "Disimpan: %s", "no_mesh": "Mesh tidak bisa dibaca; data mentah disimpan di: %s",
        "staged": "Model disiapkan:\n%s\n\nKlik SAVE untuk menulis ke %s.", "target_iso": "ISO", "target_wad": "file WAD",
        "confirm_save": "Tulis %d WAD yang diubah?\nKerjakan di SALINAN. Isi asli di-backup.",
        "saved": "Selesai:\n%s", "restored": "ISO dikembalikan (%d lokasi).", "discarded": "Perubahan dibatalkan.",
        "no_pending": "Belum ada perubahan.", "err": "Error", "err_where": "Detail error ada di:\n%s",
        "log_done": "Ditulis ke gow_debug.log (kirim ke pembuat)", "model_n": "%s  (%d model)",
        "cat_title": "Katalog Model", "search": "Cari:", "open_this": "Buka WAD ini", "cat_open_first": "Buka ISO/folder dulu.",
        "not_found": "WAD itu tidak ada di sumber yang terbuka.", "unsaved": "Ada perubahan belum di-SAVE. Keluar?",
        "ask_simplify": "Model terlalu detail (%d segitiga) untuk slot model ini.\nKurangi poligon otomatis sampai muat?\n(Bentuk model tetap, jumlah poligon berkurang.)",
        "export_all": "Ekstrak semua model WAD", "exported_all": "%d model diekstrak (glTF) ke:\n%s\n(%d tidak terbaca sebagai mesh)", "capacity": "%s: %d vertex terpakai, kapasitas %d slot (muat kira-kira %d segitiga model baru)",
        "pick_lang": "Pilih bahasa / Choose language", "files": "File gltf", "all": "Semua",
    },
    "en": {
        "open_iso": "Open ISO", "open_wad": "Open WAD", "open_folder": "Open WAD folder", "open_path": "Open path",
        "export": "Extract Model (glTF)", "replace": "Replace Model (glTF)", "save": "SAVE",
        "save_as": "Save as", "restore": "Restore ISO", "discard": "Discard changes",
        "catalog": "Model Catalog", "dump": "Dump Log", "lang": "Language", "path": "ISO / WAD / folder path:",
        "none": "No source opened", "reading": "Reading...", "checking": "Checking %d WADs...",
        "pick_model": "Select a model inside a WAD first.", "pick_wad": "Select a WAD first.",
        "dest": "Destination folder", "saved_to": "Saved: %s", "no_mesh": "Mesh could not be read; raw data saved to: %s",
        "staged": "Model prepared:\n%s\n\nClick SAVE to write to the %s.", "target_iso": "ISO", "target_wad": "WAD file",
        "confirm_save": "Write %d modified WAD(s)?\nWork on a COPY. Originals are backed up.",
        "saved": "Done:\n%s", "restored": "ISO restored (%d locations).", "discarded": "Changes discarded.",
        "no_pending": "No changes yet.", "err": "Error", "err_where": "Error details are in:\n%s",
        "log_done": "Written to gow_debug.log (send it to the author)", "model_n": "%s  (%d models)",
        "cat_title": "Model Catalog", "search": "Search:", "open_this": "Open this WAD", "cat_open_first": "Open an ISO/folder first.",
        "not_found": "That WAD is not in the opened source.", "unsaved": "There are unsaved changes. Quit?",
        "ask_simplify": "The model is too detailed (%d triangles) for this model's slots.\nReduce polygons automatically until it fits?\n(Shape stays, fewer polygons.)",
        "export_all": "Extract all models of WAD", "exported_all": "%d models extracted (glTF) to:\n%s\n(%d not readable as mesh)", "capacity": "%s: %d vertices used, capacity %d slots (about %d triangles of a new model fit)",
        "pick_lang": "Pilih bahasa / Choose language", "files": "glTF files", "all": "All",
    },
    "pt": {
        "open_iso": "Abrir ISO", "open_wad": "Abrir WAD", "open_folder": "Abrir pasta WAD", "open_path": "Abrir caminho",
        "export": "Extrair Modelo (glTF)", "replace": "Substituir Modelo (glTF)", "save": "SALVAR",
        "save_as": "Salvar como", "restore": "Restaurar ISO", "discard": "Descartar alterações",
        "catalog": "Catálogo de Modelos", "dump": "Dump Log", "lang": "Idioma", "path": "Caminho ISO / WAD / pasta:",
        "none": "Nenhuma fonte aberta", "reading": "Lendo...", "checking": "Verificando %d WADs...",
        "pick_model": "Selecione um modelo dentro de um WAD.", "pick_wad": "Selecione um WAD.",
        "dest": "Pasta de destino", "saved_to": "Salvo: %s", "no_mesh": "Malha ilegível; dados brutos salvos em: %s",
        "staged": "Modelo preparado:\n%s\n\nClique em SALVAR para gravar no %s.", "target_iso": "ISO", "target_wad": "arquivo WAD",
        "confirm_save": "Gravar %d WAD(s) alterado(s)?\nTrabalhe em uma CÓPIA. Os originais ficam em backup.",
        "saved": "Concluído:\n%s", "restored": "ISO restaurada (%d locais).", "discarded": "Alterações descartadas.",
        "no_pending": "Sem alterações.", "err": "Erro", "err_where": "Detalhes do erro em:\n%s",
        "log_done": "Gravado em gow_debug.log (envie ao autor)", "model_n": "%s  (%d modelos)",
        "cat_title": "Catálogo de Modelos", "search": "Buscar:", "open_this": "Abrir este WAD", "cat_open_first": "Abra uma ISO/pasta primeiro.",
        "not_found": "Esse WAD não está na fonte aberta.", "unsaved": "Há alterações não salvas. Sair?",
        "ask_simplify": "O modelo é detalhado demais (%d triângulos) para os slots deste modelo.\nReduzir polígonos automaticamente até caber?\n(A forma continua, menos polígonos.)",
        "export_all": "Extrair todos os modelos do WAD", "exported_all": "%d modelos extraídos (glTF) em:\n%s\n(%d ilegíveis como malha)", "capacity": "%s: %d vértices usados, capacidade %d slots (cabem cerca de %d triângulos de um novo modelo)",
        "pick_lang": "Pilih bahasa / Choose language", "files": "Arquivos glTF", "all": "Todos",
    },
}
LANG_NAMES = {"id": "Indonesia", "en": "English", "pt": "Português"}


def settings_path():
    return os.path.join(app_dir(), "gow_model_tool_settings.json")


def load_settings():
    try:
        return json.load(open(settings_path(), encoding="utf-8"))
    except Exception:
        return {}


def save_settings(d):
    try:
        json.dump(d, open(settings_path(), "w", encoding="utf-8"))
    except Exception:
        pass


# ======================================================================
# GUI
# ======================================================================
def run_gui():
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox

    class App(tk.Tk):
        def __init__(self):
            super().__init__()
            self.title("GoW PS2 Model Tool")
            self.geometry("980x640")
            self.s = None
            self.meta = {}
            self.st = load_settings()
            self.lang = self.st.get("lang") if self.st.get("lang") in TXT else None
            self.widgets = {}
            self.report_callback_exception = self.on_exc
            if not self.lang:
                self.lang = self.choose_lang()
                self.st["lang"] = self.lang
                save_settings(self.st)
            self.build()
            self.protocol("WM_DELETE_WINDOW", self.on_close)

        # --- util
        def t(self, k, *a):
            s = TXT[self.lang][k]
            return s % a if a else s

        def choose_lang(self):
            win = tk.Toplevel(self)
            win.title("Language")
            var = tk.StringVar(value="id")
            ttk.Label(win, text=TXT["id"]["pick_lang"]).pack(padx=12, pady=8)
            for code, nm in LANG_NAMES.items():
                ttk.Radiobutton(win, text=nm, value=code, variable=var).pack(anchor="w", padx=20)
            ttk.Button(win, text="OK", command=win.destroy).pack(pady=8)
            win.grab_set()
            self.wait_window(win)
            return var.get()

        def on_exc(self, exc, val, tb):
            txt = "".join(traceback.format_exception(exc, val, tb))
            log(txt)
            where = log_error(txt)
            messagebox.showerror(self.t("err"), "%s\n\n%s" % (val, self.t("err_where", where)))

        def fail(self, e):
            txt = traceback.format_exc()
            log(txt)
            where = log_error(txt)
            messagebox.showerror(self.t("err"), "%s\n\n%s" % (e, self.t("err_where", where)))

        def say(self, text):
            self.after(0, lambda: self.status.config(text=text))

        # --- UI
        def build(self):
            top = ttk.Frame(self)
            top.pack(fill="x", padx=6, pady=(6, 2))
            self.path_lbl = ttk.Label(top)
            self.path_lbl.pack(side="left")
            self.path_var = tk.StringVar()
            ttk.Entry(top, textvariable=self.path_var).pack(side="left", fill="x", expand=True, padx=6)
            self.add_btn(top, "open_path", self.open_path)
            row1 = ttk.Frame(self)
            row1.pack(fill="x", padx=6, pady=2)
            self.add_btn(row1, "open_iso", self.open_iso)
            self.add_btn(row1, "open_wad", self.open_wad)
            self.add_btn(row1, "open_folder", self.open_folder)
            self.add_btn(row1, "catalog", self.show_catalog)
            row2 = ttk.Frame(self)
            row2.pack(fill="x", padx=6, pady=2)
            self.add_btn(row2, "export", self.extract)
            self.add_btn(row2, "export_all", self.extract_all)
            self.add_btn(row2, "replace", self.replace)
            self.add_btn(row2, "save", self.do_save)
            self.add_btn(row2, "save_as", self.do_save_as)
            self.add_btn(row2, "restore", self.do_restore)
            self.add_btn(row2, "discard", self.do_discard)
            self.add_btn(row2, "dump", self.dump_log)
            self.lang_lbl = ttk.Label(row2)
            self.lang_lbl.pack(side="left", padx=(12, 2))
            self.lang_box = ttk.Combobox(row2, values=list(LANG_NAMES.values()), width=10, state="readonly")
            self.lang_box.set(LANG_NAMES[self.lang])
            self.lang_box.pack(side="left")
            self.lang_box.bind("<<ComboboxSelected>>", self.on_lang)
            self.status = ttk.Label(self)
            self.status.pack(fill="x", padx=8)
            self.tree = ttk.Treeview(self, show="tree")
            self.tree.pack(fill="both", expand=True, padx=6, pady=6)
            self.tree.bind("<<TreeviewOpen>>", self.on_open)
            self.tree.bind("<<TreeviewSelect>>", self.on_select)
            self.apply_lang()
            self.status.config(text=self.t("none"))

        def add_btn(self, parent, key, cmd):
            b = ttk.Button(parent, command=cmd)
            b.pack(side="left", padx=2)
            self.widgets[key] = b

        def apply_lang(self):
            for k, w in self.widgets.items():
                w.config(text=self.t(k))
            self.path_lbl.config(text=self.t("path"))
            self.lang_lbl.config(text=self.t("lang") + ":")

        def on_lang(self, _=None):
            nm = self.lang_box.get()
            self.lang = next(c for c, n in LANG_NAMES.items() if n == nm)
            self.st["lang"] = self.lang
            save_settings(self.st)
            self.apply_lang()

        def on_close(self):
            if self.s and self.s.has_pending() and not messagebox.askyesno("", self.t("unsaved")):
                return
            self.destroy()

        # --- buka sumber
        def pick(self, title, types):
            return filedialog.askopenfilename(title=title, filetypes=types)

        def open_iso(self):
            p = self.pick(self.t("open_iso"), [("ISO", "*.iso *.bin *.img"), (self.t("all"), "*.*")])
            if p:
                self.path_var.set(p)
                self.open_path()

        def open_wad(self):
            p = self.pick(self.t("open_wad"), [("WAD", "*.wad *.WAD"), (self.t("all"), "*.*")])
            if p:
                self.path_var.set(p)
                self.open_path()

        def open_folder(self):
            p = filedialog.askdirectory(title=self.t("open_folder"))
            if p:
                self.path_var.set(p)
                self.open_path()

        def open_path(self):
            p = self.path_var.get().strip()
            if not p:
                return
            self.say(self.t("reading"))

            def work():
                try:
                    s = open_source(p)
                    self.after(0, lambda: self.loaded(s, p))
                except Exception as e:
                    log(traceback.format_exc())
                    where = log_error(traceback.format_exc())
                    msg = "%s\n\n%s" % (e, self.t("err_where", where))
                    self.after(0, lambda: messagebox.showerror(self.t("err"), msg))
            threading.Thread(target=work, daemon=True).start()

        def loaded(self, s, p):
            self.s = s
            self.meta.clear()
            self.tree.delete(*self.tree.get_children())
            if s.kind == "iso":
                for pk in s.pak_names():
                    n = self.tree.insert("", "end", text=pk)
                    self.meta[n] = ("pak", pk)
                    self.tree.insert(n, "end", text="...")
            else:
                self.say(self.t("checking", len(s.refs)))
                threading.Thread(target=self.load_wads, args=(None, ""), daemon=True).start()
            self.status.config(text="GoW%s - %s  [%s]" % (s.game, os.path.basename(p), s.method))

        # --- daftar WAD (di thread)
        def load_wads(self, parent_item, pak):
            s = self.s
            res = []
            wads = s.wads_in(pak or None)
            for i, r in enumerate(wads):
                n = s.model_count(r, pak or None)
                if n:
                    res.append((r, n))
                if i % 10 == 0:
                    self.say(self.t("checking", len(wads)))
            self.after(0, lambda: self.fill_wads(parent_item, pak, res))

        def fill_wads(self, parent_item, pak, res):
            for r, n in res:
                mark = " *" if self.s.has_pending() and r.name in self.s.pending else ""
                it = self.tree.insert(parent_item or "", "end", text=self.t("model_n", r.name, n) + mark)
                self.meta[it] = ("wad", r, pak or None)
                self.tree.insert(it, "end", text="...")
            self.status.config(text="%d WAD" % len(res))

        def placeholder(self, item):
            ch = self.tree.get_children(item)
            return len(ch) == 1 and self.tree.item(ch[0], "text") == "..."

        def on_open(self, _=None):
            item = self.tree.focus()
            m = self.meta.get(item)
            if not m or not self.placeholder(item):
                return
            self.tree.delete(*self.tree.get_children(item))
            if m[0] == "pak":
                threading.Thread(target=self.load_wads, args=(item, m[1]), daemon=True).start()
            elif m[0] == "wad":
                r = self.s.models_of(m[1], m[2])
                for md in (r[1] if r else []):
                    n = self.tree.insert(item, "end", text=md["name"])
                    self.meta[n] = ("model", m[1], md, m[2])

        # --- model terpilih
        def sel_model(self):
            m = self.meta.get(self.tree.focus())
            if not m or m[0] != "model":
                messagebox.showinfo("", self.t("pick_model"))
                return None
            return m[1], m[2], m[3]

        def on_select(self, _=None):
            m = self.meta.get(self.tree.focus())
            if self.s and m and m[0] == "model":
                try:
                    used, cap = self.s.capacity(m[1], m[2], m[3])
                    self.status.config(text=self.t("capacity", m[2]["name"], used, cap, int(cap / 1.6)))
                except Exception:
                    log(traceback.format_exc())

        def extract_all(self):
            m = self.meta.get(self.tree.focus())
            if not m or m[0] not in ("wad", "model"):
                messagebox.showinfo("", self.t("pick_wad"))
                return
            ref, pak = m[1], (m[2] if m[0] == "wad" else m[3])
            out = filedialog.askdirectory(title=self.t("dest"))
            if not out:
                return

            def work():
                try:
                    ok, raw, folder = self.s.export_all(
                        ref, out, pak, lambda i, n, nm: self.say("%d/%d  %s" % (i, n, nm)))
                    self.after(0, lambda: messagebox.showinfo("OK", self.t("exported_all", ok, folder, raw)))
                except Exception as e:
                    self.after(0, lambda: self.fail(e))
            threading.Thread(target=work, daemon=True).start()

        def extract(self):
            sm = self.sel_model()
            if not sm:
                return
            out = filedialog.askdirectory(title=self.t("dest"))
            if not out:
                return
            try:
                path, ok = self.s.export_model(sm[0], sm[1], out, sm[2])
                messagebox.showinfo("OK", self.t("saved_to", path)) if ok else \
                    messagebox.showwarning("", self.t("no_mesh", out))
            except Exception as e:
                self.fail(e)

        def replace(self):
            sm = self.sel_model()
            if not sm:
                return
            p = self.pick(self.t("replace"), [(self.t("files"), "*.gltf"), (self.t("all"), "*.*")])
            if not p:
                return
            try:
                try:
                    info = self.s.stage_replace(sm[0], sm[1], p, sm[2])
                except CapacityError as ce:
                    if not messagebox.askyesno(self.t("replace"), self.t("ask_simplify", ce.need)):
                        raise
                    info = self.s.stage_replace(sm[0], sm[1], p, sm[2], simplify=True)
                tgt = self.t("target_iso") if self.s.kind == "iso" else self.t("target_wad")
                messagebox.showinfo("OK", self.t("staged", info, tgt))
                self.mark_pending(sm[0])
            except Exception as e:
                self.fail(e)

        def mark_pending(self, ref):
            for it, m in self.meta.items():
                if m[0] == "wad" and m[1].name == ref.name:
                    txt = self.tree.item(it, "text")
                    if not txt.endswith(" *"):
                        self.tree.item(it, text=txt + " *")

        def do_save(self):
            if not self.s or not self.s.has_pending():
                messagebox.showinfo("", self.t("no_pending"))
                return
            if not messagebox.askyesno(self.t("save"), self.t("confirm_save", len(self.s.pending))):
                return
            try:
                lines = self.s.save()
                for it in self.meta:
                    txt = self.tree.item(it, "text") if self.meta[it][0] == "wad" else None
                    if txt and txt.endswith(" *"):
                        self.tree.item(it, text=txt[:-2])
                messagebox.showinfo("OK", self.t("saved", "\n".join(lines)))
            except Exception as e:
                self.fail(e)

        def do_save_as(self):
            m = self.meta.get(self.tree.focus())
            if not m or m[0] not in ("wad", "model"):
                messagebox.showinfo("", self.t("pick_wad"))
                return
            ref, pak = m[1], (m[2] if m[0] == "wad" else m[3])
            p = filedialog.asksaveasfilename(initialfile=ref.name)
            if p:
                try:
                    self.s.save_as(ref, p, pak)
                    messagebox.showinfo("OK", self.t("saved_to", p))
                except Exception as e:
                    self.fail(e)

        def do_restore(self):
            if not self.s:
                return
            try:
                n = self.s.restore()
                messagebox.showinfo("OK", self.t("restored", n))
            except Exception as e:
                self.fail(e)

        def do_discard(self):
            if self.s:
                self.s.discard()
                for it, m in self.meta.items():
                    if m[0] == "wad" and self.tree.item(it, "text").endswith(" *"):
                        self.tree.item(it, text=self.tree.item(it, "text")[:-2])
                messagebox.showinfo("", self.t("discarded"))

        # --- katalog
        def show_catalog(self):
            win = tk.Toplevel(self)
            win.title(self.t("cat_title"))
            win.geometry("720x520")
            top = ttk.Frame(win)
            top.pack(fill="x", padx=6, pady=6)
            ttk.Label(top, text=self.t("search")).pack(side="left")
            q = tk.StringVar()
            ttk.Entry(top, textvariable=q).pack(side="left", fill="x", expand=True, padx=6)
            lst = ttk.Treeview(win, columns=("game", "n", "models"), show="headings")
            for c, w in (("game", 60), ("n", 50), ("models", 520)):
                lst.heading(c, text={"game": "Game", "n": "#", "models": "WAD / Model"}[c])
                lst.column(c, width=w)
            lst.pack(fill="both", expand=True, padx=6)
            rows = []
            for g, d in catalog().items():
                for wad, ms in sorted(d.items()):
                    if ms:
                        rows.append((g, wad, ms))

            def refill(*_):
                lst.delete(*lst.get_children())
                k = q.get().strip().lower()
                for g, wad, ms in rows:
                    if not k or k in wad.lower() or any(k in m.lower() for m in ms):
                        lst.insert("", "end", values=(g, len(ms), "%s: %s" % (wad, ", ".join(ms[:6]) + (" ..." if len(ms) > 6 else ""))))
            q.trace_add("write", refill)
            refill()

            def go():
                sel = lst.selection()
                if not sel:
                    return
                wad = lst.item(sel[0], "values")[2].split(":")[0]
                if not self.s:
                    messagebox.showinfo("", self.t("cat_open_first"))
                    return
                if not self.goto_wad(wad):
                    messagebox.showinfo("", self.t("not_found"))
            ttk.Button(win, text=self.t("open_this"), command=go).pack(pady=6)

        def goto_wad(self, name):
            s = self.s
            ref = s.by_name.get(name.upper())
            if not ref:
                return False
            if s.kind == "iso":
                for it, m in list(self.meta.items()):
                    if m[0] == "pak" and any(l[0] == m[1] for l in ref.locs):
                        self.tree.item(it, open=True)
                        if self.placeholder(it):
                            self.tree.focus(it)
                            self.on_open()
                        self.say(name)
                        return True
            for it, m in self.meta.items():
                if m[0] == "wad" and m[1].name == name.upper():
                    self.tree.selection_set(it)
                    self.tree.focus(it)
                    return True
            return True

        def dump_log(self):
            out = ["=== LOG ==="] + LOG
            if self.s:
                out.append("=== sumber: %s, game GoW%s, %d WAD ===" % (self.s.kind, self.s.game, len(self.s.refs)))
                for r in self.s.refs[:20]:
                    out.append("%s size=%d locs=%s path=%s" % (r.name, r.size, r.locs, r.path))
            open("gow_debug.log", "w").write("\n".join(out))
            messagebox.showinfo("Log", self.t("log_done"))

    App().mainloop()


if __name__ == "__main__":
    run_gui()
