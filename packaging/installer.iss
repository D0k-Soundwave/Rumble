; Inno Setup script for RUMBLE — per-user, no administrator required.
;
; Build order:
;   1. pyinstaller packaging/rumble.spec      -> dist\RUMBLE\RUMBLE.exe (+ libs)
;   2. ISCC packaging/installer.iss          -> dist\RUMBLE-Setup.exe
;
; Equalizer APO is NOT bundled or installed here: the final wizard page offers an
; optional link to its download page so the user installs it themselves (that is
; the only step that needs elevation, and it stays entirely their choice).

#define AppName "RUMBLE"
#define AppVersion "1.0.0"
#define AppPublisher "SoundWave.Team"
#define AppExeName "RUMBLE.exe"
#define APOUrl "https://sourceforge.net/projects/equalizerapo/"

[Setup]
; A stable, unique AppId — keep this GUID for upgrades to replace cleanly.
AppId={{5F2C9E1A-7B3D-4C6E-9A2F-1B2C3D4E5F60}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
; Per-user install: no admin prompt, lands under the user's local profile.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
DefaultDirName={localappdata}\Programs\RUMBLE
DisableProgramGroupPage=yes
AllowNoIcons=yes
OutputDir={#SourcePath}\..\dist
OutputBaseFilename=RUMBLE-Setup
SetupIconFile={#SourcePath}\..\rumble\rumble.ico
; End-user disclaimer / terms of use, shown as the license page: the user
; must pick "I accept the agreement" to continue (Cancel exits), which is
; exactly the acceptance mechanism the document's own §6 describes.
LicenseFile={#SourcePath}\INSTALLER_DISCLAIMER.txt
UninstallDisplayIcon={app}\{#AppExeName}
WizardStyle=modern
Compression=lzma2
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Files]
; The entire PyInstaller one-folder output. App data is created next to the exe
; at first run, so nothing data-related is shipped here. Excludes are a
; belt-and-suspenders guard: even if a polluted dist\ were built, no
; runtime-generated state (the data\ tree) or logs can ever reach the package.
Source: "{#SourcePath}\..\dist\RUMBLE\*"; DestDir: "{app}"; Excludes: "data\*,*.log"; Flags: recursesubdirs createallsubdirs ignoreversion
; Legal texts, installed for later re-reading: the accepted disclaimer/terms,
; and the third-party license notices (LGPL components like PySide6 expect
; their notices to travel with the distributed binary).
Source: "{#SourcePath}\INSTALLER_DISCLAIMER.txt"; DestDir: "{app}"; DestName: "DISCLAIMER.txt"; Flags: ignoreversion
Source: "{#SourcePath}\THIRD-PARTY-NOTICES.txt"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent
; Optional, unchecked: open the Equalizer APO download page for the user to install themselves.
Filename: "{#APOUrl}"; Description: "Get Equalizer APO (required for audio processing)"; Flags: postinstall shellexec skipifsilent unchecked

[UninstallDelete]
; Remove the app data created next to the exe at runtime.
Type: filesandordirs; Name: "{app}\data"

[Registry]
; The app writes its own HKCU autostart value ("RUMBLE") when enabled; ensure a
; clean uninstall removes it. ValueType none = never created by the installer.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: none; ValueName: "RUMBLE"; Flags: uninsdeletevalue

[Code]
// On uninstall, restore the Equalizer APO config to its pre-RUMBLE state and
// remove RUMBLE's footprint. RUMBLE swaps config.txt for one it owns (saving the
// original as config.txt.rumble_backup), so a clean uninstall must put the
// original back — never just delete. APO makes its config folder user-writable,
// so this needs no admin; failures (e.g. APO already removed) are ignored. We
// only ever touch RUMBLE's own files, never APO's or the config folder itself.
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  ConfigDir, ConfigTxt, BackupTxt: string;
  FindRec: TFindRec;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    if RegQueryStringValue(HKLM, 'SOFTWARE\EqualizerAPO', 'ConfigPath', ConfigDir) then
    begin
      ConfigDir := AddBackslash(ConfigDir);
      ConfigTxt := ConfigDir + 'config.txt';
      BackupTxt := ConfigDir + 'config.txt.rumble_backup';
      // Restore the user's original config.txt that RUMBLE swapped out.
      if FileExists(BackupTxt) then
      begin
        if FileCopy(BackupTxt, ConfigTxt, False) then
          DeleteFile(BackupTxt);
      end;
      // Remove RUMBLE's pointer files and managed profile tree.
      DeleteFile(ConfigDir + 'rumble_active.txt');
      DeleteFile(ConfigDir + 'rumble_preview.txt');
      // Remove any timestamped drift snapshots RUMBLE saved when another tool
      // overwrote or co-wrote config.txt (config.txt.rumble_drift-<timestamp>).
      if FindFirst(ConfigDir + 'config.txt.rumble_drift-*', FindRec) then
      begin
        try
          repeat
            DeleteFile(ConfigDir + FindRec.Name);
          until not FindNext(FindRec);
        finally
          FindClose(FindRec);
        end;
      end;
      DelTree(ConfigDir + 'rumble', True, True, True);
    end;
  end;
end;
