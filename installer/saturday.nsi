; Saturday per-user Windows installer (NSIS 3)
; Build from the installer/ dir:
;   makensis /DVERSION=0.8.0 saturday.nsi
;   (CI passes the release version: makensis /DVERSION=1.2.3 saturday.nsi)
; Prereq: python -m PyInstaller saturday.spec --noconfirm  (produces dist\Saturday)

; Paths below are relative to this script's directory (installer/), so makensis
; works from any CWD:  makensis -DVERSION=0.8.0 installer/saturday.nsi
!ifndef VERSION
!define VERSION "0.8.0"
!endif

!include "MUI2.nsh"

Name "Saturday"
OutFile "_output\Saturday-Setup-${VERSION}.exe"
InstallDir "$LOCALAPPDATA\Programs\Saturday"
RequestExecutionLevel user
SetCompressor /SOLID lzma
BrandingText "Saturday ${VERSION}"

!define MUI_ICON "..\packaging\icons\saturday.ico"
!define MUI_UNICON "..\packaging\icons\saturday.ico"
!define MUI_ABORTWARNING
!define MUI_FINISHPAGE_RUN "$INSTDIR\Saturday.exe"

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "English"

Section "Install"
  SetOutPath "$INSTDIR"
  File /r "..\dist\Saturday\*"
  WriteUninstaller "$INSTDIR\uninstall.exe"

  CreateDirectory "$SMPROGRAMS\Saturday"
  CreateShortcut "$SMPROGRAMS\Saturday\Saturday.lnk" "$INSTDIR\Saturday.exe"
  CreateShortcut "$SMPROGRAMS\Saturday\Uninstall Saturday.lnk" "$INSTDIR\uninstall.exe"

  ; standard per-user uninstall registry entry (users' ~/.saturday data is kept)
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\Saturday" "DisplayName" "Saturday"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\Saturday" "DisplayVersion" "${VERSION}"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\Saturday" "Publisher" "Saturday Labs"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\Saturday" "DisplayIcon" "$INSTDIR\Saturday.exe"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\Saturday" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\Saturday" "UninstallString" "$\"$INSTDIR\uninstall.exe$\""
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\Saturday" "NoModify" 1
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\Saturday" "NoRepair" 1
SectionEnd

Section "Uninstall"
  Delete "$SMPROGRAMS\Saturday\Saturday.lnk"
  Delete "$SMPROGRAMS\Saturday\Uninstall Saturday.lnk"
  RMDir "$SMPROGRAMS\Saturday"
  RMDir /r "$INSTDIR"
  DeleteRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\Saturday"
SectionEnd
