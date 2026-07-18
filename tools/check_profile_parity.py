#!/usr/bin/env python3
"""Diagnostic tool: check and repair profile file parity.

This tool verifies that all profiles listed in profiles.json have corresponding
text files, and repairs any missing files from the backup folder.

Run from RUMBLE directory: python tools/check_profile_parity.py
"""

import sys
from pathlib import Path

# Add RUMBLE to path
rumble_path = Path(__file__).parent.parent
sys.path.insert(0, str(rumble_path))

from rumble.core.profile_manager import ProfileManager
from rumble.core import apo_path


def main():
    print("[RUMBLE] Profile Parity Diagnostic Tool")
    print("=" * 50)

    # Initialize profile manager
    data_dir = rumble_path / "data"
    config_path = apo_path.get_config_path()

    print(f"\nData directory: {data_dir}")
    print(f"Config path: {config_path}")

    manager = ProfileManager(data_dir, config_path)

    # Check parity
    missing, total = manager.verify_profile_parity()
    print(f"\n[CHECK] Profile parity: {total - missing}/{total} files present")

    if missing == 0:
        print("[OK] All profiles have corresponding text files")
        return 0

    print(f"[WARNING] {missing} profile(s) missing text files")

    # Attempt repair
    print("\n[REPAIR] Rebuilding missing profile files...")
    rebuilt = manager.rebuild_missing_profile_files()
    print(f"[OK] Rebuilt {rebuilt} profile file(s)")

    # Export backup
    print("\n[BACKUP] Exporting all profiles to backup folder...")
    exported = manager.export_all_profiles_to_backup()
    print(f"[OK] Exported {exported} profile(s) to backup")

    # Save changes
    print("\n[SAVE] Saving profile metadata...")
    manager.save()
    print("[OK] Metadata saved")

    # Final check
    missing_after, _ = manager.verify_profile_parity()
    if missing_after == 0:
        print("\n[SUCCESS] Profile parity restored!")
        return 0
    else:
        print(f"\n[ERROR] {missing_after} profile(s) still missing")
        return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"\n[ERROR] {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
