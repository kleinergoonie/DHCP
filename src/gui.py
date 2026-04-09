"""tkinter GUI for the DHCP server."""

import ipaddress
import json
import os
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

from .dhcp_server import DHCPServer

CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'config.json')

# ---------------------------------------------------------------------------
# Colour palette
# ---------------------------------------------------------------------------
BG          = '#1e1e2e'
FG          = '#cdd6f4'
ACCENT      = '#89b4fa'
ACCENT_DARK = '#1e66f5'
GREEN       = '#a6e3a1'
RED         = '#f38ba8'
YELLOW      = '#f9e2af'
PANEL       = '#181825'
ROW_ODD     = '#252535'
ROW_EVEN    = '#1e1e2e'


class DHCPApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title('DHCP Server')
        self.geometry('980x680')
        self.minsize(820, 580)
        self.configure(bg=BG)
        self.resizable(True, True)

        self._config = self._load_config()
        self._server: DHCPServer | None = None
        self._log_lines: list[str] = []

        self._build_ui()
        self._populate_fields()
        self._refresh_lease_table()

        # Periodically refresh the lease table
        self._schedule_refresh()

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def _load_config(self) -> dict:
        try:
            with open(CONFIG_PATH, encoding='utf-8') as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return self._default_config()

    @staticmethod
    def _default_config() -> dict:
        return {
            'server_ip':   '192.168.1.1',
            'pool_start':  '192.168.1.100',
            'pool_end':    '192.168.1.200',
            'subnet_mask': '255.255.255.0',
            'router':      '192.168.1.1',
            'dns_servers': '8.8.8.8, 8.8.4.4',
            'lease_time':  86400,
            'bind_ip':     '',
        }

    def _save_config(self, cfg: dict) -> None:
        try:
            with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
                json.dump(cfg, f, indent=2, ensure_ascii=False)
        except OSError as exc:
            messagebox.showerror('Speicherfehler', str(exc))

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        self._apply_styles()

        # ── Top bar ──────────────────────────────────────────────────
        top = tk.Frame(self, bg=PANEL, pady=8)
        top.pack(fill='x', side='top')

        tk.Label(
            top, text='⚡ DHCP Server', font=('Segoe UI', 16, 'bold'),
            bg=PANEL, fg=ACCENT,
        ).pack(side='left', padx=16)

        self._status_var = tk.StringVar(value='● Gestoppt')
        self._status_lbl = tk.Label(
            top, textvariable=self._status_var, font=('Segoe UI', 11),
            bg=PANEL, fg=RED,
        )
        self._status_lbl.pack(side='left', padx=12)

        self._toggle_btn = tk.Button(
            top, text='Starten', command=self._toggle_server,
            font=('Segoe UI', 10, 'bold'),
            bg=GREEN, fg='#1e1e2e', relief='flat', padx=14, pady=4,
            cursor='hand2', activebackground='#89dcab',
        )
        self._toggle_btn.pack(side='right', padx=16)

        # ── Notebook ─────────────────────────────────────────────────
        nb = ttk.Notebook(self, style='TNotebook')
        nb.pack(fill='both', expand=True, padx=10, pady=(6, 10))

        self._build_config_tab(nb)
        self._build_leases_tab(nb)
        self._build_log_tab(nb)

    def _apply_styles(self) -> None:
        style = ttk.Style(self)
        style.theme_use('clam')
        style.configure('TNotebook',        background=BG,    borderwidth=0)
        style.configure('TNotebook.Tab',    background=PANEL, foreground=FG,
                        padding=[14, 6],   font=('Segoe UI', 10))
        style.map('TNotebook.Tab',
                  background=[('selected', BG)],
                  foreground=[('selected', ACCENT)])
        style.configure('Treeview',
                        background=ROW_EVEN, fieldbackground=ROW_EVEN,
                        foreground=FG, rowheight=24,
                        font=('Consolas', 9))
        style.configure('Treeview.Heading',
                        background=PANEL, foreground=ACCENT,
                        font=('Segoe UI', 9, 'bold'), relief='flat')
        style.map('Treeview', background=[('selected', ACCENT_DARK)])

    # ── Config tab ────────────────────────────────────────────────────

    def _build_config_tab(self, nb: ttk.Notebook) -> None:
        frame = tk.Frame(nb, bg=BG)
        nb.add(frame, text='  ⚙ Konfiguration  ')

        canvas = tk.Canvas(frame, bg=BG, highlightthickness=0)
        canvas.pack(fill='both', expand=True)

        inner = tk.Frame(canvas, bg=BG)
        canvas.create_window((0, 0), window=inner, anchor='nw')

        fields = [
            ('Server-IP',       'server_ip',   'IP-Adresse dieses Servers'),
            ('Pool Start',      'pool_start',  'Erste vergebbare IP-Adresse'),
            ('Pool Ende',       'pool_end',    'Letzte vergebbare IP-Adresse'),
            ('Subnetzmaske',    'subnet_mask', 'z. B. 255.255.255.0'),
            ('Standard-Gateway','router',      'IP des Routers'),
            ('DNS-Server',      'dns_servers', 'Kommagetrennte DNS-IPs'),
            ('Leasedauer (s)',  'lease_time',  'Sekunden (86400 = 1 Tag)'),
            ('Bind-IP',         'bind_ip',     'Leer = alle Interfaces'),
        ]

        self._entry_vars: dict[str, tk.StringVar] = {}

        for row, (label, key, hint) in enumerate(fields):
            tk.Label(
                inner, text=label, font=('Segoe UI', 10),
                bg=BG, fg=FG, anchor='w', width=20,
            ).grid(row=row, column=0, padx=(20, 8), pady=6, sticky='w')

            var = tk.StringVar()
            self._entry_vars[key] = var

            entry = tk.Entry(
                inner, textvariable=var, font=('Consolas', 10),
                bg=PANEL, fg=FG, insertbackground=FG,
                relief='flat', bd=0,
                highlightthickness=1, highlightbackground='#45475a',
                highlightcolor=ACCENT, width=30,
            )
            entry.grid(row=row, column=1, padx=4, pady=6, sticky='ew')

            tk.Label(
                inner, text=hint, font=('Segoe UI', 9),
                bg=BG, fg='#6c7086', anchor='w',
            ).grid(row=row, column=2, padx=(8, 20), pady=6, sticky='w')

        inner.columnconfigure(1, weight=1)

        btn_frame = tk.Frame(inner, bg=BG)
        btn_frame.grid(row=len(fields), column=0, columnspan=3,
                       padx=20, pady=16, sticky='w')

        self._make_button(btn_frame, 'Speichern', self._on_save, ACCENT_DARK).pack(side='left', padx=(0, 8))
        self._make_button(btn_frame, 'Zurücksetzen', self._on_reset, '#45475a').pack(side='left')

    # ── Leases tab ────────────────────────────────────────────────────

    def _build_leases_tab(self, nb: ttk.Notebook) -> None:
        frame = tk.Frame(nb, bg=BG)
        nb.add(frame, text='  📋 Leases  ')

        cols = ('IP-Adresse', 'MAC-Adresse', 'Hostname', 'Vergeben um', 'Verbleibend')
        self._tree = ttk.Treeview(frame, columns=cols, show='headings',
                                  selectmode='browse')
        widths = (130, 150, 180, 150, 100)
        for col, w in zip(cols, widths):
            self._tree.heading(col, text=col)
            self._tree.column(col, width=w, minwidth=60, anchor='w')

        self._tree.tag_configure('odd',  background=ROW_ODD)
        self._tree.tag_configure('even', background=ROW_EVEN)

        vsb = ttk.Scrollbar(frame, orient='vertical',   command=self._tree.yview)
        hsb = ttk.Scrollbar(frame, orient='horizontal', command=self._tree.xview)
        self._tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        self._tree.grid(row=0, column=0, sticky='nsew')
        vsb.grid(row=0, column=1, sticky='ns')
        hsb.grid(row=1, column=0, sticky='ew')
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

        btn_bar = tk.Frame(frame, bg=BG, pady=6)
        btn_bar.grid(row=2, column=0, columnspan=2, sticky='ew', padx=10)

        self._make_button(btn_bar, '🔄 Aktualisieren', self._refresh_lease_table, ACCENT_DARK).pack(side='left', padx=(0, 8))
        self._make_button(btn_bar, '🗑 Ausgewählte freigeben', self._on_release_selected, RED).pack(side='left')

        self._lease_count_var = tk.StringVar(value='0 Leases')
        tk.Label(btn_bar, textvariable=self._lease_count_var,
                 bg=BG, fg='#6c7086', font=('Segoe UI', 9)).pack(side='right', padx=8)

    # ── Log tab ───────────────────────────────────────────────────────

    def _build_log_tab(self, nb: ttk.Notebook) -> None:
        frame = tk.Frame(nb, bg=BG)
        nb.add(frame, text='  📜 Protokoll  ')

        self._log_text = tk.Text(
            frame, state='disabled', font=('Consolas', 9),
            bg=PANEL, fg=FG, relief='flat', bd=0,
            wrap='word', selectbackground=ACCENT_DARK,
        )
        vsb = ttk.Scrollbar(frame, orient='vertical', command=self._log_text.yview)
        self._log_text.configure(yscrollcommand=vsb.set)

        self._log_text.grid(row=0, column=0, sticky='nsew', padx=(10, 0), pady=10)
        vsb.grid(row=0, column=1, sticky='ns', pady=10, padx=(0, 6))
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

        btn_bar = tk.Frame(frame, bg=BG, pady=4)
        btn_bar.grid(row=1, column=0, columnspan=2, sticky='ew', padx=10)
        self._make_button(btn_bar, '🗑 Protokoll leeren', self._clear_log, '#45475a').pack(side='left')

        # Colour tags for the log
        self._log_text.tag_configure('info',  foreground=FG)
        self._log_text.tag_configure('offer', foreground=YELLOW)
        self._log_text.tag_configure('ack',   foreground=GREEN)
        self._log_text.tag_configure('nak',   foreground=RED)
        self._log_text.tag_configure('rel',   foreground='#cba6f7')

    # ------------------------------------------------------------------
    # UI helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _make_button(parent, text: str, cmd, bg: str) -> tk.Button:
        return tk.Button(
            parent, text=text, command=cmd,
            font=('Segoe UI', 9), bg=bg, fg='#1e1e2e' if bg != '#45475a' else FG,
            relief='flat', padx=10, pady=4, cursor='hand2',
            activebackground=bg,
        )

    def _populate_fields(self) -> None:
        for key, var in self._entry_vars.items():
            var.set(str(self._config.get(key, '')))

    # ------------------------------------------------------------------
    # Config actions
    # ------------------------------------------------------------------

    def _on_save(self) -> None:
        cfg = {}
        for key, var in self._entry_vars.items():
            cfg[key] = var.get().strip()

        # Validate IP fields
        for field in ('pool_start', 'pool_end', 'subnet_mask'):
            try:
                ipaddress.IPv4Address(cfg[field])
            except ValueError:
                messagebox.showerror('Ungültige Eingabe', f'Ungültige IP: {cfg[field]}')
                return

        try:
            cfg['lease_time'] = int(cfg['lease_time'])
            if cfg['lease_time'] < 60:
                raise ValueError
        except ValueError:
            messagebox.showerror('Ungültige Eingabe', 'Leasedauer muss ≥ 60 Sekunden sein.')
            return

        self._config = cfg
        self._save_config(cfg)

        if self._server:
            self._server.update_config(cfg)
            self._append_log('[System] Konfiguration übernommen.')

        messagebox.showinfo('Gespeichert', 'Konfiguration erfolgreich gespeichert.')

    def _on_reset(self) -> None:
        self._config = self._default_config()
        self._populate_fields()

    # ------------------------------------------------------------------
    # Server toggle
    # ------------------------------------------------------------------

    def _toggle_server(self) -> None:
        if self._server and self._server.is_running:
            self._server.stop()
            self._server = None
            self._status_var.set('● Gestoppt')
            self._status_lbl.config(fg=RED)
            self._toggle_btn.config(text='Starten', bg=GREEN)
        else:
            try:
                self._server = DHCPServer(
                    config            = self._config,
                    on_log            = self._append_log_threadsafe,
                    on_leases_changed = self._refresh_lease_table_threadsafe,
                )
                self._server.start()
                self._status_var.set('● Läuft')
                self._status_lbl.config(fg=GREEN)
                self._toggle_btn.config(text='Stoppen', bg=RED)
            except Exception as exc:
                messagebox.showerror('Startfehler', str(exc))

    # ------------------------------------------------------------------
    # Lease table
    # ------------------------------------------------------------------

    def _refresh_lease_table(self) -> None:
        if not self._server:
            return
        leases = self._server.lease_manager.get_all_leases()
        self._tree.delete(*self._tree.get_children())
        for i, lease in enumerate(leases):
            tag = 'odd' if i % 2 else 'even'
            at = time.strftime('%d.%m.%Y %H:%M:%S',
                               time.localtime(lease.assigned_at))
            self._tree.insert('', 'end', iid=lease.mac, tags=(tag,), values=(
                lease.ip, lease.mac, lease.hostname or '–', at, lease.ttl_str,
            ))
        self._lease_count_var.set(f'{len(leases)} Lease{"s" if len(leases) != 1 else ""}')

    def _refresh_lease_table_threadsafe(self) -> None:
        self.after(0, self._refresh_lease_table)

    def _on_release_selected(self) -> None:
        sel = self._tree.selection()
        if not sel or not self._server:
            return
        for mac in sel:
            self._server.lease_manager.release_ip(mac)
            self._append_log(f'[System] Lease freigegeben: {mac}')
        self._refresh_lease_table()

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------

    def _append_log(self, msg: str) -> None:
        tag = self._auto_tag(msg)
        self._log_text.configure(state='normal')
        self._log_text.insert('end', msg + '\n', tag)
        self._log_text.see('end')
        self._log_text.configure(state='disabled')

    def _append_log_threadsafe(self, msg: str) -> None:
        self.after(0, lambda: self._append_log(msg))

    @staticmethod
    def _auto_tag(msg: str) -> str:
        upper = msg.upper()
        if 'OFFER' in upper:
            return 'offer'
        if 'ACK' in upper:
            return 'ack'
        if 'NAK' in upper or 'FEHLER' in upper:
            return 'nak'
        if 'RELEASE' in upper:
            return 'rel'
        return 'info'

    def _clear_log(self) -> None:
        self._log_text.configure(state='normal')
        self._log_text.delete('1.0', 'end')
        self._log_text.configure(state='disabled')

    # ------------------------------------------------------------------
    # Periodic refresh
    # ------------------------------------------------------------------

    def _schedule_refresh(self) -> None:
        self._refresh_lease_table()
        self.after(5000, self._schedule_refresh)
