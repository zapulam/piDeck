#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif
#ifndef BundleDir
  #define BundleDir "..\dist\piDeck"
#endif
#ifndef RedistDir
  #define RedistDir "..\build\redist"
#endif
#ifndef OutputPath
  #define OutputPath "..\dist\installers"
#endif
#ifndef BuildLabel
  #define BuildLabel "unsigned"
#endif

[Setup]
AppId={{58A52161-38C3-4764-9AE9-4E608C9DD953}
AppName=piDeck
AppVersion={#AppVersion}
AppPublisher=Zachary
DefaultDirName={localappdata}\Programs\piDeck
DefaultGroupName=piDeck
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#OutputPath}
OutputBaseFilename=piDeck-{#AppVersion}-windows-x64-{#BuildLabel}-setup
SetupIconFile=..\pideck\assets\pideck.ico
UninstallDisplayIcon={app}\piDeck.exe
LicenseFile=..\LICENSE
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
DisableProgramGroupPage=yes

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked
Name: "startup"; Description: "Start piDeck when I sign in"; Flags: unchecked

[Files]
Source: "{#BundleDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#RedistDir}\MicrosoftEdgeWebview2Setup.exe"; DestDir: "{tmp}"; Flags: deleteafterinstall; Check: NeedsWebView

[Icons]
Name: "{userprograms}\piDeck"; Filename: "{app}\piDeck.exe"; WorkingDir: "{app}"; AppUserModelID: "piDeck.Desktop"
Name: "{userdesktop}\piDeck"; Filename: "{app}\piDeck.exe"; WorkingDir: "{app}"; AppUserModelID: "piDeck.Desktop"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "piDeck"; ValueData: """{app}\piDeck.exe"""; Tasks: startup; Flags: uninsdeletevalue

[Run]
Filename: "{tmp}\MicrosoftEdgeWebview2Setup.exe"; Parameters: "/silent /install"; StatusMsg: "Installing Microsoft WebView2..."; Check: NeedsWebView; Flags: waituntilterminated
Filename: "{app}\piDeck.exe"; Description: "Open piDeck"; Flags: nowait postinstall skipifsilent

[Code]
function NeedsWebView: Boolean;
var
  Version: String;
begin
  Result := not (RegQueryStringValue(HKCU, 'Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', Version) or
    RegQueryStringValue(HKLM32, 'Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', Version) or
    RegQueryStringValue(HKLM64, 'Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', Version));
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
    RegDeleteValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Run', 'piDeck');
end;
