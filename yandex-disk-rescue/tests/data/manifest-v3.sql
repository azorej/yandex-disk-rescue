-- A schema-v3 yandex-disk-rescue manifest: one mirror of disk:/ with a verified record, a partial record and
-- a partial record of a zero-byte file. `iterdump` omits the header pragmas, so they come first.
PRAGMA application_id = 1497649747;
PRAGMA user_version = 3;
BEGIN TRANSACTION;
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
INSERT INTO "downloads" VALUES(1,2,'verified',3,'900150983cd24fb0d6963f7d28e17f72','ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad','2024-05-01T12:30:00.000000+00:00','md5+sha256',3,1792260022572992280,171956);
INSERT INTO "downloads" VALUES(1,3,'partial',5000,'aa',NULL,'2024-05-01T12:30:00.000000+00:00',NULL,NULL,NULL,NULL);
INSERT INTO "downloads" VALUES(1,4,'partial',0,'d41d8cd98f00b204e9800998ecf8427e',NULL,'2024-05-01T12:30:00.000000+00:00',NULL,NULL,NULL,NULL);
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
INSERT INTO "entries" VALUES(4,'1','disk:/empty.txt','file',0,'d41d8cd98f00b204e9800998ecf8427e',NULL,'2024-05-01T12:30:00+00:00');
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
INSERT INTO "local_names" VALUES(1,4,'empty.txt');
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
INSERT INTO "scans" VALUES('1','disk:/','2026-10-17T18:00:22.575204+00:00');
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
