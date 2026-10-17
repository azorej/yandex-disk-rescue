-- A schema-v1 yandex-disk-rescue manifest: two committed scans (accounts 1 and 2).
-- `iterdump` omits the header pragmas, so they come first.
PRAGMA application_id = 1497649747;
PRAGMA user_version = 1;
BEGIN TRANSACTION;
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
INSERT INTO "entries" VALUES(3,'1','disk:/Photos/old.bmp','file',5,NULL,NULL,'2024-05-01T12:30:00+00:00');
INSERT INTO "entries" VALUES(4,'1','disk:/notes.txt','file',0,'d41d8cd98f00b204e9800998ecf8427e',NULL,'2024-05-01T12:30:00+00:00');
INSERT INTO "entries" VALUES(5,'2','disk:/other.txt','file',1,NULL,NULL,'2024-05-01T12:30:00+00:00');
CREATE TABLE scan_sessions (
            id INTEGER PRIMARY KEY,
            account_uid TEXT NOT NULL,
            root TEXT NOT NULL
        );
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
COMMIT;
