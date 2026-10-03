; Inno Setup script for the Windows installer (docs/DOCUMENTATION.md, 6.3). CI compiles it after the PyInstaller
; build:
;   ISCC.exe /DAppVersion=1.2.0 /DSourceDir=dist\StemSplitter /DOutputDir=release
;            /DOutputName=StemSplitter-Windows-x64-Setup installer\StemSplitter.iss
;
; Per-user install into %LOCALAPPDATA%\Programs\StemSplitter: no administrator rights, and a folder the app can
; write to, which its self-update needs (stemsplitter/updater.py). The models, settings and log stay in
; %LOCALAPPDATA%\StemSplitter (app_data_dir()); uninstalling asks whether to remove them too.

#ifndef AppVersion
  #error Pass the version: /DAppVersion=x.y.z
#endif
#ifndef SourceDir
  #define SourceDir "..\dist\StemSplitter"
#endif
#ifndef OutputDir
  #define OutputDir "..\release"
#endif
#ifndef OutputName
  #define OutputName "StemSplitter-Setup"
#endif

#define AppName "StemSplitter"
#define AppExe "StemSplitter.exe"

[Setup]
; never change the AppId: Windows identifies the installed app (upgrade, uninstall) by it
AppId={{CDC6E5C7-65E0-4703-9939-D1D647383BFC}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppName}
AppPublisherURL=https://github.com/Pablokaer/StemSplitter
AppSupportURL=https://github.com/Pablokaer/StemSplitter/issues
AppUpdatesURL=https://github.com/Pablokaer/StemSplitter/releases
VersionInfoVersion={#AppVersion}
PrivilegesRequired=lowest
DefaultDirName={autopf}\{#AppName}
DisableDirPage=auto
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir={#OutputDir}
OutputBaseFilename={#OutputName}
SetupIconFile=..\assets\icon.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes
LZMAUseSeparateProcess=yes
LZMANumBlockThreads=4
CloseApplications=yes
RestartApplications=no
ShowLanguageDialog=auto

[Languages]
Name: "en"; MessagesFile: "compiler:Default.isl"
Name: "ptbr"; MessagesFile: "compiler:Languages\BrazilianPortuguese.isl"
Name: "es"; MessagesFile: "compiler:Languages\Spanish.isl"

[CustomMessages]
en.RemoveUserData=Also remove the downloaded AI models, your settings and the log?%n%n%1%n%nYour split songs are not affected.
ptbr.RemoveUserData=Remover também os modelos de IA baixados, suas configurações e o log?%n%n%1%n%nAs músicas que você separou não são afetadas.
es.RemoveUserData=¿Eliminar también los modelos de IA descargados, tu configuración y el registro?%n%n%1%n%nLas canciones que separaste no se ven afectadas.

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[InstallDelete]
; a reinstall or an upgrade starts from a clean build folder, so no file of an older version is left behind
Type: filesandordirs; Name: "{app}\_internal"
Type: filesandordirs; Name: "{app}\.ss-update"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; the self-update adds and replaces files the installer doesn't know about, all of them inside _internal
Type: filesandordirs; Name: "{app}\_internal"
Type: filesandordirs; Name: "{app}\.ss-update"
Type: dirifempty; Name: "{app}"

[Code]
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Data: String;
begin
  if CurUninstallStep <> usPostUninstall then
    Exit;
  Data := ExpandConstant('{localappdata}\{#AppName}');
  { a silent uninstall (an upgrade script, CI) keeps the data }
  if UninstallSilent or not DirExists(Data) then
    Exit;
  if SuppressibleMsgBox(FmtMessage(CustomMessage('RemoveUserData'), [Data]), mbConfirmation,
                        MB_YESNO or MB_DEFBUTTON2, IDNO) = IDYES then
  begin
    DelTree(Data, True, True, True);
    RegDeleteKeyIncludingSubkeys(HKCU, 'Software\{#AppName}');
  end;
end;
