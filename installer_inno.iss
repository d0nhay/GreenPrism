; Скрипт установщика Inno Setup 6 для GreenPrism Launcher (собирается из --onedir без тега overlay)
; 1. Сначала запусти 2_BUILD_EXE.bat (создаст dist\GreenPrism\)
; 2. Открой этот файл в Inno Setup Compiler и нажми Build -> Compile

#define MyAppName "GreenPrism Launcher"
#define MyAppVersion "1.0"
#define MyAppPublisher "GreenPrism Open Source"
#define MyAppExeName "GreenPrism.exe"

[Setup]
AppId={{8D4F6C2B-9A14-4F91-9E55-2F7C8A61B810}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\GreenPrismLauncher
DefaultGroupName={#MyAppName}
AllowNoIcons=yes
PrivilegesRequired=lowest
OutputDir=dist_installer
OutputBaseFilename=GreenPrism_Setup_v1.0
SetupIconFile=icon.ico
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"
Name: "ukrainian"; MessagesFile: "compiler:Languages\Ukrainian.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "dist\GreenPrism\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent
