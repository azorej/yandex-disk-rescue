-- A schema-v4 yandex-disk-rescue manifest: one mirror of disk:/ with a verified record and a partial record
-- whose journal confirms part of its first segment. `iterdump` omits the header pragmas, so they come first.
PRAGMA application_id = 1497649747;
PRAGMA user_version = 4;
BEGIN TRANSACTION;
CREATE TABLE download_segments (
            mirror_id INTEGER NOT NULL,
            entry_id INTEGER NOT NULL,
            start INTEGER NOT NULL,
            end INTEGER NOT NULL,
            confirmed INTEGER NOT NULL,
            digest TEXT,
            PRIMARY KEY (mirror_id, entry_id, start),
            FOREIGN KEY (mirror_id, entry_id) REFERENCES downloads (mirror_id, entry_id) ON DELETE CASCADE,
            CHECK (0 <= start AND start < end AND start <= confirmed AND confirmed <= end),
            CHECK ((confirmed = start) = (digest IS NULL))
        );
INSERT INTO "download_segments" VALUES(1,3,0,2500,1000,'abababababababababababababababababababababababababababababababab');
INSERT INTO "download_segments" VALUES(1,3,2500,5000,2500,NULL);
CREATE TABLE downloads (
            mirror_id INTEGER NOT NULL REFERENCES mirrors (id) ON DELETE CASCADE,
            entry_id INTEGER NOT NULL REFERENCES entries (id) ON DELETE CASCADE,
            state TEXT NOT NULL CHECK (state IN ('partial', 'verified')),
            size INTEGER NOT NULL CHECK (size >= 0),
            md5 TEXT,
            sha256 TEXT,
            modified TEXT NOT NULL,
            verified_by TEXT CHECK (verified_by IN ('md5+sha256', 'md5', 'sha256', 'size')),
            local_size INTEGER,
            local_mtime_ns INTEGER,
            local_ino INTEGER,
            PRIMARY KEY (mirror_id, entry_id),
            CHECK (
                (state = 'partial' AND verified_by IS NULL AND local_size IS NULL
                    AND local_mtime_ns IS NULL AND local_ino IS NULL)
                OR (state = 'verified' AND verified_by IS NOT NULL AND local_size IS NOT NULL
                    AND local_mtime_ns IS NOT NULL AND local_ino IS NOT NULL)
            )
        );
INSERT INTO "downloads" VALUES(1,2,'verified',3,'900150983cd24fb0d6963f7d28e17f72','ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad','2024-05-01T12:30:00.000000+00:00','md5+sha256',3,1792268819365464875,226118);
INSERT INTO "downloads" VALUES(1,3,'partial',5000,'aa',NULL,'2024-05-01T12:30:00.000000+00:00',NULL,NULL,NULL,NULL);
CREATE TABLE entries (
            id INTEGER PRIMARY KEY,
            account_uid TEXT NOT NULL,
            path TEXT NOT NULL,
            kind TEXT NOT NULL CHECK (kind IN ('file', 'dir')),
            size INTEGER CHECK (size >= 0),
            md5 TEXT,
            sha256 TEXT,
            modified TEXT NOT NULL,
            UNIQUE (account_uid, path)
        );
INSERT INTO "entries" VALUES(1,'1','disk:/Photos','dir',NULL,NULL,NULL,'2024-05-01T12:30:00+00:00');
INSERT INTO "entries" VALUES(2,'1','disk:/Photos/a.jpg','file',3,'900150983cd24fb0d6963f7d28e17f72','ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad','2024-05-01T12:30:00+00:00');
INSERT INTO "entries" VALUES(3,'1','disk:/Photos/big.bin','file',5000,'aa',NULL,'2024-05-01T12:30:00+00:00');
CREATE TABLE local_names (
            mirror_id INTEGER NOT NULL REFERENCES mirrors (id) ON DELETE CASCADE,
            entry_id INTEGER NOT NULL REFERENCES entries (id) ON DELETE CASCADE,
            local_path TEXT NOT NULL,
            PRIMARY KEY (mirror_id, entry_id),
            UNIQUE (mirror_id, local_path)
        );
INSERT INTO "local_names" VALUES(1,1,'Photos');
INSERT INTO "local_names" VALUES(1,2,'Photos/a.jpg');
INSERT INTO "local_names" VALUES(1,3,'Photos/big.bin');
CREATE TABLE mirrors (
            id INTEGER PRIMARY KEY,
            account_uid TEXT NOT NULL,
            remote_root TEXT NOT NULL,
            destination BLOB NOT NULL UNIQUE
        );
INSERT INTO "mirrors" VALUES(1,'1','disk:/',X'2F64657374');
CREATE TABLE scan_sessions (
            id INTEGER PRIMARY KEY,
            account_uid TEXT NOT NULL,
            root TEXT NOT NULL
        );
CREATE TABLE scans (
            account_uid TEXT NOT NULL,
            root TEXT NOT NULL,
            completed_at TEXT NOT NULL,
            PRIMARY KEY (account_uid, root)
        );
INSERT INTO "scans" VALUES('1','disk:/','2026-10-17T20:26:59.367800+00:00');
CREATE TABLE staged_entries (
            session_id INTEGER NOT NULL REFERENCES scan_sessions (id) ON DELETE CASCADE,
            path TEXT NOT NULL,
            kind TEXT NOT NULL CHECK (kind IN ('file', 'dir')),
            size INTEGER CHECK (size >= 0),
            md5 TEXT,
            sha256 TEXT,
            modified TEXT NOT NULL,
            PRIMARY KEY (session_id, path)
        );
CREATE TABLE unencodable_entries (
            account_uid TEXT NOT NULL,
            path BLOB NOT NULL,
            kind TEXT NOT NULL CHECK (kind IN ('file', 'dir')),
            PRIMARY KEY (account_uid, path)
        );
CREATE UNIQUE INDEX downloads_verified_ino ON downloads (mirror_id, local_ino) WHERE state = 'verified'
        ;
COMMIT;
