;
; AnyDoc desktop installer (Inno Setup 6.3 or newer)
;
; Build:
;     "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" desktop\installer.iss
;
; PyInstaller must have run first; dist\AnyDoc\ has to exist.
;
; ---------------------------------------------------------------------
; How dependencies are handled
;
; LibreOffice is about 350MB, pandoc 30MB, tesseract 60MB. Bundling all of them
; makes a 450MB installer that has to be redistributed for every one-line code
; change. Instead they are downloaded at install time: the installer itself is
; about 50MB and the dependencies come from their official sources, which also
; sidesteps redistributing GPL (pandoc) and MPL (LibreOffice) binaries.
;
; The price is that installation needs network access. For offline environments
; use the "offline bundle" flow: download the four installers into vendor\ by
; hand and build with ISCC /DOFFLINE.
; ---------------------------------------------------------------------

#define AppName "AnyDoc Document Converter"
#define AppVersion "1.0.0"
#define AppPublisher "Tony Huang"
#define AppExeName "AnyDoc.exe"

; Dependency versions. These URLs returned HTTP 200 when checked in 2026-08;
; re-check them when bumping, a dead link fails the install at the download step.
#define LibreOfficeVer "26.2.5"
#define PandocVer "3.10.2"
#define TesseractSetup "tesseract-ocr-w64-setup-v5.3.0.20221214.exe"
#define PopplerVer "26.02.0-0"

