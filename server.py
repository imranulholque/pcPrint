import os, sys, io, re, uuid, time, json, shutil, tempfile, threading, functools
from flask import Flask, request, jsonify, send_file
import fitz
from PIL import Image, ImageOps, ImageDraw, ImageFont, ImageGrab
import printing


def res(p):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, p)


app = Flask(__name__, static_folder=None)
FILES = {}
ORDER = []
JOBS = {}
LOCK = threading.Lock()    # fitz is not thread safe
SLOCK = threading.Lock()   # file list changes
REV = {"n": 0}
NOTICE = {"t": ""}
BATCH = {"t": 0.0, "ids": []}
HOOKS = {"pick": None, "front": None, "autostart_get": None, "autostart_set": None}   # set by app.py (file dialog, bring window to front)
IMG_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff"}
SCALES = ("fit", "actual", "fill", "custom")

CFG_DIR = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "PCPrint")
PROF_FILE = os.path.join(CFG_DIR, "profiles.json")
SET_FILE = os.path.join(CFG_DIR, "settings.json")
PLOCK = threading.Lock()
DIALOG_LOCK = threading.Lock()


_UP = {"dir": None, "n": 0}   # temp folder for dropped / pasted files


def upload_dir():
    if not _UP["dir"] or not os.path.isdir(_UP["dir"]):
        _UP["dir"] = tempfile.mkdtemp(prefix="pcprint_")
    return _UP["dir"]


def cleanup():
    if _UP["dir"]:
        shutil.rmtree(_UP["dir"], ignore_errors=True)


@app.before_request
def only_local():
    if request.remote_addr not in ("127.0.0.1", "::1"):
        return "forbidden", 403


# ------------------------------------------------------------------ files
def bump():
    REV["n"] += 1


def natural(name):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", name.lower())]


def open_file(path):
    ext = os.path.splitext(path)[1].lower()
    name = os.path.basename(path)
    if ext == ".pdf":
        with LOCK:
            d = fitz.open(path)
            n = d.page_count
            lands = []
            for i in range(n):
                r = d.load_page(i).rect
                lands.append(r.width > r.height)
            d.close()
        if n < 1:
            raise ValueError("empty PDF")
        kind = "pdf"
    elif ext in IMG_EXT:
        with Image.open(path) as im:
            im.verify()
        with Image.open(path) as im:
            w, h = im.size
            if im.getexif().get(274, 1) in (5, 6, 7, 8):
                w, h = h, w
        kind, n, lands = "img", 1, [w > h]
    else:
        raise ValueError("only PDF and image files are supported")
    fid = uuid.uuid4().hex[:10]
    FILES[fid] = {"id": fid, "name": name, "kind": kind, "pages": n, "lands": lands, "path": path}
    return fid


def register(paths, sort=True):
    """Add files. Files that arrive within 1.5 s of each other (one 'Open with' on
    many files) are treated as one batch and sorted by name."""
    added, errors = 0, []
    with SLOCK:
        if sort:
            now = time.time()
            if now - BATCH["t"] > 1.5:
                BATCH["ids"] = []
            BATCH["t"] = now
        for p in paths:
            p = os.path.abspath(p)
            if any(f["path"] == p for f in FILES.values()):
                continue
            try:
                fid = open_file(p)
            except Exception as e:
                errors.append(f"{os.path.basename(p)}: {e}")
                continue
            ORDER.append(fid)
            added += 1
            if sort:
                BATCH["ids"].append(fid)
        if sort and len(BATCH["ids"]) > 1:
            ids = BATCH["ids"]
            k = len(ids)
            if ORDER[-k:] == ids:
                ordered = sorted(ids, key=lambda i: natural(FILES[i]["name"]))
                ORDER[-k:] = ordered
                BATCH["ids"] = ordered
        if errors:
            NOTICE["t"] = "\n".join(errors)
        if added or errors:
            bump()
    return added, errors


def pub(f):
    return {"id": f["id"], "name": f["name"], "kind": f["kind"], "pages": f["pages"], "lands": f["lands"]}


