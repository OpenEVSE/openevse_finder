#!/usr/bin/env python3
"""OpenEVSE Finder — find your OpenEVSE charging station on your network.

Discovers OpenEVSE WiFi modules via mDNS (the firmware advertises
``_openevse._tcp`` and ``_http._tcp`` as ``openevse-XXXX.local``) with a
subnet-scan fallback that probes ``http://<ip>/status``. Lets the user open
the charger's web UI, copy its address, or create a desktop shortcut.

Runtime dependency: zeroconf (``pip install zeroconf``). Everything else is
Python standard library.
"""

import ipaddress
import json
import platform
import queue
import socket
import sys
import threading
import urllib.request
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import tkinter as tk
from tkinter import messagebox, ttk

try:
    from zeroconf import ServiceBrowser, Zeroconf
    HAVE_ZEROCONF = True
except ImportError:
    HAVE_ZEROCONF = False

APP_TITLE = "OpenEVSE Finder"
MDNS_SERVICES = ["_openevse._tcp.local.", "_http._tcp.local."]
AUTO_SCAN_AFTER_MS = 6000   # start a subnet scan if mDNS finds nothing
PROBE_TIMEOUT = 2.0         # per-host HTTP timeout during subnet scan
                            # (a real charger has been measured at ~1.4 s)
SCAN_WORKERS = 64


# ---------------------------------------------------------------------------
# Device probing helpers (no GUI code here — called from worker threads)
# ---------------------------------------------------------------------------

def http_get_json(host, path, port=80, timeout=PROBE_TIMEOUT):
    """GET http://host:port/path and parse JSON; return None on any failure."""
    hostpart = f"[{host}]" if ":" in host else host
    url = f"http://{hostpart}:{port}{path}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            return json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:
        return None


def looks_like_openevse(status):
    """Heuristic: does this /status JSON come from an OpenEVSE WiFi module?"""
    if not isinstance(status, dict):
        return False
    return "ipaddress" in status and any(
        key in status for key in ("free_heap", "espfree", "mode", "srssi")
    )


def probe_device(host, port=80, timeout=PROBE_TIMEOUT):
    """Probe one host. Returns a device dict if it is an OpenEVSE, else None."""
    status = http_get_json(host, "/status", port, timeout)
    if not looks_like_openevse(status):
        return None
    device = {
        "ip": status.get("ipaddress") or host,
        "port": port,
        "hostname": "",
        "name": "",
        "version": "",
    }
    # /config carries the friendly hostname and firmware version
    config = http_get_json(host, "/config", port, timeout=2.0)
    if isinstance(config, dict):
        hostname = config.get("hostname", "")
        if hostname and not hostname.endswith(".local"):
            hostname += ".local"
        device["hostname"] = hostname
        device["version"] = config.get("version", "")
    device["name"] = device["hostname"].split(".")[0] or f"OpenEVSE at {device['ip']}"
    return device


