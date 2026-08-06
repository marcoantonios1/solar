import sqlite3
from datetime import datetime
from pathlib import Path

SOURCE_DB = "/mnt/edl-data/inverter.db"
BACKUP_DIR = Path("/home/marco/Documents/edl_solar_automation/backups")
KEEP_LAST_N_BACKUPS = 6


def main():
    BACKUP_DIR.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m")
    dest = BACKUP_DIR / f"inverter_backup_{timestamp}.db"

    # Use SQLite's own backup API instead of a plain file copy - this
    # correctly produces a consistent snapshot even while the live database
    # is actively being written to in WAL mode (a plain file copy of just
    # the main .db file can miss recent data still sitting in the -wal file)
    source_conn = sqlite3.connect(SOURCE_DB)
    dest_conn = sqlite3.connect(str(dest))

    with dest_conn:
        source_conn.backup(dest_conn)

    source_conn.close()
    dest_conn.close()

    print(f"Backed up database to {dest}")

    backups = sorted(BACKUP_DIR.glob("inverter_backup_*.db"))
    if len(backups) > KEEP_LAST_N_BACKUPS:
        for old_backup in backups[:-KEEP_LAST_N_BACKUPS]:
            old_backup.unlink()
            print(f"Removed old backup: {old_backup}")


if __name__ == "__main__":
    main()