@app.get("/")
def index():
    return send_file(res("static/index.html"))


@app.get("/api/ping")
def ping():
    return jsonify(app="pcprint")


@app.post("/api/add")
def add():
    d = request.get_json(silent=True) or {}
    paths = [str(p) for p in d.get("paths", [])]
    added, errors = register(paths, sort=True)
    if HOOKS["front"]:
        try:
            HOOKS["front"]()
        except Exception:
            pass
    return jsonify(added=added, errors=errors)


@app.post("/api/pick")
def pick():
    if not HOOKS["pick"]:
        return jsonify(error="No file dialog"), 500
    try:
        paths = HOOKS["pick"]()
    except Exception as e:
        return jsonify(error=str(e)), 500
    added, errors = register(paths or [], sort=False)
    return jsonify(added=added, errors=errors)


def save_upload(up):
    """Save one dropped/pasted file into the temp folder, keeping its real name."""
    name = os.path.basename((up.filename or "").replace("\\", "/")).strip()
    name = re.sub(r'[<>:"/\\|?*]', "_", name)
    ext = os.path.splitext(name)[1].lower()
    mt = (up.mimetype or "").lower()
    generic = name.lower() in ("", "image.png", "image.jpg", "image.jpeg", "blob")
    if generic or not ext:
        e = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif",
             "image/bmp": ".bmp", "application/pdf": ".pdf"}.get(mt)
        if e:   # a pasted picture (screenshot) has no real name
            _UP["n"] += 1
            name = f"Pasted image {_UP['n']}{e}" if e != ".pdf" else f"Pasted file {_UP['n']}.pdf"
    d = os.path.join(upload_dir(), uuid.uuid4().hex[:8])
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, name or "file")
    up.save(path)
    return path


@app.post("/api/upload")
def upload():
    """Files dropped on the window or pasted with Ctrl+V."""
    paths = [save_upload(u) for u in request.files.getlist("file")]
    if not paths:
        return jsonify(added=0, errors=["No file received"])
    added, errors = register(paths, sort=True)
    return jsonify(added=added, errors=errors)


@app.post("/api/paste")
def paste():
    """Ctrl+V fallback: read the Windows clipboard (copied files, or a copied picture)."""
    try:
        data = ImageGrab.grabclipboard()
    except Exception as e:
        return jsonify(added=0, errors=[f"Cannot read the clipboard: {e}"])
    if isinstance(data, list):   # files copied in Explorer
        added, errors = register([p for p in data if os.path.isfile(p)], sort=True)
        return jsonify(added=added, errors=errors)
    if data is not None:         # a copied picture
        _UP["n"] += 1
        d = os.path.join(upload_dir(), uuid.uuid4().hex[:8])
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, f"Pasted image {_UP['n']}.png")
        data.convert("RGB").save(path)
        added, errors = register([path], sort=True)
        return jsonify(added=added, errors=errors)
    return jsonify(added=0, errors=["The clipboard has no file or picture"])


@app.get("/api/autostart")
def autostart_get():
    h = HOOKS.get("autostart_get")
    return jsonify(available=bool(h), on=bool(h()) if h else False)


@app.post("/api/autostart")
def autostart_set():
    h = HOOKS.get("autostart_set")
    if not h:
        return jsonify(error="Not available"), 400
    on = bool((request.get_json(silent=True) or {}).get("on"))
    try:
        h(on)
    except Exception as e:
        return jsonify(error=str(e)), 500
    return jsonify(on=on)


@app.get("/api/state")
def state():
    with SLOCK:
        notice, NOTICE["t"] = NOTICE["t"], ""
        return jsonify(rev=REV["n"], notice=notice, files=[pub(FILES[i]) for i in ORDER if i in FILES])


@app.delete("/api/files/<fid>")
def del_one(fid):
    with SLOCK:
        if FILES.pop(fid, None) is not None and fid in ORDER:
            ORDER.remove(fid)
            bump()
    return jsonify(ok=True)


