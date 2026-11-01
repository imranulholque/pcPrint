import base64
import win32print, win32ui, win32gui, win32con, win32api
from PIL import ImageWin, Image

DM_ORIENTATION, DM_PAPERSIZE, DM_PAPERLENGTH, DM_PAPERWIDTH = 0x1, 0x2, 0x4, 0x8
DM_COPIES, DM_COLOR, DM_DUPLEX, DM_FORMNAME = 0x100, 0x800, 0x1000, 0x10000
DC_PAPERS, DC_PAPERSIZE, DC_DUPLEX, DC_PAPERNAMES, DC_COLORDEVICE = 2, 3, 7, 16, 32
HORZRES, VERTRES, LOGPIXELSX, LOGPIXELSY = 8, 10, 88, 90
PHYSICALWIDTH, PHYSICALHEIGHT, PHYSICALOFFSETX, PHYSICALOFFSETY = 110, 111, 112, 113

# DocumentProperties modes
DM_OUT_BUFFER, DM_IN_PROMPT, DM_IN_BUFFER = 2, 4, 8
IDOK = 1

# Public DEVMODE fields we save in a profile (driver private data is saved separately)
PROFILE_FIELDS = ["Orientation", "PaperSize", "PaperLength", "PaperWidth", "Scale",
                  "Copies", "DefaultSource", "PrintQuality", "Color", "Duplex",
                  "YResolution", "TTOption", "Collate", "MediaType", "DitherType",
                  "ICMMethod", "ICMIntent"]


def list_printers():
    default = None
    try:
        default = win32print.GetDefaultPrinter()
    except Exception:
        pass
    flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
    out = []
    for p in win32print.EnumPrinters(flags, None, 2):
        port = p.get("pPortName", "") or ""
        out.append({"name": p["pPrinterName"], "port": port,
                    "usb": port.upper().startswith("USB"),
                    "default": p["pPrinterName"] == default})
    out.sort(key=lambda x: (not x["usb"], x["name"].lower()))
    return out


def caps(name):
    h = win32print.OpenPrinter(name)
    try:
        info = win32print.GetPrinter(h, 2)
        try:
            forms = win32print.EnumForms(h)
        except Exception:
            forms = []
    finally:
        win32print.ClosePrinter(h)
    port = info.get("pPortName")
    dm = info.get("pDevMode")

    def cap(c):
        try:
            return win32print.DeviceCapabilities(name, port, c)
        except Exception:
            return None

    names = [str(n).strip("\x00").strip() for n in (cap(DC_PAPERNAMES) or [])]
    ids = cap(DC_PAPERS) or []
    sizes = cap(DC_PAPERSIZE) or []
    papers, seen = [], set()
    for i, n in enumerate(names):
        if not n or n in seen:
            continue
        seen.add(n)
        w = hh = None
        try:
            w, hh = sizes[i][0] / 10, sizes[i][1] / 10
        except Exception:
            pass
        papers.append({"name": n, "id": int(ids[i]) if i < len(ids) else None,
                       "w": w, "h": hh, "custom": False})
    for fm in forms:
        n = fm.get("Name")
        if n and n not in seen and fm.get("Flags", 1) in (0, 2):
            sz = fm.get("Size") or {}
            papers.append({"name": n, "id": None,
                           "w": (sz.get("cx") or 0) / 1000 or None,
                           "h": (sz.get("cy") or 0) / 1000 or None, "custom": True})
            seen.add(n)

    d = {"paper": None, "color": True, "duplex": "off"}
    if dm is not None:
        try:
            pid = dm.PaperSize
            for p in papers:
                if p["id"] == pid:
                    d["paper"] = p["name"]
            if pid == 256:
                pw, pl = int(dm.PaperWidth), int(dm.PaperLength)
                nm = f"Custom {pw/10:g} x {pl/10:g} mm"
                papers.append({"name": nm, "id": 256, "pw": pw, "pl": pl,
                               "w": pw / 10, "h": pl / 10, "custom": True})
                d["paper"] = nm
            elif d["paper"] is None:
                fn = (dm.FormName or "").strip("\x00")
                if fn:
                    if fn not in seen:
                        papers.append({"name": fn, "id": None, "w": None, "h": None, "custom": True})
                    d["paper"] = fn
        except Exception:
            pass
        try:
            d["color"] = dm.Color != 1
            d["duplex"] = {2: "long", 3: "short"}.get(dm.Duplex, "off")
        except Exception:
            pass
    return {"papers": papers, "color": cap(DC_COLORDEVICE) == 1,
            "duplex": cap(DC_DUPLEX) == 1, "defaults": d}


