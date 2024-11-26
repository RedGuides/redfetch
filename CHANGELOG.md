# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.6.0] - 2024-11-26

### Added
- TUI: Added server-select to Fetch tab.
- TUI: Added a unique default theme for each server type.

### Changed
- Resource names (as well as IDs) are now displayed when updating.
- TUI: EverQuest directory validation
- TUI: Check for presence of files for shortcuts
- TUI: The setting being changed will now appear in the log.
- TUI: Removed some emojis and colors that were especially ugly in Win10's conhost (cmd prompt).
- TUI: Uninstall confirmation.

### Fixed
- MQ will now terminate prior to unload. 

## [0.5.0] - 2024-11-22

### Added
- A bit of flair for initial setup.
- Detect directories from RedGuides Launcher.
- Detect EverQuest directory.  
- Added "themes" to the TUI. You can keep different themes for Live, Test, and Emulator. 
- Auto unload & close MacroQuest before update. 

### Changed
- Uninstall now removes settings and cache and logs the user out.
- Changed appearance to fit with the new theme system.

### Fixed
- true/false settings now work when set from the command line
- opting out of special resources no longer requires a restart
- A few fixes for linux (tested on ubuntu)

## [0.3.7] - 2024-10-29

### CHANGED
- Another README.md change.

## [0.3.6] - 2024-10-29

### Added
- Added a README.md.

### Changed
- Standardized name as `redfetch`

## [0.3.5] - 2024-10-28

### Changed
- And again, added a better check for pasting on Windows.

## [0.3.4] - 2024-10-28

### Changed
- Added a better check for pasting on Windows.

## [0.3.3] - 2024-10-28

### Changed
- Removed suggestion for users to paste dirs when running under conhost (cmd.exe)

## [0.3.2] - 2024-10-27

### Changed
- Update check will no longer trigger in CI environment.

## [0.3.1] - 2024-10-27

### Fixed
- Confirming a fix of the update check.

## [0.2.9] - 2024-10-27

### Added
- Added error handling to push commands, mostly for github actions pipeline.

## [0.2.8] - 2024-10-27

### Added
- Final pipeline test part 3. I hope it works.

## [0.2.7] - 2024-10-27

### Added
- Final pipeline test part 2. I hope it works.

## [0.2.6] - 2024-10-27

### Added
- Final pipeline test part 1. I hope it works.