@app.delete("/api/files")
def del_all():
    with SLOCK:
        FILES.clear()
        ORDER.clear()
        bump()
    return jsonify(ok=True)


@app.post("/api/move")
def move():
    d = request.get_json(silent=True) or {}
    fid, step = d.get("id"), -1 if d.get("dir") == "up" else 1
    with SLOCK:
        if fid in ORDER:
            i = ORDER.index(fid)
            j = i + step
            if 0 <= j < len(ORDER):
                ORDER[i], ORDER[j] = ORDER[j], ORDER[i]
                bump()
    return jsonify(ok=True)


# ------------------------------------------------------------- settings
@app.get("/api/settings")
def settings_get():
    try:
        with open(SET_FILE, "r", encoding="utf-8") as fh:
            return jsonify(json.load(fh))
    except Exception:
        return jsonify({})


@app.post("/api/settings")
def settings_set():
    d = request.get_json(silent=True) or {}
    try:
        os.makedirs(CFG_DIR, exist_ok=True)
        with open(SET_FILE, "w", encoding="utf-8") as fh:
            json.dump(d, fh)
    except Exception:
        pass
    return jsonify(ok=True)


# ------------------------------------------------------- page numbers
def gnum(fid, p):
    """1-based position of a page in the whole document (all files in order)."""
    n = 0
    for i in list(ORDER):
        f = FILES.get(i)
        if not f:
            continue
        if i == fid:
            return n + p + 1
        n += f["pages"]
    return None


def numspec(args, fid, p):
    if str(args.get("num")) not in ("1", "true", "True"):
        return None
    pos = args.get("npos") if args.get("npos") in ("top", "bottom") else "bottom"
    align = args.get("nalign") if args.get("nalign") in ("left", "center", "right") else "center"
    try:
        size = max(6.0, min(72.0, float(args.get("nsize", 12))))
    except Exception:
        size = 12.0
    try:
        start = int(float(args.get("nstart", 1)))
    except Exception:
        start = 1
    try:
        margin = max(2.0, min(40.0, float(args.get("nmargin", 8))))
    except Exception:
        margin = 8.0
    bold = str(args.get("nbold")) in ("1", "true", "True")
    n = gnum(fid, p)   # position in the whole document, so one file keeps its serial number
    if n is None:
        return None
    return {"text": str(start - 1 + n), "pos": pos, "align": align, "size": size,
            "margin": margin, "bold": bold}


@functools.lru_cache(maxsize=64)
def get_font(px, bold=False):
    win = os.environ.get("WINDIR", r"C:\Windows")
    names = (["timesbd.ttf", "Times New Roman Bold.ttf", "LiberationSerif-Bold.ttf", "DejaVuSerif-Bold.ttf"]
             if bold else
             ["times.ttf", "Times New Roman.ttf", "LiberationSerif-Regular.ttf", "DejaVuSerif.ttf"])
    for name in [os.path.join(win, "Fonts", names[0])] + names:
        try:
            return ImageFont.truetype(name, max(6, int(px)))
        except Exception:
            pass
    try:
        return ImageFont.load_default(size=max(6, int(px)))
    except Exception:
        return ImageFont.load_default()


def stamp(img, num, ppmm):
    """Print the page number (Times New Roman) directly on the page, at the chosen place."""
    d = ImageDraw.Draw(img)
    fnt = get_font(round(num["size"] * 25.4 / 72 * ppmm), num["bold"])
    text = num["text"]
    l, t, r, b = d.textbbox((0, 0), text, font=fnt)
    tw, th = r - l, b - t
    W, H = img.size
    m = int(num["margin"] * ppmm)
    if num["align"] == "left":
        x = m
    elif num["align"] == "right":
        x = W - m - tw
    else:
        x = (W - tw) // 2
    y = m if num["pos"] == "top" else H - m - th
    d.text((x - l, y - t), text, font=fnt, fill=0 if img.mode == "L" else (0, 0, 0))


# ------------------------------------------------------------- drawing
def get_pct(v):
    """Custom scaling percent (of actual size), kept between 10 and 400."""
    try:
        return max(10.0, min(400.0, float(v)))
    except Exception:
        return 100.0


