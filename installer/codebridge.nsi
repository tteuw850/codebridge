; CodeBridge 2.0.12-prealpha: instalador Windows x64 por usuario
!include "MUI2.nsh"
!include "LogicLib.nsh"
!include "x64.nsh"
!ifndef SOURCE_ROOT
  !error "Defina /DSOURCE_ROOT"
!endif
!ifndef TOOLS_ROOT
  !error "Defina /DTOOLS_ROOT"
!endif
!ifndef OUTPUT_FILE
  !error "Defina /DOUTPUT_FILE"
!endif
Unicode true
Name "CodeBridge 2.0.12-prealpha"
OutFile "${OUTPUT_FILE}"
InstallDir "$LOCALAPPDATA\Programs\CodeBridge"
RequestExecutionLevel user
SetCompressor /SOLID lzma
ShowInstDetails show
ShowUninstDetails show

!define MUI_ABORTWARNING
!define MUI_ICON "${SOURCE_ROOT}\assets\codebridge.ico"
!define MUI_UNICON "${SOURCE_ROOT}\assets\codebridge.ico"
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "PortugueseBR"

Function .onInit
  ${IfNot} ${RunningX64}
    MessageBox MB_ICONSTOP "Este instalador requer Windows x64."
    Abort
  ${EndIf}
FunctionEnd

Section "Instalar CodeBridge" SecMain
  IfFileExists "$INSTDIR\version.json" 0 +3
    MessageBox MB_ICONEXCLAMATION "CodeBridge ja instalado neste caminho. Para preservar dados e configuracoes, este Setup nao sobrescreve uma instalacao existente. Escolha outra pasta ou use o atualizador."
    Abort

  SetOutPath "$INSTDIR"
  File "${SOURCE_ROOT}\README.md"
  File "${SOURCE_ROOT}\requirements.txt"
  File "${SOURCE_ROOT}\version.json"
  File "${SOURCE_ROOT}\SOURCE_SNAPSHOT.md"
  File "${SOURCE_ROOT}\INSTALLAR_OUTRO_PC.md"
  SetOutPath "$INSTDIR\app_rewrite"
  File /r "${SOURCE_ROOT}\app_rewrite\*.*"
  SetOutPath "$INSTDIR\author_mcp"
  File /r "${SOURCE_ROOT}\author_mcp\*.*"
  SetOutPath "$INSTDIR\browser_extension"
  File /r "${SOURCE_ROOT}\browser_extension\*.*"
  SetOutPath "$INSTDIR\docs"
  File /r "${SOURCE_ROOT}\docs\*.*"
  SetOutPath "$INSTDIR\rag"
  File /r "${SOURCE_ROOT}\rag\*.*"
  SetOutPath "$INSTDIR\assets"
  File /r "${SOURCE_ROOT}\assets\*.*"
  SetOutPath "$INSTDIR\installer"
  File /r "${SOURCE_ROOT}\installer\*.py"
  File /r "${SOURCE_ROOT}\installer\*.ps1"
  SetOutPath "$INSTDIR\tools"
  File "${TOOLS_ROOT}\cloudflared.exe"
  File "${TOOLS_ROOT}\tunnel-client.exe"

  DetailPrint "Baixando Python e preparando dependencias; internet necessaria..."
  ExecWait '"$SYSDIR\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "$INSTDIR\installer\bootstrap.ps1" -InstallRoot "$INSTDIR"' $0
  ${If} $0 != 0
    MessageBox MB_ICONSTOP "Falha ao preparar as dependencias (codigo $0). Consulte $LOCALAPPDATA\CodeBridge-MCP-Bridge\installer.log."
    Abort
  ${EndIf}

  CreateDirectory "$SMPROGRAMS\CodeBridge"
  CreateShortCut "$SMPROGRAMS\CodeBridge\CodeBridge.lnk" "$INSTDIR\runtime\python\pythonw.exe" '"$INSTDIR\app_rewrite\main.py"' "$INSTDIR\assets\codebridge.ico"
  CreateShortCut "$SMPROGRAMS\CodeBridge\Configurar CodeBridge.lnk" "$INSTDIR\runtime\python\pythonw.exe" '"$INSTDIR\installer\company_setup.py"' "$INSTDIR\assets\codebridge.ico"
  CreateShortCut "$SMPROGRAMS\CodeBridge\Atualizar CodeBridge.lnk" "$INSTDIR\runtime\python\pythonw.exe" '"$INSTDIR\installer\updater.py"' "$INSTDIR\assets\codebridge.ico"
  CreateShortCut "$DESKTOP\CodeBridge.lnk" "$INSTDIR\runtime\python\pythonw.exe" '"$INSTDIR\app_rewrite\main.py"' "$INSTDIR\assets\codebridge.ico"

  WriteUninstaller "$INSTDIR\Uninstall.exe"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\CodeBridge" "DisplayName" "CodeBridge 2.0.12-prealpha"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\CodeBridge" "DisplayVersion" "2.0.12"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\CodeBridge" "Publisher" "CodeBridge"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\CodeBridge" "UninstallString" '"$INSTDIR\Uninstall.exe"'
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\CodeBridge" "NoModify" 1
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\CodeBridge" "NoRepair" 1
SectionEnd

Section "Uninstall"
  Delete "$SMPROGRAMS\CodeBridge\CodeBridge.lnk"
  Delete "$SMPROGRAMS\CodeBridge\Configurar CodeBridge.lnk"
  Delete "$SMPROGRAMS\CodeBridge\Atualizar CodeBridge.lnk"
  RMDir "$SMPROGRAMS\CodeBridge"
  Delete "$DESKTOP\CodeBridge.lnk"
  DeleteRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\CodeBridge"
  ; Preserva os dados do usuario que ficam fora da pasta de instalacao.
  RMDir /r "$INSTDIR\app_rewrite"
  RMDir /r "$INSTDIR\author_mcp"
  RMDir /r "$INSTDIR\browser_extension"
  RMDir /r "$INSTDIR\docs"
  RMDir /r "$INSTDIR\rag"
  RMDir /r "$INSTDIR\assets"
  RMDir /r "$INSTDIR\installer"
  RMDir /r "$INSTDIR\tools"
  RMDir /r "$INSTDIR\runtime"
  Delete "$INSTDIR\README.md"
  Delete "$INSTDIR\requirements.txt"
  Delete "$INSTDIR\version.json"
  Delete "$INSTDIR\SOURCE_SNAPSHOT.md"
  Delete "$INSTDIR\INSTALLAR_OUTRO_PC.md"
  Delete "$INSTDIR\Uninstall.exe"
  RMDir "$INSTDIR"
SectionEnd
