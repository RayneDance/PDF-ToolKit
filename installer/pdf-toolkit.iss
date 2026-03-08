#define MyAppName "PDF Toolkit"
#define MyAppPublisher "JeffScripts"
#define MyAppURL "https://github.com/JeffScripts/pdf-toolkit"
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef SourceDir
  #error SourceDir not defined
#endif
#ifndef OutputDir
  #error OutputDir not defined
#endif
#ifndef SetupIconFile
  #error SetupIconFile not defined
#endif

[Setup]
AppId={{7D6F6F09-7775-49A2-9B6E-8C490D67C911}
AppName={#MyAppName}
AppVersion={#AppVersion}
AppVerName={#MyAppName} {#AppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
AppComments=Desktop PDF workflows for office teams and practical admin work.
DefaultDirName={localappdata}\Programs\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
WizardStyle=modern
Compression=lzma
SolidCompression=yes
OutputDir={#OutputDir}
OutputBaseFilename=pdf-toolkit-setup-windows-x64
SetupIconFile={#SetupIconFile}
UninstallDisplayIcon={app}\pdf-toolkit-gui.exe
VersionInfoVersion={#AppVersion}
VersionInfoCompany={#MyAppPublisher}
VersionInfoDescription={#MyAppName} Installer
ChangesAssociations=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\PDF Toolkit"; Filename: "{app}\pdf-toolkit-gui.exe"; WorkingDir: "{app}"
Name: "{autodesktop}\PDF Toolkit"; Filename: "{app}\pdf-toolkit-gui.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\pdf-toolkit-gui.exe"; Description: "Launch PDF Toolkit"; Flags: nowait postinstall skipifsilent unchecked