def compose(f, idx, W, H, mono, rotate=True, mode="fit", ppmm=None, pct=100.0):
    """Draw one page on a white sheet of W x H pixels.
    mode "fit":    whole page fitted inside the area, nothing cut off
    mode "actual": real size (needs ppmm = pixels per mm), may be cut at the edges
    mode "fill":   page covers the whole area, the extra part is cropped
    mode "custom": real size x pct/100, centered (needs ppmm), may be cut at the edges"""
    def pick(fw, fh, unit_mm):
        if mode == "fill":
            s = max(W / fw, H / fh)
        elif mode == "actual" and ppmm:
            s = ppmm * unit_mm
        elif mode == "custom" and ppmm:
            s = ppmm * unit_mm * pct / 100.0
        else:
            s = min(W / fw, H / fh)
        return min(s, 8000.0 / max(fw, fh))  # safety cap for huge pages

    if f["kind"] == "pdf":
        with LOCK:
            doc = fitz.open(f["path"])
            try:
                pg = doc[idx]
                pw, ph = pg.rect.width, pg.rect.height
                rot = rotate and ((pw > ph) != (W > H))
                fw, fh = (ph, pw) if rot else (pw, ph)
                s = pick(fw, fh, 25.4 / 72)
                pix = pg.get_pixmap(matrix=fitz.Matrix(s, s),
                                    colorspace=fitz.csGRAY if mono else fitz.csRGB, alpha=False)
                img = Image.frombytes("L" if mono else "RGB", (pix.width, pix.height), pix.samples)
            finally:
                doc.close()
    else:
        img = Image.open(f["path"])
        try:
            dpi = float((img.info.get("dpi") or (300, 300))[0])
        except Exception:
            dpi = 300.0
        if dpi < 1:
            dpi = 300.0
        img = ImageOps.exif_transpose(img)
        if img.mode in ("RGBA", "LA", "P"):
            img = img.convert("RGBA")
            bg = Image.new("RGB", img.size, "white")
            bg.paste(img, mask=img.split()[3])
            img = bg
        else:
            img = img.convert("RGB")
        iw, ih = img.size
        rot = rotate and ((iw > ih) != (W > H))
        fw, fh = (ih, iw) if rot else (iw, ih)
        s = pick(fw, fh, 25.4 / dpi)
        img = img.resize((max(1, int(iw * s)), max(1, int(ih * s))), Image.LANCZOS)
        if mono:
            img = img.convert("L")
    if rot:
        img = img.rotate(90, expand=True)
    canvas = Image.new(img.mode, (W, H), 255 if img.mode == "L" else (255, 255, 255))
    canvas.paste(img, ((W - img.width) // 2, (H - img.height) // 2))  # larger image is cropped
    return canvas


def sheet(f, idx, W, H, mono, mode="fit", ppmm=None, pct=100.0, num=None, rotate=True):
    """One printed sheet: the page, with the page number printed directly on it."""
    canvas = compose(f, idx, W, H, mono, rotate, mode, ppmm, pct)
    if num and ppmm:
        stamp(canvas, num, ppmm)
    return canvas


def get_page():
    f = FILES.get(request.args.get("f", ""))
    try:
        p = int(request.args.get("p", 0))
    except Exception:
        return None, None
    if not f or not (0 <= p < f["pages"]):
        return None, None
    return f, p


def send_img(img):
    b = io.BytesIO()
    img.save(b, "JPEG", quality=85)
    b.seek(0)
    r = send_file(b, mimetype="image/jpeg")
    r.headers["Cache-Control"] = "no-store"
    return r


def get_area(q):
    """Paper + printable area from the real driver; falls back to plain paper size."""
    orient = q.get("orient", "portrait")
    pw = float(q.get("w", 210))
    ph = float(q.get("h", 297))
    if (orient == "landscape") != (pw > ph):
        pw, ph = ph, pw
    a = {"pw": pw, "ph": ph, "ox": 0, "oy": 0, "aw": pw, "ah": ph, "driver": False}
    try:
        a = printing.area(q.get("printer", ""), q.get("paper"), q.get("color") == "color",
                          q.get("duplex", "off"), orient)
        a["driver"] = True
    except Exception:
        pass
    return a


@app.get("/api/thumb")
def thumb():
    f, p = get_page()
    if not f:
        return "", 404
    return send_img(compose(f, p, 240, 320, False, rotate=False))


@app.get("/api/view")
def view():
    """Reader: the page as it looks on its own sheet, with the page number."""
    f, p = get_page()
    if not f:
        return "", 404
    if f["kind"] == "pdf":
        with LOCK:
            doc = fitz.open(f["path"])
            try:
                r = doc[p].rect
                wmm, hmm = r.width * 25.4 / 72, r.height * 25.4 / 72
            finally:
                doc.close()
    else:  # an image is shown on an A4 sheet
        wmm, hmm = (297.0, 210.0) if f["lands"][0] else (210.0, 297.0)
    W = 1000 if wmm > hmm else 800
    ppmm = W / wmm
    H = max(100, int(hmm * ppmm))
    num = numspec(request.args, f["id"], p)
    img = sheet(f, p, W, H, False, "fit", ppmm, 100.0, num, rotate=False)
    return send_img(img)


@app.get("/api/area")
def area():
    return jsonify(get_area(request.args))


@app.get("/api/preview")
def preview():
    f, p = get_page()
    if not f:
        return "", 404
    a = get_area(request.args)
    mode = request.args.get("scale", "fit")
    if mode not in SCALES:
        mode = "fit"
    W = 720
    s = W / a["pw"]  # pixels per mm on the preview sheet
    H = max(100, int(a["ph"] * s))
    ox, oy = int(a["ox"] * s), int(a["oy"] * s)
    AW = max(1, min(W - ox, int(a["aw"] * s)))
    AH = max(1, min(H - oy, int(a["ah"] * s)))
    mono = request.args.get("color") != "color"
    num = numspec(request.args, f["id"], p)
    page = sheet(f, p, AW, AH, mono, mode, s, get_pct(request.args.get("pct")), num).convert("RGB")
    img = Image.new("RGB", (W, H), (205, 208, 214))  # gray = area the printer cannot print on
    img.paste(page, (ox, oy))
    d = ImageDraw.Draw(img)
    blue = (37, 99, 235)
    x1, y1, x2, y2 = ox, oy, ox + AW - 1, oy + AH - 1
    for x in range(x1, x2, 12):  # dashed printable-area border
        d.line((x, y1, min(x + 6, x2), y1), fill=blue, width=2)
        d.line((x, y2, min(x + 6, x2), y2), fill=blue, width=2)
    for y in range(y1, y2, 12):
        d.line((x1, y, x1, min(y + 6, y2)), fill=blue, width=2)
        d.line((x2, y, x2, min(y + 6, y2)), fill=blue, width=2)
    d.rectangle((0, 0, W - 1, H - 1), outline=(120, 124, 132), width=2)
    return send_img(img)


@app.get("/api/printers")
def printers():
    try:
        return jsonify(printing.list_printers())
    except Exception as e:
        return jsonify(error=str(e)), 500


@app.get("/api/printer")
def printer():
    try:
        return jsonify(printing.caps(request.args.get("name", "")))
    except Exception as e:
        return jsonify(error=str(e)), 500


# ------------------------------------------------------------- profiles
def load_profiles():
    try:
        with open(PROF_FILE, "r", encoding="utf-8") as fh:
            d = json.load(fh)
            return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_profiles(d):
    os.makedirs(CFG_DIR, exist_ok=True)
    tmp = PROF_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(d, fh)
    os.replace(tmp, PROF_FILE)


def find_profile(printer_name, pid):
    if not pid:
        return None
    with PLOCK:
        for p in load_profiles().get(printer_name, []):
            if p["id"] == pid:
                return p["data"]
    return None


@app.get("/api/profiles")
def profiles_list():
    name = request.args.get("printer", "")
    with PLOCK:
        items = load_profiles().get(name, [])
    return jsonify([{"id": p["id"], "name": p["name"]} for p in items])


@app.post("/api/profiles/new")
def profiles_new():
    """Opens the driver's own settings window (like Word's Printer Properties)."""
    d = request.get_json(silent=True) or {}
    printer_name = str(d.get("printer", ""))
    pname = str(d.get("name", "")).strip()[:40]
    if not printer_name or not pname:
        return jsonify(error="Printer and profile name are required"), 400
    if DIALOG_LOCK.locked():
        return jsonify(error="A settings window is already open"), 409
    base = find_profile(printer_name, d.get("base"))
    jid = uuid.uuid4().hex[:8]
    JOBS[jid] = {"state": "run", "kind": "profile"}

    def run():
        with DIALOG_LOCK:
            try:
                data = printing.driver_dialog(printer_name, base)
                if data is None:
                    JOBS[jid]["state"] = "cancel"
                    return
                pid = uuid.uuid4().hex[:8]
                with PLOCK:
                    allp = load_profiles()
                    allp.setdefault(printer_name, []).append({"id": pid, "name": pname, "data": data})
                    save_profiles(allp)
                JOBS[jid].update(state="done", profile={"id": pid, "name": pname})
            except Exception as e:
                JOBS[jid].update(state="error", error=str(e))

    threading.Thread(target=run, daemon=True).start()
    return jsonify(id=jid)


@app.delete("/api/profiles/<pid>")
def profiles_delete(pid):
    name = request.args.get("printer", "")
    with PLOCK:
        allp = load_profiles()
        allp[name] = [p for p in allp.get(name, []) if p["id"] != pid]
        save_profiles(allp)
    return jsonify(ok=True)


# ---------------------------------------------------------------- print
@app.post("/api/print")
def do_print():
    d = request.get_json(silent=True) or {}
    try:
        pages = [(FILES[f], int(p)) for f, p in d["pages"]]
    except Exception:
        return jsonify(error="Files not found. Add them again."), 400
    if not pages:
        return jsonify(error="No pages selected"), 400
    try:
        copies = max(1, min(99, int(d.get("copies", 1))))
    except Exception:
        copies = 1
    orient = "landscape" if d.get("orient") == "landscape" else "portrait"
    jid = uuid.uuid4().hex[:8]
    JOBS[jid] = {"state": "run", "done": 0, "total": len(pages) * copies}
    color = d.get("color") == "color"
    profile = find_profile(d.get("printer", ""), d.get("profile"))  # None = normal way
    mode = d.get("scale") if d.get("scale") in SCALES else "fit"
    pct = get_pct(d.get("pct"))
    nd = d.get("num") or {}
    nargs = {"num": "1" if nd.get("on") else "0", "npos": nd.get("pos"), "nalign": nd.get("align"),
             "nsize": nd.get("size"), "nstart": nd.get("start"),
             "nmargin": nd.get("margin"), "nbold": "1" if nd.get("bold") else "0"}

    def run():
        try:
            area_mm = get_area({"printer": d["printer"], "paper": d.get("paper"),
                                "color": "color" if color else "mono",
                                "duplex": d.get("duplex", "off"), "orient": orient})["aw"]
            printing.print_job(
                d["printer"], d.get("paper"), color, d.get("duplex", "off"), orient, copies, len(pages),
                lambda i, W, H, m: sheet(pages[i][0], pages[i][1], W, H, m, mode, W / area_mm, pct,
                                         numspec(nargs, pages[i][0]["id"], pages[i][1])),
                lambda done, total: JOBS[jid].update(done=done, total=total),
                d.get("title", "PC Print"), profile=profile)
            JOBS[jid]["state"] = "done"
        except Exception as e:
            JOBS[jid].update(state="error", error=str(e))

    threading.Thread(target=run, daemon=True).start()
    return jsonify(id=jid)


@app.get("/api/job/<jid>")
def job(jid):
    return jsonify(JOBS.get(jid, {"state": "error", "error": "unknown job"}))