# ---------------------------------------------------------------- profiles
def _default_dm(name):
    """The driver's own current default settings (a copy; nothing is written back)."""
    h = win32print.OpenPrinter(name)
    try:
        dm = win32print.GetPrinter(h, 2).get("pDevMode")
    finally:
        win32print.ClosePrinter(h)
    if dm is None:
        raise RuntimeError("This printer driver gave no settings")
    return dm


def dm_to_profile(dm):
    """Turn a DEVMODE into a JSON-safe dict (public fields + driver private bytes)."""
    p = {"Fields": int(dm.Fields)}
    for k in PROFILE_FIELDS:
        try:
            p[k] = int(getattr(dm, k))
        except Exception:
            pass
    try:
        p["FormName"] = (dm.FormName or "").strip("\x00")
    except Exception:
        pass
    try:
        p["DriverData"] = base64.b64encode(bytes(dm.DriverData)).decode("ascii")
    except Exception:
        p["DriverData"] = ""
    return p


def apply_profile(dm, p):
    """Copy a saved profile onto a DEVMODE. Private driver bytes are only applied
    if the size matches this driver's, otherwise they are skipped (old/other driver)."""
    try:
        data = base64.b64decode(p.get("DriverData") or "")
        if data and len(data) == len(bytes(dm.DriverData)):
            dm.DriverData = data
    except Exception:
        pass
    for k in PROFILE_FIELDS:
        if k in p:
            try:
                setattr(dm, k, p[k])
            except Exception:
                pass
    if p.get("FormName"):
        try:
            dm.FormName = p["FormName"]
        except Exception:
            pass
    if "Fields" in p:
        try:
            dm.Fields = p["Fields"]
        except Exception:
            pass
    return dm


def _owner_window():
    """Tiny invisible always-on-top window; the driver window is owned by it,
    so the driver window opens on top of everything."""
    try:
        hwnd = win32gui.CreateWindowEx(
            win32con.WS_EX_TOPMOST | win32con.WS_EX_TOOLWINDOW, "STATIC", "PhonePrint",
            win32con.WS_POPUP, 0, 0, 0, 0, 0, 0, 0, None)
        win32gui.ShowWindow(hwnd, win32con.SW_SHOWNA)
        try:
            # a tap on ALT lets a background app take the foreground
            win32api.keybd_event(win32con.VK_MENU, 0, 0, 0)
            win32api.keybd_event(win32con.VK_MENU, 0, win32con.KEYEVENTF_KEYUP, 0)
            win32gui.SetForegroundWindow(hwnd)
        except Exception:
            pass
        return hwnd
    except Exception:
        return 0


def driver_dialog(name, base_profile=None):
    """Open the printer driver's own settings window (like Word's Printer Properties),
    on top of all other windows. Starts from the printer's current defaults (or from
    base_profile). Returns a profile dict, or None if the user pressed Cancel.
    Nothing is saved to Windows: printer defaults on the PC stay untouched."""
    base = _default_dm(name)
    if base_profile:
        apply_profile(base, base_profile)
    owner = _owner_window()
    h = win32print.OpenPrinter(name)
    try:
        # separate DEVMODE object used as the output buffer
        out = win32print.GetPrinter(h, 2).get("pDevMode")
        if out is None:
            raise RuntimeError("This printer driver gave no settings")
        r = win32print.DocumentProperties(owner, h, name, out, base,
                                          DM_IN_BUFFER | DM_IN_PROMPT | DM_OUT_BUFFER)
    finally:
        win32print.ClosePrinter(h)
        if owner:
            try:
                win32gui.DestroyWindow(owner)
            except Exception:
                pass
    if isinstance(r, int) and r != IDOK:
        return None
    result = out if (r is None or isinstance(r, int)) else r
    return dm_to_profile(result)


# ------------------------------------------------------------- print job
def build_dm(name, paper, color, duplex, orient, c, profile=None):
    """Take the driver's own default settings and change only what the user chose.
    If a profile is given, it is applied first (driver-specific options like paper
    type, quality, 2-sided); paper, orientation, color and copies still follow the phone."""
    dm = _default_dm(name)
    if profile:
        apply_profile(dm, profile)

    p = next((x for x in c["papers"] if x["name"] == paper), None)
    if p:
        if p.get("pw"):
            dm.PaperSize, dm.PaperWidth, dm.PaperLength = 256, p["pw"], p["pl"]
            dm.Fields |= DM_PAPERSIZE | DM_PAPERWIDTH | DM_PAPERLENGTH
        elif p["id"]:
            dm.PaperSize = p["id"]
            dm.Fields = (dm.Fields | DM_PAPERSIZE) & ~(DM_FORMNAME | DM_PAPERLENGTH | DM_PAPERWIDTH)
        else:
            dm.FormName = p["name"]
            dm.Fields = (dm.Fields | DM_FORMNAME) & ~(DM_PAPERSIZE | DM_PAPERLENGTH | DM_PAPERWIDTH)

    dm.Orientation = 2 if orient == "landscape" else 1
    dm.Fields |= DM_ORIENTATION

    dm.Copies = 1
    dm.Fields |= DM_COPIES

    if c["color"]:
        dm.Color = 2 if color else 1
        dm.Fields |= DM_COLOR
    else:
        color = False
    if c["duplex"] and not profile:
        # with a profile, the profile decides duplex
        dm.Duplex = {"long": 2, "short": 3}.get(duplex, 1)
        dm.Fields |= DM_DUPLEX
    return dm, color


def open_dc(name, dm):
    return win32ui.CreateDCFromHandle(win32gui.CreateDC("WINSPOOL", name, dm))


def area(name, paper, color, duplex, orient):
    """Real paper size and printable area (mm) as the driver reports them."""
    c = caps(name)
    dm, _ = build_dm(name, paper, color, duplex, orient, c)
    hdc = open_dc(name, dm)
    try:
        dx = hdc.GetDeviceCaps(LOGPIXELSX) or 300
        dy = hdc.GetDeviceCaps(LOGPIXELSY) or dx

        def mm(i, d):
            return hdc.GetDeviceCaps(i) / d * 25.4
        return {"pw": mm(PHYSICALWIDTH, dx), "ph": mm(PHYSICALHEIGHT, dy),
                "ox": mm(PHYSICALOFFSETX, dx), "oy": mm(PHYSICALOFFSETY, dy),
                "aw": mm(HORZRES, dx), "ah": mm(VERTRES, dy)}
    finally:
        hdc.DeleteDC()


def print_job(name, paper, color, duplex, orient, copies, n, provider, progress,
              title="Phone Print", profile=None):
    c = caps(name)
    try:
        dm, color = build_dm(name, paper, color, duplex, orient, c, profile)
        hdc = open_dc(name, dm)
    except Exception:
        if not profile:
            raise
        # profile did not work with this driver: fall back to the normal way
        profile = None
        dm, color = build_dm(name, paper, color, duplex, orient, c, None)
        hdc = open_dc(name, dm)

    try:
        two_sided = bool(c["duplex"]) and int(dm.Duplex) in (2, 3)
    except Exception:
        two_sided = False

    seq = []
    for k in range(copies):
        seq += list(range(n))
        if two_sided and n % 2 == 1 and k < copies - 1:
            seq.append(None)  # blank back side so the next copy starts on a new sheet

    try:
        W, H = hdc.GetDeviceCaps(HORZRES), hdc.GetDeviceCaps(VERTRES)
        dpi = hdc.GetDeviceCaps(LOGPIXELSX) or 300
        f = min(1.0, 300.0 / dpi)
        rw, rh = max(1, int(W * f)), max(1, int(H * f))
        hdc.StartDoc(title)
        for j, i in enumerate(seq):
            if i is None:
                img = Image.new("L" if not color else "RGB", (rw, rh), 255 if not color else (255, 255, 255))
            else:
                img = provider(i, rw, rh, not color)
            hdc.StartPage()
            ImageWin.Dib(img).draw(hdc.GetHandleOutput(), (0, 0, W, H))
            hdc.EndPage()
            progress(j + 1, len(seq))
        hdc.EndDoc()
    except Exception:
        try:
            hdc.AbortDoc()
        except Exception:
            pass
        raise
    finally:
        hdc.DeleteDC()