def local_ipv4():
    """Best-effort local IPv4 via the UDP-connect trick (no packets sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def hostname_resolves(host, timeout=3.0):
    """Check whether the OS resolver can resolve `host` (e.g. foo.local)."""
    result = []

    def worker():
        try:
            socket.getaddrinfo(host, 80, proto=socket.IPPROTO_TCP)
            result.append(True)
        except OSError:
            result.append(False)

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join(timeout)
    return bool(result and result[0])


def create_shortcut(name, url):
    """Write a desktop shortcut file for `url`; returns the created Path."""
    desktop = Path.home() / "Desktop"
    if not desktop.is_dir():
        desktop = Path.home()
    safe = "".join(c for c in name if c not in '\\/:*?"<>|').strip() or "OpenEVSE"
    system = platform.system()
    if system == "Windows":
        path = desktop / f"{safe}.url"
        path.write_text(f"[InternetShortcut]\nURL={url}\n", encoding="utf-8")
    elif system == "Darwin":
        path = desktop / f"{safe}.webloc"
        path.write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"'
            ' "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            '<plist version="1.0">\n<dict>\n'
            f"\t<key>URL</key>\n\t<string>{url}</string>\n"
            "</dict>\n</plist>\n",
            encoding="utf-8",
        )
    else:
        path = desktop / f"{safe}.desktop"
        path.write_text(
            "[Desktop Entry]\nEncoding=UTF-8\n"
            f"Name={name}\nType=Link\nURL={url}\nIcon=text-html\n",
            encoding="utf-8",
        )
        path.chmod(0o755)
    return path


# ---------------------------------------------------------------------------
# mDNS listener
# ---------------------------------------------------------------------------

class MdnsListener:
    """Feeds discovered OpenEVSE services into the app's event queue."""

    def __init__(self, events):
        self.events = events

    def add_service(self, zc, type_, name):
        # Only follow _http._tcp entries that look like an OpenEVSE
        if type_ == "_http._tcp.local." and not name.lower().startswith("openevse"):
            return
        threading.Thread(
            target=self._resolve, args=(zc, type_, name), daemon=True
        ).start()

    update_service = add_service

    def remove_service(self, zc, type_, name):
        pass  # keep devices listed; stale entries are harmless here

    def _resolve(self, zc, type_, name):
        info = zc.get_service_info(type_, name, timeout=3000)
        if info is None:
            return
        addresses = info.parsed_scoped_addresses()
        ipv4 = next((a for a in addresses if ":" not in a), None)
        if ipv4 is None:
            return
        props = {}
        for key, value in (info.properties or {}).items():
            try:
                props[key.decode()] = (value or b"").decode()
            except (UnicodeDecodeError, AttributeError):
                continue
        hostname = (info.server or "").rstrip(".")
        port = info.port or 80
        # Only _openevse._tcp is authoritative; an _http._tcp name starting
        # with "openevse" may be another gadget (e.g. an ESPHome display) —
        # confirm it actually answers like an OpenEVSE before listing it.
        if type_ == "_http._tcp.local.":
            if not looks_like_openevse(http_get_json(ipv4, "/status", port, timeout=2.0)):
                return
        device = {
            "ip": ipv4,
            "port": port,
            "hostname": hostname,
            "name": hostname.split(".")[0] or name.split(".")[0],
            "version": props.get("version", ""),
            "source": "mDNS",
        }
        self.events.put(("device", device))


# ---------------------------------------------------------------------------
# GUI application
# ---------------------------------------------------------------------------

