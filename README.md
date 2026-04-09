# DHCP Server für Windows

Ein vollständiger DHCP-Server mit grafischer Oberfläche (GUI) für Windows.  
Konfigurierbar, keine externen Abhängigkeiten zur Laufzeit.

---

## Features

| Funktion | Beschreibung |
|---|---|
| DHCP-Protokoll | DISCOVER / OFFER / REQUEST / ACK / NAK / RELEASE / INFORM |
| GUI | Modernes dunkles Design mit tkinter |
| IP-Pool | Frei konfigurierbarer Adressbereich |
| Lease-Verwaltung | Tabelle aller aktiven Leases, manuelle Freigabe |
| Konfiguration | Subnetzmaske, Gateway, DNS, Leasedauer – alles in der GUI einstellbar |
| Protokoll-Tab | Farbiges Echtzeit-Protokoll aller DHCP-Ereignisse |
| Installer | Professioneller Windows-Installer (Inno Setup) |

---

## Voraussetzungen

| Werkzeug | Version | Zweck |
|---|---|---|
| Python | ≥ 3.11 | Laufzeit / Build |
| PyInstaller | ≥ 6.0 | Erzeugt die `.exe` |
| Inno Setup | ≥ 6.0 | Erzeugt den Windows-Installer |

> Python: https://python.org  
> Inno Setup: https://jrsoftware.org/isdl.php

---

## Schnellstart (Quellcode)

```bat
# Administrator-Eingabeaufforderung öffnen
pip install -r requirements.txt
python main.py
```

> **Hinweis:** Der DHCP-Server benötigt **Administrator-Rechte**, da er auf UDP-Port 67 lauscht.  
> Das Programm fordert die Rechte beim Start automatisch an (UAC-Abfrage).

---

## .exe bauen

```bat
build.bat
```

Das Skript führt automatisch aus:
1. `pip install -r requirements.txt`
2. `pyinstaller --clean --noconfirm build.spec` → `dist\DHCPServer.exe`
3. `ISCC.exe installer.iss` → `installer_output\DHCPServer_Setup_1.0.0.exe`

---

## Windows-Installer

Nach dem Build findet sich der Installer unter:

```
installer_output\DHCPServer_Setup_1.0.0.exe
```

Der Installer:
- Installiert die Anwendung nach `%ProgramFiles%\DHCP Server`
- Legt Einträge im Startmenü an
- Erstellt optional eine Desktop-Verknüpfung
- Enthält einen Deinstaller

---

## Konfiguration

Alle Einstellungen sind in der GUI unter dem Tab **⚙ Konfiguration** änderbar und werden in `config.json` gespeichert.

| Feld | Beschreibung | Beispiel |
|---|---|---|
| Server-IP | IP-Adresse dieses Servers | `192.168.1.1` |
| Pool Start | Erste vergebbare IP | `192.168.1.100` |
| Pool Ende | Letzte vergebbare IP | `192.168.1.200` |
| Subnetzmaske | Netzmaske | `255.255.255.0` |
| Standard-Gateway | Router-IP | `192.168.1.1` |
| DNS-Server | Kommagetrennte DNS-IPs | `8.8.8.8, 8.8.4.4` |
| Leasedauer (s) | Sekunden (86400 = 1 Tag) | `86400` |
| Bind-IP | Interface-IP (leer = alle) | *(leer)* |

---

## Windows-Firewall

Damit Clients den Server erreichen können, muss UDP-Port 67 in der Windows-Firewall freigegeben sein:

```powershell
# Als Administrator ausführen
netsh advfirewall firewall add rule `
  name="DHCP Server" protocol=UDP dir=in localport=67 action=allow
```

---

## Projektstruktur

```
DHCP/
├── main.py              # Einstiegspunkt (UAC-Erhöhung + GUI-Start)
├── config.json          # Standardkonfiguration
├── requirements.txt     # PyInstaller
├── build.spec           # PyInstaller-Spec (→ DHCPServer.exe)
├── installer.iss        # Inno Setup-Skript (→ Installer .exe)
├── build.bat            # Ein-Klick-Build-Skript
└── src/
    ├── __init__.py
    ├── dhcp_packet.py   # RFC-2131-Paketformat (Parse + Build)
    ├── lease_manager.py # IP-Pool und Lease-Verwaltung
    ├── dhcp_server.py   # DHCP-Server-Logik
    └── gui.py           # tkinter-GUI
```