[Setup]
AppId={{8F3A2C91-4D7E-4B15-9A63-ANYDOC000001}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\AnyDoc
DefaultGroupName=AnyDoc
OutputBaseFilename=AnyDoc-Setup-{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; Dependencies go into Program Files, which needs admin rights
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
DisableProgramGroupPage=yes
LicenseFile=..\LICENSE

[Languages]
Name: "en"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"
Name: "contextmenu"; Description: "Add ""Convert with AnyDoc"" to the file context menu"; \
    GroupDescription: "Integration:"

[Files]
Source: "..\dist\AnyDoc\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Registry]
; Context menu: select files -> Convert with AnyDoc. The exe adds argv files to the list.
Root: HKCR; Subkey: "*\shell\AnyDocConvert"; ValueType: string; ValueName: ""; \
    ValueData: "Convert with AnyDoc"; Tasks: contextmenu; Flags: uninsdeletekey
Root: HKCR; Subkey: "*\shell\AnyDocConvert"; ValueType: string; ValueName: "Icon"; \
    ValueData: "{app}\{#AppExeName}"; Tasks: contextmenu
Root: HKCR; Subkey: "*\shell\AnyDocConvert\command"; ValueType: string; ValueName: ""; \
    ValueData: """{app}\{#AppExeName}"" ""%1"""; Tasks: contextmenu

[Run]
Filename: "{app}\{#AppExeName}"; Description: "Launch {#AppName} now"; \
    Flags: nowait postinstall skipifsilent

[Code]
var
  DownloadPage: TDownloadWizardPage;

{ -- Dependency detection ---------------------------------------------
  Kept in sync with the search locations in discovery.py. Anything already
  installed is skipped: LibreOffice is often present already, and reinstalling
  it is slow and may overwrite the user's settings. }

function LibreOfficeInstalled(): Boolean;
begin
  Result := FileExists(ExpandConstant('{commonpf}\LibreOffice\program\soffice.exe')) or
            FileExists(ExpandConstant('{commonpf32}\LibreOffice\program\soffice.exe'));
end;

function PandocInstalled(): Boolean;
begin
  Result := FileExists(ExpandConstant('{commonpf}\Pandoc\pandoc.exe')) or
            FileExists(ExpandConstant('{localappdata}\Pandoc\pandoc.exe'));
end;

function TesseractInstalled(): Boolean;
begin
  Result := FileExists(ExpandConstant('{commonpf}\Tesseract-OCR\tesseract.exe')) or
            FileExists(ExpandConstant('{commonpf32}\Tesseract-OCR\tesseract.exe'));
end;

function PopplerInstalled(): Boolean;
begin
  { poppler has no installer; we unzip it into {app}\engines\ where discovery.py looks }
  Result := FileExists(ExpandConstant('{app}\engines\poppler\Library\bin\pdftoppm.exe'));
end;

function OnDownloadProgress(const Url, FileName: String; const Progress, ProgressMax: Int64): Boolean;
begin
  if ProgressMax <> 0 then
    Log(Format('Downloading %s: %d / %d', [FileName, Progress, ProgressMax]));
  Result := True;
end;

procedure InitializeWizard;
begin
  DownloadPage := CreateDownloadPage(
    'Downloading conversion components',
    'AnyDoc needs the following components to produce Word and PDF files',
    @OnDownloadProgress);
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if CurPageID <> wpReady then
    Exit;

  DownloadPage.Clear;

  { LibreOffice: the only provider of docx/xlsx/pptx <-> pdf; without it 85 routes vanish }
  if not LibreOfficeInstalled() then
    DownloadPage.Add(
      'https://download.documentfoundation.org/libreoffice/stable/{#LibreOfficeVer}/win/x86_64/LibreOffice_{#LibreOfficeVer}_Win_x86-64.msi',
      'LibreOffice.msi', '');

  { pandoc: Markdown / HTML <-> Word / presentations }
  if not PandocInstalled() then
    DownloadPage.Add(
      'https://github.com/jgm/pandoc/releases/download/{#PandocVer}/pandoc-{#PandocVer}-windows-x86_64.msi',
      'pandoc.msi', '');

  { tesseract: OCR for scans and images. The UB Mannheim installer offers CJK language packs }
  if not TesseractInstalled() then
    DownloadPage.Add(
      'https://digi.bib.uni-mannheim.de/tesseract/{#TesseractSetup}',
      'tesseract.exe', '');

  { poppler: scanned PDFs must be rasterized before OCR }
  if not PopplerInstalled() then
    DownloadPage.Add(
      'https://github.com/oschwartz10612/poppler-windows/releases/download/v{#PopplerVer}/Release-{#PopplerVer}.zip',
      'poppler.zip', '');

  DownloadPage.Show;
  try
    try
      DownloadPage.Download;
    except
      { A failed download must not fail the whole install: AnyDoc itself still
        works with a reduced capability matrix, and tells the user at startup
        exactly what is missing. }
      SuppressibleMsgBox(
        'Some components failed to download:' + #13#10 + GetExceptionMessage + #13#10#13#10 +
        'AnyDoc will still be installed, but features such as producing Word / PDF ' +
        'files will be unavailable.' + #13#10 +
        'You can install LibreOffice and Pandoc manually later; ' +
        'the app detects them automatically.',
        mbInformation, MB_OK, IDOK);
      Result := True;
    end;
  finally
    DownloadPage.Hide;
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ResultCode: Integer;
  TempDir: String;
begin
  if CurStep <> ssPostInstall then
    Exit;

  TempDir := ExpandConstant('{tmp}');

  { MSIs install silently via msiexec /qn. LibreOffice's /qn installs all of
    writer/calc/impress, and all three are needed: drop one and the routes for
    its formats disappear. }
  if FileExists(TempDir + '\LibreOffice.msi') then
  begin
    WizardForm.StatusLabel.Caption := 'Installing LibreOffice (about 2-5 minutes)...';
    Exec('msiexec.exe', '/i "' + TempDir + '\LibreOffice.msi" /qn /norestart',
         '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  end;

  if FileExists(TempDir + '\pandoc.msi') then
  begin
    WizardForm.StatusLabel.Caption := 'Installing Pandoc...';
    Exec('msiexec.exe', '/i "' + TempDir + '\pandoc.msi" /qn /norestart',
         '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  end;

  { The tesseract installer is NSIS: /S installs silently. The eng language
    pack is included by default; extra packs are separate components. }
  if FileExists(TempDir + '\tesseract.exe') then
  begin
    WizardForm.StatusLabel.Caption := 'Installing Tesseract OCR...';
    Exec(TempDir + '\tesseract.exe', '/S', '', SW_HIDE,
         ewWaitUntilTerminated, ResultCode);
  end;

  { poppler is a plain zip, extracted into {app}\engines\poppler.
    _BUNDLED_SUBDIRS in discovery.py scans engines/ next to the exe, so PATH
    does not need changing. }
  if FileExists(TempDir + '\poppler.zip') then
  begin
    WizardForm.StatusLabel.Caption := 'Extracting Poppler...';
    ForceDirectories(ExpandConstant('{app}\engines\poppler'));
    Exec('powershell.exe',
         '-NoProfile -ExecutionPolicy Bypass -Command "Expand-Archive -Path ''' +
         TempDir + '\poppler.zip'' -DestinationPath ''' +
         ExpandConstant('{app}\engines\poppler') + ''' -Force"',
         '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  end;
end;