class FinderApp:
    def __init__(self, root):
        self.root = root
        self.events = queue.Queue()
        self.devices = {}          # ip -> device dict
        self.host_ok = {}          # hostname -> bool (resolver check cache)
        self.scanning = False
        self.zeroconf = None
        self.browsers = []

        self._build_ui()
        self._start_mdns()
        self.root.after(100, self._poll_events)
        self.root.after(AUTO_SCAN_AFTER_MS, self._auto_scan)

    # ---- UI construction -------------------------------------------------

    def _build_ui(self):
        self.root.title(APP_TITLE)
        self.root.geometry("720x420")
        self.root.minsize(600, 340)

        header = ttk.Frame(self.root, padding=(12, 10, 12, 4))
        header.pack(fill="x")
        ttk.Label(header, text=APP_TITLE, font=("", 16, "bold")).pack(side="left")
        self.status_var = tk.StringVar(value="Searching your network for OpenEVSE chargers…")
        ttk.Label(header, textvariable=self.status_var).pack(side="right")

        body = ttk.Frame(self.root, padding=(12, 4))
        body.pack(fill="both", expand=True)

        columns = ("name", "address", "ip", "version", "source")
        self.tree = ttk.Treeview(body, columns=columns, show="headings", selectmode="browse")
        headings = {
            "name": ("Charger", 170),
            "address": ("Address", 180),
            "ip": ("IP address", 120),
            "version": ("Firmware", 100),
            "source": ("Found via", 80),
        }
        for col, (text, width) in headings.items():
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, anchor="w")
        scroll = ttk.Scrollbar(body, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._update_buttons())
        self.tree.bind("<Double-1>", lambda e: self.open_in_browser())

        buttons = ttk.Frame(self.root, padding=(12, 6))
        buttons.pack(fill="x")
        self.btn_open = ttk.Button(buttons, text="Open in Browser", command=self.open_in_browser)
        self.btn_copy = ttk.Button(buttons, text="Copy Address", command=self.copy_address)
        self.btn_shortcut = ttk.Button(buttons, text="Create Desktop Shortcut", command=self.make_shortcut)
        self.btn_scan = ttk.Button(buttons, text="Scan Network", command=self.start_scan)
        for btn in (self.btn_open, self.btn_copy, self.btn_shortcut):
            btn.state(["disabled"])
            btn.pack(side="left", padx=(0, 6))
        self.btn_scan.pack(side="right")

        manual = ttk.Frame(self.root, padding=(12, 2, 12, 4))
        manual.pack(fill="x")
        ttk.Label(manual, text="Or type an IP / hostname:").pack(side="left")
        self.manual_var = tk.StringVar()
        entry = ttk.Entry(manual, textvariable=self.manual_var, width=28)
        entry.pack(side="left", padx=6)
        entry.bind("<Return>", lambda e: self.test_manual())
        ttk.Button(manual, text="Test", command=self.test_manual).pack(side="left")

        self.hint_var = tk.StringVar(value="")
        ttk.Label(self.root, textvariable=self.hint_var, padding=(12, 0, 12, 8),
                  foreground="#555").pack(fill="x")

    # ---- mDNS ------------------------------------------------------------

    def _start_mdns(self):
        if not HAVE_ZEROCONF:
            self.status_var.set("mDNS unavailable — using network scan")
            self.hint_var.set(
                "The 'zeroconf' package is not installed (pip install zeroconf); "
                "falling back to scanning your network."
            )
            return
        try:
            self.zeroconf = Zeroconf()
            listener = MdnsListener(self.events)
            self.browsers = [
                ServiceBrowser(self.zeroconf, service, listener)
                for service in MDNS_SERVICES
            ]
        except OSError as err:
            self.zeroconf = None
            self.status_var.set("mDNS unavailable — using network scan")
            self.hint_var.set(f"Could not start mDNS discovery ({err}); use Scan Network.")

    # ---- Event pump --------------------------------------------------------

    def _poll_events(self):
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "device":
                    self._add_device(payload)
                elif kind == "status":
                    self.status_var.set(payload)
                elif kind == "scan_done":
                    self.scanning = False
                    self.btn_scan.state(["!disabled"])
                    self.btn_scan.configure(text="Scan Network")
                    count = len(self.devices)
                    self.status_var.set(
                        f"Found {count} charger{'s' if count != 1 else ''}"
                        if count else "No chargers found — check WiFi and try again"
                    )
                elif kind == "manual_result":
                    device, query = payload
                    if device:
                        device["source"] = "manual"
                        self._add_device(device)
                        self.status_var.set(f"Found charger at {query}")
                    else:
                        messagebox.showwarning(
                            APP_TITLE,
                            f"No OpenEVSE charger answered at “{query}”.\n\n"
                            "Check the address and make sure you are on the same "
                            "WiFi network as the charger.",
                        )
                elif kind == "host_check":
                    hostname, ok = payload
                    self.host_ok[hostname] = ok
        except queue.Empty:
            pass
        self.root.after(100, self._poll_events)

    def _add_device(self, device):
        ip = device["ip"]
        existing = self.devices.get(ip)
        if existing:
            # Merge, preferring non-empty new values but keeping mDNS as source
            for key in ("hostname", "name", "version"):
                if device.get(key):
                    existing[key] = device[key]
            if device.get("source") == "mDNS":
                existing["source"] = "mDNS"
            device = existing
        else:
            device.setdefault("source", "scan")
            self.devices[ip] = device
            # Kick off a resolver check for the .local hostname in the background
            if device.get("hostname"):
                self._check_hostname(device["hostname"])

        row = (
            device.get("name") or "OpenEVSE",
            device.get("hostname") or device["ip"],
            device["ip"],
            device.get("version") or "?",
            device.get("source", "scan"),
        )
        if self.tree.exists(ip):
            self.tree.item(ip, values=row)
        else:
            self.tree.insert("", "end", iid=ip, values=row)
            if len(self.tree.selection()) == 0:
                self.tree.selection_set(ip)
        count = len(self.devices)
        self.status_var.set(f"Found {count} charger{'s' if count != 1 else ''}")
        self._update_buttons()

    def _check_hostname(self, hostname):
        if not hostname or hostname in self.host_ok:
            return

        def worker():
            self.events.put(("host_check", (hostname, hostname_resolves(hostname))))

        threading.Thread(target=worker, daemon=True).start()

    # ---- Subnet scan -------------------------------------------------------

    def _auto_scan(self):
        if not self.devices and not self.scanning:
            self.start_scan()

    def start_scan(self):
        if self.scanning:
            return
        my_ip = local_ipv4()
        if my_ip is None:
            messagebox.showwarning(
                APP_TITLE,
                "Could not determine this computer's network address.\n"
                "Make sure you are connected to your WiFi network.",
            )
            return
        self.scanning = True
        self.btn_scan.state(["disabled"])
        self.btn_scan.configure(text="Scanning…")
        network = ipaddress.ip_network(f"{my_ip}/24", strict=False)
        self.status_var.set(f"Scanning {network} …")
        threading.Thread(target=self._scan_worker, args=(network, my_ip), daemon=True).start()

    def _scan_worker(self, network, my_ip):
        hosts = [str(h) for h in network.hosts() if str(h) != my_ip]
        with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as pool:
            for device in pool.map(probe_device, hosts):
                if device:
                    self.events.put(("device", device))
        self.events.put(("scan_done", None))

    # ---- Manual test -------------------------------------------------------

    def test_manual(self):
        query = self.manual_var.get().strip()
        if not query:
            return
        host, _, port = query.partition(":")
        try:
            port = int(port) if port else 80
        except ValueError:
            port = 80
        self.status_var.set(f"Testing {query} …")

        def worker():
            device = probe_device(host, port, timeout=3.0)
            self.events.put(("manual_result", (device, query)))

        threading.Thread(target=worker, daemon=True).start()

    # ---- Selected-device actions -------------------------------------------

    def _selected(self):
        selection = self.tree.selection()
        return self.devices.get(selection[0]) if selection else None

    def _update_buttons(self):
        state = ["!disabled"] if self._selected() else ["disabled"]
        for btn in (self.btn_open, self.btn_copy, self.btn_shortcut):
            btn.state(state)

    def best_url(self, device):
        """Prefer the .local hostname if this computer can resolve it."""
        hostname = device.get("hostname", "")
        use_host = hostname and self.host_ok.get(hostname, False)
        host = hostname if use_host else device["ip"]
        port = device.get("port", 80)
        url = f"http://{host}/" if port == 80 else f"http://{host}:{port}/"
        return url, use_host

    def _ip_warning(self, device):
        hostname = device.get("hostname")
        if hostname:
            return (f"Saved by IP address — this can change when your router restarts; "
                    f"hostname {hostname} did not resolve on this computer.")
        return "Saved by IP address — this can change when your router restarts."

    def open_in_browser(self):
        device = self._selected()
        if not device:
            return
        url, used_host = self.best_url(device)
        webbrowser.open(url)
        hint = "Tip: press Ctrl+D (Cmd+D on Mac) in your browser to bookmark this page."
        if not used_host:
            hint += "  " + self._ip_warning(device)
        self.hint_var.set(hint)

    def copy_address(self):
        device = self._selected()
        if not device:
            return
        url, used_host = self.best_url(device)
        self.root.clipboard_clear()
        self.root.clipboard_append(url)
        hint = f"Copied {url} to the clipboard."
        if not used_host:
            hint += "  " + self._ip_warning(device)
        self.hint_var.set(hint)

    def make_shortcut(self):
        device = self._selected()
        if not device:
            return
        url, used_host = self.best_url(device)
        label = device.get("hostname", "").split(".")[0] or device["ip"]
        try:
            path = create_shortcut(f"OpenEVSE ({label})", url)
        except OSError as err:
            messagebox.showerror(APP_TITLE, f"Could not create the shortcut:\n{err}")
            return
        message = f"Shortcut created:\n{path}"
        if not used_host:
            message += "\n\n" + self._ip_warning(device)
        messagebox.showinfo(APP_TITLE, message)

    # ---- Shutdown ------------------------------------------------------------

    def close(self):
        if self.zeroconf is not None:
            try:
                self.zeroconf.close()
            except Exception:
                pass
        self.root.destroy()


def main():
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista" if platform.system() == "Windows" else "clam")
    except tk.TclError:
        pass
    app = FinderApp(root)
    root.protocol("WM_DELETE_WINDOW", app.close)
    root.mainloop()


if __name__ == "__main__":
    sys.exit(main())
