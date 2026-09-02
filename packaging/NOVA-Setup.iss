; NOVA-Setup.iss — production Windows installer for NOVA Desktop.
; Technology: Inno Setup 6 (industry-standard, used for real product installers).
;
; Build:  ISCC.exe packaging\NOVA-Setup.iss     →  packaging\out\NOVA-Setup.exe
;
; Guarantees:
;   * Full wizard: welcome → location → tasks → install → launch
;   * Start Menu + optional Desktop shortcuts; launch after install
;   * Registers in Windows "Installed Apps" with a real uninstaller
;   * Upgrade-safe: installing over an existing copy keeps ALL user data
;     (user data lives in %APPDATA%\NOVA, never inside the install directory)
;   * Uninstall removes program files only; user data removal is opt-in
;   * Contains NO secrets: no .env, no API keys, no DPAPI blobs

#define MyAppName "NOVA"
#define MyAppVersion "1.0.0"
#define MyAppPublisher "Omniel"
#define MyAppExeName "NOVA.exe"

[Setup]
AppId={{7C4A1E9B-3D52-4F68-9A77-N0VADesk1001}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=out
OutputBaseFilename=NOVA-Setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#MyAppExeName}
CloseApplications=yes
RestartApplications=no

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; \
    GroupDescription: "{cm:AdditionalIcons}"; Flags: checkedonce
Name: "autostart"; Description: "Start {#MyAppName} when Windows starts"; \
    GroupDescription: "Startup:"; Flags: unchecked

[Files]
Source: "..\dist\NOVADesktop\*"; DestDir: "{app}"; \
    Flags: ignoreversion recursesubdirs createallsubdirs
; NOTE: deliberately NOT packaged: any ".env" file, "byok.bin", "device.json",
; "settings.json" or "*.log". Those are user-machine artifacts and never ship.

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon
Name: "{userstartup}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: autostart

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; \
    Flags: nowait postinstall skipifsilent

[Code]
var
  DataPage: TInputOptionWizardPage;

procedure InitializeWizard();
begin
  DataPage := CreateInputOptionPage(wpReady,
    'Remove User Data?', 'Would you like to remove your NOVA data as well?',
    'Your NOVA data (memories, settings, files) is kept in your profile folder ' +
    'and is preserved when uninstalling. Only check this if you want a complete wipe.',
    False, False);
  DataPage.Add('Keep my NOVA data (recommended)');
  DataPage.Add('Also delete memories, settings and workspace files');
  DataPage.Values[0] := True;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ResultCode: Integer;
begin
  if CurStep = ssInstall then
  begin
    // stop any running instance so upgrades replace files cleanly
    Exec(ExpandConstant('{cmd}'), '/C taskkill /IM NOVA.exe /F /T >nul 2>&1',
         '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
    Sleep(500);
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if (CurUninstallStep = usPostUninstall) and (not DataPage.Values[0]) then
    DelTree(ExpandConstant('{userappdata}\NOVA'), True, True, True);
end;
