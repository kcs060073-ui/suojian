CREATE TABLE local_bgm_libraries (
 library_id TEXT PRIMARY KEY REFERENCES libraries(library_id),
 enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1))
);
