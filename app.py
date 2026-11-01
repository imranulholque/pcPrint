import os, sys, json, time, threading, urllib.request
if sys.stdout is None: sys.stdout = open(os.devnull, "w")
if sys.stderr is None: sys.stderr = open(os.devnull, "w")
import logging
logging.getLogger("werkzeug").setLevel(logging.ERROR)

from werkzeug.serving import make_server
import server

MUTEX = None   # keeps the "one window only" lock alive
EXTS = (".pdf", ".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff")
win = None
tray = None
QUIT = {"v": False}


def tray_image():
    """Small icon for the system tray."""
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, 63, 63), radius=14, fill=(37, 99, 235))
    d.rectangle((20, 10, 44, 26), fill="white")
    d.rounded_rectangle((12, 24, 52, 46), radius=4, fill="white")
    d.rectangle((20, 38, 44, 54), fill="white", outline=(37, 99, 235), width=2)
    d.rectangle((24, 43, 40, 45), fill=(37, 99, 235))
    return img


def front():
    """Show the window (also when it is hidden in the tray) and bring it to the front."""
    try:
        win.show()
        win.restore()
        win.on_top = True
        win.on_top = False
    except Exception:
        pass


def quit_app(*a):
    QUIT["v"] = True
    try:
        win.destroy()
    except Exception:
        pass


def start_tray():
    """System tray icon: click = open, menu = Open / Quit. Returns True if it worked."""
    global tray
    try:
        import pystray
        import pystray._win32  # noqa: makes PyInstaller bundle the Windows backend
        menu = pystray.Menu(pystray.MenuItem("Open PC Print", lambda icon, item: front(), default=True),
                            pystray.MenuItem("Quit", lambda icon, item: quit_app()))
        tray = pystray.Icon("PCPrint", tray_image(), "PC Print", menu)
        tray.run_detached()
        return True
    except Exception:
        tray = None
        return False


def argv_paths():
    return [os.path.abspath(a) for a in sys.argv[1:] if not a.startswith("--") and os.path.isfile(a)]


def port_file():
    return os.path.join(server.CFG_DIR, "port.txt")


def is_primary():
    """True for the first launch. Later launches (Windows starts one process per file)
    hand their files to the first window. Uses a Windows mutex, which is reliable
    (two programs can share a TCP port on Windows, so the port is not used for this)."""
    global MUTEX
    try:
        import ctypes
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateMutexW.restype = ctypes.c_void_p
        MUTEX = k.CreateMutexW(None, False, "Local\\PCPrintSingleWindow")
        return ctypes.get_last_error() != 183   # 183 = already exists
    except Exception:
        return True


def ping(port):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=1) as r:
            return json.load(r).get("app") == "pcprint"
    except Exception:
        return False


def forward(paths):
    """Send files to the window that is already open (waits until it is ready)."""
    for _ in range(60):
        try:
            with open(port_file()) as fh:
                port = int(fh.read().strip())
            if ping(port):
                req = urllib.request.Request(f"http://127.0.0.1:{port}/api/add",
                                             data=json.dumps({"paths": paths}).encode(),
                                             headers={"Content-Type": "application/json"})
                urllib.request.urlopen(req, timeout=5).read()
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "PCPrint"


def autostart_get():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, RUN_NAME)
            return True
    except Exception:
        return False


def autostart_set(on):
    """Start with Windows (current user only, no admin needed). Launched with --startup."""
    import winreg
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
        if on:
            winreg.SetValueEx(k, RUN_NAME, 0, winreg.REG_SZ, f'"{sys.executable}" --startup')
        else:
            try:
                winreg.DeleteValue(k, RUN_NAME)
            except FileNotFoundError:
                pass


def make_send_to():
    """'Send to > PC Print' for many files at once (Windows then starts the app one time)."""
    try:
        import win32com.client
        folder = os.path.join(os.environ["APPDATA"], "Microsoft", "Windows", "SendTo")
        lnk = os.path.join(folder, "PC Print.lnk")
        if os.path.exists(lnk):
            return
        sc = win32com.client.Dispatch("WScript.Shell").CreateShortCut(lnk)
        sc.TargetPath = sys.executable
        sc.WorkingDirectory = os.path.dirname(sys.executable)
        sc.save()
    except Exception:
        pass


