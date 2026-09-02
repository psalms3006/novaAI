# NOVA — BUILD & DISTRIBUTION SYNCHRONIZATION RULE

**Status:** ACTIVE — permanent project-level engineering rule  
**Effective:** 2026-08-25  
**Applies to:** ALL future changes, implementations, fixes, refactors, UI modifications, features, and configuration changes

---

## Core Principle

ONE SOURCE OF TRUTH + MULTIPLE GENERATED DISTRIBUTIONS.

```
SOURCE
   │
   ├──→ DEVELOPMENT APP
   ├──→ PRODUCTION EXE
   ├──→ INSTALLER
   ├──→ PORTABLE BUILD
   └──→ ZIP / RELEASE PACKAGE
             │
             ↓
       ALL SAME RELEASE
```

Never allow:
```
SOURCE = NEW
EXE = OLD
INSTALLER = OLD
ZIP = OLD
```

---

## Completion Criteria

A change is **DONE** only when:

```
[✓] Source implementation complete
[✓] Tests/verification complete
[✓] Production desktop build updated
[✓] EXE updated
[✓] Installer updated
[✓] Portable/ZIP package updated where applicable
[✓] Assets/resources synchronized
[✓] Production artifact tested
[✓] No known stale maintained artifact remains
```

---

## After Every Meaningful Change

1. Update the source project.
2. Run the appropriate tests/verification.
3. Rebuild the desktop application.
4. Rebuild the executable.
5. Rebuild/update the installer.
6. Rebuild/update the distributable package.
7. Rebuild/update ZIP archives or portable packages.
8. Ensure bundled assets/configuration/resources are current.
9. Verify generated artifacts contain the new implementation.
10. Test the resulting distributable, not just the development version.

---

## Verification Requirements

- After EXE rebuild: verify the new EXE launches and contains updated code/assets
- After installer rebuild: install in clean environment, launch, verify features
- After ZIP rebuild: extract, launch, verify features
- Do NOT assume a successful build command = working artifact

---

## Judgment Clause

Use judgment during active implementation. Do not rebuild every distributable after every tiny intermediate edit.

However, **before declaring any implementation/change complete**, all maintained distributable artifacts must be synchronized.

For major milestones, feature completions, bug fixes intended for release, and user-facing changes: perform the complete build/package/verification cycle.

---

## Current Maintained Artifacts

- **Source**: `C:\Users\Lenovo\project-nova\`
- **EXE**: `C:\Users\Lenovo\project-nova\dist\NOVADesktop\NOVA.exe` (PyInstaller)
- **Build spec**: `C:\Users\Lenovo\project-nova\packaging\nova_desktop.spec`

### Build Commands

```bash
# Full rebuild (kill running NOVA.exe first if needed)
cd C:\Users\Lenovo\project-nova
pyinstaller packaging/nova_desktop.spec --clean --noconfirm

# Verify EXE exists and is current
Get-Item dist\NOVADesktop\NOVA.exe | Select-Object Length, LastWriteTime
```

---

## Stale Artifact Detection

Before considering a change complete, check for:
- Old EXEs
- Old installers
- Old ZIPs
- Duplicate builds
- Outdated bundled assets
- Stale frontend builds
- Stale configuration files

Do not leave confusing old releases where they could be accidentally distributed as latest.
