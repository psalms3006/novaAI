; NOVA.iss — Inno Setup 6 script for the NOVA Desktop installer.
;
; Prerequisite: build the PyInstaller bundle first (see BUILD.md).
; Output: packaging/out/NOVASetup.exe

#define MyAppName "NOVA"
#define MyAppVersion "1.0.0"
#define MyAppPublisher "Omniel"
#define MyAppExeName "NOVA.exe"

[Setup]
AppId={{8E1B6C7A-52D4-4B7E-9A31-A1B2C3D4E5F6}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
UninstallDisplayIcon={app}\{#MyAppExeName}
OutputDir=out
OutputBaseFilename=NOVASetup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
ArchitecturesInstallIn64BitMode=x64compatible

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"
Name: "autostart"; Description: "Start NOVA when Windows starts"; Flags: unchecked

[Files]
Source: "..\dist\NOVADesktop\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon
Name: "{userstartup}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: autostart

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}"
; user data in %APPDATA%\NOVA is intentionally preserved across uninstall/reinstall