def register_open_with():
    """Adds 'PC Print' to the right-click > Open with menu (current user only, no admin)."""
    import winreg
    exe = sys.executable
    name = os.path.basename(exe)

    def setv(path, value_name, value):
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, path) as k:
            winreg.SetValueEx(k, value_name, 0, winreg.REG_SZ, value)

    base = r"Software\Classes\Applications" + "\\" + name
    setv(base, "FriendlyAppName", "PC Print")
    setv(base + r"\shell\open\command", "", f'"{exe}" "%1"')
    for ext in EXTS:
        setv(base + r"\SupportedTypes", ext, "")
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER,
                              r"Software\Classes" + "\\" + ext + r"\OpenWithList" + "\\" + name):
            pass
    try:
        import ctypes
        ctypes.windll.shell32.SHChangeNotify(0x08000000, 0, None, None)
    except Exception:
        pass


def unregister_open_with():
    import winreg
    name = os.path.basename(sys.executable)

    def kill(path):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as k:
                subs = [winreg.EnumKey(k, i) for i in range(winreg.QueryInfoKey(k)[0])]
            for sub in subs:
                kill(path + "\\" + sub)
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
        except Exception:
            pass

    try:
        os.remove(os.path.join(os.environ["APPDATA"], "Microsoft", "Windows", "SendTo", "PC Print.lnk"))
    except Exception:
        pass
    try:
        autostart_set(False)
    except Exception:
        pass
    kill(r"Software\Classes\Applications" + "\\" + name)
    for ext in EXTS:
        kill(r"Software\Classes" + "\\" + ext + r"\OpenWithList" + "\\" + name)


def main():
    if "--unregister" in sys.argv:
        unregister_open_with()
        return
    paths = argv_paths()

    startup = "--startup" in sys.argv   # started by Windows: open minimized
    if not is_primary():
        if not startup:
            forward(paths)
        return

    srv = make_server("127.0.0.1", 0, server.app, threaded=True)   # free port, only this PC
    port = srv.server_port
    try:
        os.makedirs(server.CFG_DIR, exist_ok=True)
        with open(port_file(), "w") as fh:
            fh.write(str(port))
    except Exception:
        pass
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    if getattr(sys, "frozen", False):
        try:
            register_open_with()
        except Exception:
            pass
        make_send_to()
    if paths:
        server.register(paths, sort=True)

    import webview
    global win
    tray_ok = start_tray()
    win = webview.create_window("PC Print", f"http://127.0.0.1:{port}/", width=1120, height=820,
                                min_size=(720, 520),
                                hidden=startup and tray_ok,        # started by Windows: only the tray icon
                                minimized=startup and not tray_ok)

    def on_closing(*a):
        # with "Start with Windows" on, the X button hides the window to the tray
        if not QUIT["v"] and tray and autostart_get():
            win.hide()
            return False
        return True

    win.events.closing += on_closing

    def pick():
        kind = webview.FileDialog.OPEN if hasattr(webview, "FileDialog") else webview.OPEN_DIALOG
        r = win.create_file_dialog(
            kind, allow_multiple=True,
            file_types=("PDF and images (*.pdf;*.jpg;*.jpeg;*.png;*.webp;*.bmp;*.gif;*.tif;*.tiff)",))
        return list(r or [])

    server.HOOKS["pick"] = pick
    server.HOOKS["front"] = front
    if getattr(sys, "frozen", False):
        server.HOOKS["autostart_get"] = autostart_get
        server.HOOKS["autostart_set"] = autostart_set
    webview.start()
    if tray:
        try:
            tray.stop()
        except Exception:
            pass
    try:
        srv.shutdown()
    except Exception:
        pass
    server.cleanup()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, str(e), "PC Print", 0x10)
        except Exception:
            pass
