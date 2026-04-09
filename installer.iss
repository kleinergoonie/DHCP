; Inno Setup script – creates a Windows installer for DHCP Server.
; Requires Inno Setup 6 (https://jrsoftware.org/isinfo.php).
; Run build.bat first to produce dist\DHCPServer.exe, then compile this script.

#define MyAppName      "DHCP Server"
#define MyAppVersion   "1.0.0"
#define MyAppPublisher "kleinergoonie"
#define MyAppExeName   "DHCPServer.exe"
#define MyAppURL       "https://github.com/kleinergoonie/DHCP"

[Setup]
AppId={{A3F7C2B1-4E9D-4F2A-8C3E-1B5D6A7E8F90}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
AllowNoIcons=yes
LicenseFile=
OutputDir=installer_output
OutputBaseFilename=DHCPServer_Setup_{#MyAppVersion}
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
; Request admin privileges so the server can bind port 67
PrivilegesRequired=admin
PrivilegesRequiredOverridesAllowed=dialog
SetupIconFile=
UninstallDisplayName={#MyAppName}
UninstallDisplayIcon={app}\{#MyAppExeName}
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "german";  MessagesFile: "compiler:Languages\German.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; Main executable (produced by PyInstaller)
Source: "dist\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion

; Default configuration (written to %APPDATA% on first run – see [Run])
Source: "config.json"; DestDir: "{app}"; Flags: ignoreversion onlyifdoesntexist

[Icons]
Name: "{group}\{#MyAppName}";        Filename: "{app}\{#MyAppExeName}"
Name: "{group}\{#MyAppName} deinstallieren"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}";  Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent runascurrentuser

[UninstallDelete]
Type: filesandordirs; Name: "{app}"

[Code]
// Show a note about Windows Firewall if the firewall is active.
procedure CurStepChanged(CurStep: TSetupStep);
var
  Msg: String;
begin
  if CurStep = ssPostInstall then
  begin
    Msg := 'Wichtig: Der DHCP-Server benötigt Administrator-Rechte und lauscht auf UDP-Port 67.' + #13#10 +
           'Bitte stellen Sie sicher, dass Ihre Windows-Firewall eingehenden UDP-Verkehr auf Port 67 erlaubt.';
    MsgBox(Msg, mbInformation, MB_OK);
  end;
end